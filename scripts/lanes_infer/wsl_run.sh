#!/usr/bin/env bash
# 本機（WSL）跑全台車道推論：bash scripts/lanes_infer/wsl_run.sh [final.pbf] [輸出目錄]
# PYTHON＝含 osmium 的 python（預設 /root/bmvenv/bin/python）；以低優先權執行，不搶使用者桌面。
set -u
R="$(cd "$(dirname "$0")/../.." && pwd)"
PBF="${1:-/root/rga/final.pbf}"; OUT="${2:-/root/lanesinfer/out}"; PY="${PYTHON:-/root/bmvenv/bin/python}"
mkdir -p "$OUT"
nice -n 10 /usr/bin/time -v "$PY" -I "$R/scripts/lanes_infer/infer_lanes.py" "$PBF" "$OUT" \
  --gov "$R/data/lanes/gov_lane_attrs.csv" --mly "$R/data/lanes/mapillary_ai_lanes.csv" > "$OUT/run.log" 2>&1
echo "infer exit=$?"
grep -vE '^\s+(Average|Socket|Signals|Page|Swaps|File system|Voluntary|Involuntary|Minor|Major|Percent|User time|System time|Exit status|Command)' "$OUT/run.log" | tail -25
"$PY" -I "$R/scripts/lanes_infer/verify_infer.py" "$OUT/infer_report.json" "$R/scripts/lanes_infer/gate.json"
echo "verify exit=$?"
