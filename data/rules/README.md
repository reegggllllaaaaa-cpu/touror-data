# 機車左轉規則（號誌路口 × 進入方向）

產物：`moto_left_turn.csv`（一列＝一個號誌節點的一個進入方向）、`moto_left_turn_stats.json`（統計、配對率、data.gov.tw 下載紀錄）、`raw/`（下載的政府清單原檔）。
產生腳本：`scripts/rules/build_moto_left_turn.py`（可每週重建；目前**尚未**接進 workflow，也**未**列入 offline-manifest，App 接入另排）。

## 法規依據

道路交通安全規則 §99 第2項、§102 第1項第5款：機車左轉預設換入內側車道直接左轉；只有三種情況要兩段式：
① 內側車道禁行機車　② 三快車道以上單行道　③ 設有遵20（兩段式左轉標誌）。
**沒有證據就是 `unknown`，不猜**（App 對 unknown 播「看路口標誌」）。

## 欄位

| 欄位 | 說明 |
|---|---|
| signal_node_id | OSM `highway=traffic_signals` 節點 id |
| lat, lon | 該節點座標（WGS84） |
| county | 所在縣市（OSM admin_level=4 邊界，內含的市取面積最小者） |
| osm_way_id, way_dir | 進入路段與行進方向：`forward`＝沿 way 數化方向駛入節點，`backward`＝逆向駛入 |
| approach_bearing | 由節點指向來車側的方位角（0＝來車在北邊，即南下車流） |
| road_name, highway, oneway | 進入路段的 OSM 屬性 |
| lanes_dir, lanes_src | 該方向車道數與來源（`osm`／`gov`（政府 medium↑）／`inferred_v2`（車道推論 v2 medium↑）／`osm_total_le3`（雙向總數 ≤3，推得每向 ≤2）） |
| rule | `direct`／`two_stage`／`unknown` |
| basis | 判定依據（見下表；冒號後為原始清單文字） |
| confidence | `high`／`medium`／`low`／`none`（unknown） |

## 判定順序（先中先贏）

| basis | 條件 | rule | 信心 |
|---|---|---|---|
| approach_moto_banned | 進入路段禁行機車（OSM motorcycle=no／access=no、臺北禁行機車對位） | unknown | none |
| taipei_exception_direct | 臺北「三(含)車道以上例外開放機車直接左轉」路口，40 m 內號誌節點＋來車側 ±45° | direct | high |
| taipei_exception_two_stage | 臺北「二車道路段例外實施兩段式左轉」 | two_stage | high |
| osm_sign_zun20 | OSM traffic_sign 含 遵20／兩段式（目前全台 0 個） | two_stage | high |
| osm_inner_lane_moto_no | OSM `motorcycle:lanes(:forward/:backward)` 第一個（最內側）為 no（條件①） | two_stage | high |
| taipei_lane3_inner_ban | 臺北「開放機車行駛第3車道」路段＝內側車道禁行機車（條件①） | two_stage | medium |
| oneway_3plus_osm/gov/inferred_v2 | 單行道且 ≥3 車道（條件②） | two_stage | medium；trunk/primary/secondary 為 low（多為分隔島雙線化，法律上未必是單行道） |
| penghu_list | 澎湖縣設有機車兩段式左轉路口（路名配對，路口層級） | two_stage | medium |
| kaohsiung_2016_enforce | 高雄市105年取締未依規定兩段式左轉前十大路口（舊資料） | two_stage | low |
| taipei_rule_3lane / taipei_rule_2lane | 臺北市管制原則：該方向 ≥3 車道兩段式、≤2 車道直接左轉（例外即上面兩份清冊） | two_stage／direct | medium |
| no_evidence | 其餘 | unknown | none |

## 來源、授權、更新方式

| 來源 | 授權 | 更新 |
|---|---|---|
| OpenStreetMap（`資料/搜尋/路網來源/osm/taiwan-latest.osm.pbf`） | ODbL 1.0，© OpenStreetMap contributors | 重新下載 pbf 後重跑 |
| 臺北市機車例外管制路口列表（data.gov.tw 158073；已由 `資料/車道/scripts/conflate_gov_lanes.py` 定位成 `moto_intersection_exceptions.csv`） | 政府資料開放授權條款第1版 | 先重跑 conflate_gov_lanes.py |
| 臺北市平面道路禁行機車、開放機車行駛第3車道（同上，`moto_restrictions_for_routing.csv`、`data/lanes/gov_lane_attrs.csv`） | 政府資料開放授權條款第1版 | 同上 |
| 澎湖縣設有機車兩段式左轉路口（data.gov.tw 157480，28 處） | 政府資料開放授權條款第1版 | 腳本自動下載（`--no-download` 可略過） |
| 高雄市105年度取締未依規定兩段式左轉前十大路口（data.gov.tw 47188） | 政府資料開放授權條款第1版 | 同上（2016 年資料，僅 low） |
| 車道推論 v2（WSL `/root/lanesinfer/out/inferred_lanes.csv`，只用 medium↑） | 衍生自 OSM＋政府資料 | 先跑 `scripts/lanes_infer/wsl_run.sh`；`--inferred` 可指定檔案 |

data.gov.tw 查詢結果（2026-11-26）：全國只有臺北（例外清冊）、澎湖（兩段式路口）、高雄（2016 取締前十大）有相關資料；新北、桃園、臺中、臺南等**沒有**兩段式左轉路口清單。

## 重建

```
python -I scripts/rules/build_moto_left_turn.py            # 約 1.5 分鐘，自動以低優先權執行
```

## 已知缺口

- OSM 台灣幾乎沒有遵20標誌與 `motorcycle:lanes` 標記，全台（臺北以外）絕大多數為 unknown。
- 澎湖清單多以地標描述，28 處只對到 2 處；高雄 10 處對到 2 處。
- 臺北例外清冊 114 筆中 5 筆原始無座標、3 筆 40 m 內沒有 OSM 號誌節點。
- 「三快車道」以總車道數近似，未扣除機慢車道；雙線化分隔道路被當成單行道（已降為 low）。
