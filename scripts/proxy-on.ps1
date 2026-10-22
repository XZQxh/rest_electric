# 打开系统代理，把浏览器/微信的流量指向 mitmproxy（127.0.0.1:8080）。
#
#   .\scripts\proxy-on.ps1
#
# 首次运行会把原始代理设置备份到 data\proxy-state.json，
# 之后用 .\scripts\proxy-off.ps1 精确还原（包括你原来那个 7892 代理）。

[CmdletBinding()]
param(
    [string]$ProxyServer = "127.0.0.1:8080"
)

$ErrorActionPreference = "Stop"

$key = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings"
$root = Split-Path -Parent $PSScriptRoot
$stateFile = Join-Path $root "data\proxy-state.json"

if (-not (Test-Path $key)) {
    Write-Error "找不到 Internet Settings 注册表项，系统环境异常。"
    exit 3
}

$current = Get-ItemProperty -Path $key
$stateDir = Split-Path -Parent $stateFile
if (-not (Test-Path $stateDir)) {
    New-Item -ItemType Directory -Path $stateDir -Force | Out-Null
}

# 只在"从关闭状态切到代理"时备份，避免重复运行时把 8080 自己存成"原始值"
if (-not (Test-Path $stateFile)) {
    $backup = [pscustomobject]@{
        ProxyEnable   = [int]$current.ProxyEnable
        ProxyServer   = [string]$current.ProxyServer
        ProxyOverride = [string]$current.ProxyOverride
        savedAt       = (Get-Date).ToString("o")
    }
    $backup | ConvertTo-Json | Set-Content -Path $stateFile -Encoding UTF8
    Write-Host "已备份原始代理设置：$stateFile"
} else {
    Write-Host "已存在备份（$stateFile），本次不覆盖。"
}

Set-ItemProperty -Path $key -Name ProxyEnable -Value 1 -Type DWord
Set-ItemProperty -Path $key -Name ProxyServer -Value $ProxyServer -Type String
# 本机地址必须直连，否则 mitmweb 的 8081 界面自己都会被代理绕进去
Set-ItemProperty -Path $key -Name ProxyOverride -Value "localhost;127.*;10.*;172.16.*;192.168.*;<local>" -Type String

# 通知系统代理已变化；失败不影响设置本身，重启浏览器即可
try {
    $signature = @'
[DllImport("wininet.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern bool InternetSetOption(IntPtr hInternet, int dwOption, IntPtr lpBuffer, int dwBufferLength);
'@
    $type = Add-Type -MemberDefinition $signature -Name WinInet -Namespace ProxyHelper -PassThru -ErrorAction Stop
    [void]$type::InternetSetOption([IntPtr]::Zero, 39, [IntPtr]::Zero, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
    [void]$type::InternetSetOption([IntPtr]::Zero, 37, [IntPtr]::Zero, 0)  # INTERNET_OPTION_REFRESH
    Write-Host "已通知系统刷新代理设置。"
} catch {
    Write-Host "未能自动刷新（不影响结果），请重启浏览器和微信。"
}

Write-Host ""
Write-Host "系统代理已指向 $ProxyServer（mitmproxy）。"
Write-Host "验证：浏览器打开 http://mitm.it —— 能看到证书下载页就说明通了。"
Write-Host "恢复：.\scripts\proxy-off.ps1"
