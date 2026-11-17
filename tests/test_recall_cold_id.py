#!/usr/bin/env python3
"""tests/test_recall_cold_id.py — recall 直接接受冷层 id。

背景：memorycore_find / memorycore_timeline 只返回冷层 id；recall 原先只认句柄，
于是"先用 find 缩范围、再按 id 取原文"这条工作流在第二步断掉（实测 16 位裸 id
报 handle not found）。本文件锁住 cold_id 直取兜底，并确认未知 id 仍然报错。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import memorycore.server as server  # noqa: E402


class _Client:
    """只读冷层替身：list_all 给 id→行；语义召回调用即失败。"""

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]

    def list_all(self):
        return [dict(r) for r in self.rows]

    def recall_results(self, query, top_k=5, bump=True):
        raise AssertionError("cold_id 直取不应走语义召回")


@pytest.fixture(autouse=True)
def _clean_cold_cache():
    server._COLD_CACHE["ts"] = 0.0
    server._COLD_CACHE["rows"] = {}
    yield
    server._COLD_CACHE["ts"] = 0.0
    server._COLD_CACHE["rows"] = {}


ROW = {
    "id": "549a1c5418a54a65",
    "content": "冷层原文内容",
    "timestamp": "2026-09-16T22:06:21.784756",
    "importance": 0.6,
}


def test_recall_accepts_bare_cold_id(tmp_store, monkeypatch):
    monkeypatch.setattr(server, "_store", tmp_store)
    monkeypatch.setattr(server, "_client", _Client([ROW]))
    out = json.loads(server.memorycore_recall(handle=ROW["id"]))
    assert out["mode"] == "cold_id"
    assert out["results"][0]["id"] == ROW["id"]
    assert out["results"][0]["content"] == ROW["content"]


def test_recall_accepts_cold_id_with_recall_prefix(tmp_store, monkeypatch):
    monkeypatch.setattr(server, "_store", tmp_store)
    monkeypatch.setattr(server, "_client", _Client([ROW]))
    out = json.loads(server.memorycore_recall(handle="recall:" + ROW["id"]))
    assert out["results"][0]["content"] == ROW["content"]


def test_recall_unknown_id_still_reports_handle_not_found(tmp_store, monkeypatch):
    monkeypatch.setattr(server, "_store", tmp_store)
    monkeypatch.setattr(server, "_client", _Client([ROW]))
    out = json.loads(server.memorycore_recall(handle="deadbeefdeadbeef"))
    assert "handle not found" in out["error"]
