from __future__ import annotations

import re

_WEAK_PREFIX = re.compile(
    r"^(?:请问|请帮我|帮我|麻烦帮我|我想问(?:一下)?|查询(?:一下)?|查一下|看一下|分析一下|请分析|请查询|分析)\s*"
)
_MARKDOWN = re.compile(r"[`#>*_~\[\]()]|\x00")
_SPLIT = re.compile(r"[。！？!?；;\n\r]+")


def rule_title(content: str) -> str:
    text = _MARKDOWN.sub("", content or "")
    text = re.sub(r"\s+", " ", text).strip()
    previous = None
    while previous != text:
        previous = text
        text = _WEAK_PREFIX.sub("", text).strip(" ，,：:")
    if not text:
        return "新对话"
    first = _SPLIT.split(text, maxsplit=1)[0].strip()
    if len(first) <= 32:
        return first
    return first[:31].rstrip() + "…"
