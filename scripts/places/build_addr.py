# -*- coding: utf-8 -*-
"""Touror 門牌地址離線搜尋包建置工具（NAV-ADDR-1）

用法（Python 讀外部資料一律加 -I）：
  python -I build_addr.py --pbf taiwan-latest.osm.pbf --out touror_addr.bin [--places touror_places.sqlite]
  python -I build_addr.py --out touror_addr.bin --work <暫存> --reuse-raw     # 不重掃 PBF
  python -I build_addr.py --selftest                                          # 正規化／解析測試向量（addr_vectors.tsv）
  python -I build_addr.py --verify touror_addr.bin --query 高雄市仁武區八德南路447號 ...

需要：pip install osmium（只有階段 1 需要）
流程：
  階段 1（子程序）：pyosmium 掃一次 PBF，抽出有 addr:housenumber 的節點與建物（建物取節點平均），
          寫 <work>/raw_addr.tsv（lat lon city district street housenumber place）。
  階段 2：正規化、解析（與 iOS AddressParser 同一套規則，見 地址正規化規格.md）、依（縣市, 區, 路名＋段）分組、
          組內依（巷, 巷之, 弄, 弄之, 號, 之號）排序去重、座標量化 1e-5 度差分、欄式 varint＋raw DEFLATE，寫 touror_addr.bin。
          另把地名資料庫（touror_places.sqlite）kind=road 的路名補成「無門牌的路段」（只有代表點），
          門牌缺的地區（南投、宜蘭、嘉義市、連江）查詢時退成「僅定位到路段」。
二進位格式見 說明.md §門牌包。
"""
import argparse, json, os, re, sqlite3, struct, subprocess, sys, time, unicodedata, zlib, datetime

FORMAT_VERSION = 1
NORM_VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- 正規化（iOS AddressParser.normalize 必須同一套）
CHAR_MAP = {'臺': '台', '巿': '市', '郷': '鄉', '鎭': '鎮', '恒': '恆', '号': '號'}
HYPHENS = set('-‐‑‒–—―−')
CN_DIGIT = {'〇': 0, '零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9}
CN_UNIT = {'十': 10, '百': 100, '千': 1000}
NUM_FOLLOW = set('段巷弄號樓之')

def is_ascii_digit(c):
    return '0' <= c <= '9'

def cn_value(run):
    if any(c in CN_UNIT for c in run):
        total, cur = 0, 0
        for c in run:
            if c in CN_DIGIT:
                cur = CN_DIGIT[c]
            else:
                total += (cur if cur else 1) * CN_UNIT[c]
                cur = 0
        return total + cur
    return int(''.join(str(CN_DIGIT[c]) for c in run))

def normalize(s):
    """1 NFKC 2 異體字對照 3 去所有空白與格式字元（Cf，如零寬空白） 4 數字間的連字號→之 5 中文數字（後接 段巷弄號樓之 或前一字是 之）→阿拉伯數字"""
    s = unicodedata.normalize('NFKC', s or '')
    s = ''.join(CHAR_MAP.get(c, c) for c in s if not c.isspace() and unicodedata.category(c) != 'Cf')
    chars = list(s)
    for i, c in enumerate(chars):
        if c in HYPHENS and 0 < i < len(chars) - 1 and is_ascii_digit(chars[i - 1]) and is_ascii_digit(chars[i + 1]):
            chars[i] = '之'
    out, i, n = [], 0, len(chars)
    while i < n:
        c = chars[i]
        if c in CN_DIGIT or c in CN_UNIT:
            j = i
            while j < n and (chars[j] in CN_DIGIT or chars[j] in CN_UNIT):
                j += 1
            run = chars[i:j]
            prev = out[-1] if out else ''
            nxt = chars[j] if j < n else ''
            if nxt in NUM_FOLLOW or prev == '之':
                out.append(str(cn_value(run)))
            else:
                out.extend(run)
            i = j
        else:
            out.append(c); i += 1
    return ''.join(out)

# ---------------------------------------------------------------- 解析（iOS AddressParser.parse 必須同一套）
CITIES = ['台北市', '新北市', '桃園市', '台中市', '台南市', '高雄市', '基隆市', '新竹市', '嘉義市', '新竹縣', '苗栗縣',
          '彰化縣', '南投縣', '雲林縣', '嘉義縣', '屏東縣', '宜蘭縣', '花蓮縣', '台東縣', '澎湖縣', '金門縣', '連江縣']
RE_NUM_HAO = re.compile(r'臨?([0-9]+)((?:之[0-9]+)*)[A-Za-z]?號')
RE_NUM_END = re.compile(r'臨?([0-9]+)((?:之[0-9]+)*)[A-Za-z]?$')
RE_AFTER_HAO_SUB = re.compile(r'^之([0-9]+)')
RE_ALLEY = re.compile(r'([0-9]+)(?:之([0-9]+))?弄$')
RE_LANE = re.compile(r'([0-9]+)(?:之([0-9]+))?巷$')
RE_SECTION = re.compile(r'([0-9]+)段$')
RE_POSTCODE = re.compile(r'^[0-9]{3,6}')
RE_VILLAGE = re.compile(r'^[^0-9]{1,4}?[里村][0-9]+鄰')
RE_NEIGHBOR = re.compile(r'^[0-9]+鄰')
RE_DISTRICT_GUESS = re.compile(r'^([^0-9]{1,3}[區鄉鎮])')

def parse_tail(s):
    """normalize 過的字串 → (head, section, lane, laneSub, alley, alleySub, main, sub)；沒有的為 None"""
    main = sub = None
    m = RE_NUM_HAO.search(s)
    if m:
        main = int(m.group(1))
        subs = re.findall(r'[0-9]+', m.group(2))
        if subs:
            sub = int(subs[0])
        else:
            m2 = RE_AFTER_HAO_SUB.match(s[m.end():])
            if m2: sub = int(m2.group(1))
        s = s[:m.start()]
    else:
        m = RE_NUM_END.search(s)
        if m:
            main = int(m.group(1))
            subs = re.findall(r'[0-9]+', m.group(2))
            if subs: sub = int(subs[0])
            s = s[:m.start()]
    alley = alley_sub = lane = lane_sub = section = None
    m = RE_ALLEY.search(s)
    if m:
        alley = int(m.group(1)); alley_sub = int(m.group(2)) if m.group(2) else None; s = s[:m.start()]
    m = RE_LANE.search(s)
    if m:
        lane = int(m.group(1)); lane_sub = int(m.group(2)) if m.group(2) else None; s = s[:m.start()]
    m = RE_SECTION.search(s)
    if m:
        section = int(m.group(1)); s = s[:m.start()]
    return s, section, lane, lane_sub, alley, alley_sub, main, sub

def parse(text, districts=None):
    """使用者輸入 → dict 或 None。districts：已知鄉鎮市區名稱集合（索引提供）；None 時用「X{1,3}[區鄉鎮]」猜。
    成立條件：有路名，且有號碼或巷。"""
    s = normalize(text)
    head, section, lane, lane_sub, alley, alley_sub, main, sub = parse_tail(s)
    head = RE_POSTCODE.sub('', head, count=1)
    city = None
    for c in CITIES:
        if head.startswith(c):
            city = c; head = head[len(c):]; break
    district = None
    if districts is not None:
        best = ''
        for d in districts:
            if len(d) > len(best) and head.startswith(d) and len(head) > len(d):
                best = d
        if best:
            district = best; head = head[len(best):]
    else:
        m = RE_DISTRICT_GUESS.match(head)
        if m and len(head) > len(m.group(1)):
            district = m.group(1); head = head[m.end():]
    head = RE_VILLAGE.sub('', head, count=1)
    head = RE_NEIGHBOR.sub('', head, count=1)
    if not head or (main is None and lane is None):
        return None
    return {'city': city, 'district': district, 'road': head, 'section': section, 'lane': lane, 'lane_sub': lane_sub,
            'alley': alley, 'alley_sub': alley_sub, 'main': main, 'sub': sub}

def road_key(road, section):
    return road + ('%d段' % section if section else '')

def fmt_parse(p):
    if p is None: return ''
    f = lambda v: '' if v is None else str(v)
    return '|'.join([f(p['city']), f(p['district']), p['road'], f(p['section']), f(p['lane']), f(p['lane_sub']),
                     f(p['alley']), f(p['alley_sub']), f(p['main']), f(p['sub'])])

def display(road, section, lane, lane_sub, alley, alley_sub, main, sub):
    s = road_key(road, section)
    if lane: s += '%d%s巷' % (lane, '之%d' % lane_sub if lane_sub else '')
    if alley: s += '%d%s弄' % (alley, '之%d' % alley_sub if alley_sub else '')
    if main: s += '%d%s號' % (main, '之%d' % sub if sub else '')
    return s

def selftest(vec_path, swift_test=None):
    lines = open(vec_path, encoding='utf-8').read().split('\n')
    districts = None; fail = 0; n = 0
    for line in lines:
        if not line.strip(): continue
        cols = line.split('\t')
        if cols[0] == '#districts':
            districts = set(cols[1:]); continue
        exp = cols[2] if len(cols) > 2 else ''
        got = normalize(cols[1]) if cols[0] == 'norm' else fmt_parse(parse(cols[1], districts))
        n += 1
        if got != exp:
            fail += 1; print('FAIL %s %r → %r（預期 %r）' % (cols[0], cols[1], got, exp))
    if swift_test and os.path.exists(swift_test):
        body = open(swift_test, encoding='utf-8').read()
        m = re.search(r'// VECTORS-BEGIN\n(.*?)\n\s*// VECTORS-END', body, re.S)
        if not m:
            fail += 1; print('FAIL Swift 測試檔沒有 VECTORS-BEGIN／END 區塊')
        else:
            swift_lines = [l.strip() for l in m.group(1).split('\n') if l.strip() and not l.strip().startswith('"""') and 'let vectorsTSV' not in l]
            mine = [l for l in lines if l.strip()]
            if [l.rstrip('\t') for l in swift_lines] != [l.rstrip('\t') for l in mine]:
                fail += 1; print('FAIL Swift 測試檔的向量與 addr_vectors.tsv 不一致（%d vs %d 行）' % (len(swift_lines), len(mine)))
    print('selftest %d 項，失敗 %d' % (n, fail))
    return fail

# ---------------------------------------------------------------- 階段 1：掃 PBF
def scan(pbf, raw_path, log):
    import osmium
    work = os.path.dirname(raw_path)
    idx = os.path.join(work, 'addr_nodes.idx')
    if os.path.exists(idx): os.remove(idx)
    fp = (osmium.FileProcessor(pbf).with_locations('sparse_file_array,' + idx)
          .with_filter(osmium.filter.KeyFilter('addr:housenumber')))
    t0 = time.time(); n = nw = 0
    clean = lambda v: (v or '').replace('\t', ' ').replace('\n', ' ').replace('\r', ' ')
    with open(raw_path + '.tmp', 'w', encoding='utf-8', newline='\n') as f:
        for o in fp:
            if o.is_node():
                lat, lon = o.location.lat, o.location.lon
            elif o.is_way():
                try:
                    pts = [(x.lat, x.lon) for x in o.nodes]
                except Exception:
                    continue
                if len(pts) > 1 and pts[0] == pts[-1]: pts = pts[:-1]
                if not pts: continue
                lat = sum(p[0] for p in pts) / len(pts); lon = sum(p[1] for p in pts) / len(pts); nw += 1
            else:
                continue
            t = o.tags; n += 1
            if n % 1000000 == 0: log('  掃描 %d 筆 %.0fs' % (n, time.time() - t0))
            f.write('%.7f\t%.7f\t%s\n' % (lat, lon, '\t'.join(clean(t.get(k)) for k in
                    ('addr:city', 'addr:district', 'addr:street', 'addr:housenumber', 'addr:place'))))
    os.replace(raw_path + '.tmp', raw_path)
    try:
        hdr = osmium.io.Reader(pbf, osmium.osm.osm_entity_bits.NOTHING).header()
        ts = hdr.get('osmosis_replication_timestamp')
    except Exception:
        ts = ''
    if not ts:
        ts = datetime.datetime.utcfromtimestamp(os.path.getmtime(pbf)).strftime('%Y-%m-%dT%H:%M:%SZ') + '（檔案時間）'
    with open(raw_path + '.json', 'w', encoding='utf-8') as f:
        json.dump({'pbf': os.path.basename(pbf), 'pbf_ts': ts, 'objects': n, 'ways': nw, 'scan_sec': round(time.time() - t0)}, f, ensure_ascii=False)
    log('掃描完成 %.0fs：%d 筆（建物 %d）' % (time.time() - t0, n, nw))

# ---------------------------------------------------------------- 階段 2：建包
def varint(out, v):
    while v >= 0x80:
        out.append((v & 0x7F) | 0x80); v >>= 7
    out.append(v)

def zigzag(v):
    return (v << 1) if v >= 0 else ((-v << 1) - 1)

Q = 100000  # 座標量化：1e-5 度（緯度約 1.1 m、經度約 1.0 m）

def encode_block(recs, alat, alon):
    """recs：已排序去重的 (lane, lane_sub, alley, alley_sub, main, sub, qlat, qlon)。欄式 varint。"""
    out = bytearray()
    prev = 0
    for r in recs: varint(out, r[0] - prev); prev = r[0]
    for r in recs: varint(out, r[1])
    for r in recs: varint(out, r[2])
    for r in recs: varint(out, r[3])
    pg = None; pm = 0
    for r in recs:
        g = r[:4]
        if g == pg: varint(out, r[4] - pm)
        else: varint(out, r[4])
        pg = g; pm = r[4]
    for r in recs: varint(out, r[5])
    p = alat
    for r in recs: varint(out, zigzag(r[6] - p)); p = r[6]
    p = alon
    for r in recs: varint(out, zigzag(r[7] - p)); p = r[7]
    return bytes(out)

def load_places_roads(places, log):
    """地名資料庫的路名 → [(縣市區 sub, 名稱, lat, lon)]"""
    if not places or not os.path.exists(places):
        log('  沒有地名資料庫，不補路段'); return []
    c = sqlite3.connect('file:%s?mode=ro' % places, uri=True)
    rows = c.execute("SELECT name, sub, lat, lon FROM place WHERE kind='road' AND sub IS NOT NULL").fetchall()
    c.close()
    return rows

def split_sub(sub):
    s = normalize(sub)
    for c in CITIES:
        if s.startswith(c) and len(s) > len(c):
            d = s[len(c):]
            if d[-1:] in ('區', '鄉', '鎮', '市') and 2 <= len(d) <= 4:
                return c, d
    return None

def build(raw_path, out, places, log):
    t0 = time.time()
    meta_raw = {}
    if os.path.exists(raw_path + '.json'):
        meta_raw = json.load(open(raw_path + '.json', encoding='utf-8'))
    roads = load_places_roads(places, log)
    # 合法（縣市, 區）：地名資料庫的行政區副標 ∪ 門牌標籤中出現 ≥ 50 次的組合
    valid = set()
    for name, sub, lat, lon in roads:
        cd = split_sub(sub)
        if cd: valid.add(cd)
    from collections import Counter, defaultdict
    pair_count = Counter()
    groups = defaultdict(list)   # (city, district, roadkey) → [rec]
    stats = Counter()
    with open(raw_path, encoding='utf-8') as f:
        for line in f:
            p = line.rstrip('\n').split('\t')
            if len(p) < 7: stats['bad_line'] += 1; continue
            lat, lon = float(p[0]), float(p[1])
            city, dist = normalize(p[2]), normalize(p[3])
            pair_count[(city, dist)] += 1
            street = p[4] if p[4].strip() else p[6]
            if not street.strip(): stats['no_street'] += 1; continue
            hns = [h for h in re.split(r'[;、,，]', p[5]) if h.strip()]
            if not hns: stats['no_number'] += 1; continue
            for hn in hns:
                hn = re.split(r'[~～]', hn)[0]
                s = normalize(street) + normalize(hn)
                head, section, lane, lane_sub, alley, alley_sub, main, sub = parse_tail(s)
                if main is None or not head or head[-1:].isdigit():
                    stats['unparsed'] += 1
                    if stats['unparsed'] <= 30: stats['ex_unparsed_' + str(stats['unparsed'])] = s
                    continue
                vals = [lane or 0, lane_sub or 0, alley or 0, alley_sub or 0, main, sub or 0]
                if max(vals) > 65535 or main == 0: stats['out_of_range'] += 1; continue
                groups[(city, dist, road_key(head, section))].append(
                    (vals[0], vals[1], vals[2], vals[3], vals[4], vals[5], round(lat * Q), round(lon * Q)))
                stats['records'] += 1
    for pair, cnt in pair_count.items():
        if cnt >= 50 and pair[1][-1:] in ('區', '鄉', '鎮', '市') and pair[0] in CITIES: valid.add(pair)
    log('  讀入 %d 筆門牌記錄、%d 組路段；合法行政區 %d（%.0fs）' % (stats['records'], len(groups), len(valid), time.time() - t0))
    # 整理每個路段
    streets = {}   # (city, dist, key) → (recs, alat, alon)
    dropped_pair = 0
    for (city, dist, key), recs in groups.items():
        if (city, dist) not in valid:
            dropped_pair += len(recs); continue
        recs.sort()
        uniq = []
        for r in recs:
            if uniq and uniq[-1][:6] == r[:6]: stats['dup'] += 1; continue
            uniq.append(r)
        lats = sorted(r[6] for r in uniq); lons = sorted(r[7] for r in uniq)
        streets[(city, dist, key)] = (uniq, lats[len(lats) // 2], lons[len(lons) // 2])
    stats['dropped_invalid_district'] = dropped_pair
    # 地名資料庫的路段（沒有門牌的才補）
    road_only = {}
    for name, sub, lat, lon in roads:
        cd = split_sub(sub)
        if not cd: continue
        head, section, lane, lane_sub, alley, alley_sub, main, s2 = parse_tail(normalize(name))
        if main is not None or lane is not None or alley is not None or not head: continue
        k = (cd[0], cd[1], road_key(head, section))
        if k in streets or k in road_only: continue
        road_only[k] = ([], round(lat * Q), round(lon * Q))
    stats['road_only'] = len(road_only)
    streets.update(road_only)
    # 行政區表
    dists = sorted({(c, d) for c, d, _ in streets}, key=lambda x: (x[0].encode(), x[1].encode()))
    dist_idx = {d: i for i, d in enumerate(dists)}
    keys = sorted(streets, key=lambda k: (dist_idx[(k[0], k[1])], k[2].encode()))
    # 字串表
    strings = bytearray(); str_off = {}
    def sref(s):
        if s not in str_off:
            b = s.encode('utf-8'); str_off[s] = (len(strings), len(b)); strings.extend(b)
        return str_off[s]
    # 資料區
    data = bytearray(); street_entries = []; dist_points = Counter(); total = 0; raw_total = 0
    for k in keys:
        recs, alat, alon = streets[k]
        no, nl = sref(k[2])
        if nl > 65535: raise ValueError('路名太長')
        if recs:
            raw = encode_block(recs, alat, alon)
            co = zlib.compressobj(9, zlib.DEFLATED, -15, 9)
            comp = co.compress(raw) + co.flush()
            enc, stored = (1, comp) if len(comp) < len(raw) else (0, raw)
            off = len(data); data.extend(stored); raw_total += len(raw)
            crc = zlib.crc32(stored) & 0xFFFFFFFF
        else:
            enc, off, stored, raw, crc = 0, len(data), b'', b'', 0
        street_entries.append((no, nl, dist_idx[(k[0], k[1])], off, len(stored), len(raw), len(recs), enc, alat, alon, crc))
        dist_points[(k[0], k[1])] += len(recs); total += len(recs)
    # 行政區 entries
    first_street = {}
    for i, k in enumerate(keys):
        first_street.setdefault(dist_idx[(k[0], k[1])], i)
    dist_entries = []
    for i, (c, d) in enumerate(dists):
        co, cl = sref(c); do, dl = sref(d)
        flags = 1 if dist_points[(c, d)] < 2000 else 0
        dist_entries.append((co, do, cl, dl, flags, first_street[i]))
    name_index = sorted(range(len(keys)), key=lambda i: (keys[i][2].encode(), street_entries[i][2]))
    meta = {'format_version': FORMAT_VERSION, 'norm_version': NORM_VERSION,
            'built_at': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'),
            'source': 'OpenStreetMap %s（%s）' % (meta_raw.get('pbf', ''), meta_raw.get('pbf_ts', '')),
            'attribution': '© OpenStreetMap contributors（ODbL 1.0）https://www.openstreetmap.org/copyright',
            'point_count': total, 'street_count': len(keys), 'district_count': len(dists),
            'road_only_count': len(road_only)}
    meta_b = json.dumps(meta, ensure_ascii=False).encode('utf-8')
    # 版面
    HEADER = 72
    DIST_SZ, STREET_SZ = 16, 40
    strings_off = HEADER
    dists_off = strings_off + len(strings)
    dists_off += (-dists_off) % 4
    streets_off = dists_off + DIST_SZ * len(dists)
    nidx_off = streets_off + STREET_SZ * len(keys)
    meta_off = nidx_off + 4 * len(keys)
    data_off = meta_off + len(meta_b)
    body = bytearray()
    body += strings
    body += b'\0' * (dists_off - strings_off - len(strings))
    for co, do, cl, dl, flags, fs in dist_entries:
        body += struct.pack('<IIBBBxI', co, do, cl, dl, flags, fs)
    for no, nl, di, off, sl, rl, cnt, enc, alat, alon, crc in street_entries:
        body += struct.pack('<IHHIIIIiiBxxxI', no, nl, di, off, sl, rl, cnt, alat, alon, enc, crc)
    for i in name_index:
        body += struct.pack('<I', i)
    body += meta_b
    assert HEADER + len(body) == data_off
    tables_crc = zlib.crc32(bytes(body)) & 0xFFFFFFFF
    header = struct.pack('<4sHHIIIIIIIIIIIIIIII', b'TADR', FORMAT_VERSION, NORM_VERSION, len(dists), len(keys), total,
                         strings_off, len(strings), dists_off, streets_off, nidx_off, meta_off, len(meta_b),
                         data_off, len(data), tables_crc, 0, 0, 0)
    assert len(header) == HEADER, len(header)
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    tmp = out + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(header); f.write(body); f.write(data)
    os.replace(tmp, out)
    res = {k: v for k, v in stats.items()}
    res.update(meta); res['size_mb'] = round(os.path.getsize(out) / 1024 ** 2, 1)
    res['data_mb'] = round(len(data) / 1024 ** 2, 1); res['raw_block_mb'] = round(raw_total / 1024 ** 2, 1)
    res['tables_mb'] = round(len(body) / 1024 ** 2, 1); res['build_sec'] = round(time.time() - t0)
    res['district_points'] = {c + d: dist_points[(c, d)] for c, d in dists}
    log('寫出 %s：%.1f MB，門牌 %d、路段 %d（純路段 %d）、行政區 %d，%.0fs' % (
        out, res['size_mb'], total, len(keys), len(road_only), len(dists), time.time() - t0))
    return res

# ---------------------------------------------------------------- 讀包與查詢（驗證用；iOS AddressIndex 的參考實作）
class AddrPack:
    def __init__(self, path):
        self.b = open(path, 'rb').read()
        h = struct.unpack_from('<4sHHIIIIIIIIIIIIIIII', self.b, 0)
        assert h[0] == b'TADR'
        (_, self.ver, self.normver, self.nd, self.ns, self.npts, self.so, self.sl, self.do, self.sto, self.nio,
         self.mo, self.ml, self.dao, self.dal, crc, _, _, _) = h
        assert zlib.crc32(self.b[72:self.dao]) & 0xFFFFFFFF == crc, 'tables CRC'
        self.meta = json.loads(self.b[self.mo:self.mo + self.ml].decode('utf-8'))
        self.dists = []
        for i in range(self.nd):
            co, do, cl, dl, flags, fs = struct.unpack_from('<IIBBBxI', self.b, self.do + 16 * i)
            self.dists.append((self.s(co, cl), self.s(do, dl), flags, fs))
    def s(self, o, l):
        return self.b[self.so + o:self.so + o + l].decode('utf-8')
    def street(self, i):
        no, nl, di, off, sl, rl, cnt, alat, alon, enc, crc = struct.unpack_from('<IHHIIIIiiBxxxI', self.b, self.sto + 40 * i)
        return dict(name=self.s(no, nl), dist=di, off=off, sl=sl, rl=rl, cnt=cnt, alat=alat, alon=alon, enc=enc, crc=crc)
    def records(self, st):
        stored = self.b[self.dao + st['off']:self.dao + st['off'] + st['sl']]
        raw = zlib.decompress(stored, -15) if st['enc'] == 1 else stored
        vals = []; i = 0
        while i < len(raw):
            v = 0; sh = 0
            while True:
                c = raw[i]; i += 1; v |= (c & 0x7F) << sh; sh += 7
                if c < 0x80: break
            vals.append(v)
        n = st['cnt']; cols = [vals[k * n:(k + 1) * n] for k in range(8)]
        out = []; lane = 0; pg = None; pm = 0; la = st['alat']; lo = st['alon']
        for j in range(n):
            lane += cols[0][j]; g = (lane, cols[1][j], cols[2][j], cols[3][j])
            m = pm + cols[4][j] if g == pg else cols[4][j]
            pg = g; pm = m
            z = cols[6][j]; la += (z >> 1) ^ -(z & 1)
            z = cols[7][j]; lo += (z >> 1) ^ -(z & 1)
            out.append(g + (m, cols[5][j], la / Q, lo / Q))
        return out
    def find(self, dist_i, key):
        _, _, _, fs = self.dists[dist_i]
        end = self.dists[dist_i + 1][3] if dist_i + 1 < self.nd else self.ns
        kb = key.encode()
        lo, hi = fs, end
        while lo < hi:
            mid = (lo + hi) // 2
            if self.street(mid)['name'].encode() < kb: lo = mid + 1
            else: hi = mid
        if lo < end and self.street(lo)['name'] == key: return lo
        return None

def verify(path, queries, log):
    pk = AddrPack(path)
    log('meta：%s' % json.dumps(pk.meta, ensure_ascii=False))
    names = {d for _, d, _, _ in pk.dists}
    for q in queries:
        p = parse(q, names)
        if not p: log('%s → 解析失敗' % q); continue
        key = road_key(p['road'], p['section'])
        hits = []
        for i, (c, d, fl, fs) in enumerate(pk.dists):
            if p['district'] and d != p['district']: continue
            if p['city'] and c != p['city']: continue
            si = pk.find(i, key)
            if si is None: continue
            st = pk.street(si)
            recs = pk.records(st) if st['cnt'] else []
            want = (p['lane'] or 0, p['lane_sub'] or 0, p['alley'] or 0, p['alley_sub'] or 0, p['main'] or 0, p['sub'] or 0)
            ex = [r for r in recs if r[:6] == want]
            hits.append((c + d, key, '精確' if ex else ('近似' if recs else '僅路段'),
                         (ex[0][6], ex[0][7]) if ex else (st['alat'] / Q, st['alon'] / Q)))
        log('%s → %s' % (q, hits[:5] if hits else '查無'))

def main():
    ap = argparse.ArgumentParser(description='Touror 門牌包建置')
    ap.add_argument('--pbf'); ap.add_argument('--out'); ap.add_argument('--work')
    ap.add_argument('--places', help='地名資料庫（補路段），預設同資料夾 touror_places.sqlite')
    ap.add_argument('--reuse-raw', action='store_true')
    ap.add_argument('--scan-only', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--swift-test', help='Swift 測試檔（比對向量是否一致）',
                    default=os.path.join(HERE, '..', '..', 'ios', 'UnitTests', 'AddressSearchTests.swift'))
    ap.add_argument('--verify'); ap.add_argument('--query', nargs='*', default=[])
    a = ap.parse_args()
    log = lambda s: print(time.strftime('%H:%M:%S'), s, flush=True)
    if a.selftest:
        sys.exit(1 if selftest(os.path.join(HERE, 'addr_vectors.tsv'), a.swift_test) else 0)
    if a.verify:
        verify(a.verify, a.query, log); return
    if not a.out: ap.error('需要 --out')
    work = a.work or os.path.join(os.path.dirname(os.path.abspath(a.out)), '_work')
    os.makedirs(work, exist_ok=True)
    raw = os.path.join(work, 'raw_addr.tsv')
    if a.scan_only:
        scan(a.pbf, raw, log); return
    if not (a.reuse_raw and os.path.exists(raw)):
        if not a.pbf: ap.error('需要 --pbf')
        r = subprocess.run([sys.executable, '-I', os.path.abspath(__file__), '--scan-only', '--pbf', a.pbf, '--out', a.out, '--work', work])
        if r.returncode: sys.exit(r.returncode)
        try: os.remove(os.path.join(work, 'addr_nodes.idx'))
        except OSError: pass
    if selftest(os.path.join(HERE, 'addr_vectors.tsv')):
        log('正規化測試向量失敗，不建包'); sys.exit(2)
    res = build(raw, a.out, a.places or os.path.join(HERE, 'touror_places.sqlite'), log)
    with open(os.path.join(work, 'addr_build_stats.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=1)

if __name__ == '__main__':
    main()
