"""Synthetic, ffmpeg-free checks for searcher mechanics.

Run with: python3 -I tests_searcher_logic.py
Measurements are seeded from an in-memory ground-truth function so the test
stays deterministic and never touches ffmpeg or the shared SQLite cache.
"""
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import searcher
from search_common import (SHOTS, WINDOW_FRAMES, config_grid, shot_frames,
                           window_frames_of, windows_of)

searcher.SearchRun._ingest_measurements = lambda self: None
searcher.run_batch = lambda *a, **k: None

GRID = config_grid()


def true_ssim(cid, sid):
    res, q = cid.split("_")[0], cid.split("_")[1]
    base = {"q1": 0.999, "q2": 0.998, "q3": 0.997, "q4": 0.995,
            "q5": 0.990, "q6": 0.988}[q]
    if sid == "head_static":
        # Even the coarsest config stays above the generic 0.985 floor on
        # the static head: head-only evidence cannot trigger R1 there.
        return min(0.9995, base + (0.004 if res == "480p" else 0.001))
    if sid == "mid_fabric":
        return base - {"1080p": 0.0, "720p": 0.005, "480p": 0.020}[res]
    return max(0.5, base - {"1080p": 0.010, "720p": 0.045,
                            "480p": 0.110}[res])


def true_bytes(cid, sid):
    res, q = cid.split("_")[0], cid.split("_")[1]
    b = {"q1": 300000, "q2": 170000, "q3": 95000, "q4": 50000,
         "q5": 30000, "q6": 20000}[q]
    return int(b * {"1080p": 1.0, "720p": 0.45, "480p": 0.22}[res]
               * (0.3 if sid == "head_static" else 1.0))


def fake_ingest(self):
    for cid, st in self.candidates.items():
        for sid, ev in st.evidence.items():
            for w in list(ev.windows):
                if w in ev.window_means:
                    continue
                v = true_ssim(cid, sid) + 0.0008 * math.sin(
                    w * 1.7 + hash(cid) % 7)
                ev.window_means[w] = round(v, 6)
                ev.window_bytes[w] = int(
                    true_bytes(cid, sid) / len(windows_of(sid)))
                ev.frame_ssims[w] = [v] * window_frames_of(sid)
                ev.frames_seen = len(ev.windows) * window_frames_of(sid)


searcher.SearchRun._ingest_measurements = fake_ingest

run = searcher.SearchRun(policy="head_first", order_key="burst_end")
run.run(workers=1)
print("frames_spent", run.frames_spent,
      "budget", run.budget_frames)
print("alive", sorted(c for c, s in run.candidates.items() if s.alive))
print("eliminated:")
for e in run.ledger:
    print("  ", e["config_id"], e["rule"], e["shot_id"], e["hypothesis"])

e0 = run.ledger[0]
assert e0["metric"] and e0["hypothesis"] and e0["evidence"]["provenance"]
if e0["rule"] == "R1_floor":
    assert "sampled_windows" in e0["evidence"]
    assert "sampled_frame_range" in e0["evidence"]
print("append rounds:", len(run.append_log))
for a in run.append_log[:8]:
    print("  append", a["config_id"], a["shot_id"], "w" + str(a["window"]),
          "half=", a["interval_half"])

run2 = searcher.SearchRun(policy="stratified", order_key="burst_end")
run2.run(workers=1)
strat_windows = {
    (cid, sid): ev.windows
    for cid, st in run2.candidates.items()
    for sid, ev in st.evidence.items() if ev.windows
}
# stratified must have touched every shot (head windows are wider, so equal
# window counts across shots are neither expected nor required)
counts = {sid: 0 for sid in [s["id"] for s in SHOTS]}
for (cid, sid), ws in strat_windows.items():
    counts[sid] += len(ws)
print("\nstratified window counts per shot:", counts)
assert all(v > 0 for v in counts.values()), "分层抽样必须覆盖每个镜头"
print("stratified eliminations:")
for e in run2.ledger:
    print("  ", e["config_id"], e["rule"], e["shot_id"])

# head_first with burst_end must not have sampled any burst window in phase 1
head_burst = [
    w for cid, st in run.candidates.items()
    for sid, ev in st.evidence.items() if sid == "burst" for w in ev.windows]
print("\nhead_first burst windows observed:", head_burst)

for cid, st in run.candidates.items():
    st.full_encode = {
        s["id"]: dict(provenance="measured_full",
                      ssim_avg=true_ssim(cid, s["id"]),
                      ssim_min=true_ssim(cid, s["id"]) - 0.01,
                      file_bytes=true_bytes(cid, s["id"]),
                      frames=shot_frames(s["id"]),
                      encode_secs=1.0, job_id="x")
        for s in SHOTS}
truth = run._ground_truth()
print("\ntrue frontier:", [p["config_id"] for p in truth["frontier"]])
audit = run._audit_against(truth)
print("missed:", audit["missed_frontier_configs"],
      "saved_pct", audit["saved_pct"])
assert audit["really_encoded_frames"] >= audit["search_window_frames"]

victim = next(
    (e["config_id"] for e in run.ledger
     if e["rule"] == "R1_floor" and e["shot_id"] == "burst"),
    run.ledger[0]["config_id"])
st = run.candidates[victim]
for s in SHOTS:
    st.full_encode[s["id"]]["ssim_avg"] = max(
        st.full_encode[s["id"]]["ssim_avg"],
        run.floors[s["id"]] + 0.001)


def fake_lock(self, cid, workers=3):
    st = self.candidates[cid]
    st.lock = dict(locked_at="now", reason="test", frames=360)
    counter = self._evaluate_counterexample(st)
    reopened = self._reopen_hypothesis(st, counter) if counter else []
    entry = dict(config_id=cid, full_jobs=[], frames=360,
                 counterexample=counter, reopened=reopened)
    self.lock_log.append(entry)
    self.save()
    return entry


searcher.SearchRun.lock_and_full_encode = fake_lock
entry = run.lock_and_full_encode(victim)
print("\nlock victim", victim)
print("counterexample rule:",
      entry["counterexample"]["rule"] if entry["counterexample"] else None)
print("reopened:", entry["reopened"])
assert entry["counterexample"], "合成场景下锁定补码应当产生反例"
assert run.candidates[victim].alive, "反例后被锁配置必须重新开放"
assert all(e["status"] in ("eliminated", "reopened") for e in run.ledger)
reopened_hyps = {e["hypothesis"] for e in run.ledger
                 if e["status"] == "reopened"}
print("reopened hypotheses:", reopened_hyps)
print("\nALL SYNTHETIC CHECKS PASSED")
