# ASCII only: runtime pagefile usage vs settings
Get-CimInstance Win32_PageFileUsage | ForEach-Object {
  Write-Output ("USAGE: " + $_.Name + " allocMB=" + $_.AllocatedBaseSize + " cur=" + $_.CurrentUsage + " peak=" + $_.PeakUsage)
}
Write-Output "--- E:\pagefile.sys file? ---"
$pf = Get-Item 'E:\pagefile.sys' -Force -ErrorAction SilentlyContinue
if ($pf) { Write-Output ("EXISTS sizeGB=" + [math]::Round($pf.Length/1GB,1)) } else { Write-Output "NOT PRESENT" }
Write-Output "--- all pagefile.sys on fixed drives ---"
foreach ($d in @('C','D','E','F')) {
  $f = Get-Item ($d + ':\pagefile.sys') -Force -ErrorAction SilentlyContinue
  if ($f) { Write-Output ($d + ": " + [math]::Round($f.Length/1GB,1) + "GB") }
}
Write-Output "--- system log: any kernel paging events today (all levels) ---"
Get-WinEvent -FilterHashtable @{LogName='System'; StartTime=(Get-Date).AddHours(-3)} -MaxEvents 500 -ErrorAction SilentlyContinue |
  Where-Object { $_.Message -match 'page' } |
  Select-Object -First 8 | ForEach-Object { Write-Output ($_.TimeCreated.ToString('HH:mm:ss') + " L" + $_.Level + " [" + $_.ProviderName + "] " + ($_.Message -replace "`r`n|`n", " ").Substring(0, [Math]::Min(160, ($_.Message -replace "`r`n|`n"," ").Length))) }
Write-Output "--- done ---"
