# 手动执行一次查询。计划任务也调用本脚本。
#
#   .\scripts\run-once.ps1                 # 正常查询 + 写库 + 告警
#   .\scripts\run-once.ps1 -DryRun         # 不写库、不推送
#   .\scripts\run-once.ps1 -NoNotify       # 写库但不推送
#   .\scripts\run-once.ps1 -JitterMinutes 5  # 启动前随机等待 0-5 分钟

[CmdletBinding()]
param(
    [switch]$DryRun,
    [switch]$NoNotify,
    [string]$Config = "",
    [string]$Secrets = "",
    [int]$JitterMinutes = 0,
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$env:PYTHONPATH = Join-Path $root "src"

if (-not $PythonExe) {
    $candidate = Get-Command python -ErrorAction SilentlyContinue
    if (-not $candidate) {
        $candidate = Get-Command python3 -ErrorAction SilentlyContinue
    }
    if (-not $candidate) {
        Write-Error "找不到 python。请安装 Python 3.11+ 或用 -PythonExe 指定路径。"
        exit 3
    }
    $PythonExe = $candidate.Source
}

if ($JitterMinutes -gt 0) {
    $jitter = Get-Random -Minimum 0 -Maximum ($JitterMinutes * 60)
    Write-Host "随机抖动 $jitter 秒后开始……"
    Start-Sleep -Seconds $jitter
}

$cliArgs = @("-m", "rest_electric", "once")
if ($DryRun) { $cliArgs += "--dry-run" }
if ($NoNotify) { $cliArgs += "--no-notify" }
if ($Config) { $cliArgs += @("--config", $Config) }
if ($Secrets) { $cliArgs += @("--secrets", $Secrets) }

Push-Location $root
try {
    & $PythonExe @cliArgs
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

exit $code
