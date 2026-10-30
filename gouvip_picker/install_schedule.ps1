# 注册/卸载 Windows 计划任务（每天定时跑选品）
#   .\install_schedule.ps1                  # 每天 08:30 跑
#   .\install_schedule.ps1 -Time 09:15
#   .\install_schedule.ps1 -Remove
param(
    [string]$Time = "08:30",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$taskName = "GouvipPicker-Daily"
$ps = (Get-Command powershell.exe).Source
$script = Join-Path $root "run_daily.ps1"

if ($Remove) {
    schtasks /Delete /TN $taskName /F 2>$null
    Write-Host "已删除计划任务 $taskName" -ForegroundColor Yellow
    exit 0
}

if (-not (Test-Path $script)) { Write-Error "找不到 $script"; exit 1 }

$action = "$ps -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
schtasks /Create /TN $taskName /TR $action /SC DAILY /ST $Time /RL LIMITED /F | Out-Null

Write-Host "✓ 已创建计划任务 $taskName，每天 $Time 执行" -ForegroundColor Green
Write-Host "  立即试跑: schtasks /Run /TN $taskName"
Write-Host "  查看结果: 任务计划程序，或看 $root\logs\"
Write-Host "  取消:     .\install_schedule.ps1 -Remove"
