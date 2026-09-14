# 检查 NapCat Desktop 进程是否设置了 NAPCAT_WEBUI_SECRET_KEY 等环境变量
$procs = Get-Process -Name "NapCatQQ-Desktop" -ErrorAction SilentlyContinue
foreach ($p in $procs) {
    Write-Output ("=== PID " + $p.Id + " ===")
    # 通过 WMI 读进程环境变量较困难，改用读进程模块/句柄方式
}
# 尝试读 NapCat 的 worker/node 进程环境
$nodeProcs = Get-CimInstance Win32_Process -Filter "name='node.exe'"
foreach ($n in $nodeProcs) {
    Write-Output ("node PID=" + $n.ProcessId + " => " + $n.CommandLine)
}
Write-Output "---- 查找 NAPCAT 相关环境变量的注册表/文件 ----"
