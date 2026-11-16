#!/usr/bin/env python3
"""tests/test_handle_cold_fetch.py — 句柄按 cold_id 直取原文 (功能扩展)。

覆盖:
  ① handle 直查命中 cold_id → 直接回原文, 不走语义召回, 不回写热层;
  ② cold_id 取不到 → 退化成原来的主题词语义召回 (行为不变);
  ③ memorycore_stub_index → 列出 handle/主题/cold_id/可解析;
  ④ 直取路径不调用 restore_stubs_from_results (不挤占热层预算)。

全部内存 mock, 不访问网络/生产冷层。
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memorycore import server  # noqa: E402

STUB = "[规则指针]主题戊→recall:0123456789abcdef"
COLD_ID = "cold-e5"
HANDLE = "#e5e5e5e5"
FULL_TEXT = "完整正文: 主题戊的规则全部内容, 供句柄展开使用。"


class _DirectClient:
    """list_all 有原文; recall_results 被调用就失败 (证明没走语义)。"""

    def __init__(self, rows):
        self.rows = rows

    def list_all(self):
        return [dict(r) for r in self.rows]

    def recall_results(self, query, top_k=5, bump=True):
        raise AssertionError("直取命中时不应调用语义召回")


class _FallbackClient:
    """list_all 为空; recall_results 返回一条候选 (旧路径)。"""

    def __init__(self, items):
        self.items = items

    def list_all(self):
        return []

    def recall_results(self, query, top_k=5, bump=True):
        return [dict(it) for it in self.items]


@pytest.fixture(autouse=True)
def _reset_cold_cache():
    server._COLD_CACHE.update({"ts": 0.0, "rows": {}})
    yield
    server._COLD_CACHE.update({"ts": 0.0, "rows": {}})


def _seed_stub(tmp_store, meta_for):
    tmp_store.add("memory", STUB)
    meta_for("memory").stamp(STUB, "stub", origin="stub_sink",
                             cold_id=COLD_ID, handle=HANDLE)


def test_handle_direct_fetch_returns_cold_text(
        tmp_store, meta_for, monkeypatch):
    _seed_stub(tmp_store, meta_for)
    monkeypatch.setattr(server, "_store", tmp_store)
    monkeypatch.setattr(server, "_client", _DirectClient([
        {"id": COLD_ID, "content": FULL_TEXT, "importance": 0.7},
    ]))
    response = json.loads(server.memorycore_recall("", top_k=3, handle=HANDLE))

    assert response["mode"] == "handle"
    assert response["page_fault"] is True
    assert len(response["results"]) == 1
    assert response["results"][0]["id"] == COLD_ID
    assert response["results"][0]["content"] == FULL_TEXT
    assert response["results"][0]["channel"] == "H"


def test_handle_falls_back_to_topic_when_cold_missing(
        tmp_store, meta_for, monkeypatch):
    _seed_stub(tmp_store, meta_for)
    monkeypatch.setattr(server, "_store", tmp_store)
    monkeypatch.setattr(server, "_client", _FallbackClient([
        {"id": "other-1", "content": "主题戊相关但不同的条目",
         "dense_score": 0.6, "keyword_score": 0.5, "fts_score": 0.5},
    ]))
    response = json.loads(server.memorycore_recall("", top_k=3, handle=HANDLE))

    assert response["mode"] == "handle"
    assert [r["id"] for r in response["results"]] == ["other-1"]


def test_stub_index_lists_handle_and_resolvable(
        tmp_store, meta_for, monkeypatch):
    _seed_stub(tmp_store, meta_for)
    monkeypatch.setattr(server, "_store", tmp_store)
    monkeypatch.setattr(server, "_client", _DirectClient([
        {"id": COLD_ID, "content": FULL_TEXT},
    ]))
    payload = json.loads(server.memorycore_stub_index())

    assert payload["count"] == 1
    assert payload["resolvable"] == 1
    row = payload["rows"][0]
    assert row["handle"] == HANDLE
    assert row["cold_id"] == COLD_ID
    assert row["title"] == FULL_TEXT[:40]
    assert row["cold_chars"] == len(FULL_TEXT)


def test_direct_fetch_skips_hot_layer_restore(
        tmp_store, meta_for, monkeypatch):
    _seed_stub(tmp_store, meta_for)
    monkeypatch.setattr(server, "_store", tmp_store)
    monkeypatch.setattr(server, "_client", _DirectClient([
        {"id": COLD_ID, "content": FULL_TEXT},
    ]))
    called = []

    def _boom(store, metas, results):
        called.append(True)
        raise AssertionError("直取路径不应调用 restore")

    monkeypatch.setattr(server, "restore_stubs_from_results", _boom)
    response = json.loads(server.memorycore_recall("", top_k=3, handle=HANDLE))

    assert called == []
    assert response["results"][0]["content"] == FULL_TEXT
