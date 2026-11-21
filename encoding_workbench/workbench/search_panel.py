"""Web panel for the revocable early-stop searcher.

Rendered as one self-contained page plus JSON/action endpoints and mounted
into the existing workbench app. The panel distinguishes three evidence
grades by colour and label and never lets one impersonate another:

  measured_full        real whole-shot/full-film encode rows
  measured_windows     real 30-frame sample window measurements
  estimated            aggregates extrapolated from sampled windows
"""
import html
import json
import urllib.parse

from search_common import (FLOORS_DEFAULT, ORDER_OVERRIDES, SHOTS,
                           shot_frames, window_frames_of, windows_of)
from searcher import SearchRun, list_runs

PANEL_CSS = """
.se-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(360px,1fr)); gap:14px; }
.evi-full { color:var(--good); font-weight:700; }
.evi-win  { color:var(--accent); font-weight:700; }
.evi-est  { color:#f2c14e; font-weight:700; }
.evi-none { color:var(--mut); }
.state-dead { opacity:.62; }
.ci { font-variant-numeric:tabular-nums; font-size:12px; color:var(--mut); }
.ledg td { font-size:12px; vertical-align:top; }
.tag.r1 { background:#3b1620; color:var(--bad); }
.tag.r2 { background:#2c2140; color:var(--purple); }
.tag.reopen { background:#14361f; color:var(--good); }
.tag.locked { background:#13313f; color:var(--accent); }
details.det { margin-top:8px; }
details.det summary { cursor:pointer; color:var(--mut); }
.evi-legend span { margin-right:16px; font-size:12px; }
"""


def _esc(x):
    return html.escape(str(x))


def panel_css():
    return PANEL_CSS


def panel_page(query=None):
    query = query or {}
    runs = list_runs()
    selected = query.get("run", [runs[0]["run_id"] if runs else ""])[0]
    if not runs:
        return _shell("<div class='card'>尚无搜索 run，请先运行 "
                      "<code>python3 run_search_demo.py</code>。</div>")
    if not any(r["run_id"] == selected for r in runs):
        selected = runs[0]["run_id"]
    run = SearchRun.load(selected)
    d = run.to_dict()
    return _shell(_body(d, runs))


def _shell(inner):
    return (f"<style>{PANEL_CSS}</style>"
            f"<div class='banner warn' style='background:#13313f;border-color:#1f4a5c'>"
            f"<b>早停搜索器</b>：所有淘汰都可撤销。黄色为抽样外推估计值，"
            f"蓝色为真实窗口测量，绿色为真实完整编码——三者不允许互相冒充。</div>{inner}")


def _run_selector(runs, current):
    opts = "".join(
        f"<option value='{r['run_id']}' {'selected' if r['run_id']==current else ''}>"
        f"{r['policy']} / {r['order_key']} / {r['created_at']} "
        f"(存活 {r['alive']} / 淘汰 {r['eliminated']})</option>"
        for r in runs)
    return (f"<div class='card' style='margin-bottom:16px'>"
            f"<form method='get' action='/search'><label>选择搜索 run "
            f"<select name='run' onchange='this.form.submit()'>{opts}</select>"
            f"</label></form></div>")


def _body(d, runs):
    parts = [_run_selector(runs, d["run_id"])]
    parts.append(_overview_card(d))
    parts.append(_constraint_card(d))
    parts.append(_candidate_grid(d))
    parts.append(_ledger_card(d))
    parts.append(_audit_card(d))
    parts.append(_order_compare_card(runs))
    return "".join(parts)


def _overview_card(d):
    n_alive = sum(1 for c in d["candidates"].values() if c["alive"])
    n_dead = len(d["candidates"]) - n_alive
    order_names = " → ".join(
        next(s["name"] for s in SHOTS if s["id"] == sid) for sid in d["order"])
    return (
        "<div class='grid3' style='margin-bottom:16px'>"
        f"<div class='card'><h2>抽样策略</h2><div class='kpi'>{_esc(d['policy'])}</div>"
        f"<div class='muted'>次序：{_esc(d['order_key'])}（{_esc(order_names)}）</div></div>"
        f"<div class='card'><h2>真实编码帧数 / 预算</h2>"
        f"<div class='kpi'>{d['frames_spent']:,} <small>/ {d['budget_frames']:,} 帧</small></div>"
        f"<div class='muted'>静景窗口 90 帧、其余窗口 30 帧，均为真实编码测量</div></div>"
        f"<div class='card'><h2>存活 / 已淘汰</h2>"
        f"<div class='kpi'>{n_alive} <small>/ {n_dead}</small></div>"
        f"<div class='muted'>追加窗口 {len(d['append_log'])} 次，锁定补码 "
        f"{len(d['lock_log'])} 次</div></div></div>")


def _constraint_card(d):
    rows = ""
    for s in SHOTS:
        sid = s["id"]
        strict = '<span class="tag r1">独立更严约束</span>' if sid == "burst" else ""
        rows += (f"<tr><td>{_esc(s['name'])}（{sid}）</td><td>{s['seconds']}s / "
                 f"{shot_frames(sid)} 帧 / {len(windows_of(sid))} 窗口</td>"
                 f"<td>{d['floors'][sid]:.3f} {strict}</td></tr>")
    return (f"<div class='card' style='margin-bottom:16px'><h2>素材与镜头级最低质量约束</h2>"
            f"<table><thead><tr><th>镜头</th><th>规模</th><th>SSIM 下限</th></tr>"
            f"</thead><tbody>{rows}</tbody></table>"
            f"<p class='muted evi-legend' style='margin-bottom:0'>"
            f"<span class='evi-full'>■ measured_full 完整编码实测</span>"
            f"<span class='evi-win'>■ measured_windows 窗口实测</span>"
            f"<span class='evi-est'>■ estimated 抽样外推（非实测）</span>"
            f"<span class='evi-none'>■ unseen 未采样</span></p></div>")


def _ssim_cell(sd):
    s = sd.get("ssim")
    if not s:
        return "<span class='evi-none'>unseen</span>"
    return (f"<span class='evi-win'>{s['mean']:.5f}</span> "
            f"<span class='ci'>95% CI [{s['lower']:.5f}, {s['upper']:.5f}]</span>")


def _candidate_grid(d):
    cards = []
    for cid, c in sorted(d["candidates"].items()):
        cfg = c["config"]
        film = c["film_estimate"]
        dead = not c["alive"]
        lock_btn = ""
        if dead:
            lock_btn = (f"<form method='post' action='/search/lock' style='display:inline'>"
                        f"<input type='hidden' name='run' value='{d['run_id']}'>"
                        f"<input type='hidden' name='config' value='{cid}'>"
                        f"<button type='submit'>锁住并补做完整编码</button></form>")
        if c.get("lock"):
            lock_btn = ("<p class='tag locked'>已锁定补码 "
                        f"{c['lock']['frames']} 帧</p>") + lock_btn
        shot_rows = ""
        for s in SHOTS:
            sid = s["id"]
            sd = c["shots"].get(sid, {})
            win = ",".join(f"w{w}" for w in sd.get("windows", [])) or "—"
            full = c.get("full_encode") or {}
            if sid in full:
                fe = full[sid]
                qcell = (f"<span class='evi-full'>{fe['ssim_avg']:.5f}</span> "
                         f"<span class='ci'>full {fe['frames']} 帧实测 "
                         f"<a href='/search/video?kind=full&job={urllib.parse.quote(fe['job_id'])}' "
                         f"target='_blank'>播放</a></span>")
            else:
                qcell = _ssim_cell(sd)
            shot_rows += (f"<tr><td>{_esc(s['name'])}</td><td>{qcell}</td>"
                          f"<td class='ci'>窗口 {win}</td></tr>")
        if film["provenance"] == "unseen":
            film_cell = "<span class='evi-none'>无估计</span>"
        elif film.get("provenance") == "measured_full":
            film_cell = f"<span class='evi-full'>{film['ssim_mean']:.5f} 完整实测</span>"
        else:
            film_cell = (f"<span class='evi-est'>{film['ssim_mean']:.5f}</span> "
                         f"<span class='ci'>CI [{film['ssim_lower']:.5f}, "
                         f"{film['ssim_upper']:.5f}] · 覆盖 "
                         f"{film['coverage']*100:.0f}% · 外推非实测</span>")
        elim = ""
        if dead:
            last = c["eliminations"][-1]
            elim = (f"<p><span class='tag { 'r1' if last['rule'].startswith('R1') else 'r2'}'>"
                    f"{_esc(last['rule'])}</span> "
                    f"<span class='warn'>{_esc(last['hypothesis'])}</span></p>")
        reopen_tags = "".join(
            f"<p class='tag reopen'>已重开：{_esc(r['hypothesis'])}</p>"
            for r in c.get("reopened", []))
        cards.append(
            f"<div class='card {'state-dead' if dead else ''}'>"
            f"<h2>{_esc(cid)} <span class='muted'>{cfg['width']}x{cfg['height']} "
            f"CRF{cfg['crf']}</span></h2>"
            f"<p>全片估计：{film_cell}</p>{elim}{reopen_tags}{lock_btn}"
            f"<details class='det'><summary>逐镜头证据与抽样范围</summary>"
            f"<table class='ledg'><thead><tr><th>镜头</th><th>SSIM 证据</th><th>窗口</th>"
            f"</tr></thead><tbody>{shot_rows}</tbody></table></details></div>")
    return (f"<h2 style='font-size:17px'>候选配置（{len(cards)} 个）</h2>"
            f"<div class='se-grid'>{''.join(cards)}</div>")


def _ledger_card(d):
    rows = ""
    for e in d["ledger"]:
        ev = e["evidence"]
        if e["rule"] == "R1_floor":
            detail = (f"上限 {ev['ssim_upper']:.5f} &lt; 门槛 {ev['floor']:.3f}；"
                      f"窗口 {ev['sampled_windows']}；"
                      f"帧范围 {ev['sampled_frame_range']['start_frame']}-"
                      f"{ev['sampled_frame_range']['end_frame']}")
        else:
            detail = (f"对手 {_esc(ev['rival'])}：对手 SSIM 下界 "
                      f"{ev['rival_ssim_lower']:.5f} ≥ 本候选上限 "
                      f"{ev['victim_ssim_upper']:.5f}；对手字节上界 "
                      f"{ev['rival_bytes_upper']:,} ≤ 本候选字节下界 "
                      f"{ev['victim_bytes_lower']:,}")
        status = ("reopened" if e["status"] == "reopened" else "eliminated")
        rows += (f"<tr class='{'state-dead' if status=='reopened' else ''}'>"
                 f"<td>{_esc(e['config_id'])}</td>"
                 f"<td><span class='tag {'r1' if e['rule'].startswith('R1') else 'r2'}'>"
                 f"{_esc(e['rule'])}</span></td>"
                 f"<td>{_esc(e['shot_id'])}</td>"
                 f"<td><code>{_esc(e['hypothesis'])}</code></td>"
                 f"<td>{_esc(e['metric'])}</td>"
                 f"<td class='reason'>{detail}</td>"
                 f"<td>{'已重开' if status=='reopened' else '淘汰中'}</td></tr>")
    return (f"<div class='card' style='margin-top:18px'><h2>淘汰账本"
            f"（每条绑定抽样范围 / 质量指标 / 证据 / 假设）</h2>"
            f"<table class='ledg'><thead><tr><th>配置</th><th>规则</th><th>镜头</th>"
            f"<th>假设 ID</th><th>指标</th><th>数值证据</th><th>状态</th></tr>"
            f"</thead><tbody>{rows}</tbody></table></div>")


def _audit_card(d):
    a = d.get("audit")
    if not a:
        return ("<div class='card' style='margin-top:18px'><h2>穷举前沿对照</h2>"
                "<p class='muted'>尚未构建小规模穷举参照。</p></div>")
    front = "、".join(
        f"<code>{_esc(p['config_id'])}</code>（{p['file_bytes']:,}B / "
        f"{p['ssim_avg']:.5f}）" for p in a["exhaustive_frontier"])
    missed = "、".join(f"<code class='warn'>{_esc(m)}</code>"
                      for m in a["missed_frontier_configs"]) or "无"
    rescued = "、".join(f"<code class='good'>{_esc(m)}</code>"
                       for m in a.get("rescued_frontier_configs", [])) or "无"
    ce = "".join(
        f"<tr><td><code>{_esc(e['config_id'])}</code></td>"
        f"<td>{_esc(e['counterexample']['rule'])}</td>"
        f"<td>{_esc(', '.join(e['reopened']))}</td></tr>"
        for e in a["counterexamples"]) or "<tr><td colspan='3' class='muted'>无</td></tr>"
    return (
        f"<div class='card' style='margin-top:18px'><h2>小规模穷举率失真前沿对照与成本审计</h2>"
        f"<p><b>真实前沿：</b>{front}</p>"
        f"<p><b>漏解（前沿上却被早停淘汰，当前仍未开放）：</b>{missed}"
        f"（{a['missed_count']} 个）</p>"
        f"<p><b>锁定救回的前沿解（曾被过早放弃、靠补码反例重开）：</b>{rescued}"
        f"（累计过早放弃 {a.get('abandoned_frontier_count', a['missed_count'])} 个）</p>"
        f"<table><thead><tr><th>口径</th><th>帧数</th><th>说明</th></tr></thead><tbody>"
        f"<tr><td>穷举完整编码成本</td><td>{a['exhaustive_cost_frames']:,}</td>"
        f"<td>18 配置 × 450 帧全片（270+120+60）</td></tr>"
        f"<tr><td>本 run 搜索窗口实跑</td><td>{a['search_window_frames']:,}</td>"
        f"<td>仅真实编码的抽样窗口</td></tr>"
        f"<tr><td>锁定补码完整帧</td><td>{a['lock_full_frames']:,}</td>"
        f"<td>面板锁住被排除配置后的补算（新增 "
        f"{a['lock_newly_encoded_frames']:,} / 缓存复用 "
        f"{a['lock_reused_cached_frames']:,}）</td></tr>"
        f"<tr><td>库内完整编码总量</td><td>{a['total_full_frames_in_store']:,}</td>"
        f"<td>含穷举参照，跨 run 共享缓存</td></tr>"
        f"<tr><td><b>真正节省</b></td><td><b class='good'>"
        f"{a['saved_frames_vs_exhaustive']:,} 帧（{a['saved_pct']}%）</b></td>"
        f"<td>穷举成本 − 窗口实跑；预测帧不计入</td></tr>"
        f"</tbody></table>"
        f"<h2 style='margin-top:14px;font-size:15px'>锁定反例与同假设重开</h2>"
        f"<table><thead><tr><th>锁定配置</th><th>反例规则</th><th>重开候选</th></tr>"
        f"</thead><tbody>{ce}</tbody></table>"
        f"<p class='muted' style='margin-bottom:0'>{_esc(a['note'])}</p></div>")


def _order_compare_card(runs):
    if len(runs) < 2:
        return ""
    rows = ""
    for r in runs:
        loaded = SearchRun.load(r["run_id"])
        full = loaded.to_dict()
        early = [e["config_id"] for e in full["ledger"]]
        # Per-shot window coverage across all configs, so the ordering effect
        # on which shots were actually sampled is visible at a glance.
        coverage = {s["id"]: 0 for s in SHOTS}
        for c in full["candidates"].values():
            for sid, sd in c["shots"].items():
                coverage[sid] += len(sd["windows"])
        cov_txt = " / ".join(f"{sid}:{n}" for sid, n in coverage.items())
        missed = ""
        if full.get("audit"):
            missed = str(full["audit"].get(
                "abandoned_frontier_count", full["audit"]["missed_count"]))
        rows += (
            f"<tr><td>{_esc(r['policy'])}</td><td>{_esc(r['order_key'])}</td>"
            f"<td>{full['frames_spent']:,}</td><td>{r['alive']}</td>"
            f"<td>{r['eliminated']}</td><td class='warn'>{missed}</td>"
            f"<td class='ci'>{cov_txt}</td>"
            f"<td class='reason' style='max-width:420px'>{_esc(', '.join(early))}</td></tr>")
    return (f"<div class='card' style='margin-top:18px'><h2>抽样次序对比："
            f"哪些候选被过早放弃</h2>"
            f"<table><thead><tr><th>策略</th><th>次序</th><th>实跑帧</th>"
            f"<th>存活</th><th>淘汰</th><th>过早放弃前沿</th><th>各镜头窗口数</th>"
            f"<th>被放弃配置（淘汰理由可在上方账本重建）</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
            f"<p class='muted' style='margin-bottom:0'>burst 从片中转到片末后，"
            f"head_first 在预算内更难触达 burst，R1 门槛淘汰与 R2 支配淘汰的集合随之改变；"
            f"stratified 因每个镜头先各取一窗，对次序不敏感。所有淘汰理由均由真实编码的"
            f"局部质量记录重建，不使用预测值。</p></div>")


# -- endpoints -------------------------------------------------------------- #
def handle_lock(post):
    params = urllib.parse.parse_qs(post)
    run_id = params.get("run", [""])[0]
    config_id = params.get("config", [""])[0]
    run = SearchRun.load(run_id)
    if config_id not in run.candidates:
        return "/search?run=" + urllib.parse.quote(run_id)
    run.lock_and_full_encode(config_id)
    if run.audit is not None:
        # Re-audit against the same ground truth so lock costs and
        # counterexamples show on the page immediately after the redirect.
        try:
            reference = SearchRun.load("_exhaustive_reference")
            run.audit = run._audit_against(reference._ground_truth())
            run.save()
        except FileNotFoundError:
            pass
    return "/search?run=" + urllib.parse.quote(run_id)


def api_run(run_id):
    run = SearchRun.load(run_id)
    return json.dumps(run.to_dict(), ensure_ascii=False)
