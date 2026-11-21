#!/usr/bin/env bash
# Revocable early-stop search experiment:
#   master shots -> window-sampled search (2 policies x 2 orderings)
#   -> exhaustive RD frontier -> panel lock / counterexample / reopen audit
# All measurement caches are SQLite-backed, so re-runs skip finished encodes.
set -euo pipefail
cd "$(dirname "$0")/workbench"

echo "== 1/2 render master shots + run the four search experiments"
python3 run_search_demo.py

echo "== 2/2 start workbench (open /search for the early-stop panel)"
PORT="${1:-8765}"
echo "open http://localhost:${PORT}/search"
exec python3 app.py "${PORT}"
