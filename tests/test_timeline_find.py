#!/usr/bin/env python3
"""tests/test_timeline_find.py — 只读检索工具 memorycore_find / timeline 单测。

全部走内存 mock + tmp_store, 不访问真实冷层、不访问生产 memories 目录。
"""
import json
from pathlib import Path

import pytest

from memorycore.core.metadata import MetaStore
from memorycore.core.timeline import find, timeline


class ReadOnlyMockClient:
    """最小冷层 mock: 只提供 list_all; 写/召回接口调用即测试失败。"""

    def __init__(self, rows):
        self.rows = [dict(r) for r in rows]
        self.list_all_calls = 0

    def list_all(self):
        self.list_all_calls += 1
        return [dict(r) for r in self.rows]

    def stats(self):
        return {"total": len(self.rows)}

    def recall_results(self, *args, **kwargs):
        raise AssertionError("timeline/find must not call recall_results")

    def remember(self, *args, **kwargs):
        raise AssertionError("timeline/find must not call remember")

    def update(self, *args, **kwargs):
        raise AssertionError("timeline/find must not call update")

    def forget(self, *args, **kwargs):
        raise AssertionError("timeline/find must not call forget")


# ---------------------------------------------------------------------------
# memorycore_find
# ---------------------------------------------------------------------------

FIND_ROWS = [
    {
        "id": "r1",
        "content": "服务器 build.example.internal 挂载 /srv/work;\n"
                   "主仓库 example-project 在 github.com/example/example-project.git",
        "timestamp": "2026-11-17T10:00:00",
    },
    {
        "id": "r2",
        "content": "与 buildhost 无关的普通备注",
        "timestamp": "2026-11-16T09:00:00",
    },
]


def test_find_any_case_insensitive_and_topic_capped():
    client = ReadOnlyMockClient(FIND_ROWS)
    out = json.loads(find(client, "EXAMPLE.INTERNAL", kind="any"))
    assert out["scanned"] == 2
    assert out["matched"] == 1
    assert [m["id"] for m in out["matches"]] == ["r1"]
    assert out["matches"][0]["topic"] == "服务器 build.example.internal 挂载 /srv/work;主仓库 example-project 在 githu"[:60]
    assert "content" not in out["matches"][0]
    assert out["latency_ms"] >= 0


def test_find_limit_truncates_but_matched_is_real():
    rows = [
        {"id": f"r{i}", "content": "共同关键词 echo", "timestamp": "2026-11-17T10:00:00"}
        for i in range(5)
    ]
    out = json.loads(find(ReadOnlyMockClient(rows), "echo", limit=2))
    assert len(out["matches"]) == 2
    assert out["matched"] == 5
    assert out["scanned"] == 5


def test_find_kinds_host_path_repo_date():
    client = ReadOnlyMockClient(FIND_ROWS)
    host = json.loads(find(client, "example.internal", kind="host"))
    assert [m["id"] for m in host["matches"]] == ["r1"]

    path = json.loads(find(client, "/srv/work", kind="path"))
    assert [m["id"] for m in path["matches"]] == ["r1"]

    repo = json.loads(find(client, "example-project", kind="repo"))
    assert [m["id"] for m in repo["matches"]] == ["r1"]

    date = json.loads(find(client, "2026-11-16", kind="date"))
    assert [m["id"] for m in date["matches"]] == ["r2"]

    # kind=host 不应把普通正文词当主机名。
    host_word = json.loads(find(client, "普通备注", kind="host"))
    assert host_word["matches"] == []


def test_find_illegal_kind_falls_back_to_any():
    out = json.loads(find(ReadOnlyMockClient(FIND_ROWS), "普通备注", kind="bogus"))
    assert [m["id"] for m in out["matches"]] == ["r2"]


# ---------------------------------------------------------------------------
# memorycore_timeline
# ---------------------------------------------------------------------------

def _mk(rid, day, hour, minute=0, content=None):
    return {
        "id": rid,
        "content": content if content is not None else f"内容 {rid}",
        "timestamp": f"{day}T{hour:02d}:{minute:02d}:00",
    }


def test_timeline_id_center_before_after_order_and_weak():
    rows = [
        _mk("old", "2026-01-09", 12),
        _mk("b3", "2026-01-10", 8),
        _mk("b2", "2026-01-10", 10, 30),
        _mk("b1", "2026-01-10", 11, 59),
        _mk("center", "2026-01-10", 12, content="第 1 行\n第 2 行" + "很长的正文" * 20),
        _mk("a1", "2026-01-10", 12, 1),
        _mk("a2", "2026-01-10", 13),
        _mk("a3", "2026-01-11", 11, 59),
    ]
    client = ReadOnlyMockClient(rows)
    out = json.loads(timeline(None, client, None, id="center", before=2, after=2))
    assert out["center"]["id"] == "center"
    assert [r["id"] for r in out["before"]] == ["b2", "b1"]
    assert [r["id"] for r in out["after"]] == ["a1", "a2"]
    for segment in ("before", "after"):
        stamps = [r["timestamp"] for r in out[segment]]
        assert stamps == sorted(stamps)
    assert out["before"][-1]["timestamp"] < out["center"]["timestamp"] < out["after"][0]["timestamp"]
    assert out["weak"] is False
    assert out["scanned"] == 8
    # center + b1/b2/b3/a1/a2 都是 2026-01-10, a3 是 01-11
    assert out["same_day_count"] == 6


def test_timeline_window_hours_limits_neighbors():
    rows = [
        _mk("far_before", "2026-01-10", 10),
        _mk("near_before", "2026-01-10", 11, 30),
        _mk("center", "2026-01-10", 12),
        _mk("near_after", "2026-01-10", 12, 30),
        _mk("far_after", "2026-01-10", 14),
    ]
    out = json.loads(timeline(None, ReadOnlyMockClient(rows), None, id="center",
                              window_hours=1, before=5, after=5))
    assert [r["id"] for r in out["before"]] == ["near_before"]
    assert [r["id"] for r in out["after"]] == ["near_after"]
    assert out["weak"] is False


@pytest.mark.parametrize("neighbor_count", [0, 1])
def test_timeline_weak_true_when_less_than_two_neighbors(neighbor_count):
    rows = [_mk("center", "2026-01-10", 12)]
    for i in range(neighbor_count):
        rows.append(_mk(f"n{i}", "2026-01-10", 12, 30 + i))
    out = json.loads(timeline(None, ReadOnlyMockClient(rows), None, id="center"))
    assert (len(out["before"]) + len(out["after"])) == neighbor_count
    assert out["weak"] is True


def test_timeline_topic_max_chars_and_no_full_text():
    body = "标题行\n" + "x" * 200
    row = _mk("center", "2026-01-10", 12, content=body)
    out = json.loads(timeline(None, ReadOnlyMockClient([row]), None,
                              id="center", max_chars=17))
    assert out["center"]["topic"] == "标题行" + "x" * (17 - 3)
    assert "content" not in out["center"]
    assert len(out["center"]["topic"]) == 17
    assert "\n" not in out["center"]["topic"]


def test_timeline_argument_errors():
    client = ReadOnlyMockClient([_mk("center", "2026-01-10", 12)])
    both_empty = json.loads(timeline(None, client, None))
    assert "error" in both_empty
    both_given = json.loads(timeline(None, client, None, handle="h", id="center"))
    assert "error" in both_given
    not_found = json.loads(timeline(None, client, None, id="missing"))
    assert "id not found" in not_found["error"]
    assert client.list_all_calls == 0 or both_empty.get("scanned") is None


def test_timeline_handle_uses_meta_stub_resolution(tmp_store):
    entry = "[规则指针]删除强制走回收站→recall:deadbeef12345678"
    assert tmp_store.add("memory", entry)["success"] is True
    meta = MetaStore("memory", memory_path=tmp_store.memory_path,
                     user_path=tmp_store.user_path)
    meta.stamp(entry, "stub", cold_id="center", handle="#1234abcd")

    rows = [
        _mk("center", "2026-01-10", 12, content="删除必须走回收站"),
        _mk("before", "2026-01-10", 11),
        _mk("after", "2026-01-10", 13),
    ]
    out = json.loads(timeline(tmp_store, ReadOnlyMockClient(rows),
                              lambda t: MetaStore(t, memory_path=tmp_store.memory_path,
                                                  user_path=tmp_store.user_path),
                              handle="#1234abcd", before=1, after=1))
    assert out["center"]["id"] == "center"
    assert [r["id"] for r in out["before"]] == ["before"]
    assert [r["id"] for r in out["after"]] == ["after"]

    legacy_out = json.loads(timeline(
        tmp_store, ReadOnlyMockClient(rows),
        lambda t: MetaStore(t, memory_path=tmp_store.memory_path,
                            user_path=tmp_store.user_path),
        handle="recall:deadbeef12345678", before=1, after=1))
    assert legacy_out["center"]["id"] == "center"


def test_timeline_id_lookup_does_not_touch_handle_resolution():
    rows = [_mk("center", "2026-01-10", 12)]
    out = json.loads(timeline(None, ReadOnlyMockClient(rows), None, id="center"))
    assert out["center"]["id"] == "center"
    assert out["scanned"] == 1


def test_server_registers_both_tools_and_wrappers_pass_dependencies(monkeypatch):
    import memorycore.server as server
    assert hasattr(server, "memorycore_timeline")
    assert hasattr(server, "memorycore_find")
    monkeypatch.setattr(server, "_client", ReadOnlyMockClient(FIND_ROWS))
    out = json.loads(server.memorycore_find("EXAMPLE.INTERNAL", kind="any", limit=1))
    assert out["matched"] == 1 and len(out["matches"]) == 1


def test_find_empty_entity_returns_error_without_scanning():
    client = ReadOnlyMockClient(FIND_ROWS)
    out = json.loads(find(client, "   "))
    assert out.get("error") == "entity is empty"
    assert client.list_all_calls == 0
