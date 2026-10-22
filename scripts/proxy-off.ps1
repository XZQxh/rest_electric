# 关闭系统代理，精确还原 proxy-on.ps1 运行前的状态。
#
#   .\scripts\proxy-off.ps1

[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"

$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
$root = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path $root "data\proxy-state.json"

if (Test-Path $stateFile) {
    $saved = Get-Content -Path $stateFile -Raw | ConvertFrom-Json
    Set-ItemProperty -Path $key -Name ProxyEnable -Value ([int]$saved.ProxyEnable) -Type DWord
    Set-ItemProperty -Path $key -Name ProxyServer -Value ([string]$saved.ProxyServer) -Type String
    Set-ItemProperty -Path $key -Name ProxyOverride -Value ([string]$saved.ProxyOverride) -Type String
    Write-Host "已还原原始代理设置（ProxyEnable=$($saved.ProxyEnable)，ProxyServer=$($saved.ProxyServer)）。"
    Remove-Item -Path $stateFile -Force
} else {
    # 没有备份时采取最安全的做法：直接关闭系统代理
    Set-ItemProperty -Path $key -Name ProxyEnable -Value 0 -Type DWord
    Write-Host "未找到备份，已直接关闭系统代理（ProxyEnable=0）。"
    Write-Host "如果你的 Clash 本身需要系统代理，请手动在它里面重新打开。"
}

try {
    $signature = @'
[DllImport("wininet.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern bool InternetSetOption(IntPtr hInternet, int dwOption, IntPtr lpBuffer, int dwBufferLength);
'@
    $type = Add-Type -MemberDefinition $signature -Name WinInet -Namespace ProxyHelper -PassThru -ErrorAction Stop
    [void]$type::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0)
    [void]$type::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0)
} catch {
    Write-Host "未能自动刷新，请重启浏览器。"
}

Write-Host ""
Write-Host "网络已恢复正常直连。"
