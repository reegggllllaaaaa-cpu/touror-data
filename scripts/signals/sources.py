# -*- coding: utf-8 -*-
"""紅綠燈相關政府資料來源（都在 data.gov.tw，不需要金鑰）。下載網址每次由資料集 API 取得，不寫死。"""

# 號誌位置補點：併進 taiwan-signals.bin（與 OSM 去重後才加）
GOV_SIGNAL_SOURCES = [
    dict(key='taipei', dataset='121890', file='taipei_signals_121890.csv', fmt='CSV', city='臺北市',
         title='臺北市號誌位置', lat=('WGS緯度座標',), lon=('WGS經度座標',), name=('地點',)),
    dict(key='changhua', dataset='37695', file='changhua_signals_37695.csv', fmt='CSV', city='彰化縣',
         title='彰化縣路口號誌經緯度資料', lat=('緯度',), lon=('經度',), name=('路口名稱',)),
    dict(key='penghu', dataset='113153', file='penghu_signals_113153.csv', fmt='CSV', city='澎湖縣',
         title='澎湖縣紅綠燈號誌位置一覽表', gps=('GPS',), name=('位置路口名稱',)),
    dict(key='tainan', dataset='79978', file='tainan_signals_79978.csv', fmt='CSV', city='臺南市',
         title='臺南市交通號誌(路口紅綠燈)4G_WiFi點位資料', lat=('熱點緯度',), lon=('熱點經度',), name=('熱點名稱', '地址')),
    dict(key='taichung', dataset='103743', file='taichung_signals.csv', fmt='TAICHUNG_CSV', city='臺中市',
         static_path='data/gov-signals',
         title='臺中市號誌燈圖資', lat=('lat',), lon=('lon',), name=('comboroad',)),
    # 以下四個來源由 資料/紅綠燈 的前處理（座標轉換、吸附 touror_junctions 路口、30 m 去重）產生同一個點位 CSV，
    # 依 source 欄位分開計數與標示出處（2026-11-25）
    dict(key='tainan_shp', dataset='137239', file='gov_signals_extra.csv', fmt='POINTS_CSV', city='臺南市',
         static_path='data/gov-signals', csv_source='tainan_shp_137239',
         # SHP 的 SigType 只有「三色或閃光」一類，無法排除閃光號誌（不必停車）→ 標低可信度（報告與統計可見）
         confidence='low', confidence_note='SigType 不分三色／閃光，可能含閃光號誌',
         title='臺南市公共管線圖資-交通號誌系統號誌（使用中）', lat=('lat',), lon=('lon',), name=('name',)),
    dict(key='kaohsiung_tech', dataset='176561', file='gov_signals_extra.csv', fmt='POINTS_CSV', city='高雄市',
         static_path='data/gov-signals', csv_source='kaohsiung_176561',
         title='高雄市115年路口科技執法監測系統設置地點（取締項目含闖紅燈）', lat=('lat',), lon=('lon',), name=('name',)),
    dict(key='hsinchu', dataset='67783', file='gov_signals_extra.csv', fmt='POINTS_CSV', city='新竹市',
         static_path='data/gov-signals', csv_source='hsinchu_67783',
         title='新竹市號誌化路口（路名交叉點地理編碼）', lat=('lat',), lon=('lon',), name=('name',)),
    dict(key='user_verified', dataset='', file='gov_signals_extra.csv', fmt='POINTS_CSV', city='使用者實地確認',
         static_path='data/gov-signals', csv_source='user_verified', url='',
         title='Touror 使用者實地確認新設號誌', lat=('lat',), lon=('lon',), name=('name',)),
]

# 時制（秒數）：臺北市 145720 三個資源＋澎湖 113153（同一份號誌表內含綠／黃燈秒數）
TIMING_SOURCES = [
    dict(key='taipei_plan_table', dataset='145720', file='taipei_timing_plan_table.json', fmt='JSON', match='左半邊',
         title='臺北市路口號誌時制計畫（三合一報表左半邊：時段）'),
    dict(key='taipei_plan', dataset='145720', file='taipei_timing_plan.json', fmt='JSON', match='右半邊',
         title='臺北市路口號誌時制計畫（三合一報表右半邊：時相與週期）'),
    dict(key='taipei_ic', dataset='145720', file='taipei_timing_ic.csv', fmt='CSV',
         title='臺北市政府交通局路口時制號誌資料（路口座標）'),
    dict(key='penghu', dataset='113153', file='penghu_signals_113153.csv', fmt='CSV',
         title='澎湖縣紅綠燈號誌位置一覽表（含綠燈、黃燈秒數）'),
]

# 國道速限（40476）
SPEED_LIMIT_SOURCES = [
    dict(key='freeway', dataset='40476', file='freeway_limits_40476.csv', fmt='CSV', title='國道各主要路段速限表'),
]
