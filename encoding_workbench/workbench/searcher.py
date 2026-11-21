"""Revocable early-stop configuration searcher.

The searcher never assumes a full encode is available. It spends a bounded
frame budget on 30-frame sample windows, turns those windows into
shot-level confidence intervals, and only eliminates a candidate when the
interval (not a point estimate) proves one of two explicit hypotheses:

  R1 floor      the candidate's upper SSIM bound is below a shot floor,
                including the independent stricter floor on the burst shot
  R2 dominance  another candidate is both no worse at the upper SSIM bound
                and no more expensive at its lower byte bound

Every elimination is bound to its sampled window range, the metric used and
the numerical evidence, and carries a hypothesis id so a later full-encode
counterexample can reopen every candidate sharing that hypothesis. Predicted
aggregates are always tagged ``estimated``; full-encode rows are tagged
``measured_full`` and the two are never allowed to impersonate each other.
"""
import json
import math
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from search_common import (FLOORS_DEFAULT, ORDER_OVERRIDES, WINDOW_FRAMES,
                           Z95, SINGLE_WINDOW_SIGMA, SEARCH_RUNS, SHOTS,
                           config_grid, shot_frames, window_frames_of,
                           windows_of)
from search_encode import (full_job_id, load_full, load_windows, run_batch,
                           window_job_id)
from search_store import connect

# Frame budget. Phase 1 takes INITIAL_WINDOWS_PER_CONFIG windows per config.
# The static head has exactly four windows, so four initial windows under
# burst_mid reach the burst (head x4 would end exactly at the head boundary)
# while burst_end still spends them all on head + first fabric window: the
# two orderings visibly diverge. Phase 2 spends the rest one window at a time
# on the widest uncertainty interval.
INITIAL_WINDOWS_PER_CONFIG = 4
APPEND_ROUNDS = 6
MIN_EVIDENCE_WINDOWS = 1


# --------------------------------------------------------------------------- #
# Evidence containers
# --------------------------------------------------------------------------- #
@dataclass
class ShotEvidence:
    shot_id: str
    windows: list = field(default_factory=list)        # sampled window indices
    frame_ssims: dict = field(default_factory=dict)    # idx -> [frame ssim]
    window_means: dict = field(default_factory=dict)   # idx -> mean ssim
    window_bytes: dict = field(default_factory=dict)   # idx -> file bytes
    frames_seen: int = 0

    @property
    def seen(self):
        return bool(self.windows)

    def ssim_stats(self):
        """Mean SSIM plus a 95% interval built from between-window spread."""
        if not self.seen:
            return None
        means = [self.window_means[w] for w in self.windows]
        n = len(means)
        mean = sum(means) / n
        if n == 1:
            sigma = SINGLE_WINDOW_SIGMA
        else:
            var = sum((m - mean) ** 2 for m in means) / (n - 1)
            sigma = math.sqrt(var)
        half = Z95 * sigma / math.sqrt(n)
        return dict(n=n, mean=mean, lower=max(0.0, mean - half),
                    upper=min(1.0, mean + half), sigma=sigma, half=half)

    def byte_stats(self):
        if not self.seen:
            return None
        vals = [self.window_bytes[w] for w in self.windows]
        n = len(vals)
        mean = sum(vals) / n
        if n == 1:
            sd = mean * 0.15                       # conservative spread
        else:
            var = sum((v - mean) ** 2 for v in vals) / (n - 1)
            sd = math.sqrt(var)
        half = Z95 * sd / math.sqrt(n)
        return dict(n=n, mean=mean, lower=max(0, mean - half),
                    upper=mean + half)


@dataclass
class CandidateState:
    config: dict
    alive: bool = True
    evidence: dict = field(default_factory=dict)   # shot_id -> ShotEvidence
    eliminations: list = field(default_factory=list)
    reopened: list = field(default_factory=list)
    full_encode: dict = None
    lock: dict = None

    def shot(self, shot_id):
        ev = self.evidence.get(shot_id)
        if ev is None:
            ev = ShotEvidence(shot_id)
            self.evidence[shot_id] = ev
        return ev


# --------------------------------------------------------------------------- #
# Search run
# --------------------------------------------------------------------------- #
class SearchRun:
    def __init__(self, policy, order_key, floors=None,
                 budget_frames=None, run_id=None):
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.policy = policy                       # head_first | stratified
        self.order_key = order_key                 # burst_mid | burst_end
        self.order = ORDER_OVERRIDES[order_key]
        self.floors = dict(floors or FLOORS_DEFAULT)
        # Phase 1 cost: every config takes its first N windows in film order.
        phase1 = sum(
            self._first_window_cost(INITIAL_WINDOWS_PER_CONFIG)
            for _ in config_grid())
        self.budget_frames = budget_frames or (
            phase1 + APPEND_ROUNDS * WINDOW_FRAMES)
        self.frames_spent = 0
        self.grid = config_grid()
        self.candidates = {c["config_id"]: CandidateState(c) for c in self.grid}
        self.ledger = []                           # all elimination events
        self.append_log = []                       # uncertainty-driven adds
        self.lock_log = []
        self.audit = None
        self.role = "search"                       # search | exhaustive_reference
        self.created_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self._ingest_measurements()

    def _first_window_cost(self, n_windows):
        """Frames covered by the first n windows in film order (mixed
        60-frame head and 30-frame other-shot windows)."""
        total = 0
        remaining = n_windows
        for sid in self.order:
            take = min(remaining, len(windows_of(sid)))
            total += take * window_frames_of(sid)
            remaining -= take
            if remaining <= 0:
                break
        return total

    # -- persistence ------------------------------------------------------- #
    def path(self):
        if self.role == "exhaustive_reference":
            return SEARCH_RUNS / "_exhaustive_reference.json"
        return SEARCH_RUNS / f"{self.run_id}.json"

    def save(self):
        SEARCH_RUNS.mkdir(parents=True, exist_ok=True)
        with open(self.path(), "w") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)

    def to_dict(self):
        return dict(
            run_id=self.run_id, policy=self.policy, order_key=self.order_key,
            order=self.order, floors=self.floors,
            budget_frames=self.budget_frames,
            frames_spent=self.frames_spent,
            created_at=self.created_at,
            candidates={cid: self._cand_dict(st)
                        for cid, st in self.candidates.items()},
            ledger=self.ledger, append_log=self.append_log,
            lock_log=self.lock_log, audit=self.audit, role=self.role,
        )

    def _cand_dict(self, st):
        shots = {}
        for sid, ev in st.evidence.items():
            stats = ev.ssim_stats()
            bytes_ = ev.byte_stats()
            shots[sid] = dict(
                windows=ev.windows, frames_seen=ev.frames_seen,
                ssim=stats, bytes=bytes_,
                window_means=ev.window_means, window_bytes=ev.window_bytes,
                provenance="measured_windows" if ev.seen else "unseen",
            )
        film = self.film_estimate(st)
        return dict(config=st.config, alive=st.alive, shots=shots,
                    film_estimate=film, eliminations=st.eliminations,
                    reopened=st.reopened, full_encode=st.full_encode,
                    lock=st.lock)

    @classmethod
    def load(cls, run_id):
        path = SEARCH_RUNS / f"{run_id}.json"
        data = json.load(open(path))
        run = cls(policy=data["policy"], order_key=data["order_key"],
                  floors=data["floors"],
                  budget_frames=data.get("budget_frames"),
                  run_id=data["run_id"])
        run.frames_spent = data["frames_spent"]
        run.ledger = data["ledger"]
        run.append_log = data["append_log"]
        run.lock_log = data["lock_log"]
        run.audit = data.get("audit")
        run.role = data.get("role", "search")
        for cid, cd in data["candidates"].items():
            st = run.candidates[cid]
            st.alive = cd["alive"]
            st.eliminations = cd["eliminations"]
            st.reopened = cd["reopened"]
            st.full_encode = cd.get("full_encode")
            st.lock = cd.get("lock")
            for sid, sd in cd["shots"].items():
                # Evidence is reconstructed from the saved run state, not
                # re-pulled from the shared SQLite cache: another run's
                # windows must not leak into this run's frame budget.
                ev = ShotEvidence(sid, windows=list(sd["windows"]),
                                  frames_seen=sd["frames_seen"],
                                  window_means={int(k): v for k, v in
                                                sd["window_means"].items()},
                                  window_bytes={int(k): v for k, v in
                                                sd["window_bytes"].items()})
                st.evidence[sid] = ev
        return run

    # -- measurements from the shared SQLite cache ------------------------- #
    def _ingest_measurements(self, include_full=False, full_config_ids=None):
        """Pull window rows attached to this run's reserved evidence slots.
        Window rows belonging to other runs are ignored so a run only sees the
        sample ranges it scheduled itself. Full-encode rows are ground truth
        but are only pulled when explicitly requested *and* for the named
        configs (the exhaustive reference pulls every config; a panel lock
        pulls only the one config it just paid for). Without the whitelist a
        search run would impersonate its window estimates with full-encode
        measurements produced by another run's exhaustive reference."""
        conn = connect()
        reserved = {
            (cid, sid, w)
            for cid, st in self.candidates.items()
            for sid, ev in st.evidence.items() for w in ev.windows
        }
        for row in load_windows(conn):
            key = (row["config_id"], row["shot_id"], row["window_idx"])
            if key not in reserved:
                continue
            st = self.candidates[row["config_id"]]
            ev = st.shot(row["shot_id"])
            ev.window_means[row["window_idx"]] = row["ssim_avg"]
            ev.window_bytes[row["window_idx"]] = row["file_bytes"]
            ev.frame_ssims[row["window_idx"]] = json.loads(
                row["frame_ssims"] or "[]")
            ev.frames_seen = len(ev.windows) * window_frames_of(
                row["shot_id"])
        allowed = set(full_config_ids or ())
        if include_full:
            for row in load_full(conn):
                if row["config_id"] not in allowed:
                    continue
                st = self.candidates.get(row["config_id"])
                if st is None or row["shot_id"] not in \
                        {s["id"] for s in SHOTS}:
                    continue
                st.full_encode = st.full_encode or {}
                st.full_encode[row["shot_id"]] = dict(
                    provenance="measured_full",
                    ssim_avg=row["ssim_avg"], ssim_min=row["ssim_min"],
                    file_bytes=row["file_bytes"], frames=row["frames"],
                    encode_secs=row["encode_secs"], job_id=row["job_id"])
        conn.close()

    # -- estimates --------------------------------------------------------- #
    def film_estimate(self, st):
        """Frame-weighted whole-film estimate. If every shot has a real
        full-encode row the result is tagged measured_full; otherwise it is
        extrapolated from sampled windows and tagged estimated. Unknown shots
        block a numeric CI and are disclosed explicitly."""
        total = sum(shot_frames(s["id"]) for s in SHOTS)
        if st.full_encode and len(st.full_encode) == len(SHOTS):
            fe = st.full_encode
            ssim = sum(fe[s["id"]]["ssim_avg"] * shot_frames(s["id"])
                       for s in SHOTS) / total
            bytes_ = sum(fe[s["id"]]["file_bytes"] for s in SHOTS)
            return dict(provenance="measured_full", ssim_mean=round(ssim, 6),
                        ssim_lower=round(ssim, 6), ssim_upper=round(ssim, 6),
                        bytes_mean=bytes_, bytes_lower=bytes_,
                        bytes_upper=bytes_, coverage=1.0,
                        known_shots=[s["id"] for s in SHOTS],
                        unknown_shots=[])
        num, seen_frames = 0.0, 0
        var_sum, byte_sum, byte_lo, byte_hi = 0.0, 0.0, 0.0, 0.0
        unknown = []
        for shot in SHOTS:
            sid = shot["id"]
            w = shot_frames(sid)
            ev = st.evidence.get(sid)
            stats = ev.ssim_stats() if ev else None
            if stats is None:
                unknown.append(sid)
                continue
            num += stats["mean"] * w
            var_sum += (stats["half"] ** 2) * (w / total) ** 2
            seen_frames += w
            b = ev.byte_stats()
            byte_sum += b["mean"]
            byte_lo += b["lower"]
            byte_hi += b["upper"]
        if not seen_frames:
            return dict(provenance="unseen", known_shots=[], coverage=0.0)
        # Mean over observed frames only (unseen shots are disclosed via
        # unknown_shots); the interval keeps whole-film frame weighting.
        half = math.sqrt(var_sum)
        mean = num / seen_frames
        return dict(
            provenance="estimated",
            ssim_mean=mean,
            ssim_lower=max(0.0, mean - half),
            ssim_upper=min(1.0, mean + half),
            bytes_mean=round(byte_sum), bytes_lower=round(byte_lo),
            bytes_upper=round(byte_hi),
            coverage=round(seen_frames / total, 4),
            known_shots=[s["id"] for s in SHOTS
                         if st.evidence.get(s["id"]) and
                         st.evidence[s["id"]].seen],
            unknown_shots=unknown,
        )

    # -- window scheduling ------------------------------------------------- #
    def _next_head_first(self):
        """Round-robin over configs: each call returns the next config's own
        earliest unseen window in film order. Configs are ranked by their
        film-order cursor so a config that exhausts a short shot moves on to
        the next shot immediately, instead of waiting for longer shots. This
        makes moving the burst to the end visibly delay burst sampling."""
        def cursor(cid):
            """(shot index, next window index) of this config's next slot."""
            st = self.candidates[cid]
            for sid in self.order:
                have = set(st.evidence.get(sid, ShotEvidence(sid)).windows)
                unseen = [w for w in windows_of(sid) if w not in have]
                if unseen:
                    return (self.order.index(sid), unseen[0])
            return (len(self.order), 0)
        alive = sorted((c for c in self.candidates
                        if self.candidates[c].alive),
                       key=lambda c: (cursor(c), c))
        if not alive:
            return None
        cid = alive[0]
        st = self.candidates[cid]
        for sid in self.order:
            have = set(st.evidence.get(sid, ShotEvidence(sid)).windows)
            unseen = [w for w in windows_of(sid) if w not in have]
            if unseen:
                return (st.config, sid, unseen[0])
        return None

    def _next_stratified(self):
        """Pick the (config, shot) cell with the fewest sampled windows so
        every shot is touched once before any shot gets a second window;
        ties follow film order, then config id for determinism."""
        best = None
        for cid, st in self.candidates.items():
            if not st.alive:
                continue
            for sid in self.order:
                ev = st.evidence.get(sid)
                have = set(ev.windows) if ev else set()
                unseen = [w for w in windows_of(sid) if w not in have]
                if not unseen:
                    continue
                key = (len(have), self.order.index(sid), unseen[0], cid)
                if best is None or key < best[0]:
                    best = (key, (st.config, sid, unseen[0]))
        return best[1] if best else None

    def _next_uncertainty(self):
        """Append budget goes to the alive (config, shot) cell whose SSIM
        interval is widest, measured relative to the floor gap."""
        best = None
        for cid, st in self.candidates.items():
            if not st.alive:
                continue
            for sid in self.order:
                ev = st.evidence.get(sid)
                if ev is None or not ev.seen:
                    continue
                have = set(ev.windows)
                unseen = [w for w in windows_of(sid) if w not in have]
                if not unseen:
                    continue
                stats = ev.ssim_stats()
                pressure = stats["half"] + max(
                    0.0, self.floors[sid] - stats["lower"])
                key = (pressure, cid, sid, unseen[0])
                if best is None or key > best[0]:
                    best = (key, (st.config, sid, unseen[0]),
                            dict(half=stats["half"], pressure=pressure,
                                 floor=self.floors[sid]))
        return (best[1], best[2]) if best else (None, None)

    # -- main loop --------------------------------------------------------- #
    def run(self, workers=6):
        # Phase 1: stratified/head-first initial allocation, bounded by the
        # frame budget rather than a window count (window lengths differ).
        phase1_frames = sum(
            self._first_window_cost(INITIAL_WINDOWS_PER_CONFIG)
            for _ in self.grid)
        self._drive(min(self.budget_frames, phase1_frames),
                    uncertainty=False, workers=workers)
        self._eliminate(round_no=0)
        rounds = 0
        while (self.frames_spent < self.budget_frames
               and rounds < APPEND_ROUNDS and self._alive_count() > 1):
            task, why = self._next_uncertainty()
            if task is None:
                break
            self._drive(self.budget_frames, uncertainty=True, why=why,
                        workers=workers, single=True)
            self._eliminate(round_no=rounds + 1)
            rounds += 1
        self.save()
        return self.to_dict()

    def _drive(self, target_frames, uncertainty, why=None, workers=6,
               single=False):
        scheduler = self._next_stratified if self.policy == "stratified" \
            else self._next_head_first
        tasks = []
        while self.frames_spent < target_frames:
            if uncertainty:
                task, _ = self._next_uncertainty()
            else:
                task = scheduler()
            if task is None:
                break
            config, sid, w = task
            win_frames = window_frames_of(sid)
            if self.frames_spent + win_frames > target_frames:
                break
            tasks.append((config, sid, w))
            ev = self.candidates[config["config_id"]].shot(sid)
            ev.windows.append(w)          # reserve so scheduler skips it
            ev.frames_seen += win_frames
            self.frames_spent += win_frames
            if uncertainty:
                self.append_log.append(dict(
                    config_id=config["config_id"], shot_id=sid, window=w,
                    reason="uncertainty",
                    interval_half=round(why["half"], 6) if why else None,
                    pressure=round(why["pressure"], 6) if why else None))
            if single:
                break
        if tasks:
            run_batch(tasks, kind="window", workers=workers)
            self._ingest_measurements()

    # -- elimination ------------------------------------------------------- #
    def _alive_count(self):
        return sum(1 for st in self.candidates.values() if st.alive)

    def _eliminate(self, round_no):
        for cid, st in self.candidates.items():
            if not st.alive:
                continue
            for shot in SHOTS:
                sid = shot["id"]
                ev = st.evidence.get(sid)
                stats = ev.ssim_stats() if ev else None
                if stats is None or stats["n"] < MIN_EVIDENCE_WINDOWS:
                    continue
                floor = self.floors[sid]
                if stats["upper"] < floor:
                    self._record_elimination(
                        st, rule="R1_floor", round_no=round_no, shot_id=sid,
                        hypothesis=f"R1:{sid}:floor<{floor}",
                        metric="ssim_upper_vs_floor",
                        evidence=dict(
                            shot_id=sid, floor=floor,
                            ssim_upper=round(stats["upper"], 6),
                            ssim_mean=round(stats["mean"], 6),
                            ssim_lower=round(stats["lower"], 6),
                            sampled_windows=list(ev.windows),
                            sampled_frame_range=self._frame_range(ev, sid),
                            window_means={str(k): v for k, v in
                                          ev.window_means.items()},
                            provenance="measured_windows"))
                    break
            if not st.alive:
                continue
            film = self.film_estimate(st)
            if film["provenance"] == "unseen":
                continue
            for rid, rival in self.candidates.items():
                if rid == cid or not rival.alive:
                    continue
                rf = self.film_estimate(rival)
                if rf["provenance"] == "unseen":
                    continue
                # Confidence-safe dominance: rival's worst quality still beats
                # this candidate's best, and rival's most expensive estimate
                # is still no pricier than this candidate's cheapest.
                quality_dom = rf["ssim_lower"] >= film["ssim_upper"]
                byte_dom = rf["bytes_upper"] <= film["bytes_lower"]
                if quality_dom and byte_dom:
                    self._record_elimination(
                        st, rule="R2_dominance", round_no=round_no,
                        shot_id="FILM",
                        hypothesis=f"R2:dominated_by:{rid}",
                        metric="ssim_interval_and_byte_interval",
                        evidence=dict(
                            rival=rid,
                            victim_ssim_upper=film["ssim_upper"],
                            rival_ssim_lower=rf["ssim_lower"],
                            victim_bytes_lower=film["bytes_lower"],
                            rival_bytes_upper=rf["bytes_upper"],
                            victim_known_shots=film["known_shots"],
                            rival_known_shots=rf["known_shots"],
                            provenance="estimated_from_measured_windows"))
                    break

    def _frame_range(self, ev, shot_id):
        if not ev.windows:
            return None
        wf = window_frames_of(shot_id)
        lo = min(ev.windows) * wf
        hi = (max(ev.windows) + 1) * wf - 1
        return dict(shot_id=shot_id, start_frame=lo, end_frame=hi,
                    frames=(hi - lo + 1), note="窗口可能不连续，以窗口清单为准")

    def _record_elimination(self, st, rule, round_no, shot_id, hypothesis,
                            metric, evidence):
        event = dict(elim_id=uuid.uuid4().hex[:10],
                     config_id=st.config["config_id"], rule=rule,
                     round=round_no, shot_id=shot_id,
                     hypothesis=hypothesis, metric=metric,
                     evidence=evidence,
                     created_at=time.strftime("%H:%M:%S"),
                     status="eliminated")
        st.alive = False
        st.eliminations.append(event)
        self.ledger.append(event)

    # -- lock / full encode / counterexample / reopen ---------------------- #
    def lock_and_full_encode(self, config_id, workers=3):
        """Panel lock: fully encode every shot of one excluded config.

        Frames already present in the shared cache are reused, not re-encoded;
        the entry reports both newly encoded and reused frame counts so the
        panel can show the real marginal cost of the lock. Only this config's
        full-encode rows are ingested into the run: other rows in the shared
        cache stay ground truth for audits, never evidence for this run."""
        st = self.candidates[config_id]
        tasks = [(st.config, s["id"]) for s in SHOTS]
        before = set(self._full_job_ids())
        run_batch(tasks, kind="full", workers=workers)
        self._ingest_measurements(include_full=True,
                                  full_config_ids={config_id})
        after = set(self._full_job_ids())
        new_jobs = sorted(after - before)
        new_frames = self._frames_of_full_jobs(new_jobs)
        film_frames = sum(shot_frames(s["id"]) for s in SHOTS)
        st.lock = dict(locked_at=time.strftime("%Y-%m-%d %H:%M:%S"),
                       reason="面板锁定被排除配置，补做整片完整编码",
                       frames=film_frames, newly_encoded_frames=new_frames,
                       reused_cached_frames=film_frames - new_frames)
        entry = dict(config_id=config_id, full_jobs=new_jobs,
                     frames=film_frames,
                     newly_encoded_frames=new_frames,
                     reused_cached_frames=film_frames - new_frames,
                     counterexample=None, reopened=[])
        counter = self._evaluate_counterexample(st)
        if counter:
            entry["counterexample"] = counter
            entry["reopened"] = self._reopen_hypothesis(st, counter)
        self.lock_log.append(entry)
        self.save()
        return entry

    def _full_job_ids(self):
        conn = connect()
        ids = {r["job_id"] for r in conn.execute(
            "SELECT job_id FROM full_encodes WHERE status='ok'")}
        conn.close()
        return ids

    def _frames_of_full_jobs(self, job_ids):
        if not job_ids:
            return 0
        conn = connect()
        placeholders = ",".join("?" for _ in job_ids)
        total = conn.execute(
            f"SELECT COALESCE(SUM(frames),0) FROM full_encodes "
            f"WHERE status='ok' AND job_id IN ({placeholders})",
            tuple(job_ids)).fetchone()[0]
        conn.close()
        return total

    def _store_full_encode(self, config_id):
        """Read one config's real full-encode rows from the shared cache
        without attaching them to run state. Used only when judging a lock
        counterexample, so a rival's ground-truth measurement can refute a
        dominance hypothesis without being presented as this run's evidence."""
        conn = connect()
        rows = load_full(conn)
        conn.close()
        out = {}
        for row in rows:
            if row["config_id"] != config_id:
                continue
            out[row["shot_id"]] = dict(
                provenance="measured_full",
                ssim_avg=row["ssim_avg"], ssim_min=row["ssim_min"],
                file_bytes=row["file_bytes"], frames=row["frames"],
                encode_secs=row["encode_secs"], job_id=row["job_id"])
        return out

    def _evaluate_counterexample(self, st):
        """A full encode is a counterexample when it contradicts the exact
        hypothesis the config was eliminated under."""
        if not st.eliminations or not st.full_encode:
            return None
        last = st.eliminations[-1]
        full = st.full_encode
        if last["rule"] == "R1_floor":
            sid = last["shot_id"]
            real = full[sid]["ssim_avg"]
            if real >= self.floors[sid]:
                return dict(rule=last["rule"], shot_id=sid,
                            eliminated_belief=dict(
                                ssim_upper=last["evidence"]["ssim_upper"],
                                floor=self.floors[sid]),
                            measured_full=dict(ssim_avg=real,
                                               ssim_min=full[sid]["ssim_min"],
                                               frames=full[sid]["frames"]),
                            explanation="完整编码 SSIM 达到门槛，"
                                        "抽样区间给出的上限判断被证伪。")
        if last["rule"] == "R2_dominance":
            rival_id = last["evidence"]["rival"]
            # The rival was never locked in this run, so its full encode is
            # not part of run state; read the real rows from the shared cache
            # and disclose them as store-sourced ground truth in the verdict.
            rival_full = self.candidates[rival_id].full_encode \
                or self._store_full_encode(rival_id)
            if rival_full and len(rival_full) == len(SHOTS):
                v_q = min(full[s]["ssim_avg"] for s in full)
                r_q = min(rival_full[s]["ssim_avg"]
                          for s in rival_full)
                v_b = sum(full[s]["file_bytes"] for s in full)
                r_b = sum(rival_full[s]["file_bytes"]
                          for s in rival_full)
                if v_q > r_q or v_b < r_b:
                    return dict(rule=last["rule"], rival=rival_id,
                                rival_evidence="measured_full_from_shared_store",
                                victim_full_ssim_min=v_q,
                                rival_full_ssim_min=r_q,
                                victim_full_bytes=v_b,
                                rival_full_bytes=r_b,
                                explanation="完整编码后该配置在质量或字节上反超，"
                                            "支配假设被证伪。")
        return None

    def _reopen_hypothesis(self, st, counter):
        """Reopen every candidate bound to the same falsified hypothesis."""
        hyp = st.eliminations[-1]["hypothesis"]
        reopened = []
        for cid, other in self.candidates.items():
            if other.alive:
                continue
            for ev in other.eliminations:
                if ev["hypothesis"] == hyp and ev["status"] == "eliminated":
                    ev["status"] = "reopened"
                    ev["reopened_by"] = st.config["config_id"]
                    ev["reopened_at"] = time.strftime("%H:%M:%S")
                    other.alive = True
                    other.reopened.append(dict(
                        hypothesis=hyp, via=st.config["config_id"],
                        counterexample=counter["rule"]))
                    reopened.append(cid)
                    break
        return reopened

    # -- exhaustive reference + audit -------------------------------------- #
    def build_exhaustive(self, workers=6):
        """Small-scale exhaustive ground truth: full encode of every config
        on every shot, then the true whole-film Pareto frontier."""
        self.role = "exhaustive_reference"
        tasks = [(c, s["id"]) for c in self.grid for s in SHOTS]
        run_batch(tasks, kind="full", workers=workers)
        self._ingest_measurements(
            include_full=True,
            full_config_ids={c["config_id"] for c in self.grid})
        truth = self._ground_truth()
        self.audit = self._audit_against(truth)
        self.save()
        return self.audit

    def _ground_truth(self):
        points = []
        for cid, st in self.candidates.items():
            if not st.full_encode or len(st.full_encode) < len(SHOTS):
                continue
            fe = st.full_encode
            total_frames = sum(fe[s["id"]]["frames"] for s in SHOTS)
            ssim = sum(fe[s["id"]]["ssim_avg"] * fe[s["id"]]["frames"]
                       for s in SHOTS) / total_frames
            bytes_ = sum(fe[s["id"]]["file_bytes"] for s in SHOTS)
            floor_pass = all(fe[s["id"]]["ssim_avg"] >= self.floors[s["id"]]
                             for s in SHOTS)
            points.append(dict(config_id=cid, ssim_avg=round(ssim, 6),
                               file_bytes=bytes_, floor_pass=floor_pass,
                               per_shot={s["id"]: fe[s["id"]]["ssim_avg"]
                                         for s in SHOTS}))
        eligible = [p for p in points if p["floor_pass"]]
        frontier = []
        for p in sorted(eligible, key=lambda x: x["file_bytes"]):
            if not frontier or p["ssim_avg"] > frontier[-1]["ssim_avg"] + 1e-9:
                frontier.append(p)
        return dict(points=points, frontier=frontier)

    def _audit_against(self, truth):
        frontier_ids = {p["config_id"] for p in truth["frontier"]}
        eliminated = {cid for cid, st in self.candidates.items()
                      if not st.alive}
        survivors = {cid for cid, st in self.candidates.items()
                     if st.alive}
        missed = sorted(frontier_ids & eliminated)
        # A lock counterexample can reopen a frontier config: it is no longer
        # eliminated, but it was still abandoned early and only rescued by the
        # paid full encode, so track it separately instead of hiding it.
        reopened_ids = {
            cid for cid, st in self.candidates.items()
            for ev in st.eliminations if ev["status"] == "reopened"}
        rescued = sorted(frontier_ids & reopened_ids)
        full_frames_per_film = sum(shot_frames(s["id"]) for s in SHOTS)
        exhaustive_cost = len(self.grid) * full_frames_per_film
        lock_frames = sum(e.get("frames", 0) for e in self.lock_log)
        lock_new = sum(e.get("newly_encoded_frames", 0) for e in self.lock_log)
        locked_ids = {e["config_id"] for e in self.lock_log}
        # Frames really encoded *for this run*: windows scheduled by this run
        # plus its locked full encodes. The shared SQLite store may hold other
        # runs' windows and the exhaustive reference; those are not counted.
        win_frames = sum(
            len(ev.windows) * window_frames_of(sid)
            for st in self.candidates.values()
            for sid, ev in st.evidence.items())
        full_frames = sum(
            fe["frames"] for e in self.lock_log
            for fe in self.candidates[e["config_id"]].full_encode.values())
        conn = connect()
        store_full = conn.execute(
            "SELECT COALESCE(SUM(frames),0) FROM full_encodes WHERE status='ok'"
        ).fetchone()[0]
        conn.close()
        return dict(
            exhaustive_frontier=[
                dict(config_id=p["config_id"], file_bytes=p["file_bytes"],
                     ssim_avg=p["ssim_avg"]) for p in truth["frontier"]],
            missed_frontier_configs=missed,
            missed_count=len(missed),
            rescued_frontier_configs=rescued,
            abandoned_frontier_count=len(missed) + len(rescued),
            survivor_count=len(survivors),
            eliminated_count=len(eliminated),
            exhaustive_cost_frames=exhaustive_cost,
            search_window_frames=win_frames,
            lock_full_frames=lock_frames,
            lock_newly_encoded_frames=lock_new,
            lock_reused_cached_frames=lock_frames - lock_new,
            total_full_frames_in_store=store_full,
            really_encoded_frames=win_frames + full_frames,
            saved_frames_vs_exhaustive=exhaustive_cost - win_frames,
            saved_pct=round(100 * (exhaustive_cost - win_frames)
                            / exhaustive_cost, 2),
            locked_configs=sorted(locked_ids),
            counterexamples=[e for e in self.lock_log if e["counterexample"]],
            note=("漏解 = 完整编码前沿上却被早停淘汰的配置；"
                  "节省帧数只统计真实跑过的编码，预测值不计入。"))


def list_runs():
    if not SEARCH_RUNS.exists():
        return []
    out = []
    for p in sorted(SEARCH_RUNS.glob("*.json")):
        d = json.load(open(p))
        if d.get("role", "search") != "search":
            continue
        out.append(dict(run_id=d["run_id"], policy=d["policy"],
                        order_key=d["order_key"],
                        frames_spent=d["frames_spent"],
                        budget_frames=d["budget_frames"],
                        alive=sum(1 for c in d["candidates"].values()
                                  if c["alive"]),
                        eliminated=sum(1 for c in d["candidates"].values()
                                       if not c["alive"]),
                        created_at=d["created_at"],
                        audited=bool(d.get("audit"))))
    return out
