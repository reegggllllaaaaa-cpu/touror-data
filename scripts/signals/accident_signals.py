# -*- coding: utf-8 -*-
"""警政署交通事故資料 → 號誌化路口補點；疑似漏燈路口多線索升級（2026-11-28）。

資料（政府資料開放授權條款第 1 版）：
  13139  即時交通事故資料(A2類)（當年度逐月 ZIP）
  158865 傷亡道路交通事故資料（A1／A2 逐案，年度 ZIP）
原始 ZIP 放 raw/signals/npa_accidents/（gitignore，不進版控）；逐列串流讀，不整檔載入。

1. candidates（工作 1）：
   - 一列是一位當事者 → 以（日期, 時間, 經緯度）去重成一件事故。
   - 只取「號誌種類」以「行車管制號誌」開頭（含附設行人專用號誌；閃光號誌、無號誌排除）且「道路型態大類別＝交岔路」。
   - 30 m 內的事故聚成一群（群心＝成員平均），事故數 ≥ 3 才採用。
   - 群心 30 m 內最近的路口庫平面路口（三岔以上、非交流道／匝道＝兩條以上道路交會）為吸附點；沒有就不採用。
   - 該路口 40 m 內已有號誌點（taiwan-signals.bin 或補點 CSV 其他來源）就不加。
   - 寫進 data/gov-signals/gov_signals_extra.csv（source=npa-accident；舊的同來源列先刪再重算）。
2. upgrade（工作 2）：taiwan-signals-quality.bin 的疑似未收錄號誌路口，獨立線索（OSM 號誌節點、Mapillary 燈頭、
   主幹道交會、事故資料：30 m 內有號誌化交岔路口事故）≥ 2 類者升級為正式號誌（source=suspect-multi）；單一線索不升級。
3. sample：抽 N 件符合條件的事故，看到最近路口的距離分布（判斷座標準確度用）。

用法：
  python -I scripts/signals/accident_signals.py fetch
  python -I scripts/signals/accident_signals.py candidates
  python -I scripts/signals/accident_signals.py upgrade
  python -I scripts/signals/accident_signals.py sample --n 20
"""
import argparse, csv, glob, io, json, os, random, sys, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from common import formats, paths  # noqa: E402
from signals import gov_signals as g  # noqa: E402
from signals import signal_quality as sq  # noqa: E402

DATASETS = ('13139', '158865')
ACC_DIR = os.path.join(paths.RAW_SIGNALS, 'npa_accidents')
SIG_BIN = os.path.join(paths.ROOT, 'data', 'signals', 'taiwan-signals.bin')
QUALITY_BIN = os.path.join(paths.ROOT, 'data', 'signals', 'taiwan-signals-quality.bin')
SRC_ACC = 'npa-accident'
SRC_UPGRADE = 'suspect-multi'
OWN_SOURCES = (SRC_ACC, SRC_UPGRADE)

CLUSTER_M = 30.0       # 事故聚群半徑
MIN_ACCIDENTS = 3      # 一群至少幾件才採用
SNAP_M = 30.0          # 群心吸附路口的最大距離
EXISTING_M = 40.0      # 路口 40 m 內已有號誌就不加
CLUE_M = 30.0          # 疑似點與事故群的命中距離（工作 2）
CLUE_MIN_ACCIDENTS = 1  # 事故線索至少幾件（工作 2）
R_ACC = 8              # 工作 2 內部用的事故線索位元（不寫進 TSGQ）
CLUE_NAMES = ((sq.R_OSM_NODE, 'osm'), (sq.R_MLY, 'mly'), (sq.R_MAJOR, 'major'), (R_ACC, 'accident'))


# ---------- 讀取與過濾 ----------
def is_signal_junction(d):
    """號誌種類＝行車管制號誌（含附設行人專用號誌），且道路型態大類別＝交岔路。閃光號誌、無號誌不收。"""
    kind = (d.get('號誌-號誌種類名稱') or '').strip()
    road = (d.get('道路型態大類別名稱') or '').strip()
    return kind.startswith('行車管制號誌') and '閃光' not in kind and road == '交岔路'


def iter_rows(acc_dir=ACC_DIR):
    """逐列串流讀所有 ZIP 內的事故 CSV（每列一位當事者）。"""
    for zp in sorted(glob.glob(os.path.join(acc_dir, '*.zip'))):
        with zipfile.ZipFile(zp) as z:
            for info in z.infolist():
                if not info.filename.lower().endswith('.csv') or info.file_size < 4096:
                    continue  # 跳過 manifest／schema 小檔
                with z.open(info) as fh:
                    head = fh.read(4)
                with z.open(info) as fh:
                    enc = 'utf-8-sig' if head[:3] == b'\xef\xbb\xbf' or _looks_utf8(z, info) else 'cp950'
                    for d in csv.DictReader(io.TextIOWrapper(fh, encoding=enc, errors='replace', newline='')):
                        yield d


def _looks_utf8(z, info):
    with z.open(info) as fh:
        b = fh.read(65536)
    try:
        b.decode('utf-8')
        return True
    except UnicodeDecodeError as e:
        return e.start > len(b) - 4  # 只是截斷在多位元組中間


def accidents(rows):
    """回傳 (符合條件的事故 [(lat, lon)], 統計)。以（日期, 時間, 地點, 經緯度）去重當事者列。"""
    st = dict(rows=0, accidents=0, matched=0, bad_coord=0)
    seen_all, seen, out = set(), set(), []
    for d in rows:
        st['rows'] += 1
        key = (d.get('發生日期'), d.get('發生時間'), d.get('發生地點'), d.get('經度'), d.get('緯度'))
        if key not in seen_all:
            seen_all.add(key)
            st['accidents'] += 1
        if key in seen or not is_signal_junction(d):
            continue
        seen.add(key)
        c = g.coord(dict(lat=('緯度',), lon=('經度',)), d)
        if c is None:
            st['bad_coord'] += 1
            continue
        st['matched'] += 1
        out.append(c)
    return out, st


# ---------- 聚群與吸附 ----------
def cluster(points, radius=CLUSTER_M):
    """貪婪聚群：每點併進 radius 內最近的群（群心取平均），否則自成一群。回傳 [(lat, lon, n)]。"""
    gr = g.Grid()
    cl = []  # [sum_lat, sum_lon, n]
    for lat, lon in sorted(points):
        best = None
        for d, p in sq.near_list(gr, lat, lon, radius):
            if best is None or d < best[0]:
                best = (d, p)
        if best is None:
            idx = len(cl)
            cl.append([lat, lon, 1])
            gr.cells.setdefault(gr.key(lat, lon), []).append((lat, lon, idx))
        else:
            c = cl[best[1][2]]
            c[0] += lat
            c[1] += lon
            c[2] += 1
    return [(s_la / n, s_lo / n, n) for s_la, s_lo, n in cl]


def snap(clusters, junctions, existing, min_n=MIN_ACCIDENTS, snap_m=SNAP_M, existing_m=EXISTING_M):
    """clusters [(lat, lon, n)]；junctions [(lat, lon, id, name)]；existing [(lat, lon)] 現有號誌。
    回傳 (補點 [(j_lat, j_lon, jid, jname, n, snap_dist)], 統計)。同一路口多群時事故數相加。"""
    jg = sq.grid_of(junctions)
    eg = sq.grid_of(existing)
    st = dict(clusters=len(clusters), clusters_ge_min=0, no_junction=0, has_signal=0, added=0)
    by_j = {}
    for lat, lon, n in clusters:
        if n < min_n:
            continue
        st['clusters_ge_min'] += 1
        js = sq.near_list(jg, lat, lon, snap_m)
        if not js:
            st['no_junction'] += 1
            continue
        d, j = min(js, key=lambda x: x[0])
        if eg.near(j[0], j[1], existing_m):
            st['has_signal'] += 1
            continue
        if j[2] in by_j:
            by_j[j[2]][4] += n
        else:
            by_j[j[2]] = [j[0], j[1], j[2], j[3], n, d]
    out = [tuple(v) for v in by_j.values()]
    st['added'] = len(out)
    return out, st


# ---------- 疑似點升級 ----------
def suspect_clues(suspects, acc_clusters, clue_m=CLUE_M, min_n=CLUE_MIN_ACCIDENTS):
    """suspects [(lat_e6, lon_e6, bits)] → [(lat, lon, bits含事故位元)]"""
    ag = sq.grid_of([(la, lo) for la, lo, n in acc_clusters if n >= min_n])
    out = []
    for a, b, bits in suspects:
        la, lo = a / 1e6, b / 1e6
        out.append((la, lo, bits | (R_ACC if ag.near(la, lo, clue_m) else 0)))
    return out


def upgrade(suspects_with_clues, existing=(), existing_m=EXISTING_M):
    """線索 ≥ 2 類才升級；單一線索不升級。existing：已有號誌點（含工作 1 新增），40 m 內重複不加。
    回傳 (升級點 [(lat, lon, bits)], 組合統計 {組合: 數量})。"""
    eg = sq.grid_of(list(existing))
    up, combos = [], {}
    for la, lo, bits in suspects_with_clues:
        names = [nm for bit, nm in CLUE_NAMES if bits & bit]
        if len(names) < 2:
            continue
        tag = '+'.join(names)
        if eg.near(la, lo, existing_m):
            combos['(已被其他補點涵蓋)' + tag] = combos.get('(已被其他補點涵蓋)' + tag, 0) + 1
            continue
        combos[tag] = combos.get(tag, 0) + 1
        up.append((la, lo, bits))
    return up, combos


# ---------- 檔案 ----------
def existing_points(exclude=OWN_SOURCES):
    _, rows = formats.read_signals_bin(open(SIG_BIN, 'rb').read())
    pts = [(r[0] / 1e6, r[1] / 1e6) for r in rows]
    with open(sq.EXTRA_CSV, newline='', encoding='utf-8-sig') as f:
        for d in csv.DictReader(f):
            if d.get('source') not in exclude:
                c = g.coord(dict(lat=('lat',), lon=('lon',)), d)
                if c:
                    pts.append(c)
    return pts


def write_rows(source, new_rows):
    with open(sq.EXTRA_CSV, newline='', encoding='utf-8-sig') as f:
        rd = csv.DictReader(f)
        fields = rd.fieldnames
        keep = [d for d in rd if d.get('source') != source]
    with open(sq.EXTRA_CSV, 'w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(keep + new_rows)


def fetch():
    from common import datagov
    os.makedirs(ACC_DIR, exist_ok=True)
    got = []
    for ds in DATASETS:
        info = datagov.dataset_info(ds)
        for i, x in enumerate(info.get('distribution', [])):
            name = (x.get('resourceDescription') or str(i)).replace('.zip', '')
            fn = os.path.join(ACC_DIR, '%s_%s.zip' % (ds, name))
            if not os.path.exists(fn):
                b = datagov.http_get(x['resourceDownloadUrl'], timeout=600)
                with open(fn + '.part', 'wb') as f:
                    f.write(b)
                os.replace(fn + '.part', fn)
            got.append(os.path.basename(fn))
    return got


def load_clusters():
    pts, st = accidents(iter_rows())
    return pts, cluster(pts), st


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['fetch', 'candidates', 'upgrade', 'sample'])
    ap.add_argument('--n', type=int, default=20)
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    if a.cmd == 'fetch':
        print(json.dumps(fetch(), ensure_ascii=False))
        return
    pts, cls, st = load_clusters()
    if a.cmd == 'sample':
        jg = sq.grid_of(sq.junctions())
        rnd = random.Random(20261128)
        sample = rnd.sample(pts, min(a.n, len(pts)))
        ds = []
        for la, lo in sample:
            near = sq.near_list(jg, la, lo, 300)
            ds.append(round(min(d for d, _ in near), 1) if near else None)
        allnear = []
        for la, lo in rnd.sample(pts, min(2000, len(pts))):
            near = sq.near_list(jg, la, lo, 300)
            allnear.append(min(d for d, _ in near) if near else 999)
        allnear.sort()
        pct = {p: round(allnear[int(len(allnear) * p / 100) - 1], 1) for p in (25, 50, 75, 90, 95)}
        within = {r: round(100.0 * sum(d <= r for d in allnear) / len(allnear), 1) for r in (10, 20, 30, 50)}
        print(json.dumps(dict(sample20=[(round(la, 6), round(lo, 6), d) for (la, lo), d in zip(sample, ds)],
                              n2000_percentile_m=pct, n2000_within_pct=within), ensure_ascii=False))
        return
    if a.cmd == 'candidates':
        added, sst = snap(cls, sq.junctions(), existing_points())
        st.update(sst)
        rows = [dict(lat='%.7f' % la, lon='%.7f' % lo, source=SRC_ACC,
                     name='警政署事故資料：號誌化路口事故 %d 件' % n, junction_id=jid, junction_name=jn)
                for la, lo, jid, jn, n, d in sorted(added, key=lambda x: x[2])]
        if not a.dry_run:
            write_rows(SRC_ACC, rows)
        st['snap_dist_p50'] = sorted(x[5] for x in added)[len(added) // 2] if added else None
        print(json.dumps(st, ensure_ascii=False))
        return
    _, _, sus = formats.read_signal_quality_bin(open(QUALITY_BIN, 'rb').read())
    sus = [(s[0], s[1], s[2]) for s in sus] if sus and len(sus[0]) == 3 else sus
    with_clues = suspect_clues(sus, cls)
    up, combos = upgrade(with_clues, existing=existing_points(exclude=(SRC_UPGRADE,)))
    rows = [dict(lat='%.7f' % la, lon='%.7f' % lo, source=SRC_UPGRADE,
                 name='疑似漏燈路口多線索升級：' + '+'.join(nm for bit, nm in CLUE_NAMES if bits & bit),
                 junction_id='', junction_name='') for la, lo, bits in up]
    if not a.dry_run:
        write_rows(SRC_UPGRADE, rows)
    acc_hits = sum(1 for s in with_clues if s[2] & R_ACC)
    print(json.dumps(dict(suspects=len(sus), accident_clue_hits=acc_hits, upgraded=len(up), combos=combos), ensure_ascii=False))


if __name__ == '__main__':
    main()
