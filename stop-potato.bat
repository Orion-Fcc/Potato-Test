@echo off
rem ============================================================================
rem  Potato Test - 停止后台服务
rem
rem  用 PID 文件定位进程并结束；PID 文件丢失或过期时，
rem  退化为"扫描端口占用者"，保证一定能停掉。
rem
rem  编码：本文件为 GBK，故意不调用 chcp。
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PIDFILE=%~dp0.potato.pid"

title Potato Test - 停止服务
color 0E

echo.
echo   ============================================
echo      Potato Test  -  停止服务
echo   ============================================
echo.

set "KILLED=0"

rem ---- 1. 按 PID 文件停止 ----------------------------------------------------
if exist "%PIDFILE%" (
  set "OLD_PID="
  set "OLD_PORT="
  for /f "usebackq tokens=1,2" %%a in ("%PIDFILE%") do (
    set "OLD_PID=%%a"
    set "OLD_PORT=%%b"
  )
  if defined OLD_PID (
    tasklist /FI "PID eq !OLD_PID!" 2>nul | findstr /C:"!OLD_PID!" >nul
    if not errorlevel 1 (
      echo   正在结束进程 PID !OLD_PID! ...
      taskkill /F /T /PID !OLD_PID! >nul 2>&1
      if not errorlevel 1 (
        echo         已停止。
        set "KILLED=1"
      ) else (
        echo         结束失败，可能权限不足。
      )
    ) else (
      echo   PID 文件里的进程 !OLD_PID! 已不存在（服务可能已停止）。
    )
  )
) else (
  echo   未找到 PID 文件 .potato.pid。
)

rem ---- 2. 兜底：端口占用者 ---------------------------------------------------
rem  PID 文件可能被删掉、或进程被别的工具重启过。端口不会撒谎。
for %%P in (18080 18081 18082 18083 18084 18085) do (
  for /f "tokens=5" %%x in ('netstat -ano ^| findstr /C:":%%P " ^| findstr /C:"LISTENING"') do (
    if not "%%x"=="0" (
      echo   端口 %%P 仍被 PID %%x 占用，正在结束 ...
      taskkill /F /T /PID %%x >nul 2>&1
      set "KILLED=1"
    )
  )
)

if exist "%PIDFILE%" del /q "%PIDFILE%" >nul 2>&1

echo.
if "!KILLED!"=="1" (
  echo   服务已停止。再次使用请双击 start-potato.bat
) else (
  echo   没有发现正在运行的服务。
)
echo.
pause
endlocal & exit /b 0
