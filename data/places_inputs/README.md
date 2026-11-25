# 店家庫建置的快取輸入（每週 workflow 用）

`scripts/places/build_all.sh` 每次會先嘗試線上抓新的；抓不到（被擋、逾時、筆數掉超過 10%）才用這裡的快取。實際用了哪一份，記在每次執行的 artifact `places-candidate/report/inputs.json`。

| 檔案 | 用途 | 來源 | CI 會自動抓新的嗎 | 人工更新方式 |
|---|---|---|---|---|
| 名冊/7-11_emap.csv | 7-11 分店名升格、去重、補名 | 統一超商 emap（emap.pcsc.com.tw） | 會（fetch_chain_rosters.py），失敗用快取 | 本機跑 `python scripts/places/fetch_chain_rosters.py --out data/places_inputs/名冊` 後 commit |
| 名冊/OK_okmart.csv | OK 分店名 | okmart.com.tw 門市查詢 | 同上 | 同上 |
| 真值/32086.csv | 全家、萊爾富等商工登記分公司名（全家／萊爾富官網擋爬，改用這份） | data.gov.tw 資料集 32086（商工登記分公司） | 不會 | 到 data.gov.tw/dataset/32086 下載 CSV 覆蓋，約每季一次 |
| 真值/Attraction-json.zip、Hotel-json.zip | 政府庫：觀光署景點、旅宿 | media.taiwan.net.tw XMLReleaseAll_public | 會，失敗用快取 | 下載同名檔覆蓋 |
| gov_raw/cpc.json、post.csv | 政府庫：中油加油站、郵局 | vipmbr.cpc.com.tw、post.gov.tw 開放資料 | 會，失敗用快取 | 下載同名檔覆蓋 |
| gov_raw/police.zip | 目前 build_places_gov.py 沒有使用，保留備查 | — | — | — |
| 名冊2/ | 名冊補名：教育部學校名錄（moe_*）、健保特約醫事機構（nhi）、全國宗教寺廟／教會（temple.xml、church.xml） | data.gov.tw 6086–6091、168341、內政部全國宗教資訊 | 不會（原本是人工下載） | 到對應資料集下載同名檔覆蓋，約每半年一次 |

門牌包 `touror_addr.bin` 不放這裡：workflow 直接下載 manifest 的線上版（kind `addr`）。
