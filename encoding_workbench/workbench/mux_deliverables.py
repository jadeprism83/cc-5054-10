"""Mux the selected encodes into deliverables and measure container overhead.

Deliverables:
  * one independently playable MP4 per selected segment (the files the byte
    budget actually pays for -- no re-encode, stream-copied from the grid)
  * one whole-film Matroska concatenation for sequential playback (MKV allows
    resolution changes mid-stream; MP4 does not, so it is a convenience copy
    and is explicitly kept OUT of the byte-budget accounting)

Container overhead = file size - sum of elementary video stream payload
bytes (summed over video packets with ffprobe).
"""
import json
import subprocess

from paths import (ENC_DIR, FFMPEG, FFPROBE, OVERHEAD_PATH, RESULT_DIR,
                   SEGMENTS)


def _packet_stream_size(path):
    cmd = [FFPROBE, "-v", "error", "-select_streams", "v:0",
           "-show_entries", "packet=size", "-of", "csv=p=0", str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return sum(int(x) for x in out.stdout.split())


def _container_overhead(path):
    file_size = path.stat().st_size
    stream_size = _packet_stream_size(path)
    return dict(file=str(path), file_bytes=file_size,
                stream_bytes=stream_size,
                overhead_bytes=file_size - stream_size,
                overhead_pct=round(100 * (file_size - stream_size) / file_size, 3))


def build_deliverables(plan_key="joint"):
    with open(RESULT_DIR / "selection.json") as f:
        plan = json.load(f)
    chosen = plan[plan_key]
    if not chosen["feasible"]:
        raise RuntimeError(f"{plan_key} selection is infeasible, nothing to mux")

    out_dir = ENC_DIR / "_deliverables"
    out_dir.mkdir(exist_ok=True)
    per_segment = []
    manifest = out_dir / "concat.txt"
    ordered_paths = []
    for seg in SEGMENTS:
        sid = seg["id"]
        cand = chosen["picks"][sid]
        src = cand["output_path"]
        dst = out_dir / f"{sid}_selected.mp4"
        # Stream copy: byte content identical to the budgeted grid output,
        # only re-homed under a clean deliverable name.
        subprocess.run([FFMPEG, "-hide_banner", "-y", "-i", src,
                        "-c", "copy", "-movflags", "+faststart", str(dst)],
                       check=True, capture_output=True)
        ordered_paths.append(dst)
        per_segment.append(dict(segment=sid, source=src, deliverable=str(dst),
                                grid_bytes=cand["file_bytes"],
                                **_container_overhead(dst)))

    with open(manifest, "w") as f:
        for p in ordered_paths:
            f.write(f"file '{p}'\n")
    film_mkv = out_dir / "short_film_full.mkv"
    subprocess.run([FFMPEG, "-hide_banner", "-y", "-f", "concat", "-safe", "0",
                    "-i", str(manifest), "-c", "copy", str(film_mkv)],
                   check=True, capture_output=True)
    film_stats = _container_overhead(film_mkv)

    # Also produce a browser-playable preview where every segment is scaled
    # to 720p first. This one DOES re-encode, is purely a preview, and is not
    # counted against the budget.
    preview = out_dir / "short_film_preview_720p.mp4"
    filters, maps = [], []
    for i, p in enumerate(ordered_paths):
        filters.append(f"[{i}:v]scale=1280:720:flags=lanczos,setsar=1[v{i}]")
        maps.append(f"[v{i}]")
    fc = ";".join(filters) + ";" + "".join(maps) + \
        f"concat=n={len(ordered_paths)}:v=1:a=0[vout]"
    cmd = [FFMPEG, "-hide_banner", "-y"]
    for p in ordered_paths:
        cmd += ["-i", str(p)]
    cmd += ["-filter_complex", fc, "-map", "[vout]",
            "-c:v", "libx264", "-crf", "23", "-preset", "veryfast",
            "-movflags", "+faststart", str(preview)]
    subprocess.run(cmd, check=True, capture_output=True)
    preview_stats = _container_overhead(preview)

    budget = plan["budget"]
    # Budget covers the elementary-stream payload of the chosen grid files;
    # compare both against raw grid sizes and against delivered MP4 sizes.
    grid_total = sum(c["file_bytes"] for c in chosen["picks"].values())
    delivered_total = sum(d["file_bytes"] for d in per_segment)
    report = dict(
        plan_key=plan_key,
        budget_bytes=budget,
        grid_total_bytes=grid_total,
        delivered_mp4_total_bytes=delivered_total,
        within_budget_grid=grid_total <= budget,
        within_budget_delivered=delivered_total <= budget,
        over_by_bytes=max(0, delivered_total - budget),
        per_segment=per_segment,
        full_film_mkv=film_stats,
        preview_mp4=preview_stats,
    )
    with open(OVERHEAD_PATH, "w") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


if __name__ == "__main__":
    r = build_deliverables()
    print(json.dumps({k: v for k, v in r.items()
                      if k not in ("per_segment",)}, indent=2,
                     ensure_ascii=False))
