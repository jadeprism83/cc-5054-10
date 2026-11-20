#!/usr/bin/env bash
# End-to-end pipeline: sources -> encoding grid -> selection -> deliverables -> web
set -euo pipefail
cd "$(dirname "$0")/workbench"

echo "== 1/4 render 1080p master clips (FFV1)"
python3 generate_sources.py

echo "== 2/4 run encoding grid (54 real jobs + 9 deliberate failures, cached)"
python3 encode_grid.py

echo "== 3/4 rate-distortion selection (greedy vs joint) + budget gate"
python3 selection.py
python3 budget_gate.py

echo "== 4/4 start web workbench"
PORT="${1:-8765}"
echo "open http://localhost:${PORT}"
exec python3 app.py "${PORT}"
