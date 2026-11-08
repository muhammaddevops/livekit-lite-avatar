#!/usr/bin/env bash
# One-time renderer setup (CPU only, macOS or Linux): a Python venv with the renderer dependencies, then
# LiteAvatar's code, weights and one presenter from upstream (~1.3 GB) into $LITEAVATAR_HOME
# (default ~/.cache/livekit-liteavatar).
#
#   scripts/setup-renderer.sh [presenter-id]     default P1yerb8kIA7eBpaIydU2lwzA, or "sample"
#
# Environment: VENV (default .venv), PYTHON_VERSION (default 3.10), CONSTRAINTS (default constraints-renderer.txt).
# Uses uv when installed, otherwise python -m venv and pip. On Linux, torch comes from PyTorch's CPU-only index.
set -euo pipefail
cd "$(dirname "$0")/.."
VENV=${VENV:-.venv}
PY=${PYTHON_VERSION:-3.10}
CONSTRAINTS=${CONSTRAINTS:-constraints-renderer.txt}

extra=()
if [[ "$(uname -s)" == Linux ]]; then
  extra=(--extra-index-url https://download.pytorch.org/whl/cpu)
fi

if command -v uv >/dev/null 2>&1; then
  [[ -d "$VENV" ]] || uv venv -q -p "$PY" "$VENV"
  if [[ ${#extra[@]} -gt 0 ]]; then extra+=(--index-strategy unsafe-best-match); fi
  uv pip install -q -p "$VENV/bin/python" ${extra[@]+"${extra[@]}"} -c "$CONSTRAINTS" -e ".[renderer,dev]"
else
  [[ -d "$VENV" ]] || "python$PY" -m venv "$VENV"
  "$VENV/bin/python" -m pip install -q --upgrade pip
  "$VENV/bin/python" -m pip install -q ${extra[@]+"${extra[@]}"} -c "$CONSTRAINTS" -e ".[renderer,dev]"
fi

"$VENV/bin/liteavatar-renderer" setup ${1:+--presenter "$1"}
echo "Next: $VENV/bin/liteavatar-renderer render    (offline test), then $VENV/bin/liteavatar-renderer serve"
