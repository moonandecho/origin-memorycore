#!/usr/bin/env python3
"""core/config.py — MemoryCore configuration

Thresholds / paths / remote memory service URL / timeouts.
All values can be overridden via environment variables.
"""
import os
from pathlib import Path

# ---- capacity (chars) ----
CHAR_LIMIT_MEMORY = 5000
CHAR_LIMIT_USER = 5000

SOFT_THRESHOLD = 0.60   # 60% of limit: overflow once before writing new hot data
HARD_THRESHOLD = 0.80   # 80%: force overflow
TARGET_RATIO = 0.40     # overflow target: <=40%

# ---- hot tier paths (Hermes local memory, overridable) ----
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", os.path.expanduser("~/.hermes/memories")))
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
USER_FILE = MEMORY_DIR / "USER.md"

# ---- cold tier engine (remote MCP memory service) ----
# Required: point MNEMOSYNE_URL at any MCP memory service exposing
# remember/recall/update/forget/stats (see examples/cold-store-contract.md).
MNEMOSYNE_URL = os.environ.get("MNEMOSYNE_URL", "")
MNEMOSYNE_TIMEOUT = 10.0

# ---- 冷热判定关键词 (热数据特征: 每轮都要用的偏好/准则/纠正/常量) ----
HOT_KEYWORDS = [
    "偏好", "准则", "原则", "禁止", "必须", "习惯", "要求",
    "纠正", "用户拍板", "用户明确", "零容忍", "不允许",
    "环境常量", "交互习惯", "行为准则", "写作风格", "回答风格",
]

# ---- 过时状态标记 (E2: 状态记录类不再反映当前状态 → forget 不迁移) ----
STALE_MARKERS = [
    "已修复", "已解决", "已切换", "已退役", "已停用",
    "已迁移", "已删除", "已完成", "不再使用", "已废弃",
]
