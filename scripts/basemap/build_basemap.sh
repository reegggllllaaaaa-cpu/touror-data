#!/usr/bin/env bash
# 用「與路網同一份」OSM pbf 重建底圖 taiwan-z14.pmtiles（Protomaps Basemap 圖層，App 樣式免改圖層名）
# 用法：build_basemap.sh <input.pbf> <output.pmtiles> [workdir]
# 需求：java 21+、maven、curl、git。峰值記憶體約 4–6 GB，下載外部來源約 3 GB（水域／陸地多邊形、Natural Earth、landcover、qrank）。
# 與 Protomaps 官方每日檔的差異（刻意的補丁）：
#   官方把 access=private/no 的路壓到 z15 才出現，z14 抽取版就整條不見；這裡改成 z14 出現，
#   並在 z14 就帶 access 屬性，讓 App 能把私有路畫成虛線／淡色。其他規則與官方相同。
set -euo pipefail
SCRIPT_DIR=$(dirname "$(realpath "$0")"); PBF=$(realpath "$1"); OUT=$(realpath -m "$2"); WORK=${3:-$(dirname "$OUT")/bm-work}
BASEMAPS_REV=${BASEMAPS_REV:-42ffaaa4a85a41bfcb23e43cc0f5b492a5eca123}   # protomaps/basemaps main @ 2026-11-25（planetiler 0.10.2）
BBOX=${BASEMAP_BBOX:-119.3,21.8,122.1,25.4}
MAXZOOM=${BASEMAP_MAXZOOM:-14}
XMX=${BASEMAP_XMX:-6g}
mkdir -p "$WORK"; cd "$WORK"
if [ ! -d basemaps/.git ]; then
  git init -q basemaps && git -C basemaps remote add origin https://github.com/protomaps/basemaps
fi
git -C basemaps fetch -q --depth 1 origin "$BASEMAPS_REV" && git -C basemaps checkout -q -f FETCH_HEAD
R=basemaps/tiles/src/main/java/com/protomaps/basemap/layers/Roads.java
grep -q 'sf.hasTag("access", "private", "no") ? Math.max(minZoom, 15)' "$R" || { echo "補丁失敗：Roads.java 私有路規則已變（請檢查 BASEMAPS_REV）"; exit 3; }
sed -i 's/sf.hasTag("access", "private", "no") ? Math.max(minZoom, 15)/sf.hasTag("access", "private", "no") ? Math.max(minZoom, 14)/; s/setAttrWithMinzoom("access", sf.getTag("access"), 15)/setAttrWithMinzoom("access", sf.getTag("access"), 14)/' "$R"
grep -q 'getTag("access"), 14)' "$R" || { echo "補丁失敗：access 屬性"; exit 3; }
# Touror 車道級圖層（lanes／lane_lines／road_areas／islands_pt）：只新增圖層，既有圖層不動
B=basemaps/tiles/src/main/java/com/protomaps/basemap/Basemap.java
cp "$SCRIPT_DIR/lanes/Lanes.java" basemaps/tiles/src/main/java/com/protomaps/basemap/layers/Lanes.java
grep -q 'import com.protomaps.basemap.layers.Roads;' "$B" && grep -q '    if (clip != null) {' "$B" || { echo "補丁失敗：Basemap.java 結構已變"; exit 3; }
sed -i 's/^import com.protomaps.basemap.layers.Roads;/import com.protomaps.basemap.layers.Roads;\nimport com.protomaps.basemap.layers.Lanes;/' "$B"
sed -i 's/^    if (clip != null) {/    if (layer.isEmpty()) {\n      var lanes = new Lanes();\n      registerHandler(lanes);\n      registerSourceHandler("osm", lanes::processOsm);\n    }\n\n    if (clip != null) {/' "$B"
grep -q 'lanes::processOsm' "$B" || { echo "補丁失敗：Lanes 註冊"; exit 3; }
( cd basemaps/tiles && mvn -q -B -DskipTests package )
JAR=$(ls basemaps/tiles/target/*-with-deps.jar | head -1)
mkdir -p data/sources
# 政府補入路段用 9e12 起的合成 ID，planetiler 節點索引吃不下，先改號（只給底圖用）
"${PYTHON:-python3}" -I "$SCRIPT_DIR/renumber_synthetic.py" "$PBF" data/sources/taiwan.osm.pbf
# 政府車道屬性（可選）：GOV_LANES_CSV=<資料/車道/gov_lane_attrs.csv>；不存在就跳過，lanes 只用 OSM＋推定
rm -f data/sources/gov_lane_attrs.csv
if [ -n "${GOV_LANES_CSV:-}" ] && [ -f "$GOV_LANES_CSV" ]; then
  cp "$GOV_LANES_CSV" data/sources/gov_lane_attrs.csv; echo "使用政府車道屬性：$GOV_LANES_CSV"
else
  echo "未提供政府車道屬性 CSV（GOV_LANES_CSV），略過"
fi
# Mapillary 照片 AI 判讀車道（可選，CC BY-SA 4.0）：MAPILLARY_LANES_CSV=<data/lanes/mapillary_ai_lanes.csv>
# 優先序 osm > gov > mapillary_ai > inferred；turn_lanes 只在 OSM 無值時採用
rm -f data/sources/mapillary_ai_lanes.csv
if [ -n "${MAPILLARY_LANES_CSV:-}" ] && [ -f "$MAPILLARY_LANES_CSV" ]; then
  cp "$MAPILLARY_LANES_CSV" data/sources/mapillary_ai_lanes.csv; echo "使用 Mapillary AI 判讀車道：$MAPILLARY_LANES_CSV"
  # 路段中點判讀（同欄位格式）：MAPILLARY_SEGMENTS_CSV，未設定時自動找路口檔同目錄的 mapillary_ai_segments.csv。
  # 只併入 confidence 不是 low 的列，且同一 way＋方向已在路口檔出現時以路口檔為準（不併入）。
  SEG_CSV="${MAPILLARY_SEGMENTS_CSV:-$(dirname "$MAPILLARY_LANES_CSV")/mapillary_ai_segments.csv}"
  if [ -f "$SEG_CSV" ]; then
    awk -F, 'NR==FNR { if (FNR > 1) seen[$1 "," $2] = 1; next }
             FNR > 1 && !(($1 "," $2) in seen) && $5 != "low" && $1 != "" { print }' \
      data/sources/mapillary_ai_lanes.csv "$SEG_CSV" > data/sources/.mly_seg_add.csv
    [ -n "$(tail -c1 data/sources/mapillary_ai_lanes.csv)" ] && echo >> data/sources/mapillary_ai_lanes.csv
    cat data/sources/.mly_seg_add.csv >> data/sources/mapillary_ai_lanes.csv
    echo "併入 Mapillary 路段判讀：$SEG_CSV（+$(wc -l < data/sources/.mly_seg_add.csv) 列）"; rm -f data/sources/.mly_seg_add.csv
  fi
else
  echo "未提供 Mapillary AI 判讀車道 CSV（MAPILLARY_LANES_CSV），略過"
fi
# 全台車道推論（可選）：INFER_LANES_CSV=<scripts/lanes_infer/infer_lanes.py 產出的 inferred_lanes.csv，需先過 verify_infer.py>
rm -f data/sources/inferred_lanes.csv
if [ -n "${INFER_LANES_CSV:-}" ] && [ -f "$INFER_LANES_CSV" ]; then
  cp "$INFER_LANES_CSV" data/sources/inferred_lanes.csv; echo "使用全台車道推論：$INFER_LANES_CSV"
else
  echo "未提供全台車道推論 CSV（INFER_LANES_CSV），略過"
fi
rm -f "$OUT" data/sources/taiwan.osm.pbf.tmp
java -Xmx"$XMX" -jar "$JAR" --area=taiwan --download --bounds="$BBOX" --maxzoom="$MAXZOOM" \
  --output="$OUT" --force --nodemap_type=sparsearray --storage=mmap 2>&1 | tail -n 40
ls -l "$OUT"
