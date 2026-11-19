"""RSS/Atom 解析与正文提取：默认仅用标准库，feedparser 可选。

修复 0.1 版本两处缺陷：
1. 0.1 用 `\\p{P}`（PCRE 语法）做正则，在 Python re 下会抛异常；
2. 0.1 fetch_rss_feed 中引用了未定义的 `title` 变量（应为 item.title）。
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import List, Optional, Tuple

from .http import http_get


# ─── HTML → 纯文本 ─────────────────────────────────────────
class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def strip_html(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html or "")
    return "".join(parser.parts)


def clean_html_to_text(html: str) -> str:
    text = html or ""
    for tag in ("script", "style", "nav", "header", "footer", "aside", "svg", "noscript"):
        text = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _collect_paragraphs(text: str) -> List[str]:
    result: List[str] = []
    for para in text.split("\n"):
        para = para.strip()
        if len(para) < 30:
            continue
        if para.count("|") > 5 or para.count(">") > 5:
            continue
        if any(kw in para for kw in ("登录", "注册", "首页", "菜单", "搜索", "关注我们", "版权所有")):
            continue
        result.append(para)
    return result


def _best_para_block(paras: List[str], max_paras: int = 8) -> List[str]:
    if not paras or len(paras) <= max_paras:
        return paras
    best_start, best_score = 0, -1.0
    for start in range(len(paras) - max_paras + 1):
        block = paras[start:start + max_paras]
        score = sum(len(p) for p in block) / len(block)
        if score > best_score:
            best_score, best_start = score, start
    return paras[best_start:best_start + max_paras]


def extract_article_body(html: str, limit: int = 2000) -> Tuple[str, str]:
    """返回 (摘要, 正文)：按文字密度选最密段块，取开头 + 结尾。"""
    text = clean_html_to_text(html).replace("。 ", "。\n")
    paras = _collect_paragraphs(text)
    if not paras:
        return "", ""
    dense = _best_para_block(paras, max_paras=min(12, len(paras)))
    head = dense[:3]
    tail = dense[-3:] if len(dense) > 6 else []
    body = "\n\n".join(head + tail)[:limit].strip()
    return body[:260].strip(), body


# ─── Feed 数据结构 ─────────────────────────────────────────
@dataclass
class FeedItem:
    title: str
    link: str
    description: str
    published: str
    updated: str
    author: str
    tags: List[str]
    content: str


def _local(tag: str) -> str:
    return tag.split("}", 1)[1] if "}" in tag else tag


def _child_text(element: ET.Element, names: set) -> str:
    for child in element:
        if _local(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def normalize_date(value: str) -> str:
    """RFC822 / ISO8601 → ISO8601 字符串；失败返回空串。"""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_feed(xml_text: str, source_url: str = "") -> List[FeedItem]:
    """解析 RSS 2.0 / Atom / RSS 1.0(RDF)；坏 XML 返回空列表。"""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    candidates: List[ET.Element] = []
    if _local(root.tag) == "rss":
        channel = next((c for c in root if _local(c.tag) == "channel"), root)
        candidates = [c for c in channel if _local(c.tag) == "item"]
    elif _local(root.tag) == "feed":
        candidates = [c for c in root if _local(c.tag) == "entry"]
    else:
        candidates = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]

    items: List[FeedItem] = []
    for node in candidates:
        title = _child_text(node, {"title"})
        if not title:
            continue

        link = _child_text(node, {"link"})
        if not link:
            for child in node:
                if _local(child.tag) == "link":
                    href = child.attrib.get("href")
                    if href and child.attrib.get("rel", "alternate") in ("alternate", ""):
                        link = href
                        break

        published = normalize_date(
            _child_text(node, {"published", "pubDate", "date"}) or ""
        )
        updated = normalize_date(_child_text(node, {"updated"}) or "")

        content = _child_text(node, {"encoded", "content"})
        description = _child_text(node, {"description", "summary"})

        author = _child_text(node, {"author", "creator"})
        tags: List[str] = []
        for child in node:
            if _local(child.tag) == "category":
                tags.append(child.attrib.get("term") or "".join(child.itertext()).strip())

        items.append(FeedItem(
            title=re.sub(r"\s+", " ", title).strip(),
            link=link.strip(),
            description=description,
            published=published,
            updated=updated,
            author=author,
            tags=[t for t in tags if t],
            content=content,
        ))
    return items


def _infer_region(url: str) -> str:
    lowered = url.lower()
    if "rsshub" in lowered or "zh-cn" in lowered or "hl=zh" in lowered or "36kr" in lowered:
        return "cn"
    return "int"


def fetch_rss_feed(
    name: str,
    url: str,
    keywords: Optional[List[str]] = None,
    region: Optional[str] = None,
    timeout: int = 20,
    fetch_body: bool = False,
) -> Tuple[List[dict], Optional[str]]:
    """抓取单个源；返回 (entries, error)。entries 字段与 schema.NewsEntry 一致。"""
    content, err = http_get(url, timeout=timeout)
    if err:
        return [], err

    entries: List[dict] = []
    for item in parse_feed(content, url):
        if keywords:
            haystack = f"{item.title} {item.description}".lower()
            if not any(kw.lower() in haystack for kw in keywords):
                continue

        raw_time = item.published or item.updated
        stamp = normalize_date(raw_time) or datetime.now().isoformat(timespec="seconds")

        html = item.content or item.description
        summary, body = extract_article_body(html)
        if fetch_body and item.link and len(body) < 80:
            page, page_err = http_get(item.link, timeout=timeout)
            if not page_err and page:
                summary, body = extract_article_body(page)

        if len(summary) < 50:
            summary = strip_html(item.description)[:260]

        entries.append({
            "title": item.title,
            "summary": summary,
            "body": body,
            "url": item.link,
            "sources": [name],
            "heat": 0,
            "timestamp": stamp,
            "date": stamp[:10],
            "region": region or _infer_region(url),
        })
    return entries, None
