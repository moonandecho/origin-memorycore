"""指针墓碑归档 (2026-11-17).

锁三件事:
1. 退役的指针先写进 retired_pointers.jsonl 再删热行, 条数一一对应;
2. 归档写失败时指针必须留在热层 (不允许"删了但没记录");
3. 开关 MEMORYCORE_POINTER_ARCHIVE=0 时恢复旧行为 (只删, 不写归档)。
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core import overflow as ov  # noqa: E402
from memorycore.core.config import MAX_STUB_PER_RUN, STUB_PREFIX  # noqa: E402
from memorycore.core.overflow import run_overflow  # noqa: E402


def _fill_to(tmp_store, target, pct):
    limit = 5000
    want = int(limit * pct) + 30
    need = max(0, want - tmp_store.char_count(target))
    if need:
        tmp_store.add(target, "填充条目" + "甲" * need)


def _stamp_stub(meta_for, entry, days, target="memory"):
    meta_for(target).stamp(
        entry, "stub",
        updated_at=datetime.now(timezone.utc) - timedelta(days=days),
        origin="stub_sink")


def _disable_unified_budget(monkeypatch):
    monkeypatch.setattr(ov, "enforce_rule_budget", lambda *a, **k: None)


@pytest.fixture
def archive_path(tmp_path, monkeypatch):
    p = tmp_path / "retired_pointers.jsonl"
    monkeypatch.setattr(ov, "POINTER_ARCHIVE_FILE", p)
    monkeypatch.setattr(ov, "POINTER_ARCHIVE_ENABLED", True)
    return p


def _prepare_stubs(tmp_store, meta_for):
    topics = ["自托管选型", "服务器运维", "写作风格", "安全审计", "硬件采购"]
    stubs = []
    for i, kw in enumerate(topics):
        s = f"{STUB_PREFIX}{kw}→recall({kw})"
        tmp_store.add("memory", s)
        _stamp_stub(meta_for, s, days=10 + i)
        stubs.append(s)
    _fill_to(tmp_store, "memory", 0.82)
    filler = max(tmp_store.entries("memory"), key=len)
    meta_for("memory").stamp(filler, "state", origin="test_filler")
    return stubs


def test_archive_written_before_removal(tmp_store, mock_client, meta_for,
                                        monkeypatch, archive_path):
    _disable_unified_budget(monkeypatch)
    _prepare_stubs(tmp_store, meta_for)

    stat = run_overflow(tmp_store, mock_client, "memory")

    assert 1 <= stat["stub_gc"] <= MAX_STUB_PER_RUN, stat
    assert stat["pointer_retired"] == stat["stub_gc"], stat
    lines = archive_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == stat["stub_gc"]
    rec = json.loads(lines[0])
    for key in ("event", "at", "target", "topic", "reason", "stub"):
        assert key in rec, rec
    assert rec["event"] == "retired" and rec["reason"] == "pressure"
    ents = tmp_store.entries("memory")
    for line in lines:
        assert json.loads(line)["stub"] not in ents
    assert mock_client.forgotten == [] and mock_client.stored == []


def test_archive_failure_keeps_pointer(tmp_store, mock_client, meta_for,
                                       monkeypatch, archive_path):
    _disable_unified_budget(monkeypatch)
    _prepare_stubs(tmp_store, meta_for)
    before = list(tmp_store.entries("memory"))

    monkeypatch.setattr(ov, "_pointer_archive_append", lambda *a, **k: False)
    stat = run_overflow(tmp_store, mock_client, "memory")

    assert stat["stub_gc"] == 0, stat
    assert sorted(tmp_store.entries("memory")) == sorted(before)
    assert not archive_path.exists()


def test_archive_disabled_restores_old_behavior(tmp_store, mock_client,
                                                meta_for, monkeypatch,
                                                archive_path):
    _disable_unified_budget(monkeypatch)
    _prepare_stubs(tmp_store, meta_for)
    monkeypatch.setattr(ov, "POINTER_ARCHIVE_ENABLED", False)

    stat = run_overflow(tmp_store, mock_client, "memory")

    assert 1 <= stat["stub_gc"] <= MAX_STUB_PER_RUN, stat
    assert stat["pointer_retired"] == 0, stat
    assert not archive_path.exists()
