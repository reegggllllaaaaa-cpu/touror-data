# -*- coding: utf-8 -*-
"""移動式測速常出沒路段的「路名參考點」。

建置_移動式.py 用 資料\\搜尋\\touror_places.sqlite（OSM 地名資料庫，50 MB，不進本倉庫）的 kind='road' 列當參考點。
本倉庫只放那幾欄的匯出檔 raw/speedcams/road-anchors.tsv.gz（約 2 MB），建置時還原成一個只有 place 表的小 sqlite，
放在 build/work/資料/搜尋/touror_places.sqlite，vendor 腳本不用改。

匯出（地名資料庫重建後，本機執行一次）：
  python -I scripts/speedcams/road_anchors.py export --places <touror_places.sqlite>
"""
import argparse
import gzip
import io
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from common import paths  # noqa: E402

ANCHORS = os.path.join(paths.RAW_SPEEDCAMS, 'road-anchors.tsv.gz')


def export(places, out=ANCHORS):
    db = sqlite3.connect('file:%s?mode=ro' % places, uri=True)
    rows = db.execute("select name, coalesce(sub, ''), lat, lon from place where kind = 'road' order by name, sub, lat, lon")
    buf = io.StringIO()
    n = 0
    for name, sub, lat, lon in rows:
        buf.write('%s\t%s\t%.6f\t%.6f\n' % (name.replace('\t', ' '), sub.replace('\t', ' '), lat, lon))
        n += 1
    data = gzip.compress(buf.getvalue().encode('utf-8'), mtime=0)
    with open(out, 'wb') as f:
        f.write(data)
    return n, len(data)


def restore(sqlite_path, anchors=ANCHORS):
    """anchors → 只有 place(id, name, kind, sub, lat, lon, weight) 的 sqlite"""
    if os.path.exists(sqlite_path):
        os.remove(sqlite_path)
    os.makedirs(os.path.dirname(sqlite_path), exist_ok=True)
    db = sqlite3.connect(sqlite_path)
    db.execute('create table place (id integer primary key, name text not null, kind text not null, sub text, '
               'lat real not null, lon real not null, weight integer not null default 0)')
    text = gzip.decompress(open(anchors, 'rb').read()).decode('utf-8')
    rows = []
    for line in text.splitlines():
        name, sub, lat, lon = line.split('\t')
        rows.append((name, 'road', sub or None, float(lat), float(lon)))
    db.executemany('insert into place (name, kind, sub, lat, lon) values (?, ?, ?, ?, ?)', rows)
    db.execute('create index place_name on place(name)')
    db.commit()
    db.close()
    return len(rows)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['export'])
    ap.add_argument('--places', required=True)
    a = ap.parse_args(argv)
    n, size = export(a.places)
    print('匯出 %d 個路名點，%d bytes → %s' % (n, size, os.path.relpath(ANCHORS, paths.ROOT)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
