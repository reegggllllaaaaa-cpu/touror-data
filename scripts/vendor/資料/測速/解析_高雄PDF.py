# -*- coding: utf-8 -*-
"""把高雄市政府警察局 115 年「固定式違規照相科技執法設備」設置地點一覽表（PDF，115/6/22 更新）主表轉成 CSV。

用法：python -I 解析_高雄PDF.py        （需要 PyMuPDF：import pymupdf）
輸入：原始\\kaohsiung_tech_115.pdf
輸出：原始\\kaohsiung_tech_115.csv（UTF-8 BOM；欄位：編號,型式,地點,方向,速限,行政區,測照型式,緯度,經度,頁）
只取主表（第 1–18 頁，欄位：編號、型式、測照地點、測照方向、速限、行政區、測照型式、座標）。
PDF 有幾列跨格斷行，find_tables 會拆成好幾列；這幾列由 MANUAL 手動補（逐字對照 PDF 第 18 頁文字），
其餘無法解析的列印出來，不靜默丟掉。建置.py 只讀這份 CSV（建置.py 維持只用標準庫）。
"""
import csv, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PDF = os.path.join(HERE, '原始', 'kaohsiung_tech_115.pdf')
OUT = os.path.join(HERE, '原始', 'kaohsiung_tech_115.csv')
MAIN_PAGES = range(0, 18)

# 跨格斷行、find_tables 拆壞的列（對照 PDF 原文手動補；座標數字被拆成兩段的照原文接回）
MANUAL = {
    118: dict(型式='非線圈數位-雷射', 地點='左營區自由三路新莊國小前', 方向='北向南', 速限='50', 行政區='左營',
              測照型式='闖紅燈', 緯度='22.6700033', 經度='120.3102426', 頁=8),
    134: dict(型式='雷達數位', 地點='岡山區石潭路 89 號前', 方向='南向北', 速限='50', 行政區='岡山',
              測照型式='超速', 緯度='22.7721813', 經度='120.2685041', 頁=9),
    253: dict(型式='非線圈數位-雷射', 地點='鳳山區國泰路二段高雄市議會前', 方向='南向北', 速限='60/40', 行政區='鳳山',
              測照型式='闖紅燈', 緯度='22.6201602', 經度='120.3473054'),
    265: dict(型式='雷達數位', 地點='燕巢區深興路130 號前', 方向='西向東', 速限='50', 行政區='燕巢',
              測照型式='超速', 緯度='22.7721021', 經度='120.3888039'),
}


def clean(c):
    return (c or '').replace('\n', '').strip()


def main():
    import pymupdf
    doc = pymupdf.open(PDF)
    rows, bad = {}, []
    for pi in MAIN_PAGES:
        for t in doc[pi].find_tables().tables:
            for r in t.extract():
                cells = [clean(c) for c in r]
                if len(cells) == 27:
                    cells = [cells[i] for i in range(0, 27, 3)]
                if len(cells) != 9 or not re.fullmatch(r'\d{1,3}', cells[0]):
                    if any(cells) and not any('編' in c or '一覽表' in c for c in cells):
                        bad.append((pi + 1, cells))
                    continue
                n = int(cells[0])
                lat, lon = cells[7].replace(' ', ''), cells[8].replace(' ', '')
                try:
                    float(lat), float(lon)
                except ValueError:
                    bad.append((pi + 1, cells))
                    continue
                rows[n] = dict(編號=n, 型式=cells[1], 地點=cells[2], 方向=cells[3], 速限=cells[4], 行政區=cells[5],
                               測照型式=cells[6], 緯度=lat, 經度=lon, 頁=pi + 1)
    for n, d in MANUAL.items():
        if n not in rows:
            rows[n] = dict({'編號': n, '頁': 18}, **d)
    missing = [n for n in range(1, max(rows) + 1) if n not in rows]
    with open(OUT, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['編號', '型式', '地點', '方向', '速限', '行政區', '測照型式', '緯度', '經度', '頁'])
        w.writeheader()
        for n in sorted(rows):
            w.writerow(rows[n])
    from collections import Counter
    print('主表列數', len(rows), '最大編號', max(rows), '缺號', missing)
    print('測照型式', dict(Counter(r['測照型式'] for r in rows.values())))
    for p, c in bad:
        if any(re.fullmatch(r'\d{1,3}', x) for x in c[:1]) or len([x for x in c if x]) >= 3:
            print('未解析（第 %d 頁）' % p, c)
    return 0 if not missing else 1


if __name__ == '__main__':
    sys.exit(main())
