# -*- coding: utf-8 -*-
"""紅綠燈時制精簡版。

臺北（signal-timing-taipei.json，來源：臺北市政府交通局「臺北市路口號誌時制計畫」data.gov.tw 145720）：
  timing_plan_table.json（左半邊）：每個路口 icid、星期別 segmenttype（1＝週一 … 7＝週日）、各時段起點 time 與時制計畫 planid
  timing_plan.json（右半邊）：每個 icid＋planid 的週期 cycletime 與各分相 subplan（green、yellow、allred）
  路口座標 CSV：icid、經度、緯度
  紅燈秒數（該分相車流看到的紅燈）＝ 週期 − 綠燈 − 黃燈（全紅算在紅燈內）。分相總和與週期差 > 3 秒的計畫不收。

澎湖（signal-timing-penghu.json，來源：澎湖縣政府「紅綠燈號誌位置一覽表」113153，檔名 2018，可能偏舊；App 目前不讀）：
  綠燈時制「50.2」＝兩分相 50、20 秒（小數第二位被試算表省略，一位數補 0）；週期、黃燈、全紅欄；運作時段外為閃光。

輸出格式（App 端 SignalTimingTable 同規格）：
  {"version","license","source":"臺北市時制計畫","sources":[…],
   "junctions":[{"id":"I0SSA","n":"老泉街 防汛道路","la":24.97,"lo":121.55,
                 "p":[{"w":31,"s":0,"e":60,"c":30,"r":[15,15]}, …]}]}
  w＝星期遮罩（bit0＝週一 … bit6＝週日），s／e＝當天第幾分鐘 [s, e)，c＝週期秒，r＝各分相紅燈秒（依分相順序）。
  同一路口連續時段計畫相同就合併；星期別時段完全相同就合併遮罩。
"""
import csv
import io
import json
import math
import os
import re

TAIPEI_LICENSE = '資料來源：臺北市政府交通局（政府資料開放平臺 145720），依政府資料開放授權條款第1版'
PENGHU_LICENSE = '資料來源：澎湖縣政府（政府資料開放平臺 113153），依政府資料開放授權條款第1版'
PLAN_KEY = 'planid（SeqNo）'


def _decode(b):
    for enc in ('utf-8-sig', 'cp950', 'big5'):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode('utf-8', errors='replace')


def _f(s):
    try:
        v = float(str(s).strip())
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def clean_name(s):
    return re.sub(r'[\s　]+', ' ', s or '').strip()


def read_ic_coords(path):
    """icid → (lat, lon, name)"""
    text = _decode(open(path, 'rb').read())
    first = text.splitlines()[0] if text else ''
    delim = '\t' if first.count('\t') > first.count(',') else ','
    out = {}
    for d in csv.DictReader(io.StringIO(text), delimiter=delim):
        d = {(k or '').strip().lstrip('﻿'): (v or '').strip() for k, v in d.items()}
        icid = d.get('icid') or d.get('ICID')
        lat, lon = _f(d.get('緯度')), _f(d.get('經度'))
        if not icid or lat is None or lon is None:
            continue
        if not (21.5 <= lat <= 26.5 and 118 <= lon <= 123):
            if 21.5 <= lon <= 26.5 and 118 <= lat <= 123:
                lat, lon = lon, lat
            else:
                continue
        out[icid] = (lat, lon, clean_name(d.get('路口名稱', '')))
    return out


def plan_reds(plan):
    """timing_plan 一筆 → (週期, [各分相紅燈秒])；不合理回 None"""
    cycle = plan.get('cycletime') or 0
    subs = plan.get('subplan') or []
    if not (20 <= cycle <= 300) or len(subs) < 2:
        return None
    total = sum((p.get('green') or 0) + (p.get('yellow') or 0) + (p.get('allred') or 0) for p in subs)
    if abs(total - cycle) > 3:
        return None
    reds = []
    for p in subs:
        r = cycle - (p.get('green') or 0) - (p.get('yellow') or 0)
        if not (0 < r < cycle):
            return None
        reds.append(int(r))
    return int(cycle), tuple(reds)


def hhmm(t):
    t = str(t).strip().zfill(4)
    return int(t[:2]) * 60 + int(t[2:])


def day_segments(subsegments, plans_of_ic):
    """一天的時段 → [(s, e, c, reds)]；缺計畫的時段略過；相鄰相同的合併"""
    subs = sorted(subsegments, key=lambda s: hhmm(s['time']))
    out = []
    for i, s in enumerate(subs):
        start = hhmm(s['time'])
        end = hhmm(subs[i + 1]['time']) if i + 1 < len(subs) else 1440
        if end <= start:
            continue
        pr = plans_of_ic.get(str(s.get(PLAN_KEY)))
        if pr is None:
            continue
        if out and out[-1][1] == start and out[-1][2:] == pr:
            out[-1] = (out[-1][0], end) + pr
        else:
            out.append((start, end) + pr)
    return out


def build_taipei(table_path, plan_path, ic_path, version):
    table = json.load(open(table_path, encoding='utf-8-sig'))
    plans = json.load(open(plan_path, encoding='utf-8-sig'))
    coords = read_ic_coords(ic_path)
    stats = dict(table_rows=len(table), plan_rows=len(plans), ic_coords=len(coords), bad_plans=0,
                 no_coord=0, no_plan=0, junctions=0, plans_out=0)
    by_ic = {}
    for p in plans:
        pr = plan_reds(p)
        if pr is None:
            stats['bad_plans'] += 1
            continue
        by_ic.setdefault(p['icid'], {})[str(p[PLAN_KEY])] = pr
    days = {}
    for row in table:
        st = row.get('segmenttype')
        if not isinstance(st, int) or not (1 <= st <= 7):
            continue
        days.setdefault(row['icid'], {})[st] = row.get('subsegment') or []
    junctions = []
    for icid in sorted(days):
        if icid not in coords:
            stats['no_coord'] += 1
            continue
        plans_of_ic = by_ic.get(icid)
        if not plans_of_ic:
            stats['no_plan'] += 1
            continue
        groups = {}
        for st, subs in days[icid].items():
            segs = tuple(day_segments(subs, plans_of_ic))
            if segs:
                groups.setdefault(segs, 0)
                groups[segs] |= 1 << (st - 1)
        if not groups:
            stats['no_plan'] += 1
            continue
        lat, lon, name = coords[icid]
        p = []
        for segs, mask in sorted(groups.items(), key=lambda kv: kv[1]):
            for s, e, c, reds in segs:
                p.append(dict(w=mask, s=s, e=e, c=c, r=list(reds)))
        stats['plans_out'] += len(p)
        junctions.append(dict(id=icid, n=name, la=round(lat, 6), lo=round(lon, 6), p=p))
    stats['junctions'] = len(junctions)
    doc = dict(version=version, license=TAIPEI_LICENSE, source='臺北市時制計畫',
               note='臺北市路口號誌時制計畫精簡版：各路口各星期、時段的週期與各分相紅燈秒數（週期−綠燈−黃燈）。'
                    '是計畫值，實際可能因感應控制、臨時調整而不同；App 只用來估計平均等待，不做倒數。',
               sources=[dict(title='臺北市路口號誌時制計畫', dataset='145720', url='https://data.gov.tw/dataset/145720')],
               junctions=junctions)
    return doc, stats


# ------------------------------------------------------------------ 澎湖

def parse_penghu_greens(text):
    """'50.2' → [50, 20]；'30.15' → [30, 15]；'31.34' → [31, 34]"""
    t = str(text or '').strip()
    if not t:
        return []
    parts = t.split('.')
    out = []
    for i, p in enumerate(parts):
        if not p.isdigit():
            return []
        v = int(p)
        if i > 0 and len(p) == 1:
            v *= 10
        out.append(v)
    return out


def build_penghu(path, version):
    text = _decode(open(path, 'rb').read())
    rows = list(csv.DictReader(io.StringIO(text)))
    stats = dict(rows=len(rows), flashing=0, bad=0, junctions=0)
    junctions = []
    for d in rows:
        d = {(k or '').strip(): (v or '').strip() for k, v in d.items()}
        if '閃' in d.get('號誌別', '') or '閃' in d.get('運作情形', ''):
            stats['flashing'] += 1
            continue
        lat, lon = _f(d.get('緯度')), _f(d.get('經度'))
        cycle = _f(d.get('週期'))
        greens = parse_penghu_greens(d.get('綠燈時制'))
        yellow = _f(d.get('時制（黃燈）（數量）')) or 3
        if lat is None or lon is None or not cycle or len(greens) < 2 or not (20 <= cycle <= 300):
            stats['bad'] += 1
            continue
        reds = [int(cycle - g - yellow) for g in greens]
        if any(not (0 < r < cycle) for r in reds):
            stats['bad'] += 1
            continue
        p = []
        for key in ('運作時段1', '運作時段2', '觀光季運作時段3'):
            m = re.match(r'^(\d{4})-(\d{4})$', d.get(key, ''))
            if m and key != '觀光季運作時段3':
                s, e = hhmm(m.group(1)), hhmm(m.group(2))
                if e > s:
                    p.append(dict(w=127, s=s, e=e, c=int(cycle), r=reds))
        if not p:
            p.append(dict(w=127, s=0, e=1440, c=int(cycle), r=reds))
        junctions.append(dict(id='PH%03d' % int(_f(d.get('編號')) or len(junctions) + 1), n=clean_name(d.get('位置路口名稱')),
                              la=round(lat, 6), lo=round(lon, 6), p=p))
    stats['junctions'] = len(junctions)
    doc = dict(version=version, license=PENGHU_LICENSE, source='澎湖縣號誌時制（2018）',
               note='澎湖縣紅綠燈號誌一覽表的時制欄位（資料檔名 2018，可能偏舊）。運作時段外為閃光。',
               sources=[dict(title='紅綠燈號誌位置一覽表', dataset='113153', url='https://data.gov.tw/dataset/113153')],
               junctions=junctions)
    return doc, stats


def validate(doc):
    problems = []
    js = doc.get('junctions') or []
    if not js:
        problems.append('沒有路口')
    ids = set()
    for j in js:
        if j['id'] in ids:
            problems.append('路口 id 重複：%s' % j['id'])
        ids.add(j['id'])
        if not (21.5 <= j['la'] <= 26.5 and 118 <= j['lo'] <= 123):
            problems.append('%s 座標不在台灣' % j['id'])
        for p in j.get('p', []):
            if not (1 <= p['w'] <= 127 and 0 <= p['s'] < p['e'] <= 1440 and 20 <= p['c'] <= 300 and p['r']
                    and all(0 < r < p['c'] for r in p['r'])):
                problems.append('%s 計畫不合理：%s' % (j['id'], p))
                break
    return problems
