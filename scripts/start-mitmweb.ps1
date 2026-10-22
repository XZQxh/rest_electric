# 启动 mitmweb，并自动加载 token_watcher 插件。
#
#   .\scripts\start-mitmweb.ps1
#
# 效果：你只要打开一次小程序，新 token 就会自动写入 config\secrets.toml，
# 不需要复制粘贴，也不需要跑 import-flow.py。

[CmdletBinding()]
param(
    [int]$ListenPort = 8080,
    [int]$WebPort = 8081
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$addon = Join-Path $root "scripts\token_watcher.py"
$logFile = Join-Path $root "data\token-watcher.log"
$logDir = Split-Path -Parent $logFile
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-LauncherLog([string]$message) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [launcher] $message"
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

if (-not (Test-Path $addon)) {
    Write-LauncherLog "启动失败：找不到插件 $addon"
    Write-Error "找不到插件：$addon"
    exit 3
}

$mitmweb = Get-Command mitmweb -ErrorAction SilentlyContinue
if (-not $mitmweb) {
    Write-LauncherLog "启动失败：PATH 里找不到 mitmweb"
    Write-Host "没找到 mitmweb 命令。" -ForegroundColor Yellow
    Write-Host "请确认已经安装 mitmproxy，并且它所在的目录在 PATH 里："
    Write-Host "  pip install mitmproxy"
    Write-Host "或者用完整路径启动，例如："
    Write-Host "  & `"C:\Python312\Scripts\mitmweb.exe`" -s `"$addon`""
    exit 3
}

Write-LauncherLog "启动 mitmweb：$($mitmweb.Source)，插件 $addon"

# 顺手检查系统代理，避免“抓了半天什么都没有”
$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
$proxy = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
if ($proxy.ProxyEnable -ne 1 -or $proxy.ProxyServer -notmatch "8080") {
    Write-Host "提醒：系统代理当前没有指向 127.0.0.1:8080。" -ForegroundColor Yellow
    Write-Host "请先在另一个窗口运行：.\scripts\proxy-on.ps1"
    Write-Host ""
}

Write-Host "启动 mitmweb（代理端口 $ListenPort，界面端口 $WebPort）"
Write-Host "插件：$addon"
Write-Host "界面：http://127.0.0.1:$WebPort"
Write-Host "按 Ctrl+C 退出；退出后记得运行 .\scripts\proxy-off.ps1 恢复上网。"
Write-Host ""

& $mitmweb.Source --listen-port $ListenPort --web-port $WebPort -s $addon
exit $LASTEXITCODE
