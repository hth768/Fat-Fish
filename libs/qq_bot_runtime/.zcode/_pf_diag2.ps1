# ASCII only: check E root ACL and app-log pagefile events
Write-Output "--- E:\ ACL ---"
$acl = Get-Acl 'E:\'
$acl.Access | ForEach-Object { Write-Output ($_.IdentityReference.ToString() + " | " + $_.FileSystemRights + " | " + $_.AccessControlType) }
Write-Output "--- System/SystemPageFile? free-space gate ---"
Write-Output ("E free bytes: " + (Get-PSDrive E).Free)
Write-Output "--- application log pagefile events since boot ---"
Get-WinEvent -FilterHashtable @{LogName='Application'; StartTime=(Get-Date).AddHours(-2)} -MaxEvents 300 -ErrorAction SilentlyContinue |
  Where-Object { $_.Message -match 'pagefile|paging' } |
  ForEach-Object { Write-Output ($_.TimeCreated.ToString('HH:mm:ss') + " [" + $_.ProviderName + "] " + $_.Message.Replace("`r`n", " ")) }
Write-Output "--- done ---"
