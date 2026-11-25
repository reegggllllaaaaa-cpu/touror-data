# -*- coding: utf-8 -*-
"""店家庫路名／門牌獨立表 place_addr（導航終點依店家地址吸附到店面所在路用）。

不改既有表與欄位：只在店家庫 sqlite 內「新增」兩張表 place_addr、place_addr_meta（舊版 App 明列欄位查詢，不受影響）。
place_addr(place_id PK＝place.id, city, district, road（路名含段，例 忠孝東路4段）, road_base, section, road_kind（路／街／大道／道／線）,
           lane（巷，例 51、6之16）, alley（弄）, number（門牌號，例 136、665之20；無號碼為 NULL）, source)
正規化：與門牌包同一套（build_addr.normalize／parse_tail）：NFKC、臺→台、中文數字在「段巷弄號樓之」前轉阿拉伯數字、數字間連字號→之；
        再去郵遞區號、縣市、區鄉鎮市、村里鄰；路名必須以 路／街／大道／道／線 結尾才寫入（村里門牌、說明文字、純英文地址列入缺口，不寫入）。
來源優先序：osm（raw.pkl 的 addr:street／addr:housenumber，座標＋名稱配對）> overture（Overture 原始地址，座標＋名稱配對）> gov（政府詳細資料 address）
          > note（庫內 note 欄地址）> name（店名括號內的路名門牌）；有門牌者優先於只有路名者。

用法（Python 讀外部資料一律加 -I）：
  python -I build_place_addr.py --kind main     --src 原庫.sqlite --out 新庫.sqlite --raw raw.pkl --report-dir 報告目錄
  python -I build_place_addr.py --kind overture --src 原庫.sqlite --out 新庫.sqlite --flat flat.parquet --osm-roads _work_en/osm_road_names.pkl --report-dir ...
  （osm_road_names.pkl 由 extract_osm_road_names.py 從 taiwan-latest.osm.pbf 產生；英文轉換邏輯在 en_road.py）
  python -I build_place_addr.py --kind gov      --src 原庫.sqlite --out 新庫.sqlite --report-dir ...
  python -I build_place_addr.py --verify --kind main --src 原庫.sqlite --out 新庫.sqlite   # 只做一致性檢查
"""
import argparse, collections, csv, hashlib, importlib.util, json, os, pickle, re, shutil, sqlite3, sys, time, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _load(name, fn):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, fn))
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m); return m


ba = _load('build_addr', 'build_addr.py')
normalize, parse_tail, CITIES = ba.normalize, ba.parse_tail, ba.CITIES
SKIP_KINDS = {'road'}                    # 道路段本身沒有店面地址
ROAD_KIND_RE = re.compile(r'(大道|道|路|街|線|巷|弄)$')
VILLAGE_RE = ba.RE_VILLAGE
NEIGH_RE = ba.RE_NEIGHBOR
POST_RE = ba.RE_POSTCODE
DIST_END = re.compile(r'[區鄉鎮市]$')
CJK = re.compile(r'[一-鿿]')
NAME_PAREN = re.compile(r'[（(]([^（()）]*?)[）)]\s*$')


def has_cjk(s):
    return bool(CJK.search(s or ''))


def split_sub(sub):
    sub = normalize(sub or '')
    city = next((c for c in CITIES if sub.startswith(c)), None)
    rest = sub[len(city):] if city else sub
    dist = rest if rest and DIST_END.search(rest) and len(rest) <= 4 else None
    return city, dist


def parse_loose(text, dists):
    """text → (dict|None, reason)。dict: city,district,road,road_base,section,road_kind,lane,alley,number"""
    if not text or not text.strip():
        return None, '空'
    if not has_cjk(text):
        return None, '純英文地址'
    s = normalize(text)
    if re.search(r'[;；]', s):                       # 「信義路;三民路52號」：取最後一段（帶門牌的那個）
        s = re.split(r'[;；]', s)[-1]
    s = re.sub(r'([0-9]+(?:之[0-9]+)?)[、,，][0-9、,，之]+(?=號)', r'', s)   # 「169、171號」取第一個號
    head, section, lane, lane_sub, alley, alley_sub, main, sub = parse_tail(s)
    head = POST_RE.sub('', head, count=1)
    city = None
    for c in CITIES:
        if head.startswith(c):
            city = c; head = head[len(c):]; break
    district = None
    best = ''
    for d in dists:
        if len(d) > len(best) and head.startswith(d) and len(head) > len(d):
            best = d
    if best:
        district = best; head = head[len(best):]
    for _ in range(2):                              # 「嘉義市嘉義市博愛路」「東區東區」：縣轄市名重複出現
        for c in CITIES:
            if head.startswith(c) and len(head) > len(c):
                city = city or c; head = head[len(c):]; break
        best = ''
        for d in dists:
            if len(d) > len(best) and head.startswith(d) and len(head) > len(d):
                best = d
        if best:
            district = district or best; head = head[len(best):]
    head = VILLAGE_RE.sub('', head, count=1)
    head = NEIGH_RE.sub('', head, count=1)
    if not head:
        return None, '只有行政區'
    if len(head) > 24 or re.search(r'[，,、;；。:：\s]', head):
        return None, '說明文字非地址'
    if re.search(r'[0-9]$', head):
        return None, '路名以數字結尾'
    m = ROAD_KIND_RE.search(head)
    if not m:
        return None, '非路名（村里／地名門牌）'
    kind = m.group(1)
    if len(head) < 2 or (len(head) == len(kind)):
        return None, '路名過短'
    road = head + ('%d段' % section if section else '')
    num = None
    if main is not None:
        num = str(main) + ('之%d' % sub if sub else '')
    return dict(city=city, district=district, road=road, road_base=head, section=section, road_kind=kind,
                lane=(str(lane) + ('之%d' % lane_sub if lane_sub else '')) if lane is not None else None,
                alley=(str(alley) + ('之%d' % alley_sub if alley_sub else '')) if alley is not None else None,
                number=num), ''


def osm_text(t):
    st = (t.get('addr:street') or '').strip()
    hn = (t.get('addr:housenumber') or '').strip()
    if not st:
        return None
    hn = re.split(r'[;、,，]', hn)[0].strip() if hn else ''
    hn = re.split(r'[~～]', hn)[0].strip()
    if hn and not hn.endswith('號') and re.match(r'^[0-9一二三四五六七八九十〇零之\-－A-Za-z]+$', hn):
        hn += '號'
    return st + hn


def name_text(name):
    m = NAME_PAREN.search(name or '')
    if not m:
        return None
    inner = m.group(1)
    return inner if re.search(r'(路|街|大道|道|線)', inner) else None


def key(s):
    return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', s or '').replace('臺', '台').lower())


def load_osm_index(raw_path):
    raw = pickle.load(open(raw_path, 'rb'))
    idx = collections.defaultdict(list)
    for t, lat, lon, _ in raw['pois']:
        if t.get('addr:street'):
            idx[(round(lat, 6), round(lon, 6))].append(t)
    return idx


def osm_candidate(idx, lat, lon, name):
    c = idx.get((round(lat, 6), round(lon, 6)))
    if not c:
        return None
    nk = key(name)
    for t in c:
        k = key(t.get('name', ''))
        if k and (k == nk or k in nk or nk in k):
            return osm_text(t)
    return osm_text(c[0]) if len(c) == 1 else None


def load_flat_match(rows, flat_path):
    import numpy as np, pandas as pd
    from scipy.spatial import cKDTree
    flat = pd.read_parquet(flat_path, columns=['lat', 'lon', 'primary_name', 'c_zhhant', 'c_zhtw', 'c_zh', 'addr', 'locality'])
    flat = flat[flat['addr'].notna() & (flat['addr'].astype(str).str.strip() != '')].reset_index(drop=True)
    kx = 111000.0 * np.cos(np.radians(23.7))
    ft = cKDTree(np.c_[flat.lat.values * 111000.0, flat.lon.values * kx])
    xy = np.c_[[r[4] * 111000.0 for r in rows], [r[5] * kx for r in rows]]
    near = ft.query_ball_point(xy, 3.0)
    near10 = ft.query_ball_point(xy, 10.0)
    P, A = flat.primary_name.values, flat.addr.values
    Z = [flat.c_zhhant.values, flat.c_zhtw.values, flat.c_zh.values]
    out, stat = {}, collections.Counter()
    for i, r in enumerate(rows):
        cand = near[i]
        nk = key(r[1])
        if not cand:
            # 3 m 內沒有原始資料：放寬到 10 m，但必須名稱相符（相等，或互相包含且較短者 >= 3 字），且相符者地址唯一
            hits = set()
            for j in near10[i]:
                for n in [P[j]] + [z[j] for z in Z]:
                    kn = key(n) if n else ''
                    if kn and (kn == nk or (min(len(kn), len(nk)) >= 3 and (kn in nk or nk in kn))):
                        hits.add(j); break
            addrs = {str(A[j]) for j in hits}
            if len(addrs) == 1:
                j = min(hits); out[r[0]] = (str(A[j]), flat.locality.values[j], True); stat['10m名稱配對'] += 1
            else:
                stat['無座標鄰近' if not hits else '10m名稱配對地址不一'] += 1
            continue
        pick = None
        for j in cand:
            names = [P[j]] + [z[j] for z in Z]
            if any(n and (key(n) == nk or (len(key(n)) >= 2 and (key(n) in nk or nk in key(n)))) for n in names):
                pick = j; break
        if pick is None and len(cand) == 1:
            pick = cand[0]; stat['單一鄰近免名稱'] += 1
        if pick is None:
            stat['多筆鄰近無法定名'] += 1; continue
        out[r[0]] = (str(A[pick]), flat.locality.values[pick], False)
    return out, stat


def build(kind, src, outp, raw=None, flat=None, report_dir=None, osm_roads=None):
    t0 = time.time()
    if os.path.abspath(src) != os.path.abspath(outp):
        shutil.copyfile(src, outp)
    con = sqlite3.connect(outp)
    cols = [r[1] for r in con.execute('PRAGMA table_info(place)')]
    has_note = 'note' in cols
    rows = con.execute('SELECT id,name,kind,sub,lat,lon' + (',note' if has_note else ',NULL') + ' FROM place').fetchall()
    dists = set()
    for r in rows:
        _, d = split_sub(r[3])
        if d: dists.add(d)
    gov = {}
    if kind == 'gov':
        gov = dict(con.execute('SELECT place_id,address FROM detail').fetchall())
    osm_idx = load_osm_index(raw) if kind == 'main' else None
    flat_map, flat_stat = ({}, {})
    if kind == 'overture':
        flat_map, flat_stat = load_flat_match([r for r in rows if r[2] not in SKIP_KINDS], flat)
    en = None
    if osm_roads:
        en_mod = _load('en_road', 'en_road.py'); en = en_mod.EnRoad(osm_roads, normalize)
    en_stat = collections.Counter()
    print('prep %.0fs 行政區 %d' % (time.time() - t0, len(dists)), flush=True)

    out_rows, avail, gaps, srcstat = [], {}, [], collections.Counter()
    for (pid, name, k, sub, lat, lon, note) in rows:
        if k in SKIP_KINDS:
            continue
        cands = []
        if kind == 'main':
            tx = osm_candidate(osm_idx, lat, lon, name)
            if tx: cands.append(('osm', tx, None))
        elif kind == 'overture':
            f = flat_map.get(pid)
            if f: cands.append(('overture_10m' if f[2] else 'overture', f[0], f[1]))
        else:
            if gov.get(pid): cands.append(('gov', gov[pid], None))
        if note: cands.append(('note', note, None))
        nt = name_text(name)
        if nt: cands.append(('name', nt, None))
        parsed, reasons = [], []
        for src_name, tx, loc in cands:
            d, why = parse_loose(tx, dists)
            if d:
                if not d['district'] and loc and DIST_END.search(loc) and len(loc) <= 4: d['district'] = normalize(loc)
                parsed.append((src_name, d))
            else:
                reasons.append((src_name, why))
        if not parsed and en:
            for src_name, tx, loc in cands:
                if not src_name.startswith('overture') or not re.search(r'[A-Za-z]', tx):
                    continue
                d, why = en.convert(tx, lat, lon, normalize)
                en_stat[why or '採用'] += 1
                if d:
                    if loc and DIST_END.search(loc) and len(loc) <= 4: d['district'] = normalize(loc)
                    parsed.append(('overture_en', d))
                else:
                    reasons.insert(0, ('overture_en', why))
                break
        sc, sd = split_sub(sub)
        avail[pid] = {s for s, _ in parsed}
        best = next((p for p in parsed if p[1]['number']), None) or (parsed[0] if parsed else None)
        if best:
            s, d = best
            out_rows.append((pid, d['city'] or sc, d['district'] or sd, d['road'], d['road_base'], d['section'], d['road_kind'],
                             d['lane'], d['alley'], d['number'], s))
            srcstat[s] += 1
        else:
            why = reasons[0][1] if reasons else '沒有任何地址來源'
            gaps.append((pid, name, k, sub, why, cands[0][1] if cands else ''))
    print('parse %.0fs rows=%d gaps=%d' % (time.time() - t0, len(out_rows), len(gaps)), flush=True)

    con.executescript("""
DROP TABLE IF EXISTS place_addr; DROP TABLE IF EXISTS place_addr_meta;
CREATE TABLE place_addr(place_id INTEGER PRIMARY KEY, city TEXT, district TEXT, road TEXT NOT NULL, road_base TEXT NOT NULL, section INTEGER,
  road_kind TEXT NOT NULL, lane TEXT, alley TEXT, number TEXT, source TEXT NOT NULL);
CREATE TABLE place_addr_meta(key TEXT PRIMARY KEY, value TEXT);
""")
    con.executemany('INSERT INTO place_addr VALUES(?,?,?,?,?,?,?,?,?,?,?)', out_rows)
    con.executemany('INSERT INTO place_addr_meta VALUES(?,?)', [
        ('version', '1'), ('norm', 'NAV-ADDR-1 normalize（臺→台、中文數字、之）；road 含段（忠孝東路4段）'),
        ('road_kinds', '路,街,大道,道,線,巷,弄（巷弄僅限有名稱者）'), ('sources', 'osm,overture,overture_10m,overture_en,gov,note,name（overture_10m＝座標10m內名稱相符配對；overture_en＝英文地址經OSM道路300m內唯一對應轉中文路名）'), ('rows', str(len(out_rows))),
        ('built_at', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))])
    con.commit()
    con.execute('VACUUM'); con.close()

    byid = {r[0]: r for r in out_rows}
    rep = {'kind': kind, 'rows': len(out_rows), 'sources': dict(srcstat), 'flat_match': dict(flat_stat), 'en_convert': dict(en_stat)}
    by_kind = collections.defaultdict(collections.Counter)
    by_city = collections.defaultdict(collections.Counter)
    for (pid, name, k, sub, lat, lon, note) in rows:
        if k in SKIP_KINDS:
            by_kind['road(不適用)']['total'] += 1; continue
        r = byid.get(pid)
        c = (r[1] if r and r[1] else split_sub(sub)[0]) or '(縣市不明)'
        for tgt in (by_kind[k], by_city[c]):
            tgt['total'] += 1
            if r:
                tgt['road'] += 1
                if r[9]: tgt['number'] += 1
            for s in avail[pid]:
                tgt['av_' + s] += 1
    rep['by_kind'] = {k: dict(v) for k, v in by_kind.items()}
    rep['by_city'] = {k: dict(v) for k, v in by_city.items()}
    rep['gap_reasons'] = dict(collections.Counter(g[4] for g in gaps))
    if report_dir:
        os.makedirs(report_dir, exist_ok=True)
        json.dump(rep, open(os.path.join(report_dir, 'place_addr_%s.json' % kind), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        with open(os.path.join(report_dir, 'place_addr_%s_gaps.csv' % kind), 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f); w.writerow(['place_id', 'name', 'kind', 'sub', '原因', '手邊地址文字']); w.writerows(gaps)
    return rep


def verify(kind, src, outp):
    """全量一致性檢查；回傳 (ok, 訊息 list)。"""
    msgs, ok = [], True

    def bad(m):
        nonlocal ok; ok = False; msgs.append('失敗：' + m)
    a = sqlite3.connect('file:%s?mode=ro' % src, uri=True)
    b = sqlite3.connect('file:%s?mode=ro' % outp, uri=True)
    for t in [r[0] for r in a.execute("SELECT name FROM sqlite_master WHERE type='table'")]:
        ha, hb = hashlib.sha256(), hashlib.sha256()
        for r in a.execute('SELECT * FROM "%s" ORDER BY 1' % t): ha.update(repr(r).encode())
        try:
            for r in b.execute('SELECT * FROM "%s" ORDER BY 1' % t): hb.update(repr(r).encode())
        except Exception as e:
            bad('既有表 %s 無法讀：%s' % (t, e)); continue
        if ha.digest() != hb.digest(): bad('既有表 %s 內容與原庫不同' % t)
    q = "SELECT name,sql FROM sqlite_master WHERE type IN ('table','index','view')"
    sa = {r[0]: r[1] for r in a.execute(q)}
    sb = {r[0]: r[1] for r in b.execute(q) if 'place_addr' not in r[0]}
    if sa != sb: bad('既有 schema 被改動')
    msgs.append('既有表逐表（含 FTS 內部表）全量 hash 與原庫一致、schema 未動（%d 個物件）' % len(sa))
    ids = {r[0]: r[1] for r in b.execute('SELECT id,kind FROM place')}
    rows = b.execute('SELECT place_id,city,district,road,road_base,section,road_kind,lane,alley,number,source FROM place_addr').fetchall()
    n = len(rows)
    if len({r[0] for r in rows}) != n: bad('place_id 重複')
    orphan = [r[0] for r in rows if r[0] not in ids]
    if orphan: bad('孤兒 place_id %d 筆（例 %s）' % (len(orphan), orphan[:5]))
    onroad = [r[0] for r in rows if ids.get(r[0]) == 'road']
    if onroad: bad('道路段不應有地址 %d' % len(onroad))
    if [r for r in rows if not r[3] or not r[4] or not r[6]]: bad('空路名')
    if set(r[6] for r in rows) - {'路', '街', '大道', '道', '線', '巷', '弄'}: bad('未知 road_kind')
    badr = [r for r in rows if r[3] != r[4] + ('%d段' % r[5] if r[5] else '') or not r[4].endswith(r[6])]
    if badr: bad('road 與 road_base／section／road_kind 不一致 %d（例 %s）' % (len(badr), badr[:2]))
    if [r for r in rows if r[1] and r[1] not in CITIES]: bad('city 不在 22 縣市')
    pat = re.compile(r'^[0-9]+(之[0-9]+)?$')
    badn = [r for r in rows if (r[9] and not pat.match(r[9])) or (r[7] and not pat.match(r[7])) or (r[8] and not pat.match(r[8]))]
    if badn: bad('lane/alley/number 格式不符 %d（例 %s）' % (len(badn), badn[:2]))
    if [r for r in rows if r[10] not in ('osm', 'overture', 'overture_10m', 'overture_en', 'gov', 'note', 'name')]: bad('source 未知')
    if [r for r in rows if '臺' in r[3] or re.search(r'[一二三四五六七八九十]段', r[3])]: bad('路名未正規化（臺或中文段）')
    dists = {r[2] for r in rows if r[2]}
    mism = []
    for r in rows:
        if not r[9] and not r[7]:
            continue
        txt = (r[1] or '') + (r[2] or '') + r[4] + ('%d段' % r[5] if r[5] else '') + ('%s巷' % r[7] if r[7] else '') + ('%s弄' % r[8] if r[8] else '') + ('%s號' % r[9] if r[9] else '')
        p = ba.parse(txt, dists or None)
        gm = (str(p['main']) + ('之%d' % p['sub'] if p['sub'] else '')) if p and p['main'] is not None else None
        if not p or p['road'] != r[4] or (p['section'] or None) != r[5] or gm != r[9]:
            mism.append((r, txt, p and (p['road'], p['section'], gm)))
    if mism: bad('重組後再解析不一致 %d 筆（例 %s）' % (len(mism), mism[:2]))
    ic = b.execute('PRAGMA integrity_check').fetchone()[0]
    if ic != 'ok': bad('integrity_check ' + ic)
    msgs.append('place_addr %d 筆：孤兒／重複／空路名／格式／重組再解析 全量檢查完成' % n)
    return ok, msgs


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--kind', required=True, choices=['main', 'overture', 'gov'])
    ap.add_argument('--src', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--raw'); ap.add_argument('--flat'); ap.add_argument('--report-dir')
    ap.add_argument('--osm-roads', help='extract_osm_road_names.py 產物；給了才做英文地址轉中文路名（overture）')
    ap.add_argument('--verify', action='store_true')
    a = ap.parse_args()
    if not a.verify:
        rep = build(a.kind, a.src, a.out, a.raw, a.flat, a.report_dir, a.osm_roads)
        print(json.dumps({k: rep[k] for k in ('rows', 'sources', 'flat_match', 'en_convert', 'gap_reasons')}, ensure_ascii=False))
    ok, msgs = verify(a.kind, a.src, a.out)
    print('\n'.join(msgs)); print('VERIFY', 'OK' if ok else 'FAIL')
    sys.exit(0 if ok else 1)
