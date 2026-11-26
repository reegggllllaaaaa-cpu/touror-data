# -*- coding: utf-8 -*-
"""Mapillary map_features 全台下載器（號誌燈頭＋機車標誌共用，可續跑的平行佇列）。

用法：
  python -I scripts/common/mly_features.py download [--workers 12]   # 下載（每塊結果落地快取，中斷後重跑會續傳）
  python -I scripts/common/mly_features.py images [--workers 16]     # 只對號誌補點候選補抓照片數（map_features 的 images 欄很慢，下載時不抓）
  python -I scripts/common/mly_features.py export                    # 由快取輸出 raw/signals/mly_traffic_lights.csv 與 data/rules/mapillary_moto_signs.csv
  python -I scripts/common/mly_features.py all                       # 依序做上面三步

分塊：路口庫（資料/搜尋/touror_junctions.sqlite）有路口的 z13 圖磚；單塊回傳達上限 2000 就自動四分成 z+1 再查（佇列內平行跑，
      切過的塊寫 .split 標記，續跑時直接跳到子塊）。失敗以指數退避重試，仍失敗的塊不寫快取，下次重跑會補。
快取：raw/mly_cache/tiles/{z}_{x}_{y}.json、raw/mly_cache/images.json（feature_id → 照片數）。
本行程自動降為低優先權（Windows BELOW_NORMAL）。
金鑰：%USERPROFILE%\\.claude\\secrets\\mapillary-token.txt 或環境變數 MAPILLARY_TOKEN（只在本行程內讀，不印出、不寫檔）。
"""
import argparse, csv, json, math, os, random, sqlite3, sys, threading, time, datetime
import urllib.error, urllib.parse, urllib.request, concurrent.futures as cf

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
JUNCTIONS = os.path.join(os.path.dirname(ROOT), '資料', '搜尋', 'touror_junctions.sqlite')
CACHE = os.path.join(ROOT, 'raw', 'mly_cache')
TILES = os.path.join(CACHE, 'tiles')
IMAGES = os.path.join(CACHE, 'images.json')
SIG_CSV = os.path.join(ROOT, 'raw', 'signals', 'mly_traffic_lights.csv')
MOTO_CSV = os.path.join(ROOT, 'data', 'rules', 'mapillary_moto_signs.csv')

# 號誌燈頭（2026-11 實查 API 回傳值）
SIGNAL_VALUES = ['object--traffic-light--general-upright', 'object--traffic-light--general-horizontal',
                 'object--traffic-light--general-single', 'object--traffic-light--pedestrians',
                 'object--traffic-light--cyclists', 'object--traffic-light--other']
# 機車標誌：遵20（Mapillary 用日本類別名，圖形相同）、禁行機車
MOTO_KIND = {'regulatory--two-stage-right-turn-for-mopeds--g1': 'zun20',
             'regulatory--no-motorcycles--g1': 'no_moto', 'regulatory--no-motorcycles--g2': 'no_moto'}
VALUES = SIGNAL_VALUES + list(MOTO_KIND)
VEHICLE = ('general-upright', 'general-horizontal', 'general-single', 'other')
GOOD_KINDS = {2, 7, 8, 9, 10}
LIMIT, Z0, ZMAX = 2000, 13, 19


def low_priority():
    if os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)
    else:
        os.nice(10)


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


def get_json(url, tries=7):
    """指數退避：1,2,4,…秒＋抖動；429／5xx 多等。全部失敗回 None。"""
    for i in range(tries):
        try:
            return json.load(urllib.request.urlopen(url, timeout=120))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                return {}
            wait = (2 ** i) * (3 if e.code == 429 else 1)
        except Exception:
            wait = 2 ** i
        time.sleep(min(wait, 120) + random.random())
    return None


def day(v):
    if v in (None, ''):
        return ''
    if isinstance(v, (int, float)) or str(v).isdigit():
        return datetime.datetime.utcfromtimestamp(int(v) / 1000).strftime('%Y-%m-%d')
    return str(v)[:10]


def tile_path(x, y, z, ext='json'):
    return os.path.join(TILES, '%d_%d_%d.%s' % (z, x, y, ext))


def query_tile(xyz, tok):
    x, y, z = xyz
    q = dict(access_token=tok, fields='id,object_value,geometry,first_seen_at,last_seen_at,aligned_direction',
             bbox='%.7f,%.7f,%.7f,%.7f' % tile_bbox(x, y, z), object_values=','.join(VALUES), limit=LIMIT)
    d = get_json('https://graph.mapillary.com/map_features?' + urllib.parse.urlencode(q))
    if d is None:
        return None
    out = []
    for f in d.get('data', []):
        g = f.get('geometry') or {}
        if g.get('type') != 'Point':
            continue
        lon, lat = g['coordinates']
        ad = f.get('aligned_direction')
        out.append([str(f['id']), f.get('object_value', ''), round(lat, 7), round(lon, 7),
                    day(f.get('first_seen_at')), day(f.get('last_seen_at')), '' if ad is None else ad])
    return out


def base_tiles():
    con = sqlite3.connect(JUNCTIONS)
    return sorted({tile_of(la / 1e7, lo / 1e7, Z0) for la, lo in con.execute('select lat, lon from junction')})


def download(workers):
    os.makedirs(TILES, exist_ok=True)
    tok = token()
    st = dict(queried=0, cached=0, split=0, fail=0, leaf=0)
    lock = threading.Lock()
    t0 = time.time()

    def children(x, y, z):
        return [(cx, cy, z + 1) for cx in (2 * x, 2 * x + 1) for cy in (2 * y, 2 * y + 1)]

    def work(xyz):
        x, y, z = xyz
        if os.path.exists(tile_path(x, y, z, 'split')):
            return children(x, y, z)
        if os.path.exists(tile_path(x, y, z)):
            with lock:
                st['cached'] += 1; st['leaf'] += 1
            return []
        out = query_tile(xyz, tok)
        with lock:
            st['queried'] += 1
        if out is None:
            with lock:
                st['fail'] += 1
            return []
        if len(out) >= LIMIT and z < ZMAX:
            open(tile_path(x, y, z, 'split'), 'w').close()
            with lock:
                st['split'] += 1
            return children(x, y, z)
        tmp = tile_path(x, y, z) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(out, f, separators=(',', ':'))
        os.replace(tmp, tile_path(x, y, z))
        with lock:
            st['leaf'] += 1
        return []

    base = [(x, y, Z0) for x, y in base_tiles()]
    print('base tiles', len(base), flush=True)
    with cf.ThreadPoolExecutor(workers) as ex:
        pend = {ex.submit(work, t) for t in base}
        last = 0
        while pend:
            done, pend = cf.wait(pend, return_when=cf.FIRST_COMPLETED)
            for fu in done:
                for c in fu.result():
                    pend.add(ex.submit(work, c))
            if time.time() - last > 60:
                last = time.time()
                print('%ds' % (last - t0), st, 'pending', len(pend), flush=True)
    st['seconds'] = round(time.time() - t0)
    print('download done', st, flush=True)
    return st


def load_features():
    feats = {}
    for fn in os.listdir(TILES):
        if fn.endswith('.json'):
            for r in json.load(open(os.path.join(TILES, fn), encoding='utf-8')):
                feats[r[0]] = r
    return feats


def load_images():
    return json.load(open(IMAGES, encoding='utf-8')) if os.path.exists(IMAGES) else {}


def image_targets(feats):
    """補抓照片數的對象：車用號誌燈頭、距合格路口（圓環／Y／多岔／十字／T、≥3 岔）40 m 內。"""
    sys.path.insert(0, os.path.dirname(HERE))
    from signals import gov_signals as g
    gr = g.Grid(0.001)
    con = sqlite3.connect(JUNCTIONS)
    for la, lo, kind, arms in con.execute('select lat, lon, kind, arms from junction'):
        if kind in GOOD_KINDS and arms >= 3:
            gr.cells.setdefault(gr.key(la / 1e7, lo / 1e7), []).append((la / 1e7, lo / 1e7))
    return [fid for fid, r in feats.items()
            if r[1].replace('object--traffic-light--', '') in VEHICLE and gr.near(r[2], r[3], 40.0)]


def images(workers):
    feats = load_features()
    cache = load_images()
    todo = [f for f in image_targets(feats) if f not in cache]
    print('image targets todo', len(todo), 'cached', len(cache), flush=True)
    tok = token()
    t0, fail = time.time(), 0

    def one(fid):
        d = get_json('https://graph.mapillary.com/%s?%s' % (fid, urllib.parse.urlencode(dict(access_token=tok, fields='images'))), tries=5)
        return fid, None if d is None else len((d.get('images') or {}).get('data', []))

    with cf.ThreadPoolExecutor(workers) as ex:
        for i, (fid, n) in enumerate(ex.map(one, todo), 1):
            if n is None:
                fail += 1
            else:
                cache[fid] = n
            if i % 2000 == 0 or i == len(todo):
                tmp = IMAGES + '.tmp'
                json.dump(cache, open(tmp, 'w', encoding='utf-8'))
                os.replace(tmp, IMAGES)
                print('images', i, '/', len(todo), 'fail', fail, '%ds' % (time.time() - t0), flush=True)
    return dict(image_lookups=len(todo), image_fail=fail, image_seconds=round(time.time() - t0))


def export():
    feats = load_features()
    imgs = load_images()
    sig = [r for r in feats.values() if r[1] in SIGNAL_VALUES]
    moto = [r for r in feats.values() if r[1] in MOTO_KIND]
    os.makedirs(os.path.dirname(SIG_CSV), exist_ok=True)
    with open(SIG_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['feature_id', 'object_value', 'lat', 'lon', 'images', 'first_seen', 'last_seen'])
        for r in sorted(sig):
            w.writerow([r[0], r[1], '%.7f' % r[2], '%.7f' % r[3], imgs.get(r[0], ''), r[4], r[5]])
    with open(MOTO_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['feature_id', 'kind', 'object_value', 'lat', 'lon', 'first_seen', 'last_seen', 'aligned_direction'])
        for r in sorted(moto):
            w.writerow([r[0], MOTO_KIND[r[1]], r[1], '%.7f' % r[2], '%.7f' % r[3], r[4], r[5], r[6]])
    from collections import Counter
    st = dict(signal_detections=len(sig), signal_with_images=sum(1 for r in sig if r[0] in imgs),
              moto=dict(Counter(MOTO_KIND[r[1]] for r in moto)))
    print('export', st, flush=True)
    return st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['download', 'images', 'export', 'all'])
    ap.add_argument('--workers', type=int, default=12)
    a = ap.parse_args()
    low_priority()
    if a.cmd in ('download', 'all'):
        st = download(a.workers)
        if st['fail'] and a.cmd == 'all':
            print('有失敗塊，重跑 download 會續傳', flush=True)
            return 3
    if a.cmd in ('images', 'all'):
        images(a.workers)
    if a.cmd in ('export', 'all'):
        export()
    return 0


if __name__ == '__main__':
    sys.exit(main())
