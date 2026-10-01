@echo off
rem ============================================================================
rem  Potato Test - 开启开机自启
rem
rem  在"启动"文件夹里放一个引导脚本，登录后自动把服务后台拉起来。
rem  用启动文件夹而不是注册服务：不需要管理员权限，卸载只要删文件。
rem
rem  编码：本文件为 GBK，故意不调用 chcp。
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PROJ=%~dp0"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "ENTRY=%STARTUP%\PotatoTest-Autostart.cmd"

title Potato Test - 开机自启
color 0A

echo.
echo   ============================================
echo      Potato Test  -  开启开机自启
echo   ============================================
echo.

if not exist "%STARTUP%" (
  echo   [错误] 找不到启动文件夹：
  echo          %STARTUP%
  echo.
  pause
  exit /b 1
)

if not exist "%PROJ%start-potato.bat" (
  echo   [错误] 找不到 start-potato.bat，请确认本脚本和它放在同一目录。
  echo.
  pause
  exit /b 1
)

rem  生成的引导脚本内容保持纯 ASCII：它由 cmd 以自己的代码页写入并读取，
rem  写入带中文的路径在不同区域设置下有可能损坏。
> "%ENTRY%" echo @echo off
>>"%ENTRY%" echo rem 由 autostart-on.bat 生成；删除本文件即可取消开机自启。
>>"%ENTRY%" echo cd /d "%PROJ%"
>>"%ENTRY%" echo call "%PROJ%start-potato.bat" silent

if not exist "%ENTRY%" (
  echo   [错误] 写入启动项失败。
  echo.
  pause
  exit /b 1
)

rem  先验证一次能正常启动，避免"设了自启但每次都起不来"这种沉默故障。
echo   已写入开机自启项：
echo     %ENTRY%
echo.
echo   正在做一次试启动，确认服务能被正常拉起 ...
echo.
call "%PROJ%start-potato.bat" silent

netstat -ano | findstr /C:":18080 " | findstr /C:"LISTENING" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [警告] 试启动后 18080 未处于监听状态。
  echo          开机自启已配置，但请先手动双击 start-potato.bat 看看是否有报错。
  echo          日志：logs\potato.err.log
) else (
  echo.
  echo   试启动成功，服务已在后台运行。
  echo.
  echo   从现在起：每次登录 Windows 会自动在后台启动 Potato Test，
  echo   你只需要打开浏览器访问 http://127.0.0.1:18080/
  echo.
  echo   取消自启：双击 autostart-off.bat
)
echo.
pause
endlocal & exit /b 0
