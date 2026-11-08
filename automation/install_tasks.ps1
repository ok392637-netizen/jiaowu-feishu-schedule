# 安装/更新两个 Windows 计划任务：课表同步 + 会话续命。
# 用法（在 automation 目录下）：
#   powershell -ExecutionPolicy Bypass -File .\install_tasks.ps1
# 可选参数：-TaskPrefix Jiaowu -SyncHours 6 -KeepAliveMinutes 10 -Python "C:\Python312\python.exe"
param(
    [string]$Root = (Split-Path -Parent $PSCommandPath),
    [string]$Python = "",
    [string]$TaskPrefix = "Jiaowu",
    [int]$SyncHours = 6,
    [int]$KeepAliveMinutes = 10
)

$ErrorActionPreference = "Stop"
if (-not $Python) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "找不到 python，请用 -Python 指定解释器路径" }
    $Python = $cmd.Source
}
if (-not (Test-Path $Python)) { throw "python 不存在：$Python" }
$Pythonw = Join-Path (Split-Path $Python) "pythonw.exe"
if (-not (Test-Path $Pythonw)) { $Pythonw = $Python }
if (-not (Test-Path (Join-Path $Root "sync_schedule.py"))) { throw "Root 不是 automation 目录：$Root" }
if (-not (Test-Path (Join-Path (Split-Path $Root) "skill\scripts\jw_normalize.py"))) {
    Write-Warning "没找到 ../skill/scripts，请把 skill 目录放在仓库同级；或用环境变量 JIAOWU_SKILL_SCRIPTS 指定"
}

function Write-Launcher([string]$name, [string]$script) {
    $vbs = @"
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "$Root"
sh.Run """$Pythonw"" ""$Root\$script""", 0, False
"@
    $path = Join-Path $Root $name
    [IO.File]::WriteAllText($path, $vbs, (New-Object System.Text.UTF8Encoding($false)))
    return $path
}

$syncVbs = Write-Launcher "run_sync.vbs" "sync_schedule.py"
$keepVbs = Write-Launcher "run_keepalive.vbs" "keepalive.py"

$loginCmd = @"
@echo off
rem 教务登录过期时双击本文件：用统一认证登录并勾选「7 天内无需再次登录」，登录后关掉窗口。
start "" "$Pythonw" "$Root\relogin_helper.py"
"@
[IO.File]::WriteAllText((Join-Path $Root "重新登录课表账号.cmd"), $loginCmd, (New-Object System.Text.UTF8Encoding($false)))

$settings = 'StartWhenAvailable=$true; DisallowStartIfOnBatteries=$false; StopIfGoingOnBatteries=$false; ExecutionTimeLimit="PT10M"'

function Ensure-Task([string]$name, [string]$target, [string]$scheduleArgs) {
    schtasks /create /tn $name /tr """wscript.exe \""$target\""""" /f @scheduleArgs 2>&1 | Out-Null
    schtasks /query /tn $name | Out-Null
    $global:LASTEXITCODE = 0
}

$syncName = "${TaskPrefix}ScheduleSync"
$keepName = "${TaskPrefix}KeepAlive"

schtasks /create /tn $syncName /tr "`"wscript.exe `"`"$syncVbs`"`"" /sc hourly /mo $SyncHours /st 05:00 /f | Out-Null
schtasks /create /tn $keepName /tr "`"wscript.exe `"`"$keepVbs`"`"" /sc minute /mo $KeepAliveMinutes /st 00:00 /f | Out-Null

foreach ($n in @($syncName, $keepName)) {
    $t = Get-ScheduledTask -TaskName $n -ErrorAction Stop
    $s = $t.Settings
    $s.StartWhenAvailable = $true
    $s.DisallowStartIfOnBatteries = $false
    $s.StopIfGoingOnBatteries = $false
    $s.ExecutionTimeLimit = "PT10M"
    $s.MultipleInstances = "IgnoreNew"
    Set-ScheduledTask -TaskName $n -Settings $s | Out-Null
    $info = Get-ScheduledTaskInfo -TaskName $n
    Write-Host ("OK  {0}  next={1}" -f $n, $info.NextRunTime)
}

Write-Host ""
Write-Host "已安装。下一步："
Write-Host "  1) 复制 config.example.json -> config.json 并填 calendarId / notifyUserId / stem / d1 / d2"
Write-Host "  2) 双击 重新登录课表账号.cmd 登录一次（统一认证 + 7 天免登录）"
Write-Host "  3) 运行 $Python $Root\sync_schedule.py --dry-run 检查差异"
