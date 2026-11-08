#!/usr/bin/env python3
"""tests/test_work_state.py — R3 未完成事项补丁层验收。

覆盖 fold 归并语义 / render 格式与预算 / 容错 / 原子追加校验 /
插件注入去重与 P2 基线逐字节不变。
"""
import hashlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_ROOT = REPO_ROOT.with_name("mc-r3-baseline")
# 在 import 时固化真实家目录: 用例会 monkeypatch HOME 到临时目录,
# 到时再用 Path.home() 会指到临时 home, 找不到生产安装的插件。
_REAL_HOME = Path(os.path.expanduser("~"))
sys.path.insert(0, str(REPO_ROOT))

try:  # 生产布局: <root>/core
    from core import work_state as ws  # noqa: E402
except ImportError:  # 发布版包布局: <root>/memorycore/core
    from memorycore.core import work_state as ws  # noqa: E402


def _resolve_plugin(root: Path) -> Path:
    """插件定位: 工作副本 / 开源仓 / 生产安装三种布局都算候选。

    工作副本是 <root>/plugin, 开源仓是 <root>/hermes-plugin/memorycore-prefetch,
    生产安装是 ~/.hermes/plugins/memorycore-prefetch。测试在真机上跑时只
    有第三种存在, 写死单一布局会 FileNotFoundError。
    """
    env = os.environ.get("MEMORYCORE_PLUGIN_PATH", "").strip()
    candidates = [Path(env)] if env else []
    candidates += [
        root / "plugin" / "__init__.py",
        root / "hermes-plugin" / "memorycore-prefetch" / "__init__.py",
        _REAL_HOME / ".hermes" / "plugins" / "memorycore-prefetch" / "__init__.py",
    ]
    for cand in candidates:
        if cand.is_file():
            return cand
    raise FileNotFoundError(
        "plugin 未找到, 已尝试: " + ", ".join(str(c) for c in candidates))



def _line(ts, list_name, fields):
    return json.dumps({"ts": ts, "list": list_name, "fields": fields},
                      ensure_ascii=False)


def _write_state(tmp_path, *entries):
    path = tmp_path / "work_state.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        for entry in entries:
            if isinstance(entry, str):
                fh.write(entry + "\n")
            elif isinstance(entry, dict):
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            else:
                fh.write(_line(*entry) + "\n")
    return path


@pytest.fixture
def state_path(tmp_path, monkeypatch):
    path = tmp_path / "work_state.jsonl"
    monkeypatch.setenv("MEMORYCORE_WORK_STATE_PATH", str(path))
    return path


# ---------------------------------------------------------------------------
# fold 归并语义
# ---------------------------------------------------------------------------

def test_fold_latest_wins_and_null_clears(state_path):
    _write_state(
        state_path.parent,
        (10.0, "release", {"version": "1.0"}),
        (20.0, "release", {"version": "2.0", "owner": "echo"}),
        (30.0, "release", {"owner": None}),
    )
    view = ws.fold()
    lst = view["lists"]["release"]
    assert lst["fields"]["version"] == "2.0"
    assert "owner" not in lst["fields"]
    assert lst["updated_ts"] == 30.0


def test_fold_task_and_list_state_are_separate(state_path):
    _write_state(
        state_path.parent,
        (10.0, "memorycore-r3", {"status": "active", "milestone": "M1"}),
        (11.0, "memorycore-r3", {"task": "移植 work_state", "status": "doing"}),
    )
    view = ws.fold()
    lst = view["lists"]["memorycore-r3"]
    assert lst["fields"]["status"] == "active"
    assert lst["tasks"]["移植 work_state"]["fields"] == {"status": "doing"}
    assert "task" not in lst["fields"]


def test_fold_done_dropped_and_list_done_hidden(state_path):
    _write_state(
        state_path.parent,
        (10.0, "a", {"task": "t1", "status": "doing"}),
        (11.0, "a", {"task": "t2", "status": "doing"}),
        (12.0, "a", {"task": "t1", "status": "done"}),
        (13.0, "a", {"task": "t2", "status": "dropped"}),
        (14.0, "b", {"task": "t3", "status": "doing"}),
        (15.0, "b", {"status": "done"}),
    )
    view = ws.fold()
    assert "a" not in view["lists"], "任务全部完成后工作线不渲染"
    assert "b" not in view["lists"], "列表 status=done 整表隐藏 (含开放任务)"


def test_fold_same_ts_uses_line_order(state_path):
    # 同 ts: 后写的行获胜; 再晚一个 ts 的 done 会隐藏任务。
    _write_state(
        state_path.parent,
        (10.0, "w", {"task": "p", "status": "todo"}),
        (10.0, "w", {"task": "p", "status": "doing"}),
        (10.0, "w", {"status": "active"}),
        (10.0, "w", {"status": "blocked"}),
    )
    view = ws.fold()
    assert view["lists"]["w"]["fields"]["status"] == "blocked"
    assert view["lists"]["w"]["tasks"]["p"]["fields"]["status"] == "doing"


# ---------------------------------------------------------------------------
# render 格式 / 年龄 / 预算 / 空态
# ---------------------------------------------------------------------------

def test_render_exact_three_line_sample(state_path):
    _write_state(
        state_path.parent,
        (1000.0, "release", {"version": "13.25.3", "status": "active"}),
        (1000.0, "release", {"task": "publish", "status": "doing"}),
    )
    expected = (
        "- release: version=13.25.3, status=active, 5 分钟前更新\n"
        "  - [doing] publish, 5 分钟前更新"
    )
    assert ws.render(now_ts=1300.0) == expected


def test_render_age_four_tiers(state_path):
    now = 1_000_000.0
    _write_state(
        state_path.parent,
        (now - 59, "just", {"status": "active"}),
        (now - 60, "minutes", {"status": "active"}),
        (now - 3600, "hours", {"status": "active"}),
        (now - 86400, "days", {"status": "active"}),
    )
    text = ws.render(now_ts=now)
    assert "just: status=active, 刚刚更新" in text
    assert "minutes: status=active, 1 分钟前更新" in text
    assert "hours: status=active, 1 小时前更新" in text
    assert "days: status=active, 1 天前更新" in text


def test_render_budget_truncation_and_extra_hint(state_path):
    entries = [
        (1000.0 + i, "w", {"task": f"task-{i:02d}", "status": "doing"})
        for i in range(50)
    ]
    _write_state(state_path.parent, *entries)
    text = ws.render(now_ts=5000.0, max_chars=600)
    assert len(text) <= 600
    assert "(另有" in text and "memorycore_work_state" in text


def test_render_empty_returns_empty_string(state_path, tmp_path):
    assert ws.render() == ""

    missing = tmp_path / "missing.jsonl"
    os.environ["MEMORYCORE_WORK_STATE_PATH"] = str(missing)
    try:
        assert ws.render() == ""
        assert ws.fold() == {"lists": {}}
    finally:
        os.environ["MEMORYCORE_WORK_STATE_PATH"] = str(state_path)


# ---------------------------------------------------------------------------
# 容错
# ---------------------------------------------------------------------------

def test_fold_skips_bad_json_line_and_keeps_rest(state_path):
    _write_state(
        state_path.parent,
        "{this is not json",
        (10.0, "ok", {"status": "active"}),
        {"ts": 11.0, "list": "ok", "fields": "not-a-dict"},
    )
    view = ws.fold()
    assert view["lists"]["ok"]["fields"]["status"] == "active"
    # 坏行绝不抛异常; render 同样不抛。
    assert "ok: status=active" in ws.render(now_ts=10.0)


def test_fold_missing_file_no_write(state_path):
    assert state_path.exists() is False
    assert ws.render() == ""
    assert ws.fold() == {"lists": {}}
    assert state_path.exists() is False, "读路径容错不得创建/写文件"


# ---------------------------------------------------------------------------
# append 校验与原子追加
# ---------------------------------------------------------------------------

def test_append_rejects_fields_over_2000_and_file_unchanged(state_path):
    fields = {"note": "x" * 2001}
    with pytest.raises(ValueError):
        ws.append("w", fields)
    assert not state_path.exists(), "超限拒绝必须不落盘"

    _write_state(state_path.parent, (1.0, "w", {"status": "active"}))
    before = len(state_path.read_text(encoding="utf-8").splitlines())
    with pytest.raises(ValueError):
        ws.append("w", fields)
    assert len(state_path.read_text(encoding="utf-8").splitlines()) == before


def test_append_rejects_empty_or_non_string_list_name(state_path):
    for bad in ("", "   ", None, 7, ["x"]):
        with pytest.raises(ValueError):
            ws.append(bad, {"status": "active"})
    assert not state_path.exists()


def test_append_persists_jsonl_line(state_path):
    ws.append("w", {"status": "active"})
    lines = state_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["list"] == "w"
    assert obj["fields"] == {"status": "active"}
    assert isinstance(obj["ts"], float)


# ---------------------------------------------------------------------------
# 插件注入: 存在时追加 ## 未完成事项; 不存在时与基线逐字节相同
# ---------------------------------------------------------------------------

_SUBPROCESS_HASH = r'''
import hashlib, importlib.util, os, sys, types
root, plugin_file, state_path = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["MEMORYCORE_WORK_STATE_PATH"] = state_path
os.environ["HOME"] = sys.argv[4]
agent = types.ModuleType("agent"); agent.__path__ = []
prov = types.ModuleType("agent.memory_provider")
class MemoryProvider:
    def __init__(self, *a, **k): pass
prov.MemoryProvider = MemoryProvider
import re
prov.TRIVIAL_PROMPT_RE = re.compile(r"^(?:yes|no|ok)$")
def is_trivial_prompt(q):
    q = (q or "").strip()
    return not q or q.startswith("/")
prov.is_trivial_prompt = is_trivial_prompt
sys.modules["agent"] = agent
sys.modules["agent.memory_provider"] = prov
sys.path.insert(0, root)
spec = importlib.util.spec_from_file_location("ws_plugin_under_test", plugin_file)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
text = mod.MemoryCorePrefetchProvider().system_prompt_block()
print(hashlib.sha256(text.encode("utf-8")).hexdigest())
'''

_WS_PLUGIN_HASH_SCRIPT = _SUBPROCESS_HASH


def _run_plugin_hash(root, plugin_file, state_path, tmp_home):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = subprocess.run(
        [sys.executable, "-c", _WS_PLUGIN_HASH_SCRIPT,
         str(root), str(plugin_file), str(state_path), str(tmp_home)],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip().splitlines()[-1]


def test_plugin_work_state_block_present_when_file_exists(tmp_path, monkeypatch):
    state_path = tmp_path / "work_state.jsonl"
    monkeypatch.setenv("MEMORYCORE_WORK_STATE_PATH", str(state_path))
    ws.append("memorycore-r3", {"status": "active"})
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ws_plugin_current", _resolve_plugin(REPO_ROOT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    provider = mod.MemoryCorePrefetchProvider()
    provider._format_directory = lambda: ""
    text = provider.system_prompt_block()
    assert "## 未完成事项" in text
    assert "memorycore-r3: status=active" in text


def test_plugin_no_work_state_file_identical_to_baseline(tmp_path):
    if not BASELINE_ROOT.exists():
        pytest.skip("缺少基线目录 mc-r3-baseline")
    state_path = tmp_path / "no-work-state.jsonl"
    tmp_home = tmp_path / "empty-home"
    tmp_home.mkdir()
    current_hash = _run_plugin_hash(
        REPO_ROOT, _resolve_plugin(REPO_ROOT), state_path, tmp_home)
    baseline_hash = _run_plugin_hash(
        BASELINE_ROOT, _resolve_plugin(BASELINE_ROOT),
        state_path, tmp_home)
    assert current_hash == baseline_hash

def test_plugin_work_state_dedup_and_combined_budget(tmp_path, monkeypatch):
    state_path = tmp_path / "work_state.jsonl"
    monkeypatch.setenv("MEMORYCORE_WORK_STATE_PATH", str(state_path))
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    ws.append("w", {"task": "hide", "status": "doing"})
    ws.append("w", {"task": "keep", "status": "doing"})

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "ws_plugin_current_dedup", _resolve_plugin(REPO_ROOT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    provider = mod.MemoryCorePrefetchProvider()
    provider._format_directory = lambda: ""
    # 与热层全文重复的行不注入; 其余行照常注入。
    provider._hot_norm = mod.normalize_for_compare("  - [doing] hide, 刚刚更新")
    text = provider.system_prompt_block()
    assert "## 未完成事项" in text
    assert "[doing] keep" in text
    assert "[doing] hide" not in text
    assert len(text) <= 1400


def test_server_work_state_tool_returns_open_json(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORYCORE_WORK_STATE_PATH",
                       str(tmp_path / "work_state.jsonl"))
    sys.path.insert(0, str(REPO_ROOT))
    try:  # 生产布局: <root>/server.py
        import server
    except ImportError:  # 发布版包布局: memorycore/server.py
        from memorycore import server
    out = server.memorycore_work_state(
        "memorycore-r3", {"task": "移植 work_state", "status": "doing"})
    data = json.loads(out)
    assert "lists" in data
    assert data["lists"][0]["list"] == "memorycore-r3"
    assert data["lists"][0]["tasks"][0]["task"] == "移植 work_state"
    assert "updated" in data["lists"][0]

