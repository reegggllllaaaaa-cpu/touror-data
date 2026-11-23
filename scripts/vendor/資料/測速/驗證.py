# -*- coding: utf-8 -*-
"""驗證 speedcams.json / speedcams.csv / meta.json（只用標準庫）。用法：python -I 驗證.py
全部通過結束碼 0，否則 1。"""
import csv, json, math, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
REQ = ['id', 'lat', 'lon', 'bearing', 'limit_kmh', 'type', 'source', 'city', 'address', 'status']
TYPES = {'fixed', 'redlight', 'redlight_speed', 'section', 'unknown'}
STATUSES = {'active', 'suspended', 'retired', 'merged'}
LEN_SRCS = {'text', 'official', 'est', 'unverified'}
results = []


def check(name, ok, detail=''):
    results.append((name, bool(ok), detail))


data = json.load(open(os.path.join(HERE, 'speedcams.json'), encoding='utf-8'))
meta = json.load(open(os.path.join(HERE, 'meta.json'), encoding='utf-8'))
with open(os.path.join(HERE, 'speedcams.csv'), encoding='utf-8-sig', newline='') as f:
    csv_rows = list(csv.DictReader(f))

check('JSON 是陣列且非空', isinstance(data, list) and len(data) > 0, 'n=%d' % len(data))
check('筆數合理（1500–5000）', 1500 <= len(data) <= 5000, 'n=%d' % len(data))
check('meta.count 等於 JSON 筆數', meta.get('count') == len(data), 'meta=%s' % meta.get('count'))
check('CSV 筆數等於 JSON 筆數', len(csv_rows) == len(data), 'csv=%d' % len(csv_rows))

missing = [d.get('id') for d in data if any(k not in d for k in REQ)]
check('每筆都有必要欄位', not missing, '缺欄位 %d 筆' % len(missing))

bad_id = [d.get('id') for d in data if not isinstance(d.get('id'), str) or not d['id']]
ids = [d.get('id') for d in data]
check('id 為非空字串', not bad_id, '%d 筆' % len(bad_id))
check('id 無重複', len(ids) == len(set(ids)), '重複 %d' % (len(ids) - len(set(ids))))

bad_coord = [d['id'] for d in data if not (isinstance(d['lat'], (int, float)) and isinstance(d['lon'], (int, float))
                                          and 21.5 <= d['lat'] <= 26.5 and 118 <= d['lon'] <= 123)]
check('座標在台灣範圍（緯 21.5–26.5、經 118–123）', not bad_coord, '%d 筆 %s' % (len(bad_coord), bad_coord[:5]))

bad_lim = [d['id'] for d in data if not (d['limit_kmh'] is None or
                                         (isinstance(d['limit_kmh'], int) and 20 <= d['limit_kmh'] <= 110))]
check('limit_kmh 為 20–110 整數或 null', not bad_lim, '%d 筆 %s' % (len(bad_lim), bad_lim[:5]))

bad_b = [d['id'] for d in data if not (d['bearing'] is None or
                                       (isinstance(d['bearing'], (int, float)) and 0 <= d['bearing'] < 360))]
check('bearing 為 0–359 或 null', not bad_b, '%d 筆' % len(bad_b))

bad_t = [d['id'] for d in data if d['type'] not in TYPES]
check('type 在 fixed|redlight|redlight_speed|section|unknown', not bad_t, '%d 筆' % len(bad_t))

# 狀態（NAV-CAM-DATA-2）：active／suspended／retired／merged；merged 要指到存在、使用中的點；retired／suspended 要寫原因
bad_st = [d['id'] for d in data if d.get('status') not in STATUSES]
check('status 在 active|suspended|retired|merged', not bad_st, '%d 筆 %s' % (len(bad_st), bad_st[:5]))
by_id = {d['id']: d for d in data}
bad_merge = [d['id'] for d in data if d.get('status') == 'merged'
             and by_id.get(d.get('merged_into'), {}).get('status') not in ('active', 'suspended')]
check('merged 都指到使用中的點', not bad_merge, '%d 筆 %s' % (len(bad_merge), bad_merge[:5]))
bad_note = [d['id'] for d in data if d.get('status') in ('retired', 'suspended', 'merged') and not d.get('status_note')]
check('retired／suspended／merged 都有 status_note', not bad_note, '%d 筆 %s' % (len(bad_note), bad_note[:5]))
rs_nolim = [d['id'] for d in data if d['type'] == 'redlight_speed' and d['limit_kmh'] is None]
check('redlight_speed（闖紅燈兼超速）都有速限', not rs_nolim, '%d 筆 %s' % (len(rs_nolim), rs_nolim[:5]))


def _dist(a, b):
    r = 6371008.8
    p1, p2 = math.radians(a['lat']), math.radians(b['lat'])
    dl = math.radians(b['lon'] - a['lon'])
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# 重複：使用中、同類型、同方向（方位角相同或都沒有、拍攝方向原文相同）、同速限、10 公尺內 → 重複
#（同座標「往長庚／往大埔」這種兩個方向各一筆的不算）
live = [d for d in data if d.get('status') in ('active', 'suspended')]
cells = {}
for d in live:
    cells.setdefault((int(d['lat'] / 0.001), int(d['lon'] / 0.001)), []).append(d)
dups = []
for d in live:
    ki, kj = int(d['lat'] / 0.001), int(d['lon'] / 0.001)
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            for o in cells.get((ki + di, kj + dj), ()):
                if o['id'] <= d['id'] or o['type'] != d['type'] or o['bearing'] != d['bearing'] \
                        or o['limit_kmh'] != d['limit_kmh'] or o.get('direct_raw') != d.get('direct_raw'):
                    continue
                if _dist(d, o) <= 10:
                    dups.append((d['id'], o['id']))
check('使用中的點沒有重複（同類型、同方向、同速限 10 公尺內）', not dups, '%d 組 %s' % (len(dups), dups[:5]))
n_red = sum(1 for d in live if d['type'] in ('redlight', 'redlight_speed'))
check('闖紅燈（含兼超速）筆數合理（≥ 300）', n_red >= 300, '%d' % n_red)

src_keys = {s['key'] for s in meta.get('sources', [])}
bad_s = [d['id'] for d in data if d['source'] not in src_keys]
check('source 都列在 meta.sources', not bad_s, '%d 筆' % len(bad_s))

empty_txt = [d['id'] for d in data if not str(d['city']).strip() or not str(d['address']).strip()]
check('city／address 非空', not empty_txt, '%d 筆 %s' % (len(empty_txt), empty_txt[:5]))

dir_ok = [d['id'] for d in data if 'direction' in d and
          ((d['direction'] == 'one') != (d['bearing'] is not None))]
check('direction=one 與 bearing 有值一致', not dir_ok, '%d 筆' % len(dir_ok))

# 區間長度（NAV-CAM-HUD-1、NAV-CAM-DATA-2）：只有區間點有 len 欄位；每筆都有 len_src（text／official／est／unverified）；
# text、official、unverified 一定有長度（est 可有可無：有＝估計值）；official 一定有 len_ref 與 len_date；長度 200 m–20 km
secs = [d for d in data if d['type'] == 'section']
no_src = [d['id'] for d in secs if d.get('len_src') not in LEN_SRCS]
check('每個區間點都有 len_src（text|official|est|unverified）', not no_src, '%d 筆 %s' % (len(no_src), no_src[:5]))
bad_len = [d['id'] for d in secs if d.get('len_src') in ('text', 'official', 'unverified') and d.get('len_m') is None
           or (d.get('len_m') is not None and not (200 <= d['len_m'] <= 20000))]
check('區間點 len_m／len_src 一致且長度 200–20000 公尺', not bad_len, '%d 筆 %s' % (len(bad_len), bad_len[:5]))
no_ref = [d['id'] for d in secs if d.get('len_src') == 'official' and not (d.get('len_ref') and d.get('len_date'))]
check('official 長度都有 len_ref（出處）與 len_date', not no_ref, '%d 筆 %s' % (len(no_ref), no_ref[:5]))
unv = [d['id'] for d in secs if d.get('len_src') == 'unverified' and not d.get('status_note')]
check('unverified 長度都在 status_note 寫明待確認', not unv, '%d 筆' % len(unv))
extra_len = [d['id'] for d in data if d['type'] != 'section' and ('len_m' in d or 'len_src' in d)]
check('非區間點沒有 len 欄位', not extra_len, '%d 筆' % len(extra_len))
live_secs = [d for d in secs if d.get('status') in ('active', 'suspended')]
n_known = sum(1 for d in live_secs if d.get('len_src') in ('text', 'official'))
check('區間長度已知筆數合理（≥ 使用中區間點的 80%）', live_secs and n_known * 5 >= len(live_secs) * 4,
      '%d／%d' % (n_known, len(live_secs)))
bad_pending = [p.get('id') for p in meta.get('pending', []) if not (p.get('id') and p.get('address') and p.get('len_m'))]
check('meta.pending（缺座標的區間）欄位完整', not bad_pending, '%d 筆' % len(bad_pending))
check('meta revision 為正整數', isinstance(meta.get('revision', 1), int) and meta.get('revision', 1) >= 1,
      str(meta.get('revision')))
csv_len = {r['id']: r.get('len_m', '') for r in csv_rows}
bad_csv_len = [d['id'] for d in secs if str(csv_len.get(d['id'], '')) != ('' if d.get('len_m') is None else str(d['len_m']))]
check('CSV len_m 與 JSON 一致', not bad_csv_len, '%d 筆' % len(bad_csv_len))

# 資料可信度（NAV-CAM-AUDIT-FIX-1；驗證/audit-1/資料可信度規格.md）
CONFS = {'official', 'official-text', 'osm', 'crowd-confirmed', 'inferred', 'unverified'}
bad_conf = [d['id'] for d in data if d.get('conf') not in CONFS]
check('每筆都有 conf（official|official-text|osm|crowd-confirmed|inferred|unverified）', not bad_conf,
      '%d 筆 %s' % (len(bad_conf), bad_conf[:5]))
bad_unv = [d['id'] for d in data if d.get('conf') == 'unverified' and d.get('status') in ('active',)
           and not (d.get('audit_flags') and d.get('status_note'))]
check('unverified（使用中）都有 audit_flags 與 status_note 說明', not bad_unv, '%d 筆 %s' % (len(bad_unv), bad_unv[:5]))
bad_sus = [d['id'] for d in data if d.get('status') in ('suspended', 'retired') and d.get('conf') != 'unverified']
check('維護中／汰除的點 conf＝unverified', not bad_sus, '%d 筆 %s' % (len(bad_sus), bad_sus[:5]))
bad_ot = [d['id'] for d in data if d.get('coord_src') and d.get('conf') == 'official']
check('座標經換算／吸附（coord_src）的點不是 official', not bad_ot, '%d 筆 %s' % (len(bad_ot), bad_ot[:5]))
bad_moved = [d['id'] for d in data if d.get('coord_src') and not d.get('derived_from') and 'orig_lat' not in d]
check('改過座標的點都留原座標 orig_lat／orig_lon', not bad_moved, '%d 筆 %s' % (len(bad_moved), bad_moved[:5]))
bad_chk = [d['id'] for d in data if (d.get('conf') == 'unverified') == bool(d.get('checked'))]
check('checked（稽核通過日）只寫在非 unverified 的點', not bad_chk, '%d 筆' % len(bad_chk))
split = [d for d in data if 'section_split' in (d.get('audit_flags') or [])]
bad_split = [d['id'] for d in split if d['direction'] != 'one' or d['bearing'] is None]
check('依里程拆方向的區間都是單向且有方位', not bad_split, '%d 筆 %s' % (len(bad_split), bad_split[:5]))
shared = [d['id'] for d in live if d['type'] == 'section' and d['direction'] == 'both' and d.get('len_m')
          and 'section_shared_start' not in (d.get('audit_flags') or [])]
check('雙向共用一個起點的區間都標 section_shared_start（待現地確認）', not shared, '%d 筆 %s' % (len(shared), shared[:5]))
n_unv = sum(1 for d in live if d.get('conf') == 'unverified')
check('unverified 筆數合理（≤ 使用中的 5%）', n_unv * 20 <= len(live), '%d／%d' % (n_unv, len(live)))

check('meta 授權文字正確',meta.get('license') == '資料來源：政府資料開放平臺，依政府資料開放授權條款第1版')
check('meta 版本號＝下載日期', meta.get('version') == str(meta.get('download_date', '')).replace('-', ''),
      '%s / %s' % (meta.get('version'), meta.get('download_date')))

fails = [r for r in results if not r[1]]
for name, ok, detail in results:
    print('[%s] %s %s' % ('通過' if ok else '失敗', name, ('(' + detail + ')') if detail else ''))
print('合計 %d 項，通過 %d，失敗 %d' % (len(results), len(results) - len(fails), len(fails)))
sys.exit(1 if fails else 0)
