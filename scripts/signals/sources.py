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
    # 使用者回報候選路口，以 Mapillary 街景照片（CC BY-SA）由 AI 看圖判定有號誌燈頭者；照片 id 見 資料\紅綠燈\人工補點.csv
    dict(key='mapillary_ai', dataset='', file='gov_signals_extra.csv', fmt='POINTS_CSV', city='Mapillary 照片判讀',
         static_path='data/gov-signals', csv_source='mapillary-ai', url='https://www.mapillary.com/',
         title='Touror 候選路口 Mapillary 街景照片 AI 判讀號誌', lat=('lat',), lon=('lon',), name=('name',)),
    # Mapillary map_features 號誌燈頭偵測（全台，fetch_mly_signals.py；照片≥2、25 m 內有平面路口、原本無號誌；座標取路口）
    dict(key='mapillary_detect', dataset='', file='gov_signals_extra.csv', fmt='POINTS_CSV', city='Mapillary 號誌偵測',
         static_path='data/gov-signals', csv_source='mapillary-detect', url='https://www.mapillary.com/',
         confidence='medium', confidence_note='單一來源（Mapillary 物件偵測）',
         title='Mapillary 地圖物件：號誌燈頭偵測（object--traffic-light--*）', lat=('lat',), lon=('lon',), name=('name',)),
    # 警政署交通事故（13139 當年度 A2 即時＋158865 A1／A2 逐案；accident_signals.py candidates）：號誌種類＝行車管制號誌
    # （排除閃光）、道路型態＝交岔路；30 m 聚群 ≥3 件；群心 30 m 內吸附路口庫平面路口；路口 40 m 內已有號誌不加；座標取路口
    dict(key='npa_accident', dataset='13139', file='gov_signals_extra.csv', fmt='POINTS_CSV', city='警政署交通事故資料',
         static_path='data/gov-signals', csv_source='npa-accident',
         confidence='medium', confidence_note='間接證據（事故筆錄記載號誌化路口，座標為報案地點）',
         title='警政署交通事故資料（A1／A2）號誌化交岔路口事故 ≥3 件', lat=('lat',), lon=('lon',), name=('name',)),
    # 疑似未收錄號誌路口（taiwan-signals-quality.bin）中獨立線索 ≥2 類者升級（accident_signals.py upgrade）
    dict(key='suspect_multi', dataset='', file='gov_signals_extra.csv', fmt='POINTS_CSV', city='疑似漏燈多線索升級',
         static_path='data/gov-signals', csv_source='suspect-multi', url='',
         confidence='medium', confidence_note='OSM 號誌節點／Mapillary 燈頭／主幹道交會／事故資料 兩類以上同時命中',
         title='疑似未收錄號誌路口：兩類以上獨立線索升級', lat=('lat',), lon=('lon',), name=('name',)),
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
