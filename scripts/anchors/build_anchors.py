# -*- coding: utf-8 -*-
"""路口播報錨點庫 touror_junction_anchors.sqlite（D1，2026-11-26）。

規格：重機導航\\docs\\地標式播報研究.md §2（A 數燈＋路名、B 紅綠燈＋轉角地標、C 路名路口、D 地標、E 橋／隧道／路底）、§4（地標評分）。
獨立檔，不改 touror_junctions.sqlite（App 讀路口庫的程式不受影響）；以 junction_id 對應路口庫的 junction.id，
並存座標與路口庫 data_version，App 讀取時兩者不符就不用（避免路口庫重建後 id 錯位）。

輸入（重機導航\\資料\\搜尋）：touror_junctions.sqlite、touror_places.sqlite（OSM）、touror_places_overture.sqlite、
touror_places_gov.sqlite；倉庫 data/signals/taiwan-signals.bin＋taiwan-signals-quality.bin（TSGQ v1）。
輸出：<out>/touror_junction_anchors.sqlite、<out>/anchors-stats.json。

用法（低優先權，約 1-3 分鐘）：
  python -I scripts/anchors/build_anchors.py [--out 資料夾]   預設 重機導航\\資料\\搜尋

Schema（見 SCHEMA 常數）：
  anchor(junction_id PK, lat_e7, lon_e7, name_a, name_b, spoken_a, spoken_b, signal_conf, geom, best_lm)
    name_a/name_b：交會兩條路全名（路口庫 name 以「／」拆）；spoken_*：去掉「X段」後的念法；同一條路不同段 → spoken_b 空。
    signal_conf：0 無號誌、1 低（單一來源／只有路口庫標記）、2 中、3 高（taiwan-signals-quality 等級，40 m 內取最高）。
    geom 位元：1 T字（可能「路底」）、2 橋進出口、4 隧道進出口、8 圓環、16 附近有疑似漏燈、32 交會路含巷／弄、64 兩條可念路名。
    best_lm：landmark.id（可用：≤30 m、score≥50、400 m 內無同品牌）；沒有則 NULL。
    同一實際路口（路口庫 25 m 內的點連成「路口群」，雙向分隔道路常有 2-4 點）共用路名、號誌、地標；landmark 掛在群的第一個點。
    geom 64＝兩條不同路名（含巷名牌）；geom 32＝其中一條是巷／弄。
  landmark(id PK, junction_id, name_spoken, brand, cat, bearing, dist_dm, score, same_brand_400, src)
    cat：1 加油站 2 便利商店 3 速食／咖啡 4 大型賣場 5 銀行 6 郵局 7 學校 8 醫院 9 廟宇 10 捷運／車站 11 派出所／消防
    bearing：路口→地標方位角（度，0＝北）。轉彎前／後、左／右角依實際進入方向在 App 端算（與路線有關，不能預存）。
    dist_dm：距路口中心（0.1 m）；收 ≤60 m（≤30 m 供「紅綠燈＋轉角地標」，30-60 m 只供「過了 X」句）。
    score：0-100＝S_sem × S_str（≤15 m 1.0、≤30 m 0.7、≤60 m 0.5）× S_fresh（政府或 OSM＋Overture 互證 1.0、單一來源 0.7）。
    same_brand_400：路口 400 m 內另有幾家同品牌（不含本身）；>0 視為會混淆。
    src 位元：1 OSM、2 Overture、4 政府。
"""
import argparse, ctypes, datetime, json, math, os, re, sqlite3, sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from common import formats, paths  # noqa: E402

PROJ = os.path.dirname(paths.ROOT)
SEARCH = os.path.join(PROJ, '資料', '搜尋')
SIG_BIN = os.path.join(paths.DATA, 'signals', 'taiwan-signals.bin')
SIGQ_BIN = os.path.join(paths.DATA, 'signals', 'taiwan-signals-quality.bin')
SCHEMA_VERSION = 1
SKIP_KINDS = {1, 3, 4, 11}  # 交流道、匝道分流／合流／未知：高速公路另用交流道錨

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE anchor(junction_id INTEGER PRIMARY KEY, lat_e7 INTEGER NOT NULL, lon_e7 INTEGER NOT NULL,
  name_a TEXT NOT NULL, name_b TEXT NOT NULL, spoken_a TEXT NOT NULL, spoken_b TEXT NOT NULL,
  signal_conf INTEGER NOT NULL, geom INTEGER NOT NULL, best_lm INTEGER);
CREATE TABLE landmark(id INTEGER PRIMARY KEY, junction_id INTEGER NOT NULL, name_spoken TEXT NOT NULL,
  brand TEXT NOT NULL, cat INTEGER NOT NULL, bearing INTEGER NOT NULL, dist_dm INTEGER NOT NULL,
  score INTEGER NOT NULL, same_brand_400 INTEGER NOT NULL, src INTEGER NOT NULL);
"""

EXCLUDE = re.compile(r'ATM|提款|停車|充電|洗車|出口|入口|宿舍|辦事處|服務處|服務中心|總公司|總部|倉庫|物流|加盟|籌備|預定地|舊址|附設|幼兒園|幼稚園|補習|安親|托嬰')
CHAINS = [  # (regex, cat, brand, spoken, S_sem, 需要 fuel 類)
    (r'中油|CPC', 1, '中油', '中油加油站', 1.0, True),
    (r'台塑', 1, '台塑', '台塑加油站', 1.0, True),
    (r'全國加油', 1, '全國', '全國加油站', 1.0, False),
    (r'7-?ELEVEN|7-11|統一超商', 2, '7-11', '7-11', 0.8, False),
    (r'全家便利|^全家|FamilyMart|Family Mart', 2, '全家', '全家', 0.8, False),
    (r'萊爾富|Hi-?Life', 2, '萊爾富', '萊爾富', 0.8, False),
    (r'OK超商|OK・|OKmart|OK mart|OK便利', 2, 'OK', 'OK超商', 0.8, False),
    (r'麥當勞|McDonald', 3, '麥當勞', '麥當勞', 0.9, False),
    (r'肯德基|KFC', 3, '肯德基', '肯德基', 0.9, False),
    (r'摩斯漢堡|MOS ?BURGER', 3, '摩斯', '摩斯漢堡', 0.9, False),
    (r'漢堡王|Burger King', 3, '漢堡王', '漢堡王', 0.9, False),
    (r'星巴克|Starbucks', 3, '星巴克', '星巴克', 0.9, False),
    (r'全聯', 4, '全聯', '全聯', 0.9, False),
    (r'家樂福|Carrefour', 4, '家樂福', '家樂福', 0.9, False),
    (r'好市多|Costco', 4, '好市多', '好市多', 0.9, False),
    (r'大潤發', 4, '大潤發', '大潤發', 0.9, False),
    (r'愛買', 4, '愛買', '愛買', 0.9, False),
    (r'IKEA|宜家家居', 4, 'IKEA', 'IKEA', 0.9, False),
]
CHAINS = [(re.compile(c[0], re.I),) + tuple(c[1:]) for c in CHAINS]
BANK = re.compile(r'^([一-鿿]{2,8}?銀行)')
POST = re.compile(r'郵局$')
MRT = re.compile(r'^(?:[一-鿿]{0,4}捷運)?([一-鿿]{1,6})站$')
STATION = re.compile(r'([一-鿿]{1,6})(火車站|車站)$')
SCHOOL = re.compile(r'(國民小學|國小|國民中學|國中|高級中學|高級中等學校|高中|高職|大學|實驗小學)$')
HOSPITAL = re.compile(r'醫院$')
TEMPLE = re.compile(r'(宮|廟|寺|殿|媽祖廟|土地公廟)$')
POLICE = re.compile(r'派出所$|分駐所$|消防(隊|分隊)$')
PAREN = re.compile(r'[（(].*?[)）]')


def shorten(name):
    n = PAREN.sub('', name).strip()
    n = re.sub(r'^(?:[一-鿿]{1,3}[市縣])(?:立|政府)?', '', n) if re.match(r'^[一-鿿]{1,3}[市縣]立', n) else n
    n = n.replace('國民小學', '國小').replace('國民中學', '國中').replace('高級中等學校', '高中').replace('高級中學', '高中')
    return n


def classify(name, kind):
    """回傳 (cat, brand, spoken, S_sem) 或 None。"""
    if not name or EXCLUDE.search(name):
        return None
    for rx, cat, brand, spoken, sem, need_fuel in CHAINS:
        if rx.search(name):
            if need_fuel and kind != 'fuel' and '加油' not in name:
                return None
            return cat, brand, spoken, sem
    if kind == 'fuel':
        return 1, '加油站', '加油站', 0.8
    n = shorten(name)
    if len(n) > 10:
        return None
    m = BANK.match(n)
    if m and kind in ('service', 'poi', 'shop'):
        return 5, m.group(1), m.group(1), 0.6
    if POST.search(n):
        return 6, '郵局', '郵局', 0.7
    if kind == 'transport':
        if '捷運' in n:
            m = MRT.match(n)
            if m:
                return 10, '捷運' + m.group(1) + '站', '捷運' + m.group(1) + '站', 1.0
        m = STATION.search(n)
        if m and '公車' not in n and '客運' not in n:
            return 10, n, n, 0.9
        return None
    if SCHOOL.search(n):
        return 7, n, n, 0.7
    if HOSPITAL.search(n):
        return 8, n, n, 0.8
    if TEMPLE.search(n) and (kind in ('worship', 'poi', 'scenic')):
        return 9, n, n, 0.7
    if POLICE.search(n):
        return 11, n, n, 0.6
    return None


def dist_m(a1, o1, a2, o2):
    dy = (a2 - a1) * 111320.0
    dx = (o2 - o1) * 111320.0 * math.cos(math.radians((a1 + a2) / 2))
    return math.hypot(dx, dy)


def bearing(a1, o1, a2, o2):
    dy = a2 - a1
    dx = (o2 - o1) * math.cos(math.radians(a1))
    return int(round(math.degrees(math.atan2(dx, dy)))) % 360


CELL = 0.002


def key(lat, lon):
    return int(lat / CELL), int(lon / CELL)


def near(grid, lat, lon, r):
    ki, kj = key(lat, lon)
    s = int(math.ceil(r / 200.0))
    for di in range(-s, s + 1):
        for dj in range(-s, s + 1):
            for p in grid.get((ki + di, kj + dj), ()):
                d = dist_m(lat, lon, p[0], p[1])
                if d <= r:
                    yield d, p


def low_priority():
    try:
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL
    except Exception:
        try:
            os.nice(10)
        except Exception:
            pass


def load_landmarks():
    """三個地點庫 → 去重後的候選 [lat, lon, cat, brand, spoken, sem, src]；同品牌 50 m 內視為同一家。"""
    grid, out = {}, []
    for fn, bit in (('touror_places_gov.sqlite', 4), ('touror_places.sqlite', 1), ('touror_places_overture.sqlite', 2)):
        db = sqlite3.connect(os.path.join(SEARCH, fn))
        for name, kind, lat, lon in db.execute('SELECT name, kind, lat, lon FROM place'):
            if not (21.5 < lat < 26.5 and 118 < lon < 122.5):
                continue
            c = classify(name, kind)
            if not c:
                continue
            cat, brand, spoken, sem = c
            hit = None
            for d, p in near(grid, lat, lon, 50):
                if p[3] == brand:
                    hit = p
                    break
            if hit:
                hit[6] |= bit
                continue
            p = [lat, lon, cat, brand, spoken, sem, bit]
            grid.setdefault(key(lat, lon), []).append(p)
            out.append(p)
        db.close()
    return grid, out


def load_signals():
    data = open(SIG_BIN, 'rb').read()
    _, rows = formats.read_signals_bin(data)
    info, q, sus = formats.read_signal_quality_bin(open(SIGQ_BIN, 'rb').read())
    if info['signals_crc'] != formats.signals_header_crc(data) or len(q) != len(rows):
        raise SystemExit('號誌信心檔與 taiwan-signals.bin 不同版，先重建 quality')
    grid, sgrid = {}, {}
    for (a, o, f), lv in zip(rows, q):
        if f & 1 and not f & (16 | 32):  # 路口燈；排除高架、路外
            lat, lon = a / 1e6, o / 1e6
            grid.setdefault(key(lat, lon), []).append((lat, lon, lv & 3))
    for a, o, _ in sus:
        lat, lon = a / 1e6, o / 1e6
        sgrid.setdefault(key(lat, lon), []).append((lat, lon))
    return grid, sgrid, dict(signals=len(rows), quality_version=info['version'], suspects=len(sus))


def split_names(name):
    parts = [p.strip() for p in name.split('／') if p.strip()] if name else []
    a = parts[0] if parts else ''
    b = parts[1] if len(parts) > 1 else ''
    sa = re.sub(r'[一二三四五六七八九十]+段$', '', a)
    sb = re.sub(r'[一二三四五六七八九十]+段$', '', b)
    if sb == sa:
        sb = ''
    return a, b, sa, sb


def county_grid():
    db = sqlite3.connect(os.path.join(SEARCH, 'touror_places.sqlite'))
    g = {}
    for sub, lat, lon in db.execute("SELECT sub, lat, lon FROM place WHERE kind='road' AND sub IS NOT NULL"):
        g.setdefault(key(lat, lon), []).append((lat, lon, sub[:3]))
    db.close()
    return g


def county_of(g, lat, lon):
    best = None
    for r in (400, 2000, 8000):
        for d, p in near(g, lat, lon, r):
            if best is None or d < best[0]:
                best = (d, p[2])
        if best:
            return best[1]
    return '未知'


NOSIGN = re.compile(r'高速公路|快速道路|快速公路|交流道|高架|匝道|系統')  # 號誌桿路名牌不會掛這些


def cluster_names(names):
    """路口群內所有路名 → (a, b, sa, sb)：先挑非巷弄的兩條不同路，不足再用巷弄。"""
    full = []
    for n in names:
        for p in (n.split('／') if n else []):
            p = p.strip()
            if p and p not in full and not NOSIGN.search(p):
                full.append(p)
    base = lambda x: re.sub(r'[一二三四五六七八九十]+段$', '', x)
    main_ = [x for x in full if not re.search(r'[巷弄]', x)]
    alley = [x for x in full if re.search(r'[巷弄]', x)]
    picked = []
    for x in main_ + alley:
        if all(base(x) != base(y) for y in picked):
            picked.append(x)
        if len(picked) == 2:
            break
    a = picked[0] if picked else (full[0] if full else '')
    b = picked[1] if len(picked) > 1 else ''
    return a, b, base(a), (base(b) if b else '')


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--out', default=SEARCH)
    args = ap.parse_args(argv)
    low_priority()
    lm_grid, lms = load_landmarks()
    brand_grid = defaultdict(dict)
    for p in lms:
        brand_grid[p[3]].setdefault(key(p[0], p[1]), []).append(p)
    sig_grid, gap_grid, sig_info = load_signals()
    cg = county_grid()

    jdb = sqlite3.connect(os.path.join(SEARCH, 'touror_junctions.sqlite'))
    jmeta = dict(jdb.execute('SELECT key, value FROM meta'))
    js = []
    for jid, la, lo, kind, arms, sig, name, flags in jdb.execute('SELECT id, lat, lon, kind, arms, signal, name, flags FROM junction'):
        if kind in SKIP_KINDS:
            continue
        js.append([jid, la, lo, la / 1e7, lo / 1e7, flags | (1 << (kind - 1)), bool(sig), name])
    # 路口群：雙向分隔道路一個實際路口在路口庫會有 2-4 個點；25 m 內連成一群，共用路名、號誌、地標
    parent = list(range(len(js)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    jg = {}
    for i, j in enumerate(js):
        jg.setdefault(key(j[3], j[4]), []).append((j[3], j[4], i))
    for i, j in enumerate(js):
        for d, p in near(jg, j[3], j[4], 25):
            if p[2] > i:
                ra, rb = find(i), find(p[2])
                if ra != rb:
                    parent[rb] = ra
    clusters = defaultdict(list)
    for i in range(len(js)):
        clusters[find(i)].append(i)

    out_path = os.path.join(args.out, 'touror_junction_anchors.sqlite')
    tmp = out_path + '.tmp'
    if os.path.exists(tmp):
        os.remove(tmp)
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    anchors, lmrows = [], []
    st = defaultdict(lambda: defaultdict(int))
    lm_id = 0
    for members in clusters.values():
        mj = [js[i] for i in members]
        lat = sum(j[3] for j in mj) / len(mj)
        lon = sum(j[4] for j in mj) / len(mj)
        bits = 0
        for j in mj:
            bits |= j[5]
        flagged = any(j[6] for j in mj) or bool(bits & 2048)
        a, b, sa, sb = cluster_names([j[7] for j in mj])
        # 路口庫有號誌標記：40 m 內取最高信心；沒有標記：只認 15 m 內的燈（避免把隔壁路口的燈算進來）
        conf = max((p[2] for _, p in near(sig_grid, lat, lon, 40 if flagged else 15)), default=0)
        if conf == 0 and flagged:
            conf = 1
        geom = 0
        if bits & 512: geom |= 1
        if bits & 32: geom |= 2
        if bits & 16: geom |= 4
        if bits & 2: geom |= 8
        if any(True for _ in near(gap_grid, lat, lon, 40)): geom |= 16
        alley = bool(re.search(r'[巷弄]', a + b))
        if alley: geom |= 32
        named2 = bool(sa and sb)
        if named2: geom |= 64
        cands = []
        for d, p in near(lm_grid, lat, lon, 60):
            s_str = 1.0 if d <= 15 else 0.7 if d <= 30 else 0.5
            fresh = 1.0 if (p[6] & 4 or (p[6] & 3) == 3) else 0.7
            score = int(round(100 * p[5] * s_str * fresh))
            if score < 30:
                continue
            same = sum(1 for dd, q in near(brand_grid[p[3]], lat, lon, 400) if q is not p)
            cands.append((score, d, p, same))
        cands.sort(key=lambda t: (-t[0], t[1]))
        best, best_s, rows = None, -1, []
        for score, d, p, same in cands[:4]:
            lm_id += 1
            rows.append((lm_id, p, int(round(d * 10)), score, same))
            if d <= 30 and score >= 50 and same == 0 and score > best_s:
                best, best_s = lm_id, score
        # 每個成員點都寫一列（App 以路口庫 junction.id 查）；地標掛在群的第一個點，其他點的 best_lm 指同一筆
        first = mj[0][0]
        for lid, p, ddm, score, same in rows:
            lmrows.append((lid, first, p[4], p[3], p[2], bearing(lat, lon, p[0], p[1]), ddm, score, same, p[6]))
        for j in mj:
            anchors.append((j[0], j[1], j[2], a, b, sa, sb, conf, geom, best))
        has30 = any(d <= 30 and score >= 50 for score, d, p, same in cands)
        raw30 = any(True for _ in near(lm_grid, lat, lon, 30))
        cty = county_of(cg, lat, lon) if conf >= 1 else None
        for g in ['全部地面路口群'] + (['號誌路口群', '號誌路口群:' + cty] if conf >= 1 else []):
            st[g]['n'] += 1
            st[g]['named2'] += named2
            st[g]['named2_main'] += named2 and not alley
            st[g]['any_name'] += bool(a)
            st[g]['lm30_raw'] += raw30
            st[g]['lm30'] += has30
            st[g]['lm30_usable'] += best is not None
            st[g]['conf_high'] += conf == 3
            st[g]['anchor_any'] += bool(named2 or best is not None)
    db.executemany('INSERT INTO anchor VALUES (?,?,?,?,?,?,?,?,?,?)', anchors)
    db.executemany('INSERT INTO landmark VALUES (?,?,?,?,?,?,?,?,?,?)', lmrows)
    db.execute('CREATE INDEX landmark_junction ON landmark(junction_id)')
    built = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    meta = dict(schema_version=str(SCHEMA_VERSION), built_at=built, junctions_data_version=jmeta.get('data_version', ''),
                junctions_built_at=jmeta.get('built_at', ''), signals_count=str(sig_info['signals']),
                signal_suspects=str(sig_info['suspects']), landmark_candidates=str(len(lms)), clusters=str(len(clusters)),
                attribution='© OpenStreetMap contributors (ODbL 1.0)；Overture Maps Foundation；政府資料開放授權條款',
                spec='重機導航/docs/地標式播報研究.md §2 §4')
    db.executemany('INSERT INTO meta VALUES (?,?)', meta.items())
    db.commit()
    db.execute('VACUUM')
    db.close()
    os.replace(tmp, out_path)

    def pct(g, k):
        return round(100.0 * st[g][k] / st[g]['n'], 1) if st[g]['n'] else 0.0
    keys = ('named2', 'named2_main', 'any_name', 'lm30_raw', 'lm30', 'lm30_usable', 'anchor_any', 'conf_high')
    stats = dict(built_at=built, anchors=len(anchors), clusters=len(clusters), landmarks=len(lmrows),
                 landmark_candidates=len(lms), bytes=os.path.getsize(out_path),
                 groups={g: dict(n=st[g]['n'], **{k + '_pct': pct(g, k) for k in keys}) for g in sorted(st)})
    with open(os.path.join(args.out, 'anchors-stats.json'), 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print(json.dumps({g: stats['groups'][g] for g in ('全部地面路口群', '號誌路口群')}, ensure_ascii=False))
    print('anchors', len(anchors), 'clusters', len(clusters), 'landmarks', len(lmrows), 'cands', len(lms), 'bytes', stats['bytes'])


if __name__ == '__main__':
    main()
