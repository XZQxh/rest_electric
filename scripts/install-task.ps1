# 注册 / 卸载 Windows 计划任务：每天 09:07 与 21:07 各查一次。
#
#   .\scripts\install-task.ps1              # 安装（当前用户，无需管理员）
#   .\scripts\install-task.ps1 -Uninstall   # 卸载
#   .\scripts\install-task.ps1 -Times "08:30","20:30"
#
# 任务以当前用户身份运行、仅在用户登录时执行；无需管理员权限。

[CmdletBinding()]
param(
    [string[]]$Times = @("09:07", "21:07"),
    [int]$JitterMinutes = 5,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$runner = Join-Path $root "scripts\run-once.ps1"

if ($Uninstall) {
    foreach ($time in $Times) {
        $name = "rest_electric_$($time.Replace(':', ''))"
        & schtasks.exe /Delete /TN $name /F 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "已删除计划任务 $name"
        } else {
            Write-Host "计划任务 $name 不存在或已删除"
        }
    }
    exit 0
}

if (-not (Test-Path $runner)) {
    Write-Error "找不到 $runner"
    exit 3
}

foreach ($time in $Times) {
    $name = "rest_electric_$($time.Replace(':', ''))"
    $command = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$runner`" -JitterMinutes $JitterMinutes"
    & schtasks.exe /Create /TN $name /TR $command /SC DAILY /ST $time /F | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Error "创建计划任务 $name 失败（退出码 $LASTEXITCODE）"
        exit 2
    }
    Write-Host "已创建计划任务 $name，每天 $time 执行（抖动 0-$JitterMinutes 分钟）"
}

Write-Host ""
Write-Host "查看：schtasks /Query /TN rest_electric_0907 /V /FO LIST"
Write-Host "立即测试：schtasks /Run /TN rest_electric_0907"
