# -*- coding: utf-8 -*-
"""Mapillary 號誌燈頭偵測（map_features 的 object--traffic-light--* 類）全台下載。

用法：python -I scripts/signals/fetch_mly_signals.py [--out raw/signals/mly_traffic_lights.csv] [--workers 6]
分塊：以路口庫（資料/搜尋/touror_junctions.sqlite）有路口的 z13 圖磚為範圍；單塊回傳達上限 2000 就切成 4 個 z14 再查。
輸出欄位：feature_id, object_value, lat, lon, images（照片數）, first_seen, last_seen
金鑰：%USERPROFILE%\\.claude\\secrets\\mapillary-token.txt 或環境變數 MAPILLARY_TOKEN（只在本行程內讀，不印出、不寫檔）。
"""
import argparse, csv, json, math, os, sqlite3, sys, time, urllib.parse, urllib.request, concurrent.futures as cf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
JUNCTIONS = os.path.join(os.path.dirname(ROOT), '資料', '搜尋', 'touror_junctions.sqlite')
# Mapillary 分類中號誌燈頭的 object_value（2026-11 實查 API 回傳值）
VALUES = ['object--traffic-light--general-upright', 'object--traffic-light--general-horizontal',
          'object--traffic-light--general-single', 'object--traffic-light--pedestrians',
          'object--traffic-light--cyclists', 'object--traffic-light--other']
LIMIT = 2000


def token():
    t = os.environ.get('MAPILLARY_TOKEN')
    if t:
        return t.strip()
    return open(os.path.join(os.path.expanduser('~'), '.claude', 'secrets', 'mapillary-token.txt')).read().strip()


def tile_of(lat, lon, z):
    n = 2 ** z
    return int((lon + 180) / 360 * n), int((1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n)


def tile_bbox(x, y, z):
    n = 2 ** z
    def ll(fx, fy):
        return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * fy / n)))), fx / n * 360 - 180
    la1, lo1 = ll(x, y)
    la2, lo2 = ll(x + 1, y + 1)
    return min(lo1, lo2), min(la1, la2), max(lo1, lo2), max(la1, la2)


def query(bbox, tok):
    q = dict(access_token=tok, fields='id,object_value,geometry,first_seen_at,last_seen_at,images',
             bbox='%.7f,%.7f,%.7f,%.7f' % bbox, object_values=','.join(VALUES), limit=LIMIT)
    url = 'https://graph.mapillary.com/map_features?' + urllib.parse.urlencode(q)
    for i in range(4):
        try:
            return json.load(urllib.request.urlopen(url, timeout=180)).get('data', [])
        except Exception:
            time.sleep(3 * (i + 1))
    return None


def fetch(xyz, tok):
    x, y, z = xyz
    d = query(tile_bbox(x, y, z), tok)
    if d is None:
        return xyz, None, 1
    if len(d) >= LIMIT and z < 17:
        out, fail = [], 0
        for cx in (2 * x, 2 * x + 1):
            for cy in (2 * y, 2 * y + 1):
                _, o, f = fetch((cx, cy, z + 1), tok)
                fail += f
                out += o or []
        return xyz, out, fail
    out = []
    for f in d:
        g = f.get('geometry') or {}
        if g.get('type') != 'Point':
            continue
        lon, lat = g['coordinates']
        imgs = (f.get('images') or {}).get('data', [])
        out.append([f['id'], f.get('object_value', ''), '%.7f' % lat, '%.7f' % lon, len(imgs),
                    (f.get('first_seen_at') or '')[:10], (f.get('last_seen_at') or '')[:10]])
    return xyz, out, 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=os.path.join(ROOT, 'raw', 'signals', 'mly_traffic_lights.csv'))
    ap.add_argument('--workers', type=int, default=6)
    ap.add_argument('--z', type=int, default=13)
    a = ap.parse_args()
    con = sqlite3.connect(JUNCTIONS)
    tiles = sorted({tile_of(la / 1e7, lo / 1e7, a.z) for la, lo in con.execute('select lat, lon from junction')})
    print('tiles', len(tiles), flush=True)
    tok = token()
    rows, fail, done = {}, 0, 0
    with cf.ThreadPoolExecutor(a.workers) as ex:
        for xyz, out, f in ex.map(lambda t: fetch((t[0], t[1], a.z), tok), tiles):
            done += 1
            fail += f
            for r in out or []:
                rows[r[0]] = r
            if done % 200 == 0:
                print('done', done, 'features', len(rows), 'fail', fail, flush=True)
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['feature_id', 'object_value', 'lat', 'lon', 'images', 'first_seen', 'last_seen'])
        w.writerows(rows.values())
    print('features', len(rows), 'failed_tiles', fail, flush=True)
    return 0 if fail == 0 else 3


if __name__ == '__main__':
    sys.exit(main())
