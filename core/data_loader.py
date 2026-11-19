"""数据加载器：data/*.json 是唯一静态数据来源，代码里不再硬编码大表。

约定：load_data 返回的对象带 lru_cache，调用方不得原地修改；需要改动请先复制。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Dict, List

from .cli import project_path, read_json


@lru_cache(maxsize=None)
def load_data(name: str) -> Any:
    path = project_path("data", name)
    data = read_json(path)
    if data is None:
        raise FileNotFoundError(f"缺少数据文件: {path}")
    return data


def sources() -> List[Dict]:
    """42 个资讯源：name / region / url / keywords。"""
    return load_data("sources.json")


def models() -> Dict:
    """模型厂商静态注册表。"""
    return load_data("models.json")


def agent_platforms() -> Dict:
    """Agent 平台静态注册表。"""
    return load_data("agents.json")


def github_config() -> Dict:
    return load_data("github.json")


def arxiv_directions() -> List[Dict]:
    return load_data("arxiv_directions.json")


def arxiv_sources() -> List[Dict]:
    return load_data("arxiv_sources.json")


def jd_config() -> Dict:
    return load_data("jd_keywords.json")


def scoring() -> Dict:
    return load_data("scoring.json")


def company_dynamics() -> List[Dict]:
    return load_data("company_dynamics.json")["events"]


def themes() -> Dict:
    return load_data("themes.json")


def validate_data() -> List[str]:
    """返回数据层校验错误列表；空列表表示通过。"""
    errors: List[str] = []
    src = sources()
    if len(src) != 42:
        errors.append(f"sources.json 应有 42 个源，实际 {len(src)}")
    if sum(1 for s in src if s["region"] == "int") != 30:
        errors.append("sources.json 国际源应为 30")
    if sum(1 for s in src if s["region"] == "cn") != 12:
        errors.append("sources.json 国内源应为 12")
    weights = scoring()["dim_weights"]
    if abs(sum(weights.values()) - 1.0) > 1e-6:
        errors.append("dim_weights 权重之和必须为 1.0")
    for needed in ("signal_keywords", "oss_categories", "github_ai_keywords", "thresholds"):
        if needed not in scoring():
            errors.append(f"scoring.json 缺少 {needed}")
    if not models() or not agent_platforms():
        errors.append("models.json / agents.json 不得为空")
    for theme in themes()["themes"]:
        css = project_path("frontend", theme["css"])
        if not css.exists():
            errors.append(f"主题样式缺失: {css}")
    return errors
