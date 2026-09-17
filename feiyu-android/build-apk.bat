@echo off
rem ============================================================
rem  肥鱼娘安卓版 - 一键构建脚本(命令行出 APK,不依赖 IDE)
rem  输出: app\build\outputs\apk\debug\app-debug.apk
rem ============================================================
setlocal
set JAVA_HOME=E:\jdk17\jdk-17.0.20.1+1
set PATH=%JAVA_HOME%\bin;%PATH%
set GRADLE_USER_HOME=E:\gradle-home
set GRADLE=E:\gradle-dist\gradle-8.9\bin\gradle.bat

cd /d "%~dp0"
call "%GRADLE%" assembleDebug --no-daemon --max-workers=2 "-Dorg.gradle.jvmargs=-Xmx1024m -XX:MaxMetaspaceSize=384m" "-Dkotlin.daemon.jvm.options=-Xmx512m"

echo.
if exist "app\build\outputs\apk\debug\app-debug.apk" (
    echo 构建成功! APK 位置:
    echo   %~dp0app\build\outputs\apk\debug\app-debug.apk
) else (
    echo 构建失败,请查看上方报错信息。
)
pause
