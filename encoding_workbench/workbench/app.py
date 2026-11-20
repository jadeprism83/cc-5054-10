"""Encoding-workbench web UI (stdlib only).

Pages:
  /            overview + measured RD frontiers + selection comparison
  /compare     side-by-side selected vs rejected candidate viewer
  /failures    encoding-failure gallery with raw ffmpeg logs
  /deliverable container-overhead report and playable outputs
  /reselect    infeasible-constraint demonstration
JSON APIs back every page so figures are regenerated from measurements.
"""
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from charts import failures_table, rd_chart
from db import connect
from paths import OVERHEAD_PATH, PLAN_PATH, SEGMENTS
from selection import (greedy_allocate, joint_allocate, load_candidates,
                       run as run_selection)

CSS = """
:root { --bg:#0e1524; --panel:#16203330; --ink:#e6edf7; --mut:#93a3bd;
        --line:#26344d; --accent:#4fa8ff; --good:#7ee0a8; --warn:#ff7a59;
        --bad:#ff5d73; --purple:#b07cff; }
* { box-sizing:border-box; }
body { margin:0; background:linear-gradient(180deg,#0b1220,#0e1524 240px);
       color:var(--ink); font:14px/1.6 -apple-system,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif; }
header { padding:22px 34px 14px; border-bottom:1px solid var(--line);
         background:#0d1626; position:sticky; top:0; z-index:5; }
h1 { margin:0 0 6px; font-size:21px; }
nav a { color:var(--accent); text-decoration:none; margin-right:18px; font-weight:600; }
nav a:hover { text-decoration:underline; }
main { padding:26px 34px 60px; max-width:1280px; margin:0 auto; }
.grid2 { display:grid; grid-template-columns:1fr 1fr; gap:20px; }
.grid3 { display:grid; grid-template-columns:repeat(3,1fr); gap:18px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:14px;
        padding:18px 20px; backdrop-filter:blur(4px); }
.card h2 { margin:0 0 10px; font-size:16px; color:#cfe0ff; }
.kpi { font-size:26px; font-weight:700; }
.kpi small { font-size:13px; color:var(--mut); font-weight:400; }
table { border-collapse:collapse; width:100%; font-size:13px; }
th,td { border-bottom:1px solid var(--line); padding:7px 9px; text-align:left; }
th { color:var(--mut); font-weight:600; }
td.reason { color:var(--warn); max-width:360px; }
code { background:#0a1120; padding:2px 6px; border-radius:6px; color:#ffd9a8;
       font-size:12px; word-break:break-all; }
.tag { display:inline-block; padding:1px 9px; border-radius:999px; font-size:12px; }
.tag.ok { background:#14361f; color:var(--good); }
.tag.fail { background:#3b1620; color:var(--bad); }
.tag.greedy { background:#13313f; color:var(--accent); }
.tag.joint { background:#2c2140; color:var(--purple); }
svg { width:100%; height:auto; }
.grid { stroke:#1d2a40; stroke-width:1; }
.plot-box { fill:none; stroke:var(--line); }
.axis-label,.legend,.floor-label { fill:var(--mut); font-size:11px; }
.axis-title,.chart-title { fill:var(--ink); font-size:13px; font-weight:600; }
.raw-line { fill:none; stroke:#3a4c6e; stroke-width:1.2; }
.pt { cursor:pointer; }
.pick-ring { pointer-events:none; }
video { width:100%; border-radius:10px; background:#000; }
select,input,button { background:#0d1728; color:var(--ink); border:1px solid var(--line);
       border-radius:8px; padding:7px 10px; font-size:13px; }
button { cursor:pointer; background:var(--accent); color:#08111f; font-weight:700; }
.muted { color:var(--mut); }
.good { color:var(--good); } .bad { color:var(--bad); } .warn { color:var(--warn); }
.compare-grid { display:grid; grid-template-columns:1fr 1fr; gap:18px; }
.compare-grid video { max-height:340px; }
pre.log { background:#0a1120; border:1px solid var(--line); border-radius:10px;
       padding:14px; max-height:420px; overflow:auto; font-size:12px; color:#b9c8e0; }
.banner { border-radius:12px; padding:14px 18px; margin-bottom:18px; }
banner, .banner.bad { background:#33141c; border:1px solid #5a2432; }
.banner.good { background:#122b1c; border:1px solid #235035; }
.banner.warn { background:#33240f; border:1px solid #5a431c; }
.steps td { font-variant-numeric:tabular-nums; }
.pill { font-size:12px; color:var(--mut); border:1px solid var(--line);
        border-radius:999px; padding:2px 10px; margin-left:8px; }
"""


def load_plan():
    with open(PLAN_PATH) as f:
        return json.load(f)


def load_overhead():
    if not OVERHEAD_PATH.exists():
        return None
    with open(OVERHEAD_PATH) as f:
        return json.load(f)


def page(shell_inner, title="编码方案工作台"):
    nav = ("<nav><a href='/'>总览 / 率失真前沿</a>"
           "<a href='/compare'>入选 vs 落选对看</a>"
           "<a href='/failures'>编码失败案例</a>"
           "<a href='/deliverable'>成品与容器开销</a>"
           "<a href='/reselect'>不可行约束</a></nav>")
    return (f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{CSS}</style></head><body>"
            f"<header><h1>短片编码方案可视化工作台</h1>{nav}</header>"
            f"<main>{shell_inner}</main></body></html>")


def overview_html(plan=None):
    plan = plan or load_plan()
    floors = plan["floors"]
    g, j = plan["greedy"], plan["joint"]
    cards = [
        ("候选任务（成功/失败）", "54 / 9", "3 段 × 3 分辨率 × 6 质量档 + 9 个预期失败"),
        ("演示预算", f"{plan['budget']:,} B", f"最低门槛成本 {plan['guide']['floor_cost']:,} B"),
        ("贪心总 SSIM", f"{g['total_ssim']:.5f}", f"占用 {g['total_bytes']:,} B"),
        ("联合总 SSIM", f"{j['total_ssim']:.5f}", f"占用 {j['total_bytes']:,} B"),
    ]
    kpis = "".join(
        f"<div class='card'><h2>{t}</h2><div class='kpi'>{v}</div>"
        f"<div class='muted'>{d}</div></div>" for t, v, d in cards)

    charts = []
    for seg in SEGMENTS:
        sid = seg["id"]
        cands = plan["candidates"][sid]
        gpick = g["picks"].get(sid)
        jpick = j["picks"].get(sid)
        chart = rd_chart(cands, floor=floors[sid],
                         picks={sid: jpick} if jpick else None,
                         title=f"{seg['name']}（{sid}）实测率失真前沿")
        rows = []
        for c in sorted(cands, key=lambda c: c["file_bytes"]):
            marks = []
            if gpick and c["job_id"] == gpick["job_id"]:
                marks.append("<span class='tag greedy'>贪心选</span>")
            if jpick and c["job_id"] == jpick["job_id"]:
                marks.append("<span class='tag joint'>联合选</span>")
            if not c["on_frontier"]:
                marks.append("<span class='pill'>前沿外</span>")
            rows.append(
                "<tr>"
                f"<td>{c['quality_label']}</td><td>{c['width']}x{c['height']}</td>"
                f"<td>{c['file_bytes']:,}</td><td>{c['ssim_avg']:.5f}</td>"
                f"<td>{(c.get('psnr_avg') or 0):.2f}</td>"
                f"<td>{c['encode_secs']}s</td><td>{' '.join(marks)}</td></tr>")
        charts.append(
            f"<div class='card'><h2>{seg['name']} <span class='muted'>{seg['desc']}</span></h2>"
            f"{chart}<details><summary class='muted'>展开全部 {len(cands)} 个测量点</summary>"
            f"<table><thead><tr><th>方案</th><th>分辨率</th><th>字节</th><th>SSIM</th>"
            f"<th>PSNR</th><th>耗时</th><th>标记</th></tr></thead><tbody>"
            f"{''.join(rows)}</tbody></table></details></div>")

    gsteps = "".join(
        f"<tr><td>{s['segment']}</td><td><code>{s['from_job']}</code> → "
        f"<code>{s['to_job']}</code></td><td>{s['extra_bytes']:,}</td>"
        f"<td>{s['ssim_gain']:.6f}</td><td>{s['marginal']}</td></tr>"
        for s in g.get("steps", []))
    explain = f"""
    <div class='card'>
      <h2>为什么两种做法在这里给出相反结论</h2>
      <p>预算 {plan['budget']:,} 字节、各段 SSIM ≥ {list(floors.values())[0]:.3f}。</p>
      <p><b class='warn'>逐段贪心</b>从最便宜的合格方案起步，每一步都买“每字节 SSIM 增益”
      最高的升级。静景（seg3）与标题（seg1）的小升级极便宜，于是预算被这些
      边际增益吃掉；等到织物段（seg2）想跨档升级时，钱已经不够了。</p>
      <p><b style='color:var(--purple)'>全片联合</b>把三段当成一个多选背包求解，允许
      “拆东墙补西墙”：它把静景退回便宜档（
      {g['picks']['seg3_static']['quality_label']} →
      {j['picks']['seg3_static']['quality_label']}，
      省 {g['picks']['seg3_static']['file_bytes']-j['picks']['seg3_static']['file_bytes']:,} B），
      把钱挪给标题跨到 {j['picks']['seg1_title']['quality_label']}，
      全片总 SSIM 从 <b>{g['total_ssim']:.5f}</b> 提升到 <b class='good'>{j['total_ssim']:.5f}</b>。</p>
      <details><summary class='muted'>贪心的逐步购买记录</summary>
      <table class='steps'><thead><tr><th>段落</th><th>升级</th><th>多花字节</th>
      <th>SSIM 增益</th><th>边际增益(/MB×1e6)</th></tr></thead>
      <tbody>{gsteps}</tbody></table></details>
    </div>"""
    return page(
        f"<div class='grid3' style='margin-bottom:20px'>{kpis}</div>"
        f"{explain}"
        f"<div style='margin-top:20px'>{''.join(charts)}</div>")


def compare_html(query):
    plan = load_plan()
    seg = query.get("seg", [SEGMENTS[0]["id"]])[0]
    cands = sorted(plan["candidates"][seg], key=lambda c: c["file_bytes"])
    gpick = plan["greedy"]["picks"][seg]
    jpick = plan["joint"]["picks"][seg]
    default_left = jpick["job_id"]
    default_right = next((c["job_id"] for c in cands
                          if c["job_id"] not in (gpick["job_id"], jpick["job_id"])),
                         cands[0]["job_id"])
    left_id = query.get("left", [default_left])[0]
    right_id = query.get("right", [default_right])[0]
    left = next(c for c in cands if c["job_id"] == left_id)
    right = next(c for c in cands if c["job_id"] == right_id)

    def options(cur):
        return "".join(
            f"<option value='{c['job_id']}' {'selected' if c['job_id']==cur else ''}>"
            f"{c['quality_label']} · {c['file_bytes']:,}B · SSIM {c['ssim_avg']:.5f}</option>"
            for c in cands)

    def panel(c, label, cls):
        return (f"<div class='card'><h2>{label} "
                f"<span class='tag {cls}'>{c['quality_label']}</span></h2>"
                f"<video controls preload='metadata' src='/video?job={urllib.parse.quote(c['job_id'])}'></video>"
                f"<table><tr><th>分辨率</th><td>{c['width']}x{c['height']}</td></tr>"
                f"<tr><th>文件</th><td>{c['file_bytes']:,} B（码流 {c['stream_bytes']:,} B）</td></tr>"
                f"<tr><th>SSIM 平均/最差帧</th><td>{c['ssim_avg']:.5f} / {c['ssim_min']:.5f}</td></tr>"
                f"<tr><th>PSNR</th><td>{c.get('psnr_avg') or 0:.2f} dB</td></tr></table></div>")

    form = (f"<div class='card' style='margin-bottom:18px'><h2>选择对看方案 — "
            f"{next(s['name'] for s in SEGMENTS if s['id']==seg)}</h2>"
            f"<form method='get' action='/compare'>"
            f"<label>段落 <select name='seg' onchange='this.form.submit()'>"
            + "".join(f"<option value='{s['id']}' {'selected' if s['id']==seg else ''}>{s['name']}</option>"
                      for s in SEGMENTS)
            + f"</select></label> &nbsp; "
            f"<label>左（入选/任一） <select name='left'>{options(left_id)}</select></label> &nbsp; "
            f"<label>右（落选/任一） <select name='right'>{options(right_id)}</select></label> "
            f"<button type='submit'>对看</button></form>"
            f"<p class='muted' style='margin-bottom:0'>白圈 = 联合方案入选点；两个视频均为解码后浏览器原生播放，"
            f"可同步手动对比画质与体积。</p></div>")
    delta_ssim = left["ssim_avg"] - right["ssim_avg"]
    delta_b = left["file_bytes"] - right["file_bytes"]
    summary = (f"<div class='card' style='margin-bottom:18px'><h2>差值（左 − 右）</h2>"
               f"<p>SSIM 差 <b class='{'good' if delta_ssim>=0 else 'bad'}'>{delta_ssim:+.5f}</b>　"
               f"体积差 <b class='{'warn' if delta_b>0 else 'good'}'>{delta_b:+,} B</b></p></div>")
    return page(form + summary +
                f"<div class='card' style='margin-bottom:18px'>"
                "<button type='button' onclick='syncPlay(true)'>同步播放</button> "
                "<button type='button' onclick='syncPlay(false)'>同步暂停归零</button>"
                "<span class='muted' style='margin-left:10px'>两个视频会跳到同一时间点一起播放。</span></div>"
                f"<div class='compare-grid' id='cmp'>{panel(left,'左','joint')}{panel(right,'右','greedy')}</div>"
                "<script>"
                "function syncPlay(play){"
                "const vs=[...document.querySelectorAll('#cmp video')];"
                "vs.forEach(v=>{v.currentTime=0;});"
                "if(play){Promise.all(vs.map(v=>v.play())).catch(()=>{});}"
                "else{vs.forEach(v=>v.pause());}"
                "}"
                "</script>",
                title="对看")


def failures_html():
    conn = connect()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM jobs WHERE status='failed' ORDER BY segment_id, quality_label")]
    conn.close()
    cards = []
    for r in rows:
        log_url = "/log?job=" + urllib.parse.quote(r["job_id"])
        cards.append(
            "<div class='card'>"
            f"<h2><span class='tag fail'>失败</span> {r['segment_id']} · {r['quality_label']}</h2>"
            f"<p class='muted'>{r['codec']} @ {r['width']}x{r['height']}，ffmpeg 退出码 {r['ffmpeg_rc']}</p>"
            f"<p class='warn'>{r['fail_reason'] or ''}</p>"
            f"<p><a href='{log_url}'>查看完整 ffmpeg 日志</a></p></div>")
    return page(f"<div class='card'><h2>保留的编码失败案例（{len(rows)} 个，均为预期失败）</h2>"
                f"{failures_table(rows)}</div>"
                f"<div class='grid3' style='margin-top:20px'>{''.join(cards)}</div>",
                title="失败案例")


def deliverable_html():
    rep = load_overhead()
    if rep is None:
        return page("<div class='card'>尚未生成成品，请先运行 mux_deliverables。</div>")
    rows = "".join(
        f"<tr><td>{d['segment']}</td><td><code>{d['deliverable'].split('/')[-1]}</code></td>"
        f"<td>{d['grid_bytes']:,}</td><td>{d['file_bytes']:,}</td>"
        f"<td>{d['stream_bytes']:,}</td><td>{d['overhead_bytes']:,}</td>"
        f"<td>{d['overhead_pct']}%</td></tr>"
        for d in rep["per_segment"])
    banner_cls = "good" if rep["within_budget_delivered"] else "bad"
    banner = (
        f"<div class='banner {banner_cls}'><b>预算校验：</b>"
        f"预算 {rep['budget_bytes']:,} B；网格文件合计 {rep['grid_total_bytes']:,} B；"
        f"交付 MP4 合计（含容器）{rep['delivered_mp4_total_bytes']:,} B。"
        + ("<b class='good'> 未超限，选择有效。</b>" if rep["within_budget_delivered"]
           else f"<b class='bad'> 超限 {rep['over_by_bytes']:,} B，需回到选择步骤重排。</b>")
        + "</div>")
    mk, pv = rep["full_film_mkv"], rep["preview_mp4"]
    hist = rep.get("gate_history", [])
    hist_rows = "".join(
        f"<tr><td>第 {h['round']} 轮</td><td>{h['budget']:,}</td>"
        f"<td>{h['delivered']:,}</td>"
        f"<td class='{'good' if h['within'] else 'bad'}'>"
        f"{'通过' if h['within'] else '超限，收紧预算重排'}</td></tr>"
        for h in hist)
    gate_card = (f"<div class='card' style='margin-top:18px'><h2>预算闸门轮次</h2>"
                 f"<table><thead><tr><th>轮次</th><th>当轮预算</th><th>交付字节</th>"
                 f"<th>结果</th></tr></thead><tbody>{hist_rows}</tbody></table></div>") \
        if len(hist) > 1 or not rep["within_budget_delivered"] else ""
    return page(
        banner +
        "<div class='grid2' style='margin-bottom:18px'>"
        f"<div class='card'><h2>全片顺序播放（预览版 720p，浏览器可播）</h2>"
        f"<video controls preload='metadata' src='/play?name=preview'></video>"
        f"<p class='muted'>该预览经过统一重缩放与重编码，仅用于播放，不计入字节预算。</p></div>"
        f"<div class='card'><h2>原始码流合集 MKV（分辨率可变化）</h2>"
        f"<p>文件：<code>{mk['file'].split('/')[-1]}</code><br>"
        f"总大小 {mk['file_bytes']:,} B，其中容器开销 {mk['overhead_bytes']:,} B "
        f"（{mk['overhead_pct']}%）。可用 VLC/ffplay 播放或下载检查。</p>"
        f"<p><a href='/play?name=mkv'>下载/打开 MKV</a></p>"
        f"<p class='muted'>MP4 不允许中途换分辨率，所以预算内的原始分辨率合集只能放 MKV；"
        "字节预算始终按各段入选网格文件计。</p></div></div>"
        f"<div class='card'><h2>最终容器开销（文件大小 − 视频码流负载）</h2>"
        f"<table><thead><tr><th>段落</th><th>交付文件</th><th>网格字节</th>"
        f"<th>文件字节</th><th>码流字节</th><th>容器开销</th><th>占比</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        f"<p class='muted' style='margin-top:10px'>预览 MP4：{pv['file_bytes']:,} B，"
        f"开销 {pv['overhead_bytes']:,} B（{pv['overhead_pct']}%）。</p></div>"
        f"{gate_card}",
        title="成品与开销")


def reselect_html(query):
    plan = load_plan()
    conn = connect()
    by_seg = load_candidates(conn)
    conn.close()
    floors = dict(plan["floors"])
    if "budget" in query:
        try:
            budget = int(query["budget"][0])
        except ValueError:
            budget = plan["tight_budget"]
    else:
        budget = plan["tight_budget"]
    g = greedy_allocate(by_seg, floors, budget)
    j = joint_allocate(by_seg, floors, budget)
    cheapest = sum(min((c for c in lst if c.ssim_avg >= floors[sid]),
                       key=lambda c: c.file_bytes).file_bytes
                   for sid, lst in by_seg.items())
    body = (
        f"<div class='card' style='margin-bottom:18px'><h2>约束可行性试验台</h2>"
        f"<form method='get' action='/reselect'>"
        f"<label>总字节上限 <input name='budget' type='number' value='{budget}' style='width:160px'> B</label> "
        f"<button>重新求解</button></form>"
        f"<p class='muted'>各段最低 SSIM 门槛固定为 {list(floors.values())[0]:.3f}；"
        f"仅满足门槛的最便宜组合就需要 <b>{cheapest:,} B</b>。</p></div>")
    if not (g["feasible"] and j["feasible"]):
        body += (f"<div class='banner bad'><b>没有可行组合。</b>"
                 f"贪心：{g['reason']}<br>联合：{j['reason']}<br>"
                 "两个约束（总字节上限与各段最低质量）无法同时满足——"
                 "必须放宽预算、降低质量门槛，或允许更换编码器/分辨率范围。</div>")
    else:
        body += (f"<div class='banner good'>预算 {budget:,} B 内存在可行组合："
                 f"贪心 {g['total_bytes']:,} B / 联合 {j['total_bytes']:,} B。</div>")
    # Show the floor-cost table explaining the hard lower bound.
    rows = ""
    for sid, lst in by_seg.items():
        c = min((c for c in lst if c.ssim_avg >= floors[sid]),
                key=lambda c: c.file_bytes)
        rows += (f"<tr><td>{sid}</td><td>{c.quality_label}</td>"
                 f"<td>{c.file_bytes:,}</td><td>{c.ssim_avg:.5f}</td></tr>")
    body += (f"<div class='card'><h2>各段达标最便宜方案（可行性下界 = {cheapest:,} B）</h2>"
             f"<table><thead><tr><th>段落</th><th>方案</th><th>字节</th><th>SSIM</th></tr>"
             f"</thead><tbody>{rows}</tbody></table></div>")
    return page(body, title="重排与不可行")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype="text/html; charset=utf-8", code=200, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/":
                return self._send(overview_html())
            if path == "/compare":
                return self._send(compare_html(query))
            if path == "/failures":
                return self._send(failures_html())
            if path == "/deliverable":
                return self._send(deliverable_html())
            if path == "/reselect":
                return self._send(reselect_html(query))
            if path == "/api/plan":
                return self._send(json.dumps(load_plan(), ensure_ascii=False),
                                  "application/json")
            if path == "/api/overhead":
                rep = load_overhead()
                return self._send(json.dumps(rep or {}, ensure_ascii=False),
                                  "application/json")
            if path == "/video":
                return self._serve_job_video(query.get("job", [""])[0])
            if path == "/play":
                return self._serve_deliverable(query.get("name", [""])[0])
            if path == "/log":
                return self._serve_log(query.get("job", [""])[0])
            if path == "/health":
                return self._send("ok", "text/plain")
            self._send("not found", code=404)
        except FileNotFoundError as e:
            self._send(f"missing: {e}", code=404)
        except Exception:
            import traceback
            self._send("<pre>" + traceback.format_exc() + "</pre>", code=500)

    def _serve_job_video(self, job_id):
        conn = connect()
        row = conn.execute("SELECT output_path FROM jobs WHERE job_id=?",
                           (job_id,)).fetchone()
        conn.close()
        if not row:
            return self._send("unknown job", code=404)
        self._range_file(row["output_path"], "video/mp4")

    def _serve_deliverable(self, name):
        ddir = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "out", "encodes", "_deliverables")
        mapping = {"preview": "short_film_preview_720p.mp4",
                   "mkv": "short_film_full.mkv"}
        if name not in mapping:
            return self._send("unknown deliverable", code=404)
        self._range_file(os.path.join(ddir, mapping[name]),
                         "video/mp4" if name == "preview" else "video/x-matroska")

    def _serve_log(self, job_id):
        conn = connect()
        row = conn.execute("SELECT log_path FROM jobs WHERE job_id=?",
                           (job_id,)).fetchone()
        conn.close()
        if not row:
            return self._send("unknown job", code=404)
        with open(row["log_path"], "rb") as f:
            self._send(f.read(), "text/plain; charset=utf-8")

    def _range_file(self, fpath, ctype):
        if not os.path.exists(fpath):
            return self._send("file missing", code=404)
        size = os.path.getsize(fpath)
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            start = int(a) if a else 0
            end = int(b) if b else size - 1
        length = end - start + 1
        self.send_response(206 if rng else 200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(fpath, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(1 << 18, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)


def main(port=8765):
    if not PLAN_PATH.exists():
        run_selection()
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[web] http://localhost:{port}")
    srv.serve_forever()


if __name__ == "__main__":
    import sys
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 8765)
