# -*- coding: utf-8 -*-
"""移動式測速「常出沒路段」資料檔建置（NAV-CAM-HUD-1，2026-11-19；只用 Python 標準庫）。

用法：python -I 建置_移動式.py
輸入：原始\\移動式\\ 的三個政府開放資料（都沒有座標，只有路段文字）
  - 高雄市113年移動式測速照相地點（data.gov.tw 169075，182 列）
  - 高雄市111年移動式測速照相地點（data.gov.tw 152480，181 列）
  - 臺中市科學儀器執法設備取締地點(移動式)（data.gov.tw 170672，386 列，經緯度欄全空）
  其他縣市在 data.gov.tw 查不到移動式清單（研究員 2026-11-19 查證），App 對這些縣市不顯示。
參考點：..\\搜尋\\touror_places.sqlite（OSM 路名點，每條路每個行政區一點）。路名＋縣市（＋行政區）查得到才收；
  App 只在「目前所在路名相同、且離參考點 3 公里內」時提示（避免別縣市同名路誤判）。
輸出：mobile-hotspots.json（正本，與 App 內 ios\\App\\Resources\\mobile-hotspots.json 相同）、
      mobile-hotspots-report.json（每列的解析結果與沒收的原因）。
規則：
  1. 路名＝取締地點去掉行政區前綴後，第一個「路／街／大道／公路／鄉道／線」為止（「臺灣大道二段近中興街口」→ 臺灣大道二段、
     「三環路(中132線)10.7公里處」→ 三環路、「大寮區台25線鳳林路段」→ 台25線）；後面緊接「N段」就帶段。
  2. 臺→台、全形→半形；同一縣市＋行政區＋路名（含段）只留一筆，取最新年度。
  3. 只是「統計上常執法的路段」，不是當下有測速：App 文案寫「不一定有執法，請留意速限」。
"""
import csv, io, json, os, re, sqlite3, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, '原始', '移動式')
PLACES = os.path.join(HERE, '..', '搜尋', 'touror_places.sqlite')
APP_COPY = os.path.join(HERE, '..', '..', 'ios', 'App', 'Resources', 'mobile-hotspots.json')
VERSION = '20261119'
LICENSE = '資料來源：政府資料開放平臺（高雄市政府警察局、臺中市政府警察局），依政府資料開放授權條款第1版'

SOURCES = [
    dict(key='kaohsiung_113', file='kaohsiung_mobile_169075.csv', city='高雄市', dataset='169075',
         title='高雄市113年移動式測速照相地點', year='2024', road_col='路段'),
    dict(key='kaohsiung_111', file='kaohsiung_mobile_152480.csv', city='高雄市', dataset='152480',
         title='高雄市111年移動式測速照相地點', year='2022', road_col='路段'),
    dict(key='taichung', file='taichung_mobile_170672.csv', city='臺中市', dataset='170672',
         title='臺中市科學儀器執法設備取締地點(移動式)', year='2026', road_col='取締地點'),
]
CN = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
ROAD_RE = re.compile(r'^(.+?(?:大道|公路|鄉道|路|街|線))((?:\d+|[一二三四五六七八九十]+)段)?')
ANCHOR_MAX = 8


def norm(s):
    s = unicodedata.normalize('NFKC', s or '').replace('臺', '台').replace('缐', '線')
    return re.sub(r'\s+', '', s)


def read_rows(path):
    t = open(path, 'rb').read().decode('utf-8-sig').lstrip('﻿')
    rows = list(csv.reader(io.StringIO(t)))
    header = [h.strip().lstrip('﻿') for h in rows[0]]
    for r in rows[1:]:
        d = {header[i]: (r[i].strip() if i < len(r) else '') for i in range(len(header))}
        if any(d.values()):
            yield d


def section_num(s):
    if not s:
        return None
    s = s[:-1]
    if s.isdigit():
        return int(s)
    if s in CN:
        return CN[s]
    if len(s) == 2 and s[0] == '十' and s[1] in CN:
        return 10 + CN[s[1]]
    return None


def parse_road(text, district):
    t = norm(text)
    d = norm(district)
    if d and t.startswith(d):
        t = t[len(d):]
    t = re.sub(r'^(省道|縣道|市道)', '', t)
    m = ROAD_RE.match(t)
    if not m:
        return None, None
    base = m.group(1)
    if re.search(r'\d+(巷|弄|號)', base) or len(base) < 2:
        return None, None
    return base, section_num(m.group(2))


def anchors(db, city, district, base, section):
    c = norm(city)
    q = 'select name, sub, lat, lon from place where kind = "road" and (name = ? or name like ?)'
    rows = db.execute(q, (base, base + '%段')).fetchall()
    good = []
    for name, sub, lat, lon in rows:
        n = norm(name)
        if '巷' in n or '弄' in n:
            continue
        if section and n != base and section_num(n[len(base):]) != section:
            continue
        if n != base and not re.fullmatch(r'(\d+|[一二三四五六七八九十]+)段', n[len(base):]):
            continue
        good.append((norm(sub or ''), lat, lon))
    exact = [g for g in good if g[0].startswith(c + norm(district))] if district else []
    pick = exact or [g for g in good if g[0].startswith(c)]
    pick.sort(key=lambda g: (g[1], g[2]))
    return [[round(lat, 5), round(lon, 5)] for _, lat, lon in pick[:ANCHOR_MAX]], ('district' if exact else 'city')


def main():
    db = sqlite3.connect('file:%s?mode=ro' % PLACES, uri=True)
    seen = {}
    report = []
    for src in SOURCES:
        for d in read_rows(os.path.join(RAW, src['file'])):
            text = d.get(src['road_col'], '')
            district = d.get('行政區', '')
            if not text.strip():
                continue
            base, sec = parse_road(text, district)
            row = dict(source=src['key'], text=text, district=district, road=base, section=sec)
            if not base:
                row['skip'] = '路名解析不出'
                report.append(row)
                continue
            key = (src['city'], district, base, sec)
            if key in seen:
                row['skip'] = '重複（已收較新年度）'
                report.append(row)
                continue
            pts, level = anchors(db, src['city'], district, base, sec)
            if not pts:
                row['skip'] = '路名點資料庫查不到這個縣市的這條路'
                report.append(row)
                continue
            row['anchors'] = len(pts)
            row['anchor_level'] = level
            report.append(row)
            seen[key] = dict(c=norm(src['city']), d=district or None,
                             r=base + ('%d段' % sec if sec else ''), x=text if text != base else None, p=pts,
                             y=src['year'])
    spots = sorted(seen.values(), key=lambda s: (s['c'], s['d'] or '', s['r']))
    for s in spots:
        if s['d'] is None:
            del s['d']
        if s['x'] is None:
            del s['x']
        del s['y']
    used = {r['source'] for r in report if 'anchors' in r}
    doc = dict(version=VERSION, license=LICENSE,
               note='移動式測速常出沒路段：各縣市警察局公布的移動式測速路段清單（沒有座標），以 OSM 路名點當參考點。'
                    '只是統計上較常執法的路段，不代表當下有測速。',
               sources=[dict(city=s['city'], title=s['title'], dataset=s['dataset'], year=s['year'])
                        for s in SOURCES if s['key'] in used],
               spots=spots)
    data = (json.dumps(doc, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
    for path in (os.path.join(HERE, 'mobile-hotspots.json'), APP_COPY):
        with open(path, 'wb') as f:
            f.write(data)
    with open(os.path.join(HERE, 'mobile-hotspots-report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    from collections import Counter
    print('spots', len(spots), 'bytes', len(data))
    print('by city', dict(Counter(s['c'] for s in spots)))
    print('skip', dict(Counter(r.get('skip', 'ok') for r in report)))


if __name__ == '__main__':
    main()
