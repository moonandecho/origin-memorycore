#!/usr/bin/env python3
"""tests/test_llm_breaker_r1b.py — R1b 纠偏回归: 默认关闭 + 审计路径锚定。

基线 (未含 core/llm_breaker.py 的副本) 上本文件应收集/执行失败:
import core.llm_breaker 报 ImportError, 证明这些行为确实来自本工作副本。
"""
from __future__ import annotations

from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from memorycore.core import llm_breaker
from memorycore.core import llm_config


@pytest.mark.parametrize("raw, expected", [
    (None, False),
    ("", False),
    ("0", False),
    ("false", False),
    ("off", False),
    ("no", False),
    ("1", True),
    ("true", True),
    ("on", True),
    ("yes", True),
    (" TRUE ", True),
    (" On ", True),
])
def test_enabled_is_opt_in(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("MEMORYCORE_LLM_BREAKER", raising=False)
    else:
        monkeypatch.setenv("MEMORYCORE_LLM_BREAKER", raw)
    assert llm_breaker.enabled() is expected


def test_default_off_is_noop_no_state_file_no_intercept(monkeypatch, tmp_path):
    """未设开关时: 失败后既不落状态文件, 新 guard 也不被 breaker 拦截。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("MEMORYCORE_LLM_BREAKER", raising=False)
    monkeypatch.delenv("MEMORYCORE_LLM_BREAKER_PATH", raising=False)
    monkeypatch.setenv("MEMORYCORE_LLM_AUDIT_PATH",
                       str(tmp_path / "audit.jsonl"))
    monkeypatch.setenv("LLM_API_KEY", "sk-local-r1b")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.delenv("LLM_MODEL", raising=False)
    llm_config.invalidate_cache()

    assert llm_breaker.enabled() is False
    g1 = llm_config.LLMGuard(stat={}, max_calls=8)
    cfg1 = g1.acquire("默认关闭首轮")
    assert cfg1 is not None
    g1.note_failure("auth_failed", "fake local failure")

    g2 = llm_config.LLMGuard(stat={}, max_calls=8)
    cfg2 = g2.acquire("默认关闭次轮")
    assert cfg2 is not None
    assert g2.block_kind() != "breaker"
    assert g2.skipped_breaker == 0

    state = home / ".hermes" / "memorycore" / "llm_breaker.json"
    lock = home / ".hermes" / "memorycore" / "llm_breaker.json.lock"
    assert not state.exists(), state
    assert not lock.exists(), lock


def test_audit_path_anchored_to_memorycore_root(monkeypatch, tmp_path):
    """缺省审计路径必须锚定 MemoryCore 根目录, 不随 CWD 漂移。"""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("MEMORYCORE_LLM_AUDIT_PATH", raising=False)

    expected = (Path(llm_config.__file__).resolve().parent.parent
                / "logs" / "llm_failures.jsonl")
    assert llm_config._audit_path() == expected

    override = tmp_path / "override.jsonl"
    monkeypatch.setenv("MEMORYCORE_LLM_AUDIT_PATH", str(override))
    assert llm_config._audit_path() == override
