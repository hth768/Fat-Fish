# ASCII-only pagefile info query
$os = Get-CimInstance Win32_OperatingSystem
$cs = Get-CimInstance Win32_ComputerSystem
Write-Output ("RAM total MB: " + [int]($os.TotalVisibleMemorySize/1KB))
Write-Output ("RAM free MB:  " + [int]($os.FreePhysicalMemory/1KB))
Write-Output ("Commit limit MB:  " + [int]($os.TotalVirtualMemorySize/1KB))
Write-Output ("Commit avail MB:  " + [int]($os.FreeVirtualMemory/1KB))
Write-Output ("AutoManagedPagefile: " + $cs.AutomaticManagedPagefile)
Write-Output "--- pagefile settings ---"
Get-CimInstance Win32_PageFileSetting | ForEach-Object { Write-Output ("SETTING: " + $_.Name + " init=" + $_.InitialSize + " max=" + $_.MaximumSize) }
Write-Output "--- pagefile usage ---"
Get-CimInstance Win32_PageFileUsage | ForEach-Object { Write-Output ("USAGE: " + $_.Name + " allocMB=" + $_.AllocatedBaseSize + " currentMB=" + $_.CurrentUsage + " peakMB=" + $_.PeakUsage) }
Write-Output "--- drive free space GB ---"
Get-CimInstance Win32_LogicalDisk -Filter "DriveType=3" | ForEach-Object { Write-Output ($_.DeviceID + " free=" + [int]($_.FreeSpace/1GB) + " total=" + [int]($_.Size/1GB)) }
