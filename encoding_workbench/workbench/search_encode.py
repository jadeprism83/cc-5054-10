"""Encode and measure sample windows (and full shots) for the searcher.

A window job encodes exactly one 30-frame slice of one master shot at one
candidate configuration, then decodes it, lanczos-scales it back to
1920x1080 and scores every frame against the same master slice with SSIM.
Every measured number (bytes, per-frame SSIM, mean/min SSIM) is a *real*
encoding result; nothing here is predicted. Full-shot encodes use the same
measurement path but cover every frame of the shot, so locked counterexample
checks and the exhaustive reference share one measurement convention.
"""
import json
import re
import subprocess
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from paths import FFMPEG, HEIGHT, LOG_DIR, WIDTH
from search_common import WINDOW_FRAMES, SEARCH_ENC, window_frames_of
from search_store import connect
from generate_search_sources import shot_path

SSIM_FRAME_RE = re.compile(r"All:([0-9.]+)")


def window_job_id(config_id, shot_id, window_idx):
    return f"win_{config_id}_{shot_id}_w{window_idx:02d}"


def full_job_id(config_id, shot_id):
    return f"full_{config_id}_{shot_id}"


def _encode_slice(config, shot_id, start_frame, num_frames, out_path, job_id):
    src = str(shot_path(shot_id))
    cmd = [
        FFMPEG, "-hide_banner", "-y",
        "-ss", f"{start_frame / 30:.6f}", "-i", src,
        "-vf", f"scale={config['width']}:{config['height']}:flags=lanczos,format=yuv420p",
        "-c:v", "libx264", "-preset", "medium", "-crf", str(config["crf"]),
        "-pix_fmt", "yuv420p", "-an",
        "-frames:v", str(num_frames), str(out_path),
    ]
    log_path = LOG_DIR / f"search_encode_{job_id}.log"
    t0 = time.time()
    with open(log_path, "wb") as logf:
        enc = subprocess.run(cmd, stdout=logf, stderr=subprocess.STDOUT)
    enc_secs = round(time.time() - t0, 3)
    return enc, enc_secs, log_path


def _measure_slice(shot_id, start_frame, num_frames, out_path):
    src = str(shot_path(shot_id))
    # Per-frame stats go to a real file: with stats_file=- the ffmpeg child
    # only flushes the final summary line when stderr is a captured pipe.
    stats = tempfile.NamedTemporaryFile(delete=False, suffix=".ssim")
    stats.close()
    cmd = [
        FFMPEG, "-hide_banner",
        "-ss", f"{start_frame / 30:.6f}", "-t", f"{num_frames / 30:.6f}",
        "-i", src,
        "-i", out_path,
        "-filter_complex",
        f"[0:v]setpts=PTS-STARTPTS,format=yuv420p[a];"
        f"[1:v]scale={WIDTH}:{HEIGHT}:flags=lanczos,setpts=PTS-STARTPTS[b];"
        f"[a][b]ssim=stats_file={stats.name}[m]",
        "-map", "[m]", "-f", "null", "-",
    ]
    run = subprocess.run(cmd, capture_output=True, text=True)
    vals = []
    try:
        with open(stats.name) as f:
            for line in f:
                m = SSIM_FRAME_RE.search(line)
                # Per-frame lines start with "n:"; the summary line does not.
                if m and line.lstrip().startswith("n:"):
                    vals.append(float(m.group(1)))
    finally:
        Path(stats.name).unlink(missing_ok=True)
    return vals, run


def _tail(log_path):
    with open(log_path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 1000))
        return f.read().decode("utf-8", "replace")


def encode_window(config, shot_id, window_idx):
    cid = config["config_id"]
    job_id = window_job_id(cid, shot_id, window_idx)
    out_dir = SEARCH_ENC / shot_id / "windows"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{job_id}.mp4"
    win_frames = window_frames_of(shot_id)
    start = window_idx * win_frames

    enc, enc_secs, log_path = _encode_slice(
        config, shot_id, start, win_frames, out_path, job_id)
    result = dict(job_id=job_id, config_id=cid, shot_id=shot_id,
                  window_idx=window_idx, width=config["width"],
                  height=config["height"], crf=config["crf"],
                  ffmpeg_rc=enc.returncode, encode_secs=enc_secs,
                  output_path=str(out_path), status="ok",
                  file_bytes=None, ssim_avg=None, ssim_min=None,
                  frame_ssims=None, frames=None, error_tail=None)
    if enc.returncode != 0 or not out_path.exists():
        result.update(status="failed", error_tail=_tail(log_path))
        return result

    vals, measure_run = _measure_slice(shot_id, start, win_frames, out_path)
    if measure_run.returncode != 0 or not vals:
        result.update(status="failed",
                      error_tail=(measure_run.stderr or "")[-1000:] or "SSIM 测量失败")
        return result
    result.update(
        file_bytes=out_path.stat().st_size,
        frame_ssims=json.dumps([round(v, 6) for v in vals]),
        ssim_avg=round(sum(vals) / len(vals), 6),
        ssim_min=round(min(vals), 6),
        frames=len(vals),
    )
    return result


def encode_full_shot(config, shot_id):
    from search_common import shot_frames
    cid = config["config_id"]
    job_id = full_job_id(cid, shot_id)
    out_dir = SEARCH_ENC / shot_id / "full"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{job_id}.mp4"
    n = shot_frames(shot_id)

    enc, enc_secs, log_path = _encode_slice(config, shot_id, 0, n, out_path, job_id)
    result = dict(job_id=job_id, config_id=cid, shot_id=shot_id,
                  width=config["width"], height=config["height"],
                  crf=config["crf"], ffmpeg_rc=enc.returncode,
                  encode_secs=enc_secs, output_path=str(out_path),
                  status="ok", file_bytes=None, ssim_avg=None,
                  ssim_min=None, frame_ssims=None, frames=None,
                  error_tail=None)
    if enc.returncode != 0 or not out_path.exists():
        result.update(status="failed", error_tail=_tail(log_path))
        return result
    vals, measure_run = _measure_slice(shot_id, 0, n, out_path)
    if measure_run.returncode != 0 or not vals:
        result.update(status="failed",
                      error_tail=(measure_run.stderr or "")[-1000:] or "SSIM 测量失败")
        return result
    result.update(
        file_bytes=out_path.stat().st_size,
        frame_ssims=json.dumps([round(v, 6) for v in vals]),
        ssim_avg=round(sum(vals) / len(vals), 6),
        ssim_min=round(min(vals), 6),
        frames=len(vals),
    )
    return result


def persist_window(conn, r):
    conn.execute(
        """INSERT OR REPLACE INTO window_jobs
        (job_id, config_id, shot_id, window_idx, width, height, crf, status,
         ffmpeg_rc, file_bytes, ssim_avg, ssim_min, frame_ssims, frames,
         encode_secs, output_path, error_tail)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (r["job_id"], r["config_id"], r["shot_id"], r["window_idx"],
         r["width"], r["height"], r["crf"], r["status"], r["ffmpeg_rc"],
         r["file_bytes"], r["ssim_avg"], r["ssim_min"], r["frame_ssims"],
         r["frames"], r["encode_secs"], r["output_path"], r["error_tail"]))
    conn.commit()


def persist_full(conn, r):
    conn.execute(
        """INSERT OR REPLACE INTO full_encodes
        (job_id, config_id, shot_id, width, height, crf, status,
         file_bytes, ssim_avg, ssim_min, frame_ssims, frames,
         encode_secs, output_path, error_tail)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (r["job_id"], r["config_id"], r["shot_id"], r["width"], r["height"],
         r["crf"], r["status"], r["file_bytes"], r["ssim_avg"], r["ssim_min"],
         r["frame_ssims"], r["frames"], r["encode_secs"], r["output_path"],
         r["error_tail"]))
    conn.commit()


def run_batch(tasks, kind="window", workers=6, progress=True):
    """tasks: list of (config, shot_id[, window_idx]). Cached jobs skipped."""
    conn = connect()
    table = "window_jobs" if kind == "window" else "full_encodes"
    id_col = "window_idx" if kind == "window" else "shot_id"
    pending = []
    for t in tasks:
        config, shot_id = t[0], t[1]
        if kind == "window":
            jid = window_job_id(config["config_id"], shot_id, t[2])
        else:
            jid = full_job_id(config["config_id"], shot_id)
        hit = conn.execute(f"SELECT 1 FROM {table} WHERE job_id=?", (jid,)).fetchone()
        if not hit:
            pending.append(t)
    if progress:
        print(f"[search-encode] {kind}: {len(pending)} pending, "
              f"{len(tasks) - len(pending)} cached")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {}
        for t in pending:
            if kind == "window":
                futs[pool.submit(encode_window, t[0], t[1], t[2])] = t
            else:
                futs[pool.submit(encode_full_shot, t[0], t[1])] = t
        for k, fut in enumerate(as_completed(futs), 1):
            r = fut.result()
            if kind == "window":
                persist_window(conn, r)
            else:
                persist_full(conn, r)
            if progress:
                tag = "FAIL" if r["status"] == "failed" else "ok"
                print(f"[search-encode] {k}/{len(pending)} {tag:4s} {r['job_id']} "
                      f"bytes={r.get('file_bytes')} ssim={r.get('ssim_avg')}",
                      flush=True)
    conn.close()


def load_windows(conn):
    rows = conn.execute("SELECT * FROM window_jobs WHERE status='ok'").fetchall()
    return [dict(r) for r in rows]


def load_full(conn):
    rows = conn.execute("SELECT * FROM full_encodes WHERE status='ok'").fetchall()
    return [dict(r) for r in rows]
