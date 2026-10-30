# 每日选品任务：刷榜单 -> 采商品池 -> 打分 -> 导出 Excel
# 手动执行:  powershell -ExecutionPolicy Bypass -File .\run_daily.ps1
param(
    [int]$Pages = 0,
    [switch]$NoExcel
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# ---- 找 python ----
$py = $null
foreach ($c in @("D:\python\python.exe", "python", "py")) {
    try {
        if ($c -like "*\*") { if (Test-Path $c) { $py = $c; break } }
        else { $cmd = Get-Command $c -ErrorAction SilentlyContinue; if ($cmd) { $py = $cmd.Source; break } }
    } catch {}
}
if (-not $py) { Write-Error "未找到 python，请安装或修改本脚本中的 `$py` 路径"; exit 1 }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
New-Item -ItemType Directory -Force "$root\logs" | Out-Null
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$log = "$root\logs\daily_$stamp.log"

$args = @("cli.py", "daily")
if ($Pages -gt 0) { $args += @("--pages", "$Pages") }
if ($NoExcel)    { $args += "--no-excel" }

Write-Host "== 行业榜单选品 · 每日任务 $stamp ==" -ForegroundColor Cyan
& $py @args 2>&1 | Tee-Object -FilePath $log
$code = $LASTEXITCODE

# 保留最近 30 个日志
Get-ChildItem "$root\logs\daily_*.log" | Sort-Object LastWriteTime -Descending |
    Select-Object -Skip 30 | Remove-Item -Force -ErrorAction SilentlyContinue

Write-Host "日志: $log" -ForegroundColor DarkGray
exit $code
