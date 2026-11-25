"""檢查台灣 OSM pbf 是否合理：大小、pbf 標頭（OSMHeader）、bbox 涵蓋台灣、資料時間不過舊。
用法：python -I check_pbf.py FILE.pbf [--source 名稱] [--min-mb 200] [--max-age-days 45] [--json OUT.json]
通過 exit 0，並印出 JSON（bytes、sha256、osm_timestamp、bbox、source）；不合理 exit 1。
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import struct
import sys


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 20), b''):
            h.update(b)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pbf')
    ap.add_argument('--source', default='unknown')
    ap.add_argument('--min-mb', type=float, default=200)
    ap.add_argument('--max-age-days', type=float, default=45)
    ap.add_argument('--json')
    a = ap.parse_args()
    errs = []
    size = os.path.getsize(a.pbf)
    if size < a.min_mb * 1e6:
        errs.append(f'檔案太小：{size} bytes < {a.min_mb} MB')
    # pbf 第一個 blob 必須是 OSMHeader
    with open(a.pbf, 'rb') as f:
        head = f.read(4)
        n = struct.unpack('>I', head)[0] if len(head) == 4 else 0
        bh = f.read(n) if 0 < n < 64 * 1024 else b''
    if b'OSMHeader' not in bh:
        errs.append('不是 OSM pbf（第一個 blob 不是 OSMHeader）')
    info = {'source': a.source, 'bytes': size, 'osm_timestamp': None, 'bbox': None}
    if not errs:
        import osmium
        # 明確指定 pbf 格式：暫存檔（.part）副檔名無法讓 osmium 自動判斷
        hdr = osmium.io.Reader(osmium.io.File(a.pbf, 'pbf'), osmium.osm.osm_entity_bits.NOTHING).header()
        ts = hdr.get('osmosis_replication_timestamp') or hdr.get('timestamp') or ''
        info['osm_timestamp'] = ts or None
        box = hdr.box()
        if box.valid():
            bl, tr = box.bottom_left, box.top_right
            info['bbox'] = [round(bl.lon, 4), round(bl.lat, 4), round(tr.lon, 4), round(tr.lat, 4)]
            # 台灣本島＋澎湖（約 119.3–122.1E、21.8–25.4N）必須在範圍內
            if not (bl.lon <= 119.3 and bl.lat <= 21.9 and tr.lon >= 122.0 and tr.lat >= 25.3):
                errs.append(f'bbox 沒涵蓋台灣：{info["bbox"]}')
            if (tr.lon - bl.lon) > 20 or (tr.lat - bl.lat) > 20:
                errs.append(f'bbox 過大，不像台灣 extract：{info["bbox"]}')
        else:
            errs.append('pbf 標頭沒有 bbox')
        if ts:
            t = dt.datetime.fromisoformat(ts.replace('Z', '+00:00'))
            age = (dt.datetime.now(dt.timezone.utc) - t).total_seconds() / 86400
            info['age_days'] = round(age, 1)
            if age > a.max_age_days:
                errs.append(f'資料太舊：{ts}（{age:.1f} 天 > {a.max_age_days}）')
        else:
            errs.append('pbf 標頭沒有資料時間')
    info['sha256'] = sha256(a.pbf)
    info['ok'] = not errs
    info['errors'] = errs
    out = json.dumps(info, ensure_ascii=False)
    print(out)
    if a.json:
        with open(a.json, 'w', encoding='utf-8') as f:
            f.write(out + '\n')
    sys.exit(0 if not errs else 1)


if __name__ == '__main__':
    main()
