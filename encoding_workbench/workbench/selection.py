"""Rate-distortion frontiers and bit-budget allocation strategies.

Two selectors are implemented on the same measured candidate set:

* greedy  -- per-segment marginal-gain allocation (a classic but myopic
  heuristic: it always buys the cheapest local quality step)
* joint   -- exact whole-film allocation by dynamic programming (a
  multi-choice knapsack over all segments at once)

A feasible run demonstrates the two reaching opposite conclusions;
an infeasible run (budget below the cheapest quality-floor combination)
demonstrates constraints that cannot be satisfied together.
"""
import json
from dataclasses import dataclass, asdict

from db import connect
from paths import PLAN_PATH, SEGMENTS


@dataclass
class Candidate:
    job_id: str
    segment_id: str
    width: int
    height: int
    quality_label: str
    res_label: str
    q_label: str
    crf: int
    file_bytes: int
    stream_bytes: int
    ssim_avg: float
    ssim_min: float
    psnr_avg: float
    bitrate_meas: int
    encode_secs: float
    output_path: str
    on_frontier: bool = False


def load_candidates(conn):
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status='ok' AND ssim_avg IS NOT NULL"
    ).fetchall()
    cands = []
    for r in rows:
        extra = json.loads(r["extra"] or "{}")
        cands.append(Candidate(
            job_id=r["job_id"], segment_id=r["segment_id"],
            width=r["width"], height=r["height"],
            quality_label=r["quality_label"],
            res_label=extra.get("res_label", "?"),
            q_label=extra.get("q_label", "?"),
            crf=r["crf"], file_bytes=r["file_bytes"],
            stream_bytes=r["stream_bytes"], ssim_avg=r["ssim_avg"],
            ssim_min=r["ssim_min"], psnr_avg=r["psnr_avg"],
            bitrate_meas=r["bitrate_meas"], encode_secs=r["encode_secs"],
            output_path=r["output_path"],
        ))
    by_seg = {s["id"]: [] for s in SEGMENTS}
    for c in cands:
        by_seg[c.segment_id].append(c)
    for sid, lst in by_seg.items():
        lst.sort(key=lambda c: (c.file_bytes, -c.ssim_avg))
        _mark_frontier(lst)
    return by_seg


def _mark_frontier(lst):
    """Pareto frontier: a point is dominated if another is both smaller
    and at least as good (SSIM). Non-monotonic measurements stay in the
    raw data; only dominated points are flagged off-frontier."""
    best_ssim = -1.0
    for c in lst:  # ascending bytes
        if c.ssim_avg > best_ssim + 1e-12:
            c.on_frontier = True
            best_ssim = c.ssim_avg
        else:
            c.on_frontier = False


def floor_feasible(by_seg, floors):
    """Cheapest per-segment candidate meeting each quality floor."""
    picks = {}
    for sid, lst in by_seg.items():
        ok = [c for c in lst if c.ssim_avg >= floors[sid]]
        if not ok:
            return None
        picks[sid] = min(ok, key=lambda c: c.file_bytes)
    return picks


def greedy_allocate(by_seg, floors, budget):
    """Start from the cheapest floor-passing candidate per segment, then
    repeatedly spend remaining budget on the upgrade with the best
    delta-SSIM per extra byte (marginal gain). Myopic by construction."""
    base = floor_feasible(by_seg, floors)
    if base is None:
        return dict(feasible=False, reason="某段没有任何候选达到最低质量门槛。",
                    picks=None, total_bytes=None, budget=budget)
    picks = dict(base)
    spent = sum(c.file_bytes for c in picks.values())
    if spent > budget:
        return dict(feasible=False,
                    reason=f"仅达到各段最低质量就需要 {spent} 字节，已超过预算 {budget}。",
                    picks=None, total_bytes=spent, budget=budget)
    steps = []
    while True:
        options = []
        for sid, lst in by_seg.items():
            cur = picks[sid]
            better = [c for c in lst
                      if c.ssim_avg > cur.ssim_avg + 1e-9
                      and c.ssim_avg >= floors[sid]]
            for c in better:
                extra = c.file_bytes - cur.file_bytes
                if extra <= 0:
                    continue
                if spent + extra <= budget:
                    gain = (c.ssim_avg - cur.ssim_avg) / extra
                    options.append((gain, sid, c, extra))
        if not options:
            break
        gain, sid, c, extra = max(options, key=lambda x: x[0])
        steps.append(dict(segment=sid, from_job=picks[sid].job_id,
                          to_job=c.job_id, extra_bytes=extra,
                          ssim_gain=round(c.ssim_avg - picks[sid].ssim_avg, 6),
                          marginal=round(gain * 1e6, 4)))
        picks[sid] = c
        spent += extra
    return dict(feasible=True, picks=picks, total_bytes=spent,
                total_ssim=round(sum(c.ssim_avg for c in picks.values()), 6),
                budget=budget, steps=steps, reason=None)


def joint_allocate(by_seg, floors, budget):
    """Exact multi-choice knapsack DP over whole-film byte total.

    State tracks every reachable (segment-prefix, byte total) with the
    maximum total SSIM, plus back-pointers. Bytes are integer so the DP
    is exact; candidate counts are small (54 per film, 18 per segment)."""
    seg_ids = [s["id"] for s in SEGMENTS]
    options = {}
    for sid in seg_ids:
        opts = [c for c in by_seg[sid] if c.ssim_avg >= floors[sid]]
        if not opts:
            return dict(feasible=False,
                        reason=f"{sid} 没有任何候选达到最低质量门槛。",
                        picks=None, total_bytes=None, budget=budget)
        options[sid] = opts

    # dp: bytes -> (total_ssim, {sid: Candidate}); keep only Pareto-best states
    dp = {0: (0.0, {})}
    for sid in seg_ids:
        ndp = {}
        for used, (qual, choice) in dp.items():
            for c in options[sid]:
                b = used + c.file_bytes
                if b > budget:
                    continue
                q = qual + c.ssim_avg
                if b not in ndp or q > ndp[b][0] + 1e-12:
                    ndp[b] = (q, {**choice, sid: c})
        if not ndp:
            return dict(feasible=False,
                        reason=f"处理到 {sid} 时预算内已放不下任何合格组合。",
                        picks=None, total_bytes=None, budget=budget)
        dp = _prune(ndp)
    if not dp:
        return dict(feasible=False, reason="预算内不存在合格组合。",
                    picks=None, total_bytes=None, budget=budget)
    best_bytes = max(dp, key=lambda b: dp[b][0])
    total_ssim, picks = dp[best_bytes]
    return dict(feasible=True, picks=picks, total_bytes=best_bytes,
                total_ssim=round(total_ssim, 6), budget=budget, reason=None)


def _prune(dp):
    """Drop states dominated on (bytes -> max quality): keep a state only
    if no cheaper state has equal or better total SSIM."""
    items = sorted(dp.items())
    kept = {}
    best_q = -1.0
    for b, (q, choice) in items:
        if q > best_q + 1e-12:
            kept[b] = (q, choice)
            best_q = q
    return kept


def choose_budget(by_seg, floors):
    """Pick a demonstration budget that sits between the greedy optimum
    and the joint optimum, so the two strategies visibly disagree."""
    cheapest = floor_feasible(by_seg, floors)
    floor_cost = sum(c.file_bytes for c in cheapest.values())
    all_front = [c for lst in by_seg.values() for c in lst if c.on_frontier]
    richest = sum(max(lst, key=lambda c: c.ssim_avg).file_bytes
                  for lst in by_seg.values())
    # Sweep budgets; find the smallest where greedy and joint differ.
    lo, hi = floor_cost, richest
    divergence = None
    for b in range(lo, hi + 1, max(1, (hi - lo) // 400)):
        g = greedy_allocate(by_seg, floors, b)
        j = joint_allocate(by_seg, floors, b)
        if g["feasible"] and j["feasible"] and \
                {s: c.job_id for s, c in g["picks"].items()} != \
                {s: c.job_id for s, c in j["picks"].items()}:
            divergence = b
            break
    return dict(floor_cost=floor_cost, richest=richest,
                divergence_budget=divergence)


def run(floors=None, budget=None, tight_budget=None):
    conn = connect()
    by_seg = load_candidates(conn)
    conn.close()
    if floors is None:
        floors = {s["id"]: 0.990 for s in SEGMENTS}
    guide = choose_budget(by_seg, floors)
    if budget is None:
        budget = guide["divergence_budget"] or guide["floor_cost"] + 1
    if tight_budget is None:
        tight_budget = max(0, guide["floor_cost"] - 1)

    greedy = greedy_allocate(by_seg, floors, budget)
    joint = joint_allocate(by_seg, floors, budget)
    infeasible = greedy_allocate(by_seg, floors, tight_budget)

    plan = dict(
        floors=floors,
        budget=budget,
        tight_budget=tight_budget,
        guide=guide,
        greedy=_serialize(greedy),
        joint=_serialize(joint),
        infeasible=_serialize(infeasible),
        candidates={sid: [asdict(c) for c in lst]
                    for sid, lst in by_seg.items()},
    )
    with open(PLAN_PATH, "w") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return plan


def _serialize(res):
    out = dict(res)
    if res.get("picks"):
        out["picks"] = {sid: asdict(c) for sid, c in res["picks"].items()}
    return out


if __name__ == "__main__":
    p = run()
    print("budget:", p["budget"], "tight:", p["tight_budget"])
    print("greedy feasible:", p["greedy"]["feasible"],
          "bytes:", p["greedy"].get("total_bytes"))
    print("joint  feasible:", p["joint"]["feasible"],
          "bytes:", p["joint"].get("total_bytes"))
    print("infeasible case:", p["infeasible"]["reason"])
