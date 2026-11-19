# -*- coding: utf-8 -*-
"""人工建檔表（data/curated/*.csv）的讀取、檢查與合併。人工資料優先於自動抓取。

每列必填（空白不行；不適用的欄填「-」）：lat、lon、bearing（方向角或 -）、direction（one／both／-）、type、limit_kmh、len_m、
source（來源說明）、source_url（網址或現地照片編號）、checked_date（YYYY-MM-DD）、checker（查核人）、status。
status：active＝使用中；suspended＝暫停（官方註記維護中，App 預設不提醒）；retired＝已撤除（從發布資料移除）。
ref_id：要覆蓋或撤除的自動資料 id（測速點 id、常出沒路段「縣市|行政區|路名」、時制路口 id）；空白＝新增一筆。

四張表：
- section-ends.csv       區間測速終點（type=section_end，ref_id＝區間起點 id）：補長度與終點座標
- redlight-additions.csv 闖紅燈照相補點（type=redlight／redlight_speed）
- mobile-segments.csv    移動式測速常出沒路段（type=mobile_hotspot；city、district、road 欄）
- signal-timing.csv      路口秒數（type=signal_timing；cycle_s、red_s、days、time_from、time_to 欄）
"""
import csv
import io
import json
import os
import re

COMMON = ['id', 'ref_id', 'lat', 'lon', 'bearing', 'direction', 'type', 'limit_kmh', 'len_m', 'road',
          'source', 'source_url', 'checked_date', 'checker', 'status', 'note']
REQUIRED = ['lat', 'lon', 'bearing', 'direction', 'type', 'limit_kmh', 'len_m', 'source', 'source_url',
            'checked_date', 'checker', 'status']
TABLES = {
    'section-ends.csv': dict(types={'section_end'}, extra=[]),
    'redlight-additions.csv': dict(types={'redlight', 'redlight_speed'}, extra=[]),
    'mobile-segments.csv': dict(types={'mobile_hotspot'}, extra=['city', 'district']),
    'signal-timing.csv': dict(types={'signal_timing'}, extra=['cycle_s', 'red_s', 'days', 'time_from', 'time_to']),
}
STATUSES = {'active', 'suspended', 'retired'}
DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def header(table):
    return COMMON + TABLES[table]['extra']


def read(path):
    if not os.path.exists(path):
        return []
    text = open(path, 'rb').read().decode('utf-8-sig')
    rows = list(csv.DictReader(io.StringIO(text)))
    return [{k: (v or '').strip() for k, v in r.items() if k is not None} for r in rows
            if any((v or '').strip() for v in r.values())]


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def check_rows(table, rows):
    """回傳問題清單（每列一句），空＝通過"""
    spec = TABLES[table]
    problems = []
    seen = set()
    for i, r in enumerate(rows, start=2):
        where = '%s 第 %d 列' % (table, i)
        for k in REQUIRED + spec['extra']:
            if not r.get(k):
                problems.append('%s：%s 必填（不適用填 -）' % (where, k))
        if r.get('type') and r['type'] not in spec['types']:
            problems.append('%s：type 應為 %s' % (where, '／'.join(sorted(spec['types']))))
        if r.get('status') and r['status'] not in STATUSES:
            problems.append('%s：status 應為 active／suspended／retired' % where)
        lat, lon = _num(r.get('lat')), _num(r.get('lon'))
        if r.get('lat') and r.get('lon') and not (lat and lon and 21.5 <= lat <= 26.5 and 118 <= lon <= 123):
            problems.append('%s：座標不在台灣範圍' % where)
        b = r.get('bearing', '')
        if b and b != '-' and not (_num(b) is not None and 0 <= _num(b) < 360):
            problems.append('%s：bearing 應為 0–359 或 -' % where)
        if r.get('direction') and r['direction'] not in ('one', 'both', '-'):
            problems.append('%s：direction 應為 one／both／-' % where)
        if r.get('direction') == 'one' and (not b or b == '-'):
            problems.append('%s：direction=one 要有 bearing' % where)
        lim = r.get('limit_kmh', '')
        if lim and lim != '-' and not (_num(lim) and 20 <= _num(lim) <= 130):
            problems.append('%s：limit_kmh 應為 20–130 或 -' % where)
        ln = r.get('len_m', '')
        if ln and ln != '-' and not (_num(ln) and 100 <= _num(ln) <= 50000):
            problems.append('%s：len_m 應為 100–50000 或 -' % where)
        if r.get('checked_date') and not DATE_RE.match(r['checked_date']):
            problems.append('%s：checked_date 應為 YYYY-MM-DD' % where)
        if table == 'section-ends.csv' and not r.get('ref_id'):
            problems.append('%s：區間終點要填 ref_id（區間起點 id）' % where)
        if table == 'redlight-additions.csv' and r.get('type') == 'redlight_speed' and (lim in ('', '-')):
            problems.append('%s：闖紅燈兼超速要有速限' % where)
        if table == 'signal-timing.csv' and r.get('status') == 'active':
            c, red = _num(r.get('cycle_s')), _num(r.get('red_s'))
            if not (c and red and 20 <= c <= 300 and 0 < red < c):
                problems.append('%s：cycle_s 20–300、red_s 要小於 cycle_s' % where)
        key = r.get('id') or r.get('ref_id')
        if key:
            if key in seen:
                problems.append('%s：id／ref_id %s 重複' % (where, key))
            seen.add(key)
    return problems


def load_all(curated_dir):
    """{表名: rows}，並回傳全部問題"""
    out, problems = {}, []
    for table in TABLES:
        rows = read(os.path.join(curated_dir, table))
        out[table] = rows
        problems += check_rows(table, rows)
    return out, problems


# ------------------------------------------------------------------ 合併

def _limit(r):
    v = _num(r.get('limit_kmh'))
    return int(v) if v else None


def _bearing(r):
    if r.get('direction') != 'one':
        return None
    v = _num(r.get('bearing'))
    return int(round(v)) % 360 if v is not None else None


def merge_speedcams(doc, tables):
    """doc：打包後的 speedcams.json（App 格式）。回傳 (新 doc, 統計)。"""
    cams = {c['i']: dict(c) for c in doc['cameras']}
    st = dict(added=0, updated=0, retired=0, suspended=0, section_ends=0, missing_ref=[])
    for r in tables.get('redlight-additions.csv', []):
        target = r.get('ref_id') or ''
        cid = target if target else 'cur-' + (r.get('id') or ('%s,%s' % (r['lat'], r['lon'])))
        if r['status'] == 'retired':
            if cams.pop(cid, None) is not None:
                st['retired'] += 1
            elif target:
                st['missing_ref'].append(target)
            continue
        if target and target not in cams:
            st['missing_ref'].append(target)
            continue
        item = cams.get(cid, {'i': cid})
        item.update(la=round(float(r['lat']), 6), lo=round(float(r['lon']), 6),
                    t='rs' if r['type'] == 'redlight_speed' else 'r')
        lim, b = _limit(r), _bearing(r)
        item.pop('s', None) if lim is None else item.update(s=lim)
        item.pop('b', None) if b is None else item.update(b=b)
        if r.get('road'):
            a = r['road']
            item['a'] = a if len(a) <= 24 else a[:23] + '…'
        if r['status'] == 'suspended':
            item['st'] = 'sus'
            st['suspended'] += 1
        else:
            item.pop('st', None)
        st['updated' if cid in cams else 'added'] += 1
        cams[cid] = item
    for r in tables.get('section-ends.csv', []):
        start = cams.get(r['ref_id'])
        if start is None:
            st['missing_ref'].append(r['ref_id'])
            continue
        if r['status'] == 'retired':
            if cams.pop(r['ref_id'], None) is not None:
                st['retired'] += 1
            continue
        ln = _num(r.get('len_m'))
        if ln:
            start['len'] = int(round(ln))
            start['lenSrc'] = 'official'
        start['e'] = [round(float(r['lat']), 6), round(float(r['lon']), 6)]
        if r['status'] == 'suspended':
            start['st'] = 'sus'
            st['suspended'] += 1
        st['section_ends'] += 1
    out = dict(doc)
    out['cameras'] = sorted(cams.values(), key=lambda c: c['i'])
    out['count'] = len(out['cameras'])
    return out, st


def hotspot_key(s):
    return '%s|%s|%s' % (s.get('c', ''), s.get('d') or '', s.get('r', ''))


def merge_hotspots(doc, tables):
    spots = {hotspot_key(s): dict(s) for s in doc['spots']}
    st = dict(added=0, updated=0, retired=0, missing_ref=[])
    for r in tables.get('mobile-segments.csv', []):
        city = r['city'].replace('臺', '台')
        new = dict(c=city, r=r['road'], p=[[round(float(r['lat']), 5), round(float(r['lon']), 5)]])
        if r.get('district') and r['district'] != '-':
            new['d'] = r['district']
        if r.get('note') and r['note'] != '-':
            new['x'] = r['note']
        key = r.get('ref_id') or hotspot_key(new)
        if r['status'] == 'retired':
            if spots.pop(key, None) is not None:
                st['retired'] += 1
            else:
                st['missing_ref'].append(key)
            continue
        if r['status'] == 'suspended':
            spots.pop(key, None)
            continue
        st['updated' if key in spots else 'added'] += 1
        spots[key] = new
    out = dict(doc)
    out['spots'] = sorted(spots.values(), key=lambda s: (s['c'], s.get('d') or '', s['r']))
    return out, st


def merge_timing(doc, tables):
    juncs = {j['id']: dict(j) for j in doc['junctions']}
    st = dict(added=0, updated=0, retired=0)
    for r in tables.get('signal-timing.csv', []):
        jid = r.get('ref_id') or 'cur-' + (r.get('id') or '%s,%s' % (r['lat'], r['lon']))
        if r['status'] in ('retired', 'suspended'):
            if juncs.pop(jid, None) is not None:
                st['retired'] += 1
            continue
        days = r.get('days', '-')
        mask = 127 if days in ('-', '', 'all') else int(days)
        def minutes(t, default):
            if not t or t == '-':
                return default
            h, m = t.split(':')
            return int(h) * 60 + int(m)
        plan = dict(w=mask, s=minutes(r.get('time_from'), 0), e=minutes(r.get('time_to'), 1440),
                    c=int(float(r['cycle_s'])), r=[int(float(r['red_s']))])
        st['updated' if jid in juncs else 'added'] += 1
        j = juncs.get(jid, dict(id=jid, la=round(float(r['lat']), 6), lo=round(float(r['lon']), 6), p=[]))
        j['p'] = [plan] + [p for p in j.get('p', []) if p['w'] & mask == 0 or p['s'] >= plan['e'] or p['e'] <= plan['s']]
        j['src'] = 'curated'
        juncs[jid] = j
    out = dict(doc)
    out['junctions'] = sorted(juncs.values(), key=lambda j: j['id'])
    return out, st


def write_empty_tables(curated_dir):
    """沒有表時建立只有表頭的 CSV（UTF-8 BOM，Excel 直接開）"""
    os.makedirs(curated_dir, exist_ok=True)
    for table in TABLES:
        path = os.path.join(curated_dir, table)
        if not os.path.exists(path):
            with open(path, 'w', encoding='utf-8-sig', newline='') as f:
                csv.writer(f, lineterminator='\n').writerow(header(table))


def dumps(doc):
    return (json.dumps(doc, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
