# -*- coding: utf-8 -*-
"""每週店家庫重建的驗證閘門（三庫）。用法：python verify.py <新庫資料夾> <舊庫資料夾(目前線上版)> <報告.json>
兩個資料夾都要有 touror_places.sqlite、touror_places_overture.sqlite、touror_places_gov.sqlite。
閘門（任一不過回傳 1，不發布）：
 每庫：
  1 檔案大小與舊版相差 <= ±15%
  2 place 筆數與舊版相差 <= ±5%
  3 place_addr：孤兒／重複／空路名／道路段掛地址 全為 0；road_kind／source 合法；lane/alley/number 格式
  4 place_addr 覆蓋率（筆數／非 road 筆數）不低於舊版 2 個百分點以上
  5 place 欄位 schema 與舊版相容（只允許新增欄位）
  6 PRAGMA integrity_check ok
 主庫＋Overture 合併：
  7 四大超商（7-11／全家／萊爾富／OK）分店名覆蓋率（有分店名／該品牌筆數）整體不低於舊版 2 個百分點以上
  8 各品牌分店名比例不退步（容許 0.5 個百分點誤差）、各品牌筆數不少於舊版 95%
  9 關鍵案例：FTS 搜尋要查得到（谷關、日月潭、合歡山、逢甲夜市、台北101、九份、中油、郵局；政府庫要有中油與郵局）
"""
import json, os, re, sqlite3, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_places import fts_query  # noqa: E402

DBS = {'main': 'touror_places.sqlite', 'overture': 'touror_places_overture.sqlite', 'gov': 'touror_places_gov.sqlite'}
BRANDS = {
    '7-11': re.compile(r'^\s*(7[\s\-]?eleven|7[\s\-]?11|統一超商)', re.I),
    '全家': re.compile(r'^\s*(全家便利商店|全家|family\s*mart)', re.I),
    '萊爾富': re.compile(r'^\s*(萊爾富便利商店|萊爾富|hi[\s\-]?life)', re.I),
    'OK': re.compile(r'^\s*(ok\s*mart|ok\s*超商|ok便利商店|ok(?=[\s\-]|[一-鿿]))', re.I),
}
STRIP = re.compile(r'^[\s\-－‐·．.()（）]*(便利商店|超商)?[\s\-－‐·．.()（）]*')
KEY = ['谷關', '日月潭', '合歡山', '逢甲夜市', '台北101', '九份', '中油', '郵局']


def ro(p):
    return sqlite3.connect('file:%s?mode=ro' % p.replace('\\', '/'), uri=True)


def brand_stats(cons):
    tot = {b: 0 for b in BRANDS}; br = {b: 0 for b in BRANDS}
    for c in cons:
        for (name,) in c.execute("SELECT name FROM place WHERE kind!='road'"):
            for b, rx in BRANDS.items():
                m = rx.match(name or '')
                if m:
                    tot[b] += 1
                    rest = STRIP.sub('', name[m.end():])
                    if re.search(r'[一-鿿].*(店|門市)\s*$', rest): br[b] += 1  # 完整門市名：品牌後面接「XX店／XX門市」
                    break
    return {b: {'n': tot[b], 'branch': br[b], 'ratio': br[b] / max(1, tot[b])} for b in BRANDS}, sum(br.values()) / max(1, sum(tot.values()))


def fts_hits(c, q):
    e = fts_query(q)
    return c.execute('SELECT COUNT(*) FROM (SELECT rowid FROM search WHERE search MATCH ? LIMIT 50)', (e,)).fetchone()[0] if e else 0


def check_db(kind, new, old, fails, info):
    cn, co = ro(new), ro(old)
    d = info[kind] = {}
    r = os.path.getsize(new) / os.path.getsize(old) - 1; d['size_ratio'] = round(r, 4)
    if abs(r) > 0.15: fails.append('%s 檔案大小變化 %+.1f%% 超過 ±15%%' % (kind, 100 * r))
    nn = cn.execute('SELECT COUNT(*) FROM place').fetchone()[0]; no = co.execute('SELECT COUNT(*) FROM place').fetchone()[0]
    d['rows_new'], d['rows_old'] = nn, no
    if abs(nn / no - 1) > 0.05: fails.append('%s place 筆數 %d→%d 變化超過 ±5%%' % (kind, no, nn))
    ids = dict(cn.execute('SELECT id,kind FROM place'))
    rows = cn.execute('SELECT place_id,road,road_base,road_kind,lane,alley,number,source FROM place_addr').fetchall()
    if len({x[0] for x in rows}) != len(rows): fails.append(kind + ' place_addr place_id 重複')
    if any(x[0] not in ids for x in rows): fails.append(kind + ' place_addr 有孤兒 place_id')
    if any(not x[1] or not x[2] for x in rows): fails.append(kind + ' place_addr 有空路名')
    if any(ids.get(x[0]) == 'road' for x in rows): fails.append(kind + ' place_addr 掛在道路段上')
    if {x[3] for x in rows} - {'路', '街', '大道', '道', '線', '巷', '弄'}: fails.append(kind + ' road_kind 不合法')
    if {x[7] for x in rows} - {'osm', 'overture', 'overture_en', 'overture_10m', 'gov', 'note', 'name'}: fails.append(kind + ' source 不合法')
    pat = re.compile(r'^[0-9]+(之[0-9]+)?$')
    if any(v and not pat.match(v) for x in rows for v in x[4:7]): fails.append(kind + ' lane/alley/number 格式不符')
    nr_new = cn.execute("SELECT COUNT(*) FROM place WHERE kind!='road'").fetchone()[0]
    nr_old = co.execute("SELECT COUNT(*) FROM place WHERE kind!='road'").fetchone()[0]
    cov = len(rows) / max(1, nr_new); covo = co.execute('SELECT COUNT(*) FROM place_addr').fetchone()[0] / max(1, nr_old)
    d['addr_cov_new'], d['addr_cov_old'] = round(cov, 4), round(covo, 4)
    if cov < covo - 0.02: fails.append('%s place_addr 覆蓋率 %.1f%% 比舊版 %.1f%% 低超過 2 點' % (kind, 100 * cov, 100 * covo))
    cols = lambda c: [(x[1], x[2]) for x in c.execute('PRAGMA table_info(place)')]
    a, b = cols(co), cols(cn)
    if b[:len(a)] != a: fails.append('%s place 欄位與舊版不相容 %s vs %s' % (kind, a, b))
    if cn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': fails.append(kind + ' integrity_check 不通過')
    return cn, co


def main(newdir, olddir, out):
    fails, info = [], {}
    cons = {}
    for k, f in DBS.items():
        cons[k] = check_db(k, os.path.join(newdir, f), os.path.join(olddir, f), fails, info)
    bn, tn = brand_stats([cons['main'][0], cons['overture'][0]])
    bo, to = brand_stats([cons['main'][1], cons['overture'][1]])
    info['branch_cov_new'], info['branch_cov_old'] = round(tn, 4), round(to, 4)
    info['brands_new'], info['brands_old'] = bn, bo
    if tn < to - 0.02: fails.append('四大超商分店名覆蓋率 %.1f%% 比舊版 %.1f%% 低超過 2 點' % (100 * tn, 100 * to))
    for b in BRANDS:
        if bn[b]['ratio'] < bo[b]['ratio'] - 0.005: fails.append('%s 分店名比例 %.1f%% 退步（舊 %.1f%%）' % (b, 100 * bn[b]['ratio'], 100 * bo[b]['ratio']))
        if bn[b]['n'] < 0.95 * bo[b]['n']: fails.append('%s 筆數 %d 少於舊版 %d 的 95%%' % (b, bn[b]['n'], bo[b]['n']))
    info['key_cases'] = {}
    for q in KEY:
        h = sum(fts_hits(cons[k][0], q) for k in DBS)
        info['key_cases'][q] = h
        if not h: fails.append('關鍵案例搜尋不到：' + q)
    for q in ('中油', '郵局'):
        if not fts_hits(cons['gov'][0], q): fails.append('政府庫搜尋不到：' + q)
    rep = {'ok': not fails, 'failures': fails, 'info': info}
    json.dump(rep, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if not fails else 1


if __name__ == '__main__':
    sys.exit(main(*sys.argv[1:4]))
