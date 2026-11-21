"""Shared definitions for the revocable early-stop configuration searcher.

The searcher works on a deliberately adversarial film: a long static head,
a dense-motion middle, and a short very-high-motion shot. Every shot is
sliced into fixed 30-frame sample windows so the searcher can spend its
frame budget on partial evidence instead of full encodes.
"""
from paths import OUT

SEARCH_OUT = OUT / "search"
SEARCH_SRC = SEARCH_OUT / "shots"
SEARCH_ENC = SEARCH_OUT / "encodes"
SEARCH_DB = SEARCH_OUT / "search.sqlite3"
SEARCH_RUNS = SEARCH_OUT / "runs"

FPS = 30
WIDTH, HEIGHT = 1920, 1080
# Per-shot window lengths. The long static head uses 90-frame windows so it
# has the fewest windows (3); four initial windows per config therefore spill
# past the head, and where they land depends on the film ordering.
WINDOW_FRAMES = 30
HEAD_WINDOW_FRAMES = 90

# id, seconds, role. The burst shot carries its own stricter quality floor.
SHOTS = [
    {"id": "head_static", "name": "长静景（片头）", "seconds": 9,
     "window_frames": HEAD_WINDOW_FRAMES,
     "desc": "径向渐变黄昏远景加一团缓慢漂移的柔光，长时间几乎静止。"},
    {"id": "mid_fabric", "name": "细密织物（中部）", "seconds": 4,
     "window_frames": WINDOW_FRAMES,
     "desc": "高频经纬纹理随时间流动，空间与时间复杂度都高。"},
    {"id": "burst", "name": "短促高运动镜头", "seconds": 2,
     "window_frames": WINDOW_FRAMES,
     "desc": "高频棋盘格快速平移加亮度脉动，每帧变化剧烈。"},
]

# Two manifests the searcher can load: the burst starts in the middle of the
# film, then it is moved to the very end without re-encoding any window.
ORDER_BURST_MID = ["head_static", "mid_fabric", "burst"]
ORDER_BURST_END = ["head_static", "mid_fabric", "burst"]  # same shots, see ORDER_OVERRIDES
ORDER_OVERRIDES = {
    "burst_mid": ["head_static", "burst", "mid_fabric"],
    "burst_end": ["head_static", "mid_fabric", "burst"],
}

# Generic per-shot floor and the independent, stricter floor for the burst.
# The burst checkerboard is punishing: only high-resolution configs reach
# 0.93 there, while every config clears 0.985 on the static head.
FLOORS_DEFAULT = {"head_static": 0.985, "mid_fabric": 0.985, "burst": 0.930}

RESOLUTIONS = [
    ("1080p", WIDTH, HEIGHT),
    ("720p", 1280, 720),
    ("480p", 854, 480),
]
QUALITIES = [
    ("q1_visually_lossless", 18),
    ("q2_high", 23),
    ("q3_good", 28),
    ("q4_fair", 33),
    ("q5_coarse", 38),
    ("q6_extreme", 43),
]

# Fallback empirical spread (SSIM) when a shot has only one sampled window,
# so a single window never yields a zero-width confidence interval.
SINGLE_WINDOW_SIGMA = 0.002
Z95 = 1.96


def shot_meta(shot_id):
    return next(s for s in SHOTS if s["id"] == shot_id)


def shot_frames(shot_id):
    return shot_meta(shot_id)["seconds"] * FPS


def window_frames_of(shot_id):
    return shot_meta(shot_id).get("window_frames", WINDOW_FRAMES)


def windows_of(shot_id):
    """Window indices for a shot; window length is per-shot so the long
    static head is sliced into fewer, wider windows."""
    return list(range(shot_frames(shot_id) // window_frames_of(shot_id)))


def all_windows():
    return [(s["id"], w) for s in SHOTS for w in windows_of(s["id"])]


def config_grid():
    grid = []
    for res_label, w, h in RESOLUTIONS:
        for q_label, crf in QUALITIES:
            grid.append(dict(config_id=f"{res_label}_{q_label}",
                             res_label=res_label, q_label=q_label,
                             width=w, height=h, crf=crf))
    return grid


def config_shot_key(config_id, shot_id):
    return f"{config_id}@{shot_id}"
