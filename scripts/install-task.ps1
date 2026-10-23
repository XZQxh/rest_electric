# 注册 / 卸载 Windows 计划任务：默认白天每 2 小时查一次（08:07–22:07）。
#
# 注意：本文件必须保存为 UTF-8 with BOM。Windows PowerShell 5.1 会把无 BOM 的脚本
# 按 GBK 解析，中文会变成乱码并报「字符串缺少终止符」。
#
#   .\scripts\install-task.ps1              # 安装（当前用户，无需管理员）
#   .\scripts\install-task.ps1 -Uninstall   # 卸载
#   .\scripts\install-task.ps1 -Times "08:30","20:30"
#
# 任务以当前用户身份运行、仅在用户登录时执行；无需管理员权限。

[CmdletBinding()]
param(
    [string[]]$Times = @("08:07", "10:07", "12:07", "14:07", "16:07", "18:07", "20:07", "22:07"),
    [int]$JitterMinutes = 8,
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
    # 错过的时间点（比如当时关机）在开机后补跑一次；用电池时也照常跑
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Set-ScheduledTask -TaskName $name -Settings $settings | Out-Null
    Write-Host "已创建计划任务 $name，每天 $time 执行（抖动 0-$JitterMinutes 分钟，错过会补跑）"
}

# 清理旧版本留下的、这次没在 Times 里的任务（避免 09:07/21:07 之类的残留继续跑）
$wanted = $Times | ForEach-Object { "rest_electric_$($_.Replace(':', ''))" }
Get-ScheduledTask -TaskName 'rest_electric_*' -ErrorAction SilentlyContinue |
    Where-Object { $_.TaskName -notin $wanted } |
    ForEach-Object {
        Unregister-ScheduledTask -TaskName $_.TaskName -Confirm:$false
        Write-Host "已清理旧任务 $($_.TaskName)"
    }

Write-Host ""
Write-Host "查看：schtasks /Query /TN rest_electric_0807 /V /FO LIST"
Write-Host "立即测试：schtasks /Run /TN rest_electric_0807"
