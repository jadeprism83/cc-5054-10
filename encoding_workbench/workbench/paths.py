"""Central filesystem locations and external binary paths."""
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "out"
SRC_DIR = OUT / "sources"
ENC_DIR = OUT / "encodes"
RESULT_DIR = OUT / "results"
LOG_DIR = ROOT / "logs"
WEB_DIR = ROOT / "web"
STATIC_DIR = WEB_DIR / "static"

DB_PATH = RESULT_DIR / "workbench.sqlite3"
PLAN_PATH = RESULT_DIR / "selection.json"
OVERHEAD_PATH = RESULT_DIR / "container_overhead.json"

for d in (SRC_DIR, ENC_DIR, RESULT_DIR, LOG_DIR, STATIC_DIR):
    d.mkdir(parents=True, exist_ok=True)


def find_ffmpeg():
    bundled = ROOT.parent / "tools" / "ffmpeg-7.0.2-amd64-static"
    for name, folder in (("ffmpeg", bundled), ("ffprobe", bundled)):
        p = folder / name
        if p.exists():
            yield str(p)
    for name in ("ffmpeg", "ffprobe"):
        found = shutil.which(name)
        if found:
            yield found


_FFBIN = list(find_ffmpeg())
FFMPEG = _FFBIN[0] if len(_FFBIN) >= 1 else "ffmpeg"
FFPROBE = _FFBIN[1] if len(_FFBIN) >= 2 else "ffprobe"

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Master clip geometry
WIDTH, HEIGHT, FPS, DURATION = 1920, 1080, 30, 4

SEGMENTS = [
    {
        "id": "seg1_title",
        "name": "动画标题",
        "desc": "纯色背景上的大字标题，淡入淡出并缓慢位移，平坦区域占比高。",
    },
    {
        "id": "seg2_fabric",
        "name": "细密织物",
        "desc": "高频经纬纹理叠加随时间流动的细纹，空间与时间复杂度都高。",
    },
    {
        "id": "seg3_static",
        "name": "长静景",
        "desc": "径向渐变的黄昏远景，几乎静止，只有一团柔光缓慢移动。",
    },
]
