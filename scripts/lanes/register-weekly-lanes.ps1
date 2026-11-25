# 註冊 Windows 工作排程「Touror 車道判讀」：每週日 02:00 執行 run-weekly-lanes.ps1
# （在週一 03:00 weekly-roadnet 之前）。只在使用者登入時執行（claude -p 需要使用者的登入狀態）。
# 用法（使用者自己執行）：powershell -NoProfile -ExecutionPolicy Bypass -File <此檔>
# 取消：powershell -NoProfile -Command "Unregister-ScheduledTask -TaskName 'Touror 車道判讀' -Confirm:$false"
param([string]$TaskName = 'Touror 車道判讀')

$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'run-weekly-lanes.ps1'
if (-not (Test-Path -LiteralPath $script)) { throw "找不到 $script" }

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ('-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $script) `
    -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At '02:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
    -Priority 7 -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description '每週 Mapillary 車道 AI 判讀（touror-data scripts/lanes），log：資料中心\工作日誌\lanes_weekly.log' -Force | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
$i = $t | Get-ScheduledTaskInfo
Write-Host ("已註冊：{0}，下次執行 {1}" -f $t.TaskName, $i.NextRunTime)
