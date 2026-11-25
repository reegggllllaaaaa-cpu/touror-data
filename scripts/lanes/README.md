# 車道 AI 判讀管線（Mapillary 照片 → `data/lanes/mapillary_ai_lanes.csv`）

全台號誌路口（2026-11-25 起；`LANES_REGION=kp` 可回到舊範圍高雄＋屏東）：用 Mapillary 街景照片，經多模態模型（後備模型鏈自動接手：Codex → agy → Copilot → Claude haiku，見下方「判讀後備鏈」）判讀車道數與轉向箭頭，產生自家資料集（CC BY-SA 4.0，見 `data/lanes/LICENSE-mapillary-ai.md`）。底圖建置透過 `MAPILLARY_LANES_CSV` 帶入，優先序 osm > gov > mapillary_ai > inferred。

## 檔案

| 檔案 | 用途 |
|---|---|
| `kp.py` | `extract`／`select [N]`／`sheets`／`elevcheck`／`decide` 各階段 |
| `judge_prompt_codex.md` | 後備鏈各層共用的判讀提示詞（codex 用 `-i`、copilot 用 `--attachment` 附圖，agy／claude 用讀檔工具開圖；最後回覆只輸出 JSON） |
| `judge_prompt.md` | 舊的 Claude 手動判讀提示詞（寫檔版，排程已不用） |
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

## 範圍、每週上限與不重複判讀

- `extract` 收全台號誌（81,641 筆），只保留號誌附近（約 300–600 m 格網）的道路，避免整島路網進記憶體；改範圍或 OSM／號誌更新後重跑 `extract`。
- `select N`：N＝本週最多備妥幾個待判讀方向（排程預設 300）。Mapillary 查詢上限 900 次：先重查到期（`LANES_RECHECK_DAYS`，預設 7 天）的 pending／衝突方向，最多用 1/3；其餘給從沒查過的新路口（騎過的路口 → 兩條主幹道 → 主次幹道）。
- 已判過的方向（`work/kp_judgments.json`）只有出現更新的照片才會重選、給新標籤重判（`kp.needs_judge`：紀錄的標籤 ≠ 目前標籤）；`decide` 不把舊判讀套到新照片。
- 舊版 bug（已修）：重選有新照片的已判方向時，manifest 被整筆覆寫、標籤消失，但 sheets／todo 又以「已判過」排除，所以永遠卡在 ready（2026-11-25 select 的 ready 150 有 16 個是這種）。
- 撞號稽核（2026-11-25，全量）：`mapillary_ai_lanes.csv` 16 列逐列比對判讀紀錄標籤、manifest 三份快照（15:49／21:57／22:06）、批次檔與拼圖產生時間。4 列的標籤曾被其他方向重用（A118／A125／A135／A146），但重用方向的拼圖（S039–S046，16:35 產生）晚於判讀批次 `kp_batch_20261125b.json`（15:52），判讀只可能看到原方向的拼圖，且 S034 拼圖內容（brg=1、27m／37m 2026-04-05）與 118126010 forward 吻合；重用方向沒有任何判讀紀錄、目前都已重編新標籤待判讀。結論：16 列全部保留，無改掛、無移除。

## 每週流程

1. `python scripts/lanes/kp.py extract`（OSM 或號誌更新後才需要）
2. `python scripts/lanes/kp.py select 300` → `python scripts/lanes/kp.py sheets`
3. 判讀（需要多模態；排程用後備鏈，Codex 層的指令：`codex exec --skip-git-repo-check --ephemeral -s read-only -c model_reasoning_effort=high -i <拼圖>... -o <批次.json> -`，提示詞 `judge_prompt_codex.md`。推理強度不要用 none／low：實測會把平面道路誤判成高架。以下是舊的 Claude 手動做法，只在 Codex 不能用時備援）：
   ```
   claude -p "讀 <工作區>/照片/kp/ 中尚未判讀的 S###.jpg（標籤見 work/kp_manifest.json 的 sheet 欄位）。每個方向 a、b 兩張照片各記 lanes（看不清填 null）、tl（OSM turn:lanes 格式，只記看得到的箭頭，看不到留空、不推測；右側車道往左上斜的箭頭是透視造成的直行）、vis（none／partial／clear，clear＝所有車道箭頭都看得到）。照片若拍在高架道路上，note 寫「照片在高架道路上」。輸出 JSON 到 <工作區>/work/kp_batch_<日期>.json，格式見 scripts/lanes/kp_record.py。" --allowedTools Read,Write
   ```
   每批是獨立短任務（一次約 20–25 張拼圖），不要在同一段對話累積大量圖片。
4. `python scripts/lanes/kp_record.py <批次.json>` → `python scripts/lanes/kp.py elevcheck` → `python scripts/lanes/kp.py decide`
5. 本機驗證：`wsl -e bash scripts/lanes/wsl_build_basemap.sh`，再 `python scripts/lanes/preview_mly.py <pmtiles> <out.png> <lat> <lon>` 看預覽。
6. commit `data/lanes/*.csv`；push 後週日的 `weekly-roadnet.yml` 透過 `MAPILLARY_LANES_CSV` 帶入底圖。

## 本機排程

- `run-weekly-lanes.ps1`：依序跑 select 300 → sheets → 判讀（`-Engine auto` 預設＝後備鏈，每批 6 張拼圖）→ `kp_batch.py check` 驗格式 → kp_record → elevcheck → decide；`data/lanes/` 有變動才 commit，`pull --rebase` 後 push origin main。沒有待判讀方向就結束、不呼叫模型。`-NoCommit`：照常判讀與 decide，但不 commit／push。log：`%USERPROFILE%\資料中心\工作日誌\lanes_weekly.log`（不含 token）。`-DryRun`：只判讀 1 個方向測格式，不寫紀錄、不 commit。
- `register-weekly-lanes.ps1`：註冊工作排程「Touror 車道判讀」，每週日 02:00（週一 weekly-roadnet 之前），錯過補跑、時限 3 小時、低優先權、只在使用者登入時執行（`codex` 用使用者的 ChatGPT 登入）。由使用者自己執行：`powershell -NoProfile -ExecutionPolicy Bypass -File scripts\lanes\register-weekly-lanes.ps1`。
- `kp_batch.py`：列待判讀方向（todo）、乾跑取樣（sample）、檢查批次 JSON 格式（check）。

## 判讀後備鏈（2026-11-25 起；ChatGPT Plus 11-30 到期、Claude 12/2 起降 Pro）

每批依序嘗試，第一個通過 `kp_batch.py check` 的層採用：

| 順序 | 層 | 呼叫方式 | 實測（S034，A135 答案 2 車道 `through\|right`） |
|---|---|---|---|
| 1 | codex | `codex exec -i`，`-CodexEffort high`；只在 `-CodexUntil 2026-11-30`（含）以前 | 正確（既有基準） |
| 2 | agy | `agy -p --model gemini-4-argon-high --mode plan --add-dir <拼圖資料夾>`（Google AI Pro） | 正確，1 張約 74 秒 |
| 3 | copilot | `node <tools
ode>
ode_modules\@github\copilot
pm-loader.js -p ... --attachment <拼圖> -s`（PATH 上的 VS Code shim 會失敗，不用） | 正確，約 25 秒；額度小 |
| 4 | claude | `claude -p --model haiku --allowedTools Read`（Claude 額度） | 偏保守：A135 填 null／partial，不會寫錯，但收錄少；約 22 秒 |
| — | opencode | `opencode run -m opencode-go/kimi-k2.7-code -f <拼圖>` | **判錯**（3 車道 through\|through\|right）→ 不准進鏈 |

- 換手：找不到程式 → 不進鏈；沒有輸出或格式檢查不過 → 這批換下一層（壞檔改名 `*.json.<層>.bad` 保留）；輸出含額度／認證錯誤字樣，或同一層本次失敗 2 次 → 本次停用該層。
- log（`lanes_weekly.log`）記「判讀後備鏈：…」「批次 N：使用 X」「換手：批次 N 的 X 失敗（原因）」「各層通過批數」。
- 某批整條鏈都失敗 → 不寫入，下週重判；全部批次都失敗 → 本週跳過（exit 5），不寫判讀紀錄、不 commit。
- 手動只用某一層：`-Engine codex|agy|copilot|claude`；換 agy 模型 `-AgyModel`、copilot 模型 `-CopilotModel`。
- 排程指令不用改（`register-weekly-lanes.ps1` 沒有帶 `-Engine`，預設就是 auto）。
