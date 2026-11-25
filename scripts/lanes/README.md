# 車道 AI 判讀管線（Mapillary 照片 → `data/lanes/mapillary_ai_lanes.csv`）

高雄＋屏東號誌路口：用 Mapillary 街景照片，經多模態模型判讀車道數與轉向箭頭，產生自家資料集（CC BY-SA 4.0，見 `data/lanes/LICENSE-mapillary-ai.md`）。底圖建置透過 `MAPILLARY_LANES_CSV` 帶入，優先序 osm > gov > mapillary_ai > inferred。

## 檔案

| 檔案 | 用途 |
|---|---|
| `kp.py` | `extract`／`select [N]`／`sheets`／`elevcheck`／`decide` 各階段 |
| `kp_record.py` | 把判讀批次 JSON 寫進 `work/kp_judgments.json` |
| `mly.py` | Mapillary API；token 只從 `%USERPROFILE%\.claude\secrets\mapillary-token.txt` 讀，不印、不寫檔 |
| `wsl_build_basemap.sh` | 本機 WSL 重建底圖並驗證（對線上舊底圖與上一版） |
| `preview_mly.py` | 統計 `lanes_source=mapillary_ai` 並畫路口預覽 PNG |

工作區（查詢快取、照片、拼圖、判讀紀錄）不進 repo，預設在 `<repo>/../資料/車道/mapillary/視覺判讀`，可用 `LANES_WORKDIR` 改。輸入路徑可用 `LANES_PBF`、`LANES_SIGNALS`、`LANES_RIDES` 改。

## 收錄規則（`decide`）

- 選照片：停止線前 20–45 m、朝路口（≤30°）、白天 07–16 時、非全景，最新優先，盡量不同序列。
- **高架排除**：照片 15 m 內最近的道路是 `bridge`／`layer>0`、而目標 way 不是高架 → 該照片不用。判讀時看到照片其實在高架上，註記「照片在高架道路上」，`elevcheck` 會作廢該方向、改列 pending 重選。
- turn_lanes：兩張都清楚且一致 → `confidence=medium`；一張清楚（所有車道箭頭可見）、另一張不矛盾（車道數相同或看不清、箭頭不衝突）→ `low`。
- 只收 lanes：兩張車道數一致 → `low`（turn_lanes 留空）。
- 與 OSM lanes／turn:lanes 或政府資料（非 low）衝突 → 不收，列 `work/kp_conflicts.json`。
- way 長度 > 400 m → 不收，pending 原因「way 過長需切分」。way 沒在該路口斷開 → 不收 turn_lanes。
- 雙向道路只判讀一個方向時，底圖（`Lanes.java`）不以推定值補另一方向，該 way 維持原來源。
- 其他 → `data/lanes/mapillary_pending.csv`（每週重查）。

## 每週流程

1. `python scripts/lanes/kp.py extract`（OSM 或號誌更新後才需要）
2. `python scripts/lanes/kp.py select 150` → `python scripts/lanes/kp.py sheets`
3. 判讀（需要多模態，所以在本機用 Claude Code 非互動模式）：
   ```
   claude -p "讀 <工作區>/照片/kp/ 中尚未判讀的 S###.jpg（標籤見 work/kp_manifest.json 的 sheet 欄位）。每個方向 a、b 兩張照片各記 lanes（看不清填 null）、tl（OSM turn:lanes 格式，只記看得到的箭頭，看不到留空、不推測；右側車道往左上斜的箭頭是透視造成的直行）、vis（none／partial／clear，clear＝所有車道箭頭都看得到）。照片若拍在高架道路上，note 寫「照片在高架道路上」。輸出 JSON 到 <工作區>/work/kp_batch_<日期>.json，格式見 scripts/lanes/kp_record.py。" --allowedTools Read,Write
   ```
   每批是獨立短任務（一次約 20–25 張拼圖），不要在同一段對話累積大量圖片。
4. `python scripts/lanes/kp_record.py <批次.json>` → `python scripts/lanes/kp.py elevcheck` → `python scripts/lanes/kp.py decide`
5. 本機驗證：`wsl -e bash scripts/lanes/wsl_build_basemap.sh`，再 `python scripts/lanes/preview_mly.py <pmtiles> <out.png> <lat> <lon>` 看預覽。
6. commit `data/lanes/*.csv`；push 後週日的 `weekly-roadnet.yml` 透過 `MAPILLARY_LANES_CSV` 帶入底圖。

## 本機排程

- `run-weekly-lanes.ps1`：依序跑 select 150 → sheets → `claude -p`（提示詞 `judge_prompt.md`，`--model sonnet --allowedTools Read,Write --max-turns 40`，每批 6 張拼圖、獨立短任務）→ `kp_batch.py check` 驗格式 → kp_record → elevcheck → decide；`data/lanes/` 有變動才 commit，`pull --rebase` 後 push origin main。沒有待判讀方向就結束、不呼叫 claude。log：`%USERPROFILE%\資料中心\工作日誌\lanes_weekly.log`（不含 token）。`-DryRun`：只判讀 1 個方向測格式，不寫紀錄、不 commit。
- `register-weekly-lanes.ps1`：註冊工作排程「Touror 車道判讀」，每週日 02:00（週一 weekly-roadnet 之前），錯過補跑、時限 3 小時、低優先權、只在使用者登入時執行（`claude -p` 需要登入狀態）。由使用者自己執行：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\lanes\register-weekly-lanes.ps1`。
- `kp_batch.py`：列待判讀方向（todo）、乾跑取樣（sample）、檢查批次 JSON 格式（check）。
