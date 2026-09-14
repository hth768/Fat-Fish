# ASCII only. Diagnose why E pagefile not created after boot.
Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='E:' or DeviceID='F:' or DeviceID='C:'" | ForEach-Object {
  Write-Output ("{0} fs={1} label={2}" -f $_.DeviceID, $_.FileSystem, $_.VolumeName)
}
Write-Output "--- recent system errors around paging ---"
Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2,3; StartTime=(Get-Date).AddMinutes(-40)} -MaxEvents 100 -ErrorAction SilentlyContinue |
  Where-Object { $_.Message -match 'page|paging' } |
  ForEach-Object { Write-Output ($_.TimeCreated.ToString('HH:mm:ss') + " [" + $_.ProviderName + "] " + $_.Message.Replace("`r`n"," ").Substring(0, [Math]::Min(180, $_.Message.Length))) }
Write-Output "--- registry PagingFiles (this control set) ---"
(Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management' -Name PagingFiles).PagingFiles | ForEach-Object { Write-Output ("ENTRY: " + $_) }
Write-Output "--- CurrentControlSet selection ---"
(Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control' -Name 'Current').Current | ForEach-Object { Write-Output ("CurrentControlSet = " + $_) }
