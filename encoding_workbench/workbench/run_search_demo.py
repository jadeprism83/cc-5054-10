"""Headless driver for the early-stop experiment.

Runs the searcher under two sampling policies and two film orderings, then
builds the small-scale exhaustive rate-distortion frontier as ground truth,
and finally exercises the panel lock on a configuration that was eliminated
on head-only evidence. Every number in the printed audit comes from frames
that were really encoded; predicted aggregates stay tagged as estimates.
"""
import json
import sys

from search_common import SEARCH_RUNS
from generate_search_sources import render_all
from searcher import SearchRun, list_runs

WORKERS = 3


def main():
    print("== 0/4 render master shots (cached)")
    for sid, tag in render_all():
        print(f"   {tag:8s} {sid}")

    runs = {}
    for order_key in ("burst_mid", "burst_end"):
        for policy in ("head_first", "stratified"):
            tag = f"{policy}@{order_key}"
            print(f"== search run: {tag}")
            run = SearchRun(policy=policy, order_key=order_key)
            run.run(workers=WORKERS)
            runs[tag] = run
            alive = [cid for cid, st in run.candidates.items() if st.alive]
            print(f"   frames={run.frames_spent} alive={len(alive)} "
                  f"eliminated={len(run.ledger)}")
            for ev in run.ledger:
                print(f"   - ELIM {ev['config_id']:28s} {ev['rule']} "
                      f"shot={ev['shot_id']} hyp={ev['hypothesis']}")

    print("== exhaustive reference: full encode every config x shot")
    ref = SearchRun(policy="head_first", order_key="burst_mid")
    audit = ref.build_exhaustive(workers=WORKERS)
    print(f"   frontier: {[p['config_id'] for p in audit['exhaustive_frontier']]}")
    print(f"   exhaustive ground-truth cost="
          f"{audit['exhaustive_cost_frames']} frames "
          f"({len(ref.grid)} configs x 450 frames)")

    # The exhaustive reference is the shared ground truth each run is audited
    # against. The audits are (re)attached *after* the panel locks below so
    # lock frame counts and counterexamples land in the saved audit too.
    ground_truth = ref._ground_truth()

    print("== panel lock demonstration")
    # Lock a head_first R2 victim: its dominance verdict came entirely from
    # static-head evidence, so a full encode is the most likely counterexample.
    victim = _pick_lock_victim(runs["head_first@burst_end"])
    if victim:
        entry = runs["head_first@burst_end"].lock_and_full_encode(
            victim, workers=WORKERS)
        print(f"   locked {victim}: counterexample={entry['counterexample']}")
        print(f"   reopened={entry['reopened']}")
    # Also lock a stratified R1 victim so the panel shows both revocation
    # paths (floor-hypothesis falsification and dominance falsification).
    r1_victim = _pick_r1_victim(runs["stratified@burst_mid"])
    if r1_victim:
        entry2 = runs["stratified@burst_mid"].lock_and_full_encode(
            r1_victim, workers=WORKERS)
        print(f"   locked {r1_victim}: counterexample={entry2['counterexample']}")
        print(f"   reopened={entry2['reopened']}")

    print("== audits (attached after locks so lock costs are included)")
    for tag, run in runs.items():
        run.audit = run._audit_against(ground_truth)
        run.save()
        a = run.audit
        print(f"== audit {tag}: missed_frontier={a['missed_frontier_configs']} "
              f"window_frames={a['search_window_frames']} "
              f"lock_frames={a['lock_full_frames']} "
              f"(new {a['lock_newly_encoded_frames']}/reuse "
              f"{a['lock_reused_cached_frames']}) "
              f"saved={a['saved_frames_vs_exhaustive']} ({a['saved_pct']}%)")
    print("== runs on disk")
    for meta in list_runs():
        print("  ", meta)


def _pick_lock_victim(run):
    """Pick the cheapest-looking R2 victim: head-first evidence ranks cheap
    480p/720p configs above expensive 1080p ones on the static head, which is
    exactly the ranking the dense middle of the film reverses."""
    r2 = [e for e in run.ledger if e["rule"] == "R2_dominance"]
    if r2:
        return r2[0]["config_id"]
    return run.ledger[0]["config_id"] if run.ledger else None


def _pick_r1_victim(run):
    for ev in run.ledger:
        if ev["rule"] == "R1_floor" and ev["shot_id"] == "burst":
            return ev["config_id"]
    return run.ledger[0]["config_id"] if run.ledger else None


if __name__ == "__main__":
    sys.exit(main())
