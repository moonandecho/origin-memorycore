#!/usr/bin/env python3
"""tests/test_llm_breaker.py — R1: LLM 失败路径审计 + provider 持久熔断。

全部用例只用本地假 HTTP server, 不连接任何生产服务。breaker/audit 路径
逐用例指到 tmp_path, 绝不写 ~/.hermes/memorycore 或 logs/。
"""
from __future__ import annotations

import http.server
import json
import socketserver
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from memorycore.core import llm_breaker
from memorycore.core import llm_config
from memorycore.core import llm_judge


@pytest.fixture(autouse=True)
def _isolate_breaker(tmp_path, monkeypatch):
    """每个用例独立 breaker/audit 路径, 清进程内探测标记与 TTL 覆盖。"""
    monkeypatch.setenv("MEMORYCORE_LLM_BREAKER", "1")
    monkeypatch.setenv("MEMORYCORE_LLM_BREAKER_PATH",
                       str(tmp_path / "llm_breaker.json"))
    monkeypatch.setenv("MEMORYCORE_LLM_AUDIT_PATH",
                       str(tmp_path / "llm_failures.jsonl"))
    for name in ("MEMORYCORE_LLM_BREAKER_AUTH_FAILED_TTL",
                 "MEMORYCORE_LLM_BREAKER_TTL_AUTH_FAILED",
                 "MEMORYCORE_LLM_BREAKER_HTTP_ERROR_TTL",
                 "MEMORYCORE_LLM_BREAKER_TTL_HTTP_ERROR",
                 "MEMORYCORE_LLM_BREAKER_BAD_RESPONSE_TTL",
                 "MEMORYCORE_LLM_BREAKER_TTL_BAD_RESPONSE",
                 "MEMORYCORE_LLM_BREAKER_TIMEOUT_TTL",
                 "MEMORYCORE_LLM_BREAKER_TTL_TIMEOUT",
                 "MEMORYCORE_LLM_BREAKER_NETWORK_UNREACHABLE_TTL",
                 "MEMORYCORE_LLM_BREAKER_TTL_NETWORK_UNREACHABLE"):
        monkeypatch.delenv(name, raising=False)
    with llm_breaker._probe_lock:
        llm_breaker._probing.clear()
    yield


def _set_llm_env(monkeypatch, base_url="http://127.0.0.1:9"):
    monkeypatch.setenv("LLM_API_KEY", "sk-test-local")
    monkeypatch.setenv("LLM_BASE_URL", base_url)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    llm_config.invalidate_cache()


class _FastServer(socketserver.ThreadingTCPServer):
    """HTTPServer 跳过 getfqdn (本机反查 DNS 可能很慢)。"""
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    status = 200
    payload = b"{}"
    count = 0

    def do_POST(self):
        type(self).count += 1
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        body = type(self).payload
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextmanager
def counting_server(status=200, payload=b"{}"):
    handler = type("_Handler", (_CountingHandler,), {
        "status": status,
        "payload": payload,
        "count": 0,
    })
    srv = _FastServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield port, handler
    finally:
        srv.shutdown()
        t.join(timeout=5)


def _payload(content: str) -> bytes:
    return json.dumps({
        "choices": [{"message": {"content": content}}],
    }).encode()


# ---- 判据 2: 成功路径判定与快照计数不变 -------------------------------

def test_success_path_judge_and_snapshot_unchanged(monkeypatch):
    decision = json.dumps({
        "decision": "duplicate",
        "keep_id": "a",
        "discard_ids": ["b"],
        "reason": "same fact",
    })
    with counting_server(200, _payload(decision)) as (port, handler):
        _set_llm_env(monkeypatch, f"http://127.0.0.1:{port}")
        stat = {}
        with llm_config.guard_session(stat=stat) as g:
            result = llm_judge.judge_dedup([
                {"id": "a", "content": "数据库端口 5432"},
                {"id": "b", "content": "数据库端口 5432"},
            ])
            snap = g.snapshot()
    assert result is not None and result["decision"] == "duplicate"
    for field, expected in {
        "calls": 1,
        "success": 1,
        "failures": 0,
        "skipped_cap": 0,
        "skipped_backoff": 0,
    }.items():
        assert snap[field] == expected, (field, snap)
    assert stat["llm"]["status"] == "verified"
    assert handler.count == 1


# ---- 判据 3: 伪造 auth_failed 后冷却期内零网络 -------------------------

def test_breaker_blocks_acquire_and_zero_network(monkeypatch):
    with counting_server(401, b"{}") as (port, handler):
        _set_llm_env(monkeypatch, f"http://127.0.0.1:{port}")

        g = llm_config.LLMGuard(stat={}, max_calls=8)
        cfg = g.acquire("伪造失败")
        assert cfg is not None
        with pytest.raises(llm_config.LLMError):
            llm_config.chat(cfg, {"model": cfg.model, "messages": []},
                            timeout=2)
        g.note_failure("auth_failed", "HTTP 401")
        assert handler.count == 1

        # 新会话/新一轮 guard: 熔断命中, 全部 acquire 返回 None, 零新增请求。
        for i in range(5):
            g2 = llm_config.LLMGuard(stat={}, max_calls=8)
            assert g2.acquire(f"新轮{i}") is None
            assert g2.block_kind() == "breaker"
        assert handler.count == 1


# ---- 判据 4: TTL 到期后只放行一次探测 -----------------------------------

def test_breaker_expiry_allows_single_probe(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORYCORE_LLM_BREAKER_AUTH_FAILED_TTL", "1")
    # 直接伪造已过期桶, 零网络制造熔断状态。
    llm_breaker.record_failure("env:LLM_API_KEY", "deepseek-v4-flash",
                               "auth_failed")
    time.sleep(2.1)

    with counting_server(200, _payload("probe ok")) as (port, handler):
        _set_llm_env(monkeypatch, f"http://127.0.0.1:{port}")
        g = llm_config.LLMGuard(stat={}, max_calls=8)
        cfg = g.acquire("到期探测")
        assert cfg is not None, "到期后应放行一次探测"
        llm_config.chat(cfg, {"model": cfg.model, "messages": []}, timeout=2)
        g.note_success()
        assert handler.count == 1, "到期探测只能发出恰好 1 次真实请求"

        # 探测成功后该 source|model 的全部桶被清除, 后续不再拦截。
        g2 = llm_config.LLMGuard(stat={}, max_calls=8)
        assert g2.acquire("探测后") is not None

    state = json.loads(Path(tmp_path / "llm_breaker.json").read_text())
    assert not any(k.startswith("env:LLM_API_KEY|deepseek-v4-flash|")
                   for k in state), state


# ---- 判据 5: 状态文件降级安全 -------------------------------------------

@pytest.mark.parametrize("content", [
    None,                                   # 不存在
    "",                                     # 空文件
    "{broken json",                         # 非法 JSON
    '{"env:LLM_API_KEY|deepseek-v4-flash|auth_failed": %d}' % (
        time.time() + 3600),                # 未来时间戳
])
def test_breaker_degraded_state_never_blocks(monkeypatch, tmp_path, content):
    path = tmp_path / "llm_breaker.json"
    if content is None:
        path.unlink(missing_ok=True)
    else:
        path.write_text(content, encoding="utf-8")

    _set_llm_env(monkeypatch)
    g = llm_config.LLMGuard(stat={}, max_calls=8)
    assert g.acquire("降级安全") is not None, repr(content)
    assert g.block_kind() == ""


# ---- 判据 6: 失败审计 JSONL + 脱敏 -------------------------------------

def test_failure_audit_one_jsonl_line_redacted(monkeypatch, tmp_path):
    _set_llm_env(monkeypatch)
    g = llm_config.LLMGuard(stat={}, max_calls=8)
    cfg = g.acquire("冷层判定")
    assert cfg is not None
    g.note_failure(
        "auth_failed",
        "HTTP 401 sk-xxxx1234567890 Bearer abc.def tail",
        fallback_to="lexical",
    )

    audit = tmp_path / "llm_failures.jsonl"
    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, lines
    rec = json.loads(lines[0])
    for field in ("ts", "consumer", "category", "detail", "source",
                  "model", "round_calls", "round_failures",
                  "fallback_to"):
        assert field in rec, rec
    assert isinstance(rec["ts"], float)
    assert rec["consumer"] == "冷层判定"
    assert rec["category"] == "auth_failed"
    assert rec["source"] == "env:LLM_API_KEY"
    assert rec["model"] == "deepseek-v4-flash"
    assert rec["round_calls"] == 1
    assert rec["round_failures"] == 1
    assert rec["fallback_to"] == "lexical"
    assert len(rec["detail"]) <= 200
    assert "sk-xxxx" not in lines[0]
    assert "Bearer abc" not in lines[0]


def test_failure_audit_write_failure_is_fail_open(monkeypatch, tmp_path):
    parent_file = tmp_path / "not_a_dir"
    parent_file.write_text("x", encoding="utf-8")
    monkeypatch.setenv("MEMORYCORE_LLM_AUDIT_PATH",
                       str(parent_file / "audit.jsonl"))
    _set_llm_env(monkeypatch)

    g = llm_config.LLMGuard(stat={}, max_calls=8)
    assert g.acquire("压缩") is not None
    # 审计落盘失败只 warning, 绝不影响主流程计数/退避。
    g.note_failure("bad_response", "boom")
    assert g.failures == 1
    assert g.backoff is True


# ---- 判据 8: MEMORYCORE_LLM_BREAKER=0 完全关闭 -------------------------

def test_breaker_disabled_no_file_no_intercept(monkeypatch, tmp_path):
    monkeypatch.setenv("MEMORYCORE_LLM_BREAKER", "0")
    path = Path(tmp_path / "llm_breaker.json")
    assert not path.exists()

    with counting_server(401, b"{}") as (port, handler):
        _set_llm_env(monkeypatch, f"http://127.0.0.1:{port}")

        g = llm_config.LLMGuard(stat={}, max_calls=8)
        cfg = g.acquire("开关关闭")
        assert cfg is not None
        with pytest.raises(llm_config.LLMError):
            llm_config.chat(cfg, {"model": cfg.model, "messages": []},
                            timeout=2)
        g.note_failure("auth_failed", "HTTP 401")

        # 新轮不被熔断拦截; 真的再发一次请求证明零新增网络拦截。
        g2 = llm_config.LLMGuard(stat={}, max_calls=8)
        cfg2 = g2.acquire("开关关闭新轮")
        assert cfg2 is not None
        with pytest.raises(llm_config.LLMError):
            llm_config.chat(cfg2, {"model": cfg2.model, "messages": []},
                            timeout=2)
        g2.note_failure("auth_failed", "HTTP 401")
        assert handler.count == 2

    assert not path.exists()
    assert not Path(str(path) + ".lock").exists()
