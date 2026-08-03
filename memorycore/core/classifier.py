#!/usr/bin/env python3
"""core/classifier.py — 冷热判定规则 (纯规则, 不调 LLM)

规则 (2026-08-03 定稿, 与 v2 分流流程一致):
1. importance >= 0.8 或命中热关键词 → 热 (留本地)
2. 命中过时标记 → 过时 (E2: forget 不迁移)
3. 否则 → 冷 (下沉 cold tier)
混合条目 (既含高频偏好又含低频细节) → 建议拆开, 高频留本地低频下沉。
"""
from typing import Dict, List, Tuple

from .config import HOT_KEYWORDS, STALE_MARKERS

# 判定结果类型
HOT = "hot"
COLD = "cold"
STALE = "stale"
MIXED = "mixed"


def classify(content: str, importance: float = 0.8, scope: str = "global") -> Dict[str, str]:
    """判定一条内容的冷热。

    Returns:
        {"decision": "hot"|"cold"|"stale", "reason": "..."}
    """
    if not content or not content.strip():
        return {"decision": COLD, "reason": "empty content"}

    # E2: 过时状态记录 → 不迁移 (直接 forget/删除)
    # 长度门禁: 只有短条目 (≤80 字) 含状态标记才算过时;
    # 长条目含状态词通常是混合记录 (如配置项里引用已停用服务),
    # 宁留本地不误删。
    if len(content) <= 80:
        for marker in STALE_MARKERS:
            if marker in content:
                return {"decision": STALE, "reason": f"stale marker '{marker}' (short entry)"}

    # 热: importance 高 或 命中热关键词
    if importance >= 0.8:
        return {"decision": HOT, "reason": f"importance={importance} >= 0.8"}

    for kw in HOT_KEYWORDS:
        if kw in content:
            return {"decision": HOT, "reason": f"hot keyword '{kw}'"}

    # 默认冷
    return {"decision": COLD, "reason": "low importance, no hot signals"}


def should_keep_local(content: str) -> bool:
    """溢流场景的热保留判定 (比 classify 更严格, 只保留真正每轮要用的)。

    v2 分流判断: 行为准则 / 交互偏好 / 用户纠正 / 环境常量 → 留本地;
    状态记录 / 历史决策 / 低频配置细节 → 可下沉。
    用户偏好表达多样 (不一定带关键词), 所以这里同时检查:
    - 明确的准则/偏好/纠正/常量关键词
    - 内容以"用户""我"开头的偏好陈述 (启发式)
    """
    # 准则/偏好/纠正/常量关键词 (比 HOT_KEYWORDS 更聚焦)
    keep_markers = [
        "偏好", "准则", "原则", "禁止", "必须", "习惯", "要求",
        "纠正", "拍板", "明确要求", "零容忍", "不允许",
        "行为准则", "交互习惯", "写作风格", "回答风格", "环境常量",
        # 用户红线/强规范词 (2026-08-03 补漏)
        "强制", "绝不", "不能", "红线", "硬约束", "唯一", "必须用",
    ]
    for kw in keep_markers:
        if kw in content:
            return True
    # 用户偏好陈述启发式: "用户喜欢/偏好/希望/要求/不喜欢" 开头
    user_pref_prefixes = [
        "用户喜欢", "用户偏好", "用户希望", "用户要求", "用户不喜欢",
        "用户习惯", "用户希望我", "用户要求我", "用户纠正", "用户明确",
        "用户对",  # 2026-08-03 补漏: "用户对自托管项目兴趣..."
    ]
    for p in user_pref_prefixes:
        if content.startswith(p) or p in content[:25]:
            return True
    # 用户偏好/兴趣陈述启发式: 内容首 30 字含 "用户对...兴趣" 组合
    head30 = content[:30]
    if "用户对" in head30 and ("兴趣" in head30 or "偏好" in head30):
        return True
    return False


def split_mixed(content: str) -> Tuple[List[str], List[str]]:
    """混合条目拆分: 返回 (hot_parts, cold_parts)。

    按句子切分 (。！？;换行), 逐句判定。纯启发式, 不做语义理解。
    """
    import re

    sentences = [s.strip() for s in re.split(r"[。！？;；\n]", content) if s.strip()]
    hot_parts, cold_parts = [], []
    for s in sentences:
        d = classify(s, importance=0.5)  # 句子级用默认低 importance
        if d["decision"] == HOT:
            hot_parts.append(s)
        else:
            cold_parts.append(s)
    return hot_parts, cold_parts
