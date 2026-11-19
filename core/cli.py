"""CLI / 文件公共工具：路径解析、日志、原子 JSON 读写。"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# 项目根目录：core/ 的上一级。所有路径都相对它解析，避免依赖当前工作目录。
ROOT: Path = Path(__file__).resolve().parent.parent


def project_path(*parts: str) -> Path:
    """把项目内相对路径解析为绝对路径。"""
    return ROOT.joinpath(*parts)


def output_dir(*parts: str) -> Path:
    return project_path("output", *parts)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def setup_logging(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )
    return logging.getLogger(name)


def write_json(path: str | Path, obj: Any) -> Path:
    """原子写 JSON：先写 .tmp 再 replace，避免中途失败留下半截文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)
    tmp.replace(p)
    return p


def read_json(path: str | Path, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with open(p, "r", encoding="utf-8") as fh:
        return json.load(fh)


def base_parser(description: str) -> argparse.ArgumentParser:
    """生成带统一 --cache 参数的参数解析器。"""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--cache",
        default=str(project_path("output", "cache", "ai_intel_cache.json")),
        help="缓存文件路径（默认 output/cache/ai_intel_cache.json）",
    )
    return parser


def data_arg(parser: argparse.ArgumentParser, default_name: str) -> None:
    """添加 --data（分析产出路径）参数。"""
    parser.add_argument(
        "--data",
        default=str(output_dir("intel", default_name)),
        help="分析产出路径",
    )
