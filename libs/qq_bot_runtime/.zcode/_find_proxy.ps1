# Find installed proxy software via uninstall registry keys (ASCII only)
$paths = @(
  'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
Get-ItemProperty $paths -ErrorAction SilentlyContinue |
  Where-Object { ($_.DisplayName -match 'clash|v2ray|xray|verge|sing-box|mihomo|neko|hiddify|karing|flclash|proxy|rocket|shadowsocks|ssr|trojan') } |
  ForEach-Object { Write-Output ("NAME: " + $_.DisplayName + " | LOC: " + $_.InstallLocation + " | ICON: " + $_.DisplayIcon) }
Write-Output "--- start menu scan ---"
$sm = @("$env:APPDATA\Microsoft\Windows\Start Menu\Programs", "$env:ProgramData\Microsoft\Windows\Start Menu\Programs")
Get-ChildItem $sm -Recurse -Filter *.lnk -ErrorAction SilentlyContinue |
  Where-Object { $_.Name -match 'clash|v2ray|verge|sing|mihomo|neko|hiddify|karing|flclash|proxy|rocket' } |
  ForEach-Object { Write-Output ("LNK: " + $_.FullName) }
Write-Output "--- done ---"
