#!/usr/bin/env python3
"""Mnemosyne 原生 MCP server (FastMCP streamable-http, 单进程, 根治 supergateway 泄漏)
2026-08-02 建立 — 替代 supergateway 桥接 (mcp-mnemosyne.service)。

背景: supergateway 3.4.3 的 stateful 模式对 python stdio 子进程有泄漏 bug
(session 超时只删 transport 不 kill child → 每 5 分钟 healthcheck 泄漏 1 个 80MB 进程)。
根源方案: 直接 import mnemosyne 库, FastMCP 原生 streamable-http, 单进程服务所有请求。

工具面与 Mnemosyne 官方 MCP 对齐: remember / recall / update / forget / stats。
session 固定 mcp_default (与之前 MCP 工具写入统一, 不制造分裂)。
"""
import os
from typing import Optional

from mcp.server.fastmcp import FastMCP

# --- 环境: 与 supergateway 版一致 ---
os.environ.setdefault("MNEMOSYNE_DATA_DIR", "/var/lib/mnemosyne")
os.environ.setdefault("MNEMOSYNE_EMBEDDING_API_URL", "http://127.0.0.1:11434/v1")
os.environ.setdefault("MNEMOSYNE_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
os.environ.setdefault("MNEMOSYNE_EMBEDDING_API_KEY", "dummy")
os.environ.setdefault("MNEMOSYNE_VEC_TYPE", "float32")
# Defaults below are the values we run; every knob is overridable from the environment.

os.environ.setdefault("MNEMOSYNE_RECENCY_HALFLIFE", "8760")

# 统一 session (与历史 MCP 写入一致, 避免 session 分裂)
SESSION_ID = "mcp_default"

from mnemosyne import Mnemosyne  # noqa: E402
import json as _json  # 小项2: 统一 json 导入

mcp = FastMCP("mnemosyne")


def _mem() -> Mnemosyne:
    """单例实例 — FastMCP 进程内复用, 单进程不泄漏。"""
    return Mnemosyne(session_id=SESSION_ID)


@mcp.tool()
def remember(content: str, importance: float = 0.8, scope: str = "global") -> str:
    """Store a durable memory in Mnemosyne. Use for ANY fact that should persist
    across sessions: preferences, decisions, environment facts, config changes.
    Args:
        content: the fact to store (one sentence, Chinese, clear subject)
        importance: 0.0-1.0, how important (default 0.8)
        scope: 'global' (cross-session) or 'session' (default 'global')
    Returns: JSON with status + memory_id
    """
    try:
        mid = _mem().remember(content, importance=importance, scope=scope)
        return _json.dumps({"status": "stored", "memory_id": mid,
                            "content_preview": content[:60]}, ensure_ascii=False)
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def recall(query: str, top_k: int = 5, bump_recalled: bool = True) -> str:
    """Search Mnemosyne for relevant memories. Hybrid ranking (vector + FTS + importance).
    Args:
        query: natural language or keyword query (Chinese works well)
        top_k: number of results (default 5)
        bump_recalled: True (default) bumps recall_count/last_recalled on returned
            entries (user recall paths, e.g. prefetch plugin, rely on this);
            False = read-only recall, no side effects (governance-layer
            enumeration/prescreen uses this to avoid polluting last_recalled).
            P1-1 (2026-08-23).
    Returns: JSON with results (id, content, scores)
    """
    try:
        # 2026-08-05 大库修复: 内部放大候选召回再截断返回。
        # 原因: 底层向量候选池 k=max(top_k*3,20), 短查询+同主题高密度时
        # 详细重要记忆被相似短句挤出候选池, top_k=5 时 20 个候选全被填充占满。
        # 适配层放大内部 top_k (10x, 下限 300) 让目标进入候选, 再按分截断回请求的 top_k。
        _internal_top_k = max(top_k * 10, 300)
        # Ranking weights, overridable via the MNEMOSYNE_*_WEIGHT variables.
        _vw = float(os.environ.get("MNEMOSYNE_VEC_WEIGHT", "0.7"))
        _fw = float(os.environ.get("MNEMOSYNE_FTS_WEIGHT", "0.2"))
        _iw = float(os.environ.get("MNEMOSYNE_IMPORTANCE_WEIGHT", "0.1"))
        r = _mem().recall(query, top_k=_internal_top_k,
                          vec_weight=_vw, fts_weight=_fw, importance_weight=_iw,
                          bump_recalled=bump_recalled)
        out = []
        for m in (r or [])[:top_k]:
            if isinstance(m, dict):
                out.append({
                    "id": m.get("id"),
                    "content": m.get("content"),
                    "dense_score": round(m.get("dense_score", 0), 3),
                    "keyword_score": m.get("keyword_score", 0),
                    "fts_score": m.get("fts_score", 0),
                    "importance": m.get("importance"),
                    "last_recalled": m.get("last_recalled"),
                    "timestamp": m.get("timestamp"),
                })
        return _json.dumps({"status": "ok", "count": len(out), "results": out}, ensure_ascii=False)
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def update(memory_id: str, content: str, importance: Optional[float] = None) -> str:
    """Update the content of an existing memory by ID (in-place, no duplicate).
    Args:
        memory_id: the memory ID from recall
        content: new content
        importance: optional new importance
    Returns: JSON with status
    """
    try:
        ok = _mem().update(memory_id, content=content, importance=importance)
        status = "updated" if ok else "not_found"
        return _json.dumps({"status": status, "memory_id": memory_id}, ensure_ascii=False)
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def forget(memory_id: str) -> str:
    """Permanently delete a memory by ID.
    Args:
        memory_id: the memory ID from recall
    Returns: JSON with status
    """
    try:
        ok = _mem().forget(memory_id)
        status = "deleted" if ok else "not_found"
        return _json.dumps({"status": status, "memory_id": memory_id}, ensure_ascii=False)
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def stats() -> str:
    """Return Mnemosyne memory statistics: total count, session distribution."""
    try:
        all_mem = _mem().get_all_memories() or []
        import sqlite3
        conn = sqlite3.connect(os.path.join(os.environ.get("MNEMOSYNE_DATA_DIR", "/var/lib/mnemosyne"), "mnemosyne.db"))
        # 2026-08-23 口径修正: embeddings 只数 working+episodic 有效记忆的向量,
        # 与 total 同口径。此前 COUNT(*) 全表含 memories 旧表残留, vector_ok 恒 false。
        mem_ids = [m["id"] for m in all_mem]
        if mem_ids:
            ph = ",".join("?" * len(mem_ids))
            n_emb = conn.execute(
                f"SELECT COUNT(DISTINCT memory_id) FROM memory_embeddings WHERE memory_id IN ({ph})",
                mem_ids).fetchone()[0]
        else:
            n_emb = 0
        # 2026-09-18: 数据完整性上报 —— memories 里存在但不在 working_memory 的行
        # 对召回与治理都不可见（08-15 引入工作记忆层时 123 条历史行从未迁入，
        # 4 周后才被发现）。>0 即需要回填；周治理会把这一行写进报告。
        n_raw = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
        n_orphan = conn.execute(
            "SELECT COUNT(*) FROM memories m LEFT JOIN working_memory w "
            "ON m.id = w.id WHERE w.id IS NULL").fetchone()[0]
        conn.close()
        return _json.dumps({"total": len(all_mem), "embeddings": n_emb,
                            "memory_rows": n_raw, "orphan_rows": n_orphan},
                           ensure_ascii=False)
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)



@mcp.tool()
def list_all(limit: int = 500, offset: int = 0) -> str:
    """List all durable memories with timestamps (paginated).

    Args:
        limit: page size (default 500)
        offset: page offset (default 0)
    Returns: JSON with results [{id, content, timestamp, importance, last_recalled, superseded_by}, ...]
    """
    try:
        all_mem = _mem().get_all_memories() or []
        # Sort by timestamp ascending for stable pagination
        all_mem.sort(key=lambda m: str(m.get("timestamp", "")))
        page = all_mem[offset:offset + limit]
        out = [{
            "id": m.get("id"),
            "content": m.get("content"),
            "timestamp": m.get("timestamp"),
            "importance": m.get("importance"),
            "last_recalled": m.get("last_recalled"),
            "superseded_by": m.get("superseded_by"),
        } for m in page]
        return _json.dumps({"status": "ok", "count": len(out), "total": len(all_mem), "results": out}, ensure_ascii=False)
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)


@mcp.tool()
def embed_texts(texts: list[str]) -> str:
    """Batch-embed texts with the SAME model as recall (qwen3-embedding:0.6b,
    1024-dim float32, empty doc prefix -> same score space). Read-only, stateless.
    Args:
        texts: 1..64 strings to embed (each <= 2000 chars)
    Returns: JSON {"status":"ok","embeddings":[[f32,...],...]} (6-decimal) or error
    """
    if not texts or len(texts) > 64:
        return _json.dumps({"status": "error", "error": "1..64 texts required"}, ensure_ascii=False)
    try:
        from mnemosyne.core import embeddings as _emb
        vecs = _emb.embed(texts)
        if vecs is None:
            return _json.dumps({"status": "error", "error": "embedding unavailable"}, ensure_ascii=False)
        return _json.dumps({
            "status": "ok",
            "embeddings": [[round(float(x), 6) for x in v] for v in vecs],
        }, ensure_ascii=False)
    except Exception as e:
        return _json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)



if __name__ == "__main__":
    # 原生 streamable-http, 单进程复用, 不泄漏 (与 sqlite_mcp_server.py 同模式)
    mcp.settings.host = os.environ.get("MNEMOSYNE_HOST", "127.0.0.1")
    mcp.settings.port = int(os.environ.get("MNEMOSYNE_PORT", "8936"))
    _hosts = os.environ.get("MNEMOSYNE_ALLOWED_HOSTS", "127.0.0.1:*,localhost:*,[::1]:*")
    mcp.settings.transport_security.allowed_hosts = [h.strip() for h in _hosts.split(",") if h.strip()]
    mcp.settings.transport_security.enable_dns_rebinding_protection = False
    mcp.run(transport="streamable-http")
