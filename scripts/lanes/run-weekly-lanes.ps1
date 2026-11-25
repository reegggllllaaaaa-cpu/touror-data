# 每週 Mapillary 車道 AI 判讀（本機排程用，Windows PowerShell 5.1 相容）
# 流程：kp.py select → kp.py sheets → 外部模型分批判讀 → kp_record.py → kp.py elevcheck → kp.py decide
#       → data/lanes/ 有變動才 commit，pull --rebase 後 push origin main。
# 判讀引擎 -Engine：codex（預設，Codex CLI `codex exec -i <拼圖>`，走 ChatGPT 訂閱額度，不花 Claude／Anthropic API）
#                   claude（備援，claude -p，會用 Claude 額度；只在 codex 不能用時手動指定，建議 -Model haiku）
# 範圍：全台號誌路口（kp.py 的 LANES_REGION，預設 taiwan）；每週最多備妥 -MaxDirections 個方向，已判過的只在有新照片時重判。
# 沒有待判讀的新方向就直接結束，不呼叫模型。
# -DryRun：照常跑 select／sheets，claude 只判讀 1 個方向測試格式（寫到 kp_batch_dryrun_*.json），
#          不寫判讀紀錄、不跑 decide、不 commit／push。
# Mapillary token 只由 mly.py 自己讀檔，本腳本不碰 token；log 另外再過濾一次以防萬一。
[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$NoCommit,   # 照常判讀、寫紀錄、decide，但不 commit／push（測試用）
    [int]$MaxDirections = 300,
    [ValidateSet('codex', 'claude')][string]$Engine = 'codex',
    [string]$CodexModel = '',
    [string]$CodexEffort = 'high',
    [int]$SheetsPerBatch = 6,
    [int]$MaxTurns = 40,
    [string]$Model = 'sonnet',
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
    if ($Engine -eq 'codex') {
        $judgeExe = Find-Exe 'codex.cmd' @((Join-Path $env:USERPROFILE 'tools\node\codex.cmd'))
    } else { $judgeExe = Find-Exe 'claude' @((Join-Path $env:USERPROFILE '.local\bin\claude.exe')) }
    $git = Find-Exe 'git' @('C:\Program Files\Git\cmd\git.exe')
    if (-not $py -or -not $judgeExe -or -not $git) { Write-Log "找不到必要程式 python=$py $Engine=$judgeExe git=$git"; $exitCode = 2; return }
    Write-Log "判讀引擎：$Engine（$judgeExe）"
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

    # 4) 分批呼叫判讀模型（每批是獨立短任務）
    $promptFile = if ($Engine -eq 'codex') { 'judge_prompt_codex.md' } else { 'judge_prompt.md' }
    $template = [IO.File]::ReadAllText((Join-Path $LanesDir $promptFile), [Text.Encoding]::UTF8)
    $batches = New-Object System.Collections.Generic.List[object]
    for ($i = 0; $i -lt $todo.Count; $i += $SheetsPerBatch) {
        $end = [Math]::Min($i + $SheetsPerBatch, $todo.Count) - 1
        $batches.Add(@($todo[$i..$end]))
    }
    $good = New-Object System.Collections.Generic.List[string]
    $bi = 0
    Push-Location $WorkDir
    try {
        foreach ($b in $batches) {
            $bi++
            $items = New-Object System.Collections.Generic.List[string]; $labels = New-Object System.Collections.Generic.List[string]
            foreach ($t in $b) {
                $p = $t.Split("`t")
                $items.Add(('- {0}：{1}' -f (Join-Path $SheetDir $p[0]), ($p[1] -replace ',', '、')))
                foreach ($l in $p[1].Split(',')) { $labels.Add($l) }
            }
            if ($DryRun) { $out = Join-Path $WorkSub "kp_batch_dryrun_$Stamp.json" } else { $out = Join-Path $WorkSub ('kp_batch_weekly_{0}_{1:00}.json' -f $Stamp, $bi) }
            if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Force }
            $prompt = $template.Replace('{{SHEETDIR}}', $SheetDir).Replace('{{ITEMS}}', ($items -join "`n")).Replace('{{OUT}}', $out)
            Write-Log "批次 $bi/$($batches.Count)：$($labels.Count) 個方向（$($labels -join ',')）→ $out"
            if ($Engine -eq 'codex') {
                # 拼圖直接用 -i 附上；唯讀沙箱、不存 session；最後一則回覆（JSON）由 -o 寫到 $out
                $argv = @('exec', '--skip-git-repo-check', '--ephemeral', '-s', 'read-only', '-c', "model_reasoning_effort=$CodexEffort")
                if ($CodexModel) { $argv += @('-m', $CodexModel) }
                foreach ($t in $b) { $argv += @('-i', (Join-Path $SheetDir $t.Split("`t")[0])) }
                $argv += @('-o', $out, '-')
            } else {
                $argv = @('-p', '--model', $Model, '--allowedTools', 'Read,Write', '--max-turns', "$MaxTurns",
                          '--strict-mcp-config', '--no-session-persistence', '--add-dir', $WorkDir)
            }
            $r = Invoke-Logged $judgeExe $argv $prompt
            if ($r.Code -ne 0) { Write-Log "批次 $bi $Engine 結束碼 $($r.Code)" }
            if (-not (Test-Path -LiteralPath $out)) { Write-Log "批次 $bi 沒有產生輸出檔，略過"; continue }
            $c = Invoke-Logged $py @($kb, 'check', $out, ($labels -join ','))
            if ($c.Code -ne 0) { Write-Log "批次 $bi 格式檢查不通過，不寫入（檔案保留供人工檢查）"; continue }
            $good.Add($out)
        }
    } finally { Pop-Location }
    Write-Log "判讀完成：$($good.Count)/$($batches.Count) 批通過格式檢查"

    if ($DryRun) {
        if ($good.Count -eq 0) { $exitCode = 5 }
        Write-Log '乾跑：不寫判讀紀錄、不跑 decide、不 commit／push，結束'
        return
    }
    if ($good.Count -eq 0) { Write-Log '沒有可寫入的批次，結束'; $exitCode = 5; return }

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
