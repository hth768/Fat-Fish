# Apply pagefile layout via registry (Win11 24H2: WMI Win32_PageFileSetting is deprecated for writes).
# Requires elevation. Takes effect after reboot.
$ErrorActionPreference = 'Stop'
$log = 'F:\qq_bot\.zcode\_pf_apply.log'
try {
  $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management'
  $old = (Get-ItemProperty -LiteralPath $key -Name PagingFiles).PagingFiles
  ('BEFORE: ' + ($old -join ' | ')) | Out-File -FilePath $log -Encoding ascii
  $new = @(
    'C:\pagefile.sys 4096 8192',
    'E:\pagefile.sys 16384 32768'
  )
  Set-ItemProperty -LiteralPath $key -Name PagingFiles -Value $new -Type MultiString
  $after = (Get-ItemProperty -LiteralPath $key -Name PagingFiles).PagingFiles
  ('AFTER: ' + ($after -join ' | ')) | Out-File -FilePath $log -Append -Encoding ascii
  'RESULT: OK - reboot required to take effect' | Out-File -FilePath $log -Append -Encoding ascii
} catch {
  ('RESULT: FAIL - ' + $_.Exception.Message) | Out-File -FilePath $log -Append -Encoding ascii
}
