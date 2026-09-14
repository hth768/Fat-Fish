Get-CimInstance Win32_Process -Filter "name='QQ.exe' OR name='NapCatQQ-Desktop.exe' OR name='NapCatWinBootMain.exe'" | ForEach-Object {
    Write-Output ("PID=" + $_.ProcessId + " NAME=" + $_.Name)
    Write-Output ("  CMD=" + $_.CommandLine)
}
Write-Output "---- 系统环境变量 ----"
Get-ChildItem Env: | Where-Object { $_.Name -match "NAPCAT" } | ForEach-Object {
    Write-Output ($_.Name + "=" + $_.Value)
}
