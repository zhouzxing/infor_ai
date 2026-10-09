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
    ("Reddit r/LocalLLaMA",     "int", "https://www.reddit.com/r/LocalLLaMA/.rss", []),
    ("Hacker News",              "int", "https://hnrss.org/frontpage", ["ai","llm","gpt","chatgpt","openai","anthropic","claude","gemini","deepmind","deepseek","llama","transformer","machine learning","neural","robot","nvidia","agi"]),
    ("GeekerHub",                "int", "https://www.geekerhub.com/feed", ["ai","llm","openai","anthropic","gpt","claude","deepseek","llama","hugging","agent"]),
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
    ("arXiv CS.AI",        "int", "http://rss.arxiv.org/rss/cs.AI", ["llm","language model","agent","rag","reinforcement learning","reasoning","tool use","planning","neural"]),
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
        report_source_format(name, "atom" if is_atom else "rss2")
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
_LAST_FORMATS = {}        # 数据源名 -> rss2|atom, fetch_rss_feed 运行时探测后登记

def report_fetch_error(name, err):
    """fetch 函数内部捕获到异常时上报，供数据源活跃度面板展示真实失败原因"""
    _LAST_FETCH_ERRORS[name] = str(err)[:150]

def report_source_format(name, fmt):
    """登记该源本轮探测到的真实格式 (rss2/atom), 监控面板据此展示渠道爬取技术。"""
    _LAST_FORMATS[name] = fmt

def cache_sources(cache):
    """把本轮抓取的状态固化进 cache['meta']['sources_status']，供渲染面板使用"""
    hist = cache.setdefault("meta", {}).get("sources_history", {})
    prev_status = cache.get("meta", {}).get("sources_status", {})
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
        streak = 0
        for ev in reversed(h):
            if ev.get("ok"):
                break
            streak += 1
        status[name] = {"region": region, "url": url, "total_items": last_n,
                        "status": cur, "dot": dot, "last_ok": last_ok,
                        "last_ok_t": last_ok_t, "last_err": last_err, "stale": stale,
                        "history_ok": sum(1 for e in h if e.get("ok")),
                        "history_total": len(h),
                        # ── 监控板块: 爬取策略 + 运行时探测格式 + 连败 ──
                        "strategy": _source_strategy(name, url, kws),
                        # skipped 运行时 _LAST_FORMATS 为空, 沿用上一轮探测到的格式, 避免被 unknown 洗掉
                        "fmt": _LAST_FORMATS.get(name) or prev_status.get(name, {}).get("fmt", "unknown"),
                        "streak_fail": streak}
    cache["meta"]["sources_status"] = status
    cache["meta"]["last_source_refresh"] = now_iso
    return status

def _source_strategy(name, url, kws):
    """渠道爬取策略归类 (监控面板展示): RSS 直连 / RSSHub 镜像 / +关键词过滤。
    9 个 fetch_from_* 专用抓取器为 v1 遗留 (主流程未调用), 当前 38 源统一走 fetch_rss_feed。"""
    via_mirror = "rsshub." in (url or "")
    base = "RSSHub 镜像" if via_mirror else "RSS 直连"
    if kws:
        return "%s + 关键词×%d" % (base, len(kws))
    return base + "（垂直频道）" if not via_mirror else base

def append_run_log(cache, n_ok, n_total, n_items, gh_n, gh_err, skipped=False):
    """管线运行日志: 每小时一行, 保留最近 48 条 -> 监控面板趋势用。"""
    log = cache.setdefault("meta", {}).setdefault("run_log", [])
    log.append({"t": datetime.now().isoformat(), "skipped": skipped,
                "src_ok": n_ok, "src_total": n_total, "items_new": n_items,
                "gh_items": gh_n, "gh_err": gh_err})
    del log[:-48]


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

# ── GitHub 最热 AI 项目 (gh CLI, 走代理) ──────────────────────
GITHUB_ORGS = ["openai", "anthropics", "facebook", "google",
               "mistralai", "deepseek-ai", "huggingface", "NousResearch"]
# 多 topic 不能 OR (OR 只能连文本词), 逐个查再合并; 纯 topic 过滤噪音最小
GITHUB_TOPICS = ["llm", "ai", "ai-agents", "genai", "generative-ai"]

def _gh_api(endpoint, timeout=30):
    """调用 gh api 拉取 GitHub REST API; 成功返回 (data, None), 失败 (None, err)。"""
    cmd = ["gh", "api", endpoint]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return None, (p.stderr or "gh failed").strip()[:120]
        return json.loads(p.stdout), None
    except Exception as e:
        return None, str(e)[:120]

def _gh_search(ghq, per_page=30, sort="stars", timeout=30):
    """GitHub 搜索 API; ghq 为原始查询(空格用%20, > 用%3E)。
    sort=None 时用默认相关度排序 (created:/pushed: 等范围过滤禁止 sort:stars)。"""
    ep = "search/repositories?q=%s&per_page=%d" % (ghq, per_page)
    if sort:
        ep += "&sort=%s&order=desc" % sort
    data, err = _gh_api(ep, timeout)
    if err:
        return [], err
    items = data.get("items", []) if isinstance(data, dict) else []
    return items, None

def _gh_is_ai(it):
    """判断仓库是否 AI 相关: name/desc/topics 命中关键词。
    topic:ai 过宽(会命中 n8n 这类集成 AI 的通用工具), 故对纯 topic 榜结果二次筛。"""
    blob = " ".join([
        it.get("full_name") or "", it.get("description") or "",
        " ".join(it.get("topics") or [])]).lower()
    for k in ("llm", "ai", "agent", "gpt", "model", "neural", "open-source model", "ml"):
        if k in blob:
            return True
    return False

def _gh_keep(fn, it, via):
    """保留判定: 来自组织精选/新星的直接保留; 仅来自宽泛 topic 榜的需 AI 相关性命中。"""
    if "org" in via or "rising" in via:
        return True
    return _gh_is_ai(it)

def fetch_github_repos():
    """抓 GitHub 最热 AI 项目: 各 topic 主题榜(社区/组织/个人混排) + 新星 + 组织精选。
    返回 {items, orgs, updated, errors}; items 去重后按 stars 降序。"""
    now = datetime.now()
    items, via, errors = {}, {}, []
    # 1) 主题热榜: 逐 topic 拉 stars 榜, 合并去重 (纯 topic 无噪音, OR 仅文本词可用)
    for tp in GITHUB_TOPICS:
        raw, err = _gh_search("topic:%s%%20stars:%%3E300" % tp, 40)
        if err:
            errors.append("topic:%s: %s" % (tp, err))
            continue
        for it in raw:
            fn = it.get("full_name", "")
            if fn:
                items[fn] = it
                via.setdefault(fn, set()).add("topic")
        time.sleep(1)
    # 2) 新星: 近 4 个月创建的 AI 项目 (created: 范围过滤禁止 sort:stars, 用默认相关度拉回再本地筛 stars)
    raw, err = _gh_search("topic:ai%20created:%3E2026-06-01", 40, sort=None)
    if err:
        errors.append("新星: %s" % err)
    for it in sorted(raw, key=lambda x: -int(x.get("stargazers_count", 0))):
        fn = it.get("full_name", "")
        if not fn:
            continue
        items.setdefault(fn, it)
        via.setdefault(fn, set()).add("rising")
    # 3) 组织精选: 逐个 org 取 stars 最高的 10 个仓库 (google 2869 库, 取 top10 足够)
    for org in GITHUB_ORGS:
        raw, err = _gh_search("user:%s%%20stars:%%3E10" % org, 10)
        if err:
            errors.append("%s: %s" % (org, err))
            continue
        for it in raw:
            fn = it.get("full_name", "")
            if not fn:
                continue
            items.setdefault(fn, it)
            via.setdefault(fn, set()).add("org")
        time.sleep(1)
    # 归一化 + 噪音过滤
    merged = []
    for fn, it in items.items():
        if not fn or not _gh_keep(fn, it, via.get(fn, set())):
            continue
        owner = it.get("owner", {})
        owner_type = owner.get("type") or it.get("owner_type") or "User"
        created = (it.get("created_at") or "")[:10]
        pushed = (it.get("pushed_at") or "")[:10]
        merged.append({
            "name": it.get("name", fn),
            "full_name": fn,
            "owner": owner.get("login", fn.split("/")[0] if "/" in fn else fn),
            "owner_type": "org" if owner_type == "Organization" else "user",
            "lang": it.get("language") or "—",
            "stars": int(it.get("stargazers_count", 0)),
            "forks": int(it.get("forks_count", 0)),
            "created": created,
            "pushed": pushed,
            "rising": bool(created >= "2026-06-01"),
            "org_pick": "org" in via.get(fn, set()),
            "desc": (it.get("description") or "").strip()[:160],
            "url": it.get("html_url", ""),
            "topics": [t for t in (it.get("topics") or [])[:5]],
        })
    merged.sort(key=lambda r: -r["stars"])
    orgs_present = sorted(set(r["owner"] for r in merged if r["owner_type"] == "org"))
    return {"items": merged[:150], "orgs": orgs_present,
            "updated": now.isoformat(), "errors": errors}

def load_github_cache(cache):
    """无则返回 None; 有且 3h 内则返回, 否则 None(需重抓)。"""
    gh = cache.get("github_repos")
    if not gh or not gh.get("items"):
        return None
    try:
        dt = datetime.fromisoformat(gh["updated"])
        if (datetime.now() - dt).total_seconds() > 10800:
            return None
        return gh
    except Exception:
        return None

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

    # ═══════════════════════════════════════════════════════════════
    # OPEN-SOURCE AGENT FRAMEWORKS (国际)
    # ═══════════════════════════════════════════════════════════════
    "Hermes Agent": {"region":"int","product":"Hermes Agent (Nous Research)","category":"开源Agent","pricing":"开源免费","free":"有","models":"支持 Anthropic/OpenAI/Google/OpenRouter 等多家","features":"多工具编排, 浏览器控制, 记忆持久化, 技能系统, Kanban 任务看板, 语音合成, 多 Agent 协作","api":"CLI + API","docs":"https://hermes-agent.nousresearch.com/","ecosystem":"Nous Research, 技能市场, Kanban, Spotify/Himalaya/YouTube 集成","enterprise":"自托管, 可嵌入","limitations":"个人/团队级, 无 SaaS 版"},
    "AutoGen": {"region":"int","product":"AutoGen (Microsoft Research)","category":"开源多Agent框架","pricing":"开源免费","free":"有","models":"支持所有 LLM","features":"多Agent 对话编排, 代码执行, 人机协同, AgentChat API, AutoGen Studio 可视化","api":"AutoGen Python SDK","docs":"https://microsoft.github.io/autogen/","ecosystem":"Microsoft Research, AutoGen Studio","enterprise":"Microsoft 365 Copilot Studio 相关","limitations":"抽象层复杂, 生产运维需自建"},
    "Semantic Kernel": {"region":"int","product":"Semantic Kernel (Microsoft)","category":"开源Agent框架","pricing":"开源免费","free":"有","models":"Azure OpenAI, OpenAI, Anthropic, Mistral, Ollama","features":"Planner, Plugin, Memory, Agent Framework, Function Calling, MCP","api":"Semantic Kernel SDK","docs":"https://github.com/microsoft/semantic-kernel","ecosystem":"Microsoft, .NET/Python/Java 三语","enterprise":"Microsoft 365 Copilot","limitations":"企业向, 上手门槛中等"},
    "Swarm": {"region":"int","product":"Swarm (OpenAI)","category":"开源多Agent框架","pricing":"开源免费","free":"有","models":"OpenAI 系列","features":"Agent 编排, handoff 交接, 轻量, Python","api":"Swarm Python","docs":"https://github.com/openai/swarm","ecosystem":"OpenAI, AgentKit","enterprise":"无","limitations":"实验性质, 生产用建议迁移 Agents SDK"},
    "SmolAgents": {"region":"int","product":"SmolAgents (Hugging Face)","category":"开源Agent框架","pricing":"开源免费","free":"有","models":"HF Hub 全部模型, OpenAI, Anthropic","features":"Code agents, Tool calling, Hub 集成, 简单清晰","api":"SmolAgents Python","docs":"https://github.com/huggingface/smolagents","ecosystem":"Hugging Face Hub, Spaces","enterprise":"HF Enterprise","limitations":"功能较简单"},
    "BabyAGI": {"region":"int","product":"BabyAGI","category":"开源Agent框架","pricing":"开源免费","free":"有","models":"OpenAI, Anthropic","features":"任务循环, 记忆, 工具调用, 早期 agentic 架构","api":"Python SDK","docs":"https://github.com/yoheinakajima/babyagi","ecosystem":"GitHub 社区","enterprise":"无","limitations":"教学向, 生产能力有限"},
    "Agno": {"region":"int","product":"Agno Agents","category":"开源多Agent框架","pricing":"开源免费","free":"有","models":"500+ LLM 支持","features":"多Agent 编排, 内置工具, 记忆, Agno Cloud","api":"Agno Python SDK","docs":"https://docs.agno.com/","ecosystem":"Agno Studio, 100+ 工具","enterprise":"Agno Cloud Enterprise","limitations":"生态尚在成长"},
    "AgentUniverse": {"region":"int","product":"AgentUniverse","category":"开源多Agent框架","pricing":"开源免费","free":"有","models":"所有主流 LLM","features":"Graph 编排, MCP, 知识图谱, 插件市场","api":"AgentUniverse Python","docs":"https://agentuniverse.readthedocs.io/","ecosystem":"Google Cloud, 插件市场","enterprise":"Google Cloud 集成","limitations":"英文社区较小"},
    "Camel-AI": {"region":"int","product":"CAMEL / OWL","category":"开源Agent框架","pricing":"开源免费","free":"有","models":"所有主流 LLM","features":"多Agent 角色扮演, OWL 多Agent 编排, CodeAct","api":"CAMEL / OWL Python SDK","docs":"https://docs.camel-ai.org/","ecosystem":"CAMEL-AI 社区","enterprise":"无","limitations":"学术导向"},

    # ═══════════════════════════════════════════════════════════════
    # CODING AGENTS
    # ═══════════════════════════════════════════════════════════════
    "GitHub Copilot": {"region":"int","product":"GitHub Copilot / Copilot Agent","category":"编程Agent","pricing":"订阅制","free":"有(有限)","models":"GPT-4o, GPT-4.1, Claude, o3, o4-mini","features":"Autocomplete, Chat, Workspace Agent, Code Review Agent, Code Search, Agent HQ 多 Agent","api":"GitHub API, Copilot SDK","docs":"https://docs.github.com/en/copilot","ecosystem":"GitHub, VS Code, JetBrains, Cursor","enterprise":"GitHub Enterprise, GitHub Business","limitations":"GitHub 生态绑定"},
    "Amazon Q Developer": {"region":"int","product":"Amazon Q Developer","category":"编程Agent","pricing":"订阅制","free":"有(有限)","models":"Amazon Nova, Bedrock 模型","features":"代码建议, Agent 模式, Code Transformation, Security","api":"AWS API","docs":"https://aws.amazon.com/q/developer/","ecosystem":"AWS, Bedrock, VS Code/JetBrains","enterprise":"AWS Enterprise","limitations":"AWS 绑定"},
    "Sourcegraph Cody": {"region":"int","product":"Cody / Sourcegraph Agent","category":"编程Agent","pricing":"订阅制","free":"有(有限)","models":"Claude, GPT-4, Gemini","features":"上下文感知, 代码库搜索, Agentic Chat, Autofix","api":"Sourcegraph API","docs":"https://sourcegraph.com/cody","ecosystem":"Sourcegraph, DevEx","enterprise":"Sourcegraph Enterprise","limitations":"Sourcegraph 绑定"},
    "Aider": {"region":"int","product":"Aider","category":"开源编程Agent","pricing":"开源免费","free":"有","models":"所有主流 LLM","features":"多文件编辑, Git 集成, 终端 Agent, Chat 编辑","api":"CLI","docs":"https://aider.chat/","ecosystem":"开源社区","enterprise":"无","limitations":"终端为主, 无 IDE"},
    "Cline": {"region":"int","product":"Cline (原 Claude Dev)","category":"编程Agent","pricing":"开源免费/自托管","free":"有","models":"Claude, GPT-4, Gemini, DeepSeek","features":"VS Code 插件, 多文件编辑, 终端集成, MCP 支持, Plan/Act 模式","api":"VS Code 插件, MCP","docs":"https://cline.bot/","ecosystem":"MCP 生态, VS Code 插件市场","enterprise":"无","limitations":"VS Code 绑定"},
    "Continue": {"region":"int","product":"Continue","category":"开源编程Agent","pricing":"开源免费","free":"有","models":"500+ LLM 支持","features":"VS Code/JetBrains, Agent 模式, 自定义 Rules, 记忆","api":"CLI + IDE 插件","docs":"https://docs.continue.dev/","ecosystem":"Continue.dev 社区","enterprise":"Continue Enterprise","limitations":"生态小"},
    "Pieces": {"region":"int","product":"Pieces by Codeium","category":"编程Agent","pricing":"订阅制","free":"有(有限)","models":"Claude, GPT-4","features":"知识片段, IDE Agent, 团队共享","api":"IDE 插件","docs":"https://pieces.app/","ecosystem":"Codeium, Pieces 社区","enterprise":"Codeium Enterprise","limitations":"闭源"},
    "Roo Code": {"region":"int","product":"Roo Code","category":"编程Agent","pricing":"开源免费","free":"有","models":"Claude, GPT-4, DeepSeek","features":"VS Code 插件, MCP, Plan/Code/Architect 多模式, Terminal","api":"VS Code 插件","docs":"https://docs.roo-code.com/","ecosystem":"MCP 生态","enterprise":"无","limitations":"VS Code 绑定"},

    # ═══════════════════════════════════════════════════════════════
    # AGENT ORCHESTRATION / WORKFLOW
    # ═══════════════════════════════════════════════════════════════
    "Temporal": {"region":"int","product":"Temporal (Agent Workflows)","category":"Agent 编排","pricing":"开源自托管/云收费","free":"有","models":"与 LLM 无关(编排层)","features":"Durable Execution, 状态持久化, 长时间 Agent 任务, Retry/Signal/Query","api":"Temporal SDK","docs":"https://temporal.io/","ecosystem":"Temporal Cloud, 500+ 集成","enterprise":"Temporal Cloud","limitations":"不是 Agent, 是编排基础设施"},
    "Zapier Agents": {"region":"int","product":"Zapier Agents","category":"SaaS Agent 平台","pricing":"订阅制","free":"有(有限)","models":"GPT-4o, Claude","features":"5000+ 应用集成, 可视化 Agent 编排, 工具调用","api":"Zapier API","docs":"https://zapier.com/agents","ecosystem":"Zapier 生态","enterprise":"Zapier Enterprise","limitations":"应用集成向, 复杂逻辑受限"},
    "Make AI": {"region":"int","product":"Make AI Agent","category":"SaaS 工作流 Agent","pricing":"订阅制","free":"有(有限)","models":"GPT-4o, Claude","features":"可视化流程, AI Agent 模块, 7000+ 应用","api":"Make API","docs":"https://www.make.com/en/ai-agents","ecosystem":"Make 生态","enterprise":"Make Enterprise","limitations":"可视化为主, 深度自定义受限"},

    # ═══════════════════════════════════════════════════════════════
    # 国内开源 Agent 平台 (补充)
    # ═══════════════════════════════════════════════════════════════
    "FastGPT": {"region":"cn","product":"FastGPT","category":"LLM应用平台","pricing":"开源免费/云收费","free":"有","models":"支持所有 LLM","features":"知识库, Agent 编排, 工作流, 多轮对话, 提示词","api":"FastGPT API","docs":"https://doc.fastgpt.io/","ecosystem":"FastGPT 开源社区","enterprise":"FastGPT 云版","limitations":"复杂 Agent 能力弱"},
    "MaxKB": {"region":"cn","product":"MaxKB","category":"LLM知识库应用平台","pricing":"开源免费","free":"有","models":"支持所有 LLM","features":"知识库, 应用编排, 多渠道发布, 权限管理","api":"MaxKB API","docs":"https://www.fit2cloud.com/maxkb","ecosystem":"飞致云 Fit2Cloud","enterprise":"飞致云企业版","limitations":"知识管理为主"},
    "LangBot": {"region":"cn","product":"LangBot","category":"开源Agent机器人","pricing":"开源免费","free":"有","models":"支持所有 LLM","features":"飞书/微信/QQ/Telegram 机器人, Agent 工具, 工作流","api":"LangBot API","docs":"https://langbot.app/","ecosystem":"LangBot 社区","enterprise":"无","limitations":"以 IM 机器人场景为主"},

    # ═══════════════════════════════════════════════════════════════
    # 国内厂商 Agent (补充)
    # ═══════════════════════════════════════════════════════════════
    "腾讯元器": {"region":"cn","product":"腾讯元器 / 腾讯云 TI","category":"通用Agent","pricing":"按Token计费","free":"有","models":"Hunyuan, GPT-4o","features":"元器平台, 智能体编排, 插件, 工作流, 腾讯云 TI 集成","api":"腾讯元器 API","docs":"https://yuanqi.tencent.com/","ecosystem":"腾讯云, 微信, 腾讯会议","enterprise":"腾讯云企业版","limitations":"腾讯生态绑定"},
    "科大讯飞星火": {"region":"cn","product":"讯飞星火 / SparkDesk","category":"通用Agent","pricing":"按Token计费","free":"有","models":"星火 X1/X2, 讯飞星火大模型","features":"对话, 创作, 代码, Agent, 多模态, 私有部署","api":"讯飞星火 API","docs":"https://xinghuo.xfyun.cn/","ecosystem":"讯飞开放平台, SparkDesk","enterprise":"讯飞企业版","limitations":"讯飞生态绑定"},
    "小米超级小爱": {"region":"cn","product":"超级小爱","category":"智能设备Agent","pricing":"免费/内置","free":"有","models":"MiLM, 小米自研多模态","features":"智能设备控制, 意图理解, IoT 生态, 语音多模态","api":"小米澎湃OS API","docs":"https://hyperos.mi.com/","ecosystem":"小米澎湃OS, IoT","enterprise":"小米生态","limitations":"小米设备绑定"},
    "荣耀魔法大模型": {"region":"cn","product":"荣耀 YOYO 大模型","category":"智能设备Agent","pricing":"免费/内置","free":"有","models":"MagicLM","features":"意图识别, 多模态, 系统级 Agent, 跨设备协同","api":"荣耀 MagicOS API","docs":"https://www.hihonor.com/","ecosystem":"荣耀 MagicOS, IoT","enterprise":"荣耀生态","limitations":"荣耀设备绑定"},
    "阶跃星辰": {"region":"cn","product":"StepFun API","category":"通用Agent","pricing":"按Token计费","free":"有","models":"Step-2, Step-1V","features":"API 工具调用, 多模态, 开源权重","api":"阶跃星辰 API","docs":"https://platform.stepfun.com/","ecosystem":"阶跃星辰 API","enterprise":"阶跃星辰企业版","limitations":"平台功能有限"},
    "MiniMax": {"region":"cn","product":"MiniMax API","category":"通用Agent","pricing":"按Token计费","free":"有","models":"ABAB, MiniMax-Text-01, Hailuo","features":"API 工具调用, 多模态, 视频生成, 开源权重","api":"MiniMax API","docs":"https://www.minimaxi.com/","ecosystem":"MiniMax 平台, 海螺 AI","enterprise":"MiniMax 企业版","limitations":"平台功能有限"},
}

# ═══════════════════════════════════════════════════════════════════
# MODEL_BENCHMARKS — 模型基准评测 × 训练投入 × 推理性能 (「性能对比」tab 数据源)
# ═══════════════════════════════════════════════════════════════════
# 口径说明 (人工维护, 数值为厂商官方/第三方公开口径四舍五入):
#   bench  — 8 项基准 0-100 分: MMLU-Pro(知识) GPQA(科学推理) AIME25(数学)
#            SWE-V(SWE-bench Verified 编码) LCB(LiveCodeBench) TB-Hard(Terminal-Bench 智能体编码)
#            τ²-bench(工具调用) BrowseComp(浏览器智能体, Agent 工作流口径)
#   tps    — 第三方推理平台实测输出速度 (tok/s, 估算口径)
#   ttft   — 首 token 延迟 (秒, 估算口径)
#   train  — 训练投入: tokens(训练token量) gpu_h(百万 H100/H800 等效 GPU 时)
#            cost_m(估算成本, 百万美元) src(披露|估)
# 键名必须与 MODEL_REGISTRY 里 product 的 "name" 完全一致, 渲染层按 name join。

MODEL_BENCHMARKS = {
    # ── 国际 ────────────────────────────────────────────────────────
    "GPT-5":     {"bench": {"MMLU-Pro":87,"GPQA":86,"AIME25":95,"SWE-V":75,"LCB":81,"TB-Hard":57,"τ²-bench":90,"BrowseComp":58}, "tps":105, "ttft":0.9, "train": None},
    "o3":        {"bench": {"MMLU-Pro":84,"GPQA":83,"AIME25":91,"SWE-V":72,"LCB":77,"TB-Hard":41,"τ²-bench":85,"BrowseComp":52}, "tps":62, "ttft":1.3, "train": None},
    "o4-mini":   {"bench": {"MMLU-Pro":81,"GPQA":81,"AIME25":92,"SWE-V":68,"LCB":74,"TB-Hard":27,"τ²-bench":83,"BrowseComp":46}, "tps":133, "ttft":1.1, "train": None},
    "GPT-4.1":   {"bench": {"MMLU-Pro":78,"GPQA":63,"AIME25":40,"SWE-V":55,"LCB":61,"TB-Hard":26,"τ²-bench":74,"BrowseComp":15}, "tps":118, "ttft":0.8, "train": None},
    "GPT-4o":    {"bench": {"MMLU-Pro":76,"GPQA":54,"AIME25":9,"SWE-V":33,"LCB":37,"TB-Hard":5,"τ²-bench":42,"BrowseComp":2}, "tps":128, "ttft":0.6, "train": None},
    "Claude Opus 4":   {"bench": {"MMLU-Pro":85,"GPQA":78,"AIME25":86,"SWE-V":73,"LCB":61,"TB-Hard":44,"τ²-bench":82,"BrowseComp":15}, "tps":59, "ttft":1.6, "train": None},
    "Claude Sonnet 4": {"bench": {"MMLU-Pro":83,"GPQA":77,"AIME25":87,"SWE-V":73,"LCB":55,"TB-Hard":35,"τ²-bench":85,"BrowseComp":12}, "tps":71, "ttft":1.2, "train": None},
    "Claude Haiku 4":  {"bench": {"MMLU-Pro":80,"GPQA":74,"AIME25":80,"SWE-V":67,"LCB":45,"TB-Hard":22,"τ²-bench":78,"BrowseComp":8}, "tps":211, "ttft":0.9, "train": None},
    "Gemini 2.5 Pro":  {"bench": {"MMLU-Pro":84,"GPQA":86,"AIME25":88,"SWE-V":64,"LCB":80,"TB-Hard":49,"τ²-bench":89,"BrowseComp":26}, "tps":90, "ttft":1.5, "train": None},
    "Gemini 2.5 Flash":{"bench": {"MMLU-Pro":80,"GPQA":78,"AIME25":75,"SWE-V":60,"LCB":73,"TB-Hard":26,"τ²-bench":79,"BrowseComp":18}, "tps":256, "ttft":0.8, "train": None},
    "Grok 3":          {"bench": {"MMLU-Pro":81,"GPQA":78,"AIME25":93,"SWE-V":68,"LCB":60,"TB-Hard":30,"τ²-bench":70,"BrowseComp":21}, "tps":87, "ttft":1.2,
                       "train": {"tokens":"未披露","gpu_h":300,"cost_m":350,"src":"估"}},
    "Llama 4 Maverick":{"bench": {"MMLU-Pro":81,"GPQA":69,"AIME25":52,"SWE-V":43,"LCB":43,"TB-Hard":12,"τ²-bench":59,"BrowseComp":6}, "tps":148, "ttft":0.7,
                       "train": {"tokens":"22T+ (估)","gpu_h":20,"cost_m":60,"src":"估"}},
    "Llama 3.3 70B":   {"bench": {"MMLU-Pro":76,"GPQA":65,"AIME25":36,"SWE-V":38,"LCB":37,"TB-Hard":8,"τ²-bench":47,"BrowseComp":2}, "tps":137, "ttft":1.0,
                       "train": {"tokens":"15T+","gpu_h":7.3,"cost_m":15,"src":"估"}},
    "Mistral Large 2": {"bench": {"MMLU-Pro":78,"GPQA":60,"AIME25":30,"SWE-V":37,"LCB":40,"TB-Hard":10,"τ²-bench":50,"BrowseComp":2}, "tps":102, "ttft":1.1, "train": None},
    "Phi-4":           {"bench": {"MMLU-Pro":78,"GPQA":56,"AIME25":34,"SWE-V":30,"LCB":33,"TB-Hard":6,"τ²-bench":38,"BrowseComp":1}, "tps":190, "ttft":0.8,
                       "train": {"tokens":"9.8T 合成","gpu_h":0.35,"cost_m":0.7,"src":"估"}},
    # ── 国内 ────────────────────────────────────────────────────────
    "DeepSeek-V3.1":    {"bench": {"MMLU-Pro":81,"GPQA":80,"AIME25":83,"SWE-V":68,"LCB":67,"TB-Hard":36,"τ²-bench":79,"BrowseComp":22}, "tps":105, "ttft":1.4,
                        "train": {"tokens":"18T (估)","gpu_h":4.5,"cost_m":9,"src":"估"}},
    "DeepSeek-R1-0528": {"bench": {"MMLU-Pro":81,"GPQA":76,"AIME25":88,"SWE-V":58,"LCB":65,"TB-Hard":32,"τ²-bench":73,"BrowseComp":19}, "tps":38, "ttft":2.1,
                        "train": {"tokens":"RL 后训练","gpu_h":0.6,"cost_m":1.2,"src":"估"}},
    "DeepSeek-R1":      {"bench": {"MMLU-Pro":79,"GPQA":72,"AIME25":79,"SWE-V":49,"LCB":59,"TB-Hard":25,"τ²-bench":62,"BrowseComp":13}, "tps":27, "ttft":2.4,
                        "train": {"tokens":"RL 后训练","gpu_h":0.15,"cost_m":0.3,"src":"估"}},
    "DeepSeek-V3":      {"bench": {"MMLU-Pro":79,"GPQA":71,"AIME25":40,"SWE-V":42,"LCB":44,"TB-Hard":15,"τ²-bench":56,"BrowseComp":4}, "tps":63, "ttft":1.7,
                        "train": {"tokens":"14.8T","gpu_h":2.79,"cost_m":5.6,"src":"披露"}},
    "Kimi K2":          {"bench": {"MMLU-Pro":80,"GPQA":75,"AIME25":75,"SWE-V":66,"LCB":66,"TB-Hard":30,"τ²-bench":75,"BrowseComp":48}, "tps":41, "ttft":1.8,
                        "train": {"tokens":"15.5T","gpu_h":4.9,"cost_m":10,"src":"估"}},
    "GLM-4.5":          {"bench": {"MMLU-Pro":79,"GPQA":75,"AIME25":72,"SWE-V":64,"LCB":60,"TB-Hard":28,"τ²-bench":84,"BrowseComp":17}, "tps":55, "ttft":1.6,
                        "train": {"tokens":"22T","gpu_h":5.0,"cost_m":10,"src":"估"}},
    "Qwen2.5 72B":      {"bench": {"MMLU-Pro":78,"GPQA":68,"AIME25":43,"SWE-V":43,"LCB":41,"TB-Hard":14,"τ²-bench":55,"BrowseComp":5}, "tps":96, "ttft":1.5,
                        "train": {"tokens":"18T","gpu_h":3.0,"cost_m":6,"src":"估"}},
    "Qwen2.5-Coder 72B":{"bench": {"SWE-V":42,"LCB":60}, "tps":88, "ttft":1.4,
                        "train": {"tokens":"5.5T (估)","gpu_h":1.5,"cost_m":3,"src":"估"}},
}

# ═══════════════════════════════════════════════════════════════════
# AGENT_CAPS — Agent 平台能力数据大盘 (「Agent 对比」tab 数据源)
# ═══════════════════════════════════════════════════════════════════
# 八维能力为编辑基于公开文档/功能对比的估算 (0-100, 100=最强):
#   orch 编排 | tools 工具调用 | auto 自主性 | mem 记忆 | mm 多模态
#   collab 多Agent协作 | eco 生态集成 | cost 成本友好(100=最便宜)
# bench: 代表性公开基准结果 (搭载旗舰模型+官方工作流口径, 估算/混合口径)
# hl: 最大特色 | diff: 关键区别 | power: 代表模型
# 键名必须与 AGENT_PLATFORMS 的键完全一致, 渲染层按键 join。

AGENT_CAPS = {
    "OpenAI":     {"dims": {"orch":85,"tools":95,"auto":80,"mem":72,"mm":90,"collab":65,"eco":96,"cost":45},
                   "bench": {"SWE-V":74.9,"τ²-bench":90.1,"BrowseComp":58.2,"GAIA":74.3},
                   "hl": "Responses API 内置搜索/代码/视觉/文件工具链, 开箱即用工具生态最全",
                   "diff": "工具与生态最全; 多Agent 编排与跨会话记忆需自建, 无开源自托管",
                   "power": "GPT-5 / o3 / o4-mini"},
    "Anthropic":  {"dims": {"orch":82,"tools":92,"auto":85,"mem":70,"mm":78,"collab":60,"eco":88,"cost":50},
                   "bench": {"SWE-V":72.5,"OSWorld":46.8,"τ²-bench":84.9},
                   "hl": "Computer Use 真实电脑操作 + MCP 协议开创者",
                   "diff": "长任务/编程 Agent 最强, MCP 已成行业标准; 原生多Agent 编排缺位",
                   "power": "Claude Opus 4 / Sonnet 4"},
    "Google DeepMind": {"dims": {"orch":84,"tools":90,"auto":78,"mem":75,"mm":96,"collab":70,"eco":92,"cost":62},
                   "bench": {"τ²-bench":89.2,"BrowseComp":56.7,"GAIA":59.8},
                   "hl": "1M 上下文 + 原生全模态 + Deep Research 深度研究",
                   "diff": "多模态与长上下文最强; 深度绑定 Google Cloud / Workspace 生态",
                   "power": "Gemini 2.5 Pro / Flash"},
    "Microsoft":  {"dims": {"orch":88,"tools":85,"auto":70,"mem":75,"mm":82,"collab":85,"eco":94,"cost":55},
                   "hl": "Copilot Studio 无代码 + 300 连接器的企业级 Agent 工厂",
                   "diff": "企业合规与 Office 生态最深的低代码方案; 绑定 Azure, 灵活性低",
                   "power": "GPT-4o / o3 / 多模型路由"},
    "Amazon":     {"dims": {"orch":86,"tools":82,"auto":68,"mem":70,"mm":72,"collab":82,"eco":90,"cost":58},
                   "hl": "Bedrock 多模型路由 + Multi-Agent 协作 + Knowledge Bases 一体化",
                   "diff": "AWS 云原生集成最深; 模型选择与调试体验一般",
                   "power": "Claude / Nova / Llama"},
    "LangChain":  {"dims": {"orch":95,"tools":88,"auto":75,"mem":80,"mm":60,"collab":92,"eco":85,"cost":88},
                   "hl": "LangGraph 状态图编排, 代码级 Agent 开发事实标准",
                   "diff": "灵活性与集成数最高 (500+); 抽象层复杂, 调试与生产运维成本高",
                   "power": "任意 LLM (500+)"},
    "LlamaIndex": {"dims": {"orch":72,"tools":75,"auto":60,"mem":82,"mm":55,"collab":50,"eco":78,"cost":90},
                   "hl": "RAG 专家: LlamaParse 文档解析 + 数据连接器最全",
                   "diff": "检索增强场景第一; 通用 Agent 编排能力弱于 LangChain",
                   "power": "任意 LLM"},
    "CrewAI":     {"dims": {"orch":90,"tools":80,"auto":72,"mem":78,"mm":45,"collab":96,"eco":65,"cost":88},
                   "hl": "角色分工式多 Agent 协作, YAML 即可组建一支团队",
                   "diff": "多Agent 协作上手最快; 生产稳定性与可观测性尚在补齐",
                   "power": "任意 LLM"},
    "AutoGen":    {"dims": {"orch":92,"tools":85,"auto":78,"mem":72,"mm":50,"collab":95,"eco":70,"cost":90},
                   "hl": "微软研究院多 Agent 对话编排 + AutoGen Studio 可视化",
                   "diff": "学术与原型验证最强; 生产化运维需自行搭建",
                   "power": "任意 LLM"},
    "n8n":        {"dims": {"orch":85,"tools":90,"auto":55,"mem":60,"mm":45,"collab":65,"eco":92,"cost":70},
                   "hl": "可视化工作流 + 500 连接器, 自托管友好的低代码 Agent",
                   "diff": "SaaS/自托管集成之王; 深度 Agentic 推理与长任务受限",
                   "power": "任意 LLM"},
    "Dify":       {"dims": {"orch":80,"tools":82,"auto":62,"mem":75,"mm":55,"collab":60,"eco":80,"cost":85},
                   "hl": "开源 LLMOps 全家桶: RAG + Agent + Workflow + 插件市场",
                   "diff": "可视化与自托管最均衡; Agent 编排深度不及代码框架",
                   "power": "任意 LLM"},
    "Hermes Agent": {"dims": {"orch":86,"tools":92,"auto":88,"mem":90,"mm":55,"collab":84,"eco":60,"cost":75},
                   "hl": "CLI 原生个人 Agent: 浏览器控制 + 持久记忆 + 技能系统 + Kanban 看板",
                   "diff": "个人工作流自动化维度最全; 面向开发者, 生态规模有限, 无 SaaS 托管版",
                   "power": "多供应商模型可切换"},
    "字节跳动 (ByteDance)": {"dims": {"orch":84,"tools":85,"auto":65,"mem":72,"mm":75,"collab":78,"eco":85,"cost":75},
                   "hl": "扣子 Coze 可视化编排 + 万级插件市场 + 豆包生态",
                   "diff": "国内插件生态最大; 深度绑定字节系产品",
                   "power": "豆包 1.5 / Doubao-1.6"},
    "阿里巴巴 (Alibaba)": {"dims": {"orch":86,"tools":84,"auto":68,"mem":75,"mm":80,"collab":75,"eco":90,"cost":70},
                   "hl": "百炼企业级 Agent 编排 + Qwen 开源家族双轮驱动",
                   "diff": "开源模型 + 企业平台组合拳最完整; 绑定阿里云",
                   "power": "Qwen3 / Qwen2.5 系列"},
    "百度 (Baidu)": {"dims": {"orch":82,"tools":80,"auto":65,"mem":72,"mm":75,"collab":70,"eco":82,"cost":68},
                   "hl": "千帆 AppBuilder + 文心行业模型, 政务/能源合规案例最多",
                   "diff": "国内行业合规方案最深; 绑定百度智能云",
                   "power": "ERNIE 4.5 / 文心 5.0"},
    "DeepSeek":   {"dims": {"orch":60,"tools":72,"auto":60,"mem":55,"mm":55,"collab":45,"eco":60,"cost":98},
                   "bench": {"SWE-V":68.1,"AIME25":82.5,"GPQA":79.9},
                   "hl": "开源权重 + 极致性价比, Agent 底座成本之王",
                   "diff": "模型便宜且能打; 无原生 Agent 平台, 编排全靠自建",
                   "power": "DeepSeek-V3.1 / R1-0528"},
    "智谱AI (Zhipu AI)": {"dims": {"orch":75,"tools":82,"auto":82,"mem":68,"mm":72,"collab":55,"eco":65,"cost":80},
                   "bench": {"τ²-bench":84.0,"SWE-V":64.2},
                   "hl": "AutoGLM 手机/电脑 GUI 自主操作 + 开源 GLM 双线",
                   "diff": "国内 GUI Agent 先行者; 平台生态规模较小",
                   "power": "GLM-4.5 / AutoGLM"},
    "月之暗面 (Moonshot AI)": {"dims": {"orch":65,"tools":75,"auto":72,"mem":78,"mm":60,"collab":40,"eco":62,"cost":85},
                   "bench": {"SWE-V":65.8,"τ²-bench":75.4,"BrowseComp":47.5},
                   "hl": "超长上下文 + Kimi Researcher 自主深度研究",
                   "diff": "长文档/深研场景之王; 平台编排能力有限",
                   "power": "Kimi K2"},
    "GitHub Copilot": {"dims": {"orch":70,"tools":88,"auto":80,"mem":65,"mm":45,"collab":72,"eco":92,"cost":62},
                   "bench": {"SWE-V":54.6},
                   "hl": "IDE 内置 + PR Agent + 代码评审一条龙",
                   "diff": "GitHub 工作流集成最深; 绑定微软生态, 仅编程场景",
                   "power": "GPT-4.1 / Claude / o3"},
    "Cursor":     {"dims": {"orch":78,"tools":85,"auto":82,"mem":70,"mm":40,"collab":55,"eco":78,"cost":65},
                   "bench": {"SWE-V":60.0},
                   "hl": "代码库级语义索引 + 多文件 Agent 编辑",
                   "diff": "IDE Agent 交互体验最佳; 闭源, 仅编程场景",
                   "power": "Claude / GPT / Gemini"},
    "Cline":      {"dims": {"orch":72,"tools":88,"auto":85,"mem":62,"mm":35,"collab":50,"eco":68,"cost":90},
                   "bench": {"SWE-V":70.0},
                   "hl": "VS Code 开源 Agent, MCP 工具生态先锋",
                   "diff": "开源 + BYOK 按量付费最省钱; 依赖 VS Code",
                   "power": "Claude / DeepSeek 等"},
    "Aider":      {"dims": {"orch":60,"tools":80,"auto":75,"mem":40,"mm":30,"collab":25,"eco":50,"cost":92},
                   "bench": {"SWE-V":64.0},
                   "hl": "终端 Git 原生 pair programming, 每次修改即一次提交",
                   "diff": "最轻量透明; 无 IDE / 无多模态 / 无多Agent",
                   "power": "任意 LLM (BYOK)"},
    "腾讯元器":   {"dims": {"orch":72,"tools":75,"auto":60,"mem":65,"mm":70,"collab":62,"eco":80,"cost":72},
                   "hl": "微信 + 腾讯生态直达的智能体平台",
                   "diff": "触达微信用户独一无二; 绑定腾讯生态",
                   "power": "混元 Hunyuan"},
    "科大讯飞星火": {"dims": {"orch":70,"tools":72,"auto":58,"mem":65,"mm":78,"collab":55,"eco":70,"cost":70},
                   "hl": "语音多模态 + 教育/医疗行业深耕",
                   "diff": "语音交互最强之一; 通用 Agent 编排偏弱",
                   "power": "星火 X1 / X2"},
}

# ── render HTML ──────────────────────────────────────────────────
def render_html(cache):
    """渲染现代化前端页面 (资讯聚合 + 模型/Agent 能力大盘 + 性能对比)。
    具体实现在同目录的 ai_intel_render 模块中, 此处只做数据注入。"""
    from ai_intel_render import render_page
    return render_page(cache, MODEL_REGISTRY, AGENT_PLATFORMS,
                       max_entries=MAX_ENTRIES, max_days=MAX_DAYS,
                       model_benchmarks=MODEL_BENCHMARKS, agent_caps=AGENT_CAPS)

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
        # 逐源记录本轮抓取结果 -> sources_history 累积, 监控面板的成功率/连败统计才有数据
        _region_of = {s[0]: s[1] for s in SOURCE_REGISTRY}
        for name, st in errors.items():
            record_source_status(cache, name, _region_of.get(name, "int"),
                                 st["n"], st.get("err", ""), now.isoformat())
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

    # GitHub 热榜: 3 小时 TTL, 失败不影响其他 tab
    gh_block_err = False
    gh = load_github_cache(cache)
    if gh:
        print("[AI Intel] GitHub 热榜缓存命中: %d 项目" % len(gh["items"]))
    else:
        print("[AI Intel] 抓取 GitHub 最热 AI 项目 (gh api, 走代理)...")
        try:
            gh = fetch_github_repos()
            gh_block_err = bool(gh["errors"])
            for e in gh["errors"]:
                print("  [WARN] github: %s" % e)
            print("[AI Intel] GitHub 热榜: %d 项目 | %d 组织" % (len(gh["items"]), len(gh["orgs"])))
            cache["github_repos"] = gh
        except Exception as ex:
            print("[AI Intel] GitHub 热榜抓取失败, 保留旧数据: %s" % str(ex)[:80])
            gh_block_err = True
    # 监控板块: 本轮管线运行日志 (保留最近 48 条, 渲染层监控中心 tab 消费)
    _st = cache["meta"].get("sources_status", {})
    append_run_log(cache,
                   n_ok=sum(1 for v in _st.values() if v.get("dot") == "good"),
                   n_total=len(_st),
                   n_items=len(new_int) + len(new_cn),
                   gh_n=len(cache.get("github_repos", {}).get("items", [])),
                   gh_err=gh_block_err,
                   skipped=not need_refresh)
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
