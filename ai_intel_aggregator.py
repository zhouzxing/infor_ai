#!/usr/bin/env python3
"""
AI Daily Intelligence Aggregator - v6
国内/国际分离：Hacker News, Google News, arXiv, Geeker等
支持代理访问，各自按热度排序，≤100条全保留，>100条剔除3天外的低热度，上限300条。
"""

import html as html_mod
import json
import os
import re
import subprocess
import time
from html.parser import HTMLParser
from datetime import datetime, timedelta
from pathlib import Path

# ── config ────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DB_FILE  = BASE_DIR / "ai_intel_cache.json"
HTML_FILE = BASE_DIR / "index.html"
MAX_ENTRIES = 300
MAX_DAYS = 30

# 代理配置
PROXY_HTTP = "http://127.0.0.1:7897"
PROXY_SOCKS = "socks5://127.0.0.1:7897"

os.environ['http_proxy'] = PROXY_HTTP
os.environ['https_proxy'] = PROXY_HTTP
os.environ['all_proxy'] = PROXY_SOCKS

# ── 正文提取与清洗 ────────────────────────────────────────────────
# RSS 的 description 常带整段 HTML，直接截断会得到 "<section style=..." 之类残片。
# 这里做两层处理: 清洗已有 HTML 描述; 有直链时再抓一次正文提取纯文本。
_NOISE = ('http://', 'https://', '©', '版权所有', '点击', '加载更多', '相关阅读',
          '分享至', '微博', '微信', '订阅', '注册', '登录')

def clean_html_to_text(html_text):
    """把 RSS description 的 HTML 片段压成干净文本。
    注意: RSS description 常被截断到 200 字符, 会截在标签中间(如 '<a href="..." targ'),
    这种没有闭合 '>' 的残标签必须整段丢弃, 否则 <[^>]+> 匹配不到。"""
    t = html_text or ""
    # 0) 丢弃末尾未闭合的残标签: 从最后一个 '<' 起, 若其后没有 '>', 说明被截断, 整段删掉
    last_lt = t.rfind('<')
    if last_lt != -1 and '>' not in t[last_lt:]:
        t = t[:last_lt]
    t = re.sub(r'<script[^>]*>.*?</script>', ' ', t, flags=re.S|re.I)
    t = re.sub(r'<style[^>]*>.*?</style>', ' ', t, flags=re.S|re.I)
    t = re.sub(r'<[^>]+>', ' ', t)
    t = html_mod.unescape(t)
    t = re.sub(r'&[a-z]+;', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t

def _keep_sentence(s):
    """过滤导航/版权/图片残留等噪音句"""
    if len(s) < 8:
        return False
    low = s.lower()
    return not any(low.startswith(n) for n in _NOISE)

_ARTICLE_CACHE = {}   # url -> (summary, body)，同一篇只抓一次，避免 summary/body 重复请求
_FETCH_DEADLINE = 0   # 全局抓取时间预算(秒), 超过则停止抓新文章, 用 RSS 描述兜底

def _fetch_allowed():
    """全局时间预算检查: 超过 deadline 后不再发起新抓取"""
    return _FETCH_DEADLINE == 0 or time.time() < _FETCH_DEADLINE

def _clean_para(m_text):
    """清洗一个段落块: 去标签+实体, 合并空白"""
    return clean_html_to_text(m_text)

def _strip_page_chrome(html_text):
    """剥离导航/头部/页脚/侧栏/公告栏等页面骨架, 只保留正文区域。
    否则 IEEE Spectrum 这类站点的 <p> 里塞满 "IEEE.org IEEE Xplore Search:..." 导航项,
    会被当成正文开头段, 把摘要淹没在导航噪音里。"""
    if not html_text:
        return html_text
    t = html_text
    # 结构性噪音: 非正文容器 (用捕获组让 </tag> 能回指标签名)
    t = re.sub(r'<(nav|header|footer|aside|script|style|noscript|svg|form)[^>]*>.*?</\1\s*>',
               ' ', t, flags=re.S | re.I)
    # 按标签名/类名/ID 启发式剥离导航容器
    t = re.sub(r'<div[^>]*(?:class|id)="[^"]*(?:nav|menu|sidebar|footer|header|breadcrumb|cookie|consent|promo|banner|related|recommend|share|social)[^"]*"[^>]*>.*?</div\s*>',
               ' ', t, flags=re.S | re.I)
    t = re.sub(r'<ul[^>]*(?:class|id)="[^"]*(?:nav|menu|breadcrumb|social)[^"]*"[^>]*>.*?</ul\s*>',
               ' ', t, flags=re.S | re.I)
    t = re.sub(r'<(?:section)[^>]*(?:class|id)="[^"]*(?:nav|footer|sidebar)[^"]*"[^>]*>.*?</section\s*>',
               ' ', t, flags=re.S | re.I)
    # 注释
    t = re.sub(r'<!--.*?-->', ' ', t, flags=re.S)
    return t


def _collect_paragraphs(html_text, min_para_len=12):
    """从 HTML 收集正文段落: <p> 优先, 退化到 <li>, 再退化到整块文本切句。
    先 _strip_page_chrome 去导航, 再按标签配对分段(<p> 或 <br> 结束一段)。
    返回按文档顺序排列的干净段落列表。"""
    cleaned = _strip_page_chrome(html_text)
    paras = []

    def _extract(tag):
        """按 <tag ...> 开标签配对切分: 取开标签之后、到 </tag> 或下一个 <tag 之间的文本"""
        out = []
        for m in re.finditer(r'<%s\b[^>]*>' % tag, cleaned, flags=re.S | re.I):
            rest = cleaned[m.end():]
            nxt = re.search(r'</%s\s*>|<%s\b' % (tag, tag), rest, flags=re.I)
            seg = rest[:nxt.start()] if nxt else rest
            out.append(seg)
        return out

    # 1) <p> 段落 (最常见)
    for seg in _extract('p'):
        s = _clean_para(seg)
        if len(s) >= min_para_len and _keep_sentence(s):
            paras.append(s)
    if paras:
        return paras
    # 2) <li> 列表项 (部分站点用列表写正文)
    for seg in _extract('li'):
        s = _clean_para(seg)
        if len(s) >= min_para_len and _keep_sentence(s):
            paras.append(s)
    if paras:
        return paras
    # 3) 整块退化为按句子切分
    full = _clean_para(cleaned)
    for s in re.split(r'(?<=[.!?。！？])\s+', full):
        if len(s) >= min_para_len and _keep_sentence(s):
            paras.append(s)
    return paras


def _best_para_block(paras):
    """从所有段落中挑出"信息密度最高"的连续块, 而不是无脑取开头若干段。
    启发式打分: 总分 × log(段均长度)。中文以 CJK 字数计密度(每字自带信息量),
    英文以词数计。短碎导航段被 log 压低, 长正文块自然胜出。"""
    if len(paras) <= 5:
        return list(paras)
    scores = []
    for i in range(len(paras)):
        end = min(i + 5, len(paras))
        block = paras[i:end]
        cjk = sum(len(re.findall(r'[\u4e00-\u9fff]', p)) for p in block)
        total = sum(len(p) for p in block)
        words = sum(len(p.split()) for p in block)
        density = max(cjk, words * 1.4)
        if total <= 0:
            continue
        avg = total / len(block)
        score = total * (1 + avg / 100.0)
        scores.append((score, i, end))
    if not scores:
        return paras[:5]
    scores.sort(key=lambda x: -x[0])
    _, i, e = scores[0]
    return paras[i:e]


def extract_article_body(url, min_para_len=12, retries=1, timeout=12):
    """抓原文页, 返回 (5W1H摘要, 全文)。抓不到就返回 ('', '')。
    流程: 去页面骨架 → 收集段落 → 挑密度最高的正文块 → 取"开头+结尾"做 5W1H 摘要
    (开头覆盖 Who/What/Why/When/Where, 结尾常含 How/结论, 合起来能讲清文章脉络与主题)。
    块内段落太少(<2段/正文<30字)时退化为 meta 描述。失败/空结果时重试, 按 url 缓存。
    全局时间预算耗尽后直接放弃(返回空), 由调用方用 RSS 描述兜底。"""
    if not url or url.startswith('javascript'):
        return '', ''
    if url in _ARTICLE_CACHE:
        return _ARTICLE_CACHE[url]
    if not _fetch_allowed():
        return '', ''
    last = ('', '')
    for attempt in range(retries + 1):
        if not _fetch_allowed():
            break
        try:
            r_text, r_code, r_err = _http_get(url, timeout=timeout)
            if r_err or r_code != 200 or len(r_text) < 500:
                last = ('', '')
                continue
            # 只取 <body> 内的内容
            body_html = r_text
            bm = re.search(r'<body[^>]*>(.*?)</body>', r_text, flags=re.S | re.I)
            if bm:
                body_html = bm.group(1)
            paras = _collect_paragraphs(body_html, min_para_len)
            block = _best_para_block(paras)

            block_total = sum(len(p) for p in block)
            # 单段文章: 直接作为摘要
            if len(block) == 1 and block_total >= 20:
                last = (block[0][:500], block[0][:1500])
                break
            # 段落充足(>=2 段且 >=30 字): 取开头 3 段 + 结尾 2 段做 5W1H 摘要
            if len(block) >= 2 and block_total >= 30:
                body_full = ' '.join(p[:600] for p in block[:12])[:4000]
                head = block[:3]
                tail = block[-2:] if len(block) > 5 else []
                pick = head + [t for t in tail if t not in head]
                summary = ' '.join(p[:400] for p in pick)[:600]
                last = (summary, body_full)
                break
            # 块太碎: 退化为 meta 描述 (RSS 描述通常已含首段导语)
            for pat in (r'<meta[^>]+name="description"[^>]+content="([^"]{20,400})"',
                        r'<meta[^>]+property="og:description"[^>]+content="([^"]{20,400})"'):
                mm = re.search(pat, r_text, flags=re.I)
                if mm:
                    last = (clean_html_to_text(mm.group(1))[:400], '')
                    break
            if last[0]:
                break
        except Exception:
            last = ('', '')
            if attempt < retries:
                time.sleep(1.5)
    _ARTICLE_CACHE[url] = last
    return last

def build_summary(title, item):
    """生成 5W1H 摘要: 抓到的正文摘要(首尾拼接) > 清洗后的 RSS 描述 > 标题兜底。
    RSS 描述 <50 字或等于标题时, 直接抓原文页正文以拿到完整脉络。"""
    title = (title or "").strip()   # RSS 标题常带前导换行/空格(如 GeekerHub)
    cleaned = clean_html_to_text(item.get('desc', ''))
    # RSS 描述足够长(>=50字)且不等于标题时, 直接用 (截 500)
    if len(cleaned) >= 50 and cleaned != clean_html_to_text(title):
        return cleaned[:500]
    # RSS 描述是标签碎片或太短时, 抓原文页正文 (仅直链; Google News 跳转链接抓不到)
    url = item.get('url', '') or ''
    if url and not url.startswith('javascript') and 'news.google.com' not in url:
        art_sum, _ = extract_article_body(url)
        if len(art_sum) >= 50:
            return art_sum[:500]
        if art_sum and len(cleaned) < len(art_sum):
            return art_sum[:500]
    return cleaned[:500] if len(cleaned) >= 20 else title[:200]

def build_body(title, item):
    """生成干净正文: 抓到的全文 > 清洗后的 RSS 描述 > 标题。上限 4000 字。"""
    title = (title or "").strip()
    cleaned = clean_html_to_text(item.get('desc', ''))
    url = item.get('url', '') or ''
    if url and not url.startswith('javascript') and 'news.google.com' not in url:
        art_sum, art_body = extract_article_body(url)
        if art_body and len(art_body) >= 50:
            return art_body[:4000]
        if art_sum and len(art_sum) > len(cleaned):
            return art_sum[:4000]
    return cleaned[:4000] if cleaned else title

# ── 数据源活跃度追踪 ──────────────────────────────────────────────
# 每个数据源的展示元信息（名称/所属区域/官网地址），渲染数据源状态面板用
SOURCE_REGISTRY = [
    # (名称, 区域, RSS/Atom URL, AI 关键词过滤; 空列表=垂直频道不过滤)
    ("Google News",        "int", "https://news.google.com/rss/search?q=AI+OR+LLM&hl=en-US&gl=US&ceid=US:en", ["ai","llm","gpt","chatgpt","openai","anthropic","claude","gemini","deepmind","transformer","neural","genai","machine learning","agentic","rag","mistral","copilot","deepseek","llama"]),
    ("TechCrunch AI",      "int", "https://techcrunch.com/category/artificial-intelligence/feed/", []),
    ("The Verge AI",       "int", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", []),
    ("Ars Technica",       "int", "https://feeds.arstechnica.com/arstechnica/technology-lab", ["ai","llm","openai","anthropic","deepmind","neural","robot","chip","quantum","google","microsoft","meta","apple","amazon","nvidia","ai model"]),
    ("Wired AI",           "int", "https://www.wired.com/feed/tag/ai/latest/rss", []),
    ("MIT Tech Review",    "int", "https://www.technologyreview.com/feed/", ["ai","llm","openai","anthropic","deepmind","neural","robot","nvidia","chip","ai model"]),
    ("VentureBeat AI",     "int", "https://venturebeat.com/category/ai/feed/", []),
    ("CNBC AI",            "int", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910", []),
    ("CNBC Markets",       "int", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=100003114", ["ai","llm","market","stock","trade","crypto","wall street","nvidia","tech"]),
    ("IEEE Spectrum",      "int", "https://spectrum.ieee.org/feeds/feed.rss", ["ai","llm","robot","chip","neural","quantum","ml","deep"]),
    ("Hugging Face Blog",  "int", "https://huggingface.co/blog/feed.xml", ["ai","llm","model","transformer","open","agent","rag","multimodal"]),
    ("OpenAI News",        "int", "https://openai.com/news/rss.xml", []),
    ("Google DeepMind",    "int", "https://deepmind.google/blog/rss.xml", []),
    ("ZDNet AI",           "int", "https://www.zdnet.com/topic/artificial-intelligence/rss.xml", []),
    ("CNET Tech",          "int", "https://www.cnet.com/rss/topic/ai/", []),
    ("Reddit r/MachineLearning", "int", "https://www.reddit.com/r/MachineLearning/.rss", []),
    ("Product Hunt AI",    "int", "https://www.producthunt.com/feed?category=artificial-intelligence", []),
    ("The Rundown AI",     "int", "https://www.therundown.ai/feed", []),
    ("Import AI",          "int", "https://importai.substack.com/feed", []),
    ("Investing.com Tech", "int", "https://www.investing.com/rss/news_301.rss", ["ai","llm","nvidia","openai","tech","chip","market"]),
    ("CoinDesk (crypto+AI)","int", "https://www.coindesk.com/arc/outboundfeeds/rss/", ["ai","llm","crypto","btc","nvidia","openai","agentic","ai trading"]),
    ("Yahoo Finance",      "int", "https://finance.yahoo.com/news/rssindex", ["ai","llm","nvidia","openai","tech","market","stock","trade"]),
    ("arXiv CS.CL",        "int", "https://rss.arxiv.org/rss/cs.CL", ["llm","language model","nlp","transformer","alignment","rag","agent","prompt","reasoning","neural"]),
    ("arXiv CS.CV",        "int", "https://rss.arxiv.org/rss/cs.CV", ["llm","vision transformer","diffusion","multimodal","video generation","generative","neural","perception"]),
    ("arXiv CS.RO",        "int", "https://rss.arxiv.org/rss/cs.RO", ["llm","language model","vla","vln","embodied","autonomous","reinforcement learning","sim2real","manipulation"]),
    ("arXiv stat.ML",      "int", "https://rss.arxiv.org/rss/stat.ML", ["llm","large language model","transformer","deep learning","neural","generative","alignment","inference"]),
    ("V2EX",               "cn",  "https://www.v2ex.com/index.xml", ["ai","llm","gpt","chatgpt","openai","claude","gemini","deepseek","ai model","gpt-","codex","llama"]),
    ("Google News 中文",   "cn",  "https://news.google.com/rss/search?q=%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD+OR+%E5%A4%A7%E6%A8%A1%E5%9E%8B&hl=zh-CN&gl=CN&ceid=CN:zh-Hans", []),
    ("量子位 QbitAI",      "cn",  "https://www.qbitai.com/feed", []),
    ("爱范儿 ifanr",       "cn",  "https://www.ifanr.com/feed", ["ai","人工智能","大模型","llm","智能","gpt","机器人"]),
    ("少数派 sspai",       "cn",  "https://sspai.com/feed", ["ai","人工智能","大模型","llm","智能","gpt","agent"]),
    ("IT之家",             "cn",  "https://www.ithome.com/rss/", ["ai","人工智能","大模型","llm","gpt","chatgpt","openai","机器人","芯片"]),
    ("极客公园",           "cn",  "https://www.geekpark.net/rss", ["ai","人工智能","大模型","llm","gpt","openai","机器人","智能"]),
    ("雷锋网",             "cn",  "https://www.leiphone.com/feed", ["ai","人工智能","大模型","llm","gpt","openai","机器人","智能","算法"]),
    ("InfoQ 中文",         "cn",  "https://www.infoq.cn/feed", ["ai","人工智能","大模型","llm","gpt","openai","机器人","智能","算法"]),
    ("钛媒体 TMPost",      "cn",  "https://www.tmtpost.com/feed", ["ai","人工智能","大模型","llm","智能","gpt","机器人","算法"]),
    ("虎嗅",               "cn",  "https://rsshub.rssforever.com/huxiu/article", ["ai","人工智能","大模型","llm","智能","gpt","机器人","算法"]),
    ("36氪",               "cn",  "https://rsshub.rssforever.com/36kr/newsflashes", ["ai","人工智能","大模型","llm","智能","gpt","机器人","算法","融资"]),
]

def _http_get(url, timeout=10, proxies=None):
    """统一 HTTP GET, 返回 (text, status_code, error)。
    requests 可用时用 requests（自动处理编码/代理）; 不可用时降级到 stdlib urllib。
    保证在无 requests 的环境（如 cron 默认 python）也能工作。"""
    if proxies is None:
        proxies = {"http": PROXY_HTTP, "https": PROXY_HTTP}
    ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    # 1) 优先 requests
    try:
        import requests
        r = requests.get(url, timeout=timeout, proxies=proxies, headers={"User-Agent": ua})
        if r.encoding and r.encoding.lower() in ("iso-8859-1", "latin-1") and r.apparent_encoding:
            r.encoding = r.apparent_encoding
        return r.text, r.status_code, ""
    except ImportError:
        pass
    # 2) 降级 stdlib urllib（自带代理环境配置）
    try:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": ua})
        handlers = []
        if proxies and proxies.get("https"):
            handlers.append(urllib.request.ProxyHandler({"http": proxies["http"], "https": proxies["https"]}))
        opener = urllib.request.build_opener(*handlers)
        # 注意: build_opener 的 opener.open() 不支持 context 参数, SSL 走默认上下文
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            enc = resp.headers.get_content_charset() or "utf-8"
            return raw.decode(enc, errors="replace"), resp.status, ""
    except Exception as e:
        return "", 0, str(e)[:150]


def fetch_rss_feed(name, url, keywords=None, max_items=12):
    """通用 RSS/Atom 抓取: 自动识别 RSS2/Atom 两种格式, 按 AI 关键词过滤。
    返回 [{'title','url','desc','source','region'}], 失败返回 []。"""
    out = []
    try:
        import xml.etree.ElementTree as ET
        text, code, err = _http_get(url)
        if err:
            report_fetch_error(name, f"请求失败: {err[:80]}")
            return []
        if code != 200 or len(text) < 200:
            report_fetch_error(name, f"HTTP {code}")
            return []
        if not text.lstrip().startswith("<"):
            report_fetch_error(name, "非 XML 响应")
            return []
        try:
            root = ET.fromstring(text)
        except ET.ParseError as pe:
            report_fetch_error(name, f"XML 解析失败: {str(pe)[:60]}")
            return []
        is_atom = root.tag.lower().endswith("feed") or "atom" in root.tag.lower()
        ns_atom = "{http://www.w3.org/2005/Atom}"
        cn_names = ("中文", "量子位", "爱范儿", "少数派", "IT之家", "极客公园", "雷锋网", "InfoQ", "钛媒体", "虎嗅", "36氪", "V2EX")
        if is_atom:
            entries = root.findall(f"{ns_atom}entry") or root.findall("entry")
            for ent in entries[:max_items * 3]:
                title = (ent.findtext(f"{ns_atom}title") or ent.findtext("title") or "").strip()
                link = ""
                for lk in (ent.findall(f"{ns_atom}link") or ent.findall("link")):
                    h = lk.get("href", "")
                    rel = lk.get("rel")
                    if h and (rel == "alternate" or not rel):
                        link = h
                        break
                desc = (ent.findtext(f"{ns_atom}content") or ent.findtext(f"{ns_atom}summary") or "").strip()
                out.append({"title": title, "url": link, "desc": desc[:300], "source": name,
                            "region": "cn" if any(k in name for k in cn_names) else "int"})
        else:
            items = root.findall(".//item")
            for it in items[:max_items * 3]:
                title = (it.findtext("title") or "").strip()
                link = (it.findtext("link") or "").strip()
                desc = (it.findtext("description") or "").strip()
                out.append({"title": title, "url": link, "desc": desc[:300], "source": name,
                            "region": "cn" if any(k in name for k in cn_names) else "int"})
        if not out:
            report_fetch_error(name, "无条目")
            return []
    except Exception as e:
        report_fetch_error(name, str(e)[:120])
        return []
    kws = [k.lower() for k in (keywords or [])]
    if kws:
        return [o for o in out if any(k in (o["title"] + " " + o["desc"]).lower() for k in kws)][:max_items]
    return out[:max_items]

def fetch_all_sources():
    """并发抓取全部注册源, 返回 (results_by_region, errors)。ThreadPoolExecutor 10 线程。"""
    import concurrent.futures
    region_map = {"entries_int": "int", "entries_cn": "cn"}
    by_region = {"int": [], "cn": []}
    errors = {}
    def _one(args):
        name, region, url, kws = args
        try:
            return name, region, fetch_rss_feed(name, url, kws), None
        except Exception as e:
            return name, region, [], str(e)
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(_one, tuple(s)) for s in SOURCE_REGISTRY]
        for fut in concurrent.futures.as_completed(futs):
            name, region, items, err = fut.result()
            by_region.setdefault(region, []).extend(items)
            errors[name] = {"n": len(items), "err": err or ""}
    return by_region, errors


def record_source_status(cache, name, region, n, error="", now_iso=""):
    """记录一次数据源抓取结果到 cache['meta']['sources_history']（成功/失败都记）。
    error 留空时自动取该源最近一次上报的真实错误（来自 _LAST_FETCH_ERRORS）。"""
    if not now_iso:
        now_iso = datetime.now().isoformat()
    if not error and n == 0:
        error = _LAST_FETCH_ERRORS.get(name, "") or "抓取返回空结果"
    meta = cache.setdefault("meta", {})
    hist = meta.setdefault("sources_history", {})
    h = hist.setdefault(name, [])
    h.append({"t": now_iso, "n": int(n or 0), "ok": bool(n), "err": (error or "")[:120]})
    if len(h) > 48:
        del h[:-48]

_LAST_FETCH_ERRORS = {}   # 数据源名 -> 最近一次抓取的原始错误信息，由 fetch 函数上报

def report_fetch_error(name, err):
    """fetch 函数内部捕获到异常时上报，供数据源活跃度面板展示真实失败原因"""
    _LAST_FETCH_ERRORS[name] = str(err)[:150]

def cache_sources(cache):
    """把本轮抓取的状态固化进 cache['meta']['sources_status']，供渲染面板使用"""
    hist = cache.setdefault("meta", {}).get("sources_history", {})
    now_iso = datetime.now().isoformat()
    last_refresh = cache.get("meta", {}).get("last_source_refresh")
    status = {}
    for name, region, url, kws in SOURCE_REGISTRY:
        h = hist.get(name, [])
        last_ok = 0          # 该源最近一次抓取是成功还是失败
        last_n = 0
        last_ok_t = ""
        last_err = ""
        if h:
            ev = h[-1]
            last_ok, last_n = int(ev.get("ok", 0)), int(ev.get("n", 0))
            last_ok_t, last_err = ev.get("t", ""), ev.get("err", "")
        # 最近一次抓取距今超过 24h 视为失联（cron 停了 / 源下线）
        stale = False
        if last_ok_t:
            try:
                hrs = (datetime.now() - datetime.fromisoformat(last_ok_t)).total_seconds() / 3600
                stale = hrs > 24
            except Exception:
                pass
        if last_ok and not stale:
            cur, dot = "活跃", "good"
        elif last_ok == 0 and last_err:
            cur, dot = "重试中", "bad"
        elif stale:
            cur, dot = "失联", "bad"
        else:
            cur, dot = "待同步", "muted"
        status[name] = {"region": region, "url": url, "total_items": last_n,
                        "status": cur, "dot": dot, "last_ok": last_ok,
                        "last_ok_t": last_ok_t, "last_err": last_err, "stale": stale,
                        "history_ok": sum(1 for e in h if e.get("ok")),
                        "history_total": len(h)}
    cache["meta"]["sources_status"] = status
    cache["meta"]["last_source_refresh"] = now_iso
    return status

# ── 国内数据源 ────────────────────────────────────────────────────
def fetch_from_geeker():
    """从极客网获取AI新闻"""
    try:
        import urllib.request
        import ssl
        ctx = ssl.create_default_context()
        
        urls = ["https://www.geekerhub.com/feed", "https://www.geekpark.net/rss"]
        results = []
        
        for rss_url in urls:
            try:
                req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                    content = resp.read().decode()
                    import xml.etree.ElementTree as ET
                    root = ET.fromstring(content)
                    for item in root.findall(".//item")[:10]:
                        title = item.findtext("title", "")
                        link = item.findtext("link", "")
                        desc = item.findtext("description", "") or ""
                        if title and link and any(kw in title.lower() for kw in ['ai', '人工智能', '大模型', 'llm', 'deepseek', '通义']):
                            results.append({'title': title, 'url': link, 'desc': desc[:200], 'source': 'GeekerHub', 'region': 'cn'})
            except Exception as e:
                print(f"[WARN] Geeker RSS failed: {e}")
                report_fetch_error('GeekerHub', e)
        return results[:10]
    except Exception as e:
        print(f"[WARN] Geeker fetch failed: {e}")
        report_fetch_error('GeekerHub', e)
        return []

def fetch_from_huxiu():
    """从虎嗅获取AI相关新闻"""
    try:
        import urllib.request
        import ssl
        ctx = ssl.create_default_context()
        
        results = []
        try:
            req = urllib.request.Request("https://www.huxiu.com/rss.xml", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                content = resp.read().decode()
                import xml.etree.ElementTree as ET
                root = ET.fromstring(content)
                for item in root.findall(".//item")[:10]:
                    title = item.findtext("title", "")
                    link = item.findtext("link", "")
                    desc = item.findtext("description", "") or ""
                    if title and link and any(kw in title.lower() for kw in ['ai', '人工智能', '大模型', 'llm', 'deepseek', '通义', '文心']):
                        results.append({'title': title, 'url': link, 'desc': desc[:200], 'source': '虎嗅', 'region': 'cn'})
        except Exception as e:
            print(f"[WARN] Huxiu RSS failed: {e}")
            report_fetch_error('虎嗅', e)
        return results[:10]
    except Exception as e:
        print(f"[WARN] Huxiu fetch failed: {e}")
        report_fetch_error('虎嗅', e)
        return []

def fetch_from_36kr():
    """从36氪获取AI相关新闻"""
    try:
        import urllib.request
        import ssl
        ctx = ssl.create_default_context()
        
        results = []
        try:
            req = urllib.request.Request("https://36kr.com/feed-news.xml", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                content = resp.read().decode()
                import xml.etree.ElementTree as ET
                root = ET.fromstring(content)
                for item in root.findall(".//item")[:10]:
                    title = item.findtext("title", "")
                    link = item.findtext("link", "")
                    desc = item.findtext("description", "") or ""
                    if title and link and any(kw in title.lower() for kw in ['ai', '人工智能', '大模型', 'llm', 'deepseek', '通义']):
                        results.append({'title': title, 'url': link, 'desc': desc[:200], 'source': '36氪', 'region': 'cn'})
        except Exception as e:
            print(f"[WARN] 36kr RSS failed: {e}")
            report_fetch_error('36氪', e)
        return results[:10]
    except Exception as e:
        print(f"[WARN] 36kr fetch failed: {e}")
        report_fetch_error('36氪', e)
        return []

def fetch_from_ithome():
    """从IT之家获取AI相关新闻"""
    try:
        import urllib.request
        import ssl
        ctx = ssl.create_default_context()
        
        results = []
        try:
            req = urllib.request.Request("https://rsshub.app/ithome/it", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                content = resp.read().decode()
                import xml.etree.ElementTree as ET
                root = ET.fromstring(content)
                for item in root.findall(".//item")[:15]:
                    title = item.findtext("title", "")
                    link = item.findtext("link", "")
                    desc = item.findtext("description", "") or ""
                    if title and link:
                        results.append({'title': title, 'url': link, 'desc': desc[:200], 'source': 'IT之家', 'region': 'cn'})
        except Exception as e:
            print(f"[WARN] ithome RSS failed: {e}")
            report_fetch_error('IT之家', e)
        
        # 过滤AI相关
        filtered = [r for r in results if any(kw in r['title'].lower() for kw in ['ai', '人工智能', '大模型', 'llm', 'deepseek', '通义', '文心'])][:10]
        return filtered
    except Exception as e:
        print(f"[WARN] ithome fetch failed: {e}")
        report_fetch_error('IT之家', e)
        return []

def fetch_from_leifeng():
    """从雷锋网获取AI相关新闻"""
    try:
        import urllib.request
        import ssl
        import xml.etree.ElementTree as ET
        ctx = ssl.create_default_context()
        
        results = []
        try:
            req = urllib.request.Request("https://www.leiphone.com/feed", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                content = resp.read().decode()
                root = ET.fromstring(content)
                for item in root.findall(".//item")[:15]:
                    title = item.findtext("title", "")
                    link = item.findtext("link", "")
                    desc = item.findtext("description", "") or ""
                    if title and link and any(kw in title.lower() for kw in ['ai', '人工智能', '大模型', 'llm', 'deepseek', '机器人', '机器学习', '智能']):
                        results.append({'title': title, 'url': link, 'desc': desc[:200], 'source': '雷锋网', 'region': 'cn'})
        except Exception as e:
            print(f"[WARN] leifeng RSS failed: {e}")
            report_fetch_error('雷锋网', e)
        return results[:15]
    except Exception as e:
        print(f"[WARN] leifeng fetch failed: {e}")
        report_fetch_error('雷锋网', e)
        return []

def fetch_from_infoq():
    """从InfoQ获取AI相关新闻"""
    try:
        import urllib.request
        import ssl
        import xml.etree.ElementTree as ET
        ctx = ssl.create_default_context()
        
        results = []
        try:
            req = urllib.request.Request("https://www.infoq.cn/feed", headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
                content = resp.read().decode()
                root = ET.fromstring(content)
                for item in root.findall(".//item")[:15]:
                    title = item.findtext("title", "")
                    link = item.findtext("link", "")
                    desc = item.findtext("description", "") or ""
                    if title and link and any(kw in title.lower() for kw in ['ai', '人工智能', '大模型', 'llm', 'machine learning', 'deepseek', 'generative', 'transformer', '神经网络']):
                        results.append({'title': title, 'url': link, 'desc': desc[:200], 'source': 'InfoQ', 'region': 'cn'})
        except Exception as e:
            print(f"[WARN] infoq RSS failed: {e}")
            report_fetch_error('InfoQ', e)
        return results[:15]
    except Exception as e:
        print(f"[WARN] infoq fetch failed: {e}")
        report_fetch_error('InfoQ', e)
        return []

# ── 国际数据源 ────────────────────────────────────────────────────
def fetch_from_hackernews():
    """从Hacker News API获取最新AI相关话题"""
    try:
        result = subprocess.run(['curl', '-s', '--max-time', '8', '-x', PROXY_HTTP, 'https://hacker-news.firebaseio.com/v0/topstories.json'], capture_output=True, text=True)
        if result.returncode != 0:
            return []
        
        top_ids = json.loads(result.stdout)
        ai_keywords = ['ai', 'openai', 'anthropic', 'claude', 'gpt', 'gemini', 'deepseek', 'hugging', 'machine learning', 'llm', 'artificial intelligence', 'neural', 'transformer']
        results = []
        
        for story_id in top_ids[:20]:
            story_url = f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json"
            r = subprocess.run(['curl', '-s', '--max-time', '3', '-x', PROXY_HTTP, story_url], capture_output=True, text=True)
            if r.returncode != 0:
                continue
            try:
                story = json.loads(r.stdout)
                title = story.get('title', '').lower()
                if any(kw in title for kw in ai_keywords):
                    results.append({'title': story.get('title', ''), 'url': story.get('url', f"https://news.ycombinator.com/item?id={story_id}"), 'points': story.get('points', 0), 'source': 'Hacker News', 'region': 'int'})
            except:
                continue
        return results
    except Exception as e:
        print(f"[WARN] HN fetch failed: {e}")
        report_fetch_error('Hacker News', e)
        return []

def fetch_from_google_news():
    """从Google News RSS获取AI相关新闻"""
    try:
        import urllib.request
        import ssl
        import xml.etree.ElementTree as ET
        ctx = ssl.create_default_context()
        
        rss_urls = [
            "https://news.google.com/rss/search?q=AI+artificial+intelligence&hl=en-US&gl=US&ceid=US:en",
            "https://news.google.com/rss/search?q=OpenAI+Anthropic&hl=en-US&gl=US&ceid=US:en",
            "https://news.google.com/rss/search?q=AI+machine+learning&hl=en-US&gl=US&ceid=US:en",
        ]
        results = []
        
        for rss_url in rss_urls:
            try:
                req = urllib.request.Request(rss_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, context=ctx, timeout=8) as resp:
                    content = resp.read().decode()
                    root = ET.fromstring(content)
                    for item in root.findall(".//item")[:8]:
                        title = item.findtext("title", "")
                        link = item.findtext("link", "")
                        desc = item.findtext("description", "")[:200]
                        if title and link:
                            results.append({'title': title, 'url': link, 'desc': desc, 'source': 'Google News', 'region': 'int'})
            except Exception as e:
                print(f"[WARN] Google News fetch failed: {e}")
                report_fetch_error('Google News', e)
        return results[:20]
    except Exception as e:
        print(f"[WARN] Google News failed: {e}")
        report_fetch_error('Google News', e)
        return []

def fetch_from_arxiv():
    """从arXiv获取最新AI论文"""
    try:
        import urllib.request
        import ssl
        import xml.etree.ElementTree as ET
        ctx = ssl.create_default_context()
        
        arxiv_url = "http://rss.arxiv.org/rss/cs.AI"
        req = urllib.request.Request(arxiv_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=10) as resp:
            content = resp.read().decode()
            root = ET.fromstring(content)
            results = []
            for item in root.findall(".//item")[:10]:
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                desc = item.findtext("description", "")[:300]
                results.append({'title': title, 'url': link, 'desc': desc, 'source': 'arXiv CS.AI', 'region': 'int'})
            return results
    except Exception as e:
        print(f"[WARN] arXiv fetch failed: {e}")
        report_fetch_error('arXiv CS.AI', e)
        return []

# ── cache management ─────────────────────────────────────────────
def load_cache():
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"entries_int": [], "entries_cn": [], "meta": {"updated": None}}

def save_cache(cache):
    DB_FILE.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

def slug(title):
    return re.sub(r"[^\w\s-]", "", title.lower()).strip().replace(" ", "-")[:60]

def already_exists(entries, title):
    s = slug(title)
    return any(slug(e["title"]) == s for e in entries)

def bump_heat(entries, title, source):
    s = slug(title)
    for e in entries:
        if slug(e["title"]) == s:
            e["heat"] = min(e.get("heat", 1) + 1, 5)
            if "sources" not in e:
                e["sources"] = []
            if source not in e["sources"]:
                e["sources"].append(source)
            return True
    return False

def clean_and_trim(entries, now):
    cutoff = (now - timedelta(days=MAX_DAYS)).isoformat()[:10]
    kept = [e for e in entries if e.get("date", "") >= cutoff]

    # 超过100条时才启用3天热度窗口：剔除3天外且热度≤1的条目
    if len(kept) > 100:
        three_days_ago = (now - timedelta(days=3)).isoformat()[:10]
        kept = [e for e in kept if not (e.get("heat", 0) <= 1 and e.get("date", "") < three_days_ago)]

    # 硬上限300条（按热度排序后截取）
    kept.sort(key=lambda x: (-x.get("heat", 0), x.get("timestamp", "")))
    if len(kept) > MAX_ENTRIES:
        kept = kept[:MAX_ENTRIES]

    all_sources = set()
    for e in kept:
        for s in e.get("sources", []):
            all_sources.add(s)
    return kept, list(all_sources)

def sort_entries(entries):
    entries.sort(key=lambda x: (-x.get("heat", 0), x.get("timestamp", "")))
    return entries

# ── render HTML ──────────────────────────────────────────────────
# ── Model Registry (comprehensive) ────────────────────────────────
MODEL_REGISTRY = {
    # ═══════════════════════════════════════════════════════════════
    # INTERNATIONAL COMPANIES
    # ═══════════════════════════════════════════════════════════════
    "OpenAI": {"region":"int","hq":"San Francisco, USA","year":2015,"desc":"Generative AI research lab, GPT series, o-series reasoning models",
        "products":[
            {"series":"GPT-5","name":"GPT-5","params":"undisclosed","training":"2025 web crawl + synthetic","context":"256K","modalities":"text,vision,code","release":"2025-08","pricing":"$1.25/$10 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Unified reasoning, best cost/perf ratio"},
            {"series":"GPT-4.1","name":"GPT-4.1","params":"undisclosed","training":"2024+ web crawl","context":"1M","modalities":"text","release":"2025-04","pricing":"$2/$8 per 1M tok","license":"Closed (API only)","free":False,"evolution":"1M context, stronger coding/agentic"},
            {"series":"GPT-4.1 mini","name":"GPT-4.1 mini","params":"undisclosed","training":"2024+ web crawl","context":"1M","modalities":"text","release":"2025-04","pricing":"$0.40/$1.60 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Cost-effective 1M context"},
            {"series":"GPT-4o","name":"GPT-4o","params":"~200B (MoE, undisclosed)","training":"2023 web crawl, multi-modal","context":"128K","modalities":"text,vision,audio","release":"2024-05","pricing":"$2.50/$10 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Unified text+vision+audio, first real-time voice"},
            {"series":"GPT-4 Turbo Vision","name":"GPT-4V","params":"~180B (MoE)","training":"2023 web + image data","context":"128K","modalities":"text,vision","release":"2023-11","pricing":"$0.01/$0.03 per 1K","license":"Closed (API only)","free":False,"evolution":"Vision support added to GPT-4 Turbo"},
            {"series":"GPT-3.5 Turbo","name":"GPT-3.5 Turbo","params":"175B","training":"2022 web crawl","context":"4K","modalities":"text","release":"2023-03","pricing":"$0.001/$0.002 per 1K","license":"Closed (API only)","free":True,"evolution":"First cost-effective commercial LLM"},
            {"series":"o1","name":"o1","params":"undisclosed","training":"RL + chain-of-thought","context":"200K","modalities":"text,code","release":"2024-09","pricing":"$15/$60 per 1M tok","license":"Closed (API only)","free":False,"evolution":"First reasoning model, extended thinking"},
            {"series":"o3","name":"o3","params":"undisclosed","training":"RL + extended CoT","context":"200K","modalities":"text,code,vision","release":"2025-04","pricing":"$10/$40 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Deep reasoning, tool use, frontier math/science"},
            {"series":"o3-mini","name":"o3-mini","params":"undisclosed","training":"RL + CoT","context":"200K","modalities":"text,code,vision","release":"2025-01","pricing":"$1/$4 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Budget reasoning, strong coding"},
            {"series":"o4-mini","name":"o4-mini","params":"undisclosed","training":"RL + CoT","context":"200K","modalities":"text,code,vision","release":"2025-03","pricing":"$0.50/$2 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Fastest reasoning, agentic use"},
        ]},
    "Anthropic": {"region":"int","hq":"San Francisco, USA","year":2021,"desc":"AI safety company, Claude series, Constitutional AI alignment",
        "products":[
            {"series":"Claude 4 Opus","name":"Claude Opus 4","params":"undisclosed (est. 500B+)","training":"2024 web, synthetic + RLHF","context":"200K","modalities":"text,vision","release":"2025-05","pricing":"$15/$75 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Frontier reasoning, tool use, strongest coding"},
            {"series":"Claude 4 Sonnet","name":"Claude Sonnet 4","params":"undisclosed (est. 200B)","training":"2024 web + synthetic","context":"200K","modalities":"text,vision","release":"2025-05","pricing":"$3/$15 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Balanced perf/cost, strong coding & tool use"},
            {"series":"Claude 4 Haiku","name":"Claude Haiku 4","params":"undisclosed (est. 50B)","training":"2024 web + synthetic","context":"200K","modalities":"text","release":"2025-05","pricing":"$1/$5 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Fastest Claude, enterprise throughput"},
            {"series":"Claude 3.5 Sonnet","name":"Claude 3.5 Sonnet","params":"undisclosed","training":"2024 web","context":"200K","modalities":"text,vision","release":"2024-06","pricing":"$3/$15 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Vision support, strong coding, computer use"},
            {"series":"Claude 3.5 Haiku","name":"Claude 3.5 Haiku","params":"undisclosed","training":"2024 web","context":"200K","modalities":"text","release":"2024-10","pricing":"$0.80/$4 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Ultra-fast, strong on simple tasks"},
            {"series":"Claude 3 Opus","name":"Claude 3 Opus","params":"~175B","training":"2023 web","context":"200K","modalities":"text","release":"2024-03","pricing":"$15/$75 per 1M tok","license":"Closed (API only)","free":False,"evolution":"First 200K context Claude"},
        ]},
    "Google DeepMind": {"region":"int","hq":"Mountain View, USA","year":2010,"desc":"DeepMind + Google Brain merger, Gemini multimodal series",
        "products":[
            {"series":"Gemini 2.5 Pro","name":"Gemini 2.5 Pro","params":"undisclosed","training":"2024+ web, synthetic + RL","context":"1M","modalities":"text,vision,audio,video","release":"2025-06","pricing":"$1.25/$10 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Frontier reasoning + multimodal, 1M context"},
            {"series":"Gemini 2.5 Flash","name":"Gemini 2.5 Flash","params":"undisclosed","training":"2024+ web, synthetic","context":"1M","modalities":"text,vision,audio,video","release":"2025-06","pricing":"$0.30/$2.50 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Best cost/perf, strong vision+audio"},
            {"series":"Gemini 2.0 Flash","name":"Gemini 2.0 Flash","params":"undisclosed","training":"2024 web, synthetic","context":"1M","modalities":"text,vision,audio,video","release":"2024-12","pricing":"$0.15/$0.60 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Multimodal, agentic tool use"},
            {"series":"Gemini 1.5 Pro","name":"Gemini 1.5 Pro","params":"undisclosed","training":"2023+ web","context":"2M","modalities":"text,vision,video","release":"2024-02","pricing":"$3.50/$10.50 per 1M tok","license":"Closed (API only)","free":False,"evolution":"2M context window pioneer"},
            {"series":"Gemini 1.5 Flash","name":"Gemini 1.5 Flash","params":"undisclosed","training":"2023+ web","context":"1M","modalities":"text,vision","release":"2024-02","pricing":"$0.35/$1.05 per 1M tok","license":"Closed (API only)","free":False,"evolution":"Fast 1M context, cost-efficient"},
        ]},
    "Meta AI": {"region":"int","hq":"Menlo Park, USA","year":2013,"desc":"Meta AI division, Llama open-weight series",
        "products":[
            {"series":"Llama 4 Scout","name":"Llama 4 Scout","params":"109B (17B active, MoE)","training":"2024 web + synthetic + RL","context":"128K","modalities":"text,vision","release":"2025-04","pricing":"Open (CC-BY-NC-4.0)","license":"CC-BY-NC-4.0","free":True,"evolution":"First MoE Llama, 10M context w/ sparse"},
            {"series":"Llama 4 Maverick","name":"Llama 4 Maverick","params":"400B (17B active, MoE)","training":"2024 web + synthetic + RL","context":"128K","modalities":"text,vision","release":"2025-04","pricing":"Open (CC-BY-NC-4.0)","license":"CC-BY-NC-4.0","free":True,"evolution":"Strongest open MoE, 17B active params"},
            {"series":"Llama 3.3 70B","name":"Llama 3.3 70B","params":"70B","training":"2024 web, 15T tokens","context":"128K","modalities":"text,code","release":"2024-12","pricing":"Open (Llama Community License)","license":"Llama Community License","free":True,"evolution":"70B dense, best open 70B class"},
            {"series":"Llama 3.1 405B","name":"Llama 3.1 405B","params":"405B","training":"2024 web, 15T tokens","context":"128K","modalities":"text,code","release":"2024-07","pricing":"Open (Llama Community License)","license":"Llama Community License","free":True,"evolution":"First 405B open model, MoE-adjacent"},
            {"series":"Llama 3.1 70B","name":"Llama 3.1 70B","params":"70B","training":"2024 web","context":"128K","modalities":"text,vision","release":"2024-07","pricing":"Open (Llama Community License)","license":"Llama Community License","free":True,"evolution":"Multimodal open, vision encoder"},
            {"series":"Llama 3.2 Vision","name":"Llama 3.2 Vision 11B/90B","params":"11B / 90B","training":"2024 web + image data","context":"128K","modalities":"text,vision","release":"2024-09","pricing":"Open (Llama Community License)","license":"Llama Community License","free":True,"evolution":"Smaller multimodal open models"},
        ]},
    "Mistral AI": {"region":"int","hq":"Paris, France","year":2023,"desc":"European AI startup, open-weight + commercial series",
        "products":[
            {"series":"Mistral Large 2","name":"Mistral Large 2","params":"123B","training":"2024 web, 30T tokens","context":"128K","modalities":"text","release":"2024-06","pricing":"$0.50/$1.50 per 1M tok","license":"Closed (API) / open-weights","free":False,"evolution":"123B dense, strong multilingual"},
            {"series":"Mistral Medium 2","name":"Mistral Medium 2","params":"258B (MoE)","training":"2024 web","context":"128K","modalities":"text","release":"2025-06","pricing":"$0.40/$1.20 per 1M tok","license":"Closed (API)","free":False,"evolution":"MoE medium, 14B active"},
            {"series":"Mistral Small 3","name":"Mistral Small 3","params":"24B","training":"2024 web","context":"128K","modalities":"text,vision","release":"2025-03","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Multimodal small open model"},
            {"series":"Codestral 25","name":"Codestral 25","params":"22B","training":"2024 code, 2.7T tokens","context":"32K","modalities":"text,code","release":"2024-09","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Best open coding model at release"},
            {"series":"Mistral Pixtral","name":"Pixtral 12B","params":"12B","training":"2024 web + image data","context":"128K","modalities":"text,vision","release":"2024-07","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Open multimodal, strong vision"},
        ]},
    "xAI": {"region":"int","hq":"San Francisco, USA","year":2023,"desc":"Elon Musk's AI company, Grok series",
        "products":[
            {"series":"Grok 3","name":"Grok 3","params":"undisclosed (est. 500B+)","training":"2024+ web, RL","context":"131K","modalities":"text,vision","release":"2025-02","pricing":"$0.50/$3 per 1M tok (x.ai API)","license":"Closed (API only)","free":False,"evolution":"Frontier reasoning, 285 GPU nodes"},
            {"series":"Grok 3 Beta","name":"Grok 3 Beta","params":"undisclosed","training":"2024+ web, RL","context":"131K","modalities":"text","release":"2025-02","pricing":"$0/$0 (limited free via xAI)","license":"Closed (API only)","free":True,"evolution":"Early access beta version"},
            {"series":"Grok 2","name":"Grok 2","params":"undisclosed","training":"2024 web","context":"128K","modalities":"text","release":"2024-09","pricing":"$0/$0 (free via X)","license":"Closed (API only)","free":True,"evolution":"Free tier via X platform"},
        ]},
    "Microsoft Research": {"region":"int","hq":"Redmond, USA","year":1975,"desc":"Microsoft AI division, Phi series (small language models)",
        "products":[
            {"series":"Phi-4","name":"Phi-4","params":"14B","training":"2024 curated text + synthetic","context":"16K","modalities":"text","release":"2024-11","pricing":"$0/$0 (Azure Free)","license":"MIT","free":True,"evolution":"Strongest 14B class, synthetic data focus"},
            {"series":"Phi-3.5-mini","name":"Phi-3.5-mini","params":"3.8B","training":"2024 curated text","context":"128K","modalities":"text","release":"2024-08","pricing":"Open (MIT)","license":"MIT","free":True,"evolution":"3.8B, 128K context, on-device"},
            {"series":"Phi-3","name":"Phi-3 Mini/Medium","params":"3.8B / 14B","training":"2024 curated text","context":"8K / 128K","modalities":"text","release":"2024-04","pricing":"Open (MIT)","license":"MIT","free":True,"evolution":"Small model series, text-first"},
        ]},
    "Cohere": {"region":"int","hq":"Toronto, Canada","year":2019,"desc":"Enterprise-focused AI, Command series, multilingual",
        "products":[
            {"series":"Command R+","name":"Command R+","params":"undisclosed","training":"2023+ web, 1.7M context","context":"128K","modalities":"text","release":"2024-06","pricing":"$2.50/$10 per 1M tok","license":"Closed (API) / open-weights","free":False,"evolution":"128K context, strong RAG"},
            {"series":"Command R","name":"Command R","params":"undisclosed","training":"2023+ web","context":"128K","modalities":"text","release":"2024-03","pricing":"$0.50/$2 per 1M tok","license":"Closed (API)","free":False,"evolution":"Enterprise RAG, 128K context"},
            {"series":"Cohere Embed v3","name":"Embed v3","params":"undisclosed","training":"2024 web","context":"512","modalities":"text","release":"2024-01","pricing":"$0.00001 per 1K tok","license":"Closed (API)","free":False,"evolution":"Multilingual embeddings, 100+ languages"},
        ]},
    "NVIDIA": {"region":"int","hq":"Santa Clara, USA","year":1993,"desc":"GPU manufacturer, Nemotron series, enterprise fine-tuned models",
        "products":[
            {"series":"Llama-3.1-Nemotron-70B","name":"Llama-3.1-Nemotron-70B","params":"70B","training":"2024 enterprise + Llama 3.1 base","context":"128K","modalities":"text","release":"2024-11","pricing":"Open (Llama Community License)","license":"Llama Community License","free":True,"evolution":"Fine-tuned for enterprise, 70B"},
            {"series":"Nemotron-4-340B","name":"Nemotron-4-340B","params":"340B (MoE)","training":"2023 web","context":"32K","modalities":"text","release":"2024-01","pricing":"Open (NVIDIA Open Model License)","license":"NVIDIA Open Model License","free":True,"evolution":"340B MoE, early large MoE"},
        ]},
    "IBM Research": {"region":"int","hq":"Armonk, USA","year":1911,"desc":"IBM AI division, Granite open-weight series",
        "products":[
            {"series":"Granite 3.0","name":"Granite 3.0 8B/22B","params":"8B / 22B","training":"2024 web + synthetic","context":"128K","modalities":"text,vision","release":"2024-12","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Multimodal open, Apache 2.0"},
            {"series":"Granite 2.0","name":"Granite 2.0 13B","params":"13B","training":"2024 web","context":"128K","modalities":"text","release":"2024-09","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Enterprise open-weight, 128K"},
        ]},
    "Stability AI": {"region":"int","hq":"San Francisco, USA","year":2019,"desc":"Image generation, Stable Diffusion series",
        "products":[
            {"series":"Stable Diffusion 3.5","name":"SD 3.5 Large/Turbo","params":"8B","training":"2024 licensed image data","context":"N/A (image gen)","modalities":"image,video","release":"2024-10","pricing":"Open (open weights)","license":"Stability AI Community License","free":True,"evolution":"Stronger prompt adherence, 8B params"},
            {"series":"Stable Diffusion XL","name":"SDXL","params":"2.6B","training":"2023 licensed image data","context":"N/A","modalities":"image","release":"2023-07","pricing":"Open (open weights)","license":"Stability AI Community License","free":True,"evolution":"High-res image gen, 2.6B"},
        ]},
    "Black Forest Labs": {"region":"int","hq":"Berlin, Germany","year":2024,"desc":"FLUX image generation series",
        "products":[
            {"series":"FLUX 1.1 Pro","name":"FLUX 1.1 Pro","params":"12B","training":"2024 licensed image data","context":"N/A (image gen)","modalities":"image","release":"2024-11","pricing":"API + open weights","license":"FLUX Community License","free":False,"evolution":"High-quality image gen, 12B params"},
            {"series":"FLUX 1.1 Dev","name":"FLUX 1.1 Dev","params":"12B","training":"2024 licensed image data","context":"N/A","modalities":"image","release":"2024-11","pricing":"Open (non-commercial)","license":"FLUX Community License","free":True,"evolution":"Open dev weights, 12B"},
            {"series":"FLUX.1 Schnell","name":"FLUX.1 Schnell","params":"12B","training":"2024 licensed image data","context":"N/A","modalities":"image","release":"2024-08","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Fastest FLUX, 4-step generation"},
        ]},
    "Databricks": {"region":"int","hq":"San Francisco, USA","year":2013,"desc":"Data + AI platform, DBRX open model",
        "products":[
            {"series":"DBRX","name":"DBRX","params":"132B (36B active, MoE)","training":"2023 web, 4T tokens","context":"32K","modalities":"text,code","release":"2024-05","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Open MoE, 24 heads, 36B active"},
        ]},
    # ═══════════════════════════════════════════════════════════════
    # CHINESE COMPANIES
    # ═══════════════════════════════════════════════════════════════
    "阿里巴巴 (Alibaba)": {"region":"cn","hq":"Hangzhou, China","year":1999,"desc":"Alibaba Cloud, Qwen (通义千问) series, open-weight + commercial",
        "products":[
            {"series":"Qwen3.6 72B","name":"Qwen3.6 72B","params":"72B","training":"2025 web + synthetic","context":"128K","modalities":"text,code","release":"2025-05","pricing":"Open (Apache 2.0) / free API tier","license":"Apache 2.0","free":True,"evolution":"Multilingual reasoning, strong coding"},
            {"series":"Qwen3.5","name":"Qwen3.5 72B","params":"72B","training":"2024+ web, 28T tokens","context":"32K","modalities":"text,code","release":"2024-11","pricing":"Open (Apache 2.0) / free API tier","license":"Apache 2.0","free":True,"evolution":"Stronger reasoning, 128K context"},
            {"series":"Qwen2.5 72B","name":"Qwen2.5 72B","params":"72B","training":"2024 web, 18T tokens","context":"32K","modalities":"text,code","release":"2024-09","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Strongest open Chinese 72B"},
            {"series":"Qwen2.5 32B","name":"Qwen2.5 32B","params":"32B","training":"2024 web","context":"32K","modalities":"text,code","release":"2024-09","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"32B dense, cost-efficient"},
            {"series":"Qwen2.5-VL","name":"Qwen2.5-VL 72B","params":"72B","training":"2024 web + image data","context":"32K","modalities":"text,vision","release":"2024-12","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Open multimodal, strong vision"},
            {"series":"Qwen2.5-Coder","name":"Qwen2.5-Coder 72B","params":"72B","training":"2024 code, 18T tokens","context":"32K","modalities":"text,code","release":"2024-11","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Specialized coding model"},
        ]},
    "字节跳动 (ByteDance)": {"region":"cn","hq":"Beijing, China","year":2012,"desc":"ByteDance AI, Doubao (豆包) series, multimodal",
        "products":[
            {"series":"Doubao 1.6 Pro","name":"Doubao 1.6 Pro","params":"undisclosed","training":"2024+ web, synthetic","context":"32K","modalities":"text,vision","release":"2025-03","pricing":"$0.28/$0.28 per 1M tok","license":"Closed (API)","free":False,"evolution":"Strong reasoning, vision, multilingual"},
            {"series":"Doubao 1.5 Pro 32K","name":"Doubao 1.5 Pro 32K","params":"undisclosed","training":"2024 web","context":"32K","modalities":"text,vision,code","release":"2024-11","pricing":"$0.28/$0.28 per 1M tok","license":"Closed (API)","free":False,"evolution":"32K context, code support"},
            {"series":"Doubao 1.5 Pro","name":"Doubao 1.5 Pro","params":"undisclosed","training":"2024 web","context":"8K","modalities":"text,vision","release":"2024-09","pricing":"$0.28/$0.28 per 1M tok","license":"Closed (API)","free":False,"evolution":"Multimodal, strong Chinese"},
            {"series":"Doubao 1.4 Pro","name":"Doubao 1.4 Pro","params":"undisclosed","training":"2023+ web","context":"8K","modalities":"text,vision","release":"2024-07","pricing":"Free tier (daily quota)","license":"Closed (API)","free":True,"evolution":"First free tier Doubao"},
        ]},
    "百度 (Baidu)": {"region":"cn","hq":"Beijing, China","year":2000,"desc":"Baidu AI, Ernie (文心一言) series, ERNIE 4.0",
        "products":[
            {"series":"ERNIE 4.5","name":"ERNIE 4.5","params":"undisclosed","training":"2024+ web","context":"128K","modalities":"text,vision","release":"2025-03","pricing":"$1/$2 per 1M tok","license":"Closed (API)","free":False,"evolution":"128K context, multimodal"},
            {"series":"ERNIE 4.0","name":"ERNIE 4.0","params":"undisclosed","training":"2024 web","context":"8K","modalities":"text,vision","release":"2024-04","pricing":"$1/$2 per 1M tok","license":"Closed (API)","free":False,"evolution":"Stronger reasoning, vision"},
            {"series":"ERNIE 3.5","name":"ERNIE 3.5","params":"undisclosed","training":"2023+ web","context":"32K","modalities":"text,vision","release":"2023-10","pricing":"Free tier (daily quota)","license":"Closed (API)","free":True,"evolution":"First multimodal Chinese LLM"},
            {"series":"ERNIE X 1.0","name":"ERNIE X 1.0","params":"undisclosed","training":"2024+ web + synthetic","context":"32K","modalities":"text,vision,video","release":"2025-01","pricing":"$1/$2 per 1M tok","license":"Closed (API)","free":False,"evolution":"Multimodal video support"},
        ]},
    "智谱AI (Zhipu AI)": {"region":"cn","hq":"Beijing, China","year":2019,"desc":"Zhipu AI, GLM (智谱清言) series, open-weight + commercial",
        "products":[
            {"series":"GLM-4.5","name":"GLM-4.5","params":"undisclosed","training":"2024+ web","context":"128K","modalities":"text,vision","release":"2025-03","pricing":"$0.50/$0.50 per 1M tok","license":"Open (MIT) / commercial","free":True,"evolution":"128K context, multimodal, open"},
            {"series":"GLM-4 Plus","name":"GLM-4 Plus","params":"undisclosed","training":"2024 web","context":"32K","modalities":"text","release":"2024-07","pricing":"$0.50/$0.50 per 1M tok","license":"Closed (API)","free":False,"evolution":"Stronger reasoning, 32K"},
            {"series":"GLM-4V","name":"GLM-4V","params":"undisclosed","training":"2024 web + image data","context":"8K","modalities":"text,vision","release":"2024-06","pricing":"$0.50/$0.50 per 1M tok","license":"Closed (API)","free":False,"evolution":"Multimodal, vision support"},
            {"series":"GLM-4-32B","name":"GLM-4-32B","params":"32B","training":"2024 web","context":"8K","modalities":"text","release":"2024-06","pricing":"Open (MIT)","license":"MIT","free":True,"evolution":"Open 32B, strong Chinese"},
            {"series":"GLM-4-9B","name":"GLM-4-9B","params":"9B","training":"2024 web","context":"8K","modalities":"text","release":"2024-05","pricing":"Open (MIT)","license":"MIT","free":True,"evolution":"Small open model, on-device"},
        ]},
    "商汤科技 (SenseTime)": {"region":"cn","hq":"Shanghai, China","year":2014,"desc":"SenseTime, SenseNova (商量) series, multimodal",
        "products":[
            {"series":"SenseNova 6.8","name":"SenseNova 6.8 Flash","params":"undisclosed","training":"2024+ web, synthetic","context":"32K","modalities":"text,vision","release":"2025-03","pricing":"$0.28/$0.28 per 1M tok","license":"Closed (API)","free":False,"evolution":"Multimodal, strong Chinese, vision"},
            {"series":"SenseNova 5.6","name":"SenseNova 5.6","params":"undisclosed","training":"2024 web","context":"32K","modalities":"text,vision","release":"2024-10","pricing":"Free tier (daily quota)","license":"Closed (API)","free":True,"evolution":"Multimodal, free tier"},
            {"series":"SenseNova Lite","name":"SenseNova Lite","params":"undisclosed","training":"2024 web","context":"8K","modalities":"text","release":"2024-06","pricing":"Free tier (daily quota)","license":"Closed (API)","free":True,"evolution":"Lite model, fast inference"},
        ]},
    "月之暗面 (Moonshot AI)": {"region":"cn","hq":"Beijing, China","year":2023,"desc":"Moonshot AI, Kimi series, ultra-long context",
        "products":[
            {"series":"Kimberly K2","name":"Kimi K2","params":"undisclosed","training":"2024+ web, synthetic","context":"128K","modalities":"text,vision","release":"2025-03","pricing":"$0.50/$0.50 per 1M tok","license":"Closed (API)","free":False,"evolution":"128K context, strong reasoning"},
            {"series":"Kimi K1.5","name":"Kimi K1.5","params":"undisclosed","training":"2024 web","context":"128K","modalities":"text,vision","release":"2024-10","pricing":"$0.50/$0.50 per 1M tok","license":"Closed (API)","free":False,"evolution":"128K, multimodal, strong Chinese"},
            {"series":"Kimi Researcher","name":"Kimberly Researcher","params":"undisclosed","training":"2024 web","context":"200K","modalities":"text","release":"2025-01","pricing":"Free tier (limited)","license":"Closed (API)","free":True,"evolution":"Research agent, long context"},
            {"series":"Kimi Chat","name":"Kimi Chat","params":"undisclosed","training":"2023+ web","context":"32K","modalities":"text","release":"2023-12","pricing":"Free tier (daily quota)","license":"Closed (API)","free":True,"evolution":"Ultra-long context pioneer (200K)"},
        ]},
    "百川智能 (Baichuan)": {"region":"cn","hq":"Beijing, China","year":2023,"desc":"Baichuan Intelligence, open-weight Chinese LLMs",
        "products":[
            {"series":"Baichuan 4","name":"Baichuan 4","params":"undisclosed","training":"2024+ web","context":"32K","modalities":"text","release":"2025-01","pricing":"Open (MIT)","license":"MIT","free":True,"evolution":"Strong Chinese, open-weight"},
            {"series":"Baichuan 3","name":"Baichuan 3","params":"13B","training":"2024 web","context":"32K","modalities":"text,code","release":"2024-05","pricing":"Open (MIT)","license":"MIT","free":True,"evolution":"13B dense, open, strong Chinese"},
            {"series":"Baichuan 2","name":"Baichuan 2 13B","params":"13B","training":"2023 web","context":"128K","modalities":"text","release":"2023-12","pricing":"Open (MIT)","license":"MIT","free":True,"evolution":"128K context, first open 128K"},
        ]},
    "面壁智能 (ModelBest)": {"region":"cn","hq":"Beijing, China","year":2022,"desc":"ModelBest, MiniCPM series, small efficient models",
        "products":[
            {"series":"MiniCPM 3.0","name":"MiniCPM 3.0","params":"undisclosed","training":"2024 web","context":"32K","modalities":"text,vision","release":"2025-01","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Efficient multimodal, on-device"},
            {"series":"MiniCPM 2.6","name":"MiniCPM 2.6","params":"8B","training":"2024 web","context":"32K","modalities":"text,vision","release":"2024-08","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"8B multimodal, on-device"},
            {"series":"MiniCPM 2.5","name":"MiniCPM 2.5","params":"8B","training":"2024 web","context":"32K","modalities":"text","release":"2024-05","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"8B dense, efficient inference"},
        ]},
    "智源研究院 (BAAI)": {"region":"cn","hq":"Beijing, China","year":2018,"desc":"Beijing Academy of AI, SenseNova (商量) Chinese open-source models",
        "products":[
            {"series":"SenseNova 1.0","name":"SenseNova 1.0","params":"undisclosed (100B)","training":"2023 web","context":"32K","modalities":"text","release":"2023-10","pricing":"Open (Apache 2.0)","license":"Apache 2.0","free":True,"evolution":"Chinese open-source, 100B"},
        ]},
    "DeepSeek": {"region":"cn","hq":"Hangzhou, China","year":2023,"desc":"Chinese AI startup, open-weight reasoning models, cost-efficient",
        "products":[
            {"series":"DeepSeek-V3.1","name":"DeepSeek-V3.1","params":"685B (37B active, MoE)","training":"2024+ web, synthetic","context":"128K","modalities":"text,code","release":"2025-03","pricing":"$0.28/$0.70 per 1M tok","license":"MIT","free":True,"evolution":"Hybrid thinking, 128K context"},
            {"series":"DeepSeek-R1-0528","name":"DeepSeek-R1-0528","params":"685B (37B active, MoE)","training":"RL + CoT, synthetic","context":"128K","modalities":"text,code","release":"2025-05","pricing":"$0.55/$2.19 per 1M tok","license":"MIT","free":True,"evolution":"Distilled reasoning, open-weight"},
            {"series":"DeepSeek-R1","name":"DeepSeek-R1","params":"685B (37B active, MoE)","training":"RL + synthetic reasoning","context":"128K","modalities":"text,code","release":"2025-01","pricing":"$0.55/$2.19 per 1M tok","license":"MIT","free":True,"evolution":"Open reasoning model, 27B/70B distillations"},
            {"series":"DeepSeek-V3","name":"DeepSeek-V3","params":"685B (37B active, MoE)","training":"2023+ web, 14.8T tokens","context":"128K","modalities":"text,code","release":"2024-12","pricing":"$0.28/$0.70 per 1M tok","license":"MIT","free":True,"evolution":"First cost-efficient MoE, 14.8T tokens"},
            {"series":"DeepSeek-MoE","name":"DeepSeek-MoE","params":"16B (2B active, MoE)","training":"2024 web","context":"8K","modalities":"text","release":"2024-01","pricing":"Open (MIT)","license":"MIT","free":True,"evolution":"Early MoE experiment"},
        ]},
}

# ═══════════════════════════════════════════════════════════════════
# AGENT PLATFORMS REGISTRY — 国际+国内 AI Agent 平台调研
# ═══════════════════════════════════════════════════════════════════

AGENT_PLATFORMS = {
    # ═══════════════════════════════════════════════════════════════
    # INTERNATIONAL AGENT PLATFORMS
    # ═══════════════════════════════════════════════════════════════
    "OpenAI": {"region":"int","product":"GPTs / Assistants API","category":"通用Agent","pricing":"按Token计费","free":"有","models":"GPT-4o, o3, o4-mini","features":"Tool use, Memory, Search, Vision, Code Interpreter, Custom GPTs","api":"Assistants API v2, Responses API","docs":"https://platform.openai.com/docs/assistants","ecosystem":"GPT Store, 200+ 官方 Tools","enterprise":"ChatGPT Enterprise, API tiered pricing","limitations":"无原生多Agent协作, 需自建编排"},
    "Anthropic": {"region":"int","product":"Claude API","category":"通用Agent","pricing":"按Token计费","free":"有","models":"Claude Opus 4, Sonnet 4, Haiku 4.5","features":"Tool use, Computer Use, Vision, Code Execution, Web Search, MCP","api":"Claude API, MCP (Model Context Protocol)","docs":"https://docs.anthropic.com/","ecosystem":"MCP 生态, 200+ 工具集成","enterprise":"Claude Team/Enterprise, Amazon Bedrock","limitations":"无原生多Agent编排, 需外部框架"},
    "Google DeepMind": {"region":"int","product":"Gemini API / Vertex AI","category":"通用Agent","pricing":"按Token计费","free":"有","models":"Gemini 2.5 Pro/Flash, Veo, Imagen","features":"Tool use, Vision, Audio, Video, Code Execution, Google Workspace 集成","api":"Gemini API, Vertex AI Agent Builder","docs":"https://ai.google.dev/","ecosystem":"Google Workspace, AppSheet, 200+ 预构建Agent","enterprise":"Vertex AI Enterprise, Google Cloud","limitations":"Google 生态绑定, 无开源模型"},
    "Microsoft": {"region":"int","product":"Azure AI Foundry / Copilot","category":"企业Agent平台","pricing":"按Token计费","free":"有(有限)","models":"GPT-4o, GPT-4.1, o3, o4-mini, Claude, Mistral","features":"MaaS 多模型, Copilot Studio, Agent Builder, RAG, GraphRAG, Azure OpenAI","api":"Azure OpenAI Service, Foundry Agent Service","docs":"https://learn.microsoft.com/azure/ai-services/","ecosystem":"Copilot Studio, Power Platform, 300+ 连接器","enterprise":"Azure Enterprise, Microsoft 365 Copilot","limitations":"Azure 绑定, 复杂度高"},
    "Amazon": {"region":"int","product":"Bedrock Agents","category":"企业Agent平台","pricing":"按Token计费","free":"有(有限)","models":"Claude, Llama, Titan, Nova, 第三方","features":"Agent 编排, Multi-Agent, Knowledge Bases, Guardrails, Actions","api":"Bedrock API, Agents SDK","docs":"https://docs.aws.amazon.com/bedrock/","ecosystem":"AWS 生态, Lambda, Step Functions, QuickSight","enterprise":"AWS Enterprise, 合规认证","limitations":"AWS 绑定, 模型选择有限"},
    "Cohere": {"region":"int","product":"Command / RAG / North","category":"企业RAG平台","pricing":"按Token计费","free":"有","models":"Command R+, Command R, North, Embed v4","features":"RAG 原生, 文档理解, 企业知识库, Multi-lingual, Citations","api":"Cohere API, Command RAG","docs":"https://docs.cohere.com/","ecosystem":"Cohere Connectors, 100+ 数据源","enterprise":"Cohere Enterprise, 私有部署","limitations":"无原生Agent工具, 需配合编排框架"},
    "Databricks": {"region":"int","product":"Mosaic AI / Agent Framework","category":"企业Agent平台","pricing":"按Token+计算计费","free":"有(有限)","models":"Dbrx, 第三方 (GPT, Claude, Llama)","features":"数据+Agent 一体化, Vector Search, Agent Framework, Model Serving, MLOps","api":"Databricks API, Mosaic AI","docs":"https://docs.databricks.com/ai/","ecosystem":"Delta Lake, Lakehouse, Genie Code","enterprise":"Databricks Enterprise, 私有云","limitations":"数据平台绑定, 学习曲线陡"},
    "LangChain": {"region":"int","product":"LangChain / LangGraph","category":"Agent框架","pricing":"开源免费/云收费","free":"有","models":"支持 500+ LLM","features":"Agent 编排, Tool use, Memory, Chains, LangGraph 状态图, LangSmith 监控","api":"LangChain Python/JS SDK","docs":"https://docs.langchain.com/","ecosystem":"LangSmith, 500+ 集成, LangGraph Platform","enterprise":"LangSmith Cloud, LangGraph Platform","limitations":"抽象层复杂, 调试困难"},
    "LlamaIndex": {"region":"int","product":"LlamaIndex / Workflows","category":"RAG框架","pricing":"免费/云收费","free":"有","models":"支持所有 LLM","features":"RAG 专家, Vector stores, Agents, Workflows 编排, LlamaParse 文档解析","api":"LlamaIndex Python/TS SDK","docs":"https://docs.llamaindex.ai/","ecosystem":"300+ 集成, LlamaCloud, Parse","enterprise":"LlamaCloud 企业版","limitations":"Agent 能力弱于 LangChain"},
    "CrewAI": {"region":"int","product":"CrewAI","category":"多Agent框架","pricing":"开源免费/云收费","free":"有","models":"支持所有 LLM","features":"多Agent 协作, 角色定义, 任务编排, 记忆, 工具集成, YAML 配置","api":"CrewAI Python SDK","docs":"https://docs.crewai.com/","ecosystem":"CrewAI Studio, 200+ 工具","enterprise":"CrewAI+ 企业版","limitations":"生产环境稳定性待验证"},
    "n8n": {"region":"int","product":"n8n","category":"工作流Agent","pricing":"自托管免费/云收费","free":"有","models":"支持所有 LLM","features":"可视化工作流, AI Agent 节点, 500+ 连接器, 自托管, 社区模板","api":"n8n API, HTTP Trigger","docs":"https://docs.n8n.io/","ecosystem":"社区 10万+ 模板, 500+ 集成","enterprise":"n8n Cloud Enterprise","limitations":"非原生Agent, 需学习工作流"},
    "Dify": {"region":"int","product":"Dify.AI","category":"LLM应用平台","pricing":"开源免费/云收费","free":"有","models":"支持所有 LLM","features":"可视化 LLMOps, RAG, Agent, Workflow, 插件市场, 多语言","api":"Dify API, WebSDK","docs":"https://docs.dify.ai/","ecosystem":"开源社区, 插件市场","enterprise":"Dify Cloud 企业版","limitations":"Agent 深度不足"},
    "Replit": {"region":"int","product":"Replit Agent","category":"编程Agent","pricing":"订阅制","free":"有(有限)","models":"GPT-4o, Claude","features":"代码Agent, 全栈部署, 数据库, 域名, 团队协","api":"Replit API","docs":"https://docs.replit.com/","ecosystem":"Replit 社区, 模板市场","enterprise":"Replit Business","limitations":"仅限编程场景"},
    "Cursor": {"region":"int","product":"Cursor","category":"编程Agent","pricing":"订阅制","free":"有(有限)","models":"Claude, GPT-4o, Gemini","features":"代码Agent, 多文件编辑, 语义搜索, 终端集成, MCP","api":"本地 CLI","docs":"https://docs.cursor.com/","ecosystem":"MCP 生态, 社区规则集","enterprise":"Cursor Business","limitations":"仅限编程场景"},
    "Windsurf": {"region":"int","product":"Windsurf","category":"编程Agent","pricing":"订阅制","free":"有(有限)","models":"Claude, GPT-4o, 自有 Superagent","features":"代码Agent, Cascade, Memory, 多文件, MCP","api":"本地 CLI","docs":"https://docs.windsurf.com/","ecosystem":"MCP 生态","enterprise":"Windsurf Business","limitations":"仅限编程场景"},
    "Hugging Face": {"region":"int","product":"Hugging Face / Transformers","category":"开源Agent生态","pricing":"免费/付费API","free":"有","models":"Llama, Mistral, Qwen, 自训练","features":"开源模型库, Transformers, 推理端点, Spaces, AutoTrain, SmolAgents","api":"HF API, Transformers SDK","docs":"https://huggingface.co/docs/","ecosystem":"模型库, 社区, Spaces","enterprise":"HF Enterprise, 私有部署","limitations":"无原生Agent平台"},

    # ═══════════════════════════════════════════════════════════════
    # CHINESE AGENT PLATFORMS
    # ═══════════════════════════════════════════════════════════════
    "字节跳动 (ByteDance)": {"region":"cn","product":"扣子 Coze / 豆包","category":"通用Agent","pricing":"按Token计费","free":"有","models":"豆包 1.5 Pro, Doubao-1.6","features":"Coze 平台, 多Agent 编排, 插件市场, 知识库, 工作流, MCP","api":"豆包 API, Coze API","docs":"https://www.coze.cn/","ecosystem":"扣子社区, 10000+ 插件, 豆包App","enterprise":"豆包企业版","limitations":"字节生态绑定"},
    "阿里巴巴 (Alibaba)": {"region":"cn","product":"通义千问 / 百炼","category":"企业Agent平台","pricing":"按Token计费","free":"有","models":"Qwen3-235B, Qwen2.5-72B, Qwen-VL","features":"百炼平台, Agent 编排, 工作流, 插件, RAG, 多模态","api":"百炼 API, DashScope","docs":"https://help.aliyun.com/zh/dashscope/","ecosystem":"阿里云, 钉钉, 通义千问App","enterprise":"阿里云企业版","limitations":"阿里云绑定"},
    "百度 (Baidu)": {"region":"cn","product":"文心一言 / 千帆","category":"企业Agent平台","pricing":"按Token计费","free":"有","models":"文心 5.0, ERNIE 4.5, 文心一格","features":"千帆平台, Agent 编排, 工作流, 插件市场, RAG, 多模态","api":"千帆 API, 文心一言 API","docs":"https://cloud.baidu.com/doc/WENXINWORKSHOP/","ecosystem":"百度云, 百度智能云, 文心一言App","enterprise":"百度智能云企业版","limitations":"百度生态绑定"},
    "商汤科技 (SenseTime)": {"region":"cn","product":"商量 / 日日新","category":"通用Agent","pricing":"按Token计费","free":"有","models":"SenseNova 6.5 Flash, 日日新多模态","features":"商量 Agent, 多模态, 工作流, 插件, RAG","api":"SenseNova API","docs":"https://www.sensetime.com/","ecosystem":"商汤生态, 大装置","enterprise":"商汤企业版","limitations":"商汤生态绑定"},
    "DeepSeek": {"region":"cn","product":"DeepSeek API","category":"通用Agent","pricing":"按Token计费","free":"有","models":"DeepSeek-V3, DeepSeek-V2.5","features":"API 工具调用, 多模态, 开源权重, 低价格","api":"DeepSeek API","docs":"https://api-docs.deepseek.com/","ecosystem":"开源社区, Hugging Face","enterprise":"无(仅API)","limitations":"无原生Agent平台, 需自建"},
    "智谱AI (Zhipu AI)": {"region":"cn","product":"智谱清言 / AutoGLM","category":"通用Agent","pricing":"按Token计费","free":"有","models":"GLM-4.5, GLM-4.6, AutoGLM","features":"智谱 Agent, 多模态, AutoGLM 计算机操作, 工作流","api":"智谱 API","docs":"https://open.bigmodel.cn/","ecosystem":"智谱清言App, 开源 GLM","enterprise":"智谱企业版","limitations":"生态较小"},
    "月之暗面 (Moonshot AI)": {"region":"cn","product":"Kimi","category":"通用Agent","pricing":"按Token计费","free":"有","models":"Moonshot v2, Moonshot-v1.5","features":"超长上下文, 知识库, 多模态, Agent 工具","api":"Moonshot API","docs":"https://platform.moonshot.cn/","ecosystem":"Kimi App, 开源 Kimi-K2","enterprise":"Moonshot 企业版","limitations":"平台功能有限"},
    "百川智能 (Baichuan)": {"region":"cn","product":"百川 AI","category":"通用Agent","pricing":"按Token计费","free":"有","models":"Baichuan-M2, Baichuan-2","features":"API 工具调用, 多模态, 开源权重, 医疗 Agent","api":"百川 API","docs":"https://platform.baichuan-ai.com/","ecosystem":"开源社区, Hugging Face","enterprise":"百川企业版","limitations":"平台功能有限"},
    "面壁智能 (ModelBest)": {"region":"cn","product":"端侧 Agent","category":"端侧Agent","pricing":"开源免费","free":"有","models":"MiniCPM 3/4","features":"端侧部署, 多模态, 小模型, 边缘计算","api":"MiniCPM SDK","docs":"https://modelbest.github.io/","ecosystem":"开源社区, Hugging Face","enterprise":"无(仅开源)","limitations":"仅限端侧场景"},
    "华为云": {"region":"cn","product":"华为云盘古","category":"企业Agent平台","pricing":"按Token计费","free":"有限","models":"盘古大模型","features":"盘古 Agent, 行业模型, 私有化部署","api":"华为云 API","docs":"https://www.huaweicloud.com/product/pangu.html","ecosystem":"华为云, 鸿蒙生态","enterprise":"华为云企业版","limitations":"华为生态绑定"},
    "Dify (中国版)": {"region":"cn","product":"Dify.AI","category":"LLM应用平台","pricing":"开源免费/云收费","free":"有","models":"支持所有 LLM","features":"可视化 LLMOps, RAG, Agent, Workflow, 插件市场","api":"Dify API","docs":"https://dify.ai/","ecosystem":"开源社区, 插件市场","enterprise":"Dify Cloud 企业版","limitations":"Agent 深度不足"},
    "百度文心": {"region":"cn","product":"文心一言","category":"通用Agent","pricing":"按Token计费","free":"有","models":"文心 5.0, 文心一言","features":"对话, 创作, 代码, Agent, 多模态","api":"文心一言 API","docs":"https://yiyan.baidu.com/","ecosystem":"文心一言 App, 百度智能云","enterprise":"百度智能云企业版","limitations":"百度生态绑定"},
    "商汤日日新": {"region":"cn","product":"日日新商量","category":"通用Agent","pricing":"按Token计费","free":"有","models":"SenseNova 6.5, 日日新","features":"多模态, 对话, 创作, Agent, 工作流","api":"SenseNova API","docs":"https://www.sensetime.com/","ecosystem":"商汤生态, 大装置","enterprise":"商汤企业版","limitations":"商汤生态绑定"},
}

# ── render HTML ──────────────────────────────────────────────────
def render_html(cache):
    """渲染现代化前端页面 (资讯聚合 + 模型/Agent 能力大盘)。
    具体实现在同目录的 ai_intel_render 模块中, 此处只做数据注入。"""
    from ai_intel_render import render_page
    return render_page(cache, MODEL_REGISTRY, AGENT_PLATFORMS,
                       max_entries=MAX_ENTRIES, max_days=MAX_DAYS)

def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

# ── main ─────────────────────────────────────────────────────────
def migrate_cache(cache):
    """一次性迁移旧缓存: 清洗 summary/body 里的 HTML 残片, 补全缺失的 url 字段。
    旧的抓取代码把 RSS description 的原始 HTML 直接截了 200 字符, 导致卡片正文
    全是 <section>/<div> 标签残片; 且根本没存 url, 卡片无法点开原文。"""
    fixed_summary = 0
    fixed_url = 0
    for key in ("entries_int", "entries_cn"):
        for e in cache.get(key, []):
            # 1) 先补全 url: 从 summary/body 的原始 HTML 提取 href (必须在清洗前!)
            if not e.get("url"):
                url = None
                for field in ("summary", "body"):
                    m = re.search(r'href=["\'](https?://[^"\']+)', e.get(field, "") or "")
                    if m:
                        url = html_mod.unescape(m.group(1))
                        break
                if not url:
                    # Google News RSS 跳转链接 (用户点开仍可访问)
                    for field in ("summary", "body"):
                        m = re.search(r'(https://news\.google\.com/rss/articles/[A-Za-z0-9_%=&?+-]+)', e.get(field, "") or "")
                        if m:
                            url = html_mod.unescape(m.group(1))
                            break
                if url:
                    e["url"] = url
                    fixed_url += 1
            # 2) 清洗 summary
            raw = e.get("summary", "") or ""
            if re.search(r'<[a-zA-Z][^>]*>', raw):  # 含 HTML 标签
                clean = clean_html_to_text(raw)
                # 清洗后为空(RSS description 整段是 <section> 标签无文本)时用 title 兜底
                if not clean.strip():
                    clean = e.get("title", "") or ""
                if clean != raw:
                    e["summary"] = clean.strip()
                    fixed_summary += 1
            # 3) 清洗 body
            rawb = e.get("body", "") or ""
            if re.search(r'<[a-zA-Z][^>]*>', rawb):
                cleanb = clean_html_to_text(rawb)
                if cleanb:
                    e["body"] = cleanb
            # 4) summary 为空时, 从 title 兜底
            title = (e.get("title") or "").strip()
            if not (e.get("summary") or "").strip():
                e["summary"] = title
            if e.get("title") != title:
                e["title"] = title
    return fixed_summary, fixed_url

def seed_sources_history(cache):
    """兜底迁移: 旧缓存从未记录过数据源历史时, 从已有条目的 sources 字段反推一次状态。
    这样即使跳过本次抓取(need_refresh=False), 数据源活跃度面板也能显示真实统计。
    语义: 缓存里有条目 -> 该源曾成功(活跃); 无条目 -> 待同步(不标记为失败, 可能是本轮未抓取)。"""
    hist = cache.setdefault("meta", {}).setdefault("sources_history", {})
    if hist:
        return False   # 已有历史记录, 不再覆盖
    region_map = {n: r for n, r, _, _ in SOURCE_REGISTRY}
    now_iso = datetime.now().isoformat()
    counts = {}
    for region_key in ("entries_int", "entries_cn"):
        for e in cache.get(region_key, []):
            for s in e.get("sources", []):
                counts[s] = counts.get(s, 0) + 1
    for name, region in region_map.items():
        n = counts.get(name, 0)
        hist.setdefault(name, []).append(
            {"t": now_iso, "n": n, "ok": n > 0, "err": ""})
    return True

if __name__ == "__main__":
    cache = load_cache()
    now = datetime.now()
    print(f"[AI Intel] Loaded: 国际{len(cache.get('entries_int', []))}条 | 国内{len(cache.get('entries_cn', []))}条")

    # 迁移旧缓存 (幂等: 已清洗的不会再动)
    n_sum, n_url = migrate_cache(cache)
    if n_sum or n_url:
        print(f"[AI Intel] 迁移: 清洗 {n_sum} 条摘要 HTML 残片, 补全 {n_url} 条原文链接")
    
    last_update = cache['meta'].get('updated')
    need_refresh = False
    if not last_update:
        need_refresh = True
    else:
        last_dt = datetime.fromisoformat(last_update.replace("Z", "+00:00"))
        hours_diff = (now - last_dt).total_seconds() / 3600
        if hours_diff >= 1:
            need_refresh = True
    
    new_int, new_cn = [], []
    
    if need_refresh:
        print("[AI Intel] Fetching latest AI news from %d sources (并发)..." % len(SOURCE_REGISTRY))
        by_region, errors = fetch_all_sources()
        for name in sorted(errors):
            st = errors[name]
            if st['err']:
                print(f"  [WARN] {name}: {st['err']}")
            else:
                print(f"  {name}: {st['n']} items")

        # ── 并发预抓正文: 处理循环里 build_summary/build_body 命中缓存, 避免串行 12s×N ──
        import concurrent.futures
        all_items = by_region.get("int", []) + by_region.get("cn", [])
        art_urls = list(dict.fromkeys(
            (it.get('url') or '') for it in all_items
            if (it.get('url') or '').startswith('http') and 'news.google.com' not in (it.get('url') or '')
        ))
        # 时间预算: 留给正文抓取 420s(7分钟), 抓够即停(剩余的由 RSS 描述兜底)
        # 334 个URL / 24 线程 / 12s超时 ≈ 需 180s 全并发, 留足余量避免尾部 URL 饿死
        _FETCH_DEADLINE = time.time() + 420
        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(max_workers=24) as ex:
            futs = [ex.submit(extract_article_body, u) for u in art_urls]
            done = 0
            for fut in concurrent.futures.as_completed(futs):
                try:
                    s, b = fut.result()
                    if s:
                        done += 1
                except Exception:
                    pass
        print(f"[AI Intel] 正文预抓: {len(art_urls)} 个URL, 成功 {done} 篇, 用时 {time.time()-t0:.0f}s")

        # ── 第二轮串行重试: 第一轮并发失败的 URL 逐个重试 ──
        failed = [u for u in art_urls if not (_ARTICLE_CACHE.get(u) or ('', ''))[0]]
        if failed:
            print(f"[AI Intel] 重试 {len(failed)} 个失败URL (20s超时+0.5s间隔)...")
            for i, u in enumerate(failed):
                _ARTICLE_CACHE.pop(u, None)
                if not _fetch_allowed():
                    print(f"[AI Intel] 重试中止: 时间预算耗尽 (已重试{i}/{len(failed)})")
                    break
                time.sleep(0.5)
                try:
                    s, b = extract_article_body(u, retries=0, timeout=20)
                    if i < 5 or not s:  # 前5个+失败的都打印
                        print(f"  重试{i+1}: {len(s)}字 {u[:50]}")
                except Exception as ex:
                    if i < 5:
                        print(f"  重试{i+1}: 异常 {str(ex)[:40]} {u[:50]}")
            retried = sum(1 for u in failed if (_ARTICLE_CACHE.get(u) or ('', ''))[0])
            print(f"[AI Intel] 重试成功 {retried}/{len(failed)}")

        # ── 第三轮: 摘要太短的 URL 重新抓取 (国内站点常返回短 meta 描述) ──
        short_items = [it for it in all_items
                       if len(build_summary(it.get('title',''), it)) < 50
                       and (it.get('url') or '').startswith('http')
                       and 'news.google.com' not in (it.get('url') or '')]
        short_urls = list(dict.fromkeys(it['url'] for it in short_items))
        if short_urls:
            print(f"[AI Intel] 补抓 {len(short_urls)} 个短摘要URL...")
            for u in short_urls:
                _ARTICLE_CACHE.pop(u, None)
                if not _fetch_allowed():
                    break
                time.sleep(0.5)
                try:
                    s, b = extract_article_body(u, retries=0, timeout=20)
                    if s:
                        print(f"  补抓: {len(s)}字 {u[:50]}")
                except Exception:
                    pass
            short_ok = sum(1 for u in short_urls if len((_ARTICLE_CACHE.get(u) or ('',''))[0]) >= 50)
            print(f"[AI Intel] 补抓成功 {short_ok}/{len(short_urls)}")

        _FETCH_DEADLINE = 0
        # 热度: 论文/官方/大厂发布略高, 其余统一 2; 多源重复出现时累加
        for region_key, region, new_list in (("entries_int", "int", new_int), ("entries_cn", "cn", new_cn)):
            for item in by_region.get(region, []):
                title = (item.get('title') or '').strip()
                if not title: continue
                item['title'] = title
                name = item.get('source', '')
                # 论文/官方大厂热度更高
                heat = 2
                if 'arXiv' in name or 'Papers' in name: heat = 3
                elif name in ('OpenAI News','Google DeepMind','Hugging Face Blog','The Rundown AI','Import AI','量子位 QbitAI'): heat = 3
                entry = {"title": title, "summary": build_summary(title, item), "body": build_body(title, item),
                         "url": item.get('url', ''), "sources": [name], "heat": heat,
                         "timestamp": now.isoformat(), "date": now.strftime("%Y-%m-%d")}
                if not already_exists(cache.get(region_key, []), title):
                    cache.setdefault(region_key, []).insert(0, entry)
                    new_list.append(title)
                else:
                    bump_heat(cache[region_key], title, name)
    
    cache["entries_int"] = clean_and_trim(cache.get("entries_int", []), now)[0]
    cache["entries_cn"] = clean_and_trim(cache.get("entries_cn", []), now)[0]
    cache["entries_int"] = sort_entries(cache["entries_int"])
    cache["entries_cn"] = sort_entries(cache["entries_cn"])
    cache["meta"]["updated"] = now.isoformat()
    seed_sources_history(cache)   # 旧缓存兜底: 从条目反推数据源状态
    cache_sources(cache)          # 固化数据源活跃度状态（成功/失败/失联 + 历史）
    save_cache(cache)
    
    html = render_html(cache)
    HTML_FILE.write_text(html, encoding="utf-8")
    
    print(f"[AI Intel] 国际: {len(cache['entries_int'])}条 | 国内: {len(cache['entries_cn'])}条")
    print(f"[AI Intel] Updated: {cache['meta']['updated']}")
    print(f"[AI Intel] HTML written to {HTML_FILE}")
    if new_int:
        print(f"[AI Intel] 国际新增 {len(new_int)} 条")
    if new_cn:
        print(f"[AI Intel] 国内新增 {len(new_cn)} 条")
    if not new_int and not new_cn:
        print("[AI Intel] 无新情报（网络可能不可用）")
