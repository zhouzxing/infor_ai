#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 情报聚合 · 现代化前端渲染器 (v7)

设计目标:
  1. 资讯站式现代化布局 —— 顶部粘性导航 + 全局搜索 + 主栏/侧栏双列
  2. 国际/国内资讯合并为单一「资讯」类型, 支持模糊检索(子串 + 子序列打分)
  3. 大模型 / Agent 归为一类, 按能力评估维度做统计分析, 大盘展示

本模块只负责渲染; 数据由 ai_intel_aggregator.render_html(cache) 注入。
"""
import math
import re
from datetime import datetime


def esc(s):
    return (str(s) if s is not None else "") \
        .replace("&", "&amp;").replace("<", "&lt;") \
        .replace(">", "&gt;").replace('"', "&quot;")


# ── 能力评估维度定义 ────────────────────────────────────────────────
DIMS = [
    ("ctx",   "上下文"),
    ("mm",    "多模态"),
    ("open",  "开放度"),
    ("cost",  "性价比"),
    ("scale", "模型规模"),
    ("fresh", "前沿度"),
]
DIM_W = {"ctx": .18, "mm": .18, "open": .16, "cost": .22, "scale": .12, "fresh": .14}


# ── 解析与打分 ──────────────────────────────────────────────────────
def _ctx_tokens(s):
    t = str(s or "").lower().replace(",", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*m\b", t)
    if m:
        return float(m.group(1)) * 1_000_000
    m = re.search(r"(\d+(?:\.\d+)?)\s*k\b", t)
    if m:
        return float(m.group(1)) * 1_000
    m = re.search(r"(\d{3,})", t)
    return float(m.group(1)) if m else 0.0


def _params_b(s):
    t = str(s or "").lower()
    m = re.search(r"(\d+(?:\.\d+)?)\s*b", t)
    if m:
        return float(m.group(1))
    m = re.search(r"(\d{3,})", t)
    return float(m.group(1)) if m else 0.0


def _score_ctx(s):
    tk = _ctx_tokens(s)
    if tk <= 0:
        return 26.0
    lo, hi = 4096.0, 2_000_000.0
    v = (math.log10(max(tk, lo)) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * 100
    return max(4.0, min(100.0, round(v, 1)))


def _score_mm(m):
    t = str(m or "").lower()
    keys = ("text", "vision", "audio", "video", "image", "code")
    n = sum(1 for k in keys if k in t)
    return min(100.0, 16.0 + n * 17.0)


def _score_open(lic, free):
    t = str(lic or "").lower()
    if "apache" in t or " mit" in t or t.strip() == "mit":
        return 100.0
    if "open" in t or "cc-by" in t or "community" in t:
        return 82.0
    if "closed" in t:
        return 52.0 if free else 30.0
    return 58.0 if free else 36.0


def _score_cost(pricing):
    t = str(pricing or "").lower()
    if "open" in t or "free" in t or "免费" in t:
        return 96.0
    nums = [float(x) for x in re.findall(r"\$?\s*(\d+(?:\.\d+)?)", t)]
    nums = [n for n in nums if n > 0]
    if not nums:
        return 45.0
    take = nums[:2]
    v = sum(take) / len(take)
    for lim, sc in ((0.3, 95), (0.6, 90), (1.2, 82), (2.5, 72),
                    (5, 62), (10, 50), (20, 36), (40, 24)):
        if v <= lim:
            return float(sc)
    return 12.0


def _score_scale(params):
    b = _params_b(params)
    if b <= 0:
        return 42.0          # 未披露 -> 中性分
    return max(5.0, min(100.0, round(math.log10(max(b, 1.0)) / 3.0 * 100, 1)))


def _score_fresh(release):
    m = re.search(r"(20\d{2})", str(release or ""))
    if not m:
        return 40.0
    y = int(m.group(1))
    return max(5.0, min(100.0, (y - 2022) / 3.0 * 100))


def score_product(p, free=None):
    if free is None:
        free = bool(p.get("free"))
    return {
        "ctx":   _score_ctx(p.get("context")),
        "mm":    _score_mm(p.get("modalities")),
        "open":  _score_open(p.get("license"), free),
        "cost":  _score_cost(p.get("pricing")),
        "scale": _score_scale(p.get("params")),
        "fresh": _score_fresh(p.get("release")),
    }


def composite(sc):
    return round(sum(sc[k] * DIM_W[k] for k in DIM_W), 1)


# ── 通用小组件 ──────────────────────────────────────────────────────
def _bar_row(label, val, val2=None, cls=""):
    r = round(val, 1)
    second = ""
    if val2 is not None:
        second = f'<div class="cmp-sub"><span style="width:{max(2, val2):.1f}%"></span></div>'
    return (f'<div class="cmp-row {cls}">'
            f'<div class="cmp-label">{esc(label)}</div>'
            f'<div class="cmp-bars">'
            f'<div class="cmp-bar"><span style="width:{max(2, r):.1f}%"></span></div>{second}'
            f'</div>'
            f'<div class="cmp-val">{r:.0f}</div>'
            f'</div>')


def _radar_points(sc, cx, cy, r):
    pts = []
    n = len(DIMS)
    for i, (k, _) in enumerate(DIMS):
        ang = -math.pi / 2 + 2 * math.pi * i / n
        v = max(0.06, sc.get(k, 0) / 100.0)
        pts.append((cx + r * v * math.cos(ang), cy + r * v * math.sin(ang)))
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)


def _radar_svg(models):
    """models: [(label, scores, color), ...] 最多 3 条"""
    cx = cy = 150
    R = 105
    parts = ['<svg viewBox="0 0 300 300" class="radar">']
    for lv in (0.25, 0.5, 0.75, 1.0):
        ring = []
        for i in range(len(DIMS)):
            ang = -math.pi / 2 + 2 * math.pi * i / len(DIMS)
            ring.append(f"{cx + R * lv * math.cos(ang):.1f},{cy + R * lv * math.sin(ang):.1f}")
        parts.append(f'<polygon points="{" ".join(ring)}" class="radar-ring"/>')
    for i, (k, label) in enumerate(DIMS):
        ang = -math.pi / 2 + 2 * math.pi * i / len(DIMS)
        x, y = cx + R * math.cos(ang), cy + R * math.sin(ang)
        parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" class="radar-axis"/>')
        lx, ly = cx + (R + 22) * math.cos(ang), cy + (R + 22) * math.sin(ang)
        anchor = "middle"
        if lx > cx + 8:
            anchor = "start"
        elif lx < cx - 8:
            anchor = "end"
        parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
                     f'dominant-baseline="middle" class="radar-txt">{esc(label)}</text>')
    for label, sc, color in models:
        parts.append(f'<polygon points="{_radar_points(sc, cx, cy, R)}" '
                     f'class="radar-area" style="stroke:{color};fill:{color}"/>')
    parts.append("</svg>")
    return "".join(parts)


# ── 主渲染 ──────────────────────────────────────────────────────────
def render_page(cache, model_registry, agent_platforms, max_entries=300, max_days=30):
    entries_int = cache.get("entries_int", [])
    entries_cn = cache.get("entries_cn", [])
    meta = cache.get("meta", {}) or {}
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ── 1. 资讯合并 (国际/国内 -> 单一资讯流) ───────────────────────
    news = []
    for e in entries_int:
        news.append((e, "int"))
    for e in entries_cn:
        news.append((e, "cn"))

    # 基线排序: 热度 -> 时间
    news.sort(key=lambda t: (-(t[0].get("heat", 0) or 0), t[0].get("timestamp", "")))

    source_counts = {}
    for e, _r in news:
        for s in e.get("sources", []) or []:
            source_counts[s] = source_counts.get(s, 0) + 1
    top_sources = sorted(source_counts.items(), key=lambda x: -x[1])

    def _url_of(e):
        url = e.get("url", "") or ""
        if not url:
            m = re.search(r'href=["\'](https?://[^"\']+)', e.get("summary", "") or "")
            if m:
                url = m.group(1)
        return url

    def news_card(e, region, idx):
        heat = int(e.get("heat", 0) or 0)
        stars = "★" * min(heat, 5) + "☆" * max(0, 5 - heat)
        title = (e.get("title", "") or "").strip()
        excerpt = (e.get("excerpt", "") or e.get("summary", "") or "")[:420]
        url = _url_of(e)
        srcs = e.get("sources", []) or []
        ts = e.get("timestamp", "") or e.get("date", "")
        reg_label = "国际" if region == "int" else "国内"
        src_tags = "".join(f'<span class="tag src">{esc(s)}</span>' for s in srcs[:4])
        title_html = (f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(title)}</a>'
                      if url else esc(title))
        blob = f"{title} {' '.join(srcs)} {excerpt} {e.get('body','') or ''}"[:1200]
        return f'''<article class="ncard" data-region="{region}" data-heat="{heat}" data-ts="{esc(ts)}" data-src="{esc(' '.join(srcs))}" data-search="{esc(blob.lower())}">
  <div class="ncard-top">
    <span class="chip reg-{region}">{reg_label}</span>
    <span class="heat" title="热度 {heat}/5">{stars}</span>
    <span class="ncard-date">{esc(str(ts)[:16].replace('T', ' '))}</span>
  </div>
  <h3 class="ncard-title">{title_html}</h3>
  <p class="ncard-sum">{esc(excerpt)}</p>
  <details class="ncard-body"><summary>展开正文</summary><div>{esc((e.get('body') or '')[:2000])}</div></details>
  <div class="ncard-foot">{src_tags}{f'<a class="jump" href="{esc(url)}" target="_blank" rel="noopener noreferrer">原文 ↗</a>' if url else ''}</div>
</article>'''

    cards_html = "\n".join(news_card(e, r, i) for i, (e, r) in enumerate(news))

    # ── 2. 数据源状态 (合并, 侧栏) ───────────────────────────────
    status_map = meta.get("sources_status", {}) or {}
    order = {"活跃": 0, "待同步": 1, "重试中": 2, "失联": 3}
    src_rows = sorted(status_map.items(),
                      key=lambda r: (order.get(r[1].get("status", ""), 9), r[0]))
    ok_n = sum(1 for _, s in src_rows if s.get("dot") == "good")
    src_list = "".join(
        f'<li class="src-item {s.get("dot","muted")}">'
        f'<span class="dot {s.get("dot","muted")}"></span>'
        f'<span class="src-name" title="{esc(n)}">{esc(n)}</span>'
        f'<span class="src-reg">{"国际" if s.get("region")=="int" else "国内"}</span>'
        f'<span class="src-n">{int(s.get("total_items", 0) or 0)}</span>'
        f'</li>' for n, s in src_rows)

    top_src_html = "".join(
        f'<li><span>{esc(n)}</span><b>{c}</b></li>' for n, c in top_sources[:12])
    options = "".join(f'<option value="{esc(n)}">{esc(n)}</option>' for n, _ in top_sources[:60])

    # ── 3. 模型能力打分 ─────────────────────────────────────────
    model_rows = []
    for comp_name, comp in model_registry.items():
        for p in comp.get("products", []):
            sc = score_product(p)
            model_rows.append({
                "company": comp_name, "region": comp.get("region"),
                "hq": comp.get("hq", ""), "product": p,
                "sc": sc, "comp": composite(sc),
            })
    model_rows.sort(key=lambda m: -m["comp"])

    def avg_by_region(region):
        rs = [m for m in model_rows if m["region"] == region]
        if not rs:
            return {k: 0.0 for k, _ in DIMS}
        return {k: sum(m["sc"][k] for m in rs) / len(rs) for k, _ in DIMS}

    avg_int = avg_by_region("int")
    avg_cn = avg_by_region("cn")

    # ── 4. Agent 统计 ───────────────────────────────────────────
    agents = [(n, a) for n, a in agent_platforms.items()]
    agent_cat = {}
    for _n, a in agents:
        c = a.get("category", "其他") or "其他"
        agent_cat[c] = agent_cat.get(c, 0) + 1
    agent_cat_sorted = sorted(agent_cat.items(), key=lambda x: -x[1])
    agent_free = sum(1 for _n, a in agents if a.get("free") in ("有", "有限"))
    agent_int = sum(1 for _n, a in agents if a.get("region") == "int")
    agent_cn = len(agents) - agent_int

    # ── 5. 大盘 KPI ─────────────────────────────────────────────
    total_models = len(model_rows)
    open_models = sum(1 for m in model_rows if m["sc"]["open"] >= 80)
    mm_models = sum(1 for m in model_rows if m["sc"]["mm"] >= 50)
    ctxs = [_ctx_tokens(m["product"].get("context")) for m in model_rows]
    ctxs = [c for c in ctxs if c > 0]
    median_ctx = sorted(ctxs)[len(ctxs) // 2] if ctxs else 0
    if median_ctx >= 1_000_000:
        ctx_txt = f"{median_ctx/1_000_000:.1f}M"
    elif median_ctx >= 1000:
        ctx_txt = f"{median_ctx/1000:.0f}K"
    else:
        ctx_txt = str(int(median_ctx))

    # 模态分布
    mod_keys = ("text", "vision", "audio", "video", "image", "code")
    mod_count = {k: 0 for k in mod_keys}
    for m in model_rows:
        t = str(m["product"].get("modalities", "")).lower()
        for k in mod_keys:
            if k in t:
                mod_count[k] += 1
    mod_max = max(mod_count.values()) or 1

    # 区域 donut
    int_n = sum(1 for m in model_rows if m["region"] == "int")
    cn_n = total_models - int_n
    int_pct = round(int_n / total_models * 100) if total_models else 0
    free_models = sum(1 for m in model_rows if m["product"].get("free"))
    free_pct = round(free_models / total_models * 100) if total_models else 0

    kpi = [
        ("收录模型", f"{total_models}", "个产品"),
        ("覆盖厂商", f"{len(model_registry)}", "家"),
        ("开放权重", f"{round(open_models/total_models*100) if total_models else 0}%", f"{open_models} 个"),
        ("多模态占比", f"{round(mm_models/total_models*100) if total_models else 0}%", f"{mm_models} 个"),
        ("中位上下文", f"{ctx_txt}", "tokens"),
        ("Agent 平台", f"{len(agents)}", f"免费 {agent_free}"),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpi-v">{esc(v)}</div>'
        f'<div class="kpi-l">{esc(l)}</div><div class="kpi-s">{esc(s)}</div></div>'
        for l, v, s in kpi)

    # 维度均值对比 (国际 vs 国内)
    dim_cmp = "".join(
        _bar_row(label, avg_int[k], avg_cn[k]) for k, label in DIMS)

    # 能力榜 Top 12
    rank_html = ""
    for i, m in enumerate(model_rows[:12], 1):
        p = m["product"]
        reg = "国际" if m["region"] == "int" else "国内"
        rank_html += (
            f'<li class="rank"><span class="rk">{i}</span>'
            f'<div class="rk-main"><div class="rk-name">{esc(p.get("series") or p.get("name"))}'
            f'<em>{esc(m["company"])} · {reg}</em></div>'
            f'<div class="rk-bar"><span style="width:{m["comp"]:.1f}%"></span></div></div>'
            f'<span class="rk-score">{m["comp"]:.0f}</span></li>')

    # 雷达: 国际最佳 / 国内最佳
    best_int = next((m for m in model_rows if m["region"] == "int"), None)
    best_cn = next((m for m in model_rows if m["region"] == "cn"), None)
    radar_models = []
    if best_int:
        radar_models.append((best_int["product"].get("name", ""), best_int["sc"], "#35e0a1"))
    if best_cn:
        radar_models.append((best_cn["product"].get("name", ""), best_cn["sc"], "#ff7a59"))
    radar_html = _radar_svg(radar_models)
    radar_legend = "".join(
        f'<span><i style="background:{c}"></i>{esc(l)}</span>' for l, _s, c in radar_models)

    # 模态 / Agent 分布条
    mod_html = "".join(
        f'<div class="dist-row"><span class="dist-k">{k}</span>'
        f'<div class="dist-bar"><span style="width:{mod_count[k]/mod_max*100:.1f}%"></span></div>'
        f'<span class="dist-v">{mod_count[k]}</span></div>' for k in mod_keys)
    max_cat = max((c for _k, c in agent_cat_sorted), default=1)
    agent_cat_html = "".join(
        f'<div class="dist-row"><span class="dist-k">{esc(k)}</span>'
        f'<div class="dist-bar alt"><span style="width:{c/max_cat*100:.1f}%"></span></div>'
        f'<span class="dist-v">{c}</span></div>' for k, c in agent_cat_sorted)

    # ── 6. 模型库表 (合并, 带区域列) ────────────────────────────
    def model_table():
        rows = []
        for m in model_rows:
            p = m["product"]
            reg = "国际" if m["region"] == "int" else "国内"
            fb = ('<span class="badge free">FREE</span>' if p.get("free")
                  else '<span class="badge paid">PAID</span>')
            mini = "".join(
                f'<span class="mini" title="{label} {m["sc"][k]:.0f}">'
                f'<i style="height:{max(8, m["sc"][k]*.22):.1f}px"></i></span>' for k, label in DIMS)
            rows.append(
                f'<tr data-region="{m["region"]}">'
                f'<td class="c-org">{esc(m["company"])}<em>{esc(m["hq"])}</em></td>'
                f'<td><span class="chip reg-{m["region"]}">{reg}</span></td>'
                f'<td class="c-name">{esc(p.get("series"))}<em>{esc(p.get("name"))}</em></td>'
                f'<td>{esc(p.get("params"))}</td><td>{esc(p.get("context"))}</td>'
                f'<td class="c-mod">{esc(p.get("modalities"))}</td>'
                f'<td class="c-price">{esc(p.get("pricing"))}</td>'
                f'<td class="c-lic">{esc(p.get("license"))}</td>'
                f'<td>{fb}</td>'
                f'<td><div class="mini-wrap">{mini}<b>{m["comp"]:.0f}</b></div></td>'
                f'<td class="c-evo">{esc(p.get("evolution"))}</td></tr>')
        return "\n".join(rows)

    # ── 7. Agent 表 (合并) ──────────────────────────────────────
    def agent_table():
        rows = []
        for n, a in sorted(agents, key=lambda x: (x[1].get("region"), x[0])):
            reg = "国际" if a.get("region") == "int" else "国内"
            fb = ('<span class="badge free">免费</span>' if a.get("free") in ("有", "有限")
                  else '<span class="badge paid">付费</span>')
            rows.append(
                f'<tr data-region="{a.get("region")}">'
                f'<td class="c-org">{esc(n)}<em>{esc(a.get("product"))}</em></td>'
                f'<td><span class="chip reg-{a.get("region")}">{reg}</span></td>'
                f'<td>{esc(a.get("category"))}</td>'
                f'<td class="c-mod">{esc(a.get("models"))}</td>'
                f'<td class="c-evo">{esc(a.get("features"))}</td>'
                f'<td>{fb}</td><td class="c-price">{esc(a.get("pricing"))}</td>'
                f'<td class="c-evo">{esc(a.get("enterprise"))}</td>'
                f'<td class="c-evo dim">{esc(a.get("limitations"))}</td>'
                f'<td><a class="jump" href="{esc(a.get("docs"))}" target="_blank" rel="noopener noreferrer">文档 ↗</a></td></tr>')
        return "\n".join(rows)

    # ── 8. 页面骨架 ─────────────────────────────────────────────
    nav = [("news", "资讯聚合"), ("dash", "能力大盘"),
           ("models", "模型库"), ("agents", "Agent 平台")]

    topbar = f'''<header class="topbar">
  <div class="tb-inner">
    <div class="brand"><span class="logo">◆</span><span class="brand-t">AI 情报聚合</span></div>
    <nav class="navtabs">
      {''.join(f'<button class="navtab{" active" if i==0 else ""}" data-view="{k}">{esc(t)}</button>' for i, (k, t) in enumerate(nav))}
    </nav>
    <div class="tb-search" id="global-search-wrap">
      <input id="global-search" type="search" placeholder="模糊检索资讯 / 关键词…" autocomplete="off">
    </div>
  </div>
</header>'''

    news_view = f'''<section class="view active" id="view-news">
  <div class="hero">
    <h1>AI 行业情报 · 实时聚合</h1>
    <p class="hero-sub">权威源每小时自动检索 · 国际/国内统一资讯流 · 按热度与时间排序</p>
    <div class="hero-kpis">
      <div class="hero-kpi"><b>{len(news)}</b><span>资讯条数</span></div>
      <div class="hero-kpi"><b>{len(entries_int)}</b><span>国际</span></div>
      <div class="hero-kpi"><b>{len(entries_cn)}</b><span>国内</span></div>
      <div class="hero-kpi"><b>{len(src_rows)}</b><span>数据源</span></div>
      <div class="hero-kpi"><b>{ok_n}</b><span>源活跃</span></div>
    </div>
  </div>

  <div class="toolbar">
    <div class="seg" id="region-seg">
      <button class="seg-btn active" data-region-filter="all">全部</button>
      <button class="seg-btn" data-region-filter="int">国际</button>
      <button class="seg-btn" data-region-filter="cn">国内</button>
    </div>
    <div class="tb-search inline">
      <input id="news-search" type="search" placeholder="在结果中模糊检索…" autocomplete="off">
    </div>
    <select id="news-source" class="sel"><option value="all">全部来源</option>{options}</select>
    <select id="news-sort" class="sel">
      <option value="heat">按热度</option>
      <option value="time">按时间</option>
    </select>
    <span class="count"><b id="news-count">{len(news)}</b> 条</span>
  </div>

  <div class="layout">
    <div class="col-main">
      <div class="feed" id="news-feed">{cards_html}</div>
      <button class="more" id="news-more">加载更多</button>
    </div>
    <aside class="col-side">
      <div class="panel">
        <div class="panel-h"><span>数据源状态</span><span class="muted">{ok_n}/{len(src_rows)} 活跃</span></div>
        <ul class="src-list">{src_list}</ul>
      </div>
      <div class="panel">
        <div class="panel-h"><span>来源榜</span><span class="muted">按条数</span></div>
        <ul class="top-list">{top_src_html}</ul>
      </div>
    </aside>
  </div>
</section>'''

    dash_view = f'''<section class="view" id="view-dash">
  <div class="sec-head"><h2>模型 & Agent 能力大盘</h2>
    <p>按上下文 / 多模态 / 开放度 / 性价比 / 规模 / 前沿度 六维评估，跨国际与国内统一统计</p></div>
  <div class="kpis">{kpi_html}</div>

  <div class="dash-grid">
    <div class="panel">
      <div class="panel-h"><span>六维能力均值 · 国际 vs 国内</span>
        <span class="lg"><i class="i-int"></i>国际<i class="i-cn"></i>国内</span></div>
      {dim_cmp}
    </div>
    <div class="panel">
      <div class="panel-h"><span>区域 / 开放占比</span></div>
      <div class="donuts">
        <div class="donut" style="--p:{int_pct}%;--c1:#35e0a1;--c2:#2a2f3a">
          <div class="donut-in"><b>{int_pct}%</b><span>国际</span></div>
        </div>
        <div class="donut" style="--p:{free_pct}%;--c1:#7c5cff;--c2:#2a2f3a">
          <div class="donut-in"><b>{free_pct}%</b><span>免费</span></div>
        </div>
        <div class="donut-legend">
          <div>国际 <b>{int_n}</b> · 国内 <b>{cn_n}</b></div>
          <div>免费 <b>{free_models}</b> · 收费 <b>{total_models-free_models}</b></div>
          <div>开放权重 <b>{open_models}</b> · 多模态 <b>{mm_models}</b></div>
        </div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-h"><span>能力雷达 · 区域最佳模型</span></div>
      <div class="radar-wrap">{radar_html}<div class="radar-lg">{radar_legend}</div></div>
    </div>
    <div class="panel">
      <div class="panel-h"><span>综合能力榜 TOP 12</span></div>
      <ul class="rank-list">{rank_html}</ul>
    </div>
    <div class="panel">
      <div class="panel-h"><span>模态覆盖分布</span></div>
      {mod_html}
    </div>
    <div class="panel">
      <div class="panel-h"><span>Agent 平台类别分布</span>
        <span class="muted">{agent_int} 国际 · {agent_cn} 国内</span></div>
      {agent_cat_html}
    </div>
  </div>
</section>'''

    models_view = f'''<section class="view" id="view-models">
  <div class="sec-head"><h2>大模型能力库</h2>
    <p>{total_models} 个模型产品 · {len(model_registry)} 家厂商 · 迷你柱状为六维能力得分</p></div>
  <div class="toolbar">
    <div class="seg" data-table-filter="model-table">
      <button class="seg-btn active" data-region-filter="all">全部</button>
      <button class="seg-btn" data-region-filter="int">国际</button>
      <button class="seg-btn" data-region-filter="cn">国内</button>
    </div>
    <div class="tb-search inline">
      <input type="search" data-table-search="model-table" placeholder="模糊检索模型 / 厂商…" autocomplete="off">
    </div>
    <span class="count"><b class="tcount">{total_models}</b> 个</span>
  </div>
  <div class="table-wrap">
    <table class="dtable" id="model-table">
      <thead><tr>
        <th>厂商</th><th>区域</th><th>模型</th><th>参数</th><th>上下文</th><th>模态</th>
        <th>定价</th><th>许可证</th><th>费用</th><th>六维 / 综合</th><th>核心演进</th>
      </tr></thead>
      <tbody>{model_table()}</tbody>
    </table>
  </div>
</section>'''

    agents_view = f'''<section class="view" id="view-agents">
  <div class="sec-head"><h2>Agent 平台库</h2>
    <p>{len(agents)} 个平台 · 国际 {agent_int} / 国内 {agent_cn} · 覆盖 {len(agent_cat)} 个类别</p></div>
  <div class="toolbar">
    <div class="seg" data-table-filter="agent-table">
      <button class="seg-btn active" data-region-filter="all">全部</button>
      <button class="seg-btn" data-region-filter="int">国际</button>
      <button class="seg-btn" data-region-filter="cn">国内</button>
    </div>
    <div class="tb-search inline">
      <input type="search" data-table-search="agent-table" placeholder="模糊检索平台 / 功能…" autocomplete="off">
    </div>
    <span class="count"><b class="tcount">{len(agents)}</b> 个</span>
  </div>
  <div class="table-wrap">
    <table class="dtable" id="agent-table">
      <thead><tr>
        <th>平台</th><th>区域</th><th>类别</th><th>支持模型</th><th>核心功能</th>
        <th>费用</th><th>定价</th><th>企业方案</th><th>局限</th><th>文档</th>
      </tr></thead>
      <tbody>{agent_table()}</tbody>
    </table>
  </div>
</section>'''

    footer = (f'<footer>AI 情报聚合 v7 · 数据每小时自动更新 · 资讯保留最近 {max_days} 天 / 每区最多 {max_entries} 条 · '
              f'{total_models} 个模型 · {len(agents)} 个 Agent 平台 · 渲染于 {now_str}</footer>')

    html = ("<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"UTF-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
            "<title>AI 情报聚合 · 模型能力大盘</title>\n<style>\n" + CSS + "\n</style>\n</head>\n<body>\n"
            + topbar + "<main class=\"wrap\">" + news_view + dash_view + models_view + agents_view
            + "</main>" + footer + "\n<script>\n" + JS + "\n</script>\n</body>\n</html>")
    return html


CSS = r"""
:root{
  --bg:#0a0b0f;--bg2:#0e1016;--surface:#14161f;--surface2:#191c26;--border:#232734;--border2:#2d3242;
  --text:#e9ecf3;--dim:#9aa3b4;--muted:#6b7280;
  --accent:#35e0a1;--accent2:#7c5cff;--warn:#ffb020;--danger:#ff6b6b;--cn:#ff7a59;
  --r:14px;--shadow:0 10px 34px rgba(0,0,0,.42);
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:radial-gradient(1200px 600px at 15% -10%,rgba(53,224,161,.08),transparent 60%),
     radial-gradient(900px 500px at 90% 0%,rgba(124,92,255,.10),transparent 55%),var(--bg);
  color:var(--text);font-family:system-ui,-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif;
  line-height:1.6;-webkit-font-smoothing:antialiased}
a{color:inherit}
::-webkit-scrollbar{width:10px;height:10px}
::-webkit-scrollbar-thumb{background:#2a2f3a;border-radius:6px}

/* topbar */
.topbar{position:sticky;top:0;z-index:60;backdrop-filter:blur(16px);
  background:rgba(10,11,15,.86);border-bottom:1px solid var(--border)}
.tb-inner{max-width:1440px;margin:0 auto;display:flex;align-items:center;gap:22px;padding:11px 22px}
.brand{display:flex;align-items:center;gap:10px;font-weight:800;letter-spacing:.3px;white-space:nowrap}
.brand .logo{width:30px;height:30px;border-radius:9px;display:grid;place-items:center;color:#07130e;
  background:linear-gradient(135deg,var(--accent),var(--accent2));font-size:15px}
.brand-t{font-size:1.02rem;background:linear-gradient(120deg,var(--text),var(--accent));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.navtabs{display:flex;gap:4px}
.navtab{background:transparent;border:0;color:var(--dim);font:inherit;font-size:.9rem;font-weight:600;
  padding:8px 16px;border-radius:10px;cursor:pointer;transition:.18s}
.navtab:hover{color:var(--text);background:rgba(255,255,255,.05)}
.navtab.active{color:var(--accent);background:rgba(53,224,161,.12);box-shadow:inset 0 0 0 1px rgba(53,224,161,.28)}
.tb-search{position:relative;margin-left:auto}
.tb-search input{width:340px;max-width:42vw;background:var(--surface);border:1px solid var(--border2);
  color:var(--text);font:inherit;font-size:.86rem;padding:9px 14px 9px 34px;border-radius:11px;outline:none;transition:.18s}
.tb-search input:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(53,224,161,.14);width:380px}
.tb-search::before{content:'⌕';position:absolute;left:12px;top:50%;transform:translateY(-50%);color:var(--muted);font-size:1rem}
.tb-search.inline{flex:1;min-width:160px;margin-left:0}

.wrap{max-width:1440px;margin:0 auto;padding:22px}
.view{display:none;animation:fade .3s ease}
.view.active{display:block}
@keyframes fade{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}

/* hero */
.hero{padding:26px 4px 20px;border-bottom:1px solid var(--border);margin-bottom:18px}
.hero h1{font-size:1.9rem;font-weight:800;letter-spacing:-.5px;
  background:linear-gradient(120deg,#fff,#8ff3cf 60%,#b9a6ff);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.hero-sub{color:var(--dim);font-size:.88rem;margin-top:6px}
.hero-kpis{display:flex;gap:26px;margin-top:16px;flex-wrap:wrap}
.hero-kpi b{display:block;font-size:1.4rem;font-weight:800;color:var(--accent)}
.hero-kpi span{font-size:.72rem;color:var(--muted);letter-spacing:.6px}

/* toolbar */
.toolbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:16px;
  padding:12px 14px;background:var(--surface);border:1px solid var(--border);border-radius:var(--r)}
.seg{display:inline-flex;background:var(--bg2);border:1px solid var(--border2);border-radius:10px;padding:3px;gap:2px}
.seg-btn{background:transparent;border:0;color:var(--dim);font:inherit;font-size:.82rem;font-weight:600;
  padding:6px 14px;border-radius:8px;cursor:pointer;transition:.16s}
.seg-btn:hover{color:var(--text)}
.seg-btn.active{background:linear-gradient(135deg,rgba(53,224,161,.22),rgba(124,92,255,.22));color:var(--accent)}
.sel{background:var(--surface2);border:1px solid var(--border2);color:var(--dim);font:inherit;font-size:.82rem;
  padding:8px 10px;border-radius:10px;outline:none;cursor:pointer;max-width:190px}
.count{margin-left:auto;color:var(--muted);font-size:.8rem}
.count b{color:var(--accent)}

/* layout */
.layout{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:18px;align-items:start}
.feed{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.col-side{display:flex;flex-direction:column;gap:16px;position:sticky;top:76px}

/* news card */
.ncard{background:linear-gradient(180deg,var(--surface),var(--surface2));border:1px solid var(--border);
  border-radius:var(--r);padding:16px;display:flex;flex-direction:column;gap:9px;transition:.2s;position:relative;overflow:hidden}
.ncard::before{content:'';position:absolute;inset:0 auto 0 0;width:3px;background:var(--accent);opacity:.25}
.ncard[data-region="cn"]::before{background:var(--cn)}
.ncard[data-heat="5"]::before{opacity:1}
.ncard:hover{transform:translateY(-3px);border-color:var(--border2);box-shadow:var(--shadow)}
.ncard-top{display:flex;align-items:center;gap:8px}
.chip{font-size:.66rem;font-weight:700;padding:2px 9px;border-radius:20px;letter-spacing:.4px}
.chip.reg-int{background:rgba(53,224,161,.13);color:var(--accent);border:1px solid rgba(53,224,161,.3)}
.chip.reg-cn{background:rgba(255,122,89,.13);color:var(--cn);border:1px solid rgba(255,122,89,.3)}
.heat{color:#ffc93c;font-size:.72rem;letter-spacing:1px}
.ncard-date{margin-left:auto;color:var(--muted);font-size:.7rem}
.ncard-title{font-size:.98rem;font-weight:650;line-height:1.45}
.ncard-title a{text-decoration:none;transition:.18s}
.ncard-title a:hover{color:var(--accent)}
.ncard-sum{color:var(--dim);font-size:.83rem;line-height:1.65;
  display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.ncard-body summary{cursor:pointer;color:var(--accent2);font-size:.76rem;user-select:none}
.ncard-body div{font-size:.8rem;color:#b9c0cd;white-space:pre-wrap;margin-top:8px;max-height:280px;overflow:auto}
.ncard-foot{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin-top:auto}
.tag.src{font-size:.64rem;color:var(--dim);background:rgba(255,255,255,.05);
  border:1px solid var(--border);padding:2px 8px;border-radius:8px}
.jump{margin-left:auto;font-size:.7rem;color:var(--accent);text-decoration:none;
  border:1px solid rgba(53,224,161,.32);padding:2px 10px;border-radius:20px;transition:.18s;white-space:nowrap}
.jump:hover{background:rgba(53,224,161,.14)}
.more{margin:18px auto 0;display:block;background:var(--surface);border:1px solid var(--border2);
  color:var(--dim);font:inherit;font-size:.84rem;padding:10px 26px;border-radius:24px;cursor:pointer;transition:.18s}
.more:hover{border-color:var(--accent);color:var(--accent)}

/* panels */
.panel{background:var(--surface);border:1px solid var(--border);border-radius:var(--r);padding:14px}
.panel-h{display:flex;align-items:center;justify-content:space-between;gap:8px;
  font-size:.86rem;font-weight:650;margin-bottom:12px}
.muted{color:var(--muted);font-size:.74rem;font-weight:400}
.src-list,.top-list,.rank-list{list-style:none;display:flex;flex-direction:column;gap:6px;max-height:420px;overflow:auto}
.src-item{display:flex;align-items:center;gap:8px;font-size:.78rem;padding:5px 7px;border-radius:8px}
.src-item:hover{background:rgba(255,255,255,.04)}
.dot{width:8px;height:8px;border-radius:50%;flex:0 0 8px}
.dot.good{background:var(--accent);box-shadow:0 0 8px var(--accent);animation:pulse 2.4s infinite}
.dot.bad{background:var(--danger)}.dot.muted{background:#4b5563}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.45}}
.src-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.src-reg{font-size:.62rem;color:var(--muted);border:1px solid var(--border);padding:1px 6px;border-radius:6px}
.src-n{color:var(--dim);font-size:.72rem;min-width:22px;text-align:right}
.top-list li{display:flex;justify-content:space-between;gap:8px;font-size:.78rem;padding:5px 7px;border-radius:8px}
.top-list li:hover{background:rgba(255,255,255,.04)}
.top-list span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--dim)}
.top-list b{color:var(--accent)}

/* dashboard */
.sec-head{margin:6px 0 18px}
.sec-head h2{font-size:1.35rem;font-weight:750}
.sec-head p{color:var(--dim);font-size:.85rem;margin-top:4px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:18px}
.kpi{background:linear-gradient(180deg,var(--surface),var(--surface2));border:1px solid var(--border);
  border-radius:var(--r);padding:15px 16px;position:relative;overflow:hidden}
.kpi::after{content:'';position:absolute;top:0;left:0;right:0;height:2px;
  background:linear-gradient(90deg,var(--accent),var(--accent2))}
.kpi-v{font-size:1.6rem;font-weight:800;color:var(--accent);line-height:1.2}
.kpi-l{font-size:.78rem;color:var(--text);margin-top:3px}
.kpi-s{font-size:.68rem;color:var(--muted)}
.dash-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:14px}
.lg{display:flex;gap:12px;font-size:.72rem;color:var(--muted);align-items:center}
.lg i,.radar-lg i{width:10px;height:10px;border-radius:3px;display:inline-block;margin-right:4px}
.i-int{background:var(--accent)}.i-cn{background:var(--cn)}

.cmp-row{display:grid;grid-template-columns:76px 1fr 42px;gap:10px;align-items:center;padding:5px 0}
.cmp-label{font-size:.78rem;color:var(--dim)}
.cmp-bars{display:flex;flex-direction:column;gap:3px}
.cmp-bar,.cmp-sub{height:9px;background:rgba(255,255,255,.06);border-radius:5px;overflow:hidden}
.cmp-bar span{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#1fb586);border-radius:5px}
.cmp-sub{height:7px}
.cmp-sub span{display:block;height:100%;background:linear-gradient(90deg,var(--cn),#e0562f);border-radius:5px}
.cmp-val{font-size:.78rem;color:var(--text);text-align:right;font-variant-numeric:tabular-nums}

.donuts{display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.donut{width:104px;height:104px;border-radius:50%;flex:0 0 104px;
  background:conic-gradient(var(--c1) calc(var(--p)),var(--c2) 0);display:grid;place-items:center}
.donut-in{width:74px;height:74px;border-radius:50%;background:var(--surface);display:grid;place-items:center;line-height:1.1}
.donut-in b{font-size:1rem;color:var(--text);display:block}
.donut-in span{font-size:.64rem;color:var(--muted)}
.donut-legend{font-size:.78rem;color:var(--dim);display:flex;flex-direction:column;gap:5px}
.donut-legend b{color:var(--accent)}

.radar-wrap{display:flex;flex-direction:column;align-items:center;gap:8px}
.radar{width:100%;max-width:300px;height:auto}
.radar-ring{fill:none;stroke:rgba(255,255,255,.09)}
.radar-axis{stroke:rgba(255,255,255,.09)}
.radar-txt{fill:var(--muted);font-size:10px}
.radar-area{fill-opacity:.18;stroke-width:2}
.radar-lg{display:flex;gap:14px;font-size:.74rem;color:var(--dim);flex-wrap:wrap;justify-content:center}

.rank{display:flex;align-items:center;gap:10px;padding:6px 7px;border-radius:9px;font-size:.82rem}
.rank:hover{background:rgba(255,255,255,.04)}
.rk{width:20px;color:var(--muted);font-size:.72rem;text-align:right}
.rank:nth-child(1) .rk,.rank:nth-child(2) .rk,.rank:nth-child(3) .rk{color:var(--warn);font-weight:700}
.rk-main{flex:1;min-width:0}
.rk-name{font-size:.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rk-name em{color:var(--muted);font-style:normal;font-size:.7rem;margin-left:6px}
.rk-bar{height:5px;background:rgba(255,255,255,.07);border-radius:4px;margin-top:4px;overflow:hidden}
.rk-bar span{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2))}
.rk-score{color:var(--accent);font-weight:700;font-size:.82rem;font-variant-numeric:tabular-nums}

.dist-row{display:grid;grid-template-columns:74px 1fr 34px;gap:10px;align-items:center;padding:4px 0;font-size:.78rem}
.dist-k{color:var(--dim)}
.dist-bar{height:8px;background:rgba(255,255,255,.06);border-radius:5px;overflow:hidden}
.dist-bar span{display:block;height:100%;background:linear-gradient(90deg,var(--accent),#1fb586)}
.dist-bar.alt span{background:linear-gradient(90deg,var(--accent2),#5b3fe0)}
.dist-v{text-align:right;color:var(--text);font-variant-numeric:tabular-nums}

/* tables */
.table-wrap{overflow:auto;border:1px solid var(--border);border-radius:var(--r);background:var(--surface);max-height:78vh}
.dtable{width:100%;border-collapse:collapse;font-size:.8rem;min-width:1180px}
.dtable th{position:sticky;top:0;z-index:2;background:#171a23;color:var(--accent);
  font-weight:650;text-align:left;padding:11px 10px;border-bottom:1px solid var(--border2);white-space:nowrap}
.dtable td{padding:10px;border-bottom:1px solid var(--border);vertical-align:top}
.dtable tbody tr:hover{background:rgba(53,224,161,.05)}
.dtable tbody tr:nth-child(even){background:rgba(255,255,255,.015)}
.c-org{font-weight:650;color:#fff;min-width:150px}
.c-org em,.c-name em{display:block;font-style:normal;font-size:.7rem;color:var(--muted);font-weight:400;margin-top:2px}
.c-name{color:var(--accent);min-width:150px}
.c-mod{font-size:.76rem}
.c-price{font-family:ui-monospace,Menlo,monospace;font-size:.74rem;color:#ffd166}
.c-lic{font-size:.74rem;color:var(--dim)}
.c-evo{font-size:.75rem;color:var(--dim);max-width:260px}
.c-evo.dim{color:var(--muted)}
.badge{display:inline-block;font-size:.68rem;font-weight:700;padding:2px 9px;border-radius:20px}
.badge.free{background:rgba(53,224,161,.14);color:var(--accent);border:1px solid rgba(53,224,161,.36)}
.badge.paid{background:rgba(255,107,107,.14);color:var(--danger);border:1px solid rgba(255,107,107,.36)}
.mini-wrap{display:flex;align-items:flex-end;gap:3px}
.mini{width:7px;height:26px;display:flex;align-items:flex-end;background:rgba(255,255,255,.06);border-radius:3px}
.mini i{display:block;width:100%;background:linear-gradient(180deg,var(--accent2),var(--accent));border-radius:3px}
.mini-wrap b{margin-left:7px;color:var(--accent);font-size:.78rem}

footer{max-width:1440px;margin:34px auto 0;padding:22px;color:var(--muted);font-size:.75rem;
  border-top:1px solid var(--border);text-align:center}

@media(max-width:1080px){
  .layout{grid-template-columns:1fr}
  .col-side{position:static;flex-direction:row;flex-wrap:wrap}
  .col-side .panel{flex:1;min-width:280px}
}
@media(max-width:720px){
  .tb-inner{flex-wrap:wrap;gap:10px}
  .tb-search{margin-left:0;width:100%}
  .tb-search input{width:100%;max-width:none}
  .tb-search input:focus{width:100%}
  .navtabs{order:3;width:100%;overflow-x:auto}
  .feed{grid-template-columns:1fr}
  .hero h1{font-size:1.5rem}
}
"""


JS = r"""
(function(){
  function q(s,el){return (el||document).querySelector(s);}
  function qa(s,el){return Array.prototype.slice.call((el||document).querySelectorAll(s));}
  function debounce(fn,ms){var t;return function(){var a=arguments,c=this;clearTimeout(t);t=setTimeout(function(){fn.apply(c,a);},ms);};}

  /* ---- view switching ---- */
  var navBtns=qa('.navtab');
  function showView(id){
    navBtns.forEach(function(b){b.classList.toggle('active',b.dataset.view===id);});
    qa('.view').forEach(function(v){v.classList.toggle('active',v.id==='view-'+id);});
    window.scrollTo({top:0,behavior:'smooth'});
  }
  navBtns.forEach(function(b){b.addEventListener('click',function(){showView(b.dataset.view);});});

  /* ---- fuzzy search ---- */
  function subseq(text,qq){var i=0,j=0;while(i<text.length&&j<qq.length){if(text[i]===qq[j])j++;i++;}return j===qq.length;}
  function fuzzy(text,qq){
    if(!qq)return 1;
    text=String(text||'').toLowerCase();qq=qq.toLowerCase().trim();
    if(!qq)return 1;
    var idx=text.indexOf(qq);
    if(idx!==-1)return 1000-idx;
    var toks=qq.split(/\s+/).filter(Boolean),score=0;
    for(var i=0;i<toks.length;i++){
      var t=toks[i],p=text.indexOf(t);
      if(p!==-1){score+=200;continue;}
      if(subseq(text,t)){score+=60;continue;}
      return -1;
    }
    return score;
  }

  /* ---- news feed ---- */
  var feed=q('#news-feed');
  if(feed){
    var cards=qa('.ncard',feed),PAGE=48,shown=PAGE;
    var state={q:'',region:'all',sort:'heat',src:'all'};
    var sortSel=q('#news-sort'),srcSel=q('#news-source'),countEl=q('#news-count'),moreBtn=q('#news-more');
    var newsSearch=q('#news-search'),globalSearch=q('#global-search');

    function apply(){
      var matched=[],i;
      for(i=0;i<cards.length;i++){
        var c=cards[i],ok=true,sc=1;
        if(state.region!=='all'&&c.dataset.region!==state.region)ok=false;
        if(ok&&state.src!=='all'&&(c.dataset.src||'').indexOf(state.src)===-1)ok=false;
        if(ok&&state.q){sc=fuzzy(c.dataset.search||'',state.q);if(sc<0)ok=false;}
        c.__score=sc;
        if(ok)matched.push(c);
      }
      var byTime=function(a,b){return (b.dataset.ts||'').localeCompare(a.dataset.ts||'');};
      var byHeat=function(a,b){var h=(+b.dataset.heat||0)-(+a.dataset.heat||0);return h!==0?h:byTime(a,b);};
      if(state.q){
        matched.sort(function(a,b){var d=b.__score-a.__score;return d!==0?d:byHeat(a,b);});
      }else if(state.sort==='time'){
        matched.sort(byTime);
      }else{
        matched.sort(byHeat);
      }
      for(i=0;i<cards.length;i++){cards[i].style.display='none';}
      for(i=0;i<matched.length;i++){
        var el=matched[i];
        if(i<shown){feed.appendChild(el);el.style.display='';}
      }
      if(countEl)countEl.textContent=matched.length;
      if(moreBtn)moreBtn.style.display=matched.length>shown?'':'none';
    }
    moreBtn&&moreBtn.addEventListener('click',function(){shown+=PAGE;apply();});
    sortSel&&sortSel.addEventListener('change',function(){state.sort=sortSel.value;apply();});
    srcSel&&srcSel.addEventListener('change',function(){state.src=srcSel.value;apply();});
    var doSearch=debounce(function(v){state.q=v;shown=PAGE;apply();},160);
    newsSearch&&newsSearch.addEventListener('input',function(){doSearch(newsSearch.value);});
    if(globalSearch){
      globalSearch.addEventListener('input',function(){
        if(!q('#view-news').classList.contains('active'))showView('news');
        if(newsSearch)newsSearch.value=globalSearch.value;
        doSearch(globalSearch.value);
      });
    }
    qa('[data-region-filter]',q('#region-seg')).forEach(function(b){
      b.addEventListener('click',function(){
        qa('.seg-btn',b.parentNode).forEach(function(x){x.classList.remove('active');});
        b.classList.add('active');state.region=b.dataset.regionFilter;shown=PAGE;apply();
      });
    });
    apply();
  }

  /* ---- generic table search / region filter ---- */
  qa('[data-table-search]').forEach(function(inp){
    var table=q('#'+inp.dataset.tableSearch);if(!table)return;
    var rows=qa('tbody tr',table),countEl=table.closest('.view').querySelector('.tcount');
    var state={q:'',region:'all'};
    function apply(){
      var n=0;
      rows.forEach(function(r){
        var ok=true;
        if(state.region!=='all'&&r.dataset.region!==state.region)ok=false;
        if(ok&&state.q&&fuzzy(r.textContent,state.q)<0)ok=false;
        r.style.display=ok?'':'none';if(ok)n++;
      });
      if(countEl)countEl.textContent=n;
    }
    var doSearch=debounce(function(v){state.q=v;apply();},140);
    inp.addEventListener('input',function(){doSearch(inp.value);});
    var seg=q('[data-table-filter="'+inp.dataset.tableSearch+'"]');
    if(seg)qa('.seg-btn',seg).forEach(function(b){
      b.addEventListener('click',function(){
        qa('.seg-btn',seg).forEach(function(x){x.classList.remove('active');});
        b.classList.add('active');state.region=b.dataset.regionFilter;apply();
      });
    });
    apply();
  });
})();
"""
