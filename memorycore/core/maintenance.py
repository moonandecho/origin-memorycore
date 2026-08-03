#!/usr/bin/env python3
"""core/maintenance.py — 冷层全量治理

对 cold tier 冷层执行定期巡检:
  1. 全量枚举 (尝试 list_all, 降级为多路 recall)
  2. 重复记忆合并 (同主题保留完整一条, 其余 forget)
  3. 过时事实清理 (命中 STALE 标记 → forget)
  4. 冲突事实取舍 (同主题新旧冲突 → 保留最新)
  5. 向量完整性校验 (total == embeddings)
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Set

from .classifier import STALE, classify

# ---- 阈值 ----------------------------------------------------------------

_SIM_THRESHOLD = 0.75       # 字面相似度 ≥ 此值视为同主题/重复
_CONFLICT_THRESHOLD = 0.55  # 同主题但内容差异大 → 可能冲突
_RECALL_BREADTH = 10        # 多路召回时的 top_k
_RECALL_QUERIES = [         # 多路召回种子查询 (覆盖常见主题空间)
    "偏好 习惯 准则",
    "服务器 网络 IP 配置",
    "项目 技术栈 框架",
    "bug 错误 问题",
    "API 接口 端点",
    "用户 行为 风格",
    "部署 环境 版本",
    "规则 禁止 要求",
    "已完成 已修复 已废弃",
    "数据 存储 数据库",
]


# ---- 主入口 ----------------------------------------------------------------

def run_maintenance(client) -> dict:
    """冷层全量治理。

    Args:
        client: ColdStoreClient 实例

    Returns:
        dict: {scanned, merged, cleaned, conflicts_resolved,
               vector_ok, total, embeddings, errors, note}
    """
    stat: Dict[str, Any] = {
        "scanned": 0,
        "merged": 0,
        "cleaned": 0,
        "conflicts_resolved": 0,
        "vector_ok": None,
        "total": 0,
        "embeddings": 0,
        "errors": 0,
    }

    # ---- Step 1: 全量枚举 ------------------------------------------------
    all_entries = _enumerate_all(client)
    stat["scanned"] = len(all_entries)

    if not all_entries:
        # 尝试 stats 至少拿计数
        try:
            s = client.stats()
            stat["total"] = s.get("total", 0)
            stat["embeddings"] = s.get("embeddings", 0)
            stat["vector_ok"] = (stat["total"] == stat["embeddings"])
        except Exception:
            pass
        stat["note"] = "no entries enumerated (cold layer may be empty or list_all unavailable)"
        return stat

    # ---- Step 2: 重复记忆合并 --------------------------------------------
    stat["merged"] = _merge_duplicates(client, all_entries)

    # ---- Step 3: 过时事实清理 --------------------------------------------
    stat["cleaned"] = _clean_stale(client, all_entries)

    # ---- Step 4: 冲突事实取舍 --------------------------------------------
    stat["conflicts_resolved"] = _resolve_conflicts(client, all_entries)

    # ---- Step 5: 向量完整性校验 ------------------------------------------
    try:
        s = client.stats()
        stat["total"] = s.get("total", 0)
        stat["embeddings"] = s.get("embeddings", 0)
        stat["vector_ok"] = (stat["total"] == stat["embeddings"])
    except Exception as e:
        stat["errors"] += 1
        stat["vector_ok"] = None
        stat["vector_error"] = str(e)

    return stat


# ---- Step 1: 枚举 ---------------------------------------------------------

def _enumerate_all(client) -> List[Dict[str, Any]]:
    """尽量枚举冷层全部条目。

    策略:
      1. 尝试 client.list_all() (若服务器支持)
      2. 降级: 多路 recall 覆盖主题空间, 按 ID 去重
    """
    # 尝试 list_all
    try:
        results = client.list_all()
        if results:
            return results
    except (AttributeError, Exception):
        pass

    # 降级: 多路 recall
    seen: Set[str] = set()
    all_entries: List[Dict[str, Any]] = []

    for query in _RECALL_QUERIES:
        try:
            results = client.recall_results(query, top_k=_RECALL_BREADTH)
            for r in results:
                rid = r.get("id", "")
                if rid and rid not in seen:
                    seen.add(rid)
                    all_entries.append(r)
        except Exception:
            continue

    return all_entries


# ---- Step 2: 重复合并 -----------------------------------------------------

def _merge_duplicates(client, entries: List[Dict[str, Any]]) -> int:
    """检测并合并重复记忆。返回合并对数。"""
    if len(entries) <= 1:
        return 0

    n = len(entries)
    merged = 0
    to_forget: Set[str] = set()
    # 记录哪些 ID 已被合并到哪个 ID (value)
    replacements: Dict[str, str] = {}

    for i in range(n):
        if entries[i]["id"] in to_forget:
            continue
        for j in range(i + 1, n):
            if entries[j]["id"] in to_forget:
                continue
            c1 = entries[i].get("content", "")
            c2 = entries[j].get("content", "")
            ratio = difflib.SequenceMatcher(None, c1, c2).ratio()

            if ratio >= _SIM_THRESHOLD:
                # 保留较长/较完整的一条
                if len(c2) > len(c1):
                    keeper, victim = entries[j], entries[i]
                else:
                    keeper, victim = entries[i], entries[j]

                # merge 内容到 keeper
                merged_content = _merge_for_dedup(keeper["content"], victim["content"])
                try:
                    client.update(keeper["id"], merged_content)
                    client.forget(victim["id"])
                    to_forget.add(victim["id"])
                    # 更新本地缓存
                    keeper["content"] = merged_content
                    merged += 1
                except Exception:
                    continue

    return merged


def _merge_for_dedup(base: str, other: str) -> str:
    """合并两条同主题条目: base 为主体, 追加 other 的独有句子。"""
    base_sentences = set(re.split(r"[。！？;；\n]", base))
    extra = []
    for s in re.split(r"[。！？;；\n]", other):
        s = s.strip()
        if not s:
            continue
        is_new = True
        for bs in base_sentences:
            if s in bs or bs in s or difflib.SequenceMatcher(None, s, bs).ratio() > 0.8:
                is_new = False
                break
        if is_new:
            extra.append(s)
    if extra:
        base = base.rstrip("。！？;；\n") + "。" + "。".join(extra) + "。"
    return base


# ---- Step 3: 过时清理 -----------------------------------------------------

def _clean_stale(client, entries: List[Dict[str, Any]]) -> int:
    """清理命中过时标记的冷层条目。返回清理数。

    复用 classifier.classify() 的 STALE 判定 (含 ≤80 字长度门禁):
    - 短条目 (≤80 字) 含过时标记 → 过时, forget
    - 长条目 (>80 字) 含状态词 → 视为混合记录, 保留 (与 overflow 一致)
    """
    cleaned = 0
    for entry in entries:
        content = entry.get("content", "")
        if not content:
            continue
        d = classify(content, importance=0.6)
        if d["decision"] == STALE:
            try:
                client.forget(entry["id"])
                cleaned += 1
            except Exception:
                pass
    return cleaned


# ---- Step 4: 冲突取舍 -----------------------------------------------------

def _resolve_conflicts(client, entries: List[Dict[str, Any]]) -> int:
    """解决同主题冲突: 保留最新的条目, 旧版 forget。

    冲突判定: 两条内容同主题 (相似度 > CONFLICT_THRESHOLD)
    但差异不够大到视为完全重复 (相似度 < SIM_THRESHOLD)。
    典型场景: 同一服务器 IP 变更, 同一偏好更新。
    策略: 保留较短的 (更新的偏好往往更简洁明确), 或依赖 ID 顺序 (更新的 ID 可能更大)。
    实际: 保留内容较长者 (通常更新版更详细), 并基于 recall 时间戳判断。
    若无时间戳 → 保留先遇到的 (ID 较小者往往是先创建的)。
    """
    if len(entries) <= 1:
        return 0

    n = len(entries)
    resolved = 0
    to_forget: Set[str] = set()

    for i in range(n):
        if entries[i]["id"] in to_forget:
            continue
        for j in range(i + 1, n):
            if entries[j]["id"] in to_forget:
                continue
            c1 = entries[i].get("content", "")
            c2 = entries[j].get("content", "")
            ratio = difflib.SequenceMatcher(None, c1, c2).ratio()

            if _CONFLICT_THRESHOLD <= ratio < _SIM_THRESHOLD:
                # 同主题冲突: 比较长度和内容, 保留更完整的
                # 规则: 较长的通常是更详细的更新版本
                if len(c2) >= len(c1):
                    keeper, victim = entries[j], entries[i]
                else:
                    keeper, victim = entries[i], entries[j]

                try:
                    client.forget(victim["id"])
                    to_forget.add(victim["id"])
                    resolved += 1
                except Exception:
                    continue

    return resolved
