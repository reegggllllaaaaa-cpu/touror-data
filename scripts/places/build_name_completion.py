# -*- coding: utf-8 -*-
"""名稱補全（全類別命名規則）：把庫內「品牌名／通用名沒有地點區別」「純英文名」「永久歇業」的筆數補全或剔除。

輸入（唯讀，不覆蓋）：
  --main  OSM 主庫（含 note 欄）        預設 候選-分店名/連鎖分店名/去重/touror_places.sqlite
  --ov    Overture 庫                    預設 候選-分店名/連鎖分店名/去重/touror_places_overture.sqlite
  --flat  路網來源/overture/flat.parquet（fetch_overture_raw.py → flatten_overture_raw.py 產生；Overture 原始 names／addresses／operating_status）
  --gov   touror_places_gov.sqlite（只用於「合併去重」口徑統計）
輸出：--outdir（預設 候選-名稱補全/）：兩個新庫、前後對照（名稱前後對照.csv 全量、品質前後.csv、summary.json、剔除永久歇業.csv）。

命名規則（標題優先序，逐筆）：
  ① 名冊（連鎖名冊配對，build_chain_branches.py 已做；標題已是「品牌 門市名」者原樣保留）
  ② OSM／Overture 的完整中文名（含分店）：已含中文且不是品牌／通用名者原樣保留
  ③ Overture names.common.zh*／names.rules(language=zh)／brand.names 中最長且含中文的名稱：
     只在「純英文」或「品牌短名被較長的中文全名涵蓋（全聯→全聯福利中心）」時取用
  ④ 品牌或通用名＋地點區別：標題「名稱（路名 門牌 號）」；worship 優先「名稱（區 里）」。括號內只用該筆實際地址／里名／路名；
     沒有任何地址者保持原樣（不編造）。同名＋同行政區＋同 kind 重複者同樣加地點區別。
  純英文名：有中文名就用中文（③）；其餘保留英文，並把地址寫進 note（Overture 庫新增 nullable note 欄，App 現行查詢明列欄位，不受影響）。
  永久歇業（Overture operating_status=permanently_closed）：剔除。
FTS：標題改變者，把新增文字的 bigram 附加到原 grams 後（原英文名／品牌名仍可搜到）。
重跑：<overture_venv>\Scripts\python.exe -I build_name_completion.py
記憶體：單進程 < 3 GB。
"""
import argparse, collections, csv, datetime, json, os, re, shutil, sqlite3, sys, time, unicodedata
import numpy as np, pandas as pd
from scipy.spatial import cKDTree

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_places import norm_name, grams, is_cjk

SKIP_KINDS = {'road', 'place', 'peak'}

GENERIC_WORDS = set(x.lower() for x in """加油站 學校 公園 便利商店 超商 診所 醫院 停車場 廁所 公廁 土地公廟 福德祠 福德宮 土地公 土地廟 福德正神 觀音廟 媽祖廟 天后宮 宮廟 寺廟 廟 寺 宮 教堂 教會 魚塭 魚池 農場 民宿 旅館 飯店 餐廳 咖啡 咖啡廳 早餐店 早餐 便當 小吃 麵店 水果店 檳榔攤 檳榔 派出所 郵局 銀行 公所 衛生所 圖書館 活動中心 社區活動中心 市場 菜市場 傳統市場 涼亭 觀景台 休息站 休憩區 景觀台 步道 草地 空地 大樓 公寓 工廠 倉庫 住宅 商店 雜貨店 小吃店 餐館 美髮 理髮 理髮廳 美容院 洗衣店 藥局 藥房 牙醫 牙醫診所 中醫 中醫診所 幼兒園 國小 國中 高中 幼稚園 學校 停車場 停車 ATM atm""".split())
BRAND_WORDS = set(x.lower() for x in """7-11 7-eleven 7eleven seven-eleven 7-ELEVEN 統一超商 小七 全家 全家便利商店 familymart family mart 萊爾富 萊爾富便利商店 hi-life ok超商 ok mart okmart ok便利商店 全聯 全聯福利中心 pxmart 屈臣氏 watsons 康是美 cosmed 星巴克 starbucks 麥當勞 mcdonald's 肯德基 kfc 必勝客 pizza hut 摩斯漢堡 mos burger 路易莎 louisa 中油 台灣中油 中國石油 台塑石油 全國加油站 蝦皮店到店 蝦皮 家樂福 carrefour 大潤發 好市多 costco 小北百貨 寶雅 poya 美廉社 愛買 特力屋 光陽 三陽 台灣大哥大 中華電信 遠傳電信 遠傳 台灣之星 郵局 中華郵政 台灣銀行 合作金庫 第一銀行 華南銀行 彰化銀行 兆豐銀行 土地銀行 國泰世華 國泰世華銀行 國泰世華商業銀行 中國信託 中國信託商業銀行 玉山銀行 台新銀行 富邦銀行 永豐銀行 台北富邦 台中銀行 ibon 7-11 ibon""".split())
_NOSEP = re.compile(r'[\s\-－‐‑·．.()（）\[\]]+')
_GEN_RE = re.compile(r'^.{0,2}(加油站|公園|學校|診所|醫院|停車場|廁所|超商)$')
_PAREN = re.compile(r'[（(][^()（）]*[）)]\s*$')
CITY_RE = re.compile(r'^[一-鿿]{2}[縣市]')
DIST_RE = re.compile(r'^([一-鿿]{1,3}區|[一-鿿]{1,3}?[鄉鎮市])')
LI_RE = re.compile(r'^([一-鿿]{1,4}?[里村])')
ROAD_NUM = re.compile(r'^((?:\D|\d+(?=[段巷弄]))+?)\s*(\d+)(?![\d段巷弄])(?:\s*[-－之]\s*(\d+))?\s*號?')  # 第二輪修：門牌數字不可是「4段」「101巷」的前半（原本「辛亥路4段101巷3號」會被截成「辛亥路 4 號」）
ROAD_ONLY = re.compile(r'^([一-鿿\d]+(?:路|街|大道|道|巷|弄|段))')


def nz(s):
    return unicodedata.normalize('NFKC', s or '').replace('臺', '台').strip()


def has_cjk(s):
    return any(is_cjk(c) for c in s)


def has_latin(s):
    return any(c.isascii() and c.isalpha() for c in s)


def key(s):
    return _NOSEP.sub('', nz(s).lower())


def is_latin_only(name):
    return not has_cjk(name)  # 含 sym（無字母也無中文）


def is_brand_only(name):
    return key(name) in {_NOSEP.sub('', w) for w in BRAND_WORDS}


_BRAND_KEYS = {_NOSEP.sub('', w) for w in BRAND_WORDS}
_GEN_KEYS = {_NOSEP.sub('', w) for w in GENERIC_WORDS}


def is_brand(name):
    return key(name) in _BRAND_KEYS


def is_generic(name):
    k = key(name)
    return k in _GEN_KEYS or bool(_GEN_RE.match(nz(name)))


def parse_addr(s, locality=None):
    """回傳 (district, li, road_label)。road_label 例『鳳仁路 95 之 15 號』；沒有則 ''。"""
    s = nz(s)
    if not s:
        return '', '', ''
    s = re.sub(r'^\d{3,6}', '', s).strip()
    s = CITY_RE.sub('', s)
    dist = ''
    m = DIST_RE.match(s)
    if m and not re.match(r'^[一-鿿]*[路街道]', m.group(1)):
        dist = m.group(1); s = s[m.end():]
    elif locality:
        L = nz(locality)
        if re.search(r'[區鄉鎮市]$', L) and not re.search(r'[縣]$|^[一-鿿]{2}市$', L):
            dist = L
    li = ''
    m = LI_RE.match(s)
    if m and not re.match(r'^[一-鿿]*[路街道]', m.group(1)):
        li = m.group(1); s = s[m.end():]
    s = re.sub(r'^\d+鄰', '', s)
    road = ''
    m = ROAD_NUM.match(s)
    if m and re.search(r'[一-鿿]', m.group(1)) and re.search(r'(路|街|道|巷|弄|段|村|里|堤|橋|坑|溪|湖)', m.group(1) + ('號' if m.group(0).endswith('號') else '')):
        a, b = m.group(2), m.group(3)
        road = '%s %s%s 號' % (m.group(1).strip(), a, (' 之 ' + b) if b else '')
    else:
        m = ROAD_ONLY.match(s)
        if m:
            road = m.group(1)
    return dist, li, road


def label_for(kind, addr, locality=None):
    dist, li, road = parse_addr(addr, locality)
    if kind == 'worship' and dist and li:
        return dist + li
    if road:
        return road
    if li:
        return dist + li
    return dist


def fix_label_noise(lbl):
    return lbl.replace('  ', ' ').strip()


# ---------------------------------------------------------------- 品質指標（與名稱品質盤點報告同口徑）
def quality(rows):
    """rows: list of (name, kind) → {kind: dict}。同名≥10 以庫內全名計（road、place 不計）。"""
    cnt = collections.Counter(norm_name(n).lower() for n, k in rows if k not in ('road', 'place'))
    out = {}
    for n, k in rows:
        d = out.setdefault(k, collections.Counter())
        d['總'] += 1
        nn = norm_name(n).lower()
        lat = is_latin_only(n)
        gen = is_generic(n) or is_brand(n)
        dup = k not in ('road', 'place') and cnt[nn] >= 10
        if lat: d['純英文'] += 1
        if is_brand(n): d['品牌名無區別'] += 1
        if is_generic(n): d['過於通用'] += 1
        if dup: d['同名>=10'] += 1
        if lat or gen or dup: d['差'] += 1
    return out


def merged_quality(sources):
    """sources: list of (prio, rows[(name,kind,lat,lon)])；50 m 內同 kind 保留優先序高、名稱品質好者。"""
    allr = []
    for prio, rows in sources:
        for n, k, la, lo in rows:
            allr.append((prio, n, k, la, lo))
    bad = lambda n: 1 if (is_latin_only(n) or is_brand(n) or is_generic(n)) else 0
    keep = []
    byk = collections.defaultdict(list)
    for r in allr:
        byk[r[2]].append(r)
    for k, lst in byk.items():
        lst.sort(key=lambda r: (r[0], bad(r[1])))
        la = np.array([r[3] for r in lst]); lo = np.array([r[4] for r in lst])
        xy = np.c_[la * 111000.0, lo * 111000.0 * np.cos(np.radians(23.7))]
        tree = cKDTree(xy)
        gone = np.zeros(len(lst), bool)
        for i in range(len(lst)):
            if gone[i]:
                continue
            for j in tree.query_ball_point(xy[i], 50.0):
                if j > i:
                    gone[j] = True
        keep += [lst[i] for i in range(len(lst)) if not gone[i]]
    return quality([(r[1], r[2]) for r in keep]), len(keep)


def log(s):
    print(time.strftime('%H:%M:%S'), s, flush=True)


def main():
    S = os.path.join(HERE, '候選-分店名', '連鎖分店名', '去重')
    ap = argparse.ArgumentParser()
    ap.add_argument('--main', default=os.path.join(S, 'touror_places.sqlite'))
    ap.add_argument('--ov', default=os.path.join(S, 'touror_places_overture.sqlite'))
    ap.add_argument('--flat', default=os.path.join(HERE, '路網來源', 'overture', 'flat.parquet'))
    ap.add_argument('--gov', default=os.path.join(HERE, 'touror_places_gov.sqlite'))
    ap.add_argument('--outdir', default=os.path.join(HERE, '候選-名稱補全'))
    a = ap.parse_args()
    os.makedirs(a.outdir, exist_ok=True)
    flat = pd.read_parquet(a.flat)
    flat = flat.where(flat.notna(), None)
    log('flat %d' % len(flat))
    ftree = cKDTree(np.c_[flat.lat.values * 111000.0, flat.lon.values * 111000.0 * np.cos(np.radians(23.7))])

    # ---- 讀兩庫
    def load(path):
        c = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True)
        cols = [r[1] for r in c.execute('PRAGMA table_info(place)')]
        rows = c.execute('SELECT * FROM place').fetchall()
        c.close()
        return cols, rows
    mcols, mrows = load(a.main)
    ocols, orows = load(a.ov)
    gov = sqlite3.connect(a.gov).execute('SELECT name,kind,lat,lon FROM place').fetchall()
    gk = {'旅宿': 'lodging', '景點': 'scenic'}
    gov = [(n, gk.get(k, k), la, lo) for n, k, la, lo in gov]

    # 前（品質）
    before = {'OSM主庫': quality([(r[1], r[2]) for r in mrows]), 'Overture': quality([(r[1], r[2]) for r in orows])}
    mb, nb = merged_quality([(0, [(r[1], r[2], r[4], r[5]) for r in mrows]), (1, gov), (2, [(r[1], r[2], r[4], r[5]) for r in orows])])
    before['合併去重'] = mb
    nmerge_before = nb
    log('before done merged=%d' % nb)

    # ---- Overture 配對 flat
    ov_xy = np.c_[[r[4] * 111000.0 for r in orows], [r[5] * 111000.0 * np.cos(np.radians(23.7)) for r in orows]]
    near = ftree.query_ball_point(ov_xy, 3.0)
    P = flat.primary_name.values
    match = [None] * len(orows)
    for i, r in enumerate(orows):
        cand = near[i]
        if not cand:
            continue
        nm = r[1]
        pick = next((j for j in cand if P[j] == nm), None)
        if pick is None:
            pick = next((j for j in cand if P[j] and key(P[j]) == key(nm)), None)
        if pick is None:
            # 品牌改名者：同品牌／英文名
            pick = next((j for j in cand if P[j] and not has_cjk(P[j])), None) if not has_cjk(nm) or is_brand(nm) else None
        match[i] = pick
    mrate = sum(1 for m in match if m is not None) / max(1, len(orows))
    log('overture matched %.3f' % mrate)

    def zh_candidates(j):
        f = flat.iloc[j]
        c = []
        for k in ('c_zhhant', 'c_zhtw', 'c_zh'):
            if f[k]:
                c.append(f[k])
        for ru in (f.rules if f.rules is not None else []):
            v, lang, val = (ru.split('|', 2) + ['', ''])[:3] if ru.count('|') >= 2 else ('', '', '')
            if val and has_cjk(val) and (lang.startswith('zh') or lang == '') and v in ('short', 'language', 'official', 'alternate'):
                c.append(val)
        for k in ('brand_zhhant', 'brand_zhtw', 'brand_zh'):
            if f[k]:
                c.append(f[k])
        return [nz(x) for x in c if x and has_cjk(x)]

    changes = []   # (db, id, kind, old, new, rule, lat, lon)
    measure = collections.Counter()
    closed = []

    def process(dbname, cols, rows, is_ov):
        ix = {c: i for i, c in enumerate(cols)}
        out_rows, newgram, notes = {}, {}, {}
        dropped = []
        # 同名計數（含 note／地址前的原名）
        addr_of, loc_of = {}, {}
        for i, r in enumerate(rows):
            k = r[ix['kind']]
            if is_ov:
                j = match[i]
                if j is not None:
                    f = flat.iloc[j]
                    addr_of[i] = f['addr'] or ''
                    loc_of[i] = f['locality']
            else:
                addr_of[i] = (r[ix['note']] or '') if 'note' in ix else ''
        cnt = collections.Counter(norm_name(r[1]).lower() for r in rows if r[ix['kind']] not in ('road', 'place'))
        # 同名＋同區＋同 kind
        dkey = collections.Counter()
        dist_of = {}
        for i, r in enumerate(rows):
            if r[ix['kind']] in SKIP_KINDS:
                continue
            d, li, road = parse_addr(addr_of.get(i, ''), loc_of.get(i))
            dist_of[i] = d
            if d:
                dkey[(norm_name(r[1]).lower(), r[ix['kind']], r[ix['sub']], d)] += 1
        for i, r in enumerate(rows):
            id_, name, kind = r[0], r[1], r[ix['kind']]
            lat, lon = r[ix['lat']], r[ix['lon']]
            new, rule, note_new = name, None, None
            j = match[i] if is_ov else None
            if is_ov and j is not None and flat.operating_status.values[j] == 'permanently_closed':
                dropped.append((id_, name, kind, lat, lon)); continue
            if kind in SKIP_KINDS:
                out_rows[id_] = (new, None); continue
            addr = addr_of.get(i, '')
            lbl = fix_label_noise(label_for(kind, addr, loc_of.get(i)))
            latin = is_latin_only(name)
            # ③ 較完整的中文名
            if is_ov and j is not None:
                zhs = zh_candidates(j)
                if zhs:
                    best = max(zhs, key=len)
                    if latin:
                        new, rule = best, '③英文→中文全名'
                        measure['ov純英文有中文可補'] += 1
                    elif is_brand(name) and len(best) > len(nz(name)) and key(name) in key(best) and key(best) != key(name):
                        new, rule = best, '③品牌短名→中文全名'
                        measure['ov品牌短名有較長中文全名'] += 1
            if latin and rule is None and is_ov and j is not None:
                measure['ov純英文無中文可補'] += 1
            nn = norm_name(name).lower()
            need = False
            if not has_cjk(new) and rule is None:
                # 純英文：保留英文，補 note
                if lbl:
                    note_new = lbl; rule = '純英文保留＋note'
                    if not is_ov and 'note' in ix and (r[ix['note']] or '').strip():
                        note_new = None; rule = None
            else:
                if is_brand(new) or is_generic(new) or (kind not in SKIP_KINDS and cnt[nn] >= 10 and rule is None):
                    need = True
                elif dist_of.get(i) and dkey[(nn, kind, r[ix['sub']], dist_of[i])] >= 2:
                    need = True
                if need and not _PAREN.search(new):
                    if lbl and lbl not in new:
                        new = '%s（%s）' % (new, lbl)
                        rule = (rule + '＋' if rule else '') + '④地點區別'
                    else:
                        measure['需區別但無地址'] += 1
            if new != name or note_new:
                changes.append((dbname, id_, kind, name, new, rule or '', lat, lon))
            out_rows[id_] = (new, note_new)
        return out_rows, dropped

    mo, md = process('OSM主庫', mcols, mrows, False)
    log('main processed changes=%d' % len(changes))
    oo, od = process('Overture', ocols, orows, True)
    log('ov processed changes=%d dropped=%d' % (len(changes), len(od)))

    # ---- 寫庫
    def write(src, dst, cols, outmap, dropped, is_ov):
        shutil.copyfile(src, dst)
        c = sqlite3.connect(dst)
        if is_ov and 'note' not in cols:
            c.execute('ALTER TABLE place ADD COLUMN note TEXT')
        c.execute('BEGIN')
        drop_ids = [d[0] for d in dropped]
        for i in drop_ids:
            c.execute('DELETE FROM place WHERE id=?', (i,))
            c.execute('DELETE FROM search WHERE rowid=?', (i,))
        olds = {r[0]: r[1] for r in c.execute('SELECT id,name FROM place')}
        for id_, (new, note_new) in outmap.items():
            old = olds.get(id_)
            if old is None:
                continue
            if new != old:
                c.execute('UPDATE place SET name=? WHERE id=?', (new, id_))
                g = c.execute('SELECT grams FROM search WHERE rowid=?', (id_,)).fetchone()
                add = ' '.join(x for x in grams(new).split() if x not in (g[0].split() if g else []))
                if g is not None and add:
                    c.execute('UPDATE search SET grams=? WHERE rowid=?', (g[0] + ' ' + add, id_))
            if note_new:
                c.execute('UPDATE place SET note=? WHERE id=?', (note_new, id_))
        n = c.execute('SELECT count(*) FROM place').fetchone()[0]
        c.execute("INSERT OR REPLACE INTO meta VALUES('row_count',?)", (str(n),))
        c.execute("INSERT OR REPLACE INTO meta VALUES('name_completion', ?)", ('名稱補全 %s：build_name_completion.py（Overture 原始名稱 release 2026-11-18.0；永久歇業剔除）' % datetime.date.today(),))
        c.commit()
        c.execute("INSERT INTO search(search) VALUES('optimize')")
        c.commit()
        c.execute('VACUUM')
        c.close()
        return n
    nmain = write(a.main, os.path.join(a.outdir, 'touror_places.sqlite'), mcols, mo, md, False)
    nov = write(a.ov, os.path.join(a.outdir, 'touror_places_overture.sqlite'), ocols, oo, od, True)

    # ---- 後
    def rows_after(path, with_xy=True):
        c = sqlite3.connect(path)
        r = c.execute('SELECT name,kind,lat,lon FROM place').fetchall(); c.close(); return r
    ma, oa = rows_after(os.path.join(a.outdir, 'touror_places.sqlite')), rows_after(os.path.join(a.outdir, 'touror_places_overture.sqlite'))
    after = {'OSM主庫': quality([(r[0], r[1]) for r in ma]), 'Overture': quality([(r[0], r[1]) for r in oa])}
    aq, na = merged_quality([(0, ma), (1, gov), (2, oa)])
    after['合併去重'] = aq

    with open(os.path.join(a.outdir, '名稱前後對照.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(['庫', 'id', 'kind', '原名稱', '新名稱', '規則', 'lat', 'lon']); w.writerows(changes)
    with open(os.path.join(a.outdir, '剔除永久歇業.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(['庫', 'id', '名稱', 'kind', 'lat', 'lon']); w.writerows([('Overture',) + d[:1] + d[1:2] + d[2:3] + d[3:] for d in od])
    with open(os.path.join(a.outdir, '品質前後.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(['口徑', 'kind', '階段', '總', '純英文', '品牌名無區別', '過於通用', '同名>=10', '名稱品質差', '完整地點名稱比例'])
        for scope in before:
            for stage, D in (('前', before), ('後', after)):
                for k, d in sorted(D[scope].items(), key=lambda x: -x[1]['總']):
                    w.writerow([scope, k, stage, d['總'], d['純英文'], d['品牌名無區別'], d['過於通用'], d['同名>=10'], d['差'], '%.1f%%' % (100 - 100.0 * d['差'] / d['總'])])
    rc = collections.Counter(c[5] for c in changes)
    summ = {'main': [len(mrows), nmain], 'ov': [len(orows), nov], 'merged': [nmerge_before, na], 'ov_match_rate': mrate,
            'dropped_closed': len(od), 'changes': len(changes), 'rules': rc, 'measure': measure}
    json.dump(summ, open(os.path.join(a.outdir, 'summary.json'), 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=dict)
    log(json.dumps(summ, ensure_ascii=False, default=dict))


if __name__ == '__main__':
    main()
