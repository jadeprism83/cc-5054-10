"""Budget gate: mux chosen encodes, then verify final container sizes.

If delivered files exceed the byte cap, return to the selection step with a
reduced effective budget (cap minus the observed container overhead margin)
and re-run the joint allocation, then re-mux. When even the cheapest
quality-floor combination cannot fit, stop and report the conflicting
constraints instead of silently shipping an over-budget film.
"""
import json
import sys

from mux_deliverables import build_deliverables
from paths import OVERHEAD_PATH, PLAN_PATH
from selection import load_candidates, joint_allocate, run as run_selection
from db import connect


def gate(max_rounds=3):
    plan = json.load(open(PLAN_PATH))
    history = []
    for round_idx in range(1, max_rounds + 1):
        rep = build_deliverables("joint")
        history.append(dict(round=round_idx,
                            budget=rep["budget_bytes"],
                            delivered=rep["delivered_mp4_total_bytes"],
                            within=rep["within_budget_delivered"]))
        if rep["within_budget_delivered"]:
            print(f"[gate] round {round_idx}: PASS "
                  f"{rep['delivered_mp4_total_bytes']:,} <= {rep['budget_bytes']:,}")
            rep["gate_history"] = history
            json.dump(rep, open(OVERHEAD_PATH, "w"), ensure_ascii=False, indent=2)
            return rep

        over = rep["over_by_bytes"]
        print(f"[gate] round {round_idx}: OVER by {over:,} B; re-selecting "
              f"with tighter budget", file=sys.stderr)
        floors = plan["floors"]
        new_budget = rep["budget_bytes"] - over - 4096  # headroom for mux overhead
        conn = connect()
        by_seg = load_candidates(conn)
        conn.close()
        re_pick = joint_allocate(by_seg, floors, new_budget)
        if not re_pick["feasible"]:
            msg = dict(
                feasible=False,
                reason=re_pick["reason"],
                requested_budget=plan["budget"],
                tightest_budget_tried=new_budget,
                gate_history=history,
                explanation=("收紧到扣除容器开销后的预算后，联合分配已找不到满足各段 "
                             "SSIM 门槛的组合：总字节上限与最低质量约束不可同时满足，"
                             "需要放宽预算或降低质量门槛。"),
            )
            json.dump(msg, open(OVERHEAD_PATH, "w"), ensure_ascii=False, indent=2)
            print("[gate] INFEASIBLE:", msg["reason"], file=sys.stderr)
            return msg

        # Persist the tightened plan and loop again.
        plan["budget"] = new_budget
        plan["joint"] = json.loads(json.dumps(re_pick, default=_default))
        json.dump(plan, open(PLAN_PATH, "w"), ensure_ascii=False, indent=2)

    print("[gate] exhausted rounds without converging", file=sys.stderr)
    return dict(feasible=False, reason="重排轮次耗尽仍未收敛", gate_history=history)


def _default(o):
    from dataclasses import asdict, is_dataclass
    if is_dataclass(o):
        return asdict(o)
    raise TypeError(type(o))


if __name__ == "__main__":
    result = gate()
    print(json.dumps({k: v for k, v in result.items()
                      if k not in ("per_segment",)}, ensure_ascii=False, indent=2))
    sys.exit(0 if result.get("within_budget_delivered", result.get("feasible")) else 2)
