#!/usr/bin/env bash
# 本機（WSL）重建底圖並驗證：帶入政府＋Mapillary AI 車道 CSV，對線上舊底圖與上一版各驗一次。
# 用法：bash scripts/lanes/wsl_build_basemap.sh   （環境變數 BMTEST＝工作目錄，需有 final.pbf、old.pmtiles；PYTHON＝含 osmium 的 python）
set -u
R="$(cd "$(dirname "$0")/../.." && pwd)"
BM="${BMTEST:-/root/bmtest}"; PY="${PYTHON:-/root/bmvenv/bin/python}"
cd "$BM"
export GOV_LANES_CSV="$R/data/lanes/gov_lane_attrs.csv" MAPILLARY_LANES_CSV="$R/data/lanes/mapillary_ai_lanes.csv" PYTHON="$PY"
[ -f mly-z14.pmtiles ] && mv -f mly-z14.pmtiles prev-z14.pmtiles
start=$(date +%s)
bash "$R/scripts/basemap/build_basemap.sh" final.pbf mly-z14.pmtiles work > build_mly.log 2>&1
echo "build exit=$? sec=$(( $(date +%s) - start ))"
grep -nE 'Mapillary|政府車道|ERROR|error:|補丁失敗|BUILD|Exception' build_mly.log | head -20
ls -l mly-z14.pmtiles
for ref in old prev-z14; do
  [ -f "$ref.pmtiles" ] || continue
  "$PY" -I "$R/scripts/basemap/verify_basemap.py" final.pbf mly-z14.pmtiles --old "$ref.pmtiles" --n 2000 --json "verify_vs_$ref.json" > "verify_vs_$ref.txt" 2>&1
  echo "verify vs $ref exit=$?"; tail -n 12 "verify_vs_$ref.txt"
done
