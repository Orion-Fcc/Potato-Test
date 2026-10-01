@echo off
rem ============================================================================
rem  Potato Test - 取消开机自启（不影响正在运行的服务）
rem
rem  编码：本文件为 GBK，故意不调用 chcp。
rem ============================================================================
setlocal
cd /d "%~dp0"

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "ENTRY=%STARTUP%\PotatoTest-Autostart.cmd"

title Potato Test - 取消开机自启
color 0E

echo.
echo   ============================================
echo      Potato Test  -  取消开机自启
echo   ============================================
echo.

if exist "%ENTRY%" (
  del /f /q "%ENTRY%"
  if exist "%ENTRY%" (
    echo   [错误] 删除失败，请手动删除：
    echo          %ENTRY%
  ) else (
    echo   已取消开机自启。
  )
) else (
  echo   之前没有设置过开机自启，无需处理。
)

echo.
echo   注意：当前正在运行的服务不受影响，如需停止请双击 stop-potato.bat
echo.
pause
endlocal & exit /b 0
