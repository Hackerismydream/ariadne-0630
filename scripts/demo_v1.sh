#!/usr/bin/env bash
set -euo pipefail

DEMO_DIR="${ARIADNE_DEMO_DIR:-.ariadne-demo-v1}"
export ARIADNE_DB="${ARIADNE_DB:-$DEMO_DIR/ariadne-v1.db}"

uv run ariadne demo-v1 --output-dir "$DEMO_DIR" --reset
uv run ariadne runtime-list
uv run ariadne capability-list
uv run ariadne taskrun-list
uv run ariadne runtime-lease-list
uv run ariadne leader-decision-list
uv run ariadne benchmark-list
