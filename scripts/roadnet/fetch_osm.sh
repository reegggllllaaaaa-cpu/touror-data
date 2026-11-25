#!/usr/bin/env bash
# 下載台灣 OSM pbf，不單點依賴 Geofabrik：
#   1) Geofabrik（核對 MD5）；2) OSM_MIRRORS 環境變數列的其他鏡像（空白分隔，可附 .md5）；
#   3) 本倉庫 Release「osm-source」上最新一份 taiwan-YYYYMMDD.osm.pbf（核對 sha256）。
# 每個來源重試 5 次、指數退避（15/30/60/120/240 秒）。下載後用 check_pbf.py 檢查大小、標頭、bbox、資料時間。
# 用法：fetch_osm.sh OUT.pbf     產出 OUT.pbf 與 OUT.pbf.info.json（source、osm_timestamp、sha256…）
# 若有 $GITHUB_ENV，寫入 OSM_SOURCE、OSM_TIMESTAMP、OSM_SHA256、OSM_URL。
# 註：download.openstreetmap.fr 沒有台灣 extract（2026-11-26 查過 asia/ 與 asia/china/ 都沒有），所以預設不列鏡像。
set -uo pipefail
OUT=$1
HERE=$(cd "$(dirname "$0")" && pwd)
UA="touror-data-builder"
GEOFABRIK=${OSM_PRIMARY:-https://download.geofabrik.de/asia/taiwan-latest.osm.pbf}
TRIES=${OSM_TRIES:-5}
REPO=${REPO:-$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null)}

check() { # $1=file $2=source $3=max-age-days
  python -I "$HERE/check_pbf.py" "$1" --source "$2" --max-age-days "$3" --json "$OUT.info.json"
}

try_url() { # $1=url $2=md5url(可空) $3=source
  local url=$1 md5=$2 src=$3 d=15 i
  for i in $(seq 1 "$TRIES"); do
    echo "[$src] 第 $i 次：$url"
    if curl -fsSL --connect-timeout 30 --max-time 2400 -A "$UA" -o "$OUT.part" "$url"; then
      local okmd5=0
      if [ -n "$md5" ]; then
        curl -fsSL --connect-timeout 30 --max-time 120 -A "$UA" -o "$OUT.md5" "$md5" \
          && echo "$(cut -d' ' -f1 "$OUT.md5")  $OUT.part" | md5sum -c - || okmd5=1
      fi
      if [ $okmd5 = 0 ] && check "$OUT.part" "$src" 45; then
        mv -f "$OUT.part" "$OUT"; echo "OSM_URL=$url" >> "${GITHUB_ENV:-/dev/null}"; return 0
      fi
      echo "[$src] 檔案檢查沒過"
    fi
    [ $i -lt "$TRIES" ] && { echo "[$src] 等 $d 秒再試"; sleep $d; d=$((d * 2)); }
  done
  rm -f "$OUT.part"; return 1
}

try_release() {
  local d=15 i tmp name
  tmp=$(mktemp -d)
  for i in $(seq 1 "$TRIES"); do
    name=$(gh release view osm-source --repo "$REPO" --json assets -q '.assets[].name' 2>/dev/null | grep -E '^taiwan-[0-9]{8}\.osm\.pbf$' | sort | tail -n 1)
    if [ -n "$name" ] && gh release download osm-source --repo "$REPO" --pattern "$name" --pattern "$name.sha256" --dir "$tmp" --clobber \
        && (cd "$tmp" && sha256sum -c "$name.sha256") && check "$tmp/$name" "release:osm-source/$name" 120; then
      mv -f "$tmp/$name" "$OUT"; rm -rf "$tmp"
      echo "OSM_URL=https://github.com/$REPO/releases/download/osm-source/$name" >> "${GITHUB_ENV:-/dev/null}"; return 0
    fi
    [ $i -lt "$TRIES" ] && { echo "[release] 等 $d 秒再試"; sleep $d; d=$((d * 2)); }
  done
  rm -rf "$tmp"; return 1
}

ok=1
try_url "$GEOFABRIK" "$GEOFABRIK.md5" geofabrik && ok=0
if [ $ok != 0 ]; then
  for m in ${OSM_MIRRORS:-}; do
    try_url "$m" "" "mirror:$m" && { ok=0; break; }
  done
fi
[ $ok != 0 ] && try_release && ok=0
if [ $ok != 0 ]; then echo "所有來源都失敗（Geofabrik、鏡像、Release osm-source）"; exit 1; fi
python -I - "$OUT.info.json" <<'PY' >> "${GITHUB_ENV:-/dev/null}"
import json, sys
i = json.load(open(sys.argv[1], encoding='utf-8'))
print('OSM_SOURCE=' + i['source']); print('OSM_TIMESTAMP=' + (i['osm_timestamp'] or '')); print('OSM_SHA256=' + i['sha256'])
PY
cat "$OUT.info.json"
