#!/usr/bin/env python3
"""core/timeline.py — MemoryCore 只读检索辅助实现 (timeline / find)。

本模块只包含纯函数式检索逻辑, 不持有任何网络配置或文件写入能力:
- 依赖注入 ``store`` / ``client`` / ``metastore_for``, 生产入口在 server.py;
- 冷层只通过 ``client.list_all()`` 读取, 不调用 remember/update/forget,
  也不调用 recall (因此绝不会 bump last_recalled)。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


def _text(value: Any) -> str:
    """None -> ""; 0/False 等可表示文本保留原值 (与 server._probe_id 同口径)。"""
    return "" if value is None else str(value)


def _topic(content: Any, max_chars: int = 60) -> str:
    """正文预览: 先去掉换行再截断, 绝不回传全文。"""
    try:
        n = max(0, int(max_chars))
    except (TypeError, ValueError, OverflowError):
        n = 60
    text = re.sub(r"[\r\n]+", "", "" if content is None else str(content))
    return text[:n]


def _parse_timestamp(value: Any) -> Optional[datetime]:
    """把冷层 timestamp 解析成 datetime; 无法解析返回 None。

    兼容 ISO-8601 (含 Z / +00:00) 以及常见的 "YYYY-MM-DD HH:MM:SS[.ffffff]"
    格式。数值时间戳按 Unix epoch 处理, 给测试数据留一条稳定路径。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except (TypeError, ValueError, OverflowError, OSError):
            return None
    raw = str(value).strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        dt = None
    if dt is None:
        try:
            dt = datetime.strptime(raw[:26], "%Y-%m-%d %H:%M:%S.%f")
        except (TypeError, ValueError):
            dt = None
    if dt is None:
        try:
            dt = datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            return None
    # 统一成 naive 时间再比较, 避免同一数据源里 aware/naive 混用导致 TypeError。
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})")


def _date_key(value: Any) -> str:
    """返回 timestamp 的日历日 (YYYY-MM-DD); 无法识别返回空串。"""
    if value is None:
        return ""
    m = _DATE_RE.match(str(value).strip())
    if m:
        return m.group(1) + "-" + m.group(2) + "-" + m.group(3)
    dt = _parse_timestamp(value)
    return dt.date().isoformat() if dt is not None else ""


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _rows_by_id(rows: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        rid = row.get("id")
        if rid is None:
            continue
        out[_text(rid)] = row
    return out


def _normalize_rows(rows: Any) -> List[Dict[str, Any]]:
    """把 list_all 返回归一成行列表; 兼容 dict 包装 ({results}/{items})。"""
    if isinstance(rows, dict):
        rows = rows.get("results", rows.get("items", []))
    if not isinstance(rows, (list, tuple)):
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        if isinstance(r, dict):
            out.append(dict(r))
        elif isinstance(r, (list, tuple)) and len(r) >= 1:
            # 极老的冷层输出偶尔退化成 [id, content, ...] 列表。
            out.append({
                "id": _text(r[0]),
                "content": "" if len(r) < 2 or r[1] is None else str(r[1]),
                "timestamp": "" if len(r) < 3 or r[2] is None else str(r[2]),
            })
    return out


# ---------------------------------------------------------------------------
# 句柄解析 (与 server.memorycore_recall 的 handle 逻辑保持同一口径)
# ---------------------------------------------------------------------------

def _legacy_handles_path(store: Any) -> Optional[Path]:
    """旧句柄直连冷层的兜底映射文件。

    生产路径与既有 memorycore_recall 相同 (~/.hermes/memories/...);
    测试/手测注入临时或 snapshot LocalStore 时, 同时认数据目录里的
    legacy_handles.json, 便于只读验证真实副本。
    """
    paths: List[Path] = []
    try:
        memory_path = getattr(store, "memory_path", None)
        if memory_path is not None:
            paths.append(Path(memory_path).parent / "legacy_handles.json")
    except Exception:
        pass
    paths.append(Path(os.path.expanduser("~/.hermes/memories/legacy_handles.json")))
    for p in paths:
        try:
            if p.is_file():
                return p
        except OSError:
            continue
    return None


def build_handle_map(store: Any,
                     metastore_for: Optional[Callable[[str], Any]],
                     rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """构造 handle -> {target, stub, meta} 映射。

    口径与 server.memorycore_recall 一致:
      * 热层 stub 元数据的 handle / legacy_handles;
      * 指针条目正文里的 recall:<hex>;
      * ~/.hermes/memories/legacy_handles.json (旧 16 位句柄 -> cold_id);
      * 冷层原文 sha256 前 16 位补映射 (内容更新后旧指针仍可解析)。
    """
    handle_map: Dict[str, Dict[str, Any]] = {}
    if store is None or metastore_for is None:
        return handle_map

    for target in ("memory", "user"):
        try:
            entries = store.entries(target)
        except Exception:
            continue
        for entry in entries or []:
            try:
                meta = metastore_for(target).get_entry(entry) or {}
            except Exception:
                continue
            if meta.get("type") != "stub" or not meta.get("cold_id"):
                continue
            keys = [meta.get("handle") or ""]
            legacy = meta.get("legacy_handles") or []
            if isinstance(legacy, str):
                legacy = [legacy]
            keys.extend([str(h) for h in legacy if h])
            m = re.search(r"recall:([0-9a-fA-F]{8,64})", entry)
            if m:
                keys.append(m.group(1))
            value = {"target": target, "stub": entry, "meta": meta}
            for h in keys:
                h = str(h).strip()
                if h and h not in handle_map:
                    handle_map[h] = value

    # 旧句柄 -> cold_id 兜底映射
    legacy_path = _legacy_handles_path(store)
    if legacy_path is not None:
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8") or "{}")
            for h, cid in (data or {}).items():
                h, cid = str(h).strip(), str(cid).strip()
                if h and cid and h not in handle_map:
                    handle_map[h] = {"target": "memory", "stub": "",
                                     "meta": {"cold_id": cid}}
        except Exception:
            pass

    # 冷层原文指纹兜底: 旧式指针内容虽被更新, 仍能按原 sha256 前 16 位解析。
    by_id = _rows_by_id(rows)
    try:
        for value in list(handle_map.values()):
            meta = value.get("meta") or {}
            cid = str(meta.get("cold_id") or "")
            row = by_id.get(cid)
            if row and row.get("content"):
                fp = hashlib.sha256(
                    str(row.get("content") or "").encode("utf-8")).hexdigest()[:16]
                handle_map.setdefault(fp, value)
    except Exception:
        pass
    return handle_map


def _resolve_handle(handle: str,
                    store: Any,
                    metastore_for: Optional[Callable[[str], Any]],
                    rows: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """按既有句柄口径定位 stub 映射; 找不到返回 None。"""
    raw = (handle or "").strip()
    if not raw:
        return None
    handle_map = build_handle_map(store, metastore_for, rows)
    hkey = raw[7:] if raw.lower().startswith("recall:") else raw
    return handle_map.get(raw) or handle_map.get(hkey)


# ---------------------------------------------------------------------------
# memorycore_timeline
# ---------------------------------------------------------------------------

def timeline(store: Any,
             client: Any,
             metastore_for: Optional[Callable[[str], Any]] = None,
             *,
             handle: str = "",
             id: str = "",
             before: int = 3,
             after: int = 3,
             window_hours: int = 24,
             max_chars: int = 60) -> str:
    """``memorycore_timeline`` 检索内核。

    定位中心条目后, 扫描冷层全量, 找出时间窗口内的邻居并按时间升序切成
    before/after; 每段只保留最靠近中心的 N 条。返回值只含截断 topic,
    不含正文。
    """
    handle = (handle or "").strip()
    center_id = (id or "").strip()
    has_handle = bool(handle)
    has_id = bool(center_id)
    if has_handle == has_id:
        return json.dumps(
            {"error": "exactly one of 'handle' or 'id' must be provided"},
            ensure_ascii=False)

    try:
        rows = _normalize_rows(client.list_all())
    except Exception as e:
        return json.dumps({"error": f"cold store unavailable: {e}"},
                          ensure_ascii=False)

    scanned = len(rows)
    center_row: Optional[Dict[str, Any]] = None
    if has_handle:
        hit = _resolve_handle(handle, store, metastore_for, rows)
        if not hit:
            return json.dumps({"error": f"handle not found: {handle}"},
                              ensure_ascii=False)
        cid = str(((hit.get("meta") or {}).get("cold_id") or ""))
        center_row = _rows_by_id(rows).get(cid)
    else:
        center_row = _rows_by_id(rows).get(center_id)

    if center_row is None:
        target = f"handle not found: {handle}" if has_handle else f"id not found: {center_id}"
        return json.dumps({"error": target}, ensure_ascii=False)

    center_ts = _parse_timestamp(center_row.get("timestamp"))
    if center_ts is None:
        return json.dumps(
            {"error": f"center timestamp is invalid: {center_row.get('timestamp')!r}"},
            ensure_ascii=False)

    center_key = _date_key(center_row.get("timestamp"))
    before_n = max(0, _safe_int(before, 3))
    after_n = max(0, _safe_int(after, 3))
    window_seconds = _safe_float(window_hours, 24.0) * 3600.0

    neighbors: List[Tuple[datetime, str, Dict[str, Any]]] = []
    same_day = 0
    for row in rows:
        if _date_key(row.get("timestamp")) == center_key:
            same_day += 1
        rid = _text(row.get("id"))
        if rid == _text(center_row.get("id")):
            continue
        ts = _parse_timestamp(row.get("timestamp"))
        if ts is None:
            continue
        delta = abs((ts - center_ts).total_seconds())
        if window_seconds >= 0 and delta <= window_seconds:
            neighbors.append((ts, rid, row))

    neighbors.sort(key=lambda item: (item[0], item[1]))
    center_rid = _text(center_row.get("id"))
    center_pair = (center_ts, center_rid)
    # 时间相同但 id 不同的冷层行用 id 作稳定 tie-break, 保证仍被计入窗口邻居。
    before_rows = [row for ts, _rid, row in neighbors
                   if (ts, _rid) < center_pair]
    after_rows = [row for ts, _rid, row in neighbors
                  if (ts, _rid) > center_pair]

    before_rows = before_rows[-before_n:] if before_n else []
    after_rows = after_rows[:after_n] if after_n else []

    def entry(row: Dict[str, Any]) -> Dict[str, str]:
        return {
            "id": _text(row.get("id")),
            "timestamp": _text(row.get("timestamp")),
            "topic": _topic(row.get("content"), max_chars),
        }

    return json.dumps({
        "center": entry(center_row),
        "before": [entry(r) for r in before_rows],
        "after": [entry(r) for r in after_rows],
        "same_day_count": same_day,
        "weak": (len(before_rows) + len(after_rows)) < 2,
        "window_hours": window_hours,
        "scanned": scanned,
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# memorycore_find
# ---------------------------------------------------------------------------

_DNS_HOST_RE = re.compile(
    r"(?i)(?<![\w.-])"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?![\w.-])")
_IPV4_RE = re.compile(
    r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_URL_RE = re.compile(r"(?i)(?:https?|ftp|ssh|git)://([^/\s?#]+)")
_AT_HOST_RE = re.compile(
    r"(?i)(?<![\w.])[A-Za-z0-9_.%+-]+@([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?)")
_SSH_HOST_RE = re.compile(
    r"(?i)(?:^|[\s(,，:：;；])(?:ssh|scp|sftp)\s+"
    r"(?:-(?:p|P)\s+\d+\s+|-[^\s]+\s+)*"
    r"(?:[A-Za-z0-9_.%+-]+@)?"
    r"([A-Za-z0-9_][A-Za-z0-9_.-]*)")
_HOST_KEYWORD_RE = re.compile(
    r"(?i)(?:host(?:name)?|主机(?:名)?)\s*[:：=]?\s*"
    r"([A-Za-z0-9_][A-Za-z0-9_.-]*)")
_PATH_RE = re.compile(
    r"(?<![\w.-])(?:(?:[~/]|(?:[A-Za-z]:[\\/]))"
    r"(?:[^ \t\r\n，。；;:：)]|(?<=\\) )+)")
_GIT_URL_RE = re.compile(
    r"(?i)(?:github\.com|gitlab\.com|gitee\.com|git@[^:\s]+:)"
    r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)(?:\.git)?")
_DOT_GIT_RE = re.compile(r"(?i)(?<![\w.-])([A-Za-z0-9][A-Za-z0-9_.-]*\.git)(?![\w.-])")
_REPO_KEYWORD_RE = re.compile(
    r"(?i)(?:仓库(?:名)?|项目(?:名)?|repo(?:sitory)?|开源(?:版|项目)?)\s*"
    r"[:：=]?\s*([A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)?)")
_DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _host_candidates(content: str) -> List[str]:
    """提取正文里可作为主机名检索对象的候选串。

    覆盖 URL 主机、DNS 域名、IPv4、ssh/scp 目标主机和 user@host 里的
    host 侧; 不把普通英文单词当作主机名, 避免 kind=host 退化成全文。
    """
    out: List[str] = []
    text = content or ""
    for m in _DNS_HOST_RE.finditer(text):
        out.append(m.group(0))
    for m in _IPV4_RE.finditer(text):
        out.append(m.group(0))
    for m in _URL_RE.finditer(text):
        host = m.group(1).rsplit(":", 1)[0].split("@")[-1]
        out.append(host)
    for m in _AT_HOST_RE.finditer(text):
        out.append(m.group(1))
    for m in _SSH_HOST_RE.finditer(text):
        host = m.group(1).split("@")[-1]
        out.append(host)
    for m in _HOST_KEYWORD_RE.finditer(text):
        out.append(m.group(1))
    # 保持候选顺序, 去重 (同一 IP 可能同时被 IP/URL/@host 命中)。
    seen = set()
    deduped: List[str] = []
    for c in out:
        if c and c not in seen:
            seen.add(c)
            deduped.append(c)
    return deduped


def _path_candidates(content: str) -> List[str]:
    out: List[str] = []
    for m in _PATH_RE.finditer(content or ""):
        candidate = m.group(0).strip()
        # 去掉结尾常见的标点/闭合括号, 但保留路径内合法字符。
        while candidate and candidate[-1] in ".,;:)]}，。；：":
            candidate = candidate[:-1]
        if candidate:
            out.append(candidate)
    return out


def _repo_candidates(content: str) -> List[str]:
    out: List[str] = []
    text = content or ""
    for m in _GIT_URL_RE.finditer(text):
        out.append(m.group(1))
    for m in _DOT_GIT_RE.finditer(text):
        out.append(m.group(1))
    for m in _REPO_KEYWORD_RE.finditer(text):
        out.append(m.group(1))
    return out


def _row_matches(row: Dict[str, Any], entity: str, kind: str) -> bool:
    content = "" if row.get("content") is None else str(row.get("content"))
    q = entity.casefold()
    if kind == "host":
        return any(q in c.casefold() for c in _host_candidates(content))
    if kind == "path":
        return any(q in c.casefold() for c in _path_candidates(content))
    if kind == "repo":
        return any(q in c.casefold() for c in _repo_candidates(content))
    if kind == "date":
        if not _DATE_ONLY_RE.match(entity):
            return False
        return _date_key(row.get("timestamp")) == entity
    # any (含非法 kind)
    return q in content.casefold()


def find(client: Any, entity: str, kind: str = "any", limit: int = 20) -> str:
    """``memorycore_find`` 检索内核: 冷层全量大小写不敏感子串匹配。

    kind 只决定把 ``entity`` 拿去和哪类对象比较; 非法 kind 一律按 any 处理。
    limit 只截断 matches, matched 始终是真实命中数。
    """
    t0 = time.perf_counter()
    entity = "" if entity is None else str(entity).strip()
    normalized_kind = (kind or "").strip().casefold()
    if normalized_kind not in {"any", "host", "path", "repo", "date"}:
        normalized_kind = "any"

    if not entity:
        return json.dumps({"error": "entity is empty"}, ensure_ascii=False)

    try:
        rows = _normalize_rows(client.list_all())
    except Exception as e:
        return json.dumps({"error": f"cold store unavailable: {e}"},
                          ensure_ascii=False)

    matches: List[Dict[str, str]] = []
    for row in rows:
        if _row_matches(row, entity, normalized_kind):
            matches.append({
                "id": _text(row.get("id")),
                "timestamp": _text(row.get("timestamp")),
                "topic": _topic(row.get("content"), 60),
            })

    matched = len(matches)
    out_limit = max(0, _safe_int(limit, 20))
    latency_ms = max(0, int(round((time.perf_counter() - t0) * 1000.0)))
    return json.dumps({
        "matches": matches[:out_limit] if out_limit else [],
        "scanned": len(rows),
        "matched": matched,
        "latency_ms": latency_ms,
    }, ensure_ascii=False)


# 供 server.py 使用的薄封装名 (保持依赖注入, 便于测试替换 store/client)。
memorycore_timeline_impl = timeline
memorycore_find_impl = find
