"""7-11 同店兩筆去重（保守）：同品牌、<=100 m、保留筆為名冊配對且完整門市名，被移除筆只有品牌名／縮寫（且在重複點清單內）。"""
import csv, math, shutil, sqlite3, os, collections, re, sys
import numpy as np
from scipy.spatial import cKDTree
# CI 版：python dedupe_711.py <連鎖分店名資料夾> <7-11_emap.csv> --apply（輸出到 <連鎖分店名>/去重）
B = os.path.abspath(sys.argv[1])  # 連鎖分店名
O = os.path.join(B, '去重'); ROSTER711 = os.path.abspath(sys.argv[2])
names = ['touror_places.sqlite', 'touror_places_overture.sqlite']
def xy(la, lo):
    la = np.asarray(la, float); lo = np.asarray(lo, float)
    return np.c_[(lo - 121) * 111320 * math.cos(math.radians(24)), (la - 23.5) * 110574]
ros = list(csv.DictReader(open(ROSTER711, encoding='utf-8-sig')))
rl = []
for r in ros:
    n = re.sub(r'^(7[\s\-]?eleven|7[\s\-]?11|統一超商)[\s\-]*', '', r['name'].strip(), flags=re.I)
    rl.append(n if n.endswith(('門市', '店')) else n + '門市')
rt = cKDTree(xy([float(r['lat']) for r in ros], [float(r['lon']) for r in ros]))
dup = {(r['庫'], int(r['id'])) for r in csv.DictReader(open(os.path.join(B, '疑似歇業或名冊未收.csv'), encoding='utf-8-sig')) if r['品牌'] == '7-11' and r['可信度'].startswith('重複點')}
rows = []
for n in names:
    c = sqlite3.connect(os.path.join(B, n))
    for i, name, la, lo, sub in c.execute("select id,name,lat,lon,sub from place where name like '7-11%' or name like '統一超商%' or name like '7-ELEVEN%'"):
        rows.append((n, i, name, la, lo, sub))
# 保留候選：完整門市名、且距離 300 m 內有同名標籤的名冊店
keep = []
for r in rows:
    m = re.match(r'^7-11 (.+)$', r[2])
    if not m: continue
    lab = m.group(1)
    for j in rt.query_ball_point(xy([r[3]], [r[4]])[0], 300):
        if rl[j] == lab: keep.append(r); break
print('keep pool', len(keep))
kt = cKDTree(xy([k[3] for k in keep], [k[4] for k in keep]))
# 被移除候選：在重複點清單、名稱無完整門市名
rem = []; skipped = collections.Counter()
for r in rows:
    if (r[0], r[1]) not in dup: continue
    if re.match(r'^7-11 .+(門市|店)', r[2]): skipped['has_name'] += 1; continue
    idx = kt.query_ball_point(xy([r[3]], [r[4]])[0], 100)
    if not idx: skipped['no_keeper_100m'] += 1; continue
    lab = re.sub(r'^(7[\s\-]?eleven|7[\s\-]?11|統一超商)[\s\-]*', '', r[2].strip(), flags=re.I)
    if lab and not any(keep[k][2][5:] == lab or keep[k][2][5:].replace('門市','') == lab.replace('門市','') for k in idx):
        skipped['own_label_differs'] += 1; continue
    idx.sort(key=lambda k: (keep[k][3]-r[3])**2 + (keep[k][4]-r[4])**2)
    rem.append((r, [keep[k] for k in idx]))
# 風險：同一保留店被多個重複點對應（多筆）
cnt = collections.Counter((k[0][0], k[0][1]) for r, k in rem)
many = [x for x, v in cnt.items() if v > 2]
print('rem', len(rem), 'skipped', dict(skipped), 'keepers hit >2:', len(many), 'max', max(cnt.values()) if cnt else 0)
os.makedirs(O, exist_ok=True)
with open(os.path.join(O, '去重移除清單.csv'), 'w', encoding='utf-8-sig', newline='') as f:
    w = csv.writer(f); w.writerow(['庫', 'id', '名稱', 'lat', 'lon', '縣市區', '保留庫', '保留id', '保留名稱', '距離m'])
    for r, ks in rem:
        k = ks[0]; d = math.hypot(*(xy([r[3]], [r[4]]) - xy([k[3]], [k[4]]))[0])
        w.writerow([r[0], r[1], r[2], r[3], r[4], r[5] or '', k[0], k[1], k[2], round(d)])
byc = collections.Counter(); bydb = collections.Counter()
for r, ks in rem:
    byc[(r[5] or '')[:3]] += 1; bydb[r[0]] += 1
print(bydb); print(byc.most_common(8))
if '--apply' not in sys.argv: sys.exit()
ba = {}
for n in names:
    dst = os.path.join(O, n); shutil.copyfile(os.path.join(B, n), dst)
    c = sqlite3.connect(dst)
    before = c.execute('select count(*) from place').fetchone()[0]
    ids = [(r[1],) for r, _ in rem if r[0] == n]
    c.executemany('delete from place where id=?', ids); c.executemany('delete from search where rowid=?', ids)
    c.execute("insert into search(search) values('optimize')")
    after = c.execute('select count(*) from place').fetchone()[0]
    c.execute("insert or replace into meta values('row_count',?)", (str(after),))
    c.commit()
    ic = c.execute('pragma integrity_check').fetchone()[0]
    sc = c.execute('select count(*) from search').fetchone()[0]
    orph = c.execute('select count(*) from search where rowid not in (select id from place)').fetchone()[0]
    c.execute('vacuum'); c.close()
    print(n, before, '->', after, 'search', sc, 'orphan', orph, 'integrity', ic, os.path.getsize(dst))
