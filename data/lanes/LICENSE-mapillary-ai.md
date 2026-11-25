# mapillary_ai_lanes.csv 授權與出處

**授權：創用 CC 姓名標示－相同方式分享 4.0 國際（CC BY-SA 4.0）**
https://creativecommons.org/licenses/by-sa/4.0/deed.zh-hant

**出處標示：** 含 Mapillary 貢獻者影像判讀，CC BY-SA 4.0（© Mapillary contributors, https://www.mapillary.com）。

## 這份資料是什麼

- `mapillary_ai_lanes.csv`：高雄市、屏東縣號誌路口的進入方向，由多模態 AI 直接觀看 Mapillary 街景照片判讀的車道數（`lanes`）與地面轉向箭頭（`turn_lanes`，OSM `turn:lanes` 格式，由左到右、`|` 分隔，空白＝該車道看不到箭頭）。
- 它是 Mapillary 影像（CC BY-SA 4.0）的衍生資料，因此依「相同方式分享」條款以同一授權公開。使用、修改、再散布時須保留上方出處標示，並以 CC BY-SA 4.0 釋出衍生物。
- 欄位 `image_ids` 列出判讀所用的 Mapillary 影像 id，可在 `https://www.mapillary.com/app/?pKey=<id>` 查看原圖。
- `osm_way_id` 只用於對應 OpenStreetMap 道路（ODbL）；本檔不含 OSM 資料內容，也**沒有寫回 OSM**（OSM 規範要求 AI 產生的編輯需人工逐筆確認）。

## 品質與限制（請一併閱讀）

- 未經人工逐筆確認。收錄規則保守：同一方向兩張不同照片判讀一致且箭頭清楚才收 `turn_lanes`（`confidence=medium`）；只有車道數一致時只收 `lanes`（`confidence=low`）；與 OSM 或政府資料衝突的一律不收。
- 只記照片上看得到的地面箭頭，不依號誌、標誌或慣例推測。
- 車道計數只算汽車可行駛的一般車道；機車停等區、待轉區、以實線隔開的路肩不算。
- 台北盲判驗證（30 方向）：車道數一致約 90%、轉向完全一致約 40%（照片清楚時車道數 7/7）。使用端應以「AI 判讀」來源淡化顯示（底圖屬性 `lanes_source=mapillary_ai`、`turn_lanes_source=mapillary_ai`）。

`mapillary_pending.csv`（每週自動重查清單）同樣以 CC BY-SA 4.0 公開。
