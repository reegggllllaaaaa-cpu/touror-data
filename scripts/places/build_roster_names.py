# -*- coding: utf-8 -*-
"""名稱補全第二輪：官方名冊補名（全量、不抽樣、不編造）。接在 build_name_completion.py 之後。

輸入（唯讀）：
  --main / --ov   第一輪輸出（預設 候選-名稱補全r2/；build_name_completion.py --outdir 候選-名稱補全r2）
  --rosters       名冊資料夾（7-11_emap.csv、OK_okmart.csv，fetch_chain_rosters.py）
  --gcis          商工登記 32086（全家、萊爾富）
  --offdir        路網來源/名冊2/（moe_*.csv 教育部統計處學校名錄、nhi.csv 健保特約醫事機構、temple.xml／church.xml 內政部全國宗教資訊）
  --addr          touror_addr.bin（地址轉座標）
  --gov           touror_places_gov.sqlite（只供合併去重口徑統計）
輸出：--outdir（預設 候選-名冊補名/）：兩個新庫、前後對照.csv、連鎖前後比例.csv、官方名冊配對.csv、新增.csv、疑似歇業.csv、summary.json。

A 連鎖放寬配對：品牌名／縮寫的超商 POI，在名冊（有門市名者）250 m 內、路名相同（台臺、之-、段數字正規化）、門牌號差 <=4 者，
  一對一（先門牌差小、再距離近）。名冊店若庫內已有同名門市（250 m 內）→ 此筆是重複點，移除；否則改名。
  庫內互補（四品牌，含沒有名冊者）：同品牌完整門市名 POI 60 m 內、路名相同且門牌差 <=6（任一邊無門牌時 30 m 內）→ 品牌名筆為重複點，移除。
B 官方名冊（學校、醫事機構、寺廟、教會）：地址轉座標（教會、寺廟用名冊自帶 WGS84），同類 POI 150 m 內名稱相似 → 命中；
  POI 名稱是純英文／通用名／品牌名（或是較短的前綴）才改成名冊全稱；未命中且 40 m 內沒有任何同類 POI、300 m 內沒有相似名者 → 新增（只主庫）；
  醫事機構終止合約／歇業者：列疑似歇業（不刪）。
重跑：<overture_venv>\\Scripts\\python.exe -I build_roster_names.py
"""
import argparse, collections, csv, datetime, importlib.util, json, os, re, shutil, sqlite3, statistics, sys, time
import xml.etree.ElementTree as ET
import numpy as np
from scipy.spatial import cKDTree

HERE = os.path.dirname(os.path.abspath(__file__))


def load(name, fn):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, fn))
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m; spec.loader.exec_module(m)
    return m


cb = load('build_chain_branches', 'build_chain_branches.py')
bn = load('build_name_completion', 'build_name_completion.py')
nz = bn.nz
CN = {'一': 1, '二': 2, '三': 4 - 1, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
_PAREN_RE = bn._PAREN
STRIP_PAREN = re.compile(r'[（(][^()（）]*[路街道巷弄段村里號][^()（）]*[）)]\s*$')
ANYPAREN = re.compile(r'[（(][^()（）]*[）)]')
RN = re.compile(r'^(.+?)\s(\d+)(?: 之 (\d+))? 號$')


def log(s):
    print(time.strftime('%H:%M:%S'), s, flush=True)


def xy(lat, lon):
    return cb.xy(lat, lon)


def normroad(r):
    r = nz(r).replace('之', '-').replace(' ', '')
    r = re.sub(r'([一二三四五六七八九十])段', lambda m: '%d段' % CN[m.group(1)], r)
    return r


def road_num(text):
    """回傳 (正規化路名, 門牌號 int|None)。"""
    if not text:
        return None, None
    t = re.sub(r'\[[^\]]*\]', '', nz(text))
    t = re.sub(r'\d+鄰', '', t)
    d, li, road = bn.parse_addr(t)
    m = RN.match(road)
    if m:
        return normroad(m.group(1)), int(m.group(2))
    return (normroad(road) if road else None), None


def addr_text(p):
    if p.get('note'):
        return p['note']
    m = re.search(r'[（(]([^()（）]*)[）)]\s*$', p['name'])
    return m.group(1) if m and re.search(r'[路街道巷弄段村里號]', m.group(1)) else ''


def base_name(n):
    return STRIP_PAREN.sub('', n).strip()


PREFIX = re.compile(r'^(國立|市立|縣立|區立|私立|公立|財團法人|社團法人|學校法人|[一-鿿]{2}[縣市]立|[一-鿿]{2}[縣市])')
NOISE = re.compile(r'[\s\-－‐·．.,，、()（）\[\]]+')


def core(n):
    s = nz(ANYPAREN.sub('', n)).lower()
    s = PREFIX.sub('', s)
    return NOISE.sub('', s)


def bigrams(s):
    return {s[i:i + 2] for i in range(len(s) - 1)} or {s}


def sim(a, b):
    ca, cb_ = core(a), core(b)
    if not ca or not cb_:
        return 0
    if ca == cb_:
        return 3
    if min(len(ca), len(cb_)) >= 3 and (ca in cb_ or cb_ in ca):
        return 2
    A, B = bigrams(ca), bigrams(cb_)
    return 1 if len(A & B) / len(A | B) >= 0.6 else 0


END = re.compile(r'(國小|國中|高中|高職|高商|高工|大學|學院|幼兒園|幼稚園|診所|醫院|藥局|衛生所|福德祠|土地公|宮|廟|寺|祠|堂|教會|禮拜堂|殿|院|閣)$')


def end_class(n):
    m = END.search(core(n))
    return m.group(1) if m else None


EN_MAP = [(r'elementary|primary', r'國小|國民小學'), (r'junior', r'國中|國民中學'), (r'high|senior', r'高中|高職|高商|高工|中學|附中'),
          (r'kindergarten|preschool', r'幼兒園|幼稚園'), (r'university|college|institute', r'大學|學院|科技'),
          (r'clinic', r'診所'), (r'hospital', r'醫院'), (r'pharmacy|drugstore', r'藥局'), (r'dental', r'牙'),
          (r'temple|shrine', r'宮|廟|寺|殿|堂|祠|巖|岩|壇|院'), (r'church|chapel|cathedral', r'教會|禮拜堂|堂')]
EN_REJECT = re.compile(r'at|department|esl|cram|tutor|lesson|class|center|centre|academy of', re.I)


def en_ok(en, zh, cls):
    if EN_REJECT.search(en):
        return False
    if re.search('幼兒園|幼稚園', zh) and not re.search('kindergarten|preschool|kinder', en, re.I):
        return False
    hits = [z for k, z in EN_MAP if re.search(k, en, re.I)]
    if not hits:
        return False
    # 英文出現多種關鍵字時（Elementary and Junior High）要求名冊名稱至少吻合其中之一；且不能同時有幼兒園以外的矛盾
    return any(re.search(z, zh) for z in hits)


def is_bad(n):
    b = base_name(n)
    return (not bn.has_cjk(b)) or bn.is_generic(b) or bn.is_brand(b)


# ---------------------------------------------------------------- 讀庫
def load_db(path, tag):
    c = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)
    cols = [r[1] for r in c.execute('PRAGMA table_info(place)')]
    rows = c.execute('SELECT id,name,kind,sub,lat,lon,weight,%s FROM place' % ('note' if 'note' in cols else 'NULL')).fetchall()
    c.close()
    return [dict(db=tag, id=r[0], name=r[1], kind=r[2], sub=r[3] or '', lat=r[4], lon=r[5], w=r[6], note=r[7], new=None, newnote=None, drop=None) for r in rows]


# ---------------------------------------------------------------- 官方名冊
def read_csv(p):
    return list(csv.DictReader(open(p, encoding='utf-8-sig')))


def official_entries(offdir, geo, log_):
    """回傳 entries（dict: src, cls, name, title, addr, lat, lon, prec, closed, sub, note）與取得失敗清單。"""
    E, bad = [], []

    def add(src, cls, name, title0, addr, lat=None, lon=None, closed=False, sub=''):
        prec = 'roster' if lat is not None else ''
        if lat is None:
            addr2 = re.sub(r'\[[^\]]*\]', '', nz(addr)); addr2 = re.sub(r'\d+鄰', '', addr2)
            la, lo, pr, key, why = geo.locate(addr2)
            if la is None:
                bad.append(dict(src=src, name=name, addr=addr, reason=why)); return
            lat, lon, prec = la, lo, pr
            sub = ((key[0] or '') + (key[1] or '')) if key else sub
        title = REGION.sub('', title0) if cls == 'temple' else (LEGAL.sub('', title0) if cls == 'school' else title0)
        E.append(dict(src=src, cls=cls, name=name, title=title, addr=nz(addr), lat=lat, lon=lon, prec=prec, closed=closed, sub=sub))

    def stripp(n, keep=False):
        return n if keep else re.sub(r'^(國立|市立|縣立|區立|私立|公立)(?=.{2,}(國小|國中|高中|高職|高商|高工|幼兒園|學校))', '', nz(n) and n)

    for f, label, keep in (('e1', '教育部國小名錄', False), ('j1', '教育部國中名錄', False), ('high', '教育部高中名錄', False),
                           ('highV', '教育部進修學校名錄', False), ('u1', '教育部大專校院名錄', True), ('k1', '教育部幼兒園名錄', False)):
        p = os.path.join(offdir, 'moe_%s.csv' % f)
        if not os.path.exists(p):
            continue
        rows = [r for r in read_csv(p)]
        yrs = [r['學年度'] for r in rows if r['學年度'].isdigit()]
        mx = max(yrs, key=int)
        latest = {}
        for r in rows:
            if r['學年度'] == mx:
                latest[r['代碼']] = r
        n0 = len(E)
        for r in latest.values():
            name = r['學校名稱'].strip()
            if not name or r.get('備註', '').strip():
                continue
            add('%s（%s學年）' % (label, mx), 'school', name, stripp(name, keep), r['地址'])
        log_('%s：%d 間（%s學年）→ 可定位 %d' % (label, len(latest), mx, len(E) - n0))
    p = os.path.join(offdir, 'nhi.csv')
    if os.path.exists(p):
        n0 = len(E); tot = 0
        for r in read_csv(p):
            name = r['醫事機構名稱'].strip(); tot += 1
            if not name:
                continue
            add('健保特約醫事機構 info.nhi.gov.tw（data.gov.tw/dataset/168341）', 'medical', name, name, r['機構地址'].split('（')[0], closed=bool(r['終止合約或歇業日期'].strip()))
        log_('健保特約醫事機構：%d 筆 → 可定位 %d' % (tot, len(E) - n0))
    for fn, tag, nm, cls in (('temple.xml', '內政部全國宗教資訊-寺廟 religion.moi.gov.tw', '寺廟名稱', 'temple'), ('church.xml', '內政部全國宗教資訊-教會 religion.moi.gov.tw', '教會名稱', 'temple')):
        p = os.path.join(offdir, fn)
        if not os.path.exists(p):
            continue
        n0 = len(E); tot = 0
        for el in ET.parse(p).getroot():
            d = {c.tag: (c.text or '').strip() for c in el}
            name = d.get(nm, ''); tot += 1
            if not name:
                continue
            lat = lon = None
            try:
                lon, lat = float(d.get('WGS84X', '')), float(d.get('WGS84Y', ''))
                if not (21.5 < lat < 26.5 and 118 < lon < 123): lat = lon = None
            except ValueError:
                lat = lon = None
            add(tag, cls, name, name, d.get('地址', ''), lat, lon)
        log_('%s：%d 筆 → 可定位 %d' % (tag, tot, len(E) - n0))
    return E, bad


EN = {'school': re.compile(r'school|academy|elementary|junior|senior|kindergarten|preschool|university|college', re.I),
      'medical': re.compile(r'clinic|hospital|pharmacy|dental|medical|drugstore', re.I),
      'temple': re.compile(r'temple|church|shrine|chapel|cathedral', re.I)}
LEGAL = re.compile(r'^(財團法人|社團法人|學校法人)([一-鿿]{2}[縣市])?(?=.{3,})')
REGION = re.compile(r'^(財團法人|社團法人)?(台灣省|臺灣省)?(?:[一-鿿]{2}[縣市](?:[一-鿿]{1,3}[鄉鎮市區])?)?(?=[一-鿿]{3,})')
KINDS = {'school': {'service', 'poi', 'scenic'}, 'medical': {'service', 'shop', 'poi'}, 'temple': {'worship', 'scenic', 'poi'}}


def main():
    ap = argparse.ArgumentParser()
    r1 = os.path.join(HERE, '候選-名稱補全r2')
    ap.add_argument('--main', default=os.path.join(r1, 'touror_places.sqlite'))
    ap.add_argument('--ov', default=os.path.join(r1, 'touror_places_overture.sqlite'))
    ap.add_argument('--rosters', default=os.path.join(HERE, '候選-分店名', '名冊'))
    ap.add_argument('--gcis', default=os.path.join(HERE, '真值', '32086.csv'))
    ap.add_argument('--offdir', default=os.path.join(HERE, '路網來源', '名冊2'))
    ap.add_argument('--addr', default=os.path.join(HERE, 'touror_addr.bin'))
    ap.add_argument('--gov', default=os.path.join(HERE, 'touror_places_gov.sqlite'))
    ap.add_argument('--outdir', default=os.path.join(HERE, '候選-名冊補名'))
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    M, O = load_db(a.main, 'main'), load_db(a.ov, 'ov')
    ALL = M + O
    gov = [(n, {'旅宿': 'lodging', '景點': 'scenic'}.get(k, k), la, lo) for n, k, la, lo in sqlite3.connect(a.gov).execute('SELECT name,kind,lat,lon FROM place')]
    qrows = lambda L: [(p['name'], p['kind'], p['lat'], p['lon']) for p in L]
    before_q = {'OSM主庫': bn.quality([(p['name'], p['kind']) for p in M]), 'Overture': bn.quality([(p['name'], p['kind']) for p in O])}
    bq, nb = bn.merged_quality([(0, qrows(M)), (1, gov), (2, qrows(O))]); before_q['合併去重'] = bq
    log('載入 主庫 %d、Overture %d' % (len(M), len(O)))
    geo = cb.Geo(a.addr)
    changes, adds, closed_list, matchrows = [], [], [], []

    # ================= A 連鎖
    for p in ALL:
        p['brand'] = p['bk'] = None
        if p['kind'] == 'shop':
            b, k = cb.classify_brand(base_name(p['name']))
            if b:
                p['brand'], p['bk'] = b, k
    def chain_stats():
        c = collections.Counter()
        for p in ALL:
            if p['brand'] and not p['drop']:
                c[p['brand'], 'total'] += 1
                if (p['new'] and p.get('newfull')) or (not p['new'] and p['bk'] == 'full'):
                    c[p['brand'], 'full'] += 1
        return c
    chain_before = chain_stats()
    entries, _bad = cb.read_rosters(a.rosters, a.gcis, geo, log)
    for e in entries:
        e['road'], e['num'] = road_num(e['addr'])
    title_idx = collections.defaultdict(list)   # 標題 → POI
    for p in ALL:
        if p['brand'] and p['bk'] == 'full':
            title_idx[nz(p['name'])].append(p)
    renamed_n = collections.Counter(); dedup_n = collections.Counter(); inDB_n = collections.Counter()
    for b in cb.BRANDS:
        P = [p for p in ALL if p['brand'] == b and p['bk'] != 'full']
        for p in P:
            p['road'], p['num'] = road_num(addr_text(p))
        EE = [e for e in entries if e['brand'] == b and e['label'] and e['lat'] is not None]
        if P and EE:
            tree = cKDTree(xy([e['lat'] for e in EE], [e['lon'] for e in EE]))
            cand = []
            for pi, p in enumerate(P):
                if not p['road'] or p['num'] is None:
                    continue
                for j in tree.query_ball_point(xy([p['lat']], [p['lon']])[0], 250):
                    e = EE[j]
                    if e['road'] == p['road'] and e['num'] is not None and abs(e['num'] - p['num']) <= 4:
                        d = float(np.hypot(*(xy([p['lat']], [p['lon']])[0] - tree.data[j])))
                        cand.append((abs(e['num'] - p['num']), d, pi, j))
            cand.sort()
            usedp, usede = set(), set()
            for df, d, pi, j in cand:
                if pi in usedp or j in usede:
                    continue
                usedp.add(pi); usede.add(j)
                p, e = P[pi], EE[j]
                new = cb.title_of(b, e['label'])
                dup = [q for q in title_idx.get(new, []) if q is not p and not q['drop'] and np.hypot(*(xy([q['lat']], [q['lon']])[0] - xy([p['lat']], [p['lon']])[0])) <= 250]
                if dup:
                    p['drop'] = '連鎖放寬：同名門市「%s」已在庫（同路名、門牌差%d、%dm）' % (new, df, d); dedup_n[b] += 1
                    changes.append((p['db'], p['id'], p['kind'], p['name'], '', '連鎖放寬配對-重複移除', p['lat'], p['lon']))
                else:
                    p['new'] = new; p['newfull'] = True; p['note_if_empty'] = e['addr']; renamed_n[b] += 1
                    title_idx[new].append(p)
                    changes.append((p['db'], p['id'], p['kind'], p['name'], new, '連鎖放寬配對-改名（≤250m、同路、門牌差%d、%dm）' % (df, d), p['lat'], p['lon']))
        # 庫內互補
        Q = [p for p in ALL if p['brand'] == b and ((p['bk'] == 'full' and not p['drop']) or p['new'])]
        rest = [p for p in P if not p['new'] and not p['drop']]
        if rest and Q:
            qtree = cKDTree(xy([q['lat'] for q in Q], [q['lon'] for q in Q]))
            for q in Q:
                q.setdefault('road', None)
                q['road'], q['num'] = road_num(addr_text(q)) if not q.get('road') else (q['road'], q.get('num'))
            cand = []
            for pi, p in enumerate(rest):
                pxy = xy([p['lat']], [p['lon']])[0]
                for j in qtree.query_ball_point(pxy, 60):
                    q = Q[j]; d = float(np.hypot(*(pxy - qtree.data[j])))
                    if p['road'] and q['road'] and p['num'] is not None and q['num'] is not None:
                        if p['road'] == q['road'] and abs(p['num'] - q['num']) <= 6:
                            cand.append((d, pi, j))
                    elif d <= 30:
                        cand.append((d, pi, j))
            cand.sort(); usedp, usedq = set(), set()
            for d, pi, j in cand:
                if pi in usedp or j in usedq:
                    continue
                usedp.add(pi); usedq.add(j)
                p, q = rest[pi], Q[j]
                p['drop'] = '連鎖庫內互補：%dm 內已有完整門市名「%s」（同路名或 30m 內）' % (d, q['new'] or q['name']); inDB_n[b] += 1
                changes.append((p['db'], p['id'], p['kind'], p['name'], '', '連鎖庫內互補-重複移除', p['lat'], p['lon']))
        log('連鎖 %s：放寬改名 %d、放寬重複移除 %d、庫內互補移除 %d' % (b, renamed_n[b], dedup_n[b], inDB_n[b]))
    chain_after = chain_stats()

    # ================= B 官方名冊
    E, bad = official_entries(a.offdir, geo, log)
    live = [p for p in ALL if not p['drop'] and p['kind'] not in ('road', 'place', 'peak')]
    trees = {}
    for cls, ks in KINDS.items():
        L = [p for p in live if p['kind'] in ks]
        trees[cls] = (L, cKDTree(xy([p['lat'] for p in L], [p['lon'] for p in L])))
    alltree = cKDTree(xy([p['lat'] for p in live], [p['lon'] for p in live]))
    stat = collections.defaultdict(collections.Counter)
    weights = {}
    for k in ('service', 'worship'):
        weights[k] = int(statistics.median([p['w'] for p in M if p['kind'] == k] or [100]))
    nid = max(p['id'] for p in M)
    for e in E:
        s = stat[e['src']]
        s['名冊(可定位)'] += 1
        L, tree = trees[e['cls']]
        exy = xy([e['lat']], [e['lon']])[0]
        rad = 250 if e['src'].startswith('教育部大專') else 150
        best = None
        for j in tree.query_ball_point(exy, rad):
            p = L[j]
            if p['drop']:
                continue
            d = float(np.hypot(*(exy - tree.data[j])))
            sc = sim(base_name(p['name']), e['name'])
            bnm = base_name(p['name'])
            if sc == 0 and bn.key(bnm) in bn._GEN_KEYS and d <= 50 and end_class(bnm) and end_class(bnm) == end_class(e['name']):
                sc = 1   # 純通用詞（福德祠、診所…）且 50 m 內同類結尾
            if sc == 0 and not bn.has_cjk(bnm) and d <= 50 and en_ok(bnm, e['name'], e['cls']):
                sc = 1   # 純英文名只在英文名帶同類關鍵字（School／Clinic／Temple…）且 50 m 內才視為同一處
            if sc:
                key = (-sc, d)
                if best is None or key < best[0]:
                    best = (key, p, d, sc)
        if best:
            _, p, d, sc = best
            s['命中']+= 1
            if e['closed']:
                s['疑似歇業(命中終止/歇業)'] += 1
                closed_list.append((e['src'], e['name'], e['addr'], p['db'], p['id'], p['name'], round(d)))
                matchrows.append((e['src'], e['name'], e['addr'], p['db'], p['id'], p['name'], round(d), '命中但名冊註記終止合約或歇業（疑似歇業，未刪）'))
                continue
            bp_ = base_name(p['name'])
            longer = e['cls'] in ('medical', 'temple') and sc == 2 and d <= 60 and core(bp_) in core(e['title']) and len(core(e['title'])) - len(core(bp_)) >= 2 and len(e['title']) <= 24 and '法人' not in e['title'] and ('附設' not in e['title'] or '附設' in p['name'])
            if (is_bad(p['name']) or longer) and sc < 3 and not p['new'] and nz(p['name']) != nz(e['title']):
                p['new'] = e['title']; p['off_addr'] = e['addr']; p['off_cls'] = e['cls']; s['改名'] += 1
                changes.append((p['db'], p['id'], p['kind'], p['name'], e['title'], '官方名冊補名：' + e['src'], p['lat'], p['lon']))
                matchrows.append((e['src'], e['name'], e['addr'], p['db'], p['id'], p['name'], round(d), '改名→' + e['title']))
            else:
                matchrows.append((e['src'], e['name'], e['addr'], p['db'], p['id'], p['name'], round(d), '命中不改'))
            continue
        if e['closed']:
            s['未命中且已終止'] += 1
            continue
        # 新增？
        near_any = alltree.query_ball_point(exy, 40)
        if any(live[j]['kind'] in KINDS[e['cls']] for j in near_any):
            s['未命中但40m內有同類POI(不新增)'] += 1
            continue
        if any(sim(base_name(live[j]['name']), e['name']) for j in alltree.query_ball_point(exy, 300)):
            s['未命中但300m內有相似名(不新增)'] += 1
            continue
        nid += 1
        kind = 'worship' if e['cls'] == 'temple' else 'service'
        note = re.sub(r'^\[[^\]]*\]', '', e['addr'])
        note = re.sub(r'^[一-鿿]{2}[縣市]', '', note)
        if not e['sub']:
            pp = geo.parse(e['addr']); e['sub'] = ((pp['city'] or '') + (pp['district'] or '')) if pp else ''
        adds.append(dict(id=nid, name=e['title'], kind=kind, sub=e['sub'], lat=round(e['lat'], 6), lon=round(e['lon'], 6), w=weights[kind], note=note, src=e['src'], prec=e['prec']))
        s['新增'] += 1
    for s_, c in stat.items():
        log('%s：%s' % (s_, dict(c)))

    # ================= 新標題的地點區別（通用名／同名>=10 才加；只用名冊自己的地址，同第一輪規則）
    cnt = collections.Counter(bn.norm_name(p['new'] or p['name']).lower() for p in ALL if not p['drop'] and p['kind'] not in ('road', 'place'))
    for r in adds:
        cnt[bn.norm_name(r['name']).lower()] += 1
    def disamb(title, kind, addr):
        if _PAREN_RE.search(title):
            return title
        if bn.is_brand(title) or bn.is_generic(title) or cnt[bn.norm_name(title).lower()] >= 10:
            lbl = bn.fix_label_noise(bn.label_for(kind, addr))
            if lbl and lbl not in title:
                return '%s（%s）' % (title, lbl)
        return title
    fix = {}
    for p in ALL:
        if p.get('off_addr') and p['new']:
            t = disamb(p['new'], p['kind'], p['off_addr'])
            if t != p['new']:
                p['new'] = t; fix[(p['db'], p['id'])] = t
    for r in adds:
        r['name'] = disamb(r['name'], r['kind'], r['note'])
    changes = [(c[0], c[1], c[2], c[3], fix.get((c[0], c[1]), c[4]), c[5], c[6], c[7]) for c in changes]
    matchrows = [m for m in matchrows]

    # ================= 寫庫
    def write(src, dst, rows, is_main):
        shutil.copyfile(src, dst)
        c = sqlite3.connect(dst)
        c.execute('BEGIN')
        for p in rows:
            if p['drop']:
                c.execute('DELETE FROM place WHERE id=?', (p['id'],)); c.execute('DELETE FROM search WHERE rowid=?', (p['id'],))
            elif p['new']:
                c.execute('UPDATE place SET name=? WHERE id=?', (p['new'], p['id']))
                if p.get('note_if_empty') and not p['note']:
                    c.execute('UPDATE place SET note=? WHERE id=? AND (note IS NULL OR note=\'\')', (re.sub(r'^[一-鿿]{2}[縣市]', '', p['note_if_empty']), p['id']))
                g = c.execute('SELECT grams FROM search WHERE rowid=?', (p['id'],)).fetchone()
                extra = ''
                if p['brand']:
                    extra = ' '.join(bn.grams(x) for x in cb.ALIASES[p['brand']])
                add = ' '.join(x for x in (bn.grams(p['new']) + ' ' + extra).split() if x not in (g[0].split() if g else []))
                if g is not None and add:
                    c.execute('UPDATE search SET grams=? WHERE rowid=?', (g[0] + ' ' + add, p['id']))
        if is_main:
            for r in adds:
                c.execute('INSERT INTO place(id,name,kind,sub,lat,lon,weight,note) VALUES(?,?,?,?,?,?,?,?)', (r['id'], r['name'], r['kind'], r['sub'] or None, r['lat'], r['lon'], r['w'], r['note']))
                c.execute('INSERT INTO search(rowid,grams) VALUES(?,?)', (r['id'], bn.grams(r['name'])))
        n = c.execute('SELECT count(*) FROM place').fetchone()[0]
        c.execute("INSERT OR REPLACE INTO meta VALUES('row_count',?)", (str(n),))
        c.execute("INSERT OR REPLACE INTO meta VALUES('roster_names', ?)", ('官方名冊補名 %s：教育部統計處學校名錄、健保特約醫事機構、內政部全國宗教資訊（寺廟、教會）、連鎖放寬配對；build_roster_names.py' % datetime.date.today(),))
        c.commit(); c.execute("INSERT INTO search(search) VALUES('optimize')"); c.commit(); c.execute('VACUUM'); c.close()
        return n
    nm = write(a.main, os.path.join(a.outdir, 'touror_places.sqlite'), M, True)
    no = write(a.ov, os.path.join(a.outdir, 'touror_places_overture.sqlite'), O, False)

    # ================= 後
    def rows_after(path):
        c = sqlite3.connect(path); r = c.execute('SELECT name,kind,lat,lon FROM place').fetchall(); c.close(); return r
    ma, oa = rows_after(os.path.join(a.outdir, 'touror_places.sqlite')), rows_after(os.path.join(a.outdir, 'touror_places_overture.sqlite'))
    after_q = {'OSM主庫': bn.quality([(r[0], r[1]) for r in ma]), 'Overture': bn.quality([(r[0], r[1]) for r in oa])}
    aq, na = bn.merged_quality([(0, ma), (1, gov), (2, oa)]); after_q['合併去重'] = aq

    def w(name, header, rows):
        with open(os.path.join(a.outdir, name), 'w', encoding='utf-8-sig', newline='') as f:
            cw = csv.writer(f); cw.writerow(header); cw.writerows(rows)
    w('前後對照.csv', ['庫', 'id', 'kind', '原名稱', '新名稱（空白＝移除重複點）', '規則', 'lat', 'lon'], changes)
    w('新增.csv', ['id', '名稱', 'kind', 'sub', 'lat', 'lon', 'note', '來源', '座標精度'], [[r['id'], r['name'], r['kind'], r['sub'], r['lat'], r['lon'], r['note'], r['src'], r['prec']] for r in adds])
    w('官方名冊配對.csv', ['來源', '名冊名稱', '名冊地址', '庫', 'POI id', 'POI 名稱', '距離m', '處置'], matchrows)
    w('疑似歇業.csv', ['來源', '名冊名稱', '名冊地址', '庫', 'POI id', 'POI 名稱', '距離m'], closed_list)
    w('名冊無法轉座標.csv', ['來源', '名稱', '地址', '原因'], [[b['src'], b['name'], b['addr'], b['reason']] for b in bad])
    w('連鎖前後比例.csv', ['品牌', '前_總', '前_完整門市名', '前_比例', '後_總', '後_完整門市名', '後_比例', '放寬改名', '放寬重複移除', '庫內互補移除'],
      [[b, chain_before[b, 'total'], chain_before[b, 'full'], '%.1f%%' % (100.0 * chain_before[b, 'full'] / max(1, chain_before[b, 'total'])),
        chain_after[b, 'total'], chain_after[b, 'full'], '%.1f%%' % (100.0 * chain_after[b, 'full'] / max(1, chain_after[b, 'total'])), renamed_n[b], dedup_n[b], inDB_n[b]] for b in cb.BRANDS])
    with open(os.path.join(a.outdir, '品質前後.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        cw = csv.writer(f); cw.writerow(['口徑', 'kind', '階段', '總', '純英文', '品牌名無區別', '過於通用', '同名>=10', '名稱品質差', '完整地點名稱比例'])
        for scope in before_q:
            for stage, D in (('前', before_q), ('後', after_q)):
                for k, d in sorted(D[scope].items(), key=lambda x: -x[1]['總']):
                    cw.writerow([scope, k, stage, d['總'], d['純英文'], d['品牌名無區別'], d['過於通用'], d['同名>=10'], d['差'], '%.1f%%' % (100 - 100.0 * d['差'] / d['總'])])
    summ = dict(main=[len(M), nm], ov=[len(O), no], merged=[nb, na], chain={b: dict(before=[chain_before[b, 'full'], chain_before[b, 'total']], after=[chain_after[b, 'full'], chain_after[b, 'total']]) for b in cb.BRANDS},
                official={k: dict(v) for k, v in stat.items()}, added=len(adds), changes=len(changes), unlocated=len(bad), closed_suspect=len(closed_list))
    json.dump(summ, open(os.path.join(a.outdir, 'summary.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    log(json.dumps(summ, ensure_ascii=False))


if __name__ == '__main__':
    main()
