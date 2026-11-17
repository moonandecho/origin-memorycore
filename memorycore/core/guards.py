"""core/guards.py — 写入路径守卫 (2026-11-17).

目前只有一条：禁止把"指针文本"当成记忆内容写进冷层。

背景：周治理曾把一条热层指针行当成正文下沉，冷层里因此出现内容只有
指针串的记录（实测 8 条）。这类记录零信息量，还会在后续对账里把
指针指向另一条指针，形成死链。

判据刻意写窄：
  * 必须以 [规则指针] 开头 —— 正文里举例提到指针格式的条目不受影响；
  * 按 。/§/换行 切开后，每一段都必须是纯指针 —— 一段正文带一条指针
    的混合内容仍然允许下沉（那是正常记忆）。
"""
from __future__ import annotations

import re

_POINTER_SEG = re.compile(r"(?:\[规则指针\])+[^→]{0,40}→recall:[0-9a-f]{8,64}")
_SPLIT = re.compile(r"[。§\n]")


def is_pointer_text(text: str) -> bool:
    """整段内容是否只是指针 (纯指针或指针串拼接)。"""
    t = (text or "").strip()
    if not t.startswith("[规则指针]"):
        return False
    segs = [s.strip() for s in _SPLIT.split(t) if s.strip()]
    if not segs:
        return False
    return all(_POINTER_SEG.fullmatch(s) for s in segs)
