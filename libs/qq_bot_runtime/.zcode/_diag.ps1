# ASCII-only diagnostic + bridge restart helper
$listen = netstat -ano | Select-String "LISTENING" | Select-String ":64245|:8767"
Write-Output "ports:"; $listen | ForEach-Object { Write-Output $_.Line }
$brains = Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'mc_bot_run|bot\.py' }
Write-Output "brains:"
$brains | ForEach-Object { Write-Output ("{0}: {1}" -f $_.ProcessId, $_.CommandLine) }
if ($args[0] -eq 'startbridge') {
  if (-not ($listen | Select-String ":8767")) {
    $env:MC_HOST = '127.0.0.1'; $env:MC_PORT = '64245'; $env:MC_USER = 'feiyu_bot'
    $b = Start-Process -FilePath "C:\Users\21907\AppData\Local\OpenClawPlanTool\runtime\node\node-v24.17.0-win-x64\node.exe" -ArgumentList "bridge.js" -WorkingDirectory "F:\qq_bot\mc_bot" -WindowStyle Hidden -RedirectStandardOutput "F:\qq_bot\mc_bot\bridge.out.log" -RedirectStandardError "F:\qq_bot\mc_bot\bridge.err.log" -PassThru
    Write-Output ("bridge started pid " + $b.Id)
  } else { Write-Output "bridge already up" }
}
