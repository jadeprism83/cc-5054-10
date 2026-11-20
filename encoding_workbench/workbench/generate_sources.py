"""Generate the three 1080p master clips.

seg1: animated title rendered frame-by-frame with PIL (large flat areas,
      moving text with fade in/out) then encoded to FFV1 by ffmpeg
seg2: dense woven texture via ffmpeg geq (high spatial + temporal frequency)
seg3: long static landscape via ffmpeg geq (near-still gradient, slow light)
Masters are lossless FFV1 so later encodes are not penalised by generational loss.
"""
import math
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from paths import FFMPEG, FONT, FPS, HEIGHT, SEGMENTS, SRC_DIR, WIDTH, DURATION

SIZE = f"{WIDTH}x{HEIGHT}"
TOTAL_FRAMES = FPS * DURATION


def run(cmd, log_name):
    from paths import LOG_DIR
    log_path = LOG_DIR / log_name
    with open(log_path, "wb") as log:
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"{log_name} failed (rc={proc.returncode}); see {log_path}")


def render_title_frames(frame_dir: Path):
    """PIL renderer: flat background, accent bar, two fading drifting text lines."""
    frame_dir.mkdir(parents=True, exist_ok=True)
    font_main = ImageFont.truetype(FONT, 150)
    font_sub = ImageFont.truetype(FONT, 56)
    bg = (16, 24, 48)
    blue = (79, 168, 255)
    light = (159, 208, 255)
    for i in range(TOTAL_FRAMES):
        t = i / FPS
        if t < 0.4:
            alpha = 0.0
        elif t < 1.0:
            alpha = (t - 0.4) / 0.6
        elif t > 3.5:
            alpha = max(0.0, 1 - (t - 3.5) / 0.5)
        else:
            alpha = 1.0

        img = Image.new("RGB", (WIDTH, HEIGHT), bg)
        draw = ImageDraw.Draw(img)
        draw.rectangle([360, HEIGHT // 2 + 20, WIDTH - 360, HEIGHT // 2 + 26], fill=blue)

        layer = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        shift = 30 * math.sin(2 * math.pi * t / DURATION)
        a = int(round(255 * alpha))
        ld.text((WIDTH / 2 + shift, HEIGHT / 2 - 60), "ENCODING LAB",
                font=font_main, fill=(255, 255, 255, a), anchor="mm")
        ld.text((WIDTH / 2 - shift, HEIGHT / 2 + 90),
                "RATE-DISTORTION WORK BENCH",
                font=font_sub, fill=(*light, a), anchor="mm")
        img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
        img.save(frame_dir / f"f_{i:04d}.png")


def fabric_filter():
    # Interlaced warp/weft sines + a diagonal cross term + slow temporal shimmer.
    weave = ("128"
             "+42*sin(2*PI*X/9)"
             "+32*sin(2*PI*Y/9)"
             "+16*sin(2*PI*(X+Y)/18)"
             "+7*sin(2*PI*T*1.5+2*PI*X/40)")
    expr_r = f"min(255,max(0,{weave}+12))"
    expr_b = f"min(255,max(0,{weave}-14))"
    return (f"color=c=black:s={SIZE}:r={FPS}:d={DURATION},"
            f"geq=r='{expr_r}':g='{weave}':b='{expr_b}',format=yuv420p")


def static_filter():
    # Radial dusk gradient + one soft light that drifts slowly across the frame.
    cx, cy = 960, 520
    cloud_x = f"(X-{cx}-45*T)"
    cloud_y = f"(Y-{cy}-30*sin(2*PI*T/4))"
    cloud = f"34*exp(-({cloud_x}*{cloud_x}+{cloud_y}*{cloud_y})/70000)"
    base = f"110*(1-hypot(X-{cx},Y-{cy})/1200)"
    r = f"min(255,max(0,70+{base}+{cloud}))"
    g = f"min(255,max(0,45+0.55*{base}+{cloud}))"
    b = f"min(255,max(0,95+0.9*{base}+0.6*{cloud}))"
    return (f"color=c=black:s={SIZE}:r={FPS}:d={DURATION},"
            f"geq=r='{r}':g='{g}':b='{b}',format=yuv420p")


FILTERS = {
    "seg2_fabric": fabric_filter,
    "seg3_static": static_filter,
}


def main():
    frame_dir = SRC_DIR / "title_frames"
    for seg in SEGMENTS:
        sid = seg["id"]
        out = SRC_DIR / f"{sid}.mkv"
        if sid == "seg1_title":
            print(f"[sources] rendering {sid} frames with PIL", flush=True)
            render_title_frames(frame_dir)
            cmd = [
                FFMPEG, "-hide_banner", "-y",
                "-framerate", str(FPS), "-i", str(frame_dir / "f_%04d.png"),
                "-c:v", "ffv1", "-level", "3", "-pix_fmt", "yuv420p",
                str(out),
            ]
        else:
            vf = FILTERS[sid]()
            cmd = [
                FFMPEG, "-hide_banner", "-y",
                "-f", "lavfi", "-i", vf,
                "-c:v", "ffv1", "-level", "3",
                "-t", str(DURATION),
                str(out),
            ]
        print(f"[sources] encoding {sid} -> {out.name}", flush=True)
        run(cmd, f"source_{sid}.log")
    shutil.rmtree(frame_dir, ignore_errors=True)
    print("[sources] done")


if __name__ == "__main__":
    sys.exit(main())
