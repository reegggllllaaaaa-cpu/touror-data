# -*- coding: utf-8 -*-
"""用本機 Valhalla 取實際路線，對每個轉彎列出錨點（touror_junction_anchors.sqlite）與擬播報句（不含任何距離／時間）。
在 WSL 執行（pyvalhalla）：
  wsl -u root /root/v2venv/bin/python3 /mnt/c/.../scripts/anchors/route_report.py <anchors.sqlite> [tiles_dir]
句型依 docs/地標式播報研究.md §2.1：A 數燈＋路名、B 紅綠燈＋轉角地標、C 路名路口、D 地標、E 橋／路底；最後手段「準備X轉」。
"""
import json, math, re, sqlite3, sys

ANCH = sys.argv[1]
TD = sys.argv[2] if len(sys.argv) > 2 else '/root/v2/tiles'
CN = '零一兩三四五六七八九'
CAT = {1: '加油站', 2: '便利商店', 3: '速食', 4: '賣場', 5: '銀行', 6: '郵局', 7: '學校', 8: '醫院', 9: '廟', 10: '車站', 11: '派出所'}
ROUTES = [
    ('R1 高雄光明路・松寮陸橋一帶', ('光明路三段', 22.6150, 120.4120), ('鳳屏一路', 22.6400, 120.3900)),
    ('R2 九如一路 → 建國一路', ('九如一路', 22.6450, 120.3550), ('建國一路', 22.6320, 120.3500)),
    ('R3 萬丹路 → 琉球路一帶', ('萬丹路三段', 22.6120, 120.4850), ('琉球路', 22.6143, 120.4114)),
]
TURN = {9: '靠右', 10: '右轉', 11: '右轉', 12: '迴轉', 13: '迴轉', 14: '左轉', 15: '左轉', 16: '靠左',
        26: '進圓環', 27: '出圓環', 37: '靠右', 38: '靠左'}


def dec(s, prec=6):
    f = 10 ** prec; i = 0; lat = lon = 0; out = []
    while i < len(s):
        for w in (0, 1):
            r = sh = 0
            while True:
                b = ord(s[i]) - 63; i += 1; r |= (b & 0x1f) << sh; sh += 5
                if b < 0x20: break
            d = ~(r >> 1) if r & 1 else r >> 1
            if w == 0: lat += d
            else: lon += d
        out.append((lat / f, lon / f))
    return out


def xy(lat, lon, lat0):
    return (lon * 111320 * math.cos(math.radians(lat0)), lat * 111320)


def brg(a, b):
    return math.degrees(math.atan2((b[1] - a[1]) * math.cos(math.radians(a[0])), b[0] - a[0])) % 360


def base(n):
    n = re.sub(r'[一二三四五六七八九十]+段$', '', n or '')
    m = re.fullmatch(r'(\d+)([甲乙丙丁]?)', n)
    if m:  # 公路編號：1-99 省道，100 以上縣道
        return ('台' + n + '線') if int(m.group(1)) < 100 else ('縣道' + n)
    if re.fullmatch(r'[一-鿿]\d+[甲乙丙丁]?', n):
        return n + '線'
    return '' if re.search(r'高速公路|快速道路|交流道|高架|匝道', n) else n


db = sqlite3.connect(ANCH)


def resolve(spec):
    name, lat, lon = spec
    best = None
    for la, lo in db.execute('SELECT lat_e7, lon_e7 FROM anchor WHERE (name_a=? OR name_b=?) AND lat_e7 BETWEEN ? AND ? AND lon_e7 BETWEEN ? AND ?',
                             (name, name, int((lat - .05) * 1e7), int((lat + .05) * 1e7), int((lon - .05) * 1e7), int((lon + .05) * 1e7))):
        d = (la / 1e7 - lat) ** 2 + (lo / 1e7 - lon) ** 2
        if best is None or d < best[0]:
            best = (d, la / 1e7, lo / 1e7)
    return (best[1], best[2]) if best else (lat, lon)


def project(pts, cum, P, lat0):
    px, py = xy(P[0], P[1], lat0); best = (1e9, 0)
    for i in range(len(pts) - 1):
        ax, ay = xy(*pts[i], lat0); bx, by = xy(*pts[i + 1], lat0)
        dx, dy = bx - ax, by - ay; L2 = dx * dx + dy * dy or 1e-9
        t = max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / L2))
        d = math.hypot(ax + t * dx - px, ay + t * dy - py)
        if d < best[0]: best = (d, cum[i] + t * math.sqrt(L2))
    return best


def main():
    import valhalla
    cfg = valhalla.get_config(tile_extract=TD + '/valhalla_tiles.tar', tile_dir=TD, verbose=False)
    actor = valhalla.Actor(cfg)
    out = ['', '| # | 動作 | 轉入 | 錨點（號誌信心／交會路(等級)／幾何） | 地標 | 遠層句 | 近層句 | 採用錨 |', '|---|---|---|---|---|---|---|---|']
    md = []
    for title, o, d in ROUTES:
        O, D = resolve(o), resolve(d)
        req = {'locations': [{'lat': O[0], 'lon': O[1]}, {'lat': D[0], 'lon': D[1]}], 'costing': 'motorcycle', 'language': 'zh-TW'}
        leg = json.loads(actor.route(json.dumps(req)))['trip']['legs'][0]
        pts = dec(leg['shape']); lat0 = pts[0][0]
        cum = [0.0]
        for i in range(1, len(pts)):
            a, b = xy(*pts[i - 1], lat0), xy(*pts[i], lat0); cum.append(cum[-1] + math.hypot(a[0] - b[0], a[1] - b[1]))
        la = [p[0] for p in pts]; lo = [p[1] for p in pts]
        rows = db.execute('SELECT junction_id, lat_e7, lon_e7, spoken_a, spoken_b, signal_conf, geom, best_lm, name_a, cluster_id, same_name_ambiguous FROM anchor WHERE lat_e7 BETWEEN ? AND ? AND lon_e7 BETWEEN ? AND ?',
                          (int((min(la) - .001) * 1e7), int((max(la) + .001) * 1e7), int((min(lo) - .001) * 1e7), int((max(lo) + .001) * 1e7))).fetchall()
        on = []
        for r in rows:
            dd, s = project(pts, cum, (r[1] / 1e7, r[2] / 1e7), lat0)
            if dd <= 15: on.append((s, r))
        on.sort()
        groups = []  # 路線上 30 m 內合併成一個路口
        for s, r in on:
            if groups and s - groups[-1][0] < 30 and (groups[-1][1][3], groups[-1][1][4]) == (r[3], r[4]) or (groups and s - groups[-1][0] < 12):
                g = groups[-1]; groups[-1] = (g[0], g[1] if g[1][5] >= r[5] else r, g[2] | r[6])
            else:
                groups.append((s, r, r[6]))
        md.append(f'\n### {title}\n\n起點 {O[0]:.5f},{O[1]:.5f} → 終點 {D[0]:.5f},{D[1]:.5f}（Valhalla motorcycle）；路線上路口群 {len(groups)} 個，其中號誌 {sum(1 for g in groups if g[1][5] >= 1)} 個。\n')
        md += out[1:]
        mans = leg['maneuvers']; prev_s = 0.0; k = 0
        for m in mans:
            st = cum[m['begin_shape_index']]
            if m['type'] not in TURN:
                if m['type'] not in (1, 2, 3, 4, 5, 6): prev_s = st
                continue
            k += 1; dirw = TURN[m['type']]
            road = base((m.get('street_names') or [''])[0])
            tj = min(groups, key=lambda g: abs(g[0] - st)) if groups else None
            if tj and abs(tj[0] - st) > 25: tj = None
            mid = [g for g in groups if prev_s + 20 < g[0] < st - 20]
            sig_mid = [g for g in mid if g[1][5] >= 1]
            tconf = tj[1][5] if tj else 0
            cur = base((mans[mans.index(m) - 1].get('street_names') or [''])[0]) if mans.index(m) else ''
            ids = [r[0] for s, r in on if abs(s - tj[0]) < 30] if tj else []
            cids = sorted({r[9] for s, r in on if abs(s - tj[0]) < 30}) if tj else []
            roads = db.execute('SELECT spoken, hw, alley, name FROM road WHERE cluster_id IN (%s) ORDER BY alley, CASE hw WHEN 0 THEN 9 ELSE hw END' % ','.join('?' * len(cids)), cids).fetchall() if cids else []
            names = []
            for sp, h, al, nm in roads:
                sp = nm if al else sp
                if sp and '橋' not in sp and sp not in [x[0] for x in names]: names.append((sp, h, al))
            raw = None
            if tj:
                raw = db.execute('SELECT name_spoken, score, same_brand_near, dist_dm FROM landmark WHERE junction_id IN (%s) ORDER BY score DESC LIMIT 1' % ','.join('?' * len(cids)), cids).fetchone() if cids else None
            # 交會路：排除目前行駛道路（同名路口，例：沿九如一路在九如一路路口）；轉入路在群內就用它，否則取非巷弄、等級高者，全部同名時用巷名（例：九如一路239巷）
            cands_x = [n for n, h, al in names if n != cur]
            cross = road if road in cands_x else (cands_x[0] if cands_x else '')
            amb = bool(tj and tj[1][10])
            lm = None
            if tj:
                for jid in [tj[1][0]] + [r[0] for s, r in on if abs(s - tj[0]) < 30]:
                    pass
                bl = [r[7] for s, r in on if abs(s - tj[0]) < 30 and r[7]]
                if bl:
                    lm = db.execute('SELECT name_spoken, cat, bearing, dist_dm, score FROM landmark WHERE id=?', (bl[0],)).fetchone()
            # 轉角相對位置：進入方向 vs 路口→地標方位
            corner = ''
            if lm:
                i = m['begin_shape_index']; j = max(0, i - 1)
                while j > 0 and cum[i] - cum[j] < 25: j -= 1
                rel = (lm[2] - brg(pts[j], pts[i])) % 360
                corner = ('轉彎後' if rel < 90 or rel > 270 else '轉彎前') + ('右角' if 0 < rel < 180 else '左角')
            bridge = [g for g in mid if g[2] & 2 and re.search('橋', g[1][8] or '')]
            after_bridge = bridge and not [g for g in mid if g[0] > bridge[-1][0] + 30]
            reliable = all(g[1][5] >= 2 and not g[2] & 16 for g in sig_mid)
            n = len(sig_mid)
            HWN = {0: '?', 1: '國道', 2: '快速', 3: '主要', 4: '次要', 5: '三級', 6: '一般', 7: '住宅', 8: '巷'}
            anchors = f"號誌{tconf}／{'、'.join(n + '(' + HWN[h] + ')' for n, h, al in names) or '無路名'}" + ('／同名路口' if amb else '')
            lmcell = (f'{lm[0]}（{corner}，{CAT[lm[1]]}）' if lm else ('無' + (f'（候選 {raw[0]} 未達門檻）' if raw else '')))
            anchors += '' + ('／橋後第一路口' if after_bridge else '') + (f'／中間燈{n}(可信)' if reliable else f'／中間燈{n}(有低信心)')
            tgt = road or cross
            if tconf >= 1 and lm:
                far, near_, used = f'下個紅綠燈，在{lm[0]}{dirw}{tgt}', f'在{lm[0]}{dirw}', 'B'
                if n and n <= 3: far = f'過{CN[n]}個紅綠燈，在{lm[0]}{dirw}{tgt}'; used = 'A+B'
            elif tconf >= 1 and n <= 3 and reliable:
                far = (f'過{CN[n]}個紅綠燈，{dirw}{tgt}' if n else f'下個紅綠燈{dirw}{tgt}'); near_ = f'下個紅綠燈{dirw}'; used = 'A'
            elif tconf >= 1 and n <= 3 and cross:
                far = (f'過{CN[n]}個紅綠燈，在{cross}路口{dirw}' if n else f'下個紅綠燈，{cross}路口{dirw}'); near_ = f'{cross}路口{dirw}'; used = 'A+C'
            elif cross and n >= 4 and cur:
                far, near_, used = f'沿{cur}直行，之後在{cross}路口{dirw}', f'{cross}路口{dirw}', 'C（燈多不數）'
            elif cross:
                far, near_, used = f'到{cross}路口{dirw}', f'{cross}路口{dirw}', 'C'
            elif lm:
                far, near_, used = f'{dirw}，路口有{lm[0]}', f'{lm[0]}那邊{dirw}', 'D'
            elif after_bridge:
                bn = bridge[-1][1][8]
                far, near_, used = f'過了{bn}，第一個路口{dirw}', f'下橋{dirw}', 'E'
            elif tj and tj[2] & 1:
                far, near_, used = f'走到底{dirw}', f'路底{dirw}', 'E'
            else:
                far, near_, used = f'準備{dirw}{tgt}', f'這裡{dirw}', '最後手段'
            assert not re.search(r'公尺|公里|秒|分鐘|\d+ ?(m|km)\b', far + near_), far + near_
            md.append(f"| {k} | {dirw} | {tgt or '（無名）'} | {anchors} | {lmcell} | {far} | {near_} | {used} |")
            prev_s = st
    print('\n'.join(md))


main()
