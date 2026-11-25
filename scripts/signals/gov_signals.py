# -*- coding: utf-8 -*-
"""政府號誌位置補點：讀臺北 121890、彰化 37695、澎湖 113153、臺南 79978、臺中 103743，
以及 data/gov-signals/gov_signals_extra.csv（臺南 137239、高雄 176561、新竹市 67783、使用者實地確認）的號誌座標，
與 OSM 號誌（taiwan-signals.bin）去重後，把 OSM 沒有的路口加進同一個檔。

規則：
- 閃光號誌（彰化「號誌種類＝閃光」、澎湖「號誌別＝閃光號誌」或「運作情形＝閃燈運作」）不會讓人停車，不收。
- 座標要在台灣範圍（緯 21.5–26.5、經 118–123）；經緯顛倒的對調。
- 政府點 40 m 內已有 OSM 路口號誌（旗標 bit0）＝同一路口，不加（OSM 點常在停止線，政府點在路口中心）。
- 政府點彼此 20 m 內只留一個。
- 補進的點旗標 = 1（路口號誌）| 8（政府資料補點）。App 只看 bit0／bit1，bit3 只是來源註記。
"""
import csv
import io
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from common import paths  # noqa: E402
from signals.sources import GOV_SIGNAL_SOURCES  # noqa: E402

FLAG_SIGNAL = 1
FLAG_GOV = 8
OSM_MATCH_M = 40.0
SELF_DEDUP_M = 20.0
LAT_RANGE = (21.5, 26.5)
LON_RANGE = (118.0, 123.0)
TAICHUNG_FILTERED_KINDS = {'行人號誌桿'}


def decode(b):
    for enc in ('utf-8-sig', 'cp950', 'big5'):
        try:
            return b.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return b.decode('utf-8', errors='replace'), 'utf-8?'


def read_rows(path):
    text, _ = decode(open(path, 'rb').read())
    first = text.splitlines()[0] if text else ''
    delim = '\t' if first.count('\t') > first.count(',') else ','
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if not rows:
        return []
    header = [h.strip().lstrip('﻿') for h in rows[0]]
    out = []
    for r in rows[1:]:
        d = {header[i]: (r[i].strip() if i < len(r) else '') for i in range(len(header))}
        if any(d.values()):
            out.append(d)
    return out


def fnum(s):
    try:
        v = float(str(s).strip())
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def pick(d, names):
    for n in names or ():
        if d.get(n, '').strip():
            return d[n].strip()
    return ''


def is_flashing(src_key, d):
    text = ' '.join(d.get(k, '') for k in ('號誌種類', '號誌別', '運作情形'))
    return '閃光' in text or '閃燈' in text


def read_taichung(path):
    """讀取臺中市政府交通局清理後 CSV，回傳 (座標 list, 統計 dict)。

    CSV 欄位：lat, lon, layer, kind, comboroad, num, control, light
    只輸出 light == '三色號誌' 且 kind != '行人號誌桿' 的點（視為路口號誌）。
    """
    coords = []
    stats = dict(rows=0, flag1=0, flasher=0, pedestrian_or_blank=0, skipped_other=0)
    with open(path, newline='', encoding='utf-8-sig') as f:
        for d in csv.DictReader(f):
            stats['rows'] += 1
            light = (d.get('light') or '').strip()
            kind = (d.get('kind') or '').strip()
            if light == '三色號誌' and kind != '行人號誌桿':
                lat = fnum(d.get('lat'))
                lon = fnum(d.get('lon'))
                if lat is not None and lon is not None:
                    coords.append((lat, lon))
                    stats['flag1'] += 1
            elif light == '閃光號誌' or kind in ('閃黃燈號誌', '閃紅燈號誌'):
                stats['flasher'] += 1
            elif kind == '行人號誌桿' or light == '':
                stats['pedestrian_or_blank'] += 1
            else:
                stats['skipped_other'] += 1
    return coords, stats


def coord(src, d):
    lat, lon = fnum(pick(d, src.get('lat', ('緯度',)))), fnum(pick(d, src.get('lon', ('經度',))))
    if lat is None or lon is None:
        return None
    if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
        if LAT_RANGE[0] <= lon <= LAT_RANGE[1] and LON_RANGE[0] <= lat <= LON_RANGE[1]:
            lat, lon = lon, lat
        else:
            return None
    return lat, lon


def dist_m(a_lat, a_lon, b_lat, b_lon):
    k = 111_320.0
    dx = (b_lon - a_lon) * k * math.cos(math.radians((a_lat + b_lat) / 2))
    dy = (b_lat - a_lat) * 110_574.0
    return math.hypot(dx, dy)


class Grid:
    def __init__(self, cell_deg=0.001):
        self.cell = cell_deg
        self.cells = {}

    def key(self, lat, lon):
        return int(math.floor(lat / self.cell)), int(math.floor(lon / self.cell))

    def add(self, lat, lon):
        self.cells.setdefault(self.key(lat, lon), []).append((lat, lon))

    def near(self, lat, lon, radius):
        ki, kj = self.key(lat, lon)
        span = int(math.ceil(radius / 100.0)) + 1  # 0.001° ≈ 100 m
        for di in range(-span, span + 1):
            for dj in range(-span, span + 1):
                for p in self.cells.get((ki + di, kj + dj), ()):
                    if dist_m(lat, lon, p[0], p[1]) <= radius:
                        return True
        return False


def merge(osm_rows, raw_dir, sources=GOV_SIGNAL_SOURCES):
    """osm_rows：[(lat_e6, lon_e6, flags)]。回傳 (合併後 rows, 統計 list)。"""
    osm_grid = Grid()
    for lat, lon, fl in osm_rows:
        if fl & FLAG_SIGNAL:
            osm_grid.add(lat / 1e6, lon / 1e6)
    gov_grid = Grid()
    added_rows, stats = [], []
    for src in sources:
        path = os.path.join(raw_dir, src['file'])
        if not os.path.exists(path) and src.get('static_path'):
            path = os.path.join(paths.ROOT, src['static_path'], src['file'])
        st = dict(key=src['key'], dataset=src['dataset'], city=src['city'], file=src['file'],
                  rows=0, flashing=0, bad_coord=0, dup_osm=0, dup_gov=0, added=0, missing=False)
        if src.get('confidence'):
            st['confidence'] = src['confidence']
            st['confidence_note'] = src.get('confidence_note', '')
        if not os.path.exists(path):
            st['missing'] = True
            stats.append(st)
            continue
        if src.get('fmt') == 'POINTS_CSV':
            with open(path, newline='', encoding='utf-8-sig') as f:
                pts = [d for d in csv.DictReader(f) if d.get('source') == src.get('csv_source')]
            for d in pts:
                st['rows'] += 1
                c = coord(src, d)
                if c is None:
                    st['bad_coord'] += 1
                    continue
                if osm_grid.near(c[0], c[1], OSM_MATCH_M):
                    st['dup_osm'] += 1
                    continue
                if gov_grid.near(c[0], c[1], SELF_DEDUP_M):
                    st['dup_gov'] += 1
                    continue
                gov_grid.add(*c)
                added_rows.append((int(round(c[0] * 1e6)), int(round(c[1] * 1e6)), FLAG_SIGNAL | FLAG_GOV))
                st['added'] += 1
            stats.append(st)
            continue
        if src.get('fmt') == 'TAICHUNG_CSV':
            coords, taichung_st = read_taichung(path)
            st['rows'] = taichung_st['rows']
            st['flag1'] = taichung_st['flag1']
            st['flashing'] = taichung_st['flasher']
            st['pedestrian_or_blank'] = taichung_st['pedestrian_or_blank']
            st['skipped_other'] = taichung_st['skipped_other']
            for lat, lon in coords:
                if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]):
                    st['bad_coord'] += 1
                    continue
                if osm_grid.near(lat, lon, OSM_MATCH_M):
                    st['dup_osm'] += 1
                    continue
                if gov_grid.near(lat, lon, SELF_DEDUP_M):
                    st['dup_gov'] += 1
                    continue
                gov_grid.add(lat, lon)
                added_rows.append((int(round(lat * 1e6)), int(round(lon * 1e6)), FLAG_SIGNAL | FLAG_GOV))
                st['added'] += 1
            stats.append(st)
            continue
        for d in read_rows(path):
            st['rows'] += 1
            if is_flashing(src['key'], d):
                st['flashing'] += 1
                continue
            c = coord(src, d)
            if c is None:
                st['bad_coord'] += 1
                continue
            if osm_grid.near(c[0], c[1], OSM_MATCH_M):
                st['dup_osm'] += 1
                continue
            if gov_grid.near(c[0], c[1], SELF_DEDUP_M):
                st['dup_gov'] += 1
                continue
            gov_grid.add(*c)
            added_rows.append((int(round(c[0] * 1e6)), int(round(c[1] * 1e6)), FLAG_SIGNAL | FLAG_GOV))
            st['added'] += 1
        stats.append(st)
    return list(osm_rows) + added_rows, stats
