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
( cd basemaps/tiles && mvn -q -B -DskipTests package )
JAR=$(ls basemaps/tiles/target/*-with-deps.jar | head -1)
mkdir -p data/sources
# 政府補入路段用 9e12 起的合成 ID，planetiler 節點索引吃不下，先改號（只給底圖用）
"${PYTHON:-python3}" -I "$SCRIPT_DIR/renumber_synthetic.py" "$PBF" data/sources/taiwan.osm.pbf
rm -f "$OUT" data/sources/taiwan.osm.pbf.tmp
java -Xmx"$XMX" -jar "$JAR" --area=taiwan --download --bounds="$BBOX" --maxzoom="$MAXZOOM" \
  --output="$OUT" --force --nodemap_type=sparsearray --storage=mmap 2>&1 | tail -n 40
ls -l "$OUT"
