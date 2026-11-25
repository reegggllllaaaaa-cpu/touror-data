# 每週 Mapillary 車道 AI 判讀（本機排程用，Windows PowerShell 5.1 相容）
# 流程：kp.py select → kp.py sheets → 外部模型分批判讀 → kp_record.py → kp.py elevcheck → kp.py decide
#       → data/lanes/ 有變動才 commit，pull --rebase 後 push origin main。
# 判讀引擎 -Engine：auto（預設）＝後備模型鏈，依序 codex → agy → copilot → claude，一層失敗自動換下一層：
#   codex   Codex CLI `codex exec -i <拼圖>`（ChatGPT Plus；只在 -CodexUntil 當天以前使用，訂閱 2026-11-30 到期）
#   agy     Google AI Pro（agy -p，預設 gemini-4-argon-high，plan 模式用讀檔工具看拼圖）
#   copilot GitHub Copilot CLI（--attachment 附拼圖；額度小）
#   claude  claude -p --model haiku（最後一層，會用 Claude 額度；實測偏保守、多填 null，不會寫錯資料）
#   2026-11-25 實測（S034 A135 標準答案 2 車道 through|right）：agy／copilot 正確；opencode kimi-k2.7 判成 3 車道 → 不進鏈。
#   換手條件：找不到程式、沒有輸出、輸出格式檢查不過；輸出含額度／認證錯誤或同一層失敗 2 次 → 本次整個停用該層。
#   每批用哪一層、換手原因都寫進 log；整條鏈都失敗的批次不寫入（下週重判），全部失敗時本週跳過。
#   -Engine codex／agy／copilot／claude：只用該層（手動測試用）。
# 範圍：全台號誌路口（kp.py 的 LANES_REGION，預設 taiwan）；每週最多備妥 -MaxDirections 個方向，已判過的只在有新照片時重判。
# 沒有待判讀的新方向就直接結束，不呼叫模型。
# -DryRun：照常跑 select／sheets，後備鏈只判讀 1 個方向測試格式（寫到 kp_batch_dryrun_*.json），
#          不寫判讀紀錄、不跑 decide、不 commit／push。
# Mapillary token 只由 mly.py 自己讀檔，本腳本不碰 token；log 另外再過濾一次以防萬一。
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$NoCommit,   # 照常判讀、寫紀錄、decide，但不 commit／push（測試用）
    [int]$MaxDirections = 300,
    [ValidateSet('auto', 'codex', 'agy', 'copilot', 'claude')][string]$Engine = 'auto',
    [string]$CodexUntil = '2026-11-30',   # 這天以後 auto 鏈不再嘗試 codex（ChatGPT 訂閱到期）
    [string]$CodexModel = '',
    [string]$CodexEffort = 'high',        # 不要用 none／low：實測會把平面道路誤判成高架
    [string]$AgyModel = 'gemini-4-argon-high',
    [string]$CopilotModel = '',
    [int]$SheetsPerBatch = 6,
    [int]$MaxTurns = 40,
    [string]$Model = 'haiku',             # claude 層的模型
    [string]$LogPath = ''
)

$ErrorActionPreference = 'Continue'
$utf8 = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUTF8 = '1'

$LanesDir = $PSScriptRoot
$Repo = (Resolve-Path (Join-Path $LanesDir '..\..')).Path
$Root = Split-Path $Repo -Parent
if ($env:LANES_WORKDIR) { $WorkDir = $env:LANES_WORKDIR } else { $WorkDir = Join-Path $Root '資料\車道\mapillary\視覺判讀' }
$SheetDir = Join-Path $WorkDir '照片\kp'
$WorkSub = Join-Path $WorkDir 'work'
if (-not $LogPath) { $LogPath = Join-Path $env:USERPROFILE '資料中心\工作日誌\lanes_weekly.log' }
$Stamp = Get-Date -Format 'yyyyMMdd'

function Hide-Secret([string]$s) {
    if ($null -eq $s) { return '' }
    $s = [regex]::Replace($s, 'MLY\|[0-9]+\|[0-9A-Za-z]+', 'MLY|***')
    $s = [regex]::Replace($s, '(?i)(access_token|token|authorization)(["'':= ]+)(OAuth |Bearer )?[0-9A-Za-z|._-]{12,}', '$1$2***')
    return $s
}

function Write-Log([string]$msg) {
    $line = '{0} {1}{2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $(if ($DryRun) { '[乾跑] ' } else { '' }), (Hide-Secret $msg)
    # 用共用寫入模式開檔（log 被別的程式開著看時 Add-Content 會失敗）
    for ($k = 0; $k -lt 5; $k++) {
        try {
            $fs = [IO.File]::Open($LogPath, [IO.FileMode]::Append, [IO.FileAccess]::Write, [IO.FileShare]::ReadWrite)
            try { $b = $utf8.GetBytes($line + "`r`n"); $fs.Write($b, 0, $b.Length) } finally { $fs.Dispose() }
            break
        } catch { Start-Sleep -Milliseconds 200 }
    }
    Write-Host $line
}

function Find-Exe([string]$name, [string[]]$fallback) {
    $c = Get-Command $name -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($c) { return $c.Source }
    foreach ($f in $fallback) { if (Test-Path -LiteralPath $f) { return $f } }
    return $null
}

# 執行判讀引擎：stdout／stderr 分開收（stdout 才是判讀 JSON），都寫 log；回傳 @{Code; Out; Err}
function Invoke-Capture([string]$exe, [string[]]$argv, [string]$stdin = $null) {
    $errF = [IO.Path]::GetTempFileName()
    if ($stdin) { $o = $stdin | & $exe @argv 2>$errF } else { $o = & $exe @argv 2>$errF }
    $code = $LASTEXITCODE
    $so = @($o | ForEach-Object { "$_" })
    $se = @(Get-Content -LiteralPath $errF -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { "$_" })
    Remove-Item -LiteralPath $errF -Force -ErrorAction SilentlyContinue
    foreach ($t in $so) { Write-Log ('  | ' + $t) }
    foreach ($t in $se) { if ($t.Trim()) { Write-Log ('  ! ' + $t) } }
    return @{ Code = $code; Out = ($so -join "`n"); Err = ($se -join "`n") }
}

# PowerShell 5.1 傳參數給原生程式不會跳脫雙引號：手動補 \"（agy／node 都照 Windows 標準規則解析）
function Protect-Arg([string]$s) { return $s.Replace('"', '\"') }

# 執行外部程式，輸出逐行寫 log；回傳 @{Code; Lines}
function Invoke-Logged([string]$exe, [string[]]$argv, [string]$stdin = $null) {
    $lines = New-Object System.Collections.Generic.List[string]
    if ($null -ne $stdin) { $out = $stdin | & $exe @argv 2>&1 } else { $out = & $exe @argv 2>&1 }
    $code = $LASTEXITCODE
    foreach ($o in $out) { $t = "$o"; $lines.Add($t); Write-Log ('  | ' + $t) }
    return @{ Code = $code; Lines = $lines }
}

$logDir = Split-Path $LogPath -Parent
if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Force -Path $logDir | Out-Null }

# 同時只跑一份
$lock = Join-Path $WorkSub 'weekly.lock'
if (Test-Path -LiteralPath $lock) {
    $age = (Get-Date) - (Get-Item -LiteralPath $lock).LastWriteTime
    if ($age.TotalHours -lt 4) { Write-Log "已有一份在執行（$lock），結束"; exit 0 }
    Remove-Item -LiteralPath $lock -Force
}
New-Item -ItemType File -Force -Path $lock | Out-Null

$exitCode = 0
try {
    Write-Log "===== 開始 每週車道判讀（repo=$Repo，上限 $MaxDirections 個方向）====="
    $py = Find-Exe 'python' @()
    $git = Find-Exe 'git' @('C:\Program Files\Git\cmd\git.exe')
    if (-not $py -or -not $git) { Write-Log "找不到必要程式 python=$py git=$git"; $exitCode = 2; return }
    # 後備模型鏈：找得到程式的層才進鏈
    if ($Engine -eq 'auto') { $want = @('codex', 'agy', 'copilot', 'claude') } else { $want = @($Engine) }
    $exes = @{}; $chain = New-Object System.Collections.Generic.List[string]
    $loader = Join-Path $env:USERPROFILE 'tools\node\node_modules\@github\copilot\npm-loader.js'
    foreach ($e in $want) {
        if ($e -eq 'codex' -and $Engine -eq 'auto' -and (Get-Date).Date -gt [datetime]::ParseExact($CodexUntil, 'yyyy-MM-dd', $null)) {
            Write-Log "鏈：略過 codex（已過 -CodexUntil $CodexUntil，ChatGPT 訂閱到期）"; continue
        }
        $x = $null
        switch ($e) {
            'codex'   { $x = Find-Exe 'codex.cmd' @((Join-Path $env:USERPROFILE 'tools\node\codex.cmd')) }
            'agy'     { $x = Find-Exe 'agy.exe' @((Join-Path $env:LOCALAPPDATA 'agy\bin\agy.exe')) }
            'copilot' {
                # PATH 上的 copilot 是 VS Code shim，會因呼叫 .cmd 失敗（UnsupportedWindowsCommandValue）；直接用 node 跑 npm 套件
                $node = Find-Exe 'node.exe' @((Join-Path $env:USERPROFILE 'tools\node\node.exe'))
                if ($node -and (Test-Path -LiteralPath $loader)) { $x = $node }
            }
            'claude'  { $x = Find-Exe 'claude' @((Join-Path $env:USERPROFILE '.local\bin\claude.exe')) }
        }
        if ($x) { $exes[$e] = $x; $chain.Add($e) } else { Write-Log "鏈：略過 $e（找不到程式）" }
    }
    if ($chain.Count -eq 0) { Write-Log '後備模型鏈沒有任何可用引擎，本週跳過（不寫入任何判讀）'; $exitCode = 2; return }
    Write-Log "判讀後備鏈：$($chain -join ' → ')"
    $kp = Join-Path $LanesDir 'kp.py'
    $kb = Join-Path $LanesDir 'kp_batch.py'

    # 1) select
    Write-Log "步驟 select $MaxDirections"
    $r = Invoke-Logged $py @($kp, 'select', "$MaxDirections")
    if ($r.Code -ne 0) { Write-Log "select 失敗（exit $($r.Code)），結束"; $exitCode = 3; return }

    # 2) sheets
    Write-Log '步驟 sheets'
    $r = Invoke-Logged $py @($kp, 'sheets')
    if ($r.Code -ne 0) { Write-Log "sheets 失敗（exit $($r.Code)），結束"; $exitCode = 3; return }

    # 3) 待判讀清單
    $r = & $py $kb 'todo' "$MaxDirections" 2>&1
    $todo = @($r | ForEach-Object { "$_" } | Where-Object { $_ -match '^S\d+\.jpg\t' })
    $nDir = 0; foreach ($t in $todo) { $nDir += ($t.Split("`t")[1].Split(',')).Count }
    Write-Log "待判讀：$nDir 個方向，$($todo.Count) 張拼圖"

    if ($DryRun) {
        if ($todo.Count -gt 0) { $first = $todo[0] } else {
            $first = (& $py $kb 'sample' 2>&1 | ForEach-Object { "$_" } | Where-Object { $_ -match '^S\d+\.jpg\t' } | Select-Object -First 1)
            Write-Log '乾跑：沒有新方向，改用最後一個已有拼圖的方向測試格式（結果不寫入判讀紀錄）'
        }
        if (-not $first) { Write-Log '乾跑：找不到任何拼圖可測，結束'; $exitCode = 4; return }
        $p = $first.Split("`t"); $todo = @("$($p[0])`t$($p[1].Split(',')[0])")
    } elseif ($todo.Count -eq 0) {
        Write-Log '沒有新照片／待判讀方向，不呼叫模型，結束'
        return
    }

    # 4) 分批呼叫判讀模型（每批是獨立短任務；依鏈順序嘗試，第一個通過格式檢查的層採用）
    $template = [IO.File]::ReadAllText((Join-Path $LanesDir 'judge_prompt_codex.md'), [Text.Encoding]::UTF8)
    $batches = New-Object System.Collections.Generic.List[object]
    for ($i = 0; $i -lt $todo.Count; $i += $SheetsPerBatch) {
        $end = [Math]::Min($i + $SheetsPerBatch, $todo.Count) - 1
        $batches.Add(@($todo[$i..$end]))
    }
    $fatalRe = '(?i)quota|rate.?limit|usage.?limit|too many requests|\b429\b|\b401\b|\b403\b|unauthori[sz]ed|forbidden|authenticat|not logged in|log ?in|sign ?in|subscription|billing|payment|credit|insufficient|expired|exceeded|resource.?exhausted'
    $dead = @{}; $fails = @{}; $used = @{}
    $good = New-Object System.Collections.Generic.List[string]
    $bi = 0
    Push-Location $WorkDir
    try {
        foreach ($b in $batches) {
            $bi++
            $items = New-Object System.Collections.Generic.List[string]; $labels = New-Object System.Collections.Generic.List[string]
            $imgs = New-Object System.Collections.Generic.List[string]
            foreach ($t in $b) {
                $p = $t.Split("`t")
                $items.Add(('- {0}：{1}' -f (Join-Path $SheetDir $p[0]), ($p[1] -replace ',', '、')))
                $imgs.Add((Join-Path $SheetDir $p[0]))
                foreach ($l in $p[1].Split(',')) { $labels.Add($l) }
            }
            if ($DryRun) { $out = Join-Path $WorkSub "kp_batch_dryrun_$Stamp.json" } else { $out = Join-Path $WorkSub ('kp_batch_weekly_{0}_{1:00}.json' -f $Stamp, $bi) }
            $prompt = $template.Replace('{{SHEETDIR}}', $SheetDir).Replace('{{ITEMS}}', ($items -join "`n")).Replace('{{OUT}}', $out)
            Write-Log "批次 $bi/$($batches.Count)：$($labels.Count) 個方向（$($labels -join ',')）→ $out"
            $ok = $false
            foreach ($eng in $chain) {
                if ($dead[$eng]) { continue }
                if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Force }
                $exe = $exes[$eng]; $stdin = $null; $toFile = $true
                switch ($eng) {
                    'codex' {
                        # 拼圖用 -i 附上；唯讀沙箱、不存 session；最後一則回覆（JSON）由 -o 寫到 $out
                        $argv = @('exec', '--skip-git-repo-check', '--ephemeral', '-s', 'read-only', '-c', "model_reasoning_effort=$CodexEffort")
                        if ($CodexModel) { $argv += @('-m', $CodexModel) }
                        foreach ($im in $imgs) { $argv += @('-i', $im) }
                        $argv += @('-o', $out, '-'); $stdin = $prompt; $toFile = $false
                    }
                    'agy' {
                        # plan 模式＋--add-dir 拼圖資料夾：模型用讀檔工具看圖，回覆只有 JSON
                        $argv = @('-p', (Protect-Arg $prompt), '--model', $AgyModel, '--add-dir', $SheetDir, '--mode', 'plan', '--print-timeout', '900s')
                    }
                    'copilot' {
                        $argv = @($loader, '-p', (Protect-Arg $prompt), '-s', '--no-custom-instructions', '--no-color')
                        if ($CopilotModel) { $argv += @('--model', $CopilotModel) }
                        foreach ($im in $imgs) { $argv += @('--attachment', $im) }
                    }
                    'claude' {
                        $argv = @('-p', '--model', $Model, '--allowedTools', 'Read', '--max-turns', "$MaxTurns",
                                  '--strict-mcp-config', '--no-session-persistence', '--add-dir', $WorkDir); $stdin = $prompt
                    }
                }
                Write-Log "批次 $bi：使用 $eng"
                $r = Invoke-Capture $exe $argv $stdin
                if ($toFile -and $r.Out.Trim()) { [IO.File]::WriteAllText($out, $r.Out, $utf8) }
                $reason = $null
                if (-not (Test-Path -LiteralPath $out)) { $reason = "沒有輸出（結束碼 $($r.Code)）" }
                else {
                    $c = Invoke-Logged $py @($kb, 'check', $out, ($labels -join ','))
                    if ($c.Code -ne 0) {
                        $reason = "格式檢查不過（結束碼 $($r.Code)）"
                        Move-Item -LiteralPath $out -Destination "$out.$eng.bad" -Force   # 保留供人工檢查
                    }
                }
                if (-not $reason) { $good.Add($out); $used[$eng] = 1 + [int]$used[$eng]; $ok = $true; Write-Log "批次 $bi：$eng 通過"; break }
                $fails[$eng] = 1 + [int]$fails[$eng]
                if ((($r.Out + "`n" + $r.Err) -match $fatalRe) -or $fails[$eng] -ge 2) {
                    $dead[$eng] = $true; $reason += '；疑似額度／認證問題或已失敗 2 次，本次停用此層'
                }
                Write-Log "換手：批次 $bi 的 $eng 失敗（$reason），換下一層"
            }
            if (-not $ok) { Write-Log "批次 $bi：整條鏈都失敗，這批不寫入（下週重判）" }
        }
    } finally { Pop-Location }
    $usedTxt = ($used.Keys | ForEach-Object { "$_=$($used[$_])" }) -join ', '
    if ($usedTxt) { Write-Log "各層通過批數：$usedTxt" }
    Write-Log "判讀完成：$($good.Count)/$($batches.Count) 批通過格式檢查"

    if ($DryRun) {
        if ($good.Count -eq 0) { $exitCode = 5 }
        Write-Log '乾跑：不寫判讀紀錄、不跑 decide、不 commit／push，結束'
        return
    }
    if ($good.Count -eq 0) { Write-Log '整條後備鏈都沒有產出可用判讀：本週跳過，不寫入任何判讀紀錄'; $exitCode = 5; return }

    # 5) 寫入判讀 → elevcheck → decide
    foreach ($g in $good) {
        $r = Invoke-Logged $py @((Join-Path $LanesDir 'kp_record.py'), $g)
        if ($r.Code -ne 0) { Write-Log "kp_record 失敗：$g"; $exitCode = 6; return }
    }
    foreach ($step in @('elevcheck', 'decide')) {
        Write-Log "步驟 $step"
        $r = Invoke-Logged $py @($kp, $step)
        if ($r.Code -ne 0) { Write-Log "$step 失敗（exit $($r.Code)），結束"; $exitCode = 6; return }
    }

    if ($NoCommit) { Write-Log '-NoCommit：不 commit／push，結束'; return }
    # 6) data/lanes/ 有變動才 commit＋push
    $st = & $git -C $Repo status --porcelain -- 'data/lanes' 2>&1 | ForEach-Object { "$_" }
    if (-not ($st | Where-Object { $_.Trim() })) { Write-Log 'data/lanes/ 沒有變動，不 commit'; return }
    Write-Log "data/lanes/ 有變動：$($st -join '; ')"
    $r = Invoke-Logged $git @('-C', $Repo, 'add', '--', 'data/lanes')
    $msgFile = Join-Path $env:TEMP "lanes_weekly_commit_$Stamp.txt"
    $msg = "車道：每週 Mapillary AI 判讀更新（$(Get-Date -Format 'yyyy-MM-dd')，判讀 $nDir 個方向）`n`nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`n"
    [IO.File]::WriteAllText($msgFile, $msg, $utf8)
    $r = Invoke-Logged $git @('-C', $Repo, 'commit', '-F', $msgFile, '--', 'data/lanes')
    Remove-Item -LiteralPath $msgFile -Force -ErrorAction SilentlyContinue
    if ($r.Code -ne 0) { Write-Log 'commit 失敗，結束'; $exitCode = 7; return }
    $r = Invoke-Logged $git @('-C', $Repo, 'pull', '--rebase', '--autostash', 'origin', 'main')
    if ($r.Code -ne 0) {
        Write-Log 'pull --rebase 失敗，放棄 rebase 並結束（commit 留在本機，需人工處理）'
        & $git -C $Repo rebase --abort 2>&1 | Out-Null
        $exitCode = 8; return
    }
    $r = Invoke-Logged $git @('-C', $Repo, 'push', 'origin', 'main')
    if ($r.Code -ne 0) { Write-Log 'push 失敗，結束（commit 留在本機）'; $exitCode = 8; return }
    Write-Log 'commit＋push 完成'
}
finally {
    Remove-Item -LiteralPath $lock -Force -ErrorAction SilentlyContinue
    Write-Log "===== 結束（exit $exitCode）====="
    exit $exitCode   # 放在 finally：try 內的 return 也會走到這裡
}
