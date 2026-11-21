"""Render the three master shots for the early-stop search experiment.

Everything is synthesized with ffmpeg lavfi/geq (no Pillow dependency):

* head_static  long near-still dusk gradient with a drifting soft light
* mid_fabric   dense weaving texture with temporal shimmer
* burst        short very-high-motion shot: a fast panning checkerboard
               plus a brightness pulse, given its own quality floor

Masters are lossless FFV1 so window encodes do not suffer generation loss.
"""
import subprocess

from paths import FFMPEG, LOG_DIR
from search_common import (FPS, HEIGHT, SEARCH_SRC, SHOTS, WIDTH,
                           shot_frames, shot_meta)

SIZE = f"{WIDTH}x{HEIGHT}"


def _static():
    cx, cy = 960, 520
    cloud_x = f"(X-{cx}-45*T)"
    cloud_y = f"(Y-{cy}-30*sin(2*PI*T/6))"
    cloud = f"34*exp(-({cloud_x}*{cloud_x}+{cloud_y}*{cloud_y})/70000)"
    base = f"110*(1-hypot(X-{cx},Y-{cy})/1200)"
    r = f"min(255,max(0,70+{base}+{cloud}))"
    g = f"min(255,max(0,45+0.55*{base}+{cloud}))"
    b = f"min(255,max(0,95+0.9*{base}+0.6*{cloud}))"
    return f"color=c=black:s={SIZE}:r={FPS},geq=r='{r}':g='{g}':b='{b}',format=yuv420p"


def _fabric():
    weave = ("128"
             "+42*sin(2*PI*X/9)"
             "+32*sin(2*PI*Y/9)"
             "+16*sin(2*PI*(X+Y)/18)"
             "+7*sin(2*PI*T*1.5+2*PI*X/40)")
    expr_r = f"min(255,max(0,{weave}+12))"
    expr_b = f"min(255,max(0,{weave}-14))"
    return (f"color=c=black:s={SIZE}:r={FPS},"
            f"geq=r='{expr_r}':g='{weave}':b='{expr_b}',format=yuv420p")


def _burst():
    # 64px checkerboard that pans 420 px/sec (mod via lt branches), over a
    # pulsing baseline. Every frame changes most pixels: very high motion.
    pan = "420*T"
    cx = f"((X+{pan})-mod(X+{pan},64))/64"
    cy = "(Y-mod(Y,64))/64"
    checker = f"if(lt(mod({cx}+{cy},2),1),72,-72)"
    pulse = "34*sin(2*PI*T*3.2)"
    v = f"128+{pulse}+{checker}"
    return (f"color=c=black:s={SIZE}:r={FPS},"
            f"geq=r='{v}':g='{v}':b='{v}',format=yuv420p")


FILTERS = {
    "head_static": _static,
    "mid_fabric": _fabric,
    "burst": _burst,
}


def shot_path(shot_id):
    return SEARCH_SRC / f"{shot_id}.mkv"


def render_all(force=False):
    SEARCH_SRC.mkdir(parents=True, exist_ok=True)
    made = []
    for shot in SHOTS:
        sid = shot["id"]
        out = shot_path(sid)
        if out.exists() and not force:
            made.append((sid, "cached"))
            continue
        frames = shot_frames(sid)
        cmd = [
            FFMPEG, "-hide_banner", "-y",
            "-f", "lavfi", "-i", FILTERS[sid](),
            "-c:v", "ffv1", "-level", "3", "-pix_fmt", "yuv420p",
            "-frames:v", str(frames), str(out),
        ]
        log = LOG_DIR / f"search_source_{sid}.log"
        with open(log, "wb") as lf:
            proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT)
        if proc.returncode != 0:
            raise RuntimeError(f"render {sid} failed rc={proc.returncode}; see {log}")
        made.append((sid, "rendered"))
    return made


def main():
    for sid, tag in render_all():
        meta = shot_meta(sid)
        print(f"[search-sources] {tag:8s} {sid:12s} {meta['seconds']}s "
              f"{shot_frames(sid)} frames -> {shot_path(sid).name}")


if __name__ == "__main__":
    main()
