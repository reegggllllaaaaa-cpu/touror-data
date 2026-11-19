# -*- coding: utf-8 -*-
"""國道速限小表 speed-limits-national.json（來源：交通部高速公路局「國道各主要路段速限表」data.gov.tw 40476）。

原表只有「路線、路段（文字）、速限」三欄。from／to（公里）只在路段文字明確寫出兩個里程（例如「…(155K)至…(356K)」）
時才填；「以北／以南」這種單邊描述不推算，留空（App 只對有里程範圍的路段查表）。
"""
import csv
import io
import json
import os
import re

KM = re.compile(r'(\d+(?:\.\d+)?)\s*[Kk](?:\s*\+\s*(\d+))?')
LICENSE = '資料來源：交通部高速公路局（政府資料開放平臺 40476），依政府資料開放授權條款第1版'


def decode(b):
    for enc in ('utf-8-sig', 'cp950', 'big5'):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode('utf-8', errors='replace')


def km_values(text):
    out = []
    for m in KM.finditer(text):
        v = float(m.group(1)) + (int(m.group(2)) / 1000.0 if m.group(2) else 0.0)
        out.append(round(v, 3))
    return out


def km_range(text):
    """路段文字含「至」且前後各有里程才回傳 (from, to)"""
    if '至' not in text:
        return None, None
    left, right = text.split('至', 1)
    a, b = km_values(left), km_values(right)
    if not a or not b:
        return None, None
    return a[-1], b[0]


def parse_rows(raw_bytes):
    rows = list(csv.reader(io.StringIO(decode(raw_bytes))))
    header = [h.strip() for h in rows[0]]
    out, rejected = [], []
    for r in rows[1:]:
        d = {header[i]: (r[i].strip() if i < len(r) else '') for i in range(len(header))}
        if not any(d.values()):
            continue
        try:
            limit = int(float(d.get('速限', '')))
        except ValueError:
            rejected.append(d)
            continue
        if not (40 <= limit <= 130) or not d.get('路線'):
            rejected.append(d)
            continue
        seg = dict(road=d['路線'], text=d.get('路段', ''), limit=limit)
        a, b = km_range(seg['text'])
        if a is not None:
            seg['from'], seg['to'] = a, b
        out.append(seg)
    return out, rejected


def build(raw_path, version, out_path):
    segs, rejected = parse_rows(open(raw_path, 'rb').read())
    doc = dict(version=version, license=LICENSE,
               note='國道各主要路段速限；from／to 為公里（只有原文寫出起訖里程才有）。依現場標誌為準。',
               sources=[dict(title='國道各主要路段速限表', dataset='40476', url='https://data.gov.tw/dataset/40476')],
               segments=segs)
    data = (json.dumps(doc, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    with open(out_path, 'wb') as f:
        f.write(data)
    return dict(count=len(segs), rejected=len(rejected), with_range=sum(1 for s in segs if 'from' in s))


def validate(path):
    problems = []
    doc = json.load(open(path, encoding='utf-8'))
    segs = doc.get('segments', [])
    if not segs:
        problems.append('沒有路段')
    for s in segs:
        if not (40 <= s.get('limit', 0) <= 130):
            problems.append('速限超出 40–130：%s' % s)
        if not str(s.get('road', '')).startswith('國道'):
            problems.append('路線不是國道：%s' % s.get('road'))
    return problems
