#!/usr/bin/env bash
# 把一份檢查過的台灣 pbf 上傳到 Release「osm-source」（fetch_osm.sh 的最後備援），檔名 taiwan-YYYYMMDD.osm.pbf（OSM 資料日期），
# 附 .sha256 與 .json（來源、資料時間、大小）。先上傳新的、再刪舊的，只留最新 2 份；不設為 Latest Release。
# 用法：osm_source_upload.sh FILE.pbf [來源說明]     （本機或 workflow 都可用；需要 gh 登入或 GH_TOKEN）
set -euo pipefail
PBF=$1; SRC=${2:-local}
HERE=$(cd "$(dirname "$0")" && pwd)
REPO=${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}
TMP=$(mktemp -d)
python -I "$HERE/check_pbf.py" "$PBF" --source "$SRC" --json "$TMP/info.json" >/dev/null
DAY=$(python -I -c "import json,sys;t=json.load(open(sys.argv[1]))['osm_timestamp'];print(t[:10].replace('-',''))" "$TMP/info.json")
NAME="taiwan-$DAY.osm.pbf"
cp "$PBF" "$TMP/$NAME"; cp "$TMP/info.json" "$TMP/$NAME.json"
(cd "$TMP" && sha256sum "$NAME" > "$NAME.sha256")
if ! gh release view osm-source --repo "$REPO" >/dev/null 2>&1; then
  gh release create osm-source --repo "$REPO" --latest=false --title "OSM 來源備份（每週路網的最後備援）" \
    --notes "Geofabrik 與鏡像都下載失敗時，每週路網改用這裡最新一份台灣 pbf。檔名日期為 OSM 資料時間；.json 記錄來源與 sha256。© OpenStreetMap contributors，ODbL 1.0。"
fi
gh release upload osm-source --repo "$REPO" --clobber "$TMP/$NAME" "$TMP/$NAME.sha256" "$TMP/$NAME.json"
for old in $(gh release view osm-source --repo "$REPO" --json assets -q '.assets[].name' | grep -E '^taiwan-[0-9]{8}\.osm\.pbf$' | sort | head -n -2); do
  for f in "$old" "$old.sha256" "$old.json"; do gh release delete-asset osm-source "$f" --repo "$REPO" -y || true; done
done
echo "已上傳 $NAME 到 osm-source"; cat "$TMP/$NAME.sha256"
rm -rf "$TMP"
