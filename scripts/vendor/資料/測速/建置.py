# -*- coding: utf-8 -*-
"""測速照相資料建置腳本（只用 Python 標準庫）。

用法（在任何目錄皆可，請用 -I）：
    python -I 建置.py
讀取同目錄下 原始\\ 的下載檔，輸出 speedcams.json / speedcams.csv / meta.json /
build_stats.json（給報告用的統計與剔除清單）。
"""
import csv, hashlib, io, json, math, os, re, sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, '原始')
# 最近一次下載日（meta.version）。2026-11-18 下載 SOURCES；2026-11-19 下載 EXTRA_SOURCES 與區間參考文件（NAV-CAM-DATA-2）
DOWNLOAD_DATE = '2026-11-19'
FIRST_DOWNLOAD_DATE = '2026-11-18'
# 修訂號（App 內版本＝version.revision）。2＝加區間長度 len_m／len_src（NAV-CAM-HUD-1）；
# 3＝區間長度補完（官方文件、len_ref／len_date）、拆方向與分段、重複合併、汰除／維護中狀態、闖紅燈擴充與 redlight_speed（NAV-CAM-DATA-2）
DATA_REVISION = 3
DATA_REVISION_NOTE = ('3：區間長度依官方文件補完（len_src official／unverified、len_ref、len_date）、拆方向與分段、重複點合併、'
                      '汰除（retired）與維護中（suspended）狀態；闖紅燈擴充（高雄 115 年 PDF、臺北 135957、新竹市 178144、'
                      '苗栗 172178、基隆 178159、國道 100856）與 redlight_speed（闖紅燈兼超速）')
CORRECTIONS = os.path.join(HERE, '區間修正.json')

LON_RANGE = (118.0, 123.0)
LAT_RANGE = (21.5, 26.5)

# ---------------------------------------------------------------- 來源設定
# kind: 資料集本身宣告的類型（標題或欄位寫明「固定式」「區間」）；None＝資料集未寫明
SOURCES = [
    dict(key='national', file='national_7320.csv', dataset='7320', kind=None,
         title='測速執法設置點（內政部警政署）',
         url='https://opdadm.moi.gov.tw/api/v1/no-auth/resource/api/dataset/EA5E6FCD-B82D-43B7-A5CF-E9893253187E/resource/C4CD201D-3B2D-4345-B07F-FFAB337FCA8B/download'),
    dict(key='highway', file='highway_13940.csv', dataset='13940', kind='fixed',
         title='國道公路固定式測速照相地點（內政部警政署，ZIP 內 CSV）',
         url='https://www.tgos.tw/tgos/VirtualDir/Product/c2dd3a68-cafc-48fc-8a4a-7215ddc24cd3/1150720-國道公路固定式測速照相地點.zip'),
    dict(key='nantou', file='nantou_38357.csv', dataset='38357', kind='fixed',
         title='南投縣政府警察局固定式自動測速照相桿',
         url='https://data.nantou.gov.tw/dataset/c4ce0306-f6e4-4c37-b52e-3ca8ff679d22/resource/5fb393be-b3bf-4290-a036-b3c22ce290aa/download/1150724.csv'),
    dict(key='taipei', file='taipei_130111.csv', dataset='130111', kind='fixed',
         title='臺北市固定測速照相地點表',
         url='https://data.taipei/api/dataset/745b8808-061f-4f5b-9a62-da1590c049a9/resource/5012e8ba-5ace-4821-8482-ee07c147fd0a/download'),
    dict(key='newtaipei', file='newtaipei_123740.csv', dataset='123740', kind='fixed',
         title='新北市固定式測速照相',
         url='https://data.ntpc.gov.tw/api/datasets/99f3ff6e-0352-4399-a726-775ab765a1dc/csv/file'),
    dict(key='taoyuan', file='taoyuan_fixed_25935.csv', dataset='25935', kind='fixed',
         title='桃園市測速照相設備地點',
         url='https://opendata.tycg.gov.tw/api/dataset/ecd45ee5-4489-436b-bd08-7d4e4111c4a4/resource/6aa72aaf-d703-4a9e-acd7-24c17e934dc3/download'),
    dict(key='taoyuan_tech', file='taoyuan_tech_178168.csv', dataset='178168', kind=None,
         title='桃園市科技執法設備地點',
         url='https://opendata.tycg.gov.tw/api/dataset/46bd8de0-cb90-4149-a530-5f5b1751b450/resource/636bb62e-cc5b-4ad4-ac11-5849ad5c432d/download'),
    dict(key='hsinchu_county', file='hsinchucounty_173211.csv', dataset='173211', kind='fixed',
         title='新竹縣政府警察局固定測速設置地點',
         url='https://ws.hsinchu.gov.tw/001/Upload/1/opendata/8774/2786/daf2e188-b52f-4c91-87b7-bb13ad7b5bb8.csv'),
    dict(key='hsinchu_city', file='hsinchucity_67491.csv', dataset='67491', kind='fixed',
         title='新竹市違規自動照相固定桿設置地點',
         url='https://odws.hccg.gov.tw/001/Upload/25/opendataback/9059/156/41121ffc-4357-488a-b14e-9e51f0d7aa74.csv'),
    dict(key='changhua', file='changhua_fixed_27969.csv', dataset='27969', kind='fixed',
         title='彰化縣警察局固定式取締違規照相設備設置地點一覽表',
         url='https://www.chpb.gov.tw/opendata/2XvPDK8t9UC1v65F7FQ4Kw/csv'),
    dict(key='changhua_section', file='changhua_section_178161.csv', dataset='178161', kind='section',
         title='彰化縣區間測速設備設置地點',
         url='https://www.chpb.gov.tw/opendata/iIqaLCWmEU-cNkBdBB-CAQ/csv'),
    dict(key='taichung_section', file='taichung_section_172725.csv', dataset='172725', kind='section',
         title='臺中市科技執法設備(區間平均速率執法)',
         url='https://newdatacenter.taichung.gov.tw/api/v1/no-auth/resource.download?rid=e968989a-bfb2-4b6c-8bd0-1b1af3c54745'),
    dict(key='yunlin', file='yunlin_178358.csv', dataset='178358', kind='fixed',
         title='1150817雲林縣警察局固定式測速照相設備設置地點一覽表',
         url='https://ws.yunlin.gov.tw/001/Upload/539/opendata/0/2492/0f3f7782-6a19-490b-817b-2b075cce6bc7.csv'),
    dict(key='chiayi_county', file='chiayicounty_178143.csv', dataset='178143', kind='fixed',
         title='嘉義縣警察局固定式測速照相設置地點',
         url='https://ws-tm.cyhg.gov.tw/001/Upload/1330/relfile/13620/248277/0b69bd7a-6eaf-45c0-a181-5cc0ddd04ea7.csv'),
    dict(key='chiayi_city', file='chiayicity_52544.csv', dataset='52544', kind='fixed',
         title='嘉義市固定式測速照相及闖紅燈照相設置地點',
         url='https://data.chiayi.gov.tw/opendata/api/getResource?oid=26242d8c-9340-4553-a67f-5084a37552d4&rid=4adcea0f-ee46-4a9f-bb6e-8f4f3a1817bb'),
    dict(key='pingtung', file='pingtung_fixed_146681.csv', dataset='146681', kind='fixed',
         title='屏東縣轄內固定測速照相系統設置地點一覽表',
         url='https://www-ws.pthg.gov.tw/Upload/2015pthg/0/relfile/0/0/2ad10795-015f-4e80-a694-143c26163af5.csv'),
    dict(key='penghu', file='penghu_fixed_156415.csv', dataset='156415', kind='fixed',
         title='澎湖縣固定測速照相設置地點表',
         url='https://opendata.penghu.gov.tw/dataset/99f233c2-df0b-4291-b899-81ac3684551b/resource/a3e7b949-275a-4896-aca7-45ef2136f3ae/download/pb140320-2022-07-14'),
    dict(key='penghu_tech', file='penghu_tech_172940.csv', dataset='172940', kind=None,
         title='澎湖縣科技執法地點',
         url='https://opendata.penghu.gov.tw/dataset/bbb46c6f-dd1a-4ca4-b4e4-ef25dab065fc/resource/4666f04f-88ef-4ca9-8cad-d4ee82c76d7e/download/pb140300-2025-02-21'),
    dict(key='kinmen_section', file='kinmen_section_178120.csv', dataset='178120', kind='section',
         title='金門縣固定式科學儀器執法設備設置地點-區間測速',
         url='https://ws.kinmen.gov.tw/001/Upload/0/relfile/0/0/5ad94bbb-f590-4e3e-b779-5c3ac8a2135f.csv'),
    dict(key='keelung_section', file='keelung_section_178159.csv', dataset='178159', kind='section',
         title='基隆市科技執法取締地點(區間平均速率執法)',
         url='https://www.klg.gov.tw/cht/upload/TechAreaAvgRate.csv'),
]


# data.gov.tw 資料集 API（/api/v2/rest/dataset/<id>）於 2026-11-18 讀到的更新頻率與 modifiedDate
DATASET_INFO = {
    '7320': ('不定期更新', '2026-11-17 15:07:38'), '13940': ('不定期更新', '2026-07-22 15:10:28'),
    '38357': ('不定期更新', '2026-07-28 23:00:16'), '130111': ('不定期更新', '2026-10-13 19:59:24'),
    '123740': ('不定期更新', '2026-09-14 00:00:04'), '25935': ('不定期更新', '2026-11-14 10:16:09'),
    '178168': ('不定期更新', '2026-11-13 10:06:50'), '173211': ('不定期更新', '2026-08-10 14:00:40'),
    '67491': ('每1年', '2026-09-07 14:52:00'), '27969': ('每1年', '2026-07-23 17:56:52'),
    '178161': ('每1年', '2026-09-01 14:14:05'), '172725': ('每6月', '2026-08-07 16:39:21'),
    '178358': ('每3月', '2026-08-17 10:50:50'), '178143': ('不定期更新', '2026-11-11 16:53:27'),
    '52544': ('每1年', '2026-04-23 17:26:39'), '146681': ('不定期更新', '2026-10-21 09:06:45'),
    '156415': ('不定期更新', '2026-08-13 11:15:18'), '172940': ('不定期更新', '2026-08-12 11:26:43'),
    '178120': ('頁面填「每」（未填完整）', '2026-08-27 16:35:01'), '178159': ('不定期更新', '2026-07-23 17:13:46'),
    # 2026-11-19 讀到（NAV-CAM-DATA-2）
    '135957': ('不定期更新', '2026-10-13 20:44:44'), '178144': ('不定期更新', '2026-09-07 14:51:51'),
    '172178': ('不定期更新', '2026-08-24 19:33:31'), '100856': ('不定期更新', '2025-11-28 14:11:31'),
    '172952': ('不定期更新', '2025-02-25 17:16:31'), '171463': ('不定期更新', '2026-11-05 10:10:21'),
    '177297': ('不定期更新', '2026-03-26 08:47:45'), '170673': ('不定期更新', '2026-08-10 15:06:33'),
    '40476': ('不定期更新', '2025-06-06 10:32:25'),
}

# 闖紅燈擴充等第二批來源（2026-11-19 下載；NAV-CAM-DATA-2）。extra=True：類型由 classify_extra 判斷，
# 與既有點 30 公尺內（方向相容）合併、類型取較具體者（merge_extra）。
EXTRA_SOURCES = [
    dict(key='kaohsiung_tech', file='kaohsiung_tech_115.csv', dataset='高雄市警察局PDF', kind=None, extra=True,
         # PDF 主表由 解析_高雄PDF.py 轉 CSV（115/6/22 更新版）
         title='高雄市政府警察局115年固定式違規照相科技執法設備設置地點一覽表',
         page='https://orgws.kcg.gov.tw/001/KcgOrgUploadFiles/432/RelFile/76272/528695/81aa7f2a-d271-4bbe-840d-5008b41ad409.pdf',
         url='https://orgws.kcg.gov.tw/001/KcgOrgUploadFiles/432/RelFile/76272/528695/81aa7f2a-d271-4bbe-840d-5008b41ad409.pdf',
         raw_extra=['kaohsiung_tech_115.pdf'], page_modified='2026-06-22（文件更新日）'),
    dict(key='taipei_tech', file='taipei_tech_135957.csv', dataset='135957', kind=None, extra=True,
         title='臺北市智慧管理科技執法設備資料表',
         url='https://data.taipei/api/dataset/986fa73e-c470-4ebf-9f35-3a1c9d2a8788/resource/4715904f-6ce1-41c2-8a68-3bc5303f3607/download'),
    dict(key='hsinchu_city_tech', file='hsinchucity_tech_178144.csv', dataset='178144', kind=None, extra=True,
         title='新竹市科技執法點位資訊',
         url='https://odws.hccg.gov.tw/001/Upload/25/opendataback/9059/2006/51a0af55-4a9f-43fb-86cc-5ea09ab58bef.csv'),
    dict(key='miaoli_tech', file='miaoli_tech_172178.csv', dataset='172178', kind=None, extra=True,
         title='苗栗縣科技執法設備取締地點',
         url='https://webws.miaoli.gov.tw/Download.ashx?u=LzAwMS9VcGxvYWQvb3BlbmRhdGEvMTcxNS83MjlmODcxOC0xNGVkLTQ5NmQtOTMzZS1jOTRmZmZkZjNmYzYuY3N2&n=6IuX5qCX57ij56eR5oqA5Z%2b35rOV6Kit5YKZ5Y%2bW57eg5Zyw6bueLmNzdg%3d%3d&icon=.csv'),
    dict(key='keelung_tech', file='keelung_tech_178159.csv', dataset='178159', kind=None, extra=True,
         title='基隆市科技執法取締地點（TechArea）', url='https://www.klg.gov.tw/cht/upload/TechArea.csv'),
    dict(key='highway_redlight', file='national_redlight_100856.csv', dataset='100856', kind='redlight', extra=True,
         title='國道公路警察局闖紅燈照相地點',
         url='https://opdadm.moi.gov.tw/api/v1/no-auth/resource/api/dataset/EF974C67-917B-43DA-A49C-F1480EEEDB23/resource/C1EC920B-69C5-4A59-B965-2C15A5708C66/download'),
]

# 只當參考（區間長度、汰除、速限查證；不直接產生測速點）的文件，記進 meta.reference_documents 與 sha256
REFERENCE_DOCUMENTS = [
    dict(file='miaoli_section_172952.csv', title='苗栗縣區間測速設備取締件數（113年下半年）', page='https://data.gov.tw/dataset/172952'),
    dict(file='taoyuan_section_171463_2024h1.csv', title='桃園市區間測速地點及舉發數量（2024上半年）', page='https://data.gov.tw/dataset/171463'),
    dict(file='taoyuan_section_171463_2024h2.csv', title='桃園市區間測速地點及舉發數量（2024下半年）', page='https://data.gov.tw/dataset/171463'),
    dict(file='hualien_177297.pdf', title='花蓮縣警察局114年測速照相、闖紅燈照相固定桿及科技執法設置地點一覽表', page='https://data.gov.tw/dataset/177297'),
    dict(file='newtaipei_section_1140919.pdf', title='新北市政府警察局區間平均速率執法設備設置地點一覽表（1140919修訂）',
         page='https://www.traffic.police.ntpc.gov.tw/dl-38672-661293d4-63e9-40f8-8da3-12ec50c57b75.html'),
    dict(file='tpta_113_3279_judgment.pdf', title='臺北高等行政法院113年度交字第3279號判決（自強隧道南往北 917 公尺）',
         page='https://judgment.judicial.gov.tw/FILES/TPTA/113%2c%e4%ba%a4%2c3279%2c20250506%2c2.pdf'),
    dict(file='taichung_tech_170673.csv', title='臺中市科技執法取締地點（路口多功能 74 處取締項目欄只寫地點，未納入）',
         page='https://data.gov.tw/dataset/170673'),
    dict(file='freeway_limits_40476.csv', title='國道各主要路段速限表（App 道路限速推定用）', page='https://data.gov.tw/dataset/40476'),
]


def read_csv(path):
    b = open(path, 'rb').read()
    for enc in ('utf-8-sig', 'cp950'):
        try:
            t = b.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError('無法辨識編碼: ' + path)
    t = t.lstrip('﻿')
    rows = list(csv.reader(io.StringIO(t)))
    header = [h.strip().lstrip('﻿') for h in rows[0]]
    out = []
    for i, r in enumerate(rows[1:], start=2):
        if not any(c.strip() for c in r):
            continue
        d = {header[j]: (r[j].strip() if j < len(r) else '') for j in range(len(header))}
        d['_line'] = i
        out.append(d)
    return enc, header, out


def g(d, *names):
    for n in names:
        if n in d and d[n] != '':
            return d[n]
    return ''


# 把各來源的欄位名稱對到共同欄位
def normalize_row(key, d):
    if key in ('national', 'newtaipei', 'pingtung'):
        return dict(city=g(d, 'CityName', 'cityname'), region=g(d, 'RegionName', 'regionname'),
                    address=g(d, 'Address', 'address'), lon=g(d, 'Longitude', 'longitude'),
                    lat=g(d, 'Latitude', 'latitude'), direct=g(d, 'direct'), limit=g(d, 'limit'),
                    items='', extra=g(d, 'DeptNm', 'deptnm') + ' ' + g(d, 'BranchNm', 'branchnm'))
    if key == 'highway':
        return dict(city='國道', region='', address=g(d, '設置地點'), lon=g(d, '座標經度'), lat=g(d, '座標緯度'),
                    direct=g(d, '拍攝方向'), limit=g(d, '速限'), items=g(d, '取締項目') + ' ' + g(d, '型式'),
                    extra=g(d, '備註'))
    if key == 'nantou':
        return dict(city='南投縣', region=g(d, '行政區'), address=g(d, '設置地點'), lon=g(d, '經度'), lat=g(d, '緯度'),
                    direct=g(d, '拍攝方向'), limit=g(d, '限速（km／hr）'), items=g(d, '取締項目'), extra='')
    if key == 'taipei':
        return dict(city='臺北市', region=g(d, '轄區'), address=g(d, '設置路段') + g(d, '設置地點'),
                    lon=g(d, '經度'), lat=g(d, '緯度'), direct=g(d, '拍攝方向'), limit=g(d, '速限-速度限制'),
                    items=g(d, '功能'), extra='')
    if key in ('taoyuan', 'taoyuan_tech'):
        return dict(city='桃園市', region=g(d, '行政區'), address=g(d, '設置地點_路口或路段'),
                    lon=g(d, '座標經度'), lat=g(d, '座標緯度'), direct=g(d, '拍攝方向'), limit=g(d, '速限'),
                    items=g(d, '取締項目') + ' ' + g(d, '型式') + ' ' + g(d, '科技執法種類'), extra=g(d, '備註'))
    if key == 'hsinchu_county':
        return dict(city='新竹縣', region=g(d, '行政區'), address=g(d, '設置地點'), lon=g(d, '經度'), lat=g(d, '緯度'),
                    direct=g(d, '拍攝方向'), limit=g(d, '速限'), items=g(d, '取締項目') + ' ' + g(d, '種類'), extra='')
    if key == 'hsinchu_city':
        return dict(city='新竹市', region='', address=g(d, '地點'), lon=g(d, '經度'), lat=g(d, '緯度'),
                    direct='', limit=g(d, '速限'), items=g(d, '備註'), extra='')
    if key in ('changhua', 'changhua_section'):
        return dict(city='彰化縣', region=g(d, '行政區'), address=g(d, '設置地點'), lon=g(d, '座標經度'),
                    lat=g(d, '座標緯度'), direct=g(d, '拍攝方向'), limit=g(d, '速限'), items=g(d, '取締項目'),
                    extra=g(d, '備註'))
    if key == 'taichung_section':
        items = g(d, '科技執法種類') + ' ' + g(d, '取締項目')
        m = re.search(r'速限\s*(\d+)', items)
        return dict(city='臺中市', region='', address=g(d, '設置地點_範圍'), lon=g(d, '經度'), lat=g(d, '緯度'),
                    direct='', limit=(m.group(1) if m else ''), items=items, extra='')
    if key == 'yunlin':
        return dict(city='雲林縣', region='', address=g(d, 'Address'), lon=g(d, 'Longitude'), lat=g(d, 'Latitude'),
                    direct=g(d, 'direct'), limit=g(d, 'limit'), items=g(d, 'ban'), extra=g(d, 'Remark'))
    if key == 'chiayi_county':
        return dict(city='嘉義縣', region=g(d, '行政區'), address=g(d, '設置地點(路口或路段)'), lon=g(d, '座標經度'),
                    lat=g(d, '座標緯度'), direct=g(d, '拍攝方向'), limit=g(d, '速限'),
                    items=g(d, '取締項目(以"、"分隔)') + ' ' + g(d, '型式'), extra='')
    if key == 'chiayi_city':
        return dict(city='嘉義市', region='', address=g(d, '設置地點'), lon=g(d, '經度'), lat=g(d, '緯度'),
                    direct=g(d, '拍攝方向'), limit=g(d, '速限'), items=g(d, '設備型式'), extra='')
    if key in ('penghu', 'penghu_tech'):
        return dict(city='澎湖縣', region=g(d, '行政區'), address=g(d, '設置地點'), lon=g(d, '座標經度'),
                    lat=g(d, '座標緯度'), direct=g(d, '拍攝方向'), limit=g(d, '速限'),
                    items=g(d, '取締項目') + ' ' + g(d, '型式') + ' ' + g(d, '科技執法種類'), extra='')
    if key == 'kinmen_section':
        return dict(city='金門縣', region=g(d, '行政區'), address=g(d, '設置地點'), lon=g(d, '座標經度'),
                    lat=g(d, '座標緯度'), direct=g(d, '拍攝方向'), limit=g(d, '速限長度'), items=g(d, '取締項目'),
                    extra='')
    if key == 'kaohsiung_tech':
        return dict(city='高雄市', region=g(d, '行政區'), address=g(d, '地點'), lon=g(d, '經度'), lat=g(d, '緯度'),
                    direct=g(d, '方向'), limit=g(d, '速限'), items=g(d, '測照型式'), extra=g(d, '型式'))
    if key == 'taipei_tech':
        return dict(city='臺北市', region=g(d, '行政區'), address=g(d, '設置地點（路口或路段）'), lon=g(d, '座標經度'),
                    lat=g(d, '座標緯度'), direct='', limit='', items=g(d, '取締項目'),
                    extra=g(d, '科技執法種類') + ' ' + g(d, '備註'))
    if key == 'hsinchu_city_tech':
        a = g(d, '地點')
        return dict(city='新竹市', region='', address=a, lon=g(d, '經度'), lat=g(d, '緯度'),
                    direct=('雙向' if '雙向' in a else ''), limit='', items=g(d, '違規取締項目'), extra=g(d, '備註'))
    if key == 'miaoli_tech':
        return dict(city='苗栗縣', region=g(d, '行政區'), address=g(d, '設置地點(路口或路段)'), lon=g(d, '座標經度'),
                    lat=g(d, '座標緯度'), direct=g(d, '拍攝方向'), limit=g(d, '速限'), items=g(d, '取締項目(以"、"分隔)'),
                    extra=g(d, '科技執法種類') + ' ' + g(d, '備註'))
    if key == 'highway_redlight':
        return dict(city='國道', region='', address=g(d, '道路編號') + g(d, '設置地點'), lon=g(d, 'WGS84_東_經度'),
                    lat=g(d, 'WGS84_北_緯度'), direct='', limit='', items='闖紅燈', extra=g(d, '備註'))
    if key in ('keelung_section', 'keelung_tech'):
        return dict(city='基隆市', region=g(d, '行政區'), address=g(d, '設置地點(路口或路段)'), lon=g(d, '座標經度'),
                    lat=g(d, '座標緯度'), direct=g(d, '拍攝方向'), limit=g(d, '速限'),
                    items=g(d, '取締項目(以"、"分隔)'), extra=g(d, '備註'))
    raise KeyError(key)


# ---------------------------------------------------------------- 方向
COMPASS = {'北': 0, '東北': 45, '東': 90, '東南': 135, '南': 180, '西南': 225, '西': 270, '西北': 315}
CP = '(東北|東南|西北|西南|北|東|南|西)'


def clean_dir(s):
    s = s.replace('（', '(').replace('）', ')').replace(' ', '').replace('　', '')
    s = re.sub(r'\(.*?\)', '', s)          # 去掉括號註記（如「(區間測速)」）
    s = re.sub(r'(區間測速|超速闖紅燈)$', '', s)
    return s


def parse_direction(raw):
    """回傳 (bearing 或 None, kind)；kind = one|both|unknown。bearing＝被拍車輛的行進方位角。"""
    s = clean_dir(raw)
    if not s:
        return None, 'unknown'
    if ('雙向' in s or '相向' in s or s in ('東西南北', '全向', '多向')
            or re.fullmatch(r'(南北|東西)向?', s)
            or len(re.findall(CP + '(向|往)' + CP, s)) >= 2
            or len(re.findall(CP + r'向\d', s)) >= 2):      # 如「南向60北向70」
        return None, 'both'
    s = re.sub(r'^往(南下|北上)', lambda m: m.group(1), s)
    s = re.sub(r'^(東|西|南|北)往(東|西|南|北)向$', lambda m: m.group(1) + '往' + m.group(2), s)
    m = re.fullmatch(CP + '(向|往)' + CP + '(方向|車道)?', s)
    if m:
        return COMPASS[m.group(3)], 'one'
    m = re.fullmatch('往' + CP + '(方向|車道)?', s)
    if m:
        return COMPASS[m.group(1)], 'one'
    m = re.fullmatch(CP + '向(車道)?', s)          # 「南向」＝南向車道（往南）
    if m:
        return COMPASS[m.group(1)], 'one'
    m = re.fullmatch('(南下|北上)(車道|方向)?', s)
    if m:
        return (180 if m.group(1) == '南下' else 0), 'one'
    return None, 'unknown'


# ---------------------------------------------------------------- 速限
def parse_limit(raw):
    """回傳 (limit 或 None, note)。先移除大貨車／噸數相關片段，再取數字。"""
    if not raw:
        return None, 'empty'
    parts = re.split(r'[\n\r;；、,，/]+', raw)
    keep = [p for p in parts if not re.search(r'噸|大貨車|大車|大型車|貨車', p)]
    if not keep:
        keep = parts
    nums = []
    for p in keep:
        for n in re.findall(r'(?<!\d)(\d{2,3})(?!\d)', p):
            v = int(n)
            if 10 <= v <= 130:
                nums.append(v)
    if not nums:
        return None, 'non-numeric'
    uniq = sorted(set(nums))
    if len(uniq) == 1:
        return uniq[0], ('ok' if raw.strip().isdigit() else 'parsed')
    return uniq[0], 'multi(min)'


# ---------------------------------------------------------------- 類型
def classify(src_kind, n, limit):
    """依欄位文字判斷類型，回傳 (type, tags, rule)。"""
    text = ' '.join([n['address'], n['direct'], n['items'], n['extra']])
    if '區間' in text or '平均速率' in text:
        return 'section', [], '文字含「區間」或「平均速率」'
    if src_kind == 'section':
        return 'section', [], '資料集標題為區間測速'
    red = '紅燈' in text
    speed = any(w in text for w in ('超速', '測速', '雷達', '雷射'))
    items = n['items']
    if items.strip() and '紅燈' in items and not any(w in items for w in ('超速', '測速')):
        return 'redlight', ['redlight'], '取締項目／功能欄只寫闖紅燈'
    if red and not speed and limit is None:
        return 'redlight', ['redlight'], '只寫闖紅燈、無速限'
    tags = ['redlight'] if red else []
    if src_kind == 'fixed':
        return 'fixed', tags, '資料集標題為固定式'
    if speed and limit is not None and any(w in text for w in ('雷達', '雷射', '非線圈', '桿', '路口多功能測速')):
        return 'fixed', tags, '型式／種類欄寫實體設備（雷達／非線圈／路口多功能測速執法）'
    return 'unknown', tags, '無明確類型線索'


# ---------------------------------------------------------------- 區間長度（NAV-CAM-HUD-1，2026-11-19）
# 區間測速只有起點座標；App 要算「區間平均速度」與建議時速，需要區間長度。能從文字解析的才填 len_m（公尺）、
# len_src='text'；其餘區間點 len_src='est'（App 以進入後 1.0 km 估計並明說「長度未知」）。規則：
#  1. 明寫距離：「執法距離1,601.16公尺」「偵測長度6,169.1公尺」→ 直接用。
#  2. 里程起迄：「157.8K至151.6K」「68k+600m至56k+600m」「82.2公里至84.3公里」「429.5-433K」「21.4K主線至23.2K」
#     → |起−迄|×1000。兩個數字至少一個帶 K／公里單位（避免把「309巷-1275巷」當里程）。
#  3. 一筆有兩段：用「/」「；」分隔的是同一區間的兩個方向（長度差 ≤25% 取較長者，差太多不填）；
#     用「及」連接的是兩個不同區間（不知道起點座標屬於哪一段），不填。
#  4. 合理範圍 200 m–20 km，超出不填。
#  5. 50 公尺內合併的另一來源有長度時補上（例：臺中 172725 的文字併進全國檔同點）；
#     建完後 100 公尺內、速限相同的另一個區間點有長度時也補上（同一區間雙向各一筆）；
#     設置地點原文相同、速限相同的區間點互補（全國檔與縣市檔座標差超過 100 公尺時）。
LEN_EXPLICIT = re.compile(r'(?:執法距離|偵測長度|區間長度|執法長度|長度)\s*約?\s*([\d,]+(?:\.\d+)?)\s*公尺')
_KM_TOKEN = r'(\d+(?:\.\d+)?)\s*(?:([kK])\s*(?:\+\s*(\d+)\s*[mM])?|(公里))?'
LEN_KM_PAIR = re.compile(_KM_TOKEN + r'\s*(?:主線)?\s*(?:至|－|-|~|～)\s*' + _KM_TOKEN)
# 起迄之間夾地名／路口（「110k+960m至蘇澳鎮108k+224m」「51.7K健康路口至50.5K新龍路口」）：兩邊都要帶單位
_KM_UNIT_TOKEN = r'(\d+(?:\.\d+)?)\s*(?:([kK])\s*(?:\+\s*(\d+)\s*[mM])?|(公里))'
LEN_KM_PAIR_LOOSE = re.compile(_KM_UNIT_TOKEN + r'[一-鿿]{0,6}至[一-鿿]{0,6}' + _KM_UNIT_TOKEN)
# 路線編號（「台9線」「縣道140線」「臺21線」）先拿掉，免得編號數字被當成里程或擋住起迄
ROAD_REF = re.compile(r'(?:省道|縣道|市道|鄉道)?(?:臺|台)?\d+[甲乙丙丁]?線')
LEN_MIN, LEN_MAX = 200.0, 20000.0


def _km_value(num, k, plus_m, gongli):
    v = float(num)
    if plus_m:
        v += float(plus_m) / 1000.0
    return v, bool(k or gongli)


def parse_section_length(*texts):
    """回傳 (長度公尺 or None, 依據說明)。texts：設置地點、取締項目等原文。"""
    joined = ' '.join(t for t in texts if t)
    t = joined.replace('，', ',').replace('（', '(').replace('）', ')').replace('Ｋ', 'K').replace('ｋ', 'k')
    m = LEN_EXPLICIT.search(t)
    if m:
        v = float(m.group(1).replace(',', ''))
        if LEN_MIN <= v <= LEN_MAX:
            return round(v, 1), '明寫距離「%s」' % m.group(0)
    t = ROAD_REF.sub('', t)
    pairs = []
    matches = list(LEN_KM_PAIR.finditer(t)) or list(LEN_KM_PAIR_LOOSE.finditer(t))
    for m in matches:
        a, ua = _km_value(m.group(1), m.group(2), m.group(3), m.group(4))
        b, ub = _km_value(m.group(5), m.group(6), m.group(7), m.group(8))
        if not (ua or ub):
            continue
        pairs.append((abs(a - b) * 1000.0, m.group(0)))
    if not pairs:
        return None, '文字沒有長度或里程'
    if len(pairs) >= 2:
        if '及' in t:
            return None, '「及」連接兩個不同區間，無法判斷起點屬於哪一段'
        lens = [p[0] for p in pairs]
        if max(lens) > min(lens) * 1.25:
            return None, '兩個方向長度差太多（%s）' % '／'.join('%.0f' % x for x in lens)
        v = max(lens)
    else:
        v = pairs[0][0]
    if not (LEN_MIN <= v <= LEN_MAX):
        return None, '里程差 %.0f 公尺不合理' % v
    return round(v, 1), '里程「%s」' % '／'.join(p[1] for p in pairs)


# ---------------------------------------------------------------- 距離與格網
def dist_m(a_lat, a_lon, b_lat, b_lon):
    r = 6371008.8
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = p2 - p1
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


class Grid:
    CELL = 0.001  # 約 100 公尺

    def __init__(self):
        self.cells = defaultdict(list)

    def key(self, lat, lon):
        return (int(math.floor(lat / self.CELL)), int(math.floor(lon / self.CELL)))

    def add(self, p):
        self.cells[self.key(p['lat'], p['lon'])].append(p)

    def near(self, lat, lon, radius):
        ki, kj = self.key(lat, lon)
        out = []
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for p in self.cells.get((ki + di, kj + dj), ()):
                    d = dist_m(lat, lon, p['lat'], p['lon'])
                    if d <= radius:
                        out.append((d, p))
        out.sort(key=lambda x: x[0])
        return out


def in_tw(lat, lon):
    return LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LON_RANGE[0] <= lon <= LON_RANGE[1]


def fnum(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- 主流程
def load_source(src, stats):
    path = os.path.join(RAW, src['file'])
    enc, header, rows = read_csv(path)
    st = dict(key=src['key'], dataset=src['dataset'], file=src['file'], encoding=enc, header=header,
              raw_rows=len(rows), note_rows=0, rejected=0, swapped=0, cleaned=0, dedup_merged=0)
    rejected, recs = [], []
    for d in rows:
        n = normalize_row(src['key'], d)
        lat, lon = fnum(n['lat']), fnum(n['lon'])
        # 中文說明列（如全國檔第 2 列「設置縣市,…,經度,緯度」、雲林「A00,設置地點,…」）
        if lat is None and lon is None and (n['lat'] in ('緯度', '經緯度', '座標緯度') or n['lon'] in ('經度', '經緯度')):
            st['note_rows'] += 1
            continue
        reason = None
        if lat is None or lon is None:
            reason = '座標缺值或非數字'
        elif lat == 0 or lon == 0:
            reason = '座標為 0'
        elif not in_tw(lat, lon):
            if in_tw(lon, lat):
                lat, lon = lon, lat
                st['swapped'] += 1
            else:
                reason = '座標不在台灣範圍'
        if reason:
            st['rejected'] += 1
            rejected.append(dict(source=src['key'], line=d['_line'], reason=reason,
                                 address=n['address'], lat=n['lat'], lon=n['lon']))
            continue
        if src.get('extra'):
            limit, lnote = parse_limit(n['limit'])
            bearing, dkind = parse_direction(n['direct'])
            typ, rule = classify_extra(src, n, limit)
            if typ is None:
                st['rejected'] += 1
                st['non_speed'] = st.get('non_speed', 0) + 1
                rejected.append(dict(source=src['key'], line=d['_line'], reason=rule, address=n['address'],
                                     lat=n['lat'], lon=n['lon']))
                continue
            recs.append(dict(lat=round(lat, 6), lon=round(lon, 6), bearing=bearing, direction=dkind,
                             direct_raw=n['direct'], limit_kmh=limit, limit_raw=n['limit'], limit_note=lnote,
                             type=typ, type_rule=rule,
                             tags=(['redlight'] if typ in ('redlight', 'redlight_speed') else []),
                             source=src['key'], sources=[src['key']], city=n['city'], region=n['region'],
                             address=n['address'], line=d['_line'], len_m=None, len_rule=''))
            continue
        if src['kind'] is None and src['key'] != 'national' and not re.search(r'超速|測速|紅燈|區間', n['items']):
            st['rejected'] += 1
            st['non_speed'] = st.get('non_speed', 0) + 1
            rejected.append(dict(source=src['key'], line=d['_line'], reason='非測速／闖紅燈設備（取締項目：%s）' % n['items'].strip(),
                                 address=n['address'], lat=n['lat'], lon=n['lon']))
            continue
        limit, lnote = parse_limit(n['limit'])
        bearing, dkind = parse_direction(n['direct'])
        typ, tags, rule = classify(src['kind'], n, limit)
        sec_len, sec_rule = (parse_section_length(n['address'], n['items'], n['extra'])
                             if typ == 'section' else (None, ''))
        recs.append(dict(lat=round(lat, 6), lon=round(lon, 6), bearing=bearing, direction=dkind,
                         direct_raw=n['direct'], limit_kmh=limit, limit_raw=n['limit'], limit_note=lnote,
                         type=typ, type_rule=rule, tags=tags, source=src['key'], sources=[src['key']],
                         city=n['city'], region=n['region'], address=n['address'], line=d['_line'],
                         len_m=sec_len, len_rule=sec_rule))
    st['cleaned'] = len(recs)
    stats['rejected'].extend(rejected)
    return recs, st


def same_dir(a, b):
    if a['direction'] != b['direction']:
        return False
    if a['direction'] == 'one':
        return a['bearing'] == b['bearing']
    if a['direction'] == 'unknown':
        return clean_dir(a['direct_raw']) == clean_dir(b['direct_raw'])
    return True


def dedup(recs, st, stats, radius=15.0):
    grid, kept = Grid(), []
    for r in recs:
        dup = None
        for d, p in grid.near(r['lat'], r['lon'], radius):
            if same_dir(p, r) and p['limit_kmh'] == r['limit_kmh'] and p['type'] == r['type']:
                dup = (d, p)
                break
        if dup:
            d, p = dup
            p.setdefault('merged_lines', []).append(r['line'])
            stats['dedup'].append(dict(source=r['source'], kept_line=p['line'], dropped_line=r['line'],
                                       dist_m=round(d, 1), address_kept=p['address'], address_dropped=r['address']))
            st['dedup_merged'] += 1
            continue
        grid.add(r)
        kept.append(r)
    return kept


TYPE_RANK = {'unknown': 0, 'fixed': 1, 'redlight': 1, 'section': 1}


def merge_into(base, grid, recs, st, stats, radius=50.0):
    added = matched = type_filled = limit_filled = 0
    for r in recs:
        hits = grid.near(r['lat'], r['lon'], radius)
        if not hits:
            r['source'] = r['sources'][0]
            grid.add(r)
            base.append(r)
            added += 1
            continue
        matched += 1
        for d, p in hits:
            if r['source'] not in p['sources']:
                p['sources'].append(r['source'])
            if p['type'] == 'unknown' and r['type'] in ('fixed', 'section'):
                p['type'] = r['type']
                p['type_rule'] = '50 公尺內 %s 資料標為 %s（%s）' % (r['source'], r['type'], r['type_rule'])
                p['type_source'] = r['source']
                type_filled += 1
            if 'redlight' in r['tags'] and 'redlight' not in p['tags']:
                p['tags'].append('redlight')
            if p['type'] == 'section' and p.get('len_m') is None and r.get('len_m') is not None:
                p['len_m'] = r['len_m']
                p['len_rule'] = '50 公尺內 %s：%s' % (r['source'], r['len_rule'])
            if p['limit_kmh'] is None and r['limit_kmh'] is not None:
                p['limit_kmh'] = r['limit_kmh']
                p['limit_source'] = r['source']
                limit_filled += 1
            elif (p['limit_kmh'] is not None and r['limit_kmh'] is not None and p['limit_kmh'] != r['limit_kmh']
                  and d <= 15):
                stats['limit_conflicts'].append(dict(base_source=p['source'], base_line=p['line'],
                                                     other=r['source'], other_line=r['line'],
                                                     base_limit=p['limit_kmh'], other_limit=r['limit_kmh'],
                                                     dist_m=round(d, 1), address=p['address']))
    st.update(merge_added=added, merge_matched=matched, merge_type_filled=type_filled,
              merge_limit_filled=limit_filled)


def fill_section_lengths(merged, stats):
    """區間點補長度：類型由合併補成 section 的點先解析自己的文字；再用 100 公尺內、速限相同、有長度的區間點補。"""
    secs = [r for r in merged if r['type'] == 'section']
    for r in secs:
        if r.get('len_m') is None and not r.get('len_rule'):
            r['len_m'], r['len_rule'] = parse_section_length(r['address'])
    for r in secs:
        if r.get('len_m') is not None:
            continue
        best = None
        for o in secs:
            if o is r or o.get('len_m') is None or o['limit_kmh'] != r['limit_kmh']:
                continue
            d = dist_m(r['lat'], r['lon'], o['lat'], o['lon'])
            if d <= 100 and (best is None or d < best[0]):
                best = (d, o)
        if best:
            r['len_m'] = best[1]['len_m']
            r['len_rule'] = '%.0f 公尺內同速限區間點（%s）：%s' % (best[0], best[1]['source'], best[1]['len_rule'])
            continue
        # 設置地點原文完全相同、速限相同（同一區間在全國檔與縣市檔各一筆，座標差超過 100 公尺）
        same = [o for o in secs if o is not r and o.get('len_m') is not None and o['limit_kmh'] == r['limit_kmh']
                and o['address'].replace(' ', '') == r['address'].replace(' ', '')]
        if same:
            r['len_m'] = same[0]['len_m']
            r['len_rule'] = '設置地點相同的區間點（%s）：%s' % (same[0]['source'], same[0]['len_rule'])
    stats['section_length'] = dict(
        total=len(secs), text=sum(1 for r in secs if r.get('len_m') is not None),
        rows=[dict(source=r['source'], address=r['address'], limit=r['limit_kmh'], len_m=r.get('len_m'),
                   rule=r.get('len_rule')) for r in secs])


# ---------------------------------------------------------------- 第二批來源：闖紅燈擴充（NAV-CAM-DATA-2，2026-11-19）
# 類型：redlight＝闖紅燈；redlight_speed＝闖紅燈兼超速（App 視為闖紅燈且有速限，打包代碼 rs）；fixed＝只寫超速／測速且有速限。
# 合併：與既有點 30 公尺內、方向相容（兩邊都有方位角時差 ≤ 45°）就併進最近的一筆，類型取較具體者（見 merged_type）。
EXTRA_RADIUS = 30.0
SAME_POLE_METERS = 15.0
SPEED_WORDS = ('超速', '測速')


def classify_extra(src, n, limit):
    """回傳 (type 或 None, 依據)。None＝不是測速／闖紅燈設備（違規停車、禁行車種、違左等）。"""
    items = n['items'].replace(' ', '')
    if src['key'] == 'kaohsiung_tech':
        if items in ('闖紅燈', '闖紅燈兼路口科技執法監測'):
            return 'redlight', '高雄 115 年一覽表測照型式「%s」' % items
        if items == '闖紅燈兼超速':
            return ('redlight_speed' if limit else 'redlight'), '高雄 115 年一覽表測照型式「闖紅燈兼超速」'
        if items == '超速' and limit:
            return 'fixed', '高雄 115 年一覽表測照型式「超速」'
        return None, '高雄 115 年一覽表測照型式「%s」不是測速／闖紅燈' % items
    if src.get('kind') == 'redlight':
        return 'redlight', '資料集為闖紅燈照相地點'
    red = '紅燈' in items
    speed = any(w in items for w in SPEED_WORDS)
    if red and speed and limit:
        return 'redlight_speed', '取締項目含闖紅燈與超速／測速，有速限'
    if red:
        return 'redlight', '取締項目含闖紅燈' + ('（另含超速但沒有速限欄）' if speed else '')
    if speed and limit:
        return 'fixed', '取締項目含超速／測速，有速限'
    return None, '非測速／闖紅燈設備（取締項目：%s）' % n['items'].strip()


def bearing_compatible(a, b, max_diff=45.0):
    if a['bearing'] is None or b['bearing'] is None:
        return True
    d = abs(a['bearing'] - b['bearing']) % 360
    return min(d, 360 - d) <= max_diff


def merged_type(base, new):
    """既有點 base 與新來源 new 視為同一設備時的類型（取較具體者）。回傳 (type, 說明)。"""
    bt, nt = base['type'], new['type']
    has_limit = base['limit_kmh'] is not None or new['limit_kmh'] is not None
    red = bt in ('redlight', 'redlight_speed') or nt in ('redlight', 'redlight_speed') or 'redlight' in base['tags']
    speed = bt in ('fixed', 'redlight_speed') or nt in ('fixed', 'redlight_speed') or (bt == 'unknown' and base['limit_kmh'] is not None)
    if red and speed and has_limit:
        return 'redlight_speed', '%s（%s）＋%s（%s）→ 闖紅燈兼超速' % (base['source'], bt, new['source'], nt)
    if red:
        return 'redlight', '%s（%s）＋%s（%s）→ 闖紅燈' % (base['source'], bt, new['source'], nt)
    if speed:
        return 'fixed', '%s（%s）＋%s（%s）→ 固定式' % (base['source'], bt, new['source'], nt)
    return bt, ''


def merge_extra(merged, grid, recs, st, stats):
    added = matched = retyped = 0
    for r in recs:
        # 15 公尺內視為同一支桿（兩份資料的拍攝方向常寫得不一樣，以既有點為準）；15–30 公尺要方向相容
        hits = [(d, p) for d, p in grid.near(r['lat'], r['lon'], EXTRA_RADIUS)
                if p['type'] != 'section' and (d <= SAME_POLE_METERS or bearing_compatible(p, r))]
        if not hits:
            r['source'] = r['sources'][0]
            grid.add(r)
            merged.append(r)
            added += 1
            continue
        matched += 1
        d, p = hits[0]
        if r['source'] not in p['sources']:
            p['sources'].append(r['source'])
        t, why = merged_type(p, r)
        if t != p['type']:
            stats['extra_retyped'].append(dict(id_line=p['line'], base_source=p['source'], before=p['type'], after=t,
                                               by=r['source'], dist_m=round(d, 1), address=p['address']))
            p['type'], p['type_rule'], p['type_source'] = t, why, r['source']
            retyped += 1
        if t in ('redlight', 'redlight_speed') and 'redlight' not in p['tags']:
            p['tags'].append('redlight')
        if p['limit_kmh'] is None and r['limit_kmh'] is not None:
            p['limit_kmh'] = r['limit_kmh']
            p['limit_source'] = r['source']
    st.update(merge_added=added, merge_matched=matched, merge_type_filled=retyped, merge_limit_filled=0)


ROAD_TOKEN = re.compile(r'[一-鿿]{1,5}?(?:路|街|大道)')


def road_tokens(s):
    s = s.replace('臺', '台').replace('7', '七')
    return {t[-3:] for t in ROAD_TOKEN.findall(s)}


def same_roads(a, b):
    """路名比對：路口（兩條以上路名）要兩條都相同；只有一條路名的要那條相同"""
    ta, tb = road_tokens(a), road_tokens(b)
    return bool(ta and tb) and len(ta & tb) >= min(len(ta), len(tb), 2)


def merge_far_same_place(merged, stats, source='kaohsiung_tech', base_source='national', radius=100.0):
    """第二批來源新加的點與 100 公尺內、同縣市、類型不明的既有點寫的是同一條路（路名相同）：
    視為同一設備（兩份資料座標差較大），既有點（id 不變）改用新來源的類型，新加的點不輸出。"""
    grid = Grid()
    for r in merged:
        if r['source'] == base_source and r['type'] == 'unknown':
            grid.add(r)
    drop = set()
    for r in merged:
        if r['source'] != source or len(r['sources']) != 1:
            continue
        for d, p in grid.near(r['lat'], r['lon'], radius):
            if p['type'] != 'unknown' or p['city'] != r['city'] or not same_roads(r['address'], p['address']):
                continue
            p['type'], p['type_source'] = r['type'], source
            p['type_rule'] = '%.0f 公尺內 %s 同路名的點標為 %s（%s）' % (d, source, r['type'], r['type_rule'])
            if r['type'] in ('redlight', 'redlight_speed') and 'redlight' not in p['tags']:
                p['tags'].append('redlight')
            if source not in p['sources']:
                p['sources'].append(source)
            drop.add(id(r))
            stats['extra_far_merged'].append(dict(kept_address=p['address'], dropped_address=r['address'],
                                                  dist_m=round(d, 1), type=r['type']))
            break
    merged[:] = [r for r in merged if id(r) not in drop]


def promote_redlight_speed(merged, stats):
    """既有資料「固定式＋兼拍闖紅燈（tags=redlight）＋有速限」＝闖紅燈兼超速，統一改成 redlight_speed。"""
    n = 0
    for r in merged:
        if r['type'] == 'fixed' and 'redlight' in r['tags'] and r['limit_kmh'] is not None:
            r['type'] = 'redlight_speed'
            r['type_rule'] = (r.get('type_rule') or '') + '；固定式且兼拍闖紅燈 → 闖紅燈兼超速'
            n += 1
    stats['promoted_redlight_speed'] = n


# ---------------------------------------------------------------- 區間人工修正（區間修正.json）
SECTION_SET_KEYS = {'lat', 'lon', 'bearing', 'direction', 'direct_raw', 'limit_kmh', 'address', 'len_m', 'len_src',
                    'len_ref', 'len_date', 'len_rule', 'status', 'status_note', 'coord_src', 'source', 'sources'}


def apply_corrections(out, used, stats):
    """依 區間修正.json 修改輸出（out 為最終 dict 清單）。回傳 (out, pending, missing)。"""
    if not os.path.exists(CORRECTIONS):
        return out, [], []
    cfg = json.load(open(CORRECTIONS, encoding='utf-8'))
    refs = cfg.get('refs', {})
    by_id = {o['id']: o for o in out}
    pending, log = [], []

    def apply_set(o, s):
        for k, v in s.items():
            if k not in SECTION_SET_KEYS:
                raise KeyError('區間修正.json 不支援欄位 %s' % k)
            if k in ('lat', 'lon') and 'orig_lat' not in o:
                o['orig_lat'], o['orig_lon'] = o['lat'], o['lon']
            if k == 'direct_raw' and 'direct_raw_orig' not in o:
                o['direct_raw_orig'] = o['direct_raw']
            if k == 'len_ref':
                v = refs.get(v, v)
            o[k] = v

    for it in cfg['items']:
        act = it['action']
        if act in ('update', 'retire', 'merge') and it['id'] not in by_id:
            raise KeyError('區間修正.json：找不到 %s' % it['id'])
        if act == 'update':
            apply_set(by_id[it['id']], it['set'])
        elif act == 'add':
            base = by_id[it['from']]
            o = json.loads(json.dumps(base))
            for k in ('orig_lat', 'orig_lon', 'direct_raw_orig'):
                o.pop(k, None)
            o['id'] = it['id']
            o['derived_from'] = it['from']
            if it['id'] in used:
                raise KeyError('區間修正.json：id 重複 %s' % it['id'])
            used.add(it['id'])
            apply_set(o, it['set'])
            out.append(o)
            by_id[o['id']] = o
        elif act == 'merge':
            o, keep = by_id[it['id']], by_id[it['into']]
            o['status'], o['merged_into'], o['status_note'] = 'merged', it['into'], it.get('note', '')
            for s in o['sources']:
                if s not in keep['sources']:
                    keep['sources'].append(s)
        elif act == 'retire':
            o = by_id[it['id']]
            o['status'], o['status_note'] = 'retired', it.get('note', '')
        elif act == 'pending':
            base = by_id[it['from']]
            p = dict(id=it['id'], from_id=it['from'], city=base['city'], limit_kmh=base['limit_kmh'], note=it.get('note', ''))
            for k, v in it['set'].items():
                p[k] = refs.get(v, v) if k == 'len_ref' else v
            pending.append(p)
        else:
            raise KeyError('區間修正.json：不認得 action %s' % act)
        log.append(dict(action=act, id=it.get('id')))
    missing = [dict(m, ref=refs.get(m.get('ref'), m.get('ref'))) for m in cfg.get('missing', [])]
    stats['corrections'] = log
    return out, pending, missing


def make_id(r, used):
    base = '%s|%.5f|%.5f|%s|%s' % (r['source'], r['lat'], r['lon'], r['direct_raw'], r['limit_kmh'])
    h = hashlib.sha1(base.encode('utf-8')).hexdigest()[:10]
    i, k = 'tw-' + h, 1
    while i in used:
        k += 1
        i = 'tw-%s-%d' % (h, k)
    used.add(i)
    return i


def main():
    stats = dict(rejected=[], dedup=[], limit_conflicts=[], sources=[])
    merged, grid = [], Grid()
    stage = {}
    for src in SOURCES:
        recs, st = load_source(src, stats)
        recs = dedup(recs, st, stats)
        st['after_dedup'] = len(recs)
        if src['key'] == 'national':
            for r in recs:
                grid.add(r)
                merged.append(r)
            stage['national_raw'] = st['raw_rows']
            stage['national_cleaned'] = st['cleaned']
            stage['national_dedup'] = len(recs)
            st.update(merge_added=len(recs), merge_matched=0, merge_type_filled=0, merge_limit_filled=0)
        else:
            merge_into(merged, grid, recs, st, stats)
        st['total_after'] = len(merged)
        st['title'], st['url'] = src['title'], src['url']
        stats['sources'].append(st)

    fill_section_lengths(merged, stats)
    # id 在第二批來源合併前先定好（合併會補速限、改類型，id 不能跟著變）
    used = set()
    for r in merged:
        r['_id'] = make_id(r, used)

    # 第二批來源（闖紅燈擴充；NAV-CAM-DATA-2）
    stats['extra_retyped'] = []
    for src in EXTRA_SOURCES:
        recs, st = load_source(src, stats)
        recs = dedup(recs, st, stats)
        st['after_dedup'] = len(recs)
        st['type_after_dedup'] = dict(Counter(r['type'] for r in recs))
        merge_extra(merged, grid, recs, st, stats)
        st['total_after'] = len(merged)
        st['title'], st['url'] = src['title'], src['url']
        stats['sources'].append(st)
    stats['extra_far_merged'] = []
    merge_far_same_place(merged, stats)
    promote_redlight_speed(merged, stats)

    out = []
    for r in merged:
        o = dict(
            id=r.get('_id') or make_id(r, used), lat=r['lat'], lon=r['lon'], bearing=r['bearing'],
            limit_kmh=r['limit_kmh'], type=r['type'], source=r['source'], city=r['city'], address=r['address'],
            direction=r['direction'], direct_raw=r['direct_raw'], limit_raw=r['limit_raw'],
            tags=r['tags'], sources=r['sources'], type_rule=r['type_rule'], status='active')
        if r['type'] == 'section':
            # 區間長度（只有區間點有這些欄）：len_m 公尺或 null；len_src text＝文字解析、est＝App 估計
            #（official／unverified 由 區間修正.json 指定）；len_ref 出處、len_date 出處日期
            o['len_m'] = r.get('len_m')
            o['len_src'] = 'text' if r.get('len_m') is not None else 'est'
            o['len_rule'] = r.get('len_rule') or ''
            o['len_ref'] = ''
            o['len_date'] = ''
        out.append(o)
    out, pending, missing = apply_corrections(out, used, stats)
    for o in out:
        if o['type'] == 'section' and o['len_src'] == 'text' and not o.get('len_ref'):
            # 文字里程解析出的長度：出處＝該點座標所屬資料集
            src = next((s for s in SOURCES if s['key'] == o['source']), None)
            if src:
                o['len_ref'] = '%s（政府資料開放平臺 %s）' % (src['title'], src['dataset'])
                o['len_date'] = DATASET_INFO.get(src['dataset'], ('', ''))[1][:10]
    with open(os.path.join(HERE, 'speedcams.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    cols = ['id', 'lat', 'lon', 'bearing', 'limit_kmh', 'type', 'source', 'city', 'address', 'direction',
            'direct_raw', 'limit_raw', 'tags', 'sources', 'type_rule', 'len_m', 'len_src', 'len_ref', 'len_date',
            'status', 'status_note']
    with open(os.path.join(HERE, 'speedcams.csv'), 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f)
        w.writerow(cols)
        for o in out:
            w.writerow(['' if o.get(c) is None else ('|'.join(o[c]) if isinstance(o[c], list) else o[c])
                        for c in cols])

    meta = dict(
        name='測速照相資料', version=DOWNLOAD_DATE.replace('-', ''), download_date=DOWNLOAD_DATE,
        revision=DATA_REVISION, revision_note=DATA_REVISION_NOTE,
        count=len(out),
        license='資料來源：政府資料開放平臺，依政府資料開放授權條款第1版',
        license_url='https://data.gov.tw/license',
        fields=dict(id='穩定識別碼（來源＋座標＋方向原文＋速限的雜湊）', lat='緯度 WGS84', lon='經度 WGS84',
                    bearing='被拍車輛的行進方位角（度，0＝北、順時針）；雙向或未知為 null',
                    limit_kmh='速限 km/h；未知為 null', type='fixed|redlight|redlight_speed|section|unknown（redlight_speed＝闖紅燈兼超速）',
                    source='座標來自的資料集', city='縣市（國道點為國道名稱或「國道」）', address='設置地點原文',
                    direction='one＝單向（bearing 有值）|both＝雙向|unknown', direct_raw='拍攝方向原文（區間修正改過的原文在 direct_raw_orig）',
                    limit_raw='速限原文', tags='redlight＝兼拍闖紅燈', sources='50 公尺內（第二批來源 30 公尺內）互相印證的所有資料集',
                    type_rule='類型判斷依據',
                    status='active＝使用中|suspended＝官方註記維護中（App 預設不提醒）|retired＝官方已汰除（不進 App）|merged＝重複點，已併入 merged_into（不進 App）',
                    status_note='狀態或資料疑點說明',
                    len_m='區間長度（公尺，只有 type=section 有此欄）；不知道為 null',
                    len_src='text＝由官方設置地點文字的里程起迄推得|official＝官方文件明寫長度|est＝估計（len_m 為 null 時 App 以 1.0 km 估計）|unverified＝只有民間數字（App 當估計）',
                    len_ref='長度出處（文件名稱與網址）', len_date='長度出處日期',
                    len_rule='區間長度的依據或無法取得的原因',
                    coord_src='座標不是官方座標時的來源（OSM＝依 OSM 隧道幾何推估），原座標在 orig_lat／orig_lon',
                    derived_from='由哪一筆拆出（同一區間另一方向或第二段）'),
        sources=[dict(key=s['key'],
                      dataset_page=s.get('page') or ('https://data.gov.tw/dataset/' + s['dataset']), title=s['title'],
                      download_url=s['url'], file=s['file'],
                      downloaded=(DOWNLOAD_DATE if s in EXTRA_SOURCES else FIRST_DOWNLOAD_DATE),
                      update_frequency=DATASET_INFO.get(s['dataset'], ('', ''))[0],
                      page_modified=s.get('page_modified') or DATASET_INFO.get(s['dataset'], ('', ''))[1])
                 for s in SOURCES + EXTRA_SOURCES],
        reference_documents=[dict(d, downloaded=DOWNLOAD_DATE) for d in REFERENCE_DOCUMENTS],
        status_counts=dict(Counter(o['status'] for o in out)),
        pending=pending,
        section_missing=missing,
        sha256={},
    )
    for s in SOURCES + EXTRA_SOURCES + REFERENCE_DOCUMENTS:
        for fn in [s['file']] + s.get('raw_extra', []):
            p = os.path.join(RAW, fn)
            meta['sha256'][fn] = hashlib.sha256(open(p, 'rb').read()).hexdigest()
    zp = os.path.join(RAW, 'highway_13940.zip')
    if os.path.exists(zp):
        meta['sha256']['highway_13940.zip'] = hashlib.sha256(open(zp, 'rb').read()).hexdigest()
    with open(os.path.join(HERE, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    stats['stage'] = stage
    stats['final_count'] = len(out)
    stats['type_counts'] = Counter(o['type'] for o in out)
    stats['active_type_counts'] = Counter(o['type'] for o in out if o['status'] in ('active', 'suspended'))
    stats['status_counts'] = Counter(o['status'] for o in out)
    stats['type_by_source'] = Counter('%s/%s' % (o['source'], o['type']) for o in out)
    stats['city_counts'] = Counter(o['city'] for o in out)
    stats['direction_counts'] = Counter(o['direction'] for o in out)
    stats['limit_counts'] = Counter(str(o['limit_kmh']) for o in out)
    stats['redlight_tag'] = sum(1 for o in out if 'redlight' in o['tags'])
    stats['multi_source'] = sum(1 for o in out if len(o['sources']) > 1)
    stats['unknown_direct_raw'] = Counter(o['direct_raw'] for o in out if o['direction'] == 'unknown')
    stats['limit_notes'] = Counter()
    with open(os.path.join(HERE, 'build_stats.json'), 'w', encoding='utf-8') as f:
        json.dump(stats, f, ensure_ascii=False, indent=1)
    print('final', len(out))
    print('type', dict(stats['type_counts']))
    print('active/suspended type', dict(stats['active_type_counts']), 'status', dict(stats['status_counts']),
          'promoted', stats['promoted_redlight_speed'], 'extra retyped', len(stats['extra_retyped']))
    for s in stats['sources']:
        print(s['key'], s['encoding'], 'raw', s['raw_rows'], 'note', s['note_rows'], 'rej', s['rejected'],
              'swap', s['swapped'], 'clean', s['cleaned'], 'dedup-', s['dedup_merged'], 'matched', s['merge_matched'],
              'added', s['merge_added'], 'typefill', s['merge_type_filled'], 'total', s['total_after'])


if __name__ == '__main__':
    main()
