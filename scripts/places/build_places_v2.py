# -*- coding: utf-8 -*-
"""Touror 離線目的地搜尋資料庫建置工具 v2（以 build_places.py 為基礎；輸出 touror_places_v2.sqlite）

v2 與 v1 的差異：品牌中文化標題（7-11／全家／萊爾富／OK超商）＋分店名；非連鎖店無中文名時改用中文欄位；
alt_name／brand／branch 與品牌別名寫進 FTS；建庫時用 touror_addr.bin 為每筆 POI 算出地址寫入 note 欄。
永遠啟用（不再需要 --branch-labels，工作目錄 _work_v2）。額外參數 --addr 指向 touror_addr.bin。

（以下為 v1 原說明）

用法（Python 讀外部資料一律加 -I）：
  python -I build_places.py --pbf taiwan-latest.osm.pbf --out touror_places.sqlite
  python -I build_places.py --pbf ... --out ... --reuse-raw      # 跳過掃 PBF，用上次的 raw.pkl 重建
  python -I build_places.py --pbf ... --out ... --branch-labels  # 品牌分店名＋選用 note，使用獨立暫存
  python -I build_places.py --verify touror_places.sqlite        # 只跑驗證查詢

需要：pip install osmium shapely opencc-python-reimplemented
流程：
  階段 1：pyosmium 掃一次 PBF（節點座標索引用 sparse_file_array 落磁碟），
          抽出有名稱的 POI／place、具名或有 ref 的道路代表點、行政區多邊形，存 <work>/raw.pkl。
  階段 2：分類、正規化、行政區副標（點在多邊形）、道路合併、去重，寫 SQLite＋FTS5 bigram。
Schema 見同資料夾 說明.md（user_version=1，iOS 端依此查詢，不要擅改）。
"""
import argparse, array, ctypes, datetime, gc, importlib.util, json, math, os, pickle, re, sqlite3, sys, threading, time, unicodedata

SCHEMA_VERSION = 1
MEM_LIMIT = 5 * 1024 ** 3  # v2：5 GB 私有記憶體上限（門牌包 520 萬點需額外記憶體），超過就中止

# ---------------------------------------------------------------- 記憶體監看（Windows）
class _PMC(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage', ctypes.c_size_t), ('QuotaPagedPoolUsage', ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t), ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t)]

def mem_info():
    """回傳 (目前私有, 峰值私有, 峰值工作集) bytes；非 Windows 回傳 None。"""
    if os.name != 'nt':
        return None
    pmc = _PMC(); pmc.cb = ctypes.sizeof(_PMC)
    k = ctypes.windll.kernel32
    k.GetCurrentProcess.restype = ctypes.c_void_p
    ctypes.windll.psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
    ctypes.windll.psapi.GetProcessMemoryInfo(k.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
    return pmc.PagefileUsage, pmc.PeakPagefileUsage, pmc.PeakWorkingSetSize

def start_watchdog(log):
    def run():
        while True:
            m = mem_info()
            if m and m[0] > MEM_LIMIT:
                log('記憶體超過上限 %.2f GB，中止' % (m[0] / 1024 ** 3))
                os._exit(3)
            time.sleep(2)
    if os.name == 'nt':
        threading.Thread(target=run, daemon=True).start()

def lower_priority():
    if os.name == 'nt':
        k = ctypes.windll.kernel32
        k.GetCurrentProcess.restype = ctypes.c_void_p
        k.SetPriorityClass(ctypes.c_void_p(k.GetCurrentProcess()), 0x4000)  # BELOW_NORMAL
    else:
        try: os.nice(5)
        except Exception: pass

# ---------------------------------------------------------------- 正規化與 bigram（iOS 查詢端必須用同一套規則）
_WS = re.compile(r'\s+')

def norm_name(s):
    """名稱正規化：NFKC（全形→半形）、去前後空白、多個空白併一、臺→台。"""
    s = unicodedata.normalize('NFKC', s)
    s = _WS.sub(' ', s).strip()
    return s.replace('臺', '台')

def is_cjk(ch):
    o = ord(ch)
    return (0x3400 <= o <= 0x4DBF or 0x4E00 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF or
            0x20000 <= o <= 0x3FFFF or 0x3040 <= o <= 0x30FF or 0x3100 <= o <= 0x312F or
            0x31A0 <= o <= 0x31BF or 0xAC00 <= o <= 0xD7AF)

def grams(s):
    """名稱 → FTS 欄位字串。
    先 norm_name、轉小寫；逐字掃描：中日韓字元各自是一個單位，英數（Unicode 字母／數字）連續成一個詞，其他符號與空白略過。
    連續的中文字（中間只隔符號／空白也算連續）取相鄰兩字 bigram；只有一個中文字的段落存該單字；英數詞原樣輸出。
    例：中山路→「中山 山路」；台8線→「台 8 線」；7-ELEVEN 逢甲門市→「7 eleven 逢甲 甲門 門市」。"""
    s = norm_name(s).lower()
    out, run, word = [], [], []
    def flush_run():
        if len(run) == 1:
            out.append(run[0])
        else:
            out.extend(run[i] + run[i + 1] for i in range(len(run) - 1))
        run.clear()
    def flush_word():
        if word:
            out.append(''.join(word)); word.clear()
    for ch in s:
        if is_cjk(ch):
            flush_word()
            run.append(ch)
        elif ch.isalnum():
            if run: flush_run()
            word.append(ch)
        else:
            flush_word()  # 符號／空白：結束英數詞，但不切斷中文段
    flush_word()
    if run: flush_run()
    return ' '.join(out)

def fts_query(q):
    """查詢字串 → FTS5 MATCH 運算式（phrase）。單一中文字用前綴。"""
    g = grams(q)
    if not g:
        return None
    if ' ' not in g and len(g) == 1 and is_cjk(g):
        return '"%s"*' % g
    return '"%s"' % g

# ---------------------------------------------------------------- 簡體偵測（只決定要不要改用 name:zh-Hant／name:zh）
_SIMP_ONLY = None
def simp_only_chars():
    global _SIMP_ONLY
    if _SIMP_ONLY is None:
        _SIMP_ONLY = set()
        spec = importlib.util.find_spec('opencc')
        if spec and spec.origin:
            p = os.path.join(os.path.dirname(spec.origin), 'dictionary', 'STCharacters.txt')
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    for line in f:
                        parts = line.rstrip('\n').split('\t')
                        if len(parts) == 2 and parts[0] not in parts[1].split():
                            _SIMP_ONLY.add(parts[0])
    return _SIMP_ONLY

def looks_simplified(s):
    so = simp_only_chars()
    return any(ch in so for ch in s)

# ---------------------------------------------------------------- 階段 1：掃 PBF
NAME_KEYS = ('name', 'name:zh-Hant', 'name:zh-TW', 'name:zh_TW', 'name:zh')
KEEP_TAGS = ('place', 'amenity', 'shop', 'tourism', 'historic', 'leisure', 'natural', 'man_made', 'railway',
             'aeroway', 'office', 'craft', 'healthcare', 'public_transport', 'mountain_pass', 'waterway',
             'boundary', 'highway', 'ele', 'wikidata', 'information', 'station', 'religion', 'admin_level')
POI_TRIGGER = ('place', 'amenity', 'shop', 'tourism', 'historic', 'leisure', 'natural', 'man_made', 'railway',
               'aeroway', 'office', 'craft', 'healthcare', 'public_transport', 'mountain_pass')
HW_RANK = {'motorway': 1, 'trunk': 2, 'primary': 3, 'secondary': 4, 'tertiary': 5, 'unclassified': 6,
           'residential': 7, 'living_street': 8, 'service': 8, 'road': 8, 'track': 9, 'path': 9,
           'footway': 9, 'pedestrian': 8, 'cycleway': 9, 'steps': 9, 'bridleway': 9}

_CHAIN_TAGS = ('brand', 'brand:zh', 'branch', 'branch:zh', 'alt_name', 'addr:street', 'addr:housenumber', 'operator')
_CHAIN_NAMES = (
    ('7-Eleven', '7-11', 'Seven Eleven', '統一超商', '小七'),
    ('全家便利商店', '全家', 'FamilyMart'),
    ('萊爾富', '萊爾富便利商店', 'Hi-Life'),
    ('OK超商', 'OK便利商店', 'OK Mart', 'OK'),
)

def chain_key(s):
    return re.sub(r'[\W_]+', '', norm_name(s).casefold())

_CHAIN_ALIASES = {chain_key(s): names[0] for names in _CHAIN_NAMES for s in names}
_BRANCH_SUFFIX = re.compile(r'(?:店|門市|站|分行|分公司|餐廳|專櫃|藥局|營業部)$')

_BRAND_ZH = {'7-Eleven': '7-11', '全家便利商店': '全家', '萊爾富': '萊爾富', 'OK超商': 'OK超商'}  # 使用者定案的品牌顯示名

def zh_prefix(name):
    """名稱以連鎖品牌別名開頭（後接空白或結尾）時，換成中文化品牌名。回傳（新名, canonical）或 None。"""
    low = name.casefold()
    for names in _CHAIN_NAMES:
        for a in sorted(names, key=len, reverse=True):
            al = a.casefold()
            if low.startswith(al) and (len(name) == len(a) or name[len(a)] == ' '):
                return _BRAND_ZH[names[0]] + name[len(a):], names[0]
    return None

def chain_display(t, name):
    """v2 標題：<品牌中文名> <分店名>。回傳（顯示名, None, 統計用品牌, 是否改名）。note 改由門牌包決定。"""
    brand = norm_name(t.get('brand', ''))
    nk, bk = chain_key(name), chain_key(brand)
    if not bk and t.get('shop') == 'convenience':
        brand = _CHAIN_ALIASES.get(nk, '')
        bk = chain_key(brand)
    if not bk and nk in _CHAIN_ALIASES and nk != 'ok':  # 沒有 brand 標籤但名稱本身就是品牌（如「7 eleven」）
        brand = _CHAIN_ALIASES[nk]; bk = chain_key(brand)
    if not bk:
        z = zh_prefix(name)
        return (z[0], None, z[1], True) if z else (name, None, None, False)
    canonical = _CHAIN_ALIASES.get(bk, brand)
    if nk != bk and _CHAIN_ALIASES.get(nk) != canonical:
        z = zh_prefix(name)  # 名稱本身已帶分店（如「7-ELEVEN 逢甲門市」）：只換品牌前綴
        return (z[0], None, canonical, True) if z else (name, None, canonical, False)
    base = _BRAND_ZH.get(canonical, name)
    branch = norm_name(t.get('branch') or t.get('branch:zh') or '')
    if branch:
        key = chain_key(branch)
        if key and key != nk and key != bk and _CHAIN_ALIASES.get(key) != canonical:
            basenames = {name, brand, canonical, base}
            prefix = next((s + ' ' for s in sorted(basenames, key=len, reverse=True)
                           if branch.casefold().startswith(s.casefold() + ' ')), '')
            label = branch[len(prefix):]
            if is_cjk(label[-1]) and not _BRANCH_SUFFIX.search(label):
                label += '門市' if canonical == '7-Eleven' else '店'
            return f'{base} {label}', None, canonical, True
    return base, None, canonical, base != name

def fts_extras(t, orig_name, title):
    """FTS 額外搜尋字串：原始名稱、alt_name、brand、branch、中文別名（各自獨立算 grams，避免跨欄位 bigram）。"""
    ex = []
    if orig_name != title: ex.append(orig_name)
    for k in ('alt_name', 'brand', 'brand:zh', 'branch', 'branch:zh', 'name:zh-Hant', 'name:zh-TW', 'name:zh'):
        for v in re.split(r'[;；]', t.get(k) or ''):
            v = v.strip()
            if v and v != title and v not in ex: ex.append(v)
    bk = chain_key(t.get('brand', '')) or chain_key(orig_name)
    canon = _CHAIN_ALIASES.get(bk)
    if canon:
        for names in _CHAIN_NAMES:
            if names[0] == canon:
                ex.extend(x for x in names if x not in ex)
    return ex

def slim(t, branch_labels=False):
    d = {}
    for k in NAME_KEYS + KEEP_TAGS + _CHAIN_TAGS:
        v = t.get(k)
        if v: d[k] = v
    return d

def poi_candidate(t):
    if 'name' not in t:
        return False
    for k in POI_TRIGGER:
        if k in t: return True
    if t.get('waterway') == 'waterfall': return True
    if t.get('boundary') in ('national_park', 'protected_area'): return True
    if t.get('highway') in ('rest_area', 'services'): return True
    return False

def poly_rep(geom):
    """多邊形代表點：重心在面內就用重心，否則用 representative_point（保證在面內）。"""
    c = geom.centroid
    if not geom.contains(c):
        c = geom.representative_point()
    return c.y, c.x

def scan(pbf, work, log, branch_labels=False, name_ov=None):
    """name_ov：curated 路名覆蓋 {way_id: ...}；None＝讀 curated/路名覆蓋.csv（common.curated_names）。"""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from common.curated_names import load_names, way_tags
    if name_ov is None:
        name_ov = load_names()
    if name_ov: log('curated 路名覆蓋 %d 條 way' % len(name_ov))
    import osmium
    from shapely.geometry import Polygon, MultiPolygon
    import shapely
    idx = os.path.join(work, 'nodes.idx')
    if os.path.exists(idx): os.remove(idx)
    pois, roads, admins = [], [], []
    stats = {'area_rel_fail': 0, 'way_noloc': 0}
    fp = (osmium.FileProcessor(pbf)
          .with_locations('sparse_file_array,' + idx)
          .with_areas()
          .with_filter(osmium.filter.KeyFilter('name', 'ref')))
    t0 = time.time(); n = 0
    for o in fp:
        n += 1
        if n % 500000 == 0:
            m = mem_info()
            log('  掃描 %d 物件 %.0fs 私有記憶體 %s' % (n, time.time() - t0, '%.2f GB' % (m[0] / 1024 ** 3) if m else '?'))
        if o.is_node():
            t = o.tags
            if 'name' in t:
                d = {tg.k: tg.v for tg in t}
                if poi_candidate(d):
                    pois.append((slim(d, branch_labels), o.location.lat, o.location.lon, 'n'))
        elif o.is_way():
            t = o.tags
            hw = t.get('highway')
            if hw in HW_RANK:
                if o.id in name_ov: t = way_tags(o.id, t, name_ov)  # curated 路名覆蓋（原名移到 old_name）
                nm, ref = t.get('name'), t.get('ref')
                if not nm and not ref: continue
                nds = o.nodes
                if len(nds) == 0: continue
                try:
                    mid = nds[len(nds) // 2]
                    roads.append((nm or '', ref or '', HW_RANK[hw], mid.lat, mid.lon,
                                  dict((k, t.get(k)) for k in NAME_KEYS if t.get(k)) if nm else None))
                except Exception:
                    stats['way_noloc'] += 1
                continue
            if 'name' not in t: continue
            d = {tg.k: tg.v for tg in t}
            if not poi_candidate(d): continue
            try:
                pts = [(x.lon, x.lat) for x in o.nodes]
            except Exception:
                stats['way_noloc'] += 1; continue
            if len(pts) >= 4 and pts[0] == pts[-1]:
                try:
                    lat, lon = poly_rep(Polygon(pts))
                except Exception:
                    lon, lat = pts[len(pts) // 2]
            else:
                lon, lat = pts[len(pts) // 2]
            pois.append((slim(d, branch_labels), lat, lon, 'w'))
        elif o.is_area():
            t = o.tags
            d = None
            is_admin = t.get('boundary') == 'administrative' and t.get('admin_level') and 'name' in t
            if not is_admin and o.from_way():
                continue
            if not is_admin:
                d = {tg.k: tg.v for tg in t}
                if not poi_candidate(d): continue
            try:
                polys = []
                for outer in o.outer_rings():
                    ext = [(x.lon, x.lat) for x in outer]
                    holes = [[(x.lon, x.lat) for x in inner] for inner in o.inner_rings(outer)]
                    polys.append(Polygon(ext, holes))
                geom = MultiPolygon(polys) if len(polys) > 1 else polys[0]
                if not geom.is_valid: geom = geom.buffer(0)
            except Exception:
                stats['area_rel_fail'] += 1
                stats.setdefault('area_fail_names', []).append(t.get('name', '') + ('(admin %s)' % t.get('admin_level') if is_admin else ''))
                continue
            if is_admin:
                admins.append((t.get('admin_level'), {tg.k: tg.v for tg in t}, shapely.to_wkb(geom)))
            else:
                lat, lon = poly_rep(geom)
                pois.append((slim(d, branch_labels), lat, lon, 'r'))
    o = None; fp = None; gc.collect()
    try: os.remove(idx)
    except Exception: log('  節點索引檔仍被鎖住，掃描子程序結束後由主程序刪除')
    try:
        hdr = osmium.io.Reader(pbf, osmium.osm.osm_entity_bits.NOTHING).header()
        ts = hdr.get('osmosis_replication_timestamp')
    except Exception:
        ts = ''
    if not ts:
        ts = datetime.datetime.utcfromtimestamp(os.path.getmtime(pbf)).strftime('%Y-%m-%dT%H:%M:%SZ') + '（檔案時間）'
    log('掃描完成 %.0fs：物件 %d、POI 候選 %d、道路段 %d、行政區 %d、%s' % (time.time() - t0, n, len(pois), len(roads), len(admins), stats))
    return {'pois': pois, 'roads': roads, 'admins': admins, 'pbf_ts': ts, 'pbf_name': os.path.basename(pbf),
            'scan_sec': time.time() - t0, 'stats': stats, 'branch_labels': branch_labels}

# ---------------------------------------------------------------- 分類（kind, 基礎權重）；None＝不收
SKIP_AMENITY = {'parking', 'bench', 'waste_basket', 'toilets', 'parking_space', 'bicycle_parking', 'motorcycle_parking',
                'recycling', 'shelter', 'vending_machine', 'post_box', 'parking_entrance', 'charging_station',
                'waste_disposal', 'drinking_water', 'telephone', 'atm', 'clock', 'grit_bin', 'bbq', 'water_point',
                'loading_dock', 'hunting_stand', 'letter_box', 'smoking_area', 'compressed_air'}
FOOD = {'restaurant', 'cafe', 'fast_food', 'bar', 'pub', 'food_court', 'ice_cream', 'biergarten'}
PLACE_W = {'country': 1000, 'state': 1000, 'province': 1000, 'county': 1000, 'municipality': 1000, 'city': 1000,
           'town': 900, 'borough': 800, 'city_block': 250, 'suburb': 650, 'quarter': 500, 'village': 600,
           'hamlet': 450, 'neighbourhood': 400, 'locality': 350, 'isolated_dwelling': 250, 'island': 600,
           'islet': 450, 'archipelago': 600, 'region': 600, 'farm': 200, 'square': 300, 'allotments': 150,
           'plot': 150}
NATURAL_SCENIC = {'water': 500, 'bay': 450, 'beach': 500, 'cape': 500, 'peninsula': 450, 'spring': 400,
                  'hot_spring': 550, 'cave_entrance': 450, 'cliff': 450, 'valley': 450, 'ridge': 450, 'wetland': 450,
                  'wood': 350, 'rock': 400, 'stone': 400, 'arch': 450, 'waterfall': 550, 'glacier': 400,
                  'strait': 400, 'reef': 350, 'tree': 300, 'grassland': 350, 'heath': 300, 'scrub': 250,
                  'sand': 350, 'shingle': 300, 'bare_rock': 350, 'gorge': 500, 'canyon': 500, 'plateau': 450,
                  'hill': 500, 'shoal': 300, 'isthmus': 350, 'coastline': 300, 'landform': 350}
TOURISM_SCENIC = {'attraction': 600, 'viewpoint': 550, 'theme_park': 600, 'zoo': 600, 'museum': 500,
                  'gallery': 400, 'aquarium': 550, 'picnic_site': 350}
TOURISM_LODGING = {'hotel': 350, 'motel': 300, 'hostel': 300, 'guest_house': 300, 'apartment': 250,
                   'camp_site': 400, 'caravan_site': 350, 'chalet': 300, 'alpine_hut': 400, 'wilderness_hut': 350}
SERVICE_W = {'hospital': 450, 'university': 450, 'college': 400, 'townhall': 450, 'police': 350, 'fire_station': 300,
             'school': 300, 'library': 300, 'post_office': 300, 'bank': 250, 'clinic': 250, 'courthouse': 350,
             'community_centre': 300, 'arts_centre': 350, 'theatre': 350, 'cinema': 300, 'motorcycle_rental': 300,
             'car_rental': 250, 'veterinary': 200, 'dentist': 200, 'doctors': 200, 'pharmacy': 200,
             'kindergarten': 200}

def classify(t):
    pt, am = t.get('public_transport'), t.get('amenity')
    if pt in ('platform', 'stop_position', 'stop_area'):
        return None
    if t.get('highway') == 'bus_stop' or t.get('railway') in ('platform', 'stop', 'tram_stop', 'subway_entrance',
                                                                 'switch', 'level_crossing', 'crossing', 'signal',
                                                                 'buffer_stop', 'milestone'):
        return None
    if am in SKIP_AMENITY:
        return None
    p = t.get('place')
    if p:
        return ('place', PLACE_W.get(p, 300)) if p in PLACE_W else None
    nat = t.get('natural')
    if nat in ('peak', 'volcano', 'saddle') or t.get('mountain_pass') == 'yes':
        w = 600
        try:
            if float(re.sub(r'[^0-9.]', '', t.get('ele', '')) or 0) >= 3000: w = 700
        except ValueError:
            pass
        return 'peak', w
    rw, aw = t.get('railway'), t.get('aeroway')
    if rw in ('station', 'halt'):
        return 'transport', 650 if rw == 'station' else 500
    if aw in ('aerodrome', 'terminal', 'heliport'):
        return 'transport', 750 if aw == 'aerodrome' else 450
    if am in ('bus_station', 'ferry_terminal'):
        return 'transport', 500
    if pt == 'station':
        return 'transport', 500
    if t.get('highway') in ('rest_area', 'services'):
        return 'transport', 600
    if am == 'fuel':
        return 'fuel', 400
    if am == 'marketplace':
        return 'market', 550
    tour = t.get('tourism')
    if tour in TOURISM_SCENIC:
        return 'scenic', TOURISM_SCENIC[tour]
    if nat in NATURAL_SCENIC:
        return 'scenic', NATURAL_SCENIC[nat]
    if nat:
        return None  # 其他 natural（如 coastline 片段、tree_row）不收
    if t.get('waterway') == 'waterfall':
        return 'scenic', 550
    if t.get('boundary') in ('national_park', 'protected_area'):
        return 'scenic', 600 if t.get('boundary') == 'national_park' else 450
    lei = t.get('leisure')
    if lei in ('park', 'garden', 'nature_reserve'):
        return 'scenic', 450
    if t.get('historic'):
        return 'scenic', 450
    if tour in TOURISM_LODGING:
        return 'lodging', TOURISM_LODGING[tour]
    if am in FOOD:
        return 'food', 250
    if am == 'place_of_worship':
        return 'worship', 350
    if t.get('shop'):
        return 'shop', 300 if t.get('shop') in ('motorcycle', 'motorcycle_repair', 'tyres') else 200
    if am:
        return 'service', SERVICE_W.get(am, 220)
    if t.get('healthcare'):
        return 'service', 250
    if t.get('office') or t.get('craft'):
        return 'service', 150
    if tour == 'information':
        return ('poi', 400) if t.get('information') in ('office', 'visitor_centre') else None
    if tour:
        return 'poi', 250
    if lei:
        return 'poi', 250
    if t.get('man_made') in ('lighthouse', 'tower', 'pier', 'bridge', 'observatory', 'dam', 'breakwater', 'windmill',
                             'water_tower', 'works', 'reservoir_covered'):
        return 'poi', 300 if t.get('man_made') in ('lighthouse', 'dam', 'bridge', 'pier', 'observatory') else 200
    if aw or rw:
        return None
    return None

def has_cjk(s):
    return any(0x3400 <= ord(c) <= 0x9FFF or 0xF900 <= ord(c) <= 0xFAFF for c in s)

def pick_name(t):
    n = t.get('name')
    if not n:
        return None
    if looks_simplified(n):
        for k in ('name:zh-Hant', 'name:zh-TW', 'name:zh_TW', 'name:zh'):
            v = t.get(k)
            if v and not looks_simplified(v):
                return v, True
    elif not has_cjk(n):
        # v2：名稱沒有任何中文時，依序找中文欄位（含 alt_name 內含中文者；多值以 ; 分隔取第一個含中文的）
        for k in ('name:zh-Hant', 'name:zh-TW', 'name:zh_TW', 'name:zh', 'alt_name'):
            for v in re.split(r'[;；]', t.get(k) or ''):
                v = v.strip()
                if v and has_cjk(v) and not looks_simplified(v):
                    return v, True
    return n, False

# ---------------------------------------------------------------- 道路 ref → 路線名稱
_REF_RE = [
    (re.compile(r'^國道?(\d+[甲乙丙]?)號?$'), lambda m: '國道%s號' % m.group(1), 800),
    (re.compile(r'^(?:台|省道)(\d+[甲乙丙丁戊己庚]?)線?$'), lambda m: '台%s線' % m.group(1), 750),
    (re.compile(r'^(?:縣道?)(\d+[甲乙丙丁]?)(?:線|號)?$'), lambda m: '縣道%s' % m.group(1), 550),
    (re.compile(r'^(\d{3}[甲乙丙丁]?)縣道$'), lambda m: '縣道%s' % m.group(1), 550),
    (re.compile(r'^([北桃竹苗中彰投雲嘉南高屏宜花東澎金連基])(\d+[-之]?\d*[甲乙丙丁]?)線?$'), lambda m: '%s%s線' % (m.group(1), m.group(2)), 400),
]

_NUM_REF = re.compile(r'^(\d+)([甲乙丙丁戊己庚]?)$')
FREEWAY_NO = {'1', '2', '3', '4', '5', '6', '8', '10'}

def ref_routes(ref, rank=None):
    """ref → [(路線名稱, 權重)]。台灣 OSM 的省道／國道 ref 多半只寫數字（例如 ref=8），
    依道路等級判斷：motorway 且號碼為國道號 → 國道N號；1–99 → 台N線；100–299 → 縣道N。"""
    out = []
    for r in re.split(r'[;；/]', ref):
        r = norm_name(r).replace(' ', '')
        if not r: continue
        m = _NUM_REF.match(r)
        if m:
            num = int(m.group(1))
            if rank == 1 and m.group(1) in FREEWAY_NO:
                out.append(('國道%s號' % r, 800))
            elif 1 <= num <= 99:
                out.append(('台%s線' % r, 750))
            elif 100 <= num <= 299:
                out.append(('縣道%s' % r, 550))
            continue
        for rx, fmt, w in _REF_RE:
            m = rx.match(r)
            if m:
                out.append((fmt(m), w)); break
    return out

ROAD_W = {1: 800, 2: 750, 3: 650, 4: 550, 5: 450, 6: 350, 7: 300, 8: 200, 9: 150}

# ---------------------------------------------------------------- 階段 2：建庫
def haversine_m(lat1, lon1, lat2, lon2):
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2
    return 12742000 * math.asin(math.sqrt(a))

# 內政部縣市代碼（鄉鎮 nat_ref 前 5 碼）。高雄市的 OSM 縣市邊界組不起來，靠這張表補縣市名。
COUNTY_CODE = {'63000': '台北市', '64000': '高雄市', '65000': '新北市', '66000': '台中市', '67000': '台南市',
               '68000': '桃園市', '10002': '宜蘭縣', '10004': '新竹縣', '10005': '苗栗縣', '10007': '彰化縣',
               '10008': '南投縣', '10009': '雲林縣', '10010': '嘉義縣', '10013': '屏東縣', '10014': '台東縣',
               '10015': '花蓮縣', '10016': '澎湖縣', '10017': '基隆市', '10018': '新竹市', '10020': '嘉義市',
               '09007': '連江縣', '09020': '金門縣'}

class AdminLookup:
    """縣市＋鄉鎮市區點在多邊形查詢。
    台灣 OSM：直轄市與縣、省轄市為 admin_level 4（另有一些小島誤標 4，以名稱結尾 市/縣 過濾）；
    直轄市的區為 7、縣的鄉鎮市為 8；鄉鎮的 nat_ref 前 5 碼是縣市代碼。"""
    def __init__(self, admins, log):
        import shapely
        from shapely.strtree import STRtree
        self.shapely = shapely
        self.counties, self.towns = [], []
        for lvl, tags, wkb in admins:
            name = norm_name(pick_name(tags)[0])
            if lvl == '4' and name[-1:] in ('市', '縣'):
                self.counties.append((name, shapely.from_wkb(wkb)))
            elif lvl in ('7', '8') and name[-1:] in ('區', '鄉', '鎮', '市'):
                code = (tags.get('nat_ref') or '')[:5]
                self.towns.append((name, COUNTY_CODE.get(code, ''), shapely.from_wkb(wkb)))
        log('  行政區：縣市 %d、鄉鎮市區 %d（其中無縣市代碼 %d）' % (len(self.counties), len(self.towns),
                                                           sum(1 for t in self.towns if not t[1])))
        self.ct = STRtree([g for _, g in self.counties])
        self.tt = STRtree([g for _, _, g in self.towns])

    def lookup(self, lats, lons):
        """回傳每點 (縣市, 鄉鎮) 名稱，查不到為空字串。"""
        import numpy as np
        pts = self.shapely.points(np.array(lons), np.array(lats))
        out = [['', ''] for _ in range(len(lats))]
        pi, gi = self.tt.query(pts, predicate='within')
        for a, b in zip(pi.tolist(), gi.tolist()):
            if not out[a][1]:
                out[a][1] = self.towns[b][0]; out[a][0] = self.towns[b][1]
        pi, gi = self.ct.query(pts, predicate='within')
        for a, b in zip(pi.tolist(), gi.tolist()):
            if not out[a][0]: out[a][0] = self.counties[b][0]
        return [tuple(x) for x in out]

def make_sub(county, town):
    if town and county and not town.startswith(county):
        return county + town
    return town or county

def overture_branch_fill(rows, ov_path, log, radius=50.0):
    """沒有 branch 的四大超商：在 Overture 同品牌 50 m 內找分店名（Overture 名稱已是「7-11 三村門市」型態），取最近一筆。"""
    import numpy as np
    from scipy.spatial import cKDTree
    con = sqlite3.connect('file:%s?mode=ro' % ov_path, uri=True)
    ov = {}
    for nm, lat, lon in con.execute("SELECT name, lat, lon FROM place WHERE "
                                    "(name LIKE '7%' OR name LIKE '全家%' OR name LIKE '萊爾富%' OR name LIKE 'OK%' OR name LIKE '統一超商%')"):
        z = zh_prefix(norm_name(nm))
        if z and ' ' in z[0]:
            ov.setdefault(z[0].split(' ', 1)[0], []).append((z[0], lat, lon))
    con.close()
    def xy(la, lo):
        return np.column_stack((np.array(lo) * (111320 * math.cos(math.radians(23.7))), np.array(la) * 110574))
    trees = {b: (cKDTree(xy([x[1] for x in v], [x[2] for x in v])), v) for b, v in ov.items()}
    filled = 0
    for base, (tree, v) in trees.items():
        idx = [k for k, r in enumerate(rows) if r[0] == base and r[7] in _BRAND_ZH]
        if not idx: continue
        d, i = tree.query(xy([rows[k][3] for k in idx], [rows[k][4] for k in idx]), distance_upper_bound=radius)
        for k, dd, ii in zip(idx, d, i):
            if ii < len(v):
                rows[k][0] = v[ii][0]; rows[k][8] = True; filled += 1
    log('  Overture 補分店名 %d 筆（候選：%s）' % (filled, {b: len(v[1]) for b, v in trees.items()}))
    return filled

class AddrNearest:
    """用 touror_addr.bin 找每個點 150 m 內最近門牌；沒有就退回 500 m 內最近路段代表點。"""
    R_NUM, R_ROAD = 150.0, 500.0
    def __init__(self, path, log):
        import numpy as np
        from array import array
        spec = importlib.util.spec_from_file_location('build_addr', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'build_addr.py'))
        ba = importlib.util.module_from_spec(spec); sys.modules['build_addr'] = ba; spec.loader.exec_module(ba)
        self.np = np; pk = ba.AddrPack(path); self.pk = pk
        t0 = time.time()
        la, lo, sid = array('i'), array('i'), array('i')
        f = [array('H') for _ in range(6)]
        self.sname, self.sdist = [], []
        rla, rlo = [], []
        for i in range(pk.ns):
            st = pk.street(i)
            self.sname.append(st['name']); self.sdist.append(pk.dists[st['dist']][1])
            rla.append(st['alat'] / ba.Q); rlo.append(st['alon'] / ba.Q)
            if st['cnt']:
                for r in pk.records(st):
                    for j in range(6): f[j].append(r[j])
                    la.append(round(r[6] * ba.Q)); lo.append(round(r[7] * ba.Q)); sid.append(i)
        self.f = [np.frombuffer(a, dtype=np.uint16) for a in f]
        self.sid = np.frombuffer(sid, dtype=np.int32)
        self.q = ba.Q
        from scipy.spatial import cKDTree
        self.tree = cKDTree(self._xy(np.frombuffer(la, dtype=np.int32) / ba.Q, np.frombuffer(lo, dtype=np.int32) / ba.Q))
        self.rtree = cKDTree(self._xy(np.array(rla), np.array(rlo)))
        log('  門牌包載入：%d 點、%d 路段，%.0fs' % (len(self.sid), pk.ns, time.time() - t0))
    def _xy(self, lat, lon):
        np = self.np
        return np.column_stack((lon * (111320 * math.cos(math.radians(23.7))), lat * 110574))
    def lookup(self, lats, lons, subs=None):
        """回傳每點的地址字串（或 None）與來源（'n' 門牌、'r' 路段、''）。"""
        np = self.np
        xy = self._xy(np.array(lats), np.array(lons))
        d, i = self.tree.query(xy, k=6, distance_upper_bound=self.R_NUM, workers=4)
        d2, i2 = self.rtree.query(xy, distance_upper_bound=self.R_ROAD, workers=4)
        n = len(self.sid); out = []
        for k in range(len(lats)):
            cand = [int(x) for x in i[k] if x < n]
            if cand:
                # 優先取「行政區與 POI 副標一致」的最近門牌（避免邊界點寫成隔壁區）；都不一致才取最近
                sub = subs[k] if subs else ''
                same = [x for x in cand if sub and sub.endswith(self.sdist[int(self.sid[x])])]
                j = (same or cand)[0]; s = int(self.sid[j])
                road = self.sname[s]
                lane, ls, al, als, main, sub = (int(a[j]) for a in self.f)
                if lane: road += '%d%s巷' % (lane, '之%d' % ls if ls else '')
                if al: road += '%d%s弄' % (al, '之%d' % als if als else '')
                if main: road += '%d%s號' % (main, '之%d' % sub if sub else '')
                out.append((self.sdist[s] + road, 'n'))
            elif i2[k] < len(self.sname):
                s = int(i2[k]); out.append((self.sdist[s] + self.sname[s], 'r'))
            else:
                out.append((None, ''))
        return out

def fts_text(r):
    """FTS 欄位：標題 grams ＋ 各額外字串各自的 grams（空白相隔；每段獨立算，避免跨欄位 bigram）。"""
    parts = [grams(r[0])]
    if len(r) > 9:
        parts.extend(grams(x) for x in r[9])
    return ' '.join(p for p in parts if p)

def build(raw, out, log, branch_labels=False, addr=None, overture=None):
    t0 = time.time()
    adm = AdminLookup(raw['admins'], log)
    rows = []  # (name, kind, weight, lat, lon, sub[, note, brand, renamed])
    stat = {'poi_in': len(raw['pois']), 'poi_drop_kind': 0, 'zh_hant_used': 0}
    for t, lat, lon, src in raw['pois']:
        c = classify(t)
        if not c:
            stat['poi_drop_kind'] += 1; continue
        nm, alt_used = pick_name(t)
        if alt_used: stat['zh_hant_used'] += 1
        nm = norm_name(nm)
        if not nm or not grams(nm): continue
        # OSM 車站名常只寫「台中」「市政府」，使用者會打「台中車站」「市政府站」：補上站名尾字
        if t.get('railway') in ('station', 'halt') and not nm.endswith('站'):
            nm += '站' if t.get('station') in ('subway', 'light_rail', 'monorail') else '車站'
            stat['station_suffix'] = stat.get('station_suffix', 0) + 1
        w = c[1] + (50 if t.get('wikidata') else 0)
        orig = nm
        nm, note, brand, renamed = chain_display(t, nm)
        # 列：[名稱, kind, 權重, lat, lon, sub, note(建庫後段由門牌包填), 品牌, 是否改名, FTS 額外字串]
        rows.append([nm, c[0], w, lat, lon, None, None, brand, renamed, fts_extras(t, orig, nm)])
    if overture:
        overture_branch_fill(rows, overture, log)
    log('  POI 分類後 %d（丟棄 %d、改用繁體名 %d）' % (len(rows), stat['poi_drop_kind'], stat['zh_hant_used']))

    # 道路：先算每段所在行政區，再以（名稱, 縣市, 鄉鎮）合併
    rd = raw['roads']
    sub_rd = adm.lookup([r[3] for r in rd], [r[4] for r in rd])
    groups = {}
    for (nm, ref, rank, lat, lon, ntags), (cty, twn) in zip(rd, sub_rd):
        names = []
        if nm:
            n2, alt_used = pick_name(ntags) if ntags else (nm, False)
            names.append((norm_name(n2), ROAD_W[rank]))
        if ref:
            names.extend(ref_routes(ref, rank))
        for n2, w in names:
            if not n2 or not grams(n2): continue
            key = (n2, cty, twn) if (cty or twn) else (n2, round(lat, 1), round(lon, 1))
            g = groups.get(key)
            if g is None:
                groups[key] = [w, [lat], [lon], cty, twn]
            else:
                if w > g[0]: g[0] = w
                g[1].append(lat); g[2].append(lon)
    road_rows = []
    for key, (w, la, lo, cty, twn) in groups.items():
        ml = sorted(la)[len(la) // 2]; mo = sorted(lo)[len(lo) // 2]
        # 代表點：最接近中位點的實際路段點（一定在路上）
        best = min(range(len(la)), key=lambda i: (la[i] - ml) ** 2 + ((lo[i] - mo) * 0.91) ** 2)
        road_rows.append([key[0], 'road', w, la[best], lo[best], make_sub(cty, twn)])
    log('  道路段 %d → 合併後 %d 筆' % (len(rd), len(road_rows)))

    # 行政區本身也當 place（若附近 10 km 內沒有同名 place）
    place_idx = {}
    for r in rows:
        if r[1] == 'place': place_idx.setdefault(r[0], []).append((r[3], r[4]))
    admin_rows = []
    for name, w, g in [(n, 1000, g) for n, g in adm.counties] + [(n, 900, g) for n, _, g in adm.towns]:
        p = g.representative_point()
        lat, lon = p.y, p.x
        if any(haversine_m(lat, lon, a, b) < 10000 for a, b in place_idx.get(name, [])):
            continue
        admin_rows.append([name, 'place', w, lat, lon, 'COUNTY' if w == 900 else ''])
    log('  行政區補 place %d 筆' % len(admin_rows))

    # POI／place 的副標：所在縣市＋鄉鎮；行政區外（海上、外島缺邊界）用最近的 place
    pr = rows + admin_rows
    subs = adm.lookup([r[3] for r in pr], [r[4] for r in pr])
    for r, (cty, twn) in zip(pr, subs):
        if r[5] == 'COUNTY':
            r[5] = cty if cty != r[0] else ''
        elif r[5] is None:
            if r[0] == cty: r[5] = ''
            elif r[0] == twn: r[5] = cty
            else: r[5] = make_sub(cty, twn)
    # 最近 place 補洞（格網索引）
    big = [r for r in pr if r[1] == 'place' and r[2] >= 450]
    grid = {}
    for r in big:
        grid.setdefault((int(r[3] * 10), int(r[4] * 10)), []).append(r)
    nofill = 0
    for r in pr:
        if r[5]: continue
        if r[1] == 'place' and r[2] >= 1000: continue
        gy, gx = int(r[3] * 10), int(r[4] * 10)
        best, bd = None, 30000
        for dy in (-2, -1, 0, 1, 2):
            for dx in (-2, -1, 0, 1, 2):
                for c in grid.get((gy + dy, gx + dx), ()):
                    if c is r or c[0] == r[0]: continue
                    d = haversine_m(r[3], r[4], c[3], c[4])
                    if d < bd: best, bd = c, d
        if best:
            r[5] = (best[5] + best[0]) if best[5] and not best[0].startswith(best[5]) else best[0]
            if len(r[5]) > 30: r[5] = best[0]
        else:
            nofill += 1
    log('  副標未能填 %d 筆' % nofill)

    # 去重：同名同 kind 50 m 內合併，保留權重高者
    bykey = {}
    for r in pr:
        bykey.setdefault((r[0], r[1]), []).append(r)
    final, dup = [], 0
    for key, lst in bykey.items():
        if len(lst) == 1:
            final.append(lst[0]); continue
        # 同權重且同名時優先保留有地址的原始點，不移植別筆的地址。
        lst.sort(key=lambda r: (-r[2], not (len(r) > 6 and r[6])))
        kept = []
        for r in lst:
            if any(haversine_m(r[3], r[4], k[3], k[4]) < 50 for k in kept):
                dup += 1; continue
            kept.append(r)
        final.extend(kept)
    final.extend(road_rows)
    addr_stat = {}
    if addr:
        an = AddrNearest(addr, log)
        idx = [k for k, r in enumerate(final) if len(r) > 9]
        res = an.lookup([final[k][3] for k in idx], [final[k][4] for k in idx], [final[k][5] or '' for k in idx])
        for k, (txt, src) in zip(idx, res):
            final[k][6] = txt
            addr_stat[src] = addr_stat.get(src, 0) + 1
        del an
        log('  地址 note：門牌 %(n)d、路段 %(r)d、空 %(e)d' % {'n': addr_stat.get('n', 0), 'r': addr_stat.get('r', 0), 'e': addr_stat.get('', 0)})
    log('  去重移除 %d，最終 %d 筆' % (dup, len(final)))

    # 寫 SQLite
    tmp = out + '.tmp'
    if os.path.exists(tmp): os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute('PRAGMA journal_mode=OFF'); con.execute('PRAGMA synchronous=OFF')
    con.execute('CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, sub TEXT, '
                'lat REAL NOT NULL, lon REAL NOT NULL, weight INTEGER NOT NULL' + (', note TEXT)' if branch_labels else ')'))
    con.execute("CREATE VIRTUAL TABLE search USING fts5(grams, tokenize='unicode61')")
    con.execute('CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT)')
    final.sort(key=lambda r: (-r[2], r[0]))
    place_rows = ((i + 1, r[0], r[1], r[5] or None, round(r[3], 6), round(r[4], 6), int(r[2]))
                  + (((r[6] if len(r) > 6 else None),) if branch_labels else ()) for i, r in enumerate(final))
    stmt = ('INSERT INTO place(id,name,kind,sub,lat,lon,weight,note) VALUES(?,?,?,?,?,?,?,?)' if branch_labels
            else 'INSERT INTO place(id,name,kind,sub,lat,lon,weight) VALUES(?,?,?,?,?,?,?)')
    con.executemany(stmt, place_rows)
    con.executemany('INSERT INTO search(rowid, grams) VALUES(?,?)', ((i + 1, fts_text(r)) for i, r in enumerate(final)))
    con.execute("INSERT INTO search(search) VALUES('optimize')")
    meta = {
        'version': str(SCHEMA_VERSION),
        'built_at': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'source': 'OpenStreetMap %s %s' % (raw['pbf_name'], raw['pbf_ts']),
        'attribution': '© OpenStreetMap contributors, ODbL',
        'row_count': str(len(final)),
    }
    con.executemany('INSERT INTO meta VALUES(?,?)', meta.items())
    con.execute('PRAGMA user_version=%d' % SCHEMA_VERSION)
    con.commit()
    con.execute('VACUUM')
    con.close()
    if os.path.exists(out): os.remove(out)
    os.replace(tmp, out)
    kinds = {}
    for r in final: kinds[r[1]] = kinds.get(r[1], 0) + 1
    nosub = sum(1 for r in final if not r[5])
    log('建庫完成 %.0fs，%d 筆，%.1f MB，kind 統計 %s，無副標 %d' % (time.time() - t0, len(final), os.path.getsize(out) / 1024 ** 2,
                                                       json.dumps(kinds, ensure_ascii=False), nosub))
    result = {'rows': len(final), 'kinds': kinds, 'nosub': nosub, 'dup': dup, 'build_sec': time.time() - t0}
    if branch_labels:
        counts, samples = {}, []
        for r in final:
            if len(r) <= 6 or not r[7]:
                continue
            c = counts.setdefault(r[7], {'total': 0, 'renamed': 0, 'with_note': 0})
            c['total'] += 1
            c['renamed'] += int(r[8])
            c['with_note'] += int(bool(r[6]))
            if r[8] and r[0] not in samples and len(samples) < 20:
                samples.append(r[0])
        result['chain_counts'] = counts
        result['branch_samples'] = samples
    return result

# ---------------------------------------------------------------- 驗證
VERIFY_QUERIES = ['谷關', '埔里', '逢甲夜市', '中山路', '台8線', '合歡山', '日月潭', '臺中車站', '台中車站', '墾丁', '阿里山',
                  '谷关', '國道3號', '武嶺', '全家', '路', '7-ELEVEN']  # 後 6 個為額外壓力／邊界測試
REF_LAT, REF_LON = 24.15, 120.67  # 驗證排序用的假設目前位置（台中）

VERIFY_SQL = """WITH f AS MATERIALIZED (SELECT rowid FROM search WHERE search MATCH ? LIMIT 2000)
SELECT p.name, p.kind, p.sub, round(p.lat,4), round(p.lon,4), p.weight FROM f JOIN place p ON p.id = f.rowid
ORDER BY (p.name = ?) DESC, p.weight - ((p.lat-?)*(p.lat-?) + (p.lon-?)*(p.lon-?)) * 20 DESC LIMIT 5"""

def verify(db, log, md_out=None):
    con = sqlite3.connect(db)
    lines = ['| 查詢 | MATCH | 耗時 ms（冷／熱中位） | 前 5 筆（名稱／kind／副標） |', '|---|---|---|---|']
    worst = 0
    for q in VERIFY_QUERIES:
        m = fts_query(q); nq = norm_name(q)
        times = []
        for i in range(6):
            t = time.perf_counter()
            r = con.execute(VERIFY_SQL, (m, nq, REF_LAT, REF_LAT, REF_LON, REF_LON)).fetchall()
            times.append((time.perf_counter() - t) * 1000)
        hot = sorted(times[1:])[2]
        worst = max(worst, hot)
        top = '<br>'.join('%s／%s／%s' % (x[0], x[1], x[2] or '') for x in r) or '（無）'
        lines.append('| %s | `%s` | %.1f／%.1f | %s |' % (q, m, times[0], hot, top))
        log('%s %s cold %.1f hot %.1f ms → %s' % (q, m, times[0], hot, [(x[0], x[1], x[2]) for x in r]))
    con.close()
    md = '\n'.join(lines)
    if md_out:
        with open(md_out, 'w', encoding='utf-8') as f: f.write(md + '\n')
    return md, worst

# ---------------------------------------------------------------- 主程式
def main():
    ap = argparse.ArgumentParser(description='Touror 離線搜尋資料庫建置')
    ap.add_argument('--pbf', help='OSM PBF 路徑（例如 taiwan-latest.osm.pbf）')
    ap.add_argument('--out', help='輸出 SQLite 路徑（touror_places.sqlite）')
    ap.add_argument('--work', help='暫存資料夾（節點索引與 raw.pkl），預設為輸出旁的 _work')
    ap.add_argument('--reuse-raw', action='store_true', help='沿用 work 內的 raw.pkl，不重掃 PBF')
    ap.add_argument('--branch-labels', action='store_true', help='v2 永遠啟用（保留參數相容）')
    ap.add_argument('--overture', help='touror_places_overture.sqlite：沒有 branch 的超商用 Overture 50 m 內同品牌分店名補標題')
    ap.add_argument('--addr', help='touror_addr.bin 路徑（用來填 note 地址欄）')
    ap.add_argument('--verify', metavar='DB', help='只對既有資料庫跑驗證查詢')
    ap.add_argument('--verify-md', metavar='MD', help='驗證結果表格輸出路徑')
    ap.add_argument('--scan-only', action='store_true', help=argparse.SUPPRESS)  # 內部用：子程序只做階段 1
    a = ap.parse_args()
    def log(s):
        print(time.strftime('%H:%M:%S'), s, flush=True)
    if a.verify:
        verify(a.verify, log, a.verify_md); return
    if not a.out or (not a.pbf and not a.reuse_raw):
        ap.error('需要 --pbf 與 --out')
    a.branch_labels = True
    lower_priority(); start_watchdog(log)
    work = a.work or os.path.join(os.path.dirname(os.path.abspath(a.out)), '_work_v2')
    os.makedirs(work, exist_ok=True)
    rawp = os.path.join(work, 'raw.pkl')
    T = time.time()
    if a.scan_only:
        raw = scan(a.pbf, work, log, a.branch_labels)
        m = mem_info()
        raw['scan_peak_private_gb'] = round(m[1] / 1024 ** 3, 2) if m else None
        raw['scan_peak_workingset_gb'] = round(m[2] / 1024 ** 3, 2) if m else None
        with open(rawp, 'wb') as f: pickle.dump(raw, f, protocol=pickle.HIGHEST_PROTOCOL)
        return
    if not (a.reuse_raw and os.path.exists(rawp)):
        # 掃描放子程序：pyosmium 結束前不放開節點索引檔（Windows 鎖檔），子程序結束後才能刪，記憶體也一併歸還
        import subprocess
        r = subprocess.run([sys.executable, '-I', os.path.abspath(__file__), '--scan-only', '--pbf', a.pbf,
                            '--out', a.out, '--work', work] + (['--branch-labels'] if a.branch_labels else []))
        if r.returncode != 0:
            log('掃描子程序失敗，結束碼 %d' % r.returncode); sys.exit(r.returncode)
    else:
        log('沿用 %s' % rawp)
    with open(rawp, 'rb') as f: raw = pickle.load(f)
    if a.branch_labels and not raw.get('branch_labels'):
        ap.error('這份 raw.pkl 不含分店與地址標籤，請使用 --branch-labels 重掃')
    idx = os.path.join(work, 'nodes.idx')
    if os.path.exists(idx):
        try: os.remove(idx); log('  已刪除節點索引暫存檔')
        except Exception as e: log('  節點索引暫存檔無法刪除，請手動刪 %s：%s' % (idx, e))
    res = build(raw, a.out, log, a.branch_labels, a.addr, a.overture)
    m = mem_info()
    res.update({'scan_sec': raw.get('scan_sec'), 'total_sec': time.time() - T,
                'peak_private_gb': round(m[1] / 1024 ** 3, 2) if m else None,
                'peak_workingset_gb': round(m[2] / 1024 ** 3, 2) if m else None,
                'scan_peak_private_gb': raw.get('scan_peak_private_gb'),
                'scan_peak_workingset_gb': raw.get('scan_peak_workingset_gb'),
                'size_mb': round(os.path.getsize(a.out) / 1024 ** 2, 1), 'scan_stats': raw.get('stats')})
    with open(os.path.join(work, 'build_stats.json'), 'w', encoding='utf-8') as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    log('總計 %.0fs；掃描子程序峰值私有記憶體 %s GB／工作集 %s GB；建庫峰值私有 %s GB／工作集 %s GB' % (
        res['total_sec'], res['scan_peak_private_gb'], res['scan_peak_workingset_gb'],
        res['peak_private_gb'], res['peak_workingset_gb']))

if __name__ == '__main__':
    main()
