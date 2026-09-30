#!/usr/bin/env bash
# Install the pinned cold-tier engine and apply the published patch series.
#
#   usage: deploy/install-engine.sh [PATCH_DIR]
#   env:   VENV=/path/to/venv   (default: <repo>/.venv-engine)
#
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH_DIR="${1:-$HERE/../patches}"
VENV="${VENV:-$HERE/../.venv-engine}"
PIN="mnemosyne-memory==3.15.1"

[ -d "$PATCH_DIR" ] || { echo "patch dir not found: $PATCH_DIR" >&2; exit 1; }
for p in "$PATCH_DIR"/0001-*.patch "$PATCH_DIR"/0002-*.patch; do
    [ -f "$p" ] || { echo "missing patch: $p" >&2; exit 1; }
done

echo "== venv: $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet --upgrade pip
echo "== installing $PIN"
"$VENV/bin/pip" install --quiet "$PIN"

SP="$("$VENV/bin/python" -c 'import mnemosyne, os; print(os.path.dirname(os.path.dirname(mnemosyne.__file__)))')"
echo "== site-packages: $SP"
cd "$SP"

for p in "$PATCH_DIR"/0001-*.patch "$PATCH_DIR"/0002-*.patch; do
    echo "== applying $(basename "$p")"
    patch -p1 -f -i "$p"
done

if find "$SP/mnemosyne" -name '*.rej' | grep -q .; then
    echo "REJECTS present, aborting (engine left patched as far as it got)" >&2
    exit 1
fi

echo
echo "OK: engine installed and patched."
echo "next:"
echo "  1. cp deploy/engine.env.example /etc/mnemosyne/engine.env  (then edit it)"
echo "  2. cp deploy/mnemosyne_mcp_server.py /opt/mnemosyne/       (or run it from here)"
echo "  3. systemctl enable --now mnemosyne-mcp   (see deploy/mnemosyne-mcp.service)"
