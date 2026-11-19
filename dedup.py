"""表达文本规范化与去重。"""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable

from .quality import is_formulaic_expression

# 表达学习 prompt 里的示例，模型有时会原样抄回来
PROMPT_EXAMPLE_STYLES = frozenset({"我嘞个xxxx", "对对对", "这么强！"})

_STRIP_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def strip_usage_prefix(text: str) -> str:
    """去掉“使用……”前缀，保持和麦麦原生表达学习一致。"""
    normalized = str(text or "").strip()
    if normalized.startswith("使用"):
        normalized = normalized[len("使用") :].strip()
    return normalized


def normalize_for_match(text: str) -> str:
    """用于比较的紧凑形式：NFKC、小写、去掉空白和标点。"""
    normalized = unicodedata.normalize("NFKC", str(text or ""))
    normalized = normalized.lower()
    return _STRIP_RE.sub("", normalized)


def normalize_style(style: str) -> str:
    """规范化候选表达风格（保留“用……”这类可执行前缀）。"""
    return strip_usage_prefix(str(style or "").strip())


def normalize_style_for_match(style: str) -> str:
    """用于去重比较的风格形式，额外抹平“用「……”与“「……”的差异。"""
    normalized = normalize_style(style)
    if len(normalized) > 1 and normalized.startswith("用") and normalized[1] in "「『“\"":
        normalized = normalized[1:].strip()
    return normalize_for_match(normalized)


def similarity(left: str, right: str) -> float:
    """两个字符串的相似度，0~1。"""
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    return SequenceMatcher(None, left, right).ratio()


def _iter_situations(expression: dict[str, Any]) -> Iterable[str]:
    """取出一条表达记录里所有可用情境（situation + content_list）。"""
    yield str(expression.get("situation") or "")
    raw_content = expression.get("content_list")
    if isinstance(raw_content, str):
        try:
            import json

            content = json.loads(raw_content)
        except (TypeError, ValueError):
            content = []
    elif isinstance(raw_content, list):
        content = raw_content
    else:
        content = []
    for item in content if isinstance(content, list) else []:
        if isinstance(item, str):
            yield item


def is_duplicate(
    situation: str,
    style: str,
    existing: Iterable[dict[str, Any]],
    *,
    style_threshold: float = 0.9,
    situation_threshold: float = 0.88,
) -> tuple[bool, str]:
    """判断候选是否与现有表达重复。

    返回 ``(是否重复, 原因)``。同风格即视为重复，避免同一个口头禅反复入库；
    风格不同但情境高度相似则放行（同一情境可以有多种说法）。
    """
    candidate_style = normalize_style_for_match(style)
    candidate_situation = normalize_for_match(situation)
    if not candidate_style:
        return True, "空表达"

    for expression in existing:
        existing_style = normalize_style_for_match(str(expression.get("style") or ""))
        if not existing_style:
            continue
        style_ratio = similarity(candidate_style, existing_style)
        if style_ratio >= style_threshold:
            return True, f"与已有表达风格重复（{style_ratio:.2f}）"
        for existing_situation in _iter_situations(expression):
            situation_ratio = similarity(candidate_situation, normalize_for_match(existing_situation))
            if situation_ratio >= situation_threshold and style_ratio >= 0.75:
                return True, f"与已有表达情境/风格重复（{style_ratio:.2f}/{situation_ratio:.2f}）"
    return False, ""


def is_bad_style(style: str, *, max_chars: int = 80) -> tuple[bool, str]:
    """明显的坏候选：空、过长、纯标点/表情、prompt 示例泄漏。"""
    normalized = normalize_style(style)
    if not normalized:
        return True, "空表达"
    if len(normalized) > max_chars:
        return True, f"表达超过 {max_chars} 字"
    if normalized in PROMPT_EXAMPLE_STYLES:
        return True, "prompt 示例泄漏"
    compact = normalize_for_match(normalized)
    if not compact:
        return True, "表达只有标点或表情"
    if re.fullmatch(r"(?:哈|笑死|6|啊|草|awsl|xswl|牛|确实|支持|好耶|赞|顶)+", compact):
        return True, "过于通用的语气词"
    formulaic, reason = is_formulaic_expression(normalized)
    if formulaic:
        return True, reason
    return False, ""
