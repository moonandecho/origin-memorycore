#!/usr/bin/env python3
"""core/overflow.py — 六步溢流流程 v2 完整版

把本地热层 (MEMORY.md / USER.md) 中低频/过时数据安全迁移到冷层 cold tier。

六步:
  1. 容量基线统计
  2. 同主题查重 (冷层已有 → 不重复写)
  3. 过时事实过滤 (STALE → forget 冷层 + 删本地)
  4. 同类事实合并 (本地碎片合并再下沉)
  5. 安全溢流写入 (先写冷层确认 → 再删本地)
  6. 验证 (返回统计)
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Tuple

from .classifier import classify, should_keep_local, HOT, COLD, STALE

# ---- 相似度阈值 ------------------------------------------------------------

_SAME_FACT_RATIO = 0.85       # 内容几乎相同 → 同一事实
_SIMILAR_TOPIC_RATIO = 0.50   # 同主题但细节不同 → 需 merge
_RECALL_SCORE_SAME = 0.80     # recall dense_score >= 此值视为高度匹配
_RECALL_SCORE_SIMILAR = 0.48  # recall dense_score >= 此值视为同主题
_RECALL_TOP_K = 3             # 查重时的召回数
_MIN_TEXT_RATIO = 0.15        # 最低字面相似度门禁 (防向量误匹配)


# ---- 主入口 ----------------------------------------------------------------

def run_overflow(store, client, target: str) -> dict:
    """对单个文件 (memory / user) 执行完整六步溢流。

    Args:
        store: LocalStore 实例
        client: ColdStoreClient 实例
        target: "memory" | "user"

    Returns:
        dict: {overflowed, updated, deleted, merged, kept,
               usage_before, usage_after, chars_before, chars_after, errors}
    """
    stat: Dict[str, Any] = {
        "overflowed": 0,
        "updated": 0,
        "deleted": 0,
        "merged": 0,
        "kept": 0,
        "errors": 0,
    }

    # ---- Step 1: 容量基线统计 --------------------------------------------
    entries = store.entries(target)
    if not entries:
        stat["usage_before"] = f"{store.usage_pct(target)}%"
        stat["usage_after"] = f"{store.usage_pct(target)}%"
        stat["chars_before"] = 0
        stat["chars_after"] = 0
        return stat

    stat["chars_before"] = store.char_count(target)
    stat["usage_before"] = f"{store.usage_pct(target)}%"

    # ---- Step 4: 同类事实合并 (先于下沉) ---------------------------------
    merged_entries, merge_count = _merge_local_fragments(entries)
    if merge_count > 0:
        _rebuild_file(store, target, merged_entries)
        stat["merged"] = merge_count
        entries = merged_entries

    # ---- Step 2+3+5: 逐条处理 --------------------------------------------
    for entry in entries:
        if should_keep_local(entry):
            stat["kept"] += 1
            continue

        decision = classify(entry, importance=0.6)
        d = decision["decision"]

        if d == STALE:
            _handle_stale(store, client, target, entry, stat)
        elif d == COLD:
            _handle_cold_migration(store, client, target, entry, stat)
        else:
            stat["kept"] += 1

    # ---- Step 6: 验证 ----------------------------------------------------
    stat["chars_after"] = store.char_count(target)
    stat["usage_after"] = f"{store.usage_pct(target)}%"
    stat["target"] = target
    return stat


# ---- Step 2+5: 冷迁移 ----------------------------------------------------

def _handle_cold_migration(store, client, target: str, entry: str,
                           stat: dict) -> None:
    """冷候选条目: 查重 → (跳过/update/remember) → 删本地。"""
    try:
        existing = _recall_safe(client, entry)
    except Exception:
        stat["errors"] += 1
        return  # 冷层不可达 → 保留本地

    if existing:
        matched = _find_best_match(entry, existing)
        if matched:
            if matched["level"] == "same":
                # 冷层已有相同事实 → 不重复写, 直接删本地
                _safe_remove_local(store, target, entry, stat)
                stat["overflowed"] += 1  # 算溢流 (已在冷层)
                return
            elif matched["level"] == "similar":
                # 冷层有相似 → update 合并
                merged = _merge_two_entries(entry, matched["content"])
                try:
                    r = client.update(matched["id"], merged)
                    if r.get("status") == "updated":
                        _safe_remove_local(store, target, entry, stat)
                        stat["updated"] += 1
                        return
                except Exception:
                    stat["errors"] += 1
                    return  # update 失败 → 本地保留
            # 匹配不满足阈值或 update 返回非 updated → 降级到 remember

    # 无匹配 → 新写入冷层
    try:
        r = client.remember(entry, importance=0.6, scope="global")
        if r.get("status") == "stored":
            _safe_remove_local(store, target, entry, stat)
            stat["overflowed"] += 1
        else:
            stat["errors"] += 1
    except Exception:
        stat["errors"] += 1


# ---- Step 3: 过时处理 ----------------------------------------------------

def _handle_stale(store, client, target: str, entry: str, stat: dict) -> None:
    """过时条目: 尝试 forget 冷层匹配, 然后删本地。"""
    try:
        existing = _recall_safe(client, entry)
        if existing:
            for ex in existing:
                ratio = difflib.SequenceMatcher(
                    None, entry, ex.get("content", "")
                ).ratio()
                if ratio > _SAME_FACT_RATIO:
                    try:
                        client.forget(ex["id"])
                    except Exception:
                        pass  # forget 失败不阻塞
    except Exception:
        pass  # 冷层不可达, 至少删本地

    _safe_remove_local(store, target, entry, stat)
    stat["deleted"] += 1


# ---- Step 4: 本地碎片合并 ------------------------------------------------

def _smart_ratio(a: str, b: str) -> float:
    """智能相似度: 长条目 (>200字) 额外对比头部 150 字避免尾部稀释。

    返回 max(全文 ratio, 头部 ratio)。长条目尾部细节差异不应掩盖同主题。
    """
    full = difflib.SequenceMatcher(None, a, b).ratio()
    if len(a) > 200 or len(b) > 200:
        head = difflib.SequenceMatcher(None, a[:150], b[:150]).ratio()
        return max(full, head)
    return full


def _merge_local_fragments(entries: List[str]) -> Tuple[List[str], int]:
    """检测本地同主题碎片并合并。返回 (merged, merge_count)。"""
    if len(entries) <= 1:
        return list(entries), 0

    n = len(entries)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for i in range(n):
        for j in range(i + 1, n):
            ratio = _smart_ratio(entries[i], entries[j])
            if ratio > _SIMILAR_TOPIC_RATIO:
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)

    merged = []
    merge_count = 0
    for indices in groups.values():
        if len(indices) == 1:
            merged.append(entries[indices[0]])
        else:
            group_entries = [entries[i] for i in indices]
            merged.append(_merge_group(group_entries))
            merge_count += len(indices) - 1

    return merged, merge_count


def _merge_group(entries: List[str]) -> str:
    """合并一组同主题条目: 最长为主体, 追加不重复句子。"""
    if len(entries) == 1:
        return entries[0]

    sorted_entries = sorted(entries, key=len, reverse=True)
    base = sorted_entries[0]
    base_sentences = set(re.split(r"[。！？;；\n]", base))

    extra = []
    for entry in sorted_entries[1:]:
        for s in re.split(r"[。！？;；\n]", entry):
            s = s.strip()
            if not s:
                continue
            is_new = True
            for bs in base_sentences:
                if (s in bs or bs in s or
                        difflib.SequenceMatcher(None, s, bs).ratio() > 0.8):
                    is_new = False
                    break
            if is_new:
                extra.append(s)

    if extra:
        base = base.rstrip("。！？;；\n") + "。" + "。".join(extra) + "。"
    return base


# ---- 辅助函数 ------------------------------------------------------------

def _recall_safe(client, entry: str) -> List[Dict[str, Any]]:
    """安全调用 recall_results, 截前 200 字作查询。"""
    query = entry[:200]
    return client.recall_results(query, top_k=_RECALL_TOP_K)


def _find_best_match(entry: str,
                     candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """在召回候选中找最佳匹配。返回 {level, id, content, dense_score} 或 None。"""
    best = None
    best_score = 0.0
    best_ratio = 0.0

    for c in candidates:
        c_content = c.get("content", "")
        if not c_content:
            continue
        dense_score = c.get("dense_score", 0)
        text_ratio = difflib.SequenceMatcher(None, entry, c_content).ratio()
        # 加权组合: dense_score (语义) + text_ratio (字面), 避免单信号虚高
        combined = 0.6 * dense_score + 0.4 * text_ratio

        if combined > best_score:
            best_score = combined
            best_ratio = text_ratio
            best = {"id": c.get("id"), "content": c_content,
                    "dense_score": dense_score}

    # 门禁: 即使向量分很高, 字面完全不匹配也拒掉 (防 recall 误召回)
    if best is not None and best_ratio < _MIN_TEXT_RATIO:
        best = None

    if best is None or best_score < _RECALL_SCORE_SIMILAR:
        return None

    if best_score >= _RECALL_SCORE_SAME or best_ratio >= _SAME_FACT_RATIO:
        best["level"] = "same"
    else:
        best["level"] = "similar"
    return best


def _merge_two_entries(local: str, cold: str) -> str:
    """合并本地新条目与冷层已有条目。冲突取最新(本地 newer)。"""
    base = local
    base_sentences = set(re.split(r"[。！？;；\n]", base))

    extra = []
    for s in re.split(r"[。！？;；\n]", cold):
        s = s.strip()
        if not s:
            continue
        is_new = True
        for bs in base_sentences:
            if (s in bs or bs in s or
                    difflib.SequenceMatcher(None, s, bs).ratio() > 0.8):
                is_new = False
                break
        if is_new:
            extra.append(s)

    if extra:
        base = base.rstrip("。！？;；\n") + "。" + "。".join(extra) + "。"
    return base


def _safe_remove_local(store, target: str, entry: str, stat: dict) -> None:
    """安全删本地条目 (失败仅累加 errors, 不抛异常)。"""
    try:
        result = store.remove_by_exact(target, entry)
        if not result.get("success"):
            stat["errors"] += 1
    except Exception:
        stat["errors"] += 1


def _rebuild_file(store, target: str, entries: List[str]) -> None:
    """合并后重建文件 (调用 store 的原子写)。"""
    path = store._path_for(target)
    store._write_entries(path, entries)
