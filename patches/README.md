# Engine patches — mnemosyne-memory 3.15.1

MemoryCore's cold tier is the upstream PyPI package **`mnemosyne-memory`** (MIT,
Copyright (c) 2026 Abdias J — https://github.com/AxDSan/mnemosyne). We run a locally
patched copy of it. This directory publishes the exact patch series so that the engine we
run can be reproduced from the public package: no fork and no vendored copy live in this
repository.

## Files

| Patch | Files touched | What it is |
|---|---|---|
| `0001-upstream-to-deployed.patch` | 3 | the patches we had already been running before the engine rework (`core/beam.py`, `core/embeddings.py`, `core/memory.py`) |
| `0002-engine-rework.patch` | 25 (19 modified, 3 added, 3 removed) | the engine rework itself |

Apply in order. Both are anchored to `mnemosyne-memory==3.15.1` and carry `a/` `b/`
prefixes, so `-p1` is the correct strip level.

## Apply

```bash
python3 -m venv .venv
.venv/bin/pip install "mnemosyne-memory==3.15.1"

SP=$(.venv/bin/python -c 'import mnemosyne, os; print(os.path.dirname(os.path.dirname(mnemosyne.__file__)))')
cd "$SP"
patch -p1 -f -i /path/to/0001-upstream-to-deployed.patch
patch -p1 -f -i /path/to/0002-engine-rework.patch
```

`deploy/install-engine.sh` does the same thing with the checks included, and
`deploy/` also carries a sample systemd unit, an environment file, and the MCP server
entry point that exposes the five engine tools.

## What the rework changes (0002)

* **Recall is narrowed to the caller's domain.** `session_id` is a hard filter and
  `scope='global'` is no longer a cross-domain skeleton key. In the previous code the
  scope predicate was satisfied by every row, so a scoped recall could return rows
  belonging to other domains.
* **In-domain cap `C = 10,000`.** Read-side only: no deletes, no write-path change. The
  candidate order is made deterministic (`importance DESC, timestamp DESC, rowid ASC`) and
  the cap is reported instead of being applied silently.
* **New core schema, opt-in.** A `vector_store` table plus a dual-path read lets the
  vector index stop depending on the legacy `memories` / `working_memory` join. The
  migration tool builds the table; the read path only activates when the table is present.
* **Policy injection point + read-only recall counters** (`core/policy_ext.py`,
  `core/recall_counters.py`), so tuning values stay out of the kernel.
* **Removals** of unused modules carried over from earlier experiments.

This is a description of behaviour, not a claim about benchmarks. We publish no
performance numbers here: they were measured on one machine and one corpus, which says
little about anyone else's workload.

## Verification

The claim this directory makes is reproducible: **upstream 3.15.1 + 0001 + 0002 is
byte-identical to the engine tree we run.**

```bash
python3 -m venv .venv && .venv/bin/pip install "mnemosyne-memory==3.15.1"
# apply both patches as above, then compare the patched package against a reference copy
diff -rq -x '__pycache__' -x '*.bak-*' "$SP/mnemosyne" /path/to/reference/mnemosyne   # expect no output
```

Apply is idempotent-safe: run with `-f` and check the exit status; no `.rej` files should
be produced.

## Publish-time edits (disclosed in full)

The published series is identical to our deployment except for the following edits, and
nothing else:

1. One synonym entry in `core/beam.py` that named our own machine was dropped (2 lines
   inside `0001`).
2. Comments in 9 files that referred to our internal design notes were rewritten to be
   self-contained (`0002`). No code logic or string literal touched.
3. In the sample MCP entry point (`deploy/mnemosyne_mcp_server.py`, not part of the patch
   series) four values were generalized: the data directory and the journal location read
   from `MNEMOSYNE_DATA_DIR` instead of a hard-coded path, the bind address and the
   accepted `Host` headers read from the environment with loopback-only defaults, and the
   default embedding model is `qwen3-embedding:0.6b` rather than the longer-context variant
   our own deployment runs. Our deployment keeps its original entry point; these edits only
   affect the published sample.

So the honest statement is: the published engine is our engine minus the machine name in a
synonym list, with comments made readable outside our own notes, and with a sample launcher
whose defaults suit a fresh install instead of our machine.

## License

Upstream is MIT. The patch series is a derivative work of it and carries the same
license; the upstream license text ships with the package and is unchanged by these
patches.
