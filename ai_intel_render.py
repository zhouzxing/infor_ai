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
def _pricing_nums(pricing):
    """'$1.25/$10 per 1M tok' -> (1.25, 10.0); 'Open (...)'/'免费' -> None"""
    t = str(pricing or "").lower()
    if "open" in t or "free" in t or "免费" in t:
        return None
    nums = [float(x) for x in re.findall(r"\$?\s*(\d+(?:\.\d+)?)", t) if float(x) > 0]
    return tuple(nums[:2]) if nums else None


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
def render_page(cache, model_registry, agent_platforms, max_entries=300, max_days=30,
                model_benchmarks=None, agent_caps=None):
    model_benchmarks = model_benchmarks or {}
    agent_caps = agent_caps or {}
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
           ("perf", "性能对比"), ("models", "模型库"), ("agents", "Agent 平台"),
           ("agentscmp", "Agent 对比"), ("github", "GitHub 热榜"), ("monitor", "监控中心")]

    topbar = f'''<header class="topbar">
  <div class="tb-inner">
    <div class="brand"><span class="logo">◆</span><span class="brand-t">AI 情报聚合</span></div>
    <nav class="navtabs">
      {''.join(f'<button class="navtab{" active" if i==0 else ""}" data-view="{k}">{esc(t)}</button>' for i, (k, t) in enumerate(nav))}
    </nav>
    <div class="tb-search" id="global-search-wrap">
      <input id="global-search" type="search" placeholder="模糊检索资讯 / 关键词…" autocomplete="off">
    </div>
    <a class="gh-badge" href="https://github.com/zhouzxing" target="_blank" rel="noopener noreferrer" title="本站构建者 · GitHub: zhouzxing">
      <svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path fill="currentColor" d="M6.766 11.328c-2.063-.25-3.516-1.734-3.516-3.656 0-.781.281-1.625.75-2.188-.203-.515-.172-1.609.063-2.062.625-.078 1.468.25 1.968.703.594-.187 1.219-.281 1.985-.281.765 0 1.39.094 1.953.265.484-.437 1.344-.765 1.969-.687.218.422.25 1.515.046 2.047.5.593.766 1.39.766 2.203 0 1.922-1.453 3.375-3.547 3.64.531.344.89 1.094.89 1.954v1.625c0 .468.391.734.86.547C13.781 14.359 16 11.53 16 8.03 16 3.61 12.406 0 7.984 0 3.563 0 0 3.61 0 8.031a7.88 7.88 0 0 0 5.172 7.422c.422.156.828-.125.828-.547v-1.25c-.219.094-.5.156-.75.156-1.031 0-1.64-.562-2.078-1.609-.172-.422-.36-.672-.719-.719-.187-.015-.25-.093-.25-.187 0-.188.313-.328.625-.328.453 0 .844.281 1.25.86.313.452.64.655 1.031.655s.641-.14 1-.5c.266-.265.47-.5.657-.656"/></svg>
      <span>zhouzxing</span>
    </a>
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

    # ══════════════════════════════════════════════════════════════
    # 7b. 性能对比视图 — 基准 × 训练 × 推理 + 智能/价格象限
    # ══════════════════════════════════════════════════════════════
    BENCH_KEYS = ["MMLU-Pro", "GPQA", "AIME25", "SWE-V", "LCB", "TB-Hard", "τ²-bench", "BrowseComp"]
    BENCH_LABELS = {"MMLU-Pro": "MMLU-Pro", "GPQA": "GPQA", "AIME25": "AIME25",
                    "SWE-V": "SWE-V", "LCB": "LCB", "TB-Hard": "TB-Hard",
                    "τ²-bench": "τ²-bench", "BrowseComp": "BrowseComp"}
    bench_rows = []
    for m in model_rows:
        b = model_benchmarks.get(m["product"].get("name", ""), {})
        if not b:
            continue
        bench_rows.append({
            "name": m["product"].get("name", ""),
            "company": m["company"], "region": m["region"],
            "bench": b.get("bench", {}),
            "tps": b.get("tps"), "ttft": b.get("ttft"),
            "train": b.get("train") or {},
            "pricing": m["product"].get("pricing", ""),
            "license": m["product"].get("license", ""),
        })
    bench_rows.sort(key=lambda r: -sum(r["bench"].values()))

    def _cell(v, maxi=100.0):
        if v is None:
            return '<span class="bmv dim">—</span>'
        cls = ("bmv hot" if v >= 80 else
               "bmv good" if v >= 60 else
               "bmv mid" if v >= 40 else
               "bmv low")
        return f'<span class="{cls}">{v:g}</span>'

    def _hm(val, best):
        """热度背景: val/best 越高越绿"""
        if val is None or not best:
            return "hm-n"
        r = val / best
        if r >= .92: return "hm-9"
        if r >= .80: return "hm-8"
        if r >= .68: return "hm-7"
        if r >= .55: return "hm-6"
        if r >= .42: return "hm-5"
        if r >= .30: return "hm-4"
        if r >= .18: return "hm-3"
        if r > 0:    return "hm-2"
        return "hm-n"

    best_tps = max((r["tps"] for r in bench_rows if r["tps"]), default=0)
    best_ttft = min((r["ttft"] for r in bench_rows if r["ttft"]), default=0)

    def _train_cell(tr, key, fmt="{:g}"):
        v = tr.get(key)
        if v is None:
            return '<span class="bmv dim">未披露</span>'
        return f'<span class="bmv">{fmt.format(v)}</span>'

    def bench_matrix():
        rows = []
        for r in bench_rows:
            cells = "".join(
                f'<td class="hcell {_hm(r["bench"].get(k), 100)}">{_cell(r["bench"].get(k))}</td>'
                for k in BENCH_KEYS)
            reg = "国际" if r["region"] == "int" else "国内"
            blob = " ".join([r["name"], r["company"], reg,
                             " ".join(f'{k} {v}' for k, v in r["bench"].items())]).lower()
            avg = round(sum(r["bench"].values()) / len(BENCH_KEYS), 1) if r["bench"] else 0
            rows.append(
                f'<tr data-region="{r["region"]}" data-search="{esc(blob)}">'
                f'<td class="c-name">{esc(r["name"])}<em>{esc(r["company"])} · {reg}</em></td>'
                f'{cells}'
                f'<td class="c-avg"><b>{avg:.1f}</b></td></tr>')
        return "\n".join(rows)

    # ── 智能×价格 散点 (SVG, 自适应) ─────────────────────────────
    SC_W, SC_H, SC_PAD = 860, 420, 52
    scatter = []
    for r in bench_rows:
        nums = _pricing_nums(r["pricing"])
        if not nums:
            continue
        blend = (nums[0] * 3 + nums[1]) / 4
        avg = sum(r["bench"].values()) / max(1, len(r["bench"]))
        scatter.append({"name": r["name"], "region": r["region"],
                        "intel": round(avg, 1), "price": round(blend, 3),
                        "in_" + r["region"]: True})
    for s in scatter:
        s["is_int"] = s["region"] == "int"

    sx_max = max((s["price"] for s in scatter), default=1) * 1.18
    sx_max = max(sx_max, 1.0)
    sy_lo = max(0, min((s["intel"] for s in scatter), default=30) - 6)
    sy_hi = min(100, max((s["intel"] for s in scatter), default=80) + 6)
    if sy_hi - sy_lo < 10:
        sy_hi = min(100, sy_lo + 10)

    def _sx(p): return SC_PAD + (p / sx_max) * (SC_W - SC_PAD - 16)
    def _sy(v): return (SC_H - 40) - ((v - sy_lo) / (sy_hi - sy_lo)) * (SC_H - 40 - SC_PAD + 18)

    sc_parts = [f'<svg viewBox="0 0 {SC_W} {SC_H}" class="scatter">']
    # 网格 + X 轴刻度
    for frac in (0, .25, .5, .75, 1):
        x = _sx(sx_max * frac)
        sc_parts.append(f'<line x1="{x:.1f}" y1="{SC_PAD-18:.1f}" x2="{x:.1f}" y2="{SC_H-40:.1f}" class="sc-grid"/>')
        sc_parts.append(f'<text x="{x:.1f}" y="{SC_H-22:.1f}" text-anchor="middle" class="sc-tick">${sx_max*frac:.1f}</text>')
    for frac in (0, .25, .5, .75, 1):
        v = sy_lo + (sy_hi - sy_lo) * frac
        y = _sy(v)
        sc_parts.append(f'<line x1="{SC_PAD-14:.1f}" y1="{y:.1f}" x2="{SC_W-16:.1f}" y2="{y:.1f}" class="sc-grid"/>')
        sc_parts.append(f'<text x="{SC_PAD-20:.1f}" y="{y+4:.1f}" text-anchor="end" class="sc-tick">{v:.0f}</text>')
    # 轴标签
    sc_parts.append(f'<text x="{(SC_W+SC_PAD)/2:.0f}" y="{SC_H-4:.0f}" text-anchor="middle" class="sc-axis">混合价格 (输入×3+输出)÷4 · $/1M tokens →</text>')
    sc_parts.append(f'<text x="16" y="{(SC_H-40+SC_PAD)/2:.0f}" text-anchor="middle" class="sc-axis" transform="rotate(-90 16 {(SC_H-40+SC_PAD)/2:.0f})">8 基准均分 →</text>')
    # 象限参考线 (中位)
    if scatter:
        med_p = sorted(s["price"] for s in scatter)[len(scatter)//2]
        med_i = sorted(s["intel"] for s in scatter)[len(scatter)//2]
        sc_parts.append(f'<line x1="{_sx(med_p):.1f}" y1="{SC_PAD-18:.1f}" x2="{_sx(med_p):.1f}" y2="{SC_H-40:.1f}" class="sc-med"/>')
        sc_parts.append(f'<line x1="{SC_PAD-14:.1f}" y1="{_sy(med_i):.1f}" x2="{SC_W-16:.1f}" y2="{_sy(med_i):.1f}" class="sc-med"/>')
        # 象限标注
        sc_parts.append(f'<text x="{_sx(med_p)-10:.1f}" y="{SC_PAD-6:.1f}" text-anchor="end" class="sc-q q-good">质优价廉 ↑</text>')
        sc_parts.append(f'<text x="{_sx(med_p)+10:.1f}" y="{SC_PAD-6:.1f}" text-anchor="start" class="sc-q q-bad">高价高能 →</text>')
        sc_parts.append(f'<text x="{_sx(med_p)-10:.1f}" y="{SC_H-46:.1f}" text-anchor="end" class="sc-q q-mid">低分低价</text>')
        sc_parts.append(f'<text x="{_sx(med_p)+10:.1f}" y="{SC_H-46:.1f}" text-anchor="start" class="sc-q q-bad">溢价区</text>')
    # 散点 + 标签
    for s in sorted(scatter, key=lambda x: x["intel"], reverse=True):
        x, y = _sx(s["price"]), _sy(s["intel"])
        color = "#35e0a1" if s["is_int"] else "#ff7a59"
        sc_parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6.5" fill="{color}" fill-opacity=".85" stroke="{color}" stroke-width="1.4"><title>{esc(s["name"])} · ${s["price"]:.2f} · {s["intel"]:.1f}分</title></circle>')
        label_side = "start" if x < SC_W * .62 else "end"
        sc_parts.append(f'<text x="{x + (10 if label_side == "start" else -10):.1f}" y="{y - 9:.1f}" text-anchor="{label_side}" class="sc-lb">{esc(s["name"])}</text>')
    sc_parts.append(f'<circle cx="{SC_W-190:.1f}" cy="{SC_PAD+8:.1f}" r="6" fill="#35e0a1" fill-opacity=".85"/><text x="{SC_W-178:.1f}" y="{SC_PAD+12:.1f}" class="sc-tick">国际</text>')
    sc_parts.append(f'<circle cx="{SC_W-120:.1f}" cy="{SC_PAD+8:.1f}" r="6" fill="#ff7a59" fill-opacity=".85"/><text x="{SC_W-108:.1f}" y="{SC_PAD+12:.1f}" class="sc-tick">国内</text>')
    sc_parts.append("</svg>")
    scatter_svg = "".join(sc_parts)

    # ── 训练投入对比条 (成本 log 尺度) ──────────────────────────
    TRAIN_KEYS = ["gpu_h", "cost_m", "tokens"]
    TRAIN_LABELS = {"gpu_h": ("GPU 时 (M)", "百万 H100 等效"), "cost_m": ("训练成本 ($M)", "百万美元"),
                    "tokens": ("训练 Tokens (T)", "万亿")}
    trained = [r for r in bench_rows if r["train"].get("cost_m") is not None]
    trained.sort(key=lambda r: -(r["train"].get("cost_m") or 0))
    open_scaled = 0
    for r in trained:
        lic = str(r["license"]).lower()
        if any(t in lic for t in ("apache", "mit", "llama", "cc-by", "社区", "community")):
            open_scaled += 1
    bar_max_cost = max((r["train"].get("cost_m") or 0) for r in trained) if trained else 1
    train_html = "".join(
        f'<div class="cmp-row">'
        f'<div class="cmp-label" title="{esc(r["company"])}">{esc(r["name"][:12])}</div>'
        f'<div class="cmp-bars"><div class="cmp-bar"><span style="width:{max(1.5, math.log10(max(r["train"]["cost_m"], 0.01)) / math.log10(bar_max_cost) * 100):.1f}%"></span></div></div>'
        f'<div class="cmp-val">${r["train"]["cost_m"]:g}M</div></div>'
        for r in trained) or '<p class="muted">暂无数据</p>'

    train_stats_html = ""
    for k in TRAIN_KEYS:
        label, unit = TRAIN_LABELS[k]
        vals = [(r["name"], r["train"].get(k)) for r in bench_rows if r["train"].get(k) is not None]
        if not vals:
            continue
        if k == "tokens":
            def _tok_num(v):
                m = re.match(r"\s*(\d+(?:\.\d+)?)", str(v or ""))
                return float(m.group(1)) if m else 0.0
            top = sorted(vals, key=lambda x: -_tok_num(x[1]))[:3]
            txt = " · ".join(f'{esc(n[:10])} {esc(str(v)[:8])}' for n, v in top)
            train_stats_html += f'<div class="tstat"><b>{label}</b><span>{txt}</span></div>'
        else:
            best = max(vals, key=lambda x: x[1] or 0)
            least = min(vals, key=lambda x: x[1] or 0)
            train_stats_html += (f'<div class="tstat"><b>{label}</b>'
                                 f'<span>最多 {esc(best[0][:12])} {best[1]:g}</span>'
                                 f'<span>最少 {esc(least[0][:12])} {least[1]:g}</span></div>')

    # ── 推理性能表 ─────────────────────────────────────────────
    def infer_table():
        rows = []
        ranked = sorted(bench_rows, key=lambda r: -(r["tps"] or 0))
        for i, r in enumerate(ranked, 1):
            tps_c = _cell(r["tps"], best_tps)
            tps_c = (f'<span class="bmv {"hot" if r["tps"] == best_tps else "good" if (r["tps"] or 0) >= best_tps*.6 else "mid"}">{r["tps"]}</span>'
                     if r["tps"] else '<span class="bmv dim">—</span>')
            ttft_c = (f'<span class="bmv {"hot" if r["ttft"] == best_ttft else "good" if (r["ttft"] or 9) <= best_ttft*1.5 else "mid"}">{r["ttft"]:.1f}s</span>'
                      if r["ttft"] else '<span class="bmv dim">—</span>')
            intel = round(sum(r["bench"].values()) / max(1, len(r["bench"])), 1)
            eff = (f'<b class="bmv hot">{(r["tps"] or 0) * intel / 100:.0f}</b>'
                   if r["tps"] else '<span class="bmv dim">—</span>')
            reg = "国际" if r["region"] == "int" else "国内"
            blob = f'{r["name"]} {r["company"]} {reg}'.lower()
            rows.append(
                f'<tr data-region="{r["region"]}" data-search="{esc(blob)}">'
                f'<td class="c-rank">{i}</td>'
                f'<td class="c-name">{esc(r["name"])}<em>{esc(r["company"])} · {reg}</em></td>'
                f'<td>{tps_c}</td><td>{ttft_c}</td><td>{eff}</td>'
                f'<td class="c-lic">{esc(r["license"])}</td></tr>')
        return "\n".join(rows)

    _all_bench_vals = [v for r in bench_rows if r["bench"] for v in r["bench"].values()]
    best_single = max(_all_bench_vals, default=0)
    best_single_model = next((r["name"] for r in bench_rows if r["bench"]
                              and max(r["bench"].values(), default=0) == best_single), "")
    best_single_bench = next((k for r in bench_rows if r["bench"] for k, v in r["bench"].items()
                              if v == best_single), "")

    perf_view = f'''<section class="view" id="view-perf">
  <div class="sec-head"><h2>模型性能对比 — 训练 × 推理 × 基准</h2>
    <p>{len(bench_rows)} 个模型 · 8 项基准热力矩阵 · 智能×价格象限 · 训练投入 · 推理速度 (tok/s) · 数值为公开口径估算</p></div>

  <div class="kpis">
    <div class="kpi"><div class="kpi-v">{len(bench_rows)}</div><div class="kpi-l">评测模型</div><div class="kpi-s">覆盖国际/国内旗舰</div></div>
    <div class="kpi"><div class="kpi-v">{best_single or "—"}</div><div class="kpi-l">单项最高分</div><div class="kpi-s">{esc(best_single_bench)} · {esc(best_single_model)}</div></div>
    <div class="kpi"><div class="kpi-v">{best_tps or "—"}</div><div class="kpi-l">最快输出 tok/s</div><div class="kpi-s">第三方实测口径</div></div>
    <div class="kpi"><div class="kpi-v">{open_scaled}/{len(trained)}</div><div class="kpi-l">开放权重/已披露训练</div><div class="kpi-s">Apache/MIT/Llama 等</div></div>
  </div>

  <div class="toolbar">
    <div class="seg" data-table-filter="bench-table">
      <button class="seg-btn active" data-region-filter="all">全部</button>
      <button class="seg-btn" data-region-filter="int">国际</button>
      <button class="seg-btn" data-region-filter="cn">国内</button>
    </div>
    <div class="tb-search inline">
      <input type="search" data-table-search="bench-table" placeholder="模糊检索模型 / 厂商 / 基准…" autocomplete="off">
    </div>
    <span class="count"><b class="tcount">{len(bench_rows)}</b> 个</span>
  </div>
  <div class="table-wrap">
    <table class="dtable heatmap" id="bench-table">
      <thead><tr>
        <th>模型</th><th>{'</th><th>'.join(BENCH_LABELS[k] for k in BENCH_KEYS)}</th><th>均分</th>
      </tr></thead>
      <tbody>{bench_matrix()}</tbody>
    </table>
  </div>

  <div class="sec-head" style="margin-top:26px"><h2>智能 × 价格 象限</h2>
    <p>横轴混合价格 (输入×3+输出)÷4 越右越贵 · 纵轴 8 基准均分越高越强 · 虚线为收录模型中位 · 仅含 API 计费模型</p></div>
  <div class="panel"><div class="scatter-wrap">{scatter_svg}</div></div>

  <div class="dash-grid" style="margin-top:16px">
    <div class="panel">
      <div class="panel-h"><span>训练投入对比 (按成本, log 尺度)</span>
        <span class="muted">{len(trained)} 个已披露/估算 · 条长=成本量级</span></div>
      {train_html}
    </div>
    <div class="panel">
      <div class="panel-h"><span>训练投入速览</span><span class="muted">公开口径</span></div>
      {train_stats_html or '<p class="muted">暂无数据</p>'}
    </div>
  </div>

  <div class="sec-head" style="margin-top:26px"><h2>推理性能榜</h2>
    <p>输出速度 (tokens/s) · 首 token 延迟 (TTFT) · 性价比 = 速度 × 基准均分 ÷ 100 · 实测口径为估算</p></div>
  <div class="toolbar">
    <div class="seg" data-table-filter="infer-table">
      <button class="seg-btn active" data-region-filter="all">全部</button>
      <button class="seg-btn" data-region-filter="int">国际</button>
      <button class="seg-btn" data-region-filter="cn">国内</button>
    </div>
    <div class="tb-search inline">
      <input type="search" data-table-search="infer-table" placeholder="模糊检索模型…" autocomplete="off">
    </div>
    <span class="count"><b class="tcount">{len(bench_rows)}</b> 个</span>
  </div>
  <div class="table-wrap">
    <table class="dtable" id="infer-table">
      <thead><tr><th>#</th><th>模型</th><th>tok/s ↓</th><th>TTFT</th><th>速度×智能</th><th>许可证</th></tr></thead>
      <tbody>{infer_table()}</tbody>
    </table>
  </div>
</section>'''

    # ══════════════════════════════════════════════════════════════
    # 7c. Agent 对比视图 — 八维能力 × 特色 × 区别
    # ══════════════════════════════════════════════════════════════
    AGENT_DIMS = [
        ("orch", "编排"), ("tools", "工具"), ("auto", "自主"), ("mem", "记忆"),
        ("mm", "多模态"), ("collab", "多Agent"), ("eco", "生态"), ("cost", "成本"),
    ]
    # Agent 八维与模型六维共用 composite 权重思路: 平均
    agent_rows = []
    for n, a in agent_platforms.items():
        c = agent_caps.get(n)
        if not c:
            continue
        dims = c.get("dims", {})
        avg = round(sum(dims.values()) / len(AGENT_DIMS), 1) if dims else 0
        agent_rows.append({"name": n, "info": a, "dims": dims, "avg": avg,
                           "caps": c, "region": a.get("region")})
    agent_rows.sort(key=lambda r: -r["avg"])
    # 评估范围内的类别/区域分布 (口径: 仅 AGENT_CAPS 覆盖的 24 个平台)
    agent_cap_cats = {r["info"].get("category", "其他") for r in agent_rows}
    agent_cmp_int = sum(1 for r in agent_rows if r["region"] == "int")
    agent_cmp_cn = sum(1 for r in agent_rows if r["region"] == "cn")
    top_agent = agent_rows[0] if agent_rows else None

    def _acell(v):
        if v is None:
            return '<span class="bmv dim">—</span>'
        cls = ("bmv hot" if v >= 85 else
               "bmv good" if v >= 70 else
               "bmv mid" if v >= 50 else
               "bmv low")
        return f'<span class="{cls}">{v}</span>'

    def agent_matrix():
        rows = []
        for r in agent_rows:
            cells = "".join(
                f'<td class="hcell {_hm(r["dims"].get(k), 100)}">{_acell(r["dims"].get(k))}</td>'
                for k, _l in AGENT_DIMS)
            reg = "国际" if r["region"] == "int" else "国内"
            blob = " ".join([r["name"], r["info"].get("product", ""), reg, r["caps"].get("hl", ""),
                             " ".join(f'{k} {v}' for k, v in r["dims"].items())]).lower()
            rows.append(
                f'<tr data-region="{r["region"]}" data-search="{esc(blob)}">'
                f'<td class="c-name">{esc(r["info"].get("product") or r["name"])}<em>{esc(r["name"])} · {reg}</em></td>'
                f'{cells}'
                f'<td class="c-avg"><b>{r["avg"]:.1f}</b></td></tr>')
        return "\n".join(rows)

    # 雷达对比: 预置三组 (国际巨头/编排框架/国内平台), JS 端可切换 6 个以内
    def _agent_radar(r, color):
        return (r["info"].get("product") or r["name"][:10], r["dims"], color)

    radar_sets = {}
    _int_big = next((r for r in agent_rows if r["name"] == "OpenAI"), agent_rows[0] if agent_rows else None)
    _int_big2 = next((r for r in agent_rows if r["name"] == "Anthropic"), None)
    _int_big3 = next((r for r in agent_rows if r["name"] == "Google DeepMind"), None)
    if _int_big and _int_big2 and _int_big3:
        radar_sets["国际巨头"] = [_agent_radar(_int_big, "#35e0a1"), _agent_radar(_int_big2, "#7c5cff"),
                                  _agent_radar(_int_big3, "#4db8ff")]
    _fr = [r for r in agent_rows if r["name"] in ("LangChain", "CrewAI", "AutoGen")]
    if len(_fr) >= 2:
        radar_sets["编排框架"] = [_agent_radar(_fr[0], "#35e0a1"), _agent_radar(_fr[1], "#7c5cff"),
                                  _agent_radar(_fr[2], "#4db8ff")] if len(_fr) >= 3 else \
                                 [_agent_radar(_fr[0], "#35e0a1"), _agent_radar(_fr[1], "#7c5cff")]
    _cn = [r for r in agent_rows if r["region"] == "cn"][:3]
    if _cn:
        radar_sets["国内平台"] = [_agent_radar(r, c) for r, c in zip(_cn, ("#ff7a59", "#ffb020", "#f06ef2"))]

    # 单个 Agent 雷达 SVG (八边形)
    def _radar8_svg(models):
        cx = cy = 150
        R = 100
        n = len(AGENT_DIMS)
        parts = ['<svg viewBox="0 0 300 300" class="radar">']
        for lv in (0.25, 0.5, 0.75, 1.0):
            ring = []
            for i in range(n):
                ang = -math.pi / 2 + 2 * math.pi * i / n
                ring.append(f"{cx + R * lv * math.cos(ang):.1f},{cy + R * lv * math.sin(ang):.1f}")
            parts.append(f'<polygon points="{" ".join(ring)}" class="radar-ring"/>')
        for i, (k, label) in enumerate(AGENT_DIMS):
            ang = -math.pi / 2 + 2 * math.pi * i / n
            x, y = cx + R * math.cos(ang), cy + R * math.sin(ang)
            parts.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" class="radar-axis"/>')
            lx, ly = cx + (R + 24) * math.cos(ang), cy + (R + 24) * math.sin(ang)
            anchor = "middle"
            if lx > cx + 8:
                anchor = "start"
            elif lx < cx - 8:
                anchor = "end"
            parts.append(f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" dominant-baseline="middle" class="radar-txt">{esc(label)}</text>')
        for label, dims, color in models:
            pts = []
            for i, (k, _l) in enumerate(AGENT_DIMS):
                ang = -math.pi / 2 + 2 * math.pi * i / n
                v = max(0.06, (dims.get(k) or 0) / 100.0)
                pts.append(f"{cx + R * v * math.cos(ang):.1f},{cy + R * v * math.sin(ang):.1f}")
            parts.append(f'<polygon points="{" ".join(pts)}" class="radar-area" style="stroke:{color};fill:{color}"/>')
        parts.append("</svg>")
        return "".join(parts)

    agent_radar_html = ""
    agent_radar_tabs = ""
    for i, (set_name, models) in enumerate(radar_sets.items()):
        svg = _radar8_svg(models)
        legend = "".join(f'<span><i style="background:{c}"></i>{esc(l)}</span>' for l, _s, c in models)
        agent_radar_html += (f'<div class="aradar-set{" active" if i == 0 else ""}" data-radar-set="{esc(set_name)}">'
                             f'{svg}<div class="radar-lg">{legend}</div></div>')
        agent_radar_tabs += f'<button class="seg-btn{" active" if i == 0 else ""}" data-radar-tab="{esc(set_name)}">{esc(set_name)}</button>'

    # Agent 基准榜 (有 bench 的)
    ab_rows = [(r, r["caps"].get("bench", {})) for r in agent_rows if r["caps"].get("bench")]
    AB_KEYS = ["SWE-V", "τ²-bench", "BrowseComp", "GAIA", "OSWorld", "AIME25", "GPQA"]
    ab_table_rows = []
    for r, b in sorted(ab_rows, key=lambda x: -sum(x[1].values())):
        cells = "".join(
            f'<td class="hcell {_hm(b.get(k), 100)}">{_cell(b.get(k))}</td>' for k in AB_KEYS)
        reg = "国际" if r["region"] == "int" else "国内"
        blob = f'{r["name"]} {r["info"].get("product","")} {reg}'.lower()
        ab_table_rows.append(
            f'<tr data-region="{r["region"]}" data-search="{esc(blob)}">'
            f'<td class="c-name">{esc(r["info"].get("product") or r["name"])}<em>{esc(r["name"])} · {reg}</em></td>'
            f'{cells}</tr>')
    ab_html = ("\n".join(ab_table_rows)) or \
        '<tr><td colspan="8" class="c-empty">暂无 Agent 基准数据</td></tr>'

    # 特色卡
    hl_cards = "".join(
        f'<div class="hl-card" data-region="{r["region"]}" data-search="{esc((r["name"] + " " + r["info"].get("product","") + " " + r["caps"].get("hl","")).lower())}">'
        f'<div class="hl-head"><b>{esc(r["info"].get("product") or r["name"])}</b>'
        f'<span class="chip reg-{r["region"]}">{"国际" if r["region"] == "int" else "国内"}</span>'
        f'<span class="hl-score">{r["avg"]:.0f}</span></div>'
        f'<p class="hl-hl">★ {esc(r["caps"].get("hl", ""))}</p>'
        f'<p class="hl-diff"><b>关键区别:</b> {esc(r["caps"].get("diff", ""))}</p>'
        f'<p class="hl-power"><b>代表模型:</b> {esc(r["caps"].get("power", "—"))}</p></div>'
        for r in agent_rows) or '<p class="muted">暂无数据</p>'

    agentscmp_view = f'''<section class="view" id="view-agentscmp">
  <div class="sec-head"><h2>Agent 能力数据大盘</h2>
    <p>{len(agent_rows)} 个平台 · 八维能力评估 (编排/工具/自主/记忆/多模态/多Agent协作/生态/成本) · 估算口径 · 一眼看懂各平台最大特色与区别</p></div>

  <div class="kpis">
    <div class="kpi"><div class="kpi-v">{len(agent_rows)}</div><div class="kpi-l">评估平台</div><div class="kpi-s">覆盖 {len(agent_cap_cats)} 大类别 · 全库 {len(agents)} 个</div></div>
    <div class="kpi"><div class="kpi-v">{esc(top_agent["info"].get("product") or top_agent["name"]) if top_agent else "—"}</div><div class="kpi-l">综合八维最高</div><div class="kpi-s">{top_agent["avg"]:.1f} 分 · {esc(top_agent["name"])} · 估算口径</div></div>
    <div class="kpi"><div class="kpi-v">{len(ab_rows)}</div><div class="kpi-l">公开基准覆盖</div><div class="kpi-s">SWE-V / τ² / GAIA 等</div></div>
    <div class="kpi"><div class="kpi-v">{agent_cmp_int}<small>·{agent_cmp_cn}</small></div><div class="kpi-l">国际 · 国内</div><div class="kpi-s">评估范围内 (全库 {agent_int}·{agent_cn})</div></div>
  </div>

  <div class="toolbar">
    <div class="seg" data-table-filter="agentcap-table">
      <button class="seg-btn active" data-region-filter="all">全部</button>
      <button class="seg-btn" data-region-filter="int">国际</button>
      <button class="seg-btn" data-region-filter="cn">国内</button>
    </div>
    <div class="tb-search inline">
      <input type="search" data-table-search="agentcap-table" placeholder="模糊检索平台 / 特色…" autocomplete="off">
    </div>
    <span class="count"><b class="tcount">{len(agent_rows)}</b> 个</span>
  </div>
  <div class="table-wrap">
    <table class="dtable heatmap" id="agentcap-table">
      <thead><tr>
        <th>平台</th><th>{'</th><th>'.join(l for _k, l in AGENT_DIMS)}</th><th>八维均分</th>
      </tr></thead>
      <tbody>{agent_matrix()}</tbody>
    </table>
  </div>

  <div class="sec-head" style="margin-top:26px"><h2>八维能力雷达</h2>
    <p>切换对比组 · 绿=国际 · 紫/蓝=对比项 · 覆盖越多边形越大, 缺角即短板</p></div>
  <div class="dash-grid">
    <div class="panel">
      <div class="panel-h"><span>雷达对比</span>
        <div class="seg" id="agent-radar-tabs">{agent_radar_tabs}</div></div>
      <div class="radar-wrap">{agent_radar_html}</div>
    </div>
    <div class="panel">
      <div class="panel-h"><span>维度说明</span></div>
      <div class="dimdoc">
        <div><b>编排</b><span>工作流/状态机/任务分解能力</span></div>
        <div><b>工具</b><span>函数调用/MCP/代码执行深度</span></div>
        <div><b>自主</b><span>长任务自主执行, 无需人监督</span></div>
        <div><b>记忆</b><span>跨会话持久记忆与知识沉淀</span></div>
        <div><b>多模态</b><span>视觉/语音/文件理解</span></div>
        <div><b>多Agent</b><span>多角色协作与任务交接</span></div>
        <div><b>生态</b><span>连接器/插件/社区规模</span></div>
        <div><b>成本</b><span>100=最友好 (开源/自托管/低价)</span></div>
      </div>
    </div>
  </div>

  <div class="sec-head" style="margin-top:26px"><h2>Agent 公开基准</h2>
    <p>搭载旗舰模型 + 官方工作流口径 · 空白=未公开/不适用 · 数值为估算</p></div>
  <div class="toolbar">
    <div class="seg" data-table-filter="agentbench-table">
      <button class="seg-btn active" data-region-filter="all">全部</button>
      <button class="seg-btn" data-region-filter="int">国际</button>
      <button class="seg-btn" data-region-filter="cn">国内</button>
    </div>
    <div class="tb-search inline">
      <input type="search" data-table-search="agentbench-table" placeholder="模糊检索平台…" autocomplete="off">
    </div>
    <span class="count"><b class="tcount">{len(ab_rows)}</b> 个</span>
  </div>
  <div class="table-wrap">
    <table class="dtable heatmap" id="agentbench-table">
      <thead><tr>
        <th>平台</th><th>{'</th><th>'.join(AB_KEYS)}</th>
      </tr></thead>
      <tbody>{ab_html}</tbody>
    </table>
  </div>

  <div class="sec-head" style="margin-top:26px"><h2>最大特色 · 一图看懂区别</h2>
    <p>★ 最大特色 · 关键区别 · 代表模型 — 按八维均分降序</p></div>
  <div class="toolbar">
    <div class="seg" data-hl-filter>
      <button class="seg-btn active" data-region-filter="all">全部</button>
      <button class="seg-btn" data-region-filter="int">国际</button>
      <button class="seg-btn" data-region-filter="cn">国内</button>
    </div>
    <div class="tb-search inline">
      <input type="search" id="hl-search" placeholder="模糊检索平台 / 特色…" autocomplete="off">
    </div>
    <span class="count"><b class="tcount" id="hl-count">{len(agent_rows)}</b> 个</span>
  </div>
  <div class="hl-grid">{hl_cards}</div>
</section>'''

    # ── 9. GitHub 热榜视图 ───────────────────────────────────
    gh = cache.get("github_repos") or {}
    gh_items = gh.get("items", [])
    LANG_COLORS = {"Python": "#3572A5", "TypeScript": "#3178c6", "JavaScript": "#f1e05a",
                   "Go": "#00ADD8", "Rust": "#dea584", "C": "#555555", "C++": "#f34b7d",
                   "C#": "#178600", "Shell": "#89e051", "Jupyter Notebook": "#DA5B0B",
                   "Java": "#b07219", "Ruby": "#701516", "Haskell": "#8f4ff4",
                   "PHP": "#4F5D95", "Swift": "#F05138", "Kotlin": "#A97bff"}

    def _gh_lang(lang):
        c = LANG_COLORS.get(lang, "#6b7280")
        return f'<span class="gh-lang"><i style="background:{c}"></i>{esc(lang)}</span>'

    def _gh_stars(n):
        return f"{n / 1000:.1f}k" if n >= 1000 else str(n)

    def github_table():
        rows = []
        for i, r in enumerate(gh_items, 1):
            otype = r.get("owner_type", "user")
            ob = ('<span class="badge org">组织</span>' if otype == "org"
                  else '<span class="badge user">个人</span>')
            rising = '<span class="gh-rising">新星</span>' if r.get("rising") else ""
            pick = '<span class="gh-pick">精选</span>' if r.get("org_pick") else ""
            topics = "".join(f'<span class="tag">{esc(t)}</span>' for t in (r.get("topics") or [])[:3])
            blob = " ".join([r.get("full_name", ""), r.get("lang", ""), r.get("desc", ""),
                             " ".join(r.get("topics", []))]).lower()
            rows.append(
                f'<tr data-region="{otype}" data-search="{esc(blob)}">'
                f'<td class="c-rank">{i}</td>'
                f'<td><a class="gh-link" href="{esc(r.get("url"))}" target="_blank" rel="noopener noreferrer">{esc(r.get("full_name"))}</a>{rising}{pick}</td>'
                f'<td>{ob}</td>'
                f'<td>{_gh_lang(r.get("lang") or "—")}</td>'
                f'<td class="c-stars">{_gh_stars(r.get("stars", 0))}</td>'
                f'<td class="c-dim">{_gh_stars(r.get("forks", 0))}</td>'
                f'<td class="c-dim">{esc(r.get("pushed") or "—")}</td>'
                f'<td class="c-desc" title="{esc(r.get("desc"))}">{esc(r.get("desc"))}</td>'
                f'<td class="c-topics">{topics}</td></tr>')
        return "\n".join(rows)

    gh_orgs = gh.get("orgs", [])
    gh_rising_n = sum(1 for r in gh_items if r.get("rising"))
    gh_langs = len(set(r.get("lang") for r in gh_items if r.get("lang") not in (None, "—")))
    gh_updated = (gh.get("updated") or "")[:16].replace("T", " ")
    github_table_html = github_table() if gh_items else \
        '<tr><td colspan="9" class="c-empty">暂无数据 — 运行 python3 ai_intel_aggregator.py 抓取 GitHub 热榜</td></tr>'

    gh_author_card = f'''<div class="gh-author">
    <a class="gh-author-link" href="https://github.com/zhouzxing" target="_blank" rel="noopener noreferrer">
      <span class="gh-author-logo">
        <svg viewBox="0 0 16 16" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M6.766 11.328c-2.063-.25-3.516-1.734-3.516-3.656 0-.781.281-1.625.75-2.188-.203-.515-.172-1.609.063-2.062.625-.078 1.468.25 1.968.703.594-.187 1.219-.281 1.985-.281.765 0 1.39.094 1.953.265.484-.437 1.344-.765 1.969-.687.218.422.25 1.515.046 2.047.5.593.766 1.39.766 2.203 0 1.922-1.453 3.375-3.547 3.64.531.344.89 1.094.89 1.954v1.625c0 .468.391.734.86.547C13.781 14.359 16 11.53 16 8.03 16 3.61 12.406 0 7.984 0 3.563 0 0 3.61 0 8.031a7.88 7.88 0 0 0 5.172 7.422c.422.156.828-.125.828-.547v-1.25c-.219.094-.5.156-.75.156-1.031 0-1.64-.562-2.078-1.609-.172-.422-.36-.672-.719-.719-.187-.015-.25-.093-.25-.187 0-.188.313-.328.625-.328.453 0 .844.281 1.25.86.313.452.64.655 1.031.655s.641-.14 1-.5c.266-.265.47-.5.657-.656"/></svg>
      </span>
      <span class="gh-author-info">
        <b>@zhouzxing</b>
        <small>本站构建者 · 关注 zhouzxing 的更多开源项目与动态</small>
      </span>
    </a>
    <a class="gh-author-cta" href="https://github.com/zhouzxing" target="_blank" rel="noopener noreferrer">在 GitHub 关注 ↗</a>
  </div>'''

    github_view = f'''<section class="view" id="view-github">
  <div class="sec-head"><h2>GitHub 最热 AI 项目</h2>
    <p>{len(gh_items)} 个项目 · {len(gh_orgs)} 个组织 · 主题热榜 / 新星 / 精选组织 · 更新于 {esc(gh_updated)}</p></div>
  {gh_author_card}
  <div class="kpis">
    <div class="kpi"><div class="kpi-v">{len(gh_items)}</div><div class="kpi-l">收录项目</div><div class="kpi-s">按 stars 降序</div></div>
    <div class="kpi"><div class="kpi-v">{gh_rising_n}</div><div class="kpi-l">新星项目</div><div class="kpi-s">近 4 个月创建</div></div>
    <div class="kpi"><div class="kpi-v">{len(gh_orgs)}</div><div class="kpi-l">覆盖组织</div><div class="kpi-s">Community / Org</div></div>
    <div class="kpi"><div class="kpi-v">{gh_langs}</div><div class="kpi-l">编程语言</div><div class="kpi-s">技术栈分布</div></div>
  </div>
  <div class="toolbar">
    <div class="seg" data-table-filter="github-table">
      <button class="seg-btn active" data-region-filter="all">全部</button>
      <button class="seg-btn" data-region-filter="org">组织</button>
      <button class="seg-btn" data-region-filter="user">个人</button>
    </div>
    <div class="tb-search inline">
      <input type="search" data-table-search="github-table" placeholder="模糊检索项目 / 语言 / 描述…" autocomplete="off">
    </div>
    <span class="count"><b class="tcount">{len(gh_items)}</b> 个</span>
  </div>
  <div class="table-wrap">
    <table class="dtable" id="github-table">
      <thead><tr>
        <th>#</th><th>项目</th><th>类型</th><th>语言</th><th>Stars</th><th>Forks</th><th>最近更新</th><th>简介</th><th>标签</th>
      </tr></thead>
      <tbody>{github_table_html}</tbody>
    </table>
  </div>
</section>'''

    # ── 10. 监控中心视图 ───────────────────────────────────────
    mon = cache.get("meta", {}) or {}
    mon_src_status = mon.get("sources_status", {}) or {}
    mon_history = mon.get("sources_history", {}) or {}
    mon_runlog = mon.get("run_log", []) or []

    # 10a. 渠道稳定性: 全渠道汇总
    mon_ch_total = len(mon_src_status)
    mon_ch_good = sum(1 for v in mon_src_status.values() if v.get("dot") == "good")
    mon_ch_retry = sum(1 for v in mon_src_status.values() if v.get("dot") == "bad" and not v.get("stale"))
    mon_ch_stale = sum(1 for v in mon_src_status.values() if v.get("stale"))
    mon_hist_ok = sum(v.get("history_ok", 0) for v in mon_src_status.values())
    mon_hist_tot = sum(v.get("history_total", 0) for v in mon_src_status.values())
    mon_rate = round(mon_hist_ok / mon_hist_tot * 100, 1) if mon_hist_tot else 0.0
    mon_fail_streak = [n for n, v in mon_src_status.items() if v.get("streak_fail", 0) >= 2]

    def _src_row(name, v):
        region = v.get("region", "int")
        reg_tag = ('<span class="tag src">国际</span>' if region == "int" else '<span class="tag cn">国内</span>')
        strat = v.get("strategy") or "RSS 直连"
        kw = "关键词过滤" if "关键词" in strat else "垂直频道"
        fmt = v.get("fmt", "unknown")
        fmt_tag = {"rss2": '<span class="mfmt f-rss2">RSS</span>', "atom": '<span class="mfmt f-atom">Atom</span>'}.get(fmt, '<span class="mfmt f-unk">?</span>')
        ho, ht = v.get("history_ok", 0), v.get("history_total", 0)
        prate = round(ho / ht * 100, 0) if ht else None
        if prate is None:
            rate_cell = '<span class="muted">—</span>'
        else:
            cls = "m-ok" if prate >= 90 else ("m-warn" if prate >= 60 else "m-bad")
            rate_cell = '<span class="mr %s">%d%%</span>' % (cls, prate)
        streak = v.get("streak_fail", 0)
        streak_cell = ('<span class="mr m-bad">连败%d</span>' % streak) if streak >= 2 else '<span class="muted">—</span>'
        blob = " ".join([name, strat, kw, fmt, (v.get("last_err") or "")]).lower()
        # 注意: 不含 url — 镜像源的 url 普遍带 rsshub, 会让检索命中所有渠道
        return (f'<tr data-search="{esc(blob)}">'
                f'<td class="m-name">{esc(name)}</td><td>{reg_tag}</td>'
                f'<td class="m-strat">{esc(strat.split(" + ")[0])} <span class="m-kw">{kw}</span></td>'
                f'<td>{fmt_tag}</td>'
                f'<td class="m-c">{rate_cell}</td>'
                f'<td class="m-c">{streak_cell}</td>'
                f'<td class="m-c"><span class="dot {v.get("dot","muted")}"></span> {esc(v.get("status",""))}</td>'
                f'<td class="m-err" title="{esc(v.get("last_err",""))}">{esc((v.get("last_err") or "正常")[:34])}</td></tr>')

    mon_src_rows = "".join(_src_row(n, v) for n, v in mon_src_status.items())

    # 10b. 管线运行日志 (最近 12 条, 新→旧)
    def _log_row(e):
        t = (e.get("t") or "")[:16].replace("T", " ")
        skip = ' <span class="ml-skip">跳过抓取</span>' if e.get("skipped") else ""
        ok, tot = e.get("src_ok", 0), e.get("src_total", 0)
        okcls = "m-ok" if (tot and ok / tot >= .9) else ("m-warn" if (tot and ok / tot >= .6) else "m-bad")
        gh = e.get("gh_items", 0)
        gher = '<span class="mr m-bad">GH有错</span>' if e.get("gh_err") else '<span class="muted">GH ok</span>'
        blob = " ".join([t, "跳过" if e.get("skipped") else "抓取", "gh", gher]).lower()
        return (f'<tr data-search="{esc(blob)}">'
                f'<td class="m-c">{esc(t)}{skip}</td>'
                f'<td class="m-c"><span class="mr {okcls}">{ok}/{tot}</span></td>'
                f'<td class="m-c">{e.get("items_new", 0)}</td>'
                f'<td class="m-c">{gh}</td>'
                f'<td class="m-c">{gher}</td></tr>')
    mon_log_rows = "".join(_log_row(e) for e in list(reversed(mon_runlog))[:12]) or \
        '<tr><td colspan="5" class="c-empty">暂无运行日志（待下次管线运行产生）</td></tr>'

    # 10c. 爬取技术说明卡
    mon_tech = [
        ("HTTP 双兜底", "requests → stdlib urllib", "统一 _http_get; 无 requests 的 cron 环境自动降级, 代理 127.0.0.1:7897 必配", "good"),
        ("RSS 2.0 / Atom 双格式", "38 源 · fetch_rss_feed", "运行时探测 <feed>/<item> 自适应, 逐源登记格式到 sources_status.fmt", "good"),
        ("AI 关键词过滤", "标题+描述命中", "非垂直频道按 SOURCE_REGISTRY 关键词列表过滤, 降低噪音", "good"),
        ("正文三轮抓取", "并发24线程→重试→补抓", "420s 时间预算, 密度打分选最密段落, 5W1H 摘要", "good"),
        ("热度累加", "多源重复 +1 (cap5)", "论文/官方大厂发布初始 heat=3, 其余 heat=2", "good"),
        ("GitHub Search API", "gh CLI · 3h TTL", "5 主题榜+新星+8 组织精选, _gh_is_ai 二次筛除非 AI 噪音", "good" if not (cache.get("github_repos") or {}).get("errors") else "bad"),
        ("渠道稳定性监控", "成功率/连败/四态", "逐源 history 48 条累积, 24h 未成功判失联, 本轮新增", "good"),
    ]
    mon_tech_html = "".join(
        f'<div class="mtech"><span class="dot {d}"></span><b>{esc(t)}</b><span class="muted">{esc(sub)}</span>'
        f'<p>{esc(desc)}</p></div>'
        for t, sub, desc, d in mon_tech)

    # 10d. 资讯监控: 总量 / 24h 新增 / 热度分布 / 来源贡献 TOP10
    all_news = list(news)  # (entry, region)
    mon_news_total = len(all_news)
    _now_dt = datetime.now()
    mon_news_24h = 0
    for e, _r in all_news:
        try:
            if (_now_dt - datetime.fromisoformat(e.get("timestamp") or e.get("date"))).total_seconds() <= 86400:
                mon_news_24h += 1
        except Exception:
            pass
    heat_dist = [0] * 6  # heat 0..5
    for e, _r in all_news:
        heat_dist[min(int(e.get("heat", 0) or 0), 5)] += 1
    mon_heat_max = max(heat_dist) or 1
    heat_html = "".join(
        f'<div class="cmp-row"><div class="cmp-label">热度 {h}</div>'
        f'<div class="cmp-bars"><div class="cmp-bar"><span style="width:{max(2, heat_dist[h] / mon_heat_max * 100):.1f}%"></span></div></div>'
        f'<div class="cmp-val">{heat_dist[h]}</div></div>'
        for h in range(6))
    src_contrib = {}
    for e, _r in all_news:
        for s in (e.get("sources") or []):
            src_contrib[s] = src_contrib.get(s, 0) + 1
    top_contrib = sorted(src_contrib.items(), key=lambda x: -x[1])[:10]
    tc_max = top_contrib[0][1] if top_contrib else 1
    contrib_html = "".join(
        f'<div class="cmp-row"><div class="cmp-label" title="{esc(s)}">{esc(s[:14])}</div>'
        f'<div class="cmp-bars"><div class="cmp-bar"><span style="width:{n / tc_max * 100:.1f}%"></span></div></div>'
        f'<div class="cmp-val">{n}</div></div>'
        for s, n in top_contrib) or '<p class="muted">暂无</p>'

    monitor_view = f'''<section class="view" id="view-monitor">
  <div class="sec-head"><h2>监控中心</h2>
    <p>资讯管线 · 渠道稳定性 · 爬取技术栈 — 数据源状态更新于 {(mon.get("last_source_refresh") or "")[:16].replace("T"," ") or "—"}</p></div>

  <div class="kpis">
    <div class="kpi"><div class="kpi-v">{mon_news_total}</div><div class="kpi-l">资讯总量</div><div class="kpi-s">国际 {len(entries_int)} · 国内 {len(entries_cn)}</div></div>
    <div class="kpi"><div class="kpi-v">{mon_news_24h}</div><div class="kpi-l">24h 新增</div><div class="kpi-s">近一日入库</div></div>
    <div class="kpi"><div class="kpi-v">{mon_ch_good}<small>/{mon_ch_total}</small></div><div class="kpi-l">渠道活跃</div><div class="kpi-s">成功率 {mon_rate}%</div></div>
    <div class="kpi"><div class="kpi-v">{len(mon_fail_streak)}<small>+{mon_ch_stale}</small></div><div class="kpi-l">连败 / 失联渠道</div><div class="kpi-s">连败≥2 / &gt;24h</div></div>
  </div>

  <div class="monitor-grid">
    <div class="panel">
      <div class="panel-h"><span>资讯热度分布</span><span class="muted">heat 0–5</span></div>
      {heat_html}
    </div>
    <div class="panel">
      <div class="panel-h"><span>来源贡献 TOP 10</span><span class="muted">按入库条数</span></div>
      {contrib_html}
    </div>
    <div class="panel">
      <div class="panel-h"><span>爬取技术栈</span><span class="muted">{len(mon_tech)} 项</span></div>
      <div class="mtech-list">{mon_tech_html}</div>
    </div>
    <div class="panel">
      <div class="panel-h"><span>管线运行日志</span><span class="muted">最近 {min(len(mon_runlog),12)}/{len(mon_runlog)}</span></div>
      <div class="table-wrap" style="max-height:300px">
        <table class="dtable" id="mon-log-table">
          <thead><tr><th>时间</th><th>渠道OK</th><th>新增资讯</th><th>GH 项目</th><th>GH 状态</th></tr></thead>
          <tbody>{mon_log_rows}</tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="sec-head" style="margin-top:26px"><h2>渠道稳定性监控</h2>
    <p>{mon_ch_total} 个渠道 × 爬取策略 × 协议格式 × 成功率 × 连败 — 搜索可过滤</p></div>
  <div class="toolbar">
    <div class="tb-search inline">
      <input type="search" data-table-search="mon-src-table" placeholder="模糊检索渠道 / 策略 / 错误…" autocomplete="off">
    </div>
    <span class="count"><b class="tcount">{mon_ch_total}</b> 个</span>
  </div>
  <div class="table-wrap">
    <table class="dtable" id="mon-src-table">
      <thead><tr><th>渠道</th><th>区域</th><th>爬取策略</th><th>协议</th><th>成功率</th><th>连败</th><th>状态</th><th>最近错误</th></tr></thead>
      <tbody>{mon_src_rows}</tbody>
    </table>
  </div>
</section>'''

    footer = (f'<footer>AI 情报聚合 v9 · 数据每小时自动更新 · 资讯保留最近 {max_days} 天 / 每区最多 {max_entries} 条 · '
              f'{total_models} 个模型 · {len(agents)} 个 Agent 平台 · {len(bench_rows)} 个模型基准评测 · {len(agent_rows)} 个 Agent 八维评估 · {len(gh_items)} 个 GitHub 项目 · 渲染于 {now_str} · '
              f'由 <a class="footer-gh" href="https://github.com/zhouzxing" target="_blank" rel="noopener noreferrer">@zhouzxing</a> 构建 &amp; 维护</footer>')

    html = ("<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n<meta charset=\"UTF-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
            "<title>AI 情报聚合 · 模型能力大盘</title>\n<style>\n" + CSS + "\n</style>\n</head>\n<body>\n"
            + topbar + "<main class=\"wrap\">" + news_view + dash_view + perf_view + models_view + agents_view + agentscmp_view + github_view + monitor_view
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

/* github tab */
.gh-lang{display:inline-flex;align-items:center;gap:6px;font-size:.76rem}
.gh-lang i{width:9px;height:9px;border-radius:50%;flex:0 0 9px}
.badge.org{background:rgba(124,92,255,.16);color:#b3a1ff;border:1px solid rgba(124,92,255,.4)}
.badge.user{background:rgba(255,176,32,.14);color:var(--warn);border:1px solid rgba(255,176,32,.4)}
.gh-rising{display:inline-block;margin-left:8px;font-size:.6rem;font-weight:700;padding:1px 6px;
  border-radius:5px;background:linear-gradient(135deg,rgba(255,107,107,.3),rgba(255,176,32,.3));
  color:#ff8f8f;border:1px solid rgba(255,107,107,.4);vertical-align:2px}
.gh-pick{display:inline-block;margin-left:6px;font-size:.6rem;font-weight:700;padding:1px 6px;
  border-radius:5px;background:rgba(53,224,161,.13);color:var(--accent);
  border:1px solid rgba(53,224,161,.35);vertical-align:2px}
.gh-link{color:var(--accent);font-weight:650}
.gh-link:hover{text-decoration:underline}
.c-rank{color:var(--muted);font-variant-numeric:tabular-nums;width:34px}
.c-stars{color:var(--warn);font-weight:750;font-variant-numeric:tabular-nums}
.c-dim{color:var(--muted);font-size:.76rem}
.c-desc{max-width:340px;font-size:.74rem;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.c-topics{max-width:180px}
.c-topics .tag{margin:0 4px 4px 0}
.c-empty{text-align:center;color:var(--muted);padding:40px}

/* zhouzxing 宣传位 */
.gh-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 11px 4px 8px;margin-left:4px;
  border:1px solid var(--border2);border-radius:20px;background:rgba(255,255,255,.04);
  color:var(--text);font-size:.78rem;font-weight:650;text-decoration:none;transition:.15s}
.gh-badge svg{color:var(--text);flex:0 0 15px}
.gh-badge:hover{border-color:var(--accent);color:var(--accent);background:rgba(53,224,161,.09)}
.gh-author{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:12px 16px;margin-bottom:16px;
  border:1px solid rgba(53,224,161,.22);border-radius:12px;
  background:linear-gradient(120deg,rgba(53,224,161,.08),rgba(124,92,255,.08));position:relative;overflow:hidden}
.gh-author::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;
  background:linear-gradient(180deg,var(--accent),var(--accent2))}
.gh-author-link{display:flex;align-items:center;gap:11px;text-decoration:none;flex:1;min-width:220px}
.gh-author-logo{width:40px;height:40px;flex:0 0 40px;border-radius:50%;display:grid;place-items:center;
  background:rgba(255,255,255,.07);border:1px solid var(--border2);color:var(--text)}
.gh-author-link:hover .gh-author-logo{color:var(--accent);border-color:rgba(53,224,161,.5)}
.gh-author-info{display:flex;flex-direction:column;gap:2px}
.gh-author-info b{font-size:1rem;color:var(--accent);font-weight:750;font-family:ui-monospace,Menlo,monospace}
.gh-author-info small{font-size:.72rem;color:var(--muted)}
.gh-author-cta{padding:8px 16px;border-radius:9px;font-size:.82rem;font-weight:700;text-decoration:none;
  background:linear-gradient(135deg,var(--accent),#1fb586);color:#06281c;transition:.15s}
.gh-author-cta:hover{filter:brightness(1.12)}
.footer-gh{color:var(--accent);text-decoration:none;font-weight:650;font-family:ui-monospace,Menlo,monospace}
.footer-gh:hover{text-decoration:underline}

/* monitor tab */
.monitor-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:14px;margin-bottom:8px}
.mtech-list{display:flex;flex-direction:column;gap:8px}
.mtech{display:flex;flex-direction:column;gap:3px;padding:9px 11px;border:1px solid var(--border);
  border-radius:10px;background:rgba(255,255,255,.02);position:relative;padding-left:26px}
.mtech .dot{position:absolute;left:11px;top:13px}
.mtech b{font-size:.85rem}
.mtech span{font-size:.72rem}
.mtech p{margin:2px 0 0;font-size:.72rem;color:var(--muted);line-height:1.4}
.ml-skip{font-size:.62rem;color:var(--muted);border:1px solid var(--border2);padding:0 5px;border-radius:5px}
.m-name{font-weight:650;white-space:nowrap}
.m-strat{font-size:.76rem}
.m-kw{font-size:.64rem;color:var(--warn);border:1px solid rgba(255,176,32,.35);
  background:rgba(255,176,32,.1);padding:0 5px;border-radius:5px;margin-left:4px}
.m-c{white-space:nowrap}
.m-err{font-size:.72rem;color:var(--dim);max-width:180px;overflow:hidden;text-overflow:ellipsis}
.mfmt{display:inline-block;font-size:.66rem;font-weight:700;padding:1px 7px;border-radius:5px}
.mfmt.f-rss2{background:rgba(53,224,161,.13);color:var(--accent);border:1px solid rgba(53,224,161,.35)}
.mfmt.f-atom{background:rgba(124,92,255,.14);color:#b3a1ff;border:1px solid rgba(124,92,255,.4)}
.mfmt.f-unk{background:rgba(255,255,255,.05);color:var(--muted);border:1px solid var(--border)}
.mr{display:inline-block;font-size:.74rem;font-weight:700;min-width:34px;text-align:center}
.mr.m-ok{color:var(--accent)}.mr.m-warn{color:var(--warn)}.mr.m-bad{color:var(--danger)}
.tag.cn{background:rgba(255,122,89,.14);color:var(--cn);border:1px solid rgba(255,122,89,.35)}
#view-monitor .cmp-row{grid-template-columns:96px 1fr 40px}
#view-monitor .cmp-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ── 性能对比 / Agent 对比 (heatmap + scatter + 特色卡) ────────── */
.dtable.heatmap{min-width:1260px}
.dtable.heatmap td.hcell{text-align:center;padding:8px 6px}
.bmv{display:inline-block;min-width:38px;font-variant-numeric:tabular-nums;font-weight:700;
  font-size:.82rem;padding:3px 7px;border-radius:8px}
.bmv.hot{color:#06281c;background:linear-gradient(135deg,#35e0a1,#7ce8bd)}
.bmv.good{color:#0b2e1f;background:rgba(53,224,161,.28)}
.bmv.mid{color:#d9b45b;background:rgba(255,176,32,.16)}
.bmv.low{color:#c77e6f;background:rgba(255,107,107,.13)}
.bmv.dim{color:var(--muted);font-weight:400;background:rgba(255,255,255,.03)}
.c-avg b{color:var(--accent);font-size:.9rem}
/* 热力背景梯度: 越绿越强 */
.hm-2{background:rgba(53,224,161,.04)}
.hm-3{background:rgba(53,224,161,.08)}
.hm-4{background:rgba(53,224,161,.13)}
.hm-5{background:rgba(53,224,161,.19)}
.hm-6{background:rgba(53,224,161,.26)}
.hm-7{background:rgba(53,224,161,.34)}
.hm-8{background:rgba(53,224,161,.44)}
.hm-9{background:rgba(53,224,161,.56)}
.hm-n{background:transparent}

/* 散点象限图 */
.scatter-wrap{padding:6px 2px 0}
.scatter{width:100%;height:auto;display:block}
.sc-grid{stroke:rgba(255,255,255,.07)}
.sc-med{stroke:rgba(255,255,255,.28);stroke-dasharray:5 5}
.sc-tick{fill:var(--muted);font-size:11px}
.sc-axis{fill:var(--dim);font-size:11.5px}
.sc-lb{fill:var(--text);font-size:11px;font-weight:600}
.sc-q{font-size:11px;font-weight:700}
.sc-q.q-good{fill:var(--accent)}
.sc-q.q-bad{fill:var(--danger)}
.sc-q.q-mid{fill:var(--warn)}

/* 训练投入速览 */
.tstat{display:flex;flex-direction:column;gap:2px;padding:8px 11px;margin-bottom:7px;
  border:1px solid var(--border);border-radius:10px;background:rgba(255,255,255,.02)}
.tstat b{font-size:.8rem;color:var(--warn)}
.tstat span{font-size:.74rem;color:var(--dim)}

/* Agent 对比: 雷达 tab + 特色卡 */
#agent-radar-tabs .seg-btn{padding:4px 10px;font-size:.74rem}
.aradar-set{display:none;flex-direction:column;align-items:center;gap:8px;width:100%}
.aradar-set.active{display:flex}
.dimdoc{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.dimdoc>div{display:flex;flex-direction:column;gap:1px;padding:8px 10px;border:1px solid var(--border);
  border-radius:10px;background:rgba(255,255,255,.02)}
.dimdoc b{font-size:.78rem;color:var(--accent)}
.dimdoc span{font-size:.7rem;color:var(--muted);line-height:1.45}

.hl-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:13px;margin-top:2px}
.hl-card{background:linear-gradient(180deg,var(--surface),var(--surface2));border:1px solid var(--border);
  border-radius:var(--r);padding:14px;display:flex;flex-direction:column;gap:8px;position:relative;
  overflow:hidden;transition:.2s}
.hl-card::before{content:'';position:absolute;inset:0 auto 0 0;width:3px;background:var(--accent);opacity:.3}
.hl-card[data-region="cn"]::before{background:var(--cn)}
.hl-card:hover{transform:translateY(-3px);border-color:var(--border2);box-shadow:var(--shadow)}
.hl-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.hl-head b{font-size:.92rem;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hl-score{font-size:.78rem;font-weight:800;color:var(--accent);border:1px solid rgba(53,224,161,.32);
  background:rgba(53,224,161,.1);border-radius:8px;padding:1px 8px}
.hl-hl{font-size:.8rem;color:#bfe9d8;line-height:1.55}
.hl-diff{font-size:.76rem;color:var(--dim);line-height:1.55}
.hl-diff b{color:var(--warn)}
.hl-power{font-size:.72rem;color:var(--muted)}
.hl-power b{color:var(--dim)}

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
        if(ok&&state.q&&fuzzy(r.dataset.search!==undefined?r.dataset.search:r.textContent,state.q)<0)ok=false;
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

  /* ---- agent radar tab switching ---- */
  var radarTabs=q('#agent-radar-tabs');
  if(radarTabs){
    qa('.seg-btn',radarTabs).forEach(function(b){
      b.addEventListener('click',function(){
        qa('.seg-btn',radarTabs).forEach(function(x){x.classList.remove('active');});
        b.classList.add('active');
        qa('.aradar-set').forEach(function(s){
          s.classList.toggle('active',s.dataset.radarSet===b.dataset.radarTab);
        });
      });
    });
  }

  /* ---- highlight cards filter (Agent 对比 · 最大特色) ---- */
  var hlGrid=q('.hl-grid');
  if(hlGrid){
    var hlCards=qa('.hl-card',hlGrid),hlSearch=q('#hl-search'),hlCount=q('#hl-count');
    var hlSeg=q('[data-hl-filter]'),hlState={q:'',region:'all'};
    function hlApply(){
      var n=0;
      hlCards.forEach(function(c){
        var ok=true;
        if(hlState.region!=='all'&&c.dataset.region!==hlState.region)ok=false;
        if(ok&&hlState.q&&fuzzy(c.dataset.search||c.textContent,hlState.q)<0)ok=false;
        c.style.display=ok?'':'none';if(ok)n++;
      });
      if(hlCount)hlCount.textContent=n;
    }
    var hlDo=debounce(function(v){hlState.q=v;hlApply();},140);
    hlSearch&&hlSearch.addEventListener('input',function(){hlDo(hlSearch.value);});
    if(hlSeg)qa('.seg-btn',hlSeg).forEach(function(b){
      b.addEventListener('click',function(){
        qa('.seg-btn',hlSeg).forEach(function(x){x.classList.remove('active');});
        b.classList.add('active');hlState.region=b.dataset.regionFilter;hlApply();
      });
    });
    hlApply();
  }
})();
"""
