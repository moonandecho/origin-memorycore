#!/usr/bin/env python3
"""core/llm_breaker.py — LLM provider 持久熔断 (R1).

设计:
  - 状态文件: MEMORYCORE_LLM_BREAKER_PATH, 缺省
    ~/.hermes/memorycore/llm_breaker.json。
  - JSON 对象, 键 "source|model|category", 值为过期 epoch (浮点)。
  - acquire 前调用 check_acquire(): active bucket -> "blocked" (零网络);
    全部过期 -> 放行一次探测并在进程内标记 probing (绝不落盘),
    避免并发重复探测; 探测成功 clear() / 失败 record_failure()。
  - 读取降级: 文件不存在/空/损坏/未来时间戳 -> 视为无熔断, 不崩溃。
  - 开关 (opt-in): 默认关闭。只有 MEMORYCORE_LLM_BREAKER 取值在
    {"1","true","on","yes"} (大小写不敏感, 去首尾空白) 时才启用;
    其它值 (含未设置、空串、"0") 一律关闭。关闭时全部函数为 no-op:
    不读、不写状态文件, 不拦截任何 acquire, skipped_breaker 保持 0。
"""
from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Set

log = logging.getLogger("memorycore.llm_breaker")

# 冷却时长常量 (R1)。测试可用模块级 monkeypatch 或下方环境变量覆盖。
AUTH_FAILED_TTL = 1800.0
HTTP_ERROR_TTL = 600.0
BAD_RESPONSE_TTL = 600.0
TIMEOUT_TTL = 120.0
NETWORK_UNREACHABLE_TTL = 120.0

DEFAULT_COOLDOWNS = {
    "auth_failed": AUTH_FAILED_TTL,
    "http_error": HTTP_ERROR_TTL,
    "bad_response": BAD_RESPONSE_TTL,
    "timeout": TIMEOUT_TTL,
    "network_unreachable": NETWORK_UNREACHABLE_TTL,
}

_COOLDOWN_ATTRS = {
    "auth_failed": "AUTH_FAILED_TTL",
    "http_error": "HTTP_ERROR_TTL",
    "bad_response": "BAD_RESPONSE_TTL",
    "timeout": "TIMEOUT_TTL",
    "network_unreachable": "NETWORK_UNREACHABLE_TTL",
}

# 测试/运维可覆盖的冷却时长。数值为 0 时该 category 不落盘。
_ENV_TTL_NAMES = {
    "auth_failed": ("MEMORYCORE_LLM_BREAKER_AUTH_FAILED_TTL",
                    "MEMORYCORE_LLM_BREAKER_TTL_AUTH_FAILED",
                    "MEMORYCORE_LLM_BREAKER_AUTH_TTL"),
    "http_error": ("MEMORYCORE_LLM_BREAKER_HTTP_ERROR_TTL",
                   "MEMORYCORE_LLM_BREAKER_TTL_HTTP_ERROR"),
    "bad_response": ("MEMORYCORE_LLM_BREAKER_BAD_RESPONSE_TTL",
                     "MEMORYCORE_LLM_BREAKER_TTL_BAD_RESPONSE"),
    "timeout": ("MEMORYCORE_LLM_BREAKER_TIMEOUT_TTL",
                "MEMORYCORE_LLM_BREAKER_TTL_TIMEOUT"),
    "network_unreachable": ("MEMORYCORE_LLM_BREAKER_NETWORK_UNREACHABLE_TTL",
                            "MEMORYCORE_LLM_BREAKER_TTL_NETWORK_UNREACHABLE"),
}

_probe_lock = threading.Lock()
_probing: Set[str] = set()


def breaker_path() -> Path:
    """状态文件路径 (每次调用读 env, 便于测试 monkeypatch)。"""
    raw = os.environ.get("MEMORYCORE_LLM_BREAKER_PATH", "").strip()
    return Path(raw or os.path.expanduser(
        "~/.hermes/memorycore/llm_breaker.json"))


def enabled() -> bool:
    """熔断开关: 默认关闭 (opt-in)。

    只有 MEMORYCORE_LLM_BREAKER 取值在 {"1","true","on","yes"}
    (大小写不敏感, 去首尾空白) 时才返回 True; 其它值 (含未设置、
    空串、"0") 一律 False。
    """
    raw = os.environ.get("MEMORYCORE_LLM_BREAKER", "").strip()
    return raw.lower() in {"1", "true", "on", "yes"}


def _cooldown(category: str) -> float:
    attr = _COOLDOWN_ATTRS.get(category)
    default = float(getattr(sys.modules[__name__], attr, 0.0) or 0.0) \
        if attr else 0.0
    for name in _ENV_TTL_NAMES.get(category, ()):
        raw = os.environ.get(name, "").strip()
        if not raw:
            continue
        try:
            return float(raw)
        except ValueError:
            log.warning("LLMBREAKER: 无效 TTL 环境变量 %s=%r → 使用缺省 %.0fs",
                        name, raw, default)
            return default
    return default


def _max_horizon() -> float:
    """合法熔断过期时间的最大未来范围。

    状态值本身就是"未来过期 epoch"; 只有明显超过所有类别冷却时长的
    未来值才判为时钟漂移/损坏 → 视为无熔断 (与 R1 降级要求一致)。
    """
    try:
        return max(_cooldown(c) for c in _COOLDOWN_ATTRS) + 1.0
    except Exception:
        return float(AUTH_FAILED_TTL) + 1.0


def _load() -> Dict[str, Any]:
    """读取状态文件。任何异常 -> {} (无熔断)。未来时间戳逐条忽略。"""
    path = breaker_path()
    try:
        if not path.exists():
            return {}
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return {}
        data = json.loads(raw)
    except Exception as e:
        # 读取是 acquire 热路径上的纯观测步骤: 任何异常都必须降级为无熔断。
        log.warning("LLMBREAKER: 熔断状态读取失败 (%s) → 按无熔断降级", e)
        return {}
    if not isinstance(data, dict):
        log.warning("LLMBREAKER: 熔断状态不是 JSON 对象 → 按无熔断降级")
        return {}
    out: Dict[str, Any] = {}
    now = time.time()
    horizon = now + _max_horizon()
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        try:
            expiry = float(value)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(expiry) or expiry > horizon:
            # 非法值/明显未来时间戳 (时钟漂移): 该条视为无熔断。
            continue
        out[key] = expiry
    return out


def _save(data: Dict[str, Any]) -> None:
    try:
        path = breaker_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".llmbreaker-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError as e:
        log.warning("LLMBREAKER: 熔断状态写失败 (%s) → 本轮不更新熔断", e)


@contextmanager
def _file_lock():
    """load-modify-save 的跨进程排他锁 (与 llm_rot 同款 fcntl.flock)。"""
    path = breaker_path()
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        fd.close()


def _prefix(source: str, model: str) -> str:
    return f"{source}|{model}|"


def _matching_keys(data: Dict[str, Any], source: str, model: str) -> Set[str]:
    prefix = _prefix(source, model)
    return {k for k in data if k.startswith(prefix)}


def check_acquire(source: str, model: str) -> str:
    """acquire 前查询熔断。

    返回:
      - "allow": 无熔断记录, 直接放行。
      - "probe": 存在已过期桶, 本次放行一次探测 (已标记进程内 probing)。
      - "blocked": 存在 active 桶或已有同 source|model 的探测在进行。
    开关关闭 / 读取失败 -> "allow"。
    """
    if not enabled():
        return "allow"
    with _probe_lock:
        prefix = _prefix(source, model)
        if prefix in _probing:
            return "blocked"
        data = _load()
        now = time.time()
        keys = _matching_keys(data, source, model)
        if any(float(data[k]) > now for k in keys):
            return "blocked"
        if keys:
            # 桶已全部到期: 允许且仅允许一次探测。
            _probing.add(prefix)
            return "probe"
        return "allow"


def clear(source: str, model: str) -> None:
    """探测成功: 清除该 source|model 的全部桶 + 进程内探测标记。"""
    if not enabled():
        return
    with _probe_lock:
        prefix = _prefix(source, model)
        _probing.discard(prefix)
    try:
        with _file_lock():
            data = _load()
            changed = False
            for key in _matching_keys(data, source, model):
                del data[key]
                changed = True
            if changed:
                _save(data)
    except OSError as e:
        log.warning("LLMBREAKER: 清除熔断状态失败 (%s) → 保留下次继续探测", e)


def record_failure(source: str, model: str, category: str,
                   *, was_probe: bool = False) -> None:
    """调用失败: 重置 category 桶 TTL; 探测失败也如此。未知 category 不落盘。"""
    if not enabled():
        return
    with _probe_lock:
        prefix = _prefix(source, model)
        if was_probe:
            _probing.discard(prefix)
    ttl = _cooldown(category)
    if ttl <= 0.0:
        return
    try:
        with _file_lock():
            data = _load()
            data[f"{source}|{model}|{category}"] = time.time() + ttl
            _save(data)
    except OSError as e:
        log.warning("LLMBREAKER: 熔断状态更新失败 (%s) → 退化为仅本轮退避", e)
