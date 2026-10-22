# 一次性完成「刷新 token」的全部步骤：
#   开代理 → 启动带插件的 mitmweb → 等你打开小程序 → 收到新 token → 自动关代理、关 mitmweb
#
#   .\scripts\refresh-token.ps1
#
# 运行后你只需要做一件事：用电脑微信打开「上海海洋大学一卡通」小程序，进电费查询。
# 脚本最多等 5 分钟（可调），超时会自动恢复网络并退出。

[CmdletBinding()]
param(
    [int]$TimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$addon = Join-Path $root "scripts\token_watcher.py"
$logFile = Join-Path $root "data\token-watcher.log"

function Test-PortInUse([int]$port) {
    return [bool](netstat -ano | Select-String 'LISTENING' | Select-String ":$port\s")
}

function Stop-Mitmweb {
    $conns = netstat -ano | Select-String 'LISTENING' | Select-String ':8080\s'
    $ids = @($conns | ForEach-Object { ($_.Line -split '\s+')[-1] } | Sort-Object -Unique)
    foreach ($procId in $ids) {
        Stop-Process -Id $procId -Force -ErrorAction SilentlyContinue
    }
}

# ---- 前置检查 ----

if (Test-PortInUse 8080) {
    Write-Host "8080 端口已被占用，说明已经有一个 mitmweb 在跑。" -ForegroundColor Yellow
    Write-Host "请先关闭那个窗口（或运行 .\scripts\proxy-off.ps1 后再重试）。"
    exit 3
}

$mitmweb = Get-Command mitmweb -ErrorAction SilentlyContinue
if (-not $mitmweb) {
    Write-Host "找不到 mitmweb 命令，请确认 mitmproxy 已安装且在 PATH 里。" -ForegroundColor Yellow
    exit 3
}

if (-not (Test-Path $logFile)) {
    New-Item -ItemType Directory -Path (Split-Path -Parent $logFile) -Force | Out-Null
    New-Item -ItemType File -Path $logFile -Force | Out-Null
}

$beforeLines = @(Get-Content $logFile -ErrorAction SilentlyContinue).Count

# ---- 开代理 ----

Write-Host "1/3 打开系统代理..."
& (Join-Path $PSScriptRoot "proxy-on.ps1") | Out-Null

$started = $null
$success = $false
try {
    Write-Host "2/3 启动 mitmweb（带 token 自动保存插件）..."
    $started = Start-Process -FilePath $mitmweb.Source `
        -ArgumentList "--listen-port", "8080", "--web-port", "8081", "-s", "`"$addon`"", "--no-web-open-browser" `
        -WindowStyle Minimized -PassThru

    $ready = $false
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        if (Test-PortInUse 8080) { $ready = $true; break }
    }
    if (-not $ready) {
        Write-Host "mitmweb 没能启动成功，请检查它是否被安全软件拦截。" -ForegroundColor Red
        exit 2
    }

    Write-Host ""
    Write-Host "3/3 现在请用【电脑微信】打开「上海海洋大学一卡通」小程序，进入电费查询。" -ForegroundColor Cyan
    Write-Host "    脚本最多等待 $TimeoutSeconds 秒，收到新 token 会自动收尾。" -ForegroundColor Cyan
    Write-Host ""

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 2
        $lines = @(Get-Content $logFile -ErrorAction SilentlyContinue)
        $new = @($lines | Select-Object -Skip $beforeLines)
        $hit = $new | Where-Object { $_ -match 'token 已更新' }
        if ($hit) {
            $success = $true
            Write-Host "收到新 token！" -ForegroundColor Green
            $new | Where-Object { $_ -match 'token 已更新|有效期到' } | ForEach-Object { Write-Host "  $_" }
            break
        }
    }

    if (-not $success) {
        Write-Host ""
        Write-Host "等待超时，没有收到新 token。" -ForegroundColor Yellow
        Write-Host "可能原因：小程序里的 token 还没过期（不会主动重新登录），或没有触发查询。"
        Write-Host "可以打开小程序后多点一次查询，或等 token 过期后再试。"
    }
} finally {
    Write-Host ""
    Write-Host "收尾：恢复系统代理、关闭 mitmweb..."
    try { & (Join-Path $PSScriptRoot "proxy-off.ps1") | Out-Null } catch {
        Write-Host "自动恢复代理失败，请手动运行 .\scripts\proxy-off.ps1" -ForegroundColor Red
    }
    Stop-Mitmweb
    Write-Host "网络已恢复。"
}

if ($success) {
    Write-Host ""
    Write-Host "接下来可以验证："
    Write-Host "  .\scripts\run-once.ps1 -DryRun"
    exit 0
}
exit 1
