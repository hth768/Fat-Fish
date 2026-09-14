# Apply final pagefile layout: C guaranteed bigger, E entry kept (24H2 sometimes needs 2nd boot).
$ErrorActionPreference = 'Stop'
$log = 'F:\qq_bot\.zcode\_pf_apply2.log'
try {
  $key = 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management'
  $old = (Get-ItemProperty -LiteralPath $key -Name PagingFiles).PagingFiles
  ('BEFORE: ' + ($old -join ' | ')) | Out-File -FilePath $log -Encoding ascii
  $new = @(
    'C:\pagefile.sys 8192 24576',
    'E:\pagefile.sys 16384 32768'
  )
  Set-ItemProperty -LiteralPath $key -Name PagingFiles -Value $new -Type MultiString
  $after = (Get-ItemProperty -LiteralPath $key -Name PagingFiles).PagingFiles
  ('AFTER: ' + ($after -join ' | ')) | Out-File -FilePath $log -Append -Encoding ascii
  'RESULT: OK - reboot required' | Out-File -FilePath $log -Append -Encoding ascii
} catch {
  ('RESULT: FAIL - ' + $_.Exception.Message) | Out-File -FilePath $log -Append -Encoding ascii
}
