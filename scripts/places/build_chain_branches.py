"""四大超商分店名升格：用官方門市名冊（地址／座標）把庫內只有品牌名的超商 POI 改成「品牌＋門市名」。

輸入（全部唯讀，不覆蓋）：
  --db           候選庫（可重複，順序＝配對優先順序；第一個是主庫，名冊新增的點只寫進主庫）
  --rosters      名冊資料夾（fetch_chain_rosters.py 產生的 7-11_emap.csv、OK_okmart.csv）
  --gcis         商工登記連鎖加盟分公司（真值/32086.csv，政府資料集 32086）：全家、萊爾富的名冊來源
  --addr         touror_addr.bin（門牌包，地址轉座標、地址比對）
輸出：--outdir 內的新庫（檔名同輸入）＋ CSV 報表 ＋ summary.json。

流程：1 盤點（前）→ 2 名冊正規化＋地址轉座標 → 3 配對（同品牌；先「同門牌且 <=300 m」，再「<=150 m 最近」；一對一）
      → 4 改標題（沿用 build_places_v2 的 _BRAND_ZH 格式「品牌 門市名」）、補 FTS → 5 名冊有庫內沒有者新增（只主庫）
      → 6 庫內有名冊沒有者標「疑似歇業」（不刪）→ 盤點（後）。
重跑：python -I build_chain_branches.py   （預設路徑即本機慣例；不用參數）
記憶體：單進程，通常 < 2 GB。
"""
import argparse, collections, csv, datetime, importlib.util, json, math, os, re, shutil, sqlite3, statistics, sys, time, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))

def load(name, fn):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, fn))
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m)
    return m

ba = load('build_addr', 'build_addr.py')
bp = load('build_places_v2', 'build_places_v2.py')
import numpy as np
from scipy.spatial import cKDTree

CITIES = ba.CITIES
BRAND_ZH = {'7-11': '7-11', '全家': '全家', '萊爾富': '萊爾富', 'OK': 'OK超商'}
BRANDS = ['7-11', '全家', '萊爾富', 'OK']
ALIASES = {'7-11': ['7-ELEVEN', '7-Eleven', '統一超商', '小七', '711'], '全家': ['全家便利商店', 'FamilyMart'],
           '萊爾富': ['萊爾富便利商店', 'Hi-Life'], 'OK': ['OK超商', 'OK便利商店', 'OK Mart', '來來超商']}
GCIS_IDS = {'23060248': '全家', '23285582': '萊爾富'}

def nz(s):
    return unicodedata.normalize('NFKC', s or '').replace('臺', '台').replace('巿', '市').strip()

# ---------------------------------------------------------------- 品牌判斷
_TOK = {
    '7-11': re.compile(r'(7[\s\-]?eleven|7[\s\-]?11|統一便利商店|統一超商|小七|seven[\s\-]?eleven|ibon)', re.I),
    '全家': re.compile(r'(全家便利商店|全家便利店|全家超商|全家|family\s?mart|famimart)', re.I),
    '萊爾富': re.compile(r'(萊爾富便利商店|萊爾富超商|萊爾富|hi[\s\-]?life)', re.I),
    'OK': re.compile(r'(ok\s?超商|ok\s?便利商店|ok\s?mart|okmart|來來超商|ok)', re.I),
}
_START = {
    '7-11': re.compile(r'^(7[\s\-]?eleven|7[\s\-]?11|統一超商|統一便利商店|seven[\s\-]?eleven)', re.I),
    '全家': re.compile(r'^(全家|family\s?mart|famimart)', re.I),
    '萊爾富': re.compile(r'^(萊爾富|hi[\s\-]?life)', re.I),
    'OK': re.compile(r'^(ok\s?超商|ok\s?便利|ok\s?mart|okmart|來來超商|ok[\s\-]+\S)', re.I),
}
_NOTSTORE = re.compile(r'(機車|養生|茶|福|水電|行$|館|飲|餐|藥|肉|餅|水果|服飾|美髮|洗|修|車|工廠|批發|蔬果|雜貨|電器|診所|幼|國小|學)')

def classify_brand(name):
    """回傳 (品牌, 'full'|'brand'|'abbr') 或 (None, None)。abbr＝只有「7-11」「7-ELEVEN」這種縮寫（僅 7-11 判）。"""
    n = nz(name)
    for b in BRANDS:
        if not _START[b].match(n):
            continue
        rest = _TOK[b].sub(' ', n)
        rest = re.sub(r'[\s\-－_·.,;()（）/]+', '', rest)
        if not rest:
            kind = 'brand'
            if b == '7-11' and re.fullmatch(r'(7[\s\-]?11|7[\s\-]?eleven|seven[\s\-]?eleven)', n, re.I):
                kind = 'abbr'
            return b, kind
        if b != '7-11' and (not rest.endswith(('店', '門市', '站')) or len(rest) > 10 or _NOTSTORE.search(rest)):
            return None, None
        if b == '7-11' and _NOTSTORE.search(rest) and not rest.endswith(('店', '門市')):
            return None, None
        return b, 'full'
    return None, None

def city_of(sub):
    s = nz(sub)[:3]
    return s if s in CITIES else '未判定'

# ---------------------------------------------------------------- 門牌包地址
class Geo:
    def __init__(self, path):
        self.pk = ba.AddrPack(path)
        self.districts = set(d for _, d, _, _ in self.pk.dists)
        self.didx = {(c, d): i for i, (c, d, _, _) in enumerate(self.pk.dists)}
        self.cache = {}
        self.village = re.compile(r'^[^0-9]{1,3}?[里村]')

    def parse(self, text):
        t = re.split(r'[、,，;；]', nz(text))[0]
        t = re.sub(r'(?<=號)(.*)$', lambda m: '' if not re.match(r'^之\d', m.group(1)) else m.group(1), t)
        return ba.parse(t, self.districts)

    def key(self, p, city=None):
        if not p: return None
        z = lambda v: v or 0
        return ((city or p['city']), p['district'], ba.road_key(p['road'], p['section']),
                z(p['lane']), z(p['lane_sub']), z(p['alley']), z(p['alley_sub']), z(p['main']), z(p['sub']))

    def records(self, si):
        r = self.cache.get(si)
        if r is None:
            r = self.cache[si] = self.pk.records(self.pk.street(si))
        return r

    def locate(self, text):
        """地址 → (lat, lon, 精度 'exact'|'near', key, 原因)。失敗時 lat 為 None、最後一欄是原因。"""
        p = self.parse(text)
        if not p: return None, None, '', None, '無法解析為門牌'
        if not p['city'] or not p['district']: return None, None, '', None, '縣市或鄉鎮區缺漏'
        di = self.didx.get((p['city'], p['district']))
        if di is None: return None, None, '', self.key(p), '行政區不在門牌包'
        rk = ba.road_key(p['road'], p['section'])
        si = self.pk.find(di, rk)
        if si is None:
            road2 = self.village.sub('', p['road'], count=1)
            if road2 != p['road']:
                p = dict(p, road=road2); rk = ba.road_key(road2, p['section']); si = self.pk.find(di, rk)
        key = self.key(p)
        if si is None: return None, None, '', key, '路名不在門牌包'
        want = key[3:]
        recs = self.records(si)
        best = None
        for r in recs:
            if tuple(r[:6]) == want[:6]: return r[6], r[7], 'exact', key, ''
        grp = [r for r in recs if tuple(r[:4]) == want[:4]]
        if grp and want[4]:
            grp.sort(key=lambda r: (abs(r[4] - want[4]) + (0 if (r[4] - want[4]) % 2 == 0 else 3)))
            r = grp[0]
            if abs(r[4] - want[4]) <= 12: return r[6], r[7], 'near', key, ''
        return None, None, '', key, '門牌包無此號碼或附近號碼'

# ---------------------------------------------------------------- 名冊
SERIAL = re.compile(r'^(第?[\d０-９一二三四五六七八九十百千○〇Ｏ零O]+|.{1,3}[市縣]第[\d０-９一二三四五六七八九十百千○〇Ｏ零O]+)$')
LEGACY = re.compile(r'^(北縣|北市|桃縣|中縣|中市|南縣|南市|高縣|高市|竹縣|竹市|嘉縣|嘉市|苗縣|彰縣|投縣|雲縣|屏縣|宜縣|花縣|東縣|基市|澎縣)')

def label_of(brand, raw):
    s = nz(raw)
    s = re.sub(r'(分公司|分店)$', '', s).strip()
    if brand == 'OK':
        s = re.sub(r'^(ok\s?超商|ok\s?便利商店|ok\s?mart|okmart|ok)[\s\-]*', '', s, flags=re.I).strip()
    if brand == '7-11':
        s = re.sub(r'^(7[\s\-]?eleven|7[\s\-]?11|統一超商)[\s\-]*', '', s, flags=re.I).strip()
    if brand == '萊爾富':
        s = LEGACY.sub('', s)
    if not s or SERIAL.match(s) or re.search(r'公司$', s): return None
    if brand in ('7-11',):
        return s if s.endswith(('門市', '店')) else s + '門市'
    return s if s.endswith(('店', '門市')) else s + '店'

def title_of(brand, label):
    return '%s %s' % (BRAND_ZH[brand], label)

def read_rosters(rdir, gcis, geo, log):
    """回傳 entries 與無法轉座標清單。entry: dict(brand,label,addr,lat,lon,prec,key,src,id)"""
    E, bad = [], []
    def add(brand, rawname, addr, lat, lon, src, sid, active_note=''):
        e = dict(brand=brand, label=label_of(brand, rawname), raw=rawname, addr=nz(addr), lat=lat, lon=lon,
                 prec='roster' if lat is not None else '', src=src, id=sid)
        p = geo.parse(addr)
        e['key'] = geo.key(p) if p else None
        if lat is None:
            la, lo, prec, key, why = geo.locate(addr)
            if la is None:
                bad.append(dict(brand=brand, label=e['label'] or '', raw=rawname, addr=addr, src=src, reason=why)); e['reason'] = why
            else:
                e.update(lat=la, lon=lo, prec=prec, key=key or e['key'])
        E.append(e)
    p = os.path.join(rdir, '7-11_emap.csv')
    if os.path.exists(p):
        for r in csv.DictReader(open(p, encoding='utf-8-sig')):
            add('7-11', r['name'], r['address'], float(r['lat']), float(r['lon']), '7-ELEVEN 官方 emap.pcsc.com.tw', r['id'])
    p = os.path.join(rdir, 'OK_okmart.csv')
    if os.path.exists(p):
        for r in csv.DictReader(open(p, encoding='utf-8-sig')):
            add('OK', r['name'], r['address'], None, None, 'OK mart 官方 okmart.com.tw', r['id'])
    gc = collections.Counter()
    for r in csv.DictReader(open(gcis, encoding='utf-8-sig')):
        b = GCIS_IDS.get(r['公司統一編號'].strip())
        if not b or r['分公司狀態'].strip() != '01': continue
        gc[b] += 1
        add(b, r['分公司名稱'], r['分公司地址'], None, None, '商工登記 data.gov.tw/dataset/32086', r['分公司統一編號'])
    log('名冊：' + '、'.join('%s %d（有門市名 %d、有座標 %d）' % (b, sum(1 for e in E if e['brand'] == b),
        sum(1 for e in E if e['brand'] == b and e['label']), sum(1 for e in E if e['brand'] == b and e['lat'] is not None)) for b in BRANDS))
    return E, bad

# ---------------------------------------------------------------- 庫
def xy(lat, lon):
    return np.column_stack((np.asarray(lon) * (111320 * math.cos(math.radians(23.7))), np.asarray(lat) * 110574))

def read_pois(path, geo):
    con = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)
    cols = [r[1] for r in con.execute('PRAGMA table_info(place)')]
    hasnote = 'note' in cols
    out = []
    for r in con.execute('SELECT id,name,kind,sub,lat,lon,weight,%s FROM place WHERE kind=\'shop\'' % ('note' if hasnote else 'NULL')):
        b, k = classify_brand(r[1])
        if not b: continue
        city = city_of(r[3])
        key = None
        if r[7]:
            p = geo.parse(r[7])
            if p: key = geo.key(p, city)
        out.append(dict(id=r[0], name=r[1], sub=r[3] or '', lat=r[4], lon=r[5], w=r[6], note=r[7], brand=b, kind=k, city=city, key=key,
                        db=os.path.basename(path)))
    total = con.execute('SELECT COUNT(*), MAX(id) FROM place').fetchone()
    con.close()
    return out, hasnote, total

def inventory(pois):
    c = collections.Counter()
    for p in pois: c[p['brand'], p['city'], p['kind']] += 1
    return c

def match(entries, dbpois, log):
    """dbpois: list（依庫順序合併）。回傳 pairs{entry_idx: (poi, dist, how)}。"""
    used = set(); pairs = {}
    for b in BRANDS:
        P = [p for p in dbpois if p['brand'] == b]
        E = [i for i, e in enumerate(entries) if e['brand'] == b and e['lat'] is not None]
        if not P or not E: continue
        tree = cKDTree(xy([p['lat'] for p in P], [p['lon'] for p in P]))
        ex = xy([entries[i]['lat'] for i in E], [entries[i]['lon'] for i in E])
        bykey = collections.defaultdict(list)
        for j, p in enumerate(P):
            if p['key']: bykey[p['key']].append(j)
        # Pass A：同門牌
        for n, i in enumerate(E):
            e = entries[i]
            if not e['key']: continue
            cand = [(float(np.hypot(*(ex[n] - tree.data[j]))), j) for j in bykey.get(e['key'], ()) if id(P[j]) not in used]
            cand = [c for c in cand if c[0] <= 300]
            if cand:
                d, j = min(cand); used.add(id(P[j])); pairs[i] = (P[j], d, 'addr')
        # Pass B：150 m 最近，距離小的先配
        pend = []
        for n, i in enumerate(E):
            if i in pairs: continue
            ds, js = tree.query(ex[n], k=min(6, len(P)), distance_upper_bound=150)
            for d, j in zip(np.atleast_1d(ds), np.atleast_1d(js)):
                if j < len(P): pend.append((float(d), i, int(j)))
        pend.sort()
        for d, i, j in pend:
            if i in pairs or id(P[j]) in used: continue
            used.add(id(P[j])); pairs[i] = (P[j], d, 'dist')
    return pairs

# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser()
    base = os.path.join(HERE, '候選-分店名')
    ap.add_argument('--db', action='append', default=None)
    ap.add_argument('--rosters', default=os.path.join(base, '名冊'))
    ap.add_argument('--gcis', default=os.path.join(HERE, '真值', '32086.csv'))
    ap.add_argument('--addr', default=os.path.join(HERE, 'touror_addr.bin'))
    ap.add_argument('--outdir', default=os.path.join(base, '連鎖分店名'))
    a = ap.parse_args()
    dbs = a.db or [os.path.join(base, 'touror_places.sqlite'), os.path.join(base, 'touror_places_overture.sqlite')]
    os.makedirs(a.outdir, exist_ok=True)
    logf = open(os.path.join(a.outdir, 'build.log'), 'w', encoding='utf-8')
    def log(s):
        print(s, flush=True); logf.write(s + '\n'); logf.flush()
    t0 = time.time()
    geo = Geo(a.addr)
    log('門牌包 %d 街段' % geo.pk.ns)
    entries, bad = read_rosters(a.rosters, a.gcis, geo, log)
    pois_by_db, meta = [], []
    for d in dbs:
        ps, hasnote, tot = read_pois(d, geo)
        pois_by_db.append(ps); meta.append((d, hasnote, tot))
        log('%s：超商 POI %d（總 %d 筆）' % (os.path.basename(d), len(ps), tot[0]))
    allp = [p for ps in pois_by_db for p in ps]
    before = inventory(allp)
    pairs = match(entries, allp, log)
    log('配對 %d / 名冊 %d' % (len(pairs), len(entries)))

    # 改名
    renames = []  # (poi, new_title, new_note)
    matched_ids = set()
    for i, (p, d, how) in pairs.items():
        e = entries[i]; matched_ids.add(id(p))
        p['matched'] = True
        if not e['label']:
            continue
        new = title_of(e['brand'], e['label'])
        if nz(p['name']) == new:
            continue
        p['new'] = new
        note = p['note']
        if not note and e['key']:
            k = e['key']; note = (k[1] or '') + ba.display(k[2], None, k[3] or None, k[4] or None, k[5] or None, k[6] or None, k[7] or None, k[8] or None)
        renames.append((p, new, note, d, how))
    # 新增（只主庫）
    weights = {b: int(statistics.median([p['w'] for p in allp if p['brand'] == b] or [100])) for b in BRANDS}
    added, unlocated, noname = [], [], []
    for i, e in enumerate(entries):
        if i in pairs: continue
        if e['lat'] is None: continue
        if not e['label']:
            noname.append(e); continue
        k = e['key']
        city = k[0] if k else ''
        sub = ((k[0] or '') + (k[1] or '')) if k else ''
        note = (k[1] or '') + ba.display(k[2], None, k[3] or None, k[4] or None, k[5] or None, k[6] or None, k[7] or None, k[8] or None) if k else e['addr']
        added.append(dict(brand=e['brand'], title=title_of(e['brand'], e['label']), sub=sub, lat=e['lat'], lon=e['lon'], w=weights[e['brand']],
                          note=note, addr=e['addr'], src=e['src'], prec=e['prec']))
    # 疑似歇業
    COMPLETE = {'7-11'}
    closed = [p for p in allp if id(p) not in matched_ids]
    rt = {}
    for b in BRANDS:
        pts = [(e['lat'], e['lon']) for e in entries if e['brand'] == b and e['lat'] is not None]
        if pts: rt[b] = cKDTree(xy([x[0] for x in pts], [x[1] for x in pts]))
    for p in closed:
        t = rt.get(p['brand'])
        p['dup'] = bool(t is not None and t.query(xy([p['lat']], [p['lon']]), distance_upper_bound=200)[0][0] < 200)
    ndup = collections.Counter(p['brand'] for p in closed if p['dup'])

    # 寫庫
    outs = []
    for (d, hasnote, tot), ps in zip(meta, pois_by_db):
        out = os.path.join(a.outdir, os.path.basename(d))
        if os.path.exists(out): os.remove(out)
        shutil.copy2(d, out)
        con = sqlite3.connect(out)
        con.execute('PRAGMA journal_mode=OFF'); con.execute('PRAGMA synchronous=OFF')
        n_up = 0
        for p, new, note, dist, how in renames:
            if p['db'] != os.path.basename(d): continue
            old_g = con.execute('SELECT grams FROM search WHERE rowid=?', (p['id'],)).fetchone()
            extra = ' '.join(bp.grams(x) for x in ALIASES[p['brand']] + [p['name']])
            g = ' '.join(x for x in (bp.grams(new), extra, old_g[0] if old_g else '') if x)
            con.execute('UPDATE search SET grams=? WHERE rowid=?', (g, p['id']))
            if hasnote and note != p['note']:
                con.execute('UPDATE place SET name=?, note=? WHERE id=?', (new, note, p['id']))
            else:
                con.execute('UPDATE place SET name=? WHERE id=?', (new, p['id']))
            n_up += 1
        n_add = 0
        if d == dbs[0]:
            nid = tot[1]
            for r in added:
                nid += 1
                if hasnote:
                    con.execute('INSERT INTO place(id,name,kind,sub,lat,lon,weight,note) VALUES(?,?,?,?,?,?,?,?)',
                                (nid, r['title'], 'shop', r['sub'] or None, round(r['lat'], 6), round(r['lon'], 6), r['w'], r['note']))
                else:
                    con.execute('INSERT INTO place(id,name,kind,sub,lat,lon,weight) VALUES(?,?,?,?,?,?,?)',
                                (nid, r['title'], 'shop', r['sub'] or None, round(r['lat'], 6), round(r['lon'], 6), r['w']))
                b = next(k for k, v in BRAND_ZH.items() if v == r['title'].split(' ')[0])
                g = ' '.join(x for x in [bp.grams(r['title'])] + [bp.grams(x) for x in ALIASES[b]] if x)
                con.execute('INSERT INTO search(rowid, grams) VALUES(?,?)', (nid, g))
                r['id'] = nid; n_add += 1
        con.execute("INSERT INTO search(search) VALUES('optimize')")
        cnt = con.execute('SELECT COUNT(*) FROM place').fetchone()[0]
        con.execute("INSERT OR REPLACE INTO meta VALUES('row_count',?)", (str(cnt),))
        con.execute("INSERT OR REPLACE INTO meta VALUES('chain_branches', ?)", (
            '官方名冊升格 %s：7-ELEVEN emap.pcsc.com.tw、OK mart okmart.com.tw、商工登記 data.gov.tw/dataset/32086' % datetime.date.today(),))
        con.commit(); con.execute('VACUUM'); con.close()
        outs.append((out, n_up, n_add, cnt))
        log('%s：改名 %d、新增 %d、總 %d、%.1f MB' % (os.path.basename(out), n_up, n_add, cnt, os.path.getsize(out) / 1048576))

    # 盤點（後）
    after = collections.Counter()
    renamed_ids = {id(p) for p, *_ in renames}
    for p in allp:
        kind = p['kind']
        if id(p) in renamed_ids: kind = 'full'
        after[p['brand'], p['city'], kind] += 1
    for r in added:
        k = r['sub'][:3] if r['sub'] else '未判定'
        after[next(b for b, v in BRAND_ZH.items() if v == r['title'].split(' ')[0]), k if k in CITIES else '未判定', 'full'] += 1

    def w(name, header, rows):
        with open(os.path.join(a.outdir, name), 'w', encoding='utf-8-sig', newline='') as f:
            c = csv.writer(f); c.writerow(header); c.writerows(rows)
    inv = []
    for b in BRANDS:
        for city in CITIES + ['未判定']:
            bv = [before[b, city, k] for k in ('full', 'brand', 'abbr')]
            av = [after[b, city, k] for k in ('full', 'brand', 'abbr')]
            if sum(bv) or sum(av): inv.append([b, city] + [sum(bv)] + bv + [sum(av)] + av)
    w('盤點-前後.csv', ['品牌', '縣市', '前_總', '前_完整門市名', '前_只有品牌名', '前_只有縮寫', '後_總', '後_完整門市名', '後_只有品牌名', '後_只有縮寫'], inv)
    rate = []
    for b in BRANDS:
        for city in CITIES + ['未判定']:
            es = [(i, e) for i, e in enumerate(entries) if e['brand'] == b and ((e['key'][0] if e['key'] else '未判定') if e['key'] and e['key'][0] in CITIES else '未判定') == city]
            if not es and not before[b, city, 'full'] + before[b, city, 'brand'] + before[b, city, 'abbr']: continue
            m = sum(1 for i, _ in es if i in pairs)
            nadd = sum(1 for r in added if r['title'].split(' ')[0] == BRAND_ZH[b] and (r['sub'][:3] if r['sub'][:3] in CITIES else '未判定') == city)
            dbn = sum(before[b, city, k] for k in ('full', 'brand', 'abbr'))
            unm = sum(1 for p in closed if p['brand'] == b and p['city'] == city)
            rate.append([b, city, len(es), m, '%.1f%%' % (100 * m / len(es)) if es else '', nadd, dbn, unm])
    w('對配率.csv', ['品牌', '縣市', '名冊筆數', '配對到庫內', '對配率', '新增', '庫內原有', '庫內未配對'], rate)
    w('改名對照.csv', ['庫', 'id', '縣市區', '原名', '新名', '距離m', '方式'],
      [[p['db'], p['id'], p['sub'], p['name'], new, '%.0f' % d, how] for p, new, note, d, how in renames])
    w('新增門市.csv', ['品牌', '標題', '縣市區', 'lat', 'lon', '地址', '座標精度', '來源', '新id'],
      [[r['title'].split(' ')[0], r['title'], r['sub'], r['lat'], r['lon'], r['addr'], r['prec'], r['src'], r.get('id', '')] for r in added])
    w('疑似歇業或名冊未收.csv', ['庫', 'id', '品牌', '標題', '縣市區', 'lat', 'lon', 'note', '可信度'],
      [[p['db'], p['id'], p['brand'], p['name'], p['sub'], p['lat'], p['lon'], p['note'] or '',
        '重複點（200 m 內已有名冊店；多為 OSM 與 Overture 同店兩筆）' if p['dup'] else
        ('疑似歇業（名冊完整且 200 m 內無名冊店）' if p['brand'] in COMPLETE else '名冊未收（名冊不全，僅供參考）')] for p in closed])
    w('無法轉座標.csv', ['品牌', '門市名', '原始名稱', '地址', '來源', '原因'], [[b['brand'], b['label'], b['raw'], b['addr'], b['src'], b['reason']] for b in bad])
    w('未配對名冊.csv', ['品牌', '門市名', '地址', 'lat', 'lon', '座標精度', '狀態'],
      [[e['brand'], e['label'] or '', e['addr'], e['lat'] or '', e['lon'] or '', e['prec'],
        ('無法轉座標：' + e.get('reason', '')) if e['lat'] is None else ('已新增' if e['label'] else '名冊無門市名（登記為序號），未新增')]
       for i, e in enumerate(entries) if i not in pairs])
    # 摘要
    summ = {'built': str(datetime.datetime.now()), 'sec': round(time.time() - t0), 'dbs': {os.path.basename(o): dict(rename=u, added=ad, total=c) for o, u, ad, c in outs},
            'brands': {}}
    for b in BRANDS:
        tb = sum(v for (bb, c, k), v in before.items() if bb == b); fb = sum(v for (bb, c, k), v in before.items() if bb == b and k == 'full')
        ta = sum(v for (bb, c, k), v in after.items() if bb == b); fa = sum(v for (bb, c, k), v in after.items() if bb == b and k == 'full')
        es = [e for e in entries if e['brand'] == b]
        summ['brands'][b] = dict(before_total=tb, before_full=fb, after_total=ta, after_full=fa, roster=len(es),
                                 roster_named=sum(1 for e in es if e['label']), roster_located=sum(1 for e in es if e['lat'] is not None),
                                 matched=sum(1 for i, e in enumerate(entries) if e['brand'] == b and i in pairs),
                                 added=sum(1 for r in added if r['title'].split(' ')[0] == BRAND_ZH[b]),
                                 unlocated=sum(1 for x in bad if x['brand'] == b),
                                 closed=sum(1 for p in closed if p['brand'] == b and not p['dup']), duplicates=ndup[b])
    json.dump(summ, open(os.path.join(a.outdir, 'summary.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    log(json.dumps(summ, ensure_ascii=False, indent=1))

if __name__ == '__main__':
    main()
