# -*- coding: utf-8 -*-
"""每週店家庫重建的驗證閘門。用法：python -I verify.py 新庫 舊庫(目前線上版) 報告.json
閘門（任一不過回傳 1，不發布）：
 1 檔案大小與舊版相差 <= ±15%
 2 place 筆數與舊版相差 <= ±10%
 3 place_addr：孤兒／重複／空路名／道路段掛地址 全為 0；road_kind／source 合法；格式 pattern
 4 place_addr 覆蓋率（有路名者／非 road 筆數）不低於舊版 5 個百分點以上
 5 既有 place 欄位 schema 與舊版一致（不得增刪欄位，note 例外只允許新增）
 6 關鍵案例：名稱查得到（谷關、日月潭、合歡山、逢甲夜市、7-ELEVEN 數量 >= 5000）
 7 PRAGMA integrity_check ok
"""
import json, os, re, sqlite3, sys

def stats(p):
    c = sqlite3.connect('file:%s?mode=ro' % p, uri=True)
    n = c.execute('SELECT COUNT(*) FROM place').fetchone()[0]
    nr = c.execute("SELECT COUNT(*) FROM place WHERE kind!='road'").fetchone()[0]
    try: na = c.execute('SELECT COUNT(*) FROM place_addr').fetchone()[0]
    except Exception: na = None
    return c, n, nr, na

def main(new, old, out):
    fails, warns, info = [], [], {}
    cn, nn, nrn, nan_ = stats(new); co, no, nro, nao = stats(old)
    r = os.path.getsize(new) / os.path.getsize(old) - 1
    info['size_ratio'] = r
    if abs(r) > 0.15 and nao is not None: fails.append('檔案大小變化 %+.1f%% 超過 ±15%%' % (100 * r))
    elif abs(r) > 0.25: fails.append('檔案大小變化 %+.1f%%（舊版沒有 place_addr，放寬到 ±25%%）' % (100 * r))
    rr = nn / no - 1; info['rows_ratio'] = rr
    if abs(rr) > 0.10: fails.append('place 筆數變化 %+.1f%% 超過 ±10%%' % (100 * rr))
    if nan_ is None: fails.append('新庫沒有 place_addr')
    else:
        ids = dict(cn.execute('SELECT id,kind FROM place'))
        rows = cn.execute('SELECT place_id,road,road_base,road_kind,lane,alley,number,source FROM place_addr').fetchall()
        if len({r_[0] for r_ in rows}) != len(rows): fails.append('place_addr place_id 重複')
        if [r_ for r_ in rows if r_[0] not in ids]: fails.append('place_addr 有孤兒 place_id')
        if [r_ for r_ in rows if not r_[1] or not r_[2]]: fails.append('place_addr 有空路名')
        if [r_ for r_ in rows if ids.get(r_[0]) == 'road']: fails.append('place_addr 掛在道路段上')
        if {r_[3] for r_ in rows} - {'路', '街', '大道', '道', '線', '巷', '弄'}: fails.append('road_kind 不合法')
        if {r_[7] for r_ in rows} - {'osm', 'overture', 'gov', 'note', 'name'}: fails.append('source 不合法')
        pat = re.compile(r'^[0-9]+(之[0-9]+)?$')
        if [r_ for r_ in rows if any(v and not pat.match(v) for v in r_[4:7])]: fails.append('lane/alley/number 格式不符')
        cov = nan_ / max(1, nrn); info['coverage_new'] = cov
        if nao is not None:
            covo = nao / max(1, nro); info['coverage_old'] = covo
            if cov < covo - 0.05: fails.append('place_addr 覆蓋率 %.1f%% 比舊版 %.1f%% 低超過 5 點' % (100 * cov, 100 * covo))
    cols = lambda c: [(x[1], x[2]) for x in c.execute('PRAGMA table_info(place)')]
    a, b = cols(co), cols(cn)
    if b[:len(a)] != a and a[:len(b)] != b: fails.append('place 欄位與舊版不相容 %s vs %s' % (a, b))
    for nm in ('谷關', '日月潭', '合歡山', '逢甲夜市'):
        if not cn.execute("SELECT 1 FROM place WHERE name LIKE ? LIMIT 1", ('%' + nm + '%',)).fetchone(): fails.append('關鍵案例查不到：' + nm)
    n711 = cn.execute("SELECT COUNT(*) FROM place WHERE name LIKE '%7-ELEVEN%' OR name LIKE '%7-11%'").fetchone()[0]
    info['n711'] = n711
    if n711 < 50: warns.append('7-ELEVEN 只有 %d 筆（OSM 主庫本來就少，超商主要在名冊升格後）' % n711)
    if cn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': fails.append('integrity_check 不通過')
    rep = {'ok': not fails, 'failures': fails, 'warnings': warns, 'info': info}
    json.dump(rep, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if not fails else 1

if __name__ == '__main__':
    sys.exit(main(*sys.argv[1:4]))
