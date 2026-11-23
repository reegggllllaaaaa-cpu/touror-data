# -*- coding: utf-8 -*-
"""官方速限點層 taiwan-speedlimits.bin（省道速限標誌牌面，交通部公路局，政府資料開放平臺 105021）。只用標準庫。

用法（工作區；資料/速限/ 底下執行或任何目錄皆可）：
  python -I 建置_官方速限.py --locate <signs_locate.json>   # 有 Valhalla locate 結果：對位到道路、更新 牌面對位.json、輸出 bin
  python -I 建置_官方速限.py                                 # 沒有 locate（資料發布管線）：只用 牌面對位.json 已對位的牌面
  選項：--signs <省道速限圖資.csv> --kmfile <省道里程坐標.csv> --out <bin> --copy-to <App/Resources/...bin> --version yyyymmdd
  資料日期（標頭 20、meta.version）預設＝牌面最新的調查日期（同一份原始檔重建結果位元組相同，發布管線才不會每天都當成有變更）。
locate 檔：驗證/audit-1 的 compare_official_limits.py prep → WSL vh_locate.py work/signs_pts.json work/signs_locate.json 40
（id＝p<CSV 列序>，每面牌 40 m 內的所有路段，含 heading、names、吸附點）。

規則（NAV-SPEEDLIMIT-OFFICIAL-1，2026-11-20）：
  - 收：牌面內容 10–120 的速限牌（種類全為「禁制」、性質全為「限5」最高速限）。
  - 不收：備註寫明只管大型車／大貨車／聯結車／公車／貨車／噸位、工區臨時牌、可變標誌、無設施、非速限備註；
    牌面方向空白；現況寫拆除／撤除／遺失／廢除（實際檔只有 良好／尚佳／陳舊／損壞／空白，都是現場還在的牌，全部收）。
  - 方向：順向＝里程增加方向、逆向＝里程減少方向、雙向＝兩筆。目標方位＝7040 里程牌在該里程往前／後 100 m 的方位。
  - 對位：locate 40 m 內、同路線編號的路段中，方位與目標差 ≤ 60° 的最近一條（沒有同編號路段時，25 m 內、差 ≤ 45° 的任何路段）；
    座標改成吸附點、方位改成該路段方位。對不到的不收（列在 meta 的 unmatched）。
  - 旗標：1＝快車道、2＝慢車道／機車道、4＝側車道、8＝匝道（App 同時有一般牌與車道牌時優先一般牌）。

bin 格式（little-endian；與 taiwan-signals.bin 同樣的 24 bytes 標頭位置，DataUpdate 讀筆數與日期共用）：
  0  4s  magic "TSPL"      4  u16 版本 1        6  u16 標頭長度 24     8  u32 筆數 n
  12 u16 每筆長度 12       14 u16 保留 0        16 u32 CRC32（24 之後全部）  20 u32 資料日期（台灣時間當天 00:00 的 Unix 秒）
  每筆 12 bytes：i32 緯度×1e6、i32 經度×1e6、u8 速限 km/h、u8 方位（度×256/360，四捨五入）、u16（低 12 位元＝路線索引、高 4 位元＝旗標）
  之後：u16 路線數，每條 u8 長度＋UTF-8（例「台1」「台61」「台9甲」）。筆數依緯度、經度、方位排序（位元組可重現）。
"""
import argparse
import csv
import datetime
import io
import json
import math
import os
import re
import struct
import sys
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))
RAW = os.path.join(os.path.dirname(HERE), '測速', '原始', '省道')
SNAP = os.path.join(HERE, '牌面對位.json')
META = os.path.join(HERE, 'meta.json')
OUT = os.path.join(HERE, 'taiwan-speedlimits.bin')
MAGIC = b'TSPL'
HEADER = 24
RECORD = 12
R_EARTH = 6371000.0
LICENSE = '資料來源：交通部公路局 省道速限圖資（政府資料開放平臺 105021），依政府資料開放授權條款第1版'

EXCLUDE_NOTE = [
    (re.compile(r'大貨車|大型車|聯結車|公車|大客車|貨車|噸'), '只管大型車／公車'),
    (re.compile(r'臨時|工區'), '工區臨時牌'),
    (re.compile(r'可變'), '可變標誌'),
    (re.compile(r'無設施'), '無設施'),
    (re.compile(r'禁止停車|停機車格'), '備註不是速限'),
]
EXCLUDE_STATE = re.compile(r'拆除|撤除|遺失|廢除')
FLAG_FAST, FLAG_SLOW, FLAG_SIDE, FLAG_RAMP = 1, 2, 4, 8


def decode(b):
    for enc in ('utf-8-sig', 'cp950', 'big5'):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode('cp950', errors='replace')


def norm_route(s):
    s = str(s or '').strip().replace('臺', '台')
    m = re.search(r'(\d+)\s*([甲乙丙丁戊己庚])?', s)
    return (m.group(1) + (m.group(2) or '')) if m else ''


def parse_km(s):
    t = str(s or '').strip().upper().replace('＋', '+').replace(' ', '')
    m = re.search(r'(\d+(?:\.\d+)?)K\+?(\d+(?:\.\d+)?)?', t)
    if not m:
        return None
    return float(m.group(1)) + (float(m.group(2)) / 1000.0 if m.group(2) else 0.0)


def parse_limit(s):
    m = re.fullmatch(r'\s*(\d{2,3})\s*', str(s or ''))
    v = int(m.group(1)) if m else None
    return v if v and 10 <= v <= 120 else None


def read_csv(path):
    return list(csv.DictReader(io.StringIO(decode(open(path, 'rb').read()))))


def hav(a, b, c, d):
    la1, la2 = math.radians(a), math.radians(c)
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(math.radians(d - b) / 2) ** 2
    return 2 * R_EARTH * math.asin(min(1.0, math.sqrt(h)))


def bearing(a, b, c, d):
    la1, la2 = math.radians(a), math.radians(c)
    dl = math.radians(d - b)
    y = math.sin(dl) * math.cos(la2)
    x = math.cos(la1) * math.sin(la2) - math.sin(la1) * math.cos(la2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def angle_diff(a, b):
    d = abs(a - b) % 360
    return min(d, 360 - d)


def km_index(rows):
    idx = {}
    for r in rows:
        route, km = norm_route(r.get('公路編號')), parse_km(r.get('起點樁號'))
        try:
            la, lo = float(r['坐標-N-WGS84']), float(r['坐標-E-WGS84'])
        except (KeyError, TypeError, ValueError):
            continue
        if route and km is not None:
            idx.setdefault(route, []).append((km, la, lo))
    for v in idx.values():
        v.sort()
    return idx


def interp(idx, route, km, max_gap=3.0):
    pts = idx.get(route)
    if not pts or km < pts[0][0] or km > pts[-1][0]:
        return None
    for i in range(1, len(pts)):
        k0, a0, o0 = pts[i - 1]
        k1, a1, o1 = pts[i]
        if k0 <= km <= k1:
            if k1 - k0 > max_gap:
                return None
            t = 0 if k1 == k0 else (km - k0) / (k1 - k0)
            return a0 + (a1 - a0) * t, o0 + (o1 - o0) * t
    return None


def target_bearing(idx, route, km, sign):
    """route 在 km 處往里程增加（sign=+1）或減少（-1）的方位；里程牌間隔太大或兩點太近回 None"""
    for step in (0.1, 0.2, 0.4):
        p, q = interp(idx, route, km), interp(idx, route, km + sign * step)
        if p and q and hav(p[0], p[1], q[0], q[1]) >= 20:
            return bearing(p[0], p[1], q[0], q[1])
    return None


def flags_of(note):
    f = 0
    if re.search(r'快車道', note) and not re.search(r'快慢', note):
        f |= FLAG_FAST
    if re.search(r'慢車道|機車道|機慢', note):
        f |= FLAG_SLOW
    if '側車道' in note:
        f |= FLAG_SIDE
    if '匝道' in note:
        f |= FLAG_RAMP
    return f


def sign_key(r, d):
    # 同一路線、編號、樁號的牌偶有兩面（左右側）：加原始座標區分
    return '%s|%s|%s|%s|%s|%s' % (norm_route(r.get('公路編號')), (r.get('編號') or '').strip(), (r.get('起點樁號') or '').strip(), d,
                                  (r.get('坐標-N-WGS84') or '').strip()[:9], (r.get('坐標-E-WGS84') or '').strip()[:10])


def select(rows):
    """回傳 (候選 [(列序, row, 方向 +1/-1)], 排除統計)"""
    from collections import Counter
    out, excl = [], Counter()
    for i, r in enumerate(rows):
        lim = parse_limit(r.get('牌面內容'))
        note = (r.get('備註') or '').strip()
        if lim is None:
            excl['牌面內容不是 10–120'] += 1
            continue
        if EXCLUDE_STATE.search(r.get('現況') or ''):
            excl['現況已拆除'] += 1
            continue
        why = next((w for p, w in EXCLUDE_NOTE if p.search(note)), None)
        if why:
            excl[why] += 1
            continue
        d = (r.get('牌面方向') or '').strip()
        dirs = {'順向': (1,), '逆向': (-1,), '雙向': (1, -1)}.get(d)
        if not dirs:
            excl['牌面方向空白'] += 1
            continue
        for s in dirs:
            out.append((i, r, s))
    return out, excl


def match(r, s, edges, idx):
    """locate 路段 → (lat, lon, bearing, 方法) 或 None"""
    route = norm_route(r.get('公路編號'))
    km = parse_km(r.get('起點樁號'))
    tgt = target_bearing(idx, route, km, s) if km is not None else None
    if tgt is None:
        return None
    es = [e for e in edges if e.get('heading') is not None and e.get('distance') is not None and e.get('lat') is not None
          and e.get('use') != 'ferry' and e['distance'] <= 40]
    same = [e for e in es if route in (e.get('names') or []) and angle_diff(e['heading'], tgt) <= 60]
    if same:
        e = min(same, key=lambda e: (e['distance'], angle_diff(e['heading'], tgt)))
        return e['lat'], e['lon'], e['heading'], 'route'
    other = [e for e in es if e['distance'] <= 25 and angle_diff(e['heading'], tgt) <= 45]
    if other:
        e = min(other, key=lambda e: (e['distance'], angle_diff(e['heading'], tgt)))
        return e['lat'], e['lon'], e['heading'], 'near'
    return None


def write_bin(recs, routes, date):
    body = bytearray()
    for la, lo, kmh, brg, ri, fl in recs:
        body += struct.pack('<iiBBH', int(round(la * 1e6)), int(round(lo * 1e6)), kmh,
                            int(round(brg * 256 / 360)) % 256, (ri & 0x0FFF) | ((fl & 0xF) << 12))
    body += struct.pack('<H', len(routes))
    for name in routes:
        b = name.encode('utf-8')
        body += struct.pack('<B', len(b)) + b
    ts = int(datetime.datetime.strptime(date, '%Y%m%d').replace(
        tzinfo=datetime.timezone(datetime.timedelta(hours=8))).timestamp())
    head = struct.pack('<4sHHIHHII', MAGIC, 1, HEADER, len(recs), RECORD, 0, zlib.crc32(bytes(body)) & 0xFFFFFFFF, ts)
    return head + bytes(body)


def read_bin(data):
    """驗證用讀取器（與 App OfficialSpeedLimits.swift 同格式）：回傳 (標頭 dict, [(lat, lon, kmh, bearing, route, flags)])"""
    magic, ver, hlen, n, rlen, _, crc, ts = struct.unpack_from('<4sHHIHHII', data, 0)
    if magic != MAGIC or ver != 1 or rlen != RECORD:
        raise ValueError('taiwan-speedlimits.bin 格式不符')
    if zlib.crc32(data[hlen:]) & 0xFFFFFFFF != crc:
        raise ValueError('taiwan-speedlimits.bin CRC 不符')
    off = hlen + n * rlen
    (nr,) = struct.unpack_from('<H', data, off)
    off += 2
    routes = []
    for _ in range(nr):
        ln = data[off]
        routes.append(data[off + 1:off + 1 + ln].decode('utf-8'))
        off += 1 + ln
    out = []
    for i in range(n):
        la, lo, kmh, b, rf = struct.unpack_from('<iiBBH', data, hlen + i * rlen)
        out.append((la / 1e6, lo / 1e6, kmh, b * 360.0 / 256, routes[rf & 0x0FFF], rf >> 12))
    return dict(count=n, ts=ts, routes=len(routes)), out


def survey_date(rows):
    """牌面調查日期（「2016/10/5」）→ 最新一天 yyyymmdd 與範圍"""
    ds = []
    for r in rows:
        m = re.match(r'(\d{4})/(\d{1,2})/(\d{1,2})', (r.get('調查日期') or '').strip())
        if m:
            ds.append('%s%02d%02d' % (m.group(1), int(m.group(2)), int(m.group(3))))
    ds.sort()
    return (ds[-1] if ds else '20000101'), ds


def build(signs_path, km_path, locate_path, out_path, version=None):
    from collections import Counter
    rows = read_csv(signs_path)
    latest, dates = survey_date(rows)
    version = version or latest
    idx = km_index(read_csv(km_path))
    cand, excl = select(rows)
    snap = json.load(open(SNAP, encoding='utf-8')) if os.path.exists(SNAP) else {}
    loc = json.load(open(locate_path, encoding='utf-8')) if locate_path else None
    stats = Counter()
    unmatched = []
    new_snap = {}
    recs = []
    for i, r, s in cand:
        key = sign_key(r, '+' if s > 0 else '-')
        hit = None
        if loc is not None:
            m = match(r, s, (loc.get('p%d' % i) or {}).get('edges', []), idx)
            if m:
                hit = [round(m[0], 6), round(m[1], 6), round(m[2], 1), m[3]]
        elif key in snap:
            hit = snap[key]
        if not hit:
            stats['對不到道路'] += 1
            if len(unmatched) < 400:
                unmatched.append(dict(route='台' + norm_route(r.get('公路編號')), km=r.get('起點樁號'), dir=r.get('牌面方向'),
                                      limit=r.get('牌面內容'), note=(r.get('備註') or '')[:20]))
            continue
        new_snap[key] = hit
        stats['收錄（%s）' % ('同路線' if hit[3] == 'route' else '附近路段')] += 1
        recs.append((hit[0], hit[1], parse_limit(r['牌面內容']), hit[2], '台' + norm_route(r.get('公路編號')),
                     flags_of(r.get('備註') or '')))
    if loc is not None:
        json.dump(dict(sorted(new_snap.items())), open(SNAP, 'w', encoding='utf-8', newline='\n'), ensure_ascii=False,
                  separators=(',', ':'))
    routes = sorted({x[4] for x in recs}, key=lambda s: (int(re.match(r'台(\d+)', s).group(1)), s))
    rix = {name: k for k, name in enumerate(routes)}
    recs.sort(key=lambda x: (round(x[0], 6), round(x[1], 6), round(x[3], 1), x[2]))
    packed = [(la, lo, kmh, b, rix[rt], fl) for la, lo, kmh, b, rt, fl in recs]
    data = write_bin(packed, routes, version)
    head, back = read_bin(data)
    assert head['count'] == len(recs)
    open(out_path, 'wb').write(data)
    meta = dict(name='官方速限點（省道速限標誌牌面）', version=version, count=len(recs), bytes=len(data), license=LICENSE,
                source=dict(title='省道速限圖資', dataset='105021', url='https://data.gov.tw/dataset/105021',
                            survey_date_range=[dates[0] if dates else '', dates[-1] if dates else '']),
                rows=len(rows), candidates=len(cand), excluded=dict(excl), stats=dict(stats),
                limits=dict(Counter(x[2] for x in recs).most_common()),
                flags=dict(Counter(x[5] for x in recs).most_common()),
                unmatched_sample=unmatched[:100])
    return meta, data


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--signs', default=os.path.join(RAW, '省道速限圖資.csv'))
    ap.add_argument('--kmfile', default=os.path.join(RAW, '省道里程坐標(里程牌標誌).csv'))
    ap.add_argument('--locate')
    ap.add_argument('--out', default=OUT)
    ap.add_argument('--copy-to')
    ap.add_argument('--meta', default=META)
    ap.add_argument('--version', help='資料日期 yyyymmdd（預設牌面最新調查日期）')
    a = ap.parse_args(argv)
    meta, data = build(a.signs, a.kmfile, a.locate, a.out, a.version)
    json.dump(meta, open(a.meta, 'w', encoding='utf-8', newline='\n'), ensure_ascii=False, indent=1)
    if a.copy_to:
        open(a.copy_to, 'wb').write(data)
    print('官方速限點 %d 筆（%d bytes）；排除 %s；%s' % (meta['count'], meta['bytes'], meta['excluded'], meta['stats']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
