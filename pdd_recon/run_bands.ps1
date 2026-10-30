# 工序型爱好（有榜单覆盖的那批）· 每 30 分钟受控批次（B+ 节奏）
# 拼豆/滴胶/热缩这类超小众在 PDD 榜单里没有建榜，抓不到，换成有榜的工序型爱好
param(
    [int]$Batch = 5,
    [string]$Target = "100",
    [string]$Keywords = "模型,高达,拼装,手办,钓鱼,路亚,鱼竿,渔具,串珠,穿珠,皮革,皮具,水族,鱼缸,造景,文玩,手串,核桃,毛笔,篆刻,印章,拼图,桌游,卡牌,露营"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$py = $null
foreach ($c in @($env:PYTHON_EXE, "D:\python\python.exe", "python")) {
    if ($c -like "*\*") { if (Test-Path $c) { $py = $c; break } }
    else { $cmd = Get-Command $c -ErrorAction SilentlyContinue; if ($cmd) { $py = $cmd.Source; break } }
}
if (-not $py) { Write-Error "未找到 python"; exit 1 }

$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONUTF8 = "1"
# 让 PowerShell 以 UTF-8 解码子进程输出，否则日志会变乱码
try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }
New-Item -ItemType Directory -Force "$root\logs" | Out-Null

# ---- 单实例锁：上一批没跑完就跳过本轮 ----
$lock = "$root\logs\.bands.lock"
if (Test-Path $lock) {
    $oldPid = (Get-Content $lock -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($oldPid -and (Get-Process -Id $oldPid -ErrorAction SilentlyContinue)) {
        Write-Host "上一批仍在运行（PID $oldPid），跳过本轮" -ForegroundColor Yellow
        exit 0
    }
    Remove-Item $lock -Force -ErrorAction SilentlyContinue
}

# ---- 清理上一批残留的 Chrome（profile 被占会让整批无声退出）----
Get-CimInstance Win32_Process -Filter "Name='chrome.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like "*pdd-profile*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 800
$PID | Set-Content $lock

try {
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $log = "$root\logs\bands_$stamp.log"
    Write-Host "== 价格段榜单批次 $stamp | 本轮 $Batch 页 ==" -ForegroundColor Cyan

    & $py pdd_crawler.py run --sample $Batch --keywords $Keywords --no-wait 2>&1 |
        ForEach-Object { Write-Host $_; $_ } |
        Out-File -FilePath $log -Encoding utf8
    $code = $LASTEXITCODE
    if ($null -eq $code) { $code = 0 }
    if ($code -ne 0) { Write-Host "本轮返回码 $code（多为冷却/配额拦截，非故障）" -ForegroundColor Yellow }

    & $py pdd_crawler.py status 2>&1 | ForEach-Object { Write-Host $_; $_ } |
        Out-File -FilePath $log -Encoding utf8 -Append
    Write-Host "日志: $log" -ForegroundColor DarkGray

    # 保留最近 60 个日志
    Get-ChildItem "$root\logs\bands_*.log" | Sort-Object LastWriteTime -Descending |
        Select-Object -Skip 60 | Remove-Item -Force -ErrorAction SilentlyContinue
    exit 0
}
finally {
    Remove-Item $lock -Force -ErrorAction SilentlyContinue
}
