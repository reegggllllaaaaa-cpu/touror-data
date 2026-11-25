#!/usr/bin/env bash
# 本機（WSL）帶全台車道推論重建底圖並驗證：bash scripts/lanes_infer/wsl_build_basemap.sh
# 先跑 wsl_run.sh 產生 /root/lanesinfer/out/inferred_lanes.csv；PBF／OLD 可用環境變數覆寫。
set -u
R="$(cd "$(dirname "$0")/../.." && pwd)"
W=/root/lanesinfer/bm; PY="${PYTHON:-/root/bmvenv/bin/python}"
PBF="${PBF:-/root/rga/final.pbf}"; OLD="${OLD:-/root/rga/new-z14.pmtiles}"
mkdir -p "$W"
export GOV_LANES_CSV="$R/data/lanes/gov_lane_attrs.csv" MAPILLARY_LANES_CSV="$R/data/lanes/mapillary_ai_lanes.csv" \
  INFER_LANES_CSV=/root/lanesinfer/out/inferred_lanes.csv PYTHON="$PY"
start=$(date +%s)
nice -n 10 bash "$R/scripts/basemap/build_basemap.sh" "$PBF" "$W/infer-z14.pmtiles" "$W/work" > "$W/build.log" 2>&1
echo "build exit=$? sec=$(( $(date +%s) - start ))"
grep -nE '車道推論|Mapillary|政府車道|ERROR|error:|補丁失敗|Exception' "$W/build.log" | head -20
ls -l "$W/infer-z14.pmtiles"
nice -n 10 "$PY" -I "$R/scripts/basemap/verify_basemap.py" "$PBF" "$W/infer-z14.pmtiles" --old "$OLD" --n 2000 --json "$W/verify.json" > "$W/verify.txt" 2>&1
echo "verify exit=$?"; tail -n 12 "$W/verify.txt"
