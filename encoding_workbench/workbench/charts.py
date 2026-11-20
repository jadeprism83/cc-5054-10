"""Server-side SVG charts: measured rate-distortion frontiers per segment.

Pure stdlib SVG generation -- no charting dependencies. Each chart shows:
  * every successful candidate as a point, connected in byte order
  * Pareto-frontier points highlighted, off-frontier points muted
  * non-monotonic measurements explicitly marked
  * quality floor and current budget as guide lines
"""
import html

W, H = 860, 420
ML, MR, MT, MB = 78, 24, 40, 58
PLOT_W, PLOT_H = W - ML - MR, H - MT - MB

COLORS = {"1080p": "#4fa8ff", "720p": "#7ee0a8", "480p": "#f2c14e"}
FRONTIER = "#ff7a59"
NONMONO = "#b07cff"
FLOOR = "#ff5d73"


def _nice_ticks(lo, hi, n=6):
    if hi <= lo:
        hi = lo + 1
    step = (hi - lo) / n
    return [lo + i * step for i in range(n + 1)]


def rd_chart(candidates, floor=None, picks=None, title=""):
    pts = sorted(candidates, key=lambda c: c["file_bytes"])
    xs = [c["file_bytes"] for c in pts]
    ys = [c["ssim_avg"] for c in pts]
    x0, x1 = min(xs), max(xs)
    # SSIM axis is zoomed to the measured range so differences are visible.
    y0, y1 = min(ys), max(ys)
    ypad = (y1 - y0) * 0.12 or 0.001
    y0, y1 = y0 - ypad, y1 + ypad

    def X(v):
        return ML + (v - x0) / (x1 - x0) * PLOT_W

    def Y(v):
        return MT + (1 - (v - y0) / (y1 - y0)) * PLOT_H

    parts = [f'<svg viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" '
             f'role="img" aria-label="{html.escape(title)}">']
    parts.append(f'<text x="{W/2}" y="24" text-anchor="middle" class="chart-title">'
                 f'{html.escape(title)}</text>')
    # grid + axes
    for tx in _nice_ticks(x0, x1):
        parts.append(f'<line x1="{X(tx):.1f}" y1="{MT}" x2="{X(tx):.1f}" '
                     f'y2="{MT+PLOT_H}" class="grid"/>')
        parts.append(f'<text x="{X(tx):.1f}" y="{MT+PLOT_H+18}" text-anchor="middle" '
                     f'class="axis-label">{tx/1000:.0f} KB</text>')
    for ty in _nice_ticks(y0, y1):
        parts.append(f'<line x1="{ML}" y1="{Y(ty):.1f}" x2="{ML+PLOT_W}" '
                     f'y2="{Y(ty):.1f}" class="grid"/>')
        parts.append(f'<text x="{ML-8}" y="{Y(ty)+4:.1f}" text-anchor="end" '
                     f'class="axis-label">{ty:.4f}</text>')
    parts.append(f'<rect x="{ML}" y="{MT}" width="{PLOT_W}" height="{PLOT_H}" class="plot-box"/>')
    parts.append(f'<text x="{ML+PLOT_W/2}" y="{H-12}" text-anchor="middle" class="axis-title">'
                 f'文件体积 →</text>')
    parts.append(f'<text transform="translate(20,{MT+PLOT_H/2}) rotate(-90)" '
                 f'text-anchor="middle" class="axis-title">SSIM（回缩至 1080p）→</text>')

    # raw polyline connecting points in byte order
    poly = " ".join(f"{X(c['file_bytes']):.1f},{Y(c['ssim_avg']):.1f}" for c in pts)
    parts.append(f'<polyline points="{poly}" class="raw-line"/>')

    # non-monotonic detection: ssim decreases as bytes increase
    prev_y = None
    nonmono = set()
    for c in pts:
        if prev_y is not None and c["ssim_avg"] < prev_y - 1e-9:
            nonmono.add(c["job_id"])
        prev_y = c["ssim_avg"]

    pick_ids = {c["job_id"] for c in (picks or {}).values()} \
        if picks and isinstance(next(iter(picks.values())), dict) else set(picks or [])

    for c in pts:
        x, y = X(c["file_bytes"]), Y(c["ssim_avg"])
        color = COLORS.get(c["res_label"], "#888")
        if c["job_id"] in nonmono:
            color = NONMONO
        r = 5.5 if c.get("on_frontier") else 3.5
        cls = "pt frontier" if c.get("on_frontier") else "pt"
        stroke = FRONTIER if c.get("on_frontier") else "#233047"
        sw = 2.2 if c.get("on_frontier") else 1
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}" '
                     f'stroke="{stroke}" stroke-width="{sw}" class="{cls}">'
                     f'<title>{html.escape(_tip(c))}</title></circle>')
        if c["job_id"] in pick_ids:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="10" fill="none" '
                         f'stroke="#ffffff" stroke-width="2.5" class="pick-ring"/>')

    # quality floor
    if floor is not None and y0 <= floor <= y1:
        parts.append(f'<line x1="{ML}" y1="{Y(floor):.1f}" x2="{ML+PLOT_W}" '
                     f'y2="{Y(floor):.1f}" stroke="{FLOOR}" stroke-width="2" '
                     f'stroke-dasharray="7 5"/>')
        parts.append(f'<text x="{ML+PLOT_W-6}" y="{Y(floor)-6:.1f}" text-anchor="end" '
                     f'fill="{FLOOR}" class="floor-label">最低质量 {floor:.3f}</text>')

    # legend
    lx = ML + 10
    for i, (label, col) in enumerate(COLORS.items()):
        parts.append(f'<circle cx="{lx+i*110}" cy="{MT+14}" r="5" fill="{col}"/>')
        parts.append(f'<text x="{lx+10+i*110}" y="{MT+18}" class="legend">{label}</text>')
    parts.append(f'<circle cx="{lx+330}" cy="{MT+14}" r="5" fill="{NONMONO}"/>')
    parts.append(f'<text x="{lx+340}" y="{MT+18}" class="legend">非单调测量</text>')
    parts.append(f'<circle cx="{lx+460}" cy="{MT+14}" r="5.5" fill="#888" '
                 f'stroke="{FRONTIER}" stroke-width="2"/>')
    parts.append(f'<text x="{lx+472}" y="{MT+18}" class="legend">Pareto 前沿点</text>')
    parts.append("</svg>")
    return "".join(parts)


def _tip(c):
    return (f"{c['quality_label']} | {c['width']}x{c['height']} | "
            f"{c['file_bytes']} 字节 | SSIM {c['ssim_avg']:.5f} | "
            f"PSNR {c.get('psnr_avg') or 0:.2f} dB")


def failures_table(failed_rows):
    rows = []
    for r in failed_rows:
        rows.append(
            "<tr>"
            f"<td>{html.escape(r['segment_id'])}</td>"
            f"<td>{html.escape(r['codec'])}</td>"
            f"<td>{r['width']}x{r['height']}</td>"
            f"<td>{html.escape(r['quality_label'])}</td>"
            f"<td class='reason'>{html.escape(r['fail_reason'] or '')}</td>"
            f"<td><code>{html.escape((r['error_tail'] or '').strip().splitlines()[-1] if r['error_tail'] else '')}</code></td>"
            "</tr>")
    return ("<table class='fails'><thead><tr><th>段落</th><th>编码器</th><th>分辨率</th>"
            "<th>任务</th><th>预设失败原因</th><th>ffmpeg 最后一行</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table>")
