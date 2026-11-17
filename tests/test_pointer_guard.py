"""指针内容守卫 (2026-11-17).

冷层里实测有 8 条"内容是纯指针"的污染记录。这组用例锁三件事:
1. 判据只认纯指针/指针串, 正文里提到指针格式不算;
2. 冷层写入口拒绝指针文本, 热层条目原样保留;
3. store_fact 直接拒绝。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core.guards import is_pointer_text  # noqa: E402
from memorycore.core.overflow import (  # noqa: E402
    _cold_write_with_dedup, _handle_cold_migration)


def test_pointer_text_shapes():
    assert is_pointer_text("[规则指针]某主题→recall:0123456789abcdef")
    assert is_pointer_text(
        "[规则指针]A→recall:0123456789abcdef。[规则指针]B→recall:fedcba9876543210")
    assert not is_pointer_text(
        "说明: 热层指针形如 [规则指针]某主题→recall:0123456789abcdef")
    assert not is_pointer_text(
        "[规则指针]这是正文, 只是举例 recall:0123456789abcdef 的写法")


def test_cold_write_rejects_pointer():
    st = "[规则指针]某主题→recall:0123456789abcdef"
    ok, detail = _cold_write_with_dedup(None, st)
    assert ok is False and detail == "pointer_not_content"


def test_cold_migration_keeps_pointer_local(tmp_store, mock_client):
    st = "[规则指针]某主题→recall:0123456789abcdef"
    tmp_store.add("memory", st)
    stat = {"errors": 0, "overflowed": 0, "pointer_as_content_blocked": 0}
    _handle_cold_migration(tmp_store, mock_client, "memory", st, stat)
    assert st in tmp_store.entries("memory"), "指针必须留在热层"
    assert stat["pointer_as_content_blocked"] == 1
    assert mock_client.stored == [] and mock_client.forgotten == []


def test_store_fact_rejects_pointer():
    import memorycore.server as server
    out = json.loads(server.memorycore_store_fact(
        "[规则指针]某主题→recall:0123456789abcdef"))
    assert out["status"] == "error"
    assert "pointer_not_content" in out["detail"]
