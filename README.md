# touror-data：Touror 離線導航的公開資料

Touror（途樂）是台灣重機騎士用的完全離線導航 App。這個倉庫放 App 會自動更新的**小型公開資料**（合計約 4 MB）與產生它們的腳本，
由 GitHub Actions 定期從政府開放資料與 OpenStreetMap 重建，發布到 GitHub Pages：

`https://reegggllllaaaaa-cpu.github.io/touror-data/manifest.json`

App 只 GET 這些靜態檔：不帶位置、不帶任何識別碼。大型資料（路網、底圖、地名、門牌）不在這裡，由使用者手動匯入。

## 資料

| 名稱（manifest name） | 檔案 | 內容 | 更新 | 授權 |
|---|---|---|---|---|
| speedcams | data/speedcams/speedcams.json | 測速照相（固定式、闖紅燈、闖紅燈兼超速、區間起點、類型不明），App 格式 | 每日（核心）+ 每週（縣市） | 政府資料開放授權條款第 1 版 |
| speedcams-full | data/speedcams/speedcams-full.csv | 測速正本（全部欄位，含已汰除、重複點與出處） | 每日（核心）+ 每週（縣市） | 同上 |
| mobile-hotspots | data/speedcams/mobile-hotspots.json | 移動式測速常出沒路段（高雄、臺中公布的路段，以 OSM 路名點當參考點） | 每週 | 同上；參考點 ODbL |
| taiwan-signals | data/signals/taiwan-signals.bin | 紅綠燈位置（OSM 號誌節點＋臺北、彰化、澎湖、臺南、臺中政府號誌位置補點） | 每週 | ODbL 1.0（補點：政府資料開放授權條款） |
| taiwan-reststops | data/reststops/taiwan-reststops.bin | 休息點（服務區、休息區、加油站、便利商店、廁所、觀景點、咖啡廳） | 每週 | ODbL 1.0 |
| signal-timing-taipei | data/signals/signal-timing-taipei.json | 臺北市路口號誌時制精簡版（各路口各星期、時段的週期與各分相紅燈秒數） | 每月 | 政府資料開放授權條款第 1 版 |
| signal-timing-penghu | data/signals/signal-timing-penghu.json | 澎湖縣號誌時制（資料檔名 2018，可能偏舊；App 目前不讀） | 每月 | 同上 |
| speed-limits-national | data/speedlimits/speed-limits-national.json | 國道各主要路段速限 | 每月 | 同上 |

`manifest.json` 列出每個檔的 name、path、version（yyyymmdd.rev）、date、count、sha256、bytes、minAppVersion、license、attribution、sources。
`CHANGELOG.md` 是每次發布的新增／移除／修改筆數。

### 來源與標示
- **政府資料開放平臺（data.gov.tw）**，依「政府資料開放授權條款－第 1 版」（https://data.gov.tw/license）：可商業利用，須標示來源。
  測速：內政部警政署 7320、國道公路警察局 13940／100856 及各縣市警察局資料集（完整清單見 manifest 的 sources）；
  號誌位置：臺北市 121890、彰化縣 37695、澎湖縣 113153、臺南市 79978、臺中市 103743；時制：臺北市 145720、澎湖縣 113153；國道速限：40476。
- **OpenStreetMap**：© OpenStreetMap contributors，依 Open Database License（ODbL）1.0（https://opendatacommons.org/licenses/odbl/）。
  taiwan-signals.bin、taiwan-reststops.bin 與常出沒路段的參考點是 OSM 的衍生資料庫；本倉庫公開資料庫與產生方式（scripts/）即為 ODbL 要求的提供方式。
- 測速資料只供提醒減速，位置僅供參考，請遵守現場速限標誌。

## 更新週期與管線

| Workflow | 時間（台灣） | 做什麼 |
|---|---|---|
| `daily-speedcams.yml` | 每天 03:00 | 下載全國核心測速來源（7320、100856）→ 重建測速 → `驗證.py` → 合併人工建檔 → 筆數比上一版少 > 5% 或驗證失敗就**不發布並開 issue** → 有變更才 commit → 部署 Pages |
| `weekly-osm.yml` | 每週一 02:00 | 下載 Geofabrik 台灣 extract（核對 MD5）→ 重建紅綠燈與休息點 → 併政府號誌補點（與 OSM 40 m 內去重、閃光號誌不收）→ 下載各縣市與移動式測速原始檔 → 列出補點數 → 同上 |
| `monthly-timing.yml` | 每月 2 日 04:00 | 臺北 145720、澎湖 113153 時制，國道速限 40476 → 同上 |
| `validate-on-push.yml` | PR／推送 | 單元測試、`scripts/validate.py`（manifest 雜湊與大小、每個檔用讀取器解析、人工建檔表）、測速乾跑 |

- 只用不需要金鑰的來源；只用內建 `GITHUB_TOKEN`（開 issue、commit）。
- 對 data.gov.tw 資料集 API 請求間隔 ≥ 40 秒（`TOUROR_FETCH_INTERVAL`；下載實際檔案不套用），失敗重試 1 次（30 秒），單一來源總時間上限 120 秒。任一來源失敗或逾時時，只要 `raw/` 裡已有舊檔就標「沿用舊檔」且整體不失敗；無舊檔才失敗。
- 版本 `yyyymmdd.rev`：內容（不含 version 欄）沒變就沿用；有變就用今天日期，同一天再發布 rev＋1；一定比資料本身的版本新（App 才會下載）。

### 本機乾跑（等價於各 workflow）
```
python -I scripts/dryrun.py validate                 # validate-on-push.yml
python -I scripts/dryrun.py daily [--fetch]          # daily-speedcams.yml（--fetch 才真的連網）
python -I scripts/dryrun.py weekly --pbf <pbf>       # weekly-osm.yml（需要 pip install osmium）
python -I scripts/dryrun.py monthly [--fetch]        # monthly-timing.yml
```
乾跑只寫 `build/`。真的更新本機 `data/`：`python -I scripts/publish.py <群組…>`（群組：speedcams、signals、reststops、timing、speedlimits、all）。

### 禁行機車路段（curated/禁行機車.csv）

每週路網重建時 `apply_curated.py --moto-bans` 依 osm_way_id 對位（核對 way 仍存在、是道路、路名與公告相符、方向與單行一致），每列都寫進 `build/rn/禁行機車對應清單.csv`（成功／對不到＋原因）與 `禁行機車統計.json`。牌照照規則表慣例：四種牌照都禁的才寫 `motorcycle=no` 進 tiles；只禁綠白（禁3，大型重機除外）的不能寫進 tiles（會連黃紅一起擋），改輸出 `禁行機車走廊.json`（rules_with_plate 格式＋corridor），由 App 依牌照排除重算。`verify.py` 的牌照案例（cases.json 的 `mb_*`）照 App 做法排除走廊後檢查：白牌繞開、黃紅牌仍走。來源 CSV 由 `資料/車道/scripts/conflate_gov_lanes.py` 產生，更新後複製進來並保留「採用」欄。

### 通行限制資料集上傳（touror_restrictions.sqlite，kind=restrictions，手動）

資料集由 `資料/搜尋/restrictions/build_restrictions.py` 本機建置，不在每週 workflow 內。上傳步驟（與其他資料相同：Release 資產＋manifest sha256）：

1. `gh release view offline-<版本> || gh release create offline-<版本> --title "通行限制資料集 <版本>" --notes "OSM＋政府公告整理的通行限制屬性。© OpenStreetMap contributors，ODbL 1.0；政府資料開放授權第 1 版。"`
2. `gh release upload offline-<版本> <路徑>/touror_restrictions.sqlite`
3. `python -I scripts/roadnet/update_manifest.py offline-manifest.json <路徑>/touror_restrictions.sqlite offline-<版本> <版本> <owner/repo> restrictions "<說明>"`（第一次要給說明才會新增該項）
4. 下載 Release 上的檔案核對 sha256 與 manifest 相同，再 commit／push manifest。

注意：舊版 App 遇到不認得的 kind 會整份拒絕 manifest（見 scripts/validate.py），push 含 restrictions 的 manifest 前要先確認使用者裝的 App 已支援略過未知 kind。

## 人工建檔（data/curated/）

人工資料**優先於**自動抓取。四張表（UTF-8 CSV，Excel 可直接開）：

| 檔案 | 用途 | type |
|---|---|---|
| section-ends.csv | 區間測速終點：補長度（len_m）與終點座標；ref_id＝區間起點 id | section_end |
| redlight-additions.csv | 闖紅燈照相補點；ref_id 有值＝覆蓋或撤除該測速點 | redlight／redlight_speed |
| mobile-segments.csv | 移動式測速常出沒路段（city、district、road）；ref_id＝「縣市\|行政區\|路名」 | mobile_hotspot |
| signal-timing.csv | 路口秒數（cycle_s、red_s、days 星期遮罩、time_from、time_to）；ref_id＝時制路口 id | signal_timing |

每列必填（不適用填 `-`）：lat、lon、bearing、direction（one／both／-）、type、limit_kmh、len_m、source、source_url（網址或現地照片編號）、
checked_date（YYYY-MM-DD）、checker（查核人）、status。
- `active`：使用中。`suspended`：暫停（官方註記維護中；測速點在 App 預設不提醒）。`retired`：已撤除（從發布資料移除）。
- 區間測速的長度、拆分、合併、汰除另有 `section-corrections.json`（建置腳本內建的修正格式，逐筆附出處）。

## 回報錯誤

到本倉庫開 issue，寫：地點（座標或地圖截圖）、哪一筆資料（id）、錯在哪裡、出處（公告網址或現地照片）。
查證後會更新人工建檔表，下一次發布生效。請勿在騎車時操作手機回報。

## 倉庫結構
```
data/<類別>/…            發布的資料檔（App 下載）
data/curated/            人工建檔表
manifest.json            檔案清單（App 先讀這個）
CHANGELOG.md             每次發布的增刪改筆數
raw/speedcams/           測速原始檔快照（含非 API 來源的 PDF 與參考文件、路名參考點 road-anchors.tsv.gz）
scripts/                 下載、建置、驗證、發布（只用 Python 標準庫；OSM 部分需要 pyosmium）
scripts/vendor/          開發工作區的建置腳本原封不動的複本（由 scripts/tools/sync_from_workspace.py 同步）
scripts/tests/           單元測試
.github/workflows/       排程
```
`raw/signals`、`raw/timing`、`raw/osm`、`build/`、`site/` 每次執行時產生，不進版控。
