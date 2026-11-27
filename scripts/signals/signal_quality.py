# -*- coding: utf-8 -*-
"""紅綠燈信心等級、疑似漏燈路口、Mapillary 偵測補點（2026-11-26）。

1. candidates：Mapillary map_features 號誌燈頭偵測（common/mly_features.py 產出；照片數只對候選補抓）→ 補點候選，
   寫進 data/gov-signals/gov_signals_extra.csv（source=mapillary-detect；舊的同來源列先刪再重算）。
   採用條件：車輛號誌類（general-upright／horizontal／single／other）、照片數 ≥2、
   偵測點 25 m 內有路口庫平面路口（三岔以上、非交流道／匝道）、該路口 40 m 內原本沒有任何號誌點。
   座標取路口座標。
2. build(rows, pbf)：對最終 taiwan-signals.bin 的每一筆算信心等級，另找「疑似未收錄號誌路口」，
   輸出 taiwan-signals-quality.bin（格式見 common/formats.py 的 read_signal_quality_bin）。

用法：
  python -I scripts/signals/signal_quality.py candidates
  python -I scripts/signals/signal_quality.py report --pbf <taiwan.osm.pbf>   # 只算統計，不寫檔
"""
import argparse, csv, json, math, os, sqlite3, sys, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from common import formats, paths  # noqa: E402
from signals import gov_signals as g  # noqa: E402
from signals.sources import GOV_SIGNAL_SOURCES  # noqa: E402

# 路口庫：本機預設讀 重機導航\資料\搜尋；CI（weekly-osm）從 offline-manifest 的 Release 資產下載後以環境變數指定
JUNCTIONS = os.environ.get('TOUROR_JUNCTIONS') or os.path.join(os.path.dirname(paths.ROOT), '資料', '搜尋', 'touror_junctions.sqlite')
EXTRA_CSV = os.path.join(paths.ROOT, 'data', 'gov-signals', 'gov_signals_extra.csv')
MLY_CSV = os.path.join(paths.RAW_SIGNALS, 'mly_traffic_lights.csv')
VEHICLE = ('general-upright', 'general-horizontal', 'general-single', 'other')
GOOD_KINDS = {2, 7, 8, 9, 10}           # 圓環、Y、多岔、十字、T（排除交流道／匝道）
STRONG_GOV = {'taipei', 'changhua', 'penghu', 'tainan', 'taichung', 'kaohsiung_tech'}
WEAK_GOV = {'tainan_shp', 'hsinchu'}     # 可能含閃光／路名地理編碼推得 → 單獨存在只算 low
MLY_SRC = {'mapillary_ai', 'mapillary_detect'}
MEDIUM_SRC = {'npa_accident', 'suspect_multi'}  # 間接／多線索推得 → 單獨存在算 medium（2026-11-28）
MATCH_M = 40.0

# quality 位元
LV_LOW, LV_MED, LV_HIGH = 1, 2, 3
B_OSM, B_GOV, B_MLY, B_USER, B_GAP = 4, 8, 16, 32, 64
R_OSM_NODE, R_MLY, R_MAJOR = 1, 2, 4


def grid_of(pts, cell=0.001):
    gr = g.Grid(cell)
    for p in pts:
        gr.cells.setdefault(gr.key(p[0], p[1]), []).append(p)
    return gr


def near_list(gr, lat, lon, r):
    ki, kj = gr.key(lat, lon)
    span = int(math.ceil(r / 100.0)) + 1
    out = []
    for di in range(-span, span + 1):
        for dj in range(-span, span + 1):
            for p in gr.cells.get((ki + di, kj + dj), ()):
                d = g.dist_m(lat, lon, p[0], p[1])
                if d <= r:
                    out.append((d, p))
    return out


def junctions(required=True):
    """平面路口 [(lat, lon, jid, name)]。required=False 時路口庫不存在回傳 None（呼叫端略過疑似點偵測）"""
    if not os.path.isfile(JUNCTIONS):
        if required:
            raise FileNotFoundError('找不到路口庫：%s' % JUNCTIONS)
        return None
    con = sqlite3.connect(JUNCTIONS)
    return [(la / 1e7, lo / 1e7, jid, name) for jid, la, lo, kind, arms, name in
            con.execute('select id, lat, lon, kind, arms, name from junction') if kind in GOOD_KINDS and arms >= 3]


def read_mly(path=MLY_CSV):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline='', encoding='utf-8') as f:
        for d in csv.DictReader(f):
            v = d['object_value'].replace('object--traffic-light--', '')
            out.append((float(d['lat']), float(d['lon']), v, int(d['images'] or 0)))
    return out


def gov_points():
    """{source key: [(lat, lon)]}（排除閃光；與 merge 同一讀法）"""
    res = {}
    for src in GOV_SIGNAL_SOURCES:
        key = src['key']
        path = os.path.join(paths.RAW_SIGNALS, src['file'])
        if not os.path.exists(path) and src.get('static_path'):
            path = os.path.join(paths.ROOT, src['static_path'], src['file'])
        if not os.path.exists(path):
            continue
        pts = []
        if src.get('fmt') == 'POINTS_CSV':
            with open(path, newline='', encoding='utf-8-sig') as f:
                for d in csv.DictReader(f):
                    if d.get('source') == src.get('csv_source'):
                        c = g.coord(src, d)
                        if c:
                            pts.append(c)
        elif src.get('fmt') == 'TAICHUNG_CSV':
            pts = g.read_taichung(path)[0]
        else:
            for d in g.read_rows(path):
                if not g.is_flashing(key, d):
                    c = g.coord(src, d)
                    if c:
                        pts.append(c)
        res[key] = pts
    return res


def candidates(rows):
    """Mapillary 偵測 → 補點列（dict，gov_signals_extra.csv 欄位）"""
    mly = [m for m in read_mly() if m[2] in VEHICLE and m[3] >= 2]
    sig = grid_of([(r[0] / 1e6, r[1] / 1e6) for r in rows])
    jg = grid_of(junctions())
    seen, out = set(), []
    for lat, lon, v, n in mly:
        js = near_list(jg, lat, lon, 25)
        if not js:
            continue
        d, j = min(js)
        if j[2] in seen or sig.near(j[0], j[1], MATCH_M):
            continue
        seen.add(j[2])
        out.append(dict(lat='%.7f' % j[0], lon='%.7f' % j[1], source='mapillary-detect',
                        name='Mapillary 號誌燈頭偵測（%d 張照片）' % n, junction_id=j[2], junction_name=j[3]))
    return out


def write_candidates(cands):
    with open(EXTRA_CSV, newline='', encoding='utf-8-sig') as f:
        rd = csv.DictReader(f)
        fields = rd.fieldnames
        keep = [d for d in rd if d.get('source') != 'mapillary-detect']
    with open(EXTRA_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(keep + cands)
    return len(keep)


def osm_extras(pbf):
    """(OSM 號誌相關節點 [(lat, lon)], 主幹道交會節點 [(lat, lon)])"""
    import osmium
    major = ('trunk', 'primary', 'secondary')
    node_roads, sig = {}, []

    class W(osmium.SimpleHandler):
        def way(self, w):
            h = w.tags.get('highway')
            if h in major:
                key = w.tags.get('name') or w.tags.get('ref') or ('w%d' % w.id)
                for n in w.nodes:
                    node_roads.setdefault(n.ref, set()).add(key)

    class N(osmium.SimpleHandler):
        def __init__(self, want):
            super().__init__()
            self.want, self.pts = want, []

        def node(self, n):
            t = n.tags
            if t.get('highway') == 'traffic_signals' or t.get('crossing') == 'traffic_signals' or t.get('crossing:signals') == 'yes':
                sig.append((n.location.lat, n.location.lon))
            if n.id in self.want and n.location.valid():
                self.pts.append((n.location.lat, n.location.lon))

    W().apply_file(pbf)
    want = {k for k, v in node_roads.items() if len(v) >= 2}
    node_roads.clear()
    nh = N(want)
    nh.apply_file(pbf)
    return sig, nh.pts


def build(rows, pbf, signals_crc=0):
    """rows：最終排序後的 [(lat_e6, lon_e6, flags)]。回傳 (quality bytes, 統計 dict)"""
    pts = [(r[0] / 1e6, r[1] / 1e6) for r in rows]
    allg = grid_of(pts)
    gov = gov_points()
    gov_g = {k: grid_of(v) for k, v in gov.items()}
    mly = read_mly()
    mly2 = grid_of([m for m in mly if m[2] in VEHICLE and m[3] >= 2])
    osm_sig, major = osm_extras(pbf) if pbf else ([], [])

    q = bytearray(len(rows))
    levels = dict(low=0, medium=0, high=0)
    combo = {}
    for i, (r, (la, lo)) in enumerate(zip(rows, pts)):
        fl = r[2]
        osm = not fl & 8                      # 非補點＝OSM 節點（含 bit1/bit2 行人號誌穿越）
        src = {k for k, gr in gov_g.items() if gr.near(la, lo, MATCH_M)}
        strong = bool(src & STRONG_GOV)
        weak = bool(src & WEAK_GOV)
        user = 'user_verified' in src
        m = bool(src & MLY_SRC) or mly2.near(la, lo, MATCH_M)
        mid = bool(src & MEDIUM_SRC)
        if user or (strong and (osm or m)) or (osm and m):
            lv = LV_HIGH
        elif osm or strong or m or mid:
            lv = LV_MED
        else:
            lv = LV_LOW                         # 只有可能含閃光／地理編碼推得的政府清單
        b = lv | (B_OSM if osm else 0) | (B_GOV if (strong or weak or 'npa_accident' in src) else 0) | (B_MLY if m else 0) | (B_USER if user else 0)
        q[i] = b
        levels[{1: 'low', 2: 'medium', 3: 'high'}[lv]] += 1
        tag = '+'.join(n for n, ok in (('osm', osm), ('gov', strong), ('gov_weak', weak and not strong), ('mly', m), ('user', user), ('mid', mid and not (osm or strong or m))) if ok) or 'none'
        combo[tag] = combo.get(tag, 0) + 1

    # 疑似未收錄號誌路口：平面路口、40 m 內沒有號誌點、且有任一線索
    osg, mlg, mjg = grid_of(osm_sig), grid_of(mly), grid_of(major)
    suspects, reasons = [], dict(osm_node=0, mly=0, major=0)
    jl = junctions(required=False)
    junctions_ok = jl is not None
    if not junctions_ok:
        # 號誌主檔與逐筆信心照常發布，只有疑似點清單留空
        print('::warning::找不到路口庫 %s，略過疑似未收錄號誌路口偵測（疑似點清單留空）' % JUNCTIONS, flush=True)
        jl = []
    for la, lo, jid, name in jl:
        if allg.near(la, lo, MATCH_M):
            continue
        rb = (R_OSM_NODE if osg.near(la, lo, 30) else 0) | (R_MLY if mlg.near(la, lo, 30) else 0) | (R_MAJOR if mjg.near(la, lo, 30) else 0)
        if rb:
            suspects.append((int(round(la * 1e6)), int(round(lo * 1e6)), rb))
            for k, bit in (('osm_node', R_OSM_NODE), ('mly', R_MLY), ('major', R_MAJOR)):
                reasons[k] += bool(rb & bit)

    # 相鄰兩盞之間有沒有疑似漏燈（直線近鄰近似：每盞取 80–1000 m 內最近 4 盞為相鄰段）
    sg = grid_of([(s[0] / 1e6, s[1] / 1e6) for s in suspects])
    usable = [i for i, r in enumerate(rows) if r[2] & 1 and not r[2] & (16 | 32)]
    ug = grid_of([(pts[i][0], pts[i][1], i) for i in usable], cell=0.005)
    pairs = set()
    for i in usable:
        la, lo = pts[i]
        nb = sorted((d, p[2]) for d, p in near_list(ug, la, lo, 1000) if d >= 80)[:4]
        for d, j in nb:
            pairs.add((min(i, j), max(i, j)))
    gap_pairs = 0
    for i, j in pairs:
        (a1, o1), (a2, o2) = pts[i], pts[j]
        L = g.dist_m(a1, o1, a2, o2)
        mid = ((a1 + a2) / 2, (o1 + o2) / 2)
        hit = False
        for d, p in near_list(sg, mid[0], mid[1], L / 2 + 30):
            da, db = g.dist_m(a1, o1, p[0], p[1]), g.dist_m(a2, o2, p[0], p[1])
            if da < 40 or db < 40:
                continue
            s = (da + db + L) / 2
            h = 2 * math.sqrt(max(s * (s - da) * (s - db) * (s - L), 0)) / L
            if h <= 25 and da < L and db < L:
                hit = True
                break
        if hit:
            gap_pairs += 1
            q[i] |= B_GAP
            q[j] |= B_GAP
    data = formats.write_signal_quality_bin(q, suspects, signals_crc=signals_crc)
    st = dict(levels=levels, sources=combo, suspects=len(suspects), suspect_reasons=reasons,
              pairs=len(pairs), gap_pairs=gap_pairs, gap_pair_ratio=round(gap_pairs / max(len(pairs), 1), 4),
              gap_signals=sum(1 for b in q if b & B_GAP), mly_detections=len(mly),
              junctions_available=junctions_ok)
    return bytes(data), st


def coverage(rows):
    """各政府清單的收錄率：只看 OSM（bit0 且非補點）與合併後（全部點），30／50 m"""
    osm = grid_of([(r[0] / 1e6, r[1] / 1e6) for r in rows if r[2] & 1 and not r[2] & 8])
    allg = grid_of([(r[0] / 1e6, r[1] / 1e6) for r in rows])
    out = {}
    for k, v in gov_points().items():
        if not v or k in ('user_verified', 'mapillary_ai', 'mapillary_detect'):
            continue
        n = len(v)
        out[k] = dict(n=n, **{'%s_%d' % (nm, r): round(100.0 * sum(gr.near(a, b, r) for a, b in v) / n, 1)
                              for nm, gr in (('osm', osm), ('merged', allg)) for r in (30, 50)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['candidates', 'report'])
    ap.add_argument('--bin', default=os.path.join(paths.ROOT, 'data', 'signals', 'taiwan-signals.bin'))
    ap.add_argument('--pbf')
    a = ap.parse_args()
    _, rows = formats.read_signals_bin(open(a.bin, 'rb').read())
    if a.cmd == 'candidates':
        c = candidates(rows)
        kept = write_candidates(c)
        print(json.dumps(dict(mapillary_detect=len(c), other_rows=kept), ensure_ascii=False))
    else:
        print(json.dumps(dict(coverage=coverage(rows)), ensure_ascii=False))
        if a.pbf:
            _, st = build(rows, a.pbf)
            print(json.dumps(st, ensure_ascii=False))


if __name__ == '__main__':
    main()
