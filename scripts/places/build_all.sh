#!/usr/bin/env bash
# 店家庫三庫（主庫、Overture、政府）從零重建，順序與參數照線上版（資料/搜尋 的 說明.md、分店名升格報告.md、名稱品質盤點報告.md）。
# 用法：PY=python bash scripts/places/build_all.sh <osm.pbf> <old 資料夾> <工作資料夾> <輸出資料夾>
#   old 資料夾要有：touror_places_overture.sqlite（目前線上版，v2 用它替沒有 branch 的超商補分店名）、touror_addr.bin（線上門牌包）
#   可選環境變數：OVERTURE_RELEASE（不給就抓 S3 最新）、OVERTURE_DIR（已下載好的 raw/flat/taiwan_places.parquet，略過下載）
# 輸出：<輸出>/touror_places.sqlite、touror_places_overture.sqlite、touror_places_gov.sqlite、report/（各步報告與 inputs.json 來源紀錄）
set -euo pipefail
PBF=$(realpath "$1"); OLD=$(realpath "$2"); W=$(mkdir -p "$3" && realpath "$3"); OUT=$(mkdir -p "$4" && realpath "$4")
PY=${PY:-python}
HERE=$(cd "$(dirname "$0")" && pwd); I=$(cd "$HERE/../../data/places_inputs" && pwd)
ADDR="$OLD/touror_addr.bin"
REP="$OUT/report"; mkdir -p "$REP"
SRC_JSON="$REP/inputs.json"; echo '{}' > "$SRC_JSON"
note() { "$PY" -c "import json,sys;p=sys.argv[1];d=json.load(open(p,encoding='utf-8'));d[sys.argv[2]]=sys.argv[3];json.dump(d,open(p,'w',encoding='utf-8'),ensure_ascii=False,indent=1)" "$SRC_JSON" "$1" "$2"; }
step() { echo; echo "=== $(date -u +%H:%M:%S) $*"; }
UA="touror-data-builder"

step "1/11 Overture places"
if [ -n "${OVERTURE_DIR:-}" ]; then OV=$(realpath "$OVERTURE_DIR"); note overture "本機既有：$OV";
else OV="$W/ov"; "$PY" "$HERE/fetch_overture.py" "$OV" ${OVERTURE_RELEASE:+--release "$OVERTURE_RELEASE"} | tee "$REP/overture_fetch.log"; note overture "$(tail -1 "$REP/overture_fetch.log")"; fi

step "2/11 OSM 主庫 v2（含分店名、門牌 note）"
mkdir -p "$W/v2"
"$PY" "$HERE/build_places_v2.py" --pbf "$PBF" --out "$W/v2/touror_places.sqlite" --work "$W/v2work" --addr "$ADDR" --overture "$OLD/touror_places_overture.sqlite"

step "3/11 Overture 庫（對 v2 去重）"
"$PY" "$HERE/build_places_overture.py" --parquet "$OV/taiwan_places.parquet" --osm-db "$W/v2/touror_places.sqlite" --out "$W/v2/touror_places_overture.sqlite"

step "4/11 連鎖名冊（7-11 emap、OK）：抓新的；失敗或筆數掉超過 10% 就用 repo 快取 data/places_inputs/名冊"
R="$W/rosters"; rm -rf "$R"; mkdir -p "$R"
if TOUROR_ADDR="$ADDR" timeout 1800 "$PY" "$HERE/fetch_chain_rosters.py" --out "$W/rosters_new" > "$REP/rosters_fetch.log" 2>&1; then :; fi
for f in 7-11_emap.csv OK_okmart.csv; do
  n_new=$( [ -f "$W/rosters_new/$f" ] && wc -l < "$W/rosters_new/$f" || echo 0 ); n_old=$(wc -l < "$I/名冊/$f")
  if [ "$n_new" -ge $(( n_old * 9 / 10 )) ]; then cp "$W/rosters_new/$f" "$R/$f"; note "roster_$f" "線上新抓 $n_new 行（快取 $n_old）";
  else cp "$I/名冊/$f" "$R/$f"; note "roster_$f" "用快取 $n_old 行（新抓 $n_new 行，失敗或不足）"; fi
done

step "5/11 官方名冊升格分店名"
"$PY" "$HERE/build_chain_branches.py" --db "$W/v2/touror_places.sqlite" --db "$W/v2/touror_places_overture.sqlite" --rosters "$R" --gcis "$I/真值/32086.csv" --addr "$ADDR" --outdir "$W/chain"
cp "$W/chain/summary.json" "$REP/chain_summary.json" 2>/dev/null || true

step "6/11 7-11 同店去重"
"$PY" "$HERE/dedupe_711.py" "$W/chain" "$R/7-11_emap.csv" --apply

step "7/11 政府庫（中油、郵局、觀光署景點與旅宿）：抓新的，失敗用快取"
G="$W/gov"; rm -rf "$G"; mkdir -p "$G/gov_raw" "$G/真值"
cp "$W/chain/去重/touror_places.sqlite" "$W/chain/去重/touror_places_overture.sqlite" "$G/"
fetch() { # fetch <url> <dest> <cache> <kind>
  if curl -fsSL --retry 2 --retry-delay 20 --max-time 300 -A "$UA" -o "$2.part" "$1" && "$PY" - "$2.part" "$4" <<'PY'
import sys, json, csv, zipfile
p, k = sys.argv[1], sys.argv[2]
if k == 'json': assert len(json.load(open(p, encoding='utf-8-sig'))) > 1000
elif k == 'csv': assert len(list(csv.DictReader(open(p, encoding='utf-8-sig')))) > 1000
else: assert zipfile.ZipFile(p).testzip() is None
PY
  then mv "$2.part" "$2"; note "$(basename "$2")" "線上新抓 $1"; else rm -f "$2.part"; cp "$3" "$2"; note "$(basename "$2")" "用快取 $3"; fi
}
fetch "https://vipmbr.cpc.com.tw/openData/getStationInfo" "$G/gov_raw/cpc.json" "$I/gov_raw/cpc.json" json
fetch "https://www.post.gov.tw/post/internet/Templates/getOpenDataFile.jsp?vkey=B484EB27-B3F0-4D02-8F62-047D20C64078" "$G/gov_raw/post.csv" "$I/gov_raw/post.csv" csv
fetch "https://media.taiwan.net.tw/XMLReleaseAll_public/v2.0/Zh_tw/Attraction-json.zip" "$G/真值/Attraction-json.zip" "$I/真值/Attraction-json.zip" zip
fetch "https://media.taiwan.net.tw/XMLReleaseAll_public/v2.0/Zh_tw/Hotel-json.zip" "$G/真值/Hotel-json.zip" "$I/真值/Hotel-json.zip" zip
TOUROR_PLACES_WORK="$G" "$PY" "$HERE/build_places_gov.py"
cp "$G/政府分庫報告.md" "$REP/" 2>/dev/null || true

step "8/11 Overture 名稱補全"
"$PY" "$HERE/build_name_completion.py" --main "$W/chain/去重/touror_places.sqlite" --ov "$W/chain/去重/touror_places_overture.sqlite" --flat "$OV/flat.parquet" --gov "$G/touror_places_gov.sqlite" --outdir "$W/nc"
cp "$W/nc/summary.json" "$REP/name_completion_summary.json" 2>/dev/null || true

step "9/11 名冊補名（7-11/OK 名冊、商工登記 32086、學校／醫療／宗教名冊）"
"$PY" "$HERE/build_roster_names.py" --main "$W/nc/touror_places.sqlite" --ov "$W/nc/touror_places_overture.sqlite" --rosters "$R" --gcis "$I/真值/32086.csv" --offdir "$I/名冊2" --addr "$ADDR" --gov "$G/touror_places_gov.sqlite" --outdir "$W/rn"
cp "$W/rn/summary.json" "$REP/roster_names_summary.json" 2>/dev/null || true

step "10/11 OSM 路名表（英文地址轉中文用）"
"$PY" "$HERE/extract_osm_road_names.py" "$PBF" "$W/osm_road_names.pkl"

step "11/11 place_addr（三庫）"
"$PY" "$HERE/build_place_addr.py" --kind main --src "$W/rn/touror_places.sqlite" --out "$OUT/touror_places.sqlite" --raw "$W/v2work/raw.pkl" --report-dir "$REP/addr_main"
"$PY" "$HERE/build_place_addr.py" --kind overture --src "$W/rn/touror_places_overture.sqlite" --out "$OUT/touror_places_overture.sqlite" --flat "$OV/flat.parquet" --osm-roads "$W/osm_road_names.pkl" --report-dir "$REP/addr_overture"
"$PY" "$HERE/build_place_addr.py" --kind gov --src "$G/touror_places_gov.sqlite" --out "$OUT/touror_places_gov.sqlite" --report-dir "$REP/addr_gov"
step "完成"; ls -la "$OUT"
