# Kill bot-system processes and relaunch one clean instance of each
$killed = @()
Get-CimInstance Win32_Process -Filter "Name='node.exe' or Name='python.exe'" | ForEach-Object {
  $cl = $_.CommandLine
  if (-not $cl) { return }
  if ($cl -match 'bridge\.js' -or $cl -match 'mc_bot_run' -or $cl -match 'python\.exe" bot\.py' -or $cl -match 'vox_tts_server') {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    $killed += ("{0}: {1}" -f $_.ProcessId, $cl.Substring(0, [Math]::Min(80, $cl.Length)))
  }
}
Write-Output "KILLED:"
$killed | ForEach-Object { Write-Output ("  " + $_) }
Start-Sleep -Seconds 2

$env:PYTHONIOENCODING = 'utf-8'
# 1) local TTS server (used by QQ voice)
$t = Start-Process -FilePath "F:\qq_bot\venv_vox\Scripts\python.exe" -ArgumentList "-u", "vox_tts_server.py" -WorkingDirectory "F:\qq_bot" -WindowStyle Hidden -RedirectStandardOutput "F:\qq_bot\vox_tts_server.log" -RedirectStandardError "F:\qq_bot\vox_tts.err.log" -PassThru
Write-Output ("START vox_tts pid " + $t.Id)
# 2) QQ bot
$b = Start-Process cmd -ArgumentList '/c', 'set PYTHONIOENCODING=utf-8&& F:\qq_bot\venv\Scripts\python.exe bot.py > F:\qq_bot\bot.out.log 2>&1' -WorkingDirectory "F:\qq_bot" -WindowStyle Hidden -PassThru
Write-Output ("START bot.py wrapper pid " + $b.Id)
# 3) mineflayer bridge (LAN autodetect + spectator 25565)
$n = Start-Process -FilePath "C:\Users\21907\AppData\Local\OpenClawPlanTool\runtime\node\node-v24.17.0-win-x64\node.exe" -ArgumentList "bridge.js" -WorkingDirectory "F:\qq_bot\mc_bot" -WindowStyle Hidden -RedirectStandardOutput "F:\qq_bot\mc_bot\bridge.out.log" -RedirectStandardError "F:\qq_bot\mc_bot\bridge.err.log" -PassThru
Write-Output ("START bridge pid " + $n.Id)
# 4) MC brain
$p = Start-Process cmd -ArgumentList '/c', 'set PYTHONIOENCODING=utf-8&& F:\qq_bot\venv\Scripts\python.exe -u mc_bot_run.py > F:\qq_bot\mc_bot_run.out.log 2>&1' -WorkingDirectory "F:\qq_bot" -WindowStyle Hidden -PassThru
Write-Output ("START brain wrapper pid " + $p.Id)
Write-Output "ALL_RESTARTED"
