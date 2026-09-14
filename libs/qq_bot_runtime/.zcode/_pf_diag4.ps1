# Check E volume properties that may block pagefile creation
Write-Output "--- Win32_Volume E ---"
Get-CimInstance Win32_Volume -Filter "DriveLetter='E:'" | ForEach-Object {
  Write-Output ("Caption=" + $_.Caption)
  Write-Output ("DriverType=" + $_.DriverType + " Type=" + $_.Type)
  Write-Output ("Compressed=" + $_.Compressed + " Encrypted=" + $_.Encrypted)
  Write-Output ("IndexingEnabled=" + $_.IndexingEnabled)
  Write-Output ("Automount=" + $_.Automount + " BootVolume=" + $_.BootVolume + " PageFilePresent=" + $_.PageFilePresent)
  Write-Output ("CapGB=" + [math]::Round($_.Capacity/1GB,1) + " FreeGB=" + [math]::Round($_.FreeSpace/1GB,1))
}
Write-Output "--- BitLocker (may need admin) ---"
$bl = manage-bde -status E: 2>&1
$bl | Select-String "保护状态|Protection State|Conversion Status|Volume" | Select-Object -First 6 | ForEach-Object { Write-Output $_.Line.Trim() }
Write-Output "--- fsutil behavior query (last access time) ---"
fsutil behavior query disablelastaccess 2>&1 | Select-Object -First 1
Write-Output "--- done ---"
