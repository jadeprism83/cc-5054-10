"""Define and run the encoding grid.

Each segment is encoded at three resolutions and six quality levels with
libx264 (CRF plus a bitrate cap), plus deliberately failing jobs so the UI
can surface encoding failures. Quality is always measured against the 1080p
master after the decoded output is scaled back to 1920x1080, so points from
different resolutions are directly comparable on the same axes.
"""
import json
import os
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from db import connect
from paths import (ENC_DIR, FFMPEG, FFPROBE, HEIGHT, LOG_DIR, SEGMENTS,
                   SRC_DIR, WIDTH)

RESOLUTIONS = [
    ("1080p", WIDTH, HEIGHT),
    ("720p", 1280, 720),
    ("480p", 854, 480),
]

# quality label -> (crf, maxrate kbit/s, bufsize kbit/s)
QUALITIES = [
    ("q1_visually_lossless", 18, 6000, 12000),
    ("q2_high", 23, 4000, 8000),
    ("q3_good", 28, 2500, 5000),
    ("q4_fair", 33, 1400, 2800),
    ("q5_coarse", 38, 800, 1600),
    ("q6_extreme", 43, 450, 900),
]

# Deliberate failures, kept alongside real data for the UI.
FAILURE_JOBS = [
    dict(codec="libx264", width=1281, height=720, quality_label="fail_odd_dimensions",
         crf=23, bitrate_cap=4000, preset="medium",
         fail_reason="奇数宽度 1281，yuv420p 要求偶数采样，编码器直接拒绝。"),
    dict(codec="libx264", width=1280, height=720, quality_label="fail_bad_preset",
         crf=23, bitrate_cap=4000, preset="turbocharger",
         fail_reason="preset 名称不存在，x264 参数校验失败。"),
]
# One extra per segment: AV1 with an out-of-range CRF (libaom accepts 0..63).
def av1_overshoot_job(segment_id):
    return dict(codec="libaom-av1", width=854, height=480,
                quality_label="fail_bad_crf", crf=99, bitrate_cap=200_000,
                preset="medium",
                fail_reason=f"{segment_id}: CRF=99 超出 libaom 合法范围 0-63，"
                            "编码器初始化即失败（代表码率参数给到不可能值的场景）。")


SSIM_FRAME_RE = re.compile(r"All:([0-9.]+)")
PSNR_AVG_RE = re.compile(r"average:([0-9.]+)")


def build_matrix():
    jobs = []
    for seg in SEGMENTS:
        sid = seg["id"]
        for label, w, h in RESOLUTIONS:
            for qlabel, crf, maxrate, bufsize in QUALITIES:
                jobs.append(dict(
                    segment_id=sid, codec="libx264", width=w, height=h,
                    quality_label=f"{label}_{qlabel}", crf=crf,
                    bitrate_cap=maxrate * 1000, preset="medium",
                    extra=json.dumps(dict(bufsize=bufsize * 1000, res_label=label,
                                          q_label=qlabel)),
                    expect_fail=0, fail_reason=None,
                ))
        for fj in FAILURE_JOBS:
            jobs.append(dict(fj, segment_id=sid,
                             extra=json.dumps(dict(res_label="720p", q_label="x")),
                             expect_fail=1))
        jobs.append(dict(av1_overshoot_job(sid), segment_id=sid,
                         extra=json.dumps(dict(res_label="480p", q_label="x")),
                         expect_fail=1))
    return jobs


def job_id_for(job):
    base = f"{job['segment_id']}_{job['codec'].replace('lib','')}_{job['width']}x{job['height']}_{job['quality_label']}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", base)


def stream_size(path):
    """Sum elementary-stream payload sizes via ffprobe packets."""
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=size", "-of", "csv=p=0", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        return None
    total = 0
    for line in out.stdout.split():
        try:
            total += int(line)
        except ValueError:
            continue
    return total or None


def run_job(job):
    job_id = job_id_for(job)
    sid = job["segment_id"]
    out_dir = ENC_DIR / sid
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{job_id}.mp4"
    log_path = LOG_DIR / f"encode_{job_id}.log"

    src = SRC_DIR / f"{sid}.mkv"
    w, h = job["width"], job["height"]
    vf_encode = f"scale={w}:{h}:flags=lanczos,format=yuv420p"

    if job["codec"] == "libx264":
        encode_args = ["-c:v", "libx264", "-preset", job["preset"],
                       "-crf", str(job["crf"]),
                       "-maxrate", str(job["bitrate_cap"]),
                       "-bufsize", str(json.loads(job["extra"]).get("bufsize", job["bitrate_cap"] * 2)),
                       "-pix_fmt", "yuv420p"]
    else:  # libaom-av1
        cap = job["bitrate_cap"]
        encode_args = ["-c:v", "libaom-av1", "-crf", str(job["crf"]),
                       "-b:v", str(cap), "-cpu-used", "6",
                       "-pix_fmt", "yuv420p"]

    # Encode ... then decode, scale back to master size, score with SSIM+PSNR.
    cmd = [
        FFMPEG, "-hide_banner", "-y",
        "-i", str(src),
        "-vf", vf_encode,
        *encode_args,
        "-an", "-t", "4",
        str(out_path),
    ]
    t0 = time.time()
    with open(log_path, "wb") as logf:
        enc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
    enc_secs = time.time() - t0

    result = dict(job, job_id=job_id, output_path=str(out_path),
                  log_path=str(log_path), ffmpeg_rc=enc.returncode,
                  encode_secs=round(enc_secs, 3), status="ok")

    if enc.returncode != 0 or not out_path.exists():
        result.update(status="failed", file_bytes=None, stream_bytes=None,
                      bitrate_meas=None, ssim_avg=None, ssim_min=None,
                      psnr_avg=None, frames=None,
                      error_tail=_tail(log_path))
        return result

    file_bytes = out_path.stat().st_size
    s_bytes = stream_size(out_path)
    def measure(metric_filter):
        cmd = [
            FFMPEG, "-hide_banner",
            "-i", str(out_path),
            "-i", str(src),
            "-filter_complex",
            f"[0:v]scale={WIDTH}:{HEIGHT}:flags=lanczos,setpts=PTS-STARTPTS[a];"
            f"[1:v]setpts=PTS-STARTPTS[b];"
            f"[a][b]{metric_filter}[m]",
            "-map", "[m]", "-f", "null", "-",
        ]
        return subprocess.run(cmd, capture_output=True, text=True)

    ssim_run = measure("ssim=stats_file=-")
    psnr_run = measure("psnr=stats_file=-")
    ssim_vals = [float(x) for x in SSIM_FRAME_RE.findall(ssim_run.stderr or "")]
    psnr_m = PSNR_AVG_RE.findall(psnr_run.stderr or "")
    frames = _count_frames(ssim_run.stderr or "")
    result.update(
        file_bytes=file_bytes,
        stream_bytes=s_bytes,
        bitrate_meas=round(file_bytes * 8 / 4.0),
        ssim_avg=round(sum(ssim_vals) / len(ssim_vals), 6) if ssim_vals else None,
        ssim_min=round(min(ssim_vals), 6) if ssim_vals else None,
        psnr_avg=float(psnr_m[-1]) if psnr_m else None,
        frames=frames,
        error_tail=None,
    )
    if ssim_run.returncode != 0 or not ssim_vals:
        result["status"] = "failed"
        result["error_tail"] = (ssim_run.stderr or "")[-1200:] or "SSIM 测量阶段失败"
    return result


def _count_frames(text):
    m = re.findall(r"frame=\s*(\d+)", text)
    return int(m[-1]) if m else None


def _tail(path):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 1200))
        return f.read().decode("utf-8", "replace")


def persist(conn, r):
    conn.execute(
        """INSERT OR REPLACE INTO jobs
        (job_id, segment_id, codec, width, height, quality_label, crf,
         bitrate_cap, preset, extra, expect_fail, fail_reason, status,
         ffmpeg_rc, file_bytes, stream_bytes, bitrate_meas, ssim_avg,
         ssim_min, psnr_avg, frames, encode_secs, output_path, log_path,
         error_tail)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (r["job_id"], r["segment_id"], r["codec"], r["width"], r["height"],
         r["quality_label"], r.get("crf"), r.get("bitrate_cap"),
         r.get("preset"), r.get("extra"), r.get("expect_fail", 0),
         r.get("fail_reason"), r["status"], r.get("ffmpeg_rc"),
         r.get("file_bytes"), r.get("stream_bytes"), r.get("bitrate_meas"),
         r.get("ssim_avg"), r.get("ssim_min"), r.get("psnr_avg"),
         r.get("frames"), r.get("encode_secs"), r.get("output_path"),
         r.get("log_path"), r.get("error_tail")),
    )
    conn.commit()


def main():
    matrix = build_matrix()
    print(f"[grid] {len(matrix)} jobs scheduled")
    conn = connect()
    done = {row["job_id"] for row in conn.execute("SELECT job_id FROM jobs")}
    todo = [j for j in matrix if job_id_for(j) not in done]
    print(f"[grid] {len(todo)} pending, {len(done)} cached")
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(run_job, j): j for j in todo}
        for k, fut in enumerate(as_completed(futures), 1):
            r = fut.result()
            persist(conn, r)
            tag = "FAIL" if r["status"] == "failed" else "ok"
            ssim = r.get("ssim_avg")
            print(f"[grid] {k}/{len(todo)} {tag:4s} {r['job_id']} "
                  f"bytes={r.get('file_bytes')} ssim={ssim}", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
