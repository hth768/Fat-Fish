# Stop bot-related processes: bot.py / mc_bot_run.py / vox_tts_server / mineflayer bridge
# Keeps: QQ, NapCat(SnowLuma), Minecraft game, ZCode
foreach ($p in Get-CimInstance Win32_Process -Filter "Name='python.exe'") {
    if ($p.CommandLine -match 'bot\.py') {
        Write-Output ("stop bot.py PID=" + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($p.CommandLine -match 'mc_bot_run\.py') {
        Write-Output ("stop mc_bot_run.py PID=" + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($p.CommandLine -match 'vox_tts_server\.py') {
        Write-Output ("stop vox_tts_server.py PID=" + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
foreach ($p in Get-CimInstance Win32_Process -Filter "Name='node.exe'") {
    if ($p.CommandLine -match 'bridge\.js') {
        Write-Output ("stop mineflayer bridge PID=" + $p.ProcessId)
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
}
Write-Output "--- done ---"
