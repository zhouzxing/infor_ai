"""HTTP 工具：代理 + 重试 + 错误分类。

纯函数式：不写缓存、不记录业务日志。返回 (content, error)，调用方决定如何落盘。
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from typing import Dict, Optional, Tuple

PROXY_HTTP = os.getenv("PROXY_HTTP", "http://127.0.0.1:7897")
PROXY_HTTPS = os.getenv("PROXY_HTTPS", "http://127.0.0.1:7897")
USER_AGENT = "Mozilla/5.0 (compatible; AI-Intel-Trands/1.0)"
RETRYABLE = (403, 408, 425, 429, 500, 502, 503, 504)


def _opener(headers: Optional[Dict[str, str]] = None):
    proxy = urllib.request.ProxyHandler({"http": PROXY_HTTP, "https": PROXY_HTTPS})
    opener = urllib.request.build_opener(proxy)
    default = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    default.update(headers or {})
    opener.addheaders = list(default.items())
    return opener


def http_get(
    url: str,
    timeout: int = 20,
    max_retries: int = 3,
    headers: Optional[Dict[str, str]] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """统一 GET；403/429/5xx 与网络错误按指数退避重试。"""
    opener = _opener(headers)
    last_error = "unknown"
    req_headers = dict(headers or {})
    req_headers.setdefault("User-Agent", USER_AGENT)

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
                charset = resp.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace"), None
        except urllib.error.HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.reason}"
            if exc.code in RETRYABLE and attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None, last_error
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
        except Exception as exc:  # IncompleteRead / 连接重置等非 OSError 异常
            last_error = f"{type(exc).__name__}: {exc}"
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    return None, last_error


def classify_error(error: str) -> str:
    """把原始错误归类为监控中心可用的原因码。"""
    if not error:
        return "unknown"
    if "HTTP 403" in error:
        return "反爬拦截：403（需 UA/Cookie/TLS 指纹或镜像）"
    if "HTTP 429" in error:
        return "反爬拦截：429 限流（需降低并发 / 增加间隔）"
    if "HTTP 401" in error or "HTTP 407" in error:
        return "鉴权失败（检查 API Token / 代理认证）"
    if "HTTP 5" in error:
        return "目标站服务端错误（稍后重试）"
    if "timed out" in error.lower() or "Timeout" in error or "URLError" in error:
        return "网络/代理不可达（检查 PROXY_HTTP 是否在线）"
    if "JSON" in error or "XML" in error or "decode" in error.lower():
        return "页面结构变更（解析规则需更新）"
    return "其他：" + error[:80]
