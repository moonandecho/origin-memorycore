# origin-memorycore

**MCP memory tiering server for LLM agents — hot local tier + cold remote tier with automatic overflow.**

MemoryCore gives your LLM agent a two-tier memory system: frequently-used behavioral knowledge (preferences, rules, corrections) stays in a fast local file tier, while low-frequency facts are automatically migrated to a remote memory service. No more one-blob memory files that grow forever or lose important preferences to truncation.

Built on the [MCP](https://modelcontextprotocol.io) (Model Context Protocol) `streamable-http` / stdio standard. Works with any MCP client, tested with [Hermes Agent](https://github.com/NousResearch/hermes-agent).

---

## Features

- **Cold/hot routing** — every write is classified: high-importance or preference-like → hot (local); low-frequency fact → cold (remote); stale status record → dropped.
- **Six-step overflow** — capacity baseline → dedup → stale filtering → merge → safe write (cold first, then delete local) → verification.
- **Cold-tier maintenance** — dedup merge, stale cleanup, conflict resolution, embedding integrity check.
- **Capacity control** — soft threshold (overflow once before writing) / hard threshold (force overflow) / target ratio. Defaults: 60% / 80% / 40% of a 5000-char limit.
- **Graceful degradation** — cold tier unreachable? Writes fail loudly (never silently dropped), overflow keeps local entries, health check returns local status with `cold.error`.
- **Zero core modification** — designed as a drop-in companion; your agent's built-in memory tools keep working.

## Architecture

```
┌─────────────────────────────── Mac / local ───────────────────────────────┐
│  LLM agent (e.g. Hermes)                                                  │
│    │  MCP client                                                          │
│    ▼                                                                      │
│  MemoryCore MCP server                                                    │
│    ├─ local_store.py        hot tier: MEMORY.md / USER.md (chars-based)   │
│    ├─ classifier.py         cold/hot/stale routing rules                  │
│    ├─ overflow.py           six-step overflow                             │
│    ├─ maintenance.py        cold-tier governance                          │
│    └─ cold_store_client.py  →  MCP streamable-http                        │
└───────────────────────────────────────────────────────────────────────────┘
                                    │  remember / recall / update / forget / stats
                                    ▼
                        remote MCP memory service (cold tier)
```

## Quick Start

```bash
pip install origin-memorycore

# point at any MCP memory service exposing the cold-store contract
export MNEMOSYNE_URL="http://your-memory-service:9000/mcp"
export MEMORY_DIR="$HOME/.hermes/memories"   # optional; default shown

python -m memorycore.server          # stdio transport (default)
# or: memorycore  (if installed via console script)
```

Register it in your MCP client (example for Hermes Agent `config.yaml`):

```yaml
mcp_servers:
  memorycore:
    command: python
    args: ["-m", "memorycore.server"]
    env:
      MNEMOSYNE_URL: "http://your-memory-service:9000/mcp"
```

Exposed tools:

| Tool | Purpose |
|---|---|
| `memorycore_store_fact(content, importance, scope, target)` | Unified write entry: routes cold / hot / stale |
| `memorycore_trigger_overflow(target)` | Run six-step overflow, target ≤40% |
| `memorycore_run_cold_storage_maintenance()` | Cold-tier governance pass |
| `memorycore_get_memory_usage()` | Hot-tier usage + cold-tier stats + thresholds |

## Cold Store Contract

Any service that exposes these five MCP tools can act as the cold tier:

| Tool | Semantics |
|---|---|
| `remember(content, importance, scope)` | Store a memory, return `memory_id` |
| `recall(query, top_k)` | Semantic recall |
| `update(memory_id, content)` | Merge-update an existing memory |
| `forget(memory_id)` | Delete a memory |
| `stats()` | `total` + embedding integrity |

See [examples/cold-store-contract.md](examples/cold-store-contract.md) for the full contract and a reference client.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `MNEMOSYNE_URL` | *(required)* | Cold-tier MCP endpoint |
| `MEMORY_DIR` | `~/.hermes/memories` | Hot-tier directory (`MEMORY.md` / `USER.md`) |
| `MNEMOSYNE_TIMEOUT` | `10.0` | Cold-tier request timeout (seconds) |

Capacity constants live in `memorycore/core/config.py` (`CHAR_LIMIT_*`, `SOFT_THRESHOLD`, `HARD_THRESHOLD`, `TARGET_RATIO`).

## How It Works

1. **Write** — `store_fact` classifies the content:
   - importance ≥ 0.8 or matches hot keywords (preferences / rules / corrections / red lines) → **hot**, kept local
   - stale markers (short entry, e.g. "已修复 / fixed") → **dropped** (not migrated)
   - anything else → **cold**, written directly to the remote service
2. **Overflow** — when hot usage passes the soft threshold, overflow migrates low-frequency entries to the cold tier; at the hard threshold it force-overflows until ≤ target. Order is always *write cold first, verify, then delete local* — nothing is lost if the cold tier fails.
3. **Maintenance** — a periodic pass over the cold tier merges duplicates, removes stale entries, resolves conflicts, and verifies embedding integrity.

## License

[MIT](LICENSE) © 2026 moonandecho
