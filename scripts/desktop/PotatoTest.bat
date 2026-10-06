@echo off
rem ============================================================================
rem  Potato Test  （桌面启动器 · 自包含）
rem
rem  只管两件事：启动、关闭。
rem  所有逻辑都在本文件里，**不依赖项目文件夹里的任何 .bat**。
rem  项目位置变了的话，只改下面这行 PROJ。
rem
rem  服务是后台常驻的：关掉本窗口不会停服务，要停请用菜单里的「2 关闭」。
rem  日志在 <项目>\logs\potato.log
rem
rem  编码：GBK，故意不调用 chcp（调了中文反而乱码）。
rem ============================================================================
setlocal enabledelayedexpansion
title Potato Test

rem  PROJ 的取法（2026-10-06 迁移后改为可移植）：
rem    1) 先按"本文件在 <项目>\scripts\desktop\ 下"推算 —— 项目整体搬到哪都还能用；
rem    2) 推不出来（例如本文件被复制到了桌面）就回落到 PROJ_FALLBACK。
rem       项目再次搬家时，只需要改下面这一行。
set "PROJ=%~dp0..\.."
for %%I in ("%~dp0..\..") do set "PROJ=%%~fI"
set "PROJ_FALLBACK=D:\agent_study\Potato Test"
if not exist "%PROJ%\run_server.py" set "PROJ=%PROJ_FALLBACK%"
set "VENV=%PROJ%\.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "PIP=%VENV%\Scripts\pip.exe"
set "PIDFILE=%PROJ%\.potato.pid"
set "BOTPID=%PROJ%\.feishu_ws.pid"
set "LOGDIR=%PROJ%\logs"
set "PORT=18081"

if not exist "%PROJ%\run_server.py" (
  echo.
  echo   [错误] 找不到项目目录：
  echo          %PROJ%
  echo.
  echo   如果你把项目挪走了，用记事本打开本文件，
  echo   把开头的 PROJ 改成新路径即可。
  echo.
  pause
  exit /b 1
)
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

rem  切到项目根再操作：app 用 env_file=".env"（相对工作目录）读配置，
rem  从桌面启动时工作目录是桌面，会导致 .env 找不到、静默改用默认数据库。
cd /d "%PROJ%"

:MENU
cls
echo ==========================================
echo    Potato Test
echo ==========================================
echo.
echo  1  启动
echo  2  关闭
echo  0  退出
echo.
echo  直接回车 = 1
echo ------------------------------------------
set "C="
set /p "C=请选择: "
rem 输入流结束（没有键盘输入）时 set /p 会置 errorlevel=1，直接退出，避免空转。
if errorlevel 1 exit /b 0
if "%C%"=="" set "C=1"
if "%C%"=="1" goto START
if "%C%"=="2" goto STOP
if "%C%"=="0" exit /b 0
echo.
echo  无效选项。
ping -n 3 127.0.0.1 >nul
goto MENU

rem ============================================================================
rem  启动
rem ============================================================================
:START
echo.
rem  2026-10-05 改造：端口固定为 18081，且每次启动前先结束占用它的进程。
rem
rem  为什么去掉"自动挑空闲端口"：换端口会让第二次启动**悄悄多出一个实例**，
rem  两个实例共用同一个 SQLite 库，跑起来互相踩。要的是确定性，不是碰运气。
rem  先杀后启 —— 任何情况下都只有一个实例在跑。
rem
rem  为什么删掉原来的 :detect_running：它要判断"端口上的是不是我们自己的服务"，
rem  而那段实现里 for /f 的单引号没配对（开了引号没有闭合），命令根本没执行，
rem  于是每次都误判"不是我们的" → 换端口启动第二个实例，且顺手删掉 pid 文件。
rem  端口固定后这个判断不再需要，整段删除。
call :kill_port
if exist "%PIDFILE%" del /q "%PIDFILE%" >nul 2>&1
if exist "%PIDFILE%" del /q "%PIDFILE%" >nul 2>&1

rem ---- 1/4 Python 环境 -------------------------------------------------------
if exist "%PY%" goto :have_py
echo   [1/4] 首次运行，正在创建 Python 虚拟环境...
where python >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [错误] 找不到 Python。请先安装 Python 3.11 或更高版本：
  echo          https://www.python.org/downloads/
  echo          安装时务必勾选 "Add python.exe to PATH"。
  echo.
  pause
  goto MENU
)
python -m venv "%VENV%"
if not exist "%PY%" (
  echo   [错误] 虚拟环境创建失败。
  pause
  goto MENU
)
echo         完成。
goto :check_deps

:have_py
echo   [1/4] Python 就绪。

rem ---- 2/4 后端依赖 ---------------------------------------------------------
rem  browser_use / playwright 必须一起查：它们是懒加载的，缺了不会阻止服务启动，
rem  只会在你点"运行"时报 No module named 'browser_use'。
:check_deps
"%PY%" -c "import fastapi, sqlalchemy, aiosqlite, uvicorn, sse_starlette, playwright, browser_use" >nul 2>&1
if not errorlevel 1 (
  echo   [2/4] 后端依赖已安装。
  goto :check_chromium
)
echo   [2/4] 正在安装后端依赖（首次约 5-10 分钟，browser-use 依赖树较大）...
echo.
rem  分批安装：中途断网会整批回滚，一次装几十个包可能最后一个都不剩。
"%PIP%" install --disable-pip-version-check --upgrade pip
for %%P in (
  "fastapi uvicorn[standard]"
  "sqlalchemy[asyncio] aiosqlite pydantic-settings"
  "httpx sse-starlette python-multipart"
  "cryptography bcrypt pyjwt openpyxl"
  "playwright==1.63.0"
) do (
  echo   ---- installing %%P
  "%PIP%" install --disable-pip-version-check %%P
  if errorlevel 1 (
    echo.
    echo   [错误] 安装 %%P 失败。常见原因是网络不通。
    echo          若在用代理，请先在另一个窗口执行：
    echo            set HTTP_PROXY=http://127.0.0.1:端口
    echo            set HTTPS_PROXY=http://127.0.0.1:端口
    echo          然后再运行本程序。
    echo.
    pause
    goto MENU
  )
)
echo   ---- installing browser-use[video]==0.13.10 (大依赖，耐心等待)
"%PIP%" install --disable-pip-version-check "browser-use[video]==0.13.10"
if errorlevel 1 (
  echo.
  echo   [错误] 安装 browser-use 失败，没有它无法执行任何测试用例。
  echo.
  pause
  goto MENU
)
echo         依赖安装完成。

rem ---- 3/4 Chromium ---------------------------------------------------------
rem  装了 playwright 这个包 ≠ 有浏览器，必须另外下载。
:check_chromium
if exist "%USERPROFILE%\AppData\Local\ms-playwright" (
  echo   [3/4] Chromium 已就绪。
  goto :check_env
)
echo   [3/4] 正在下载 Chromium（约 150MB，用于执行测试用例）...
"%PY%" -m playwright install chromium
if errorlevel 1 (
  echo   [警告] Chromium 下载失败。服务能启动，但跑用例时会报找不到浏览器。
  echo          联网后手动重试： "%PY%" -m playwright install chromium
)

rem ---- 4/4 配置文件 ---------------------------------------------------------
:check_env
if exist "%PROJ%\.env" (
  echo   [4/4] .env 已存在。
) else (
  if exist "%PROJ%\.env.example" (
    echo   [4/4] 未找到 .env，正在从 .env.example 复制...
    copy /y "%PROJ%\.env.example" "%PROJ%\.env" >nul
  ) else (
    echo   [4/4] 未找到 .env（将使用内置默认值）。
  )
)

rem  端口已固定为 18081（见文件开头的 PORT）。
rem  这里不再扫描空闲端口：占用者已在 :START 里由 :kill_port 结束，
rem  所以走到这里端口一定是空的。

rem ---- 后台启动 -------------------------------------------------------------
rem  pythonw.exe 是 GUI 子系统程序，不分配控制台 -> 不弹窗口；
rem  start 让它脱离本窗口 -> 关掉本窗口服务照样活着。
rem  日志由 run_server.py 自己写文件，所以不需要重定向。
echo.
echo   正在后台启动 ...
rem 虚拟环境被删/被移走时 %PYW% 会指向不存在的文件，
rem 而 start 会把它当空目标处理，弹「找不到 '\\' 文件」。
if not exist "%PYW%" (
  echo.
  echo   [错误] 找不到虚拟环境里的 pythonw.exe：
  echo          %PYW%
  echo          删除目录 "%VENV%" 后重新运行本脚本即可重建。
  echo.
  pause
  goto MENU
)
start "" "%PYW%" "%PROJ%\run_server.py" %PORT%

rem ---- 等它就绪 -------------------------------------------------------------
set /a _w=0
:wait_ready
set /a _w+=1
netstat -ano | findstr /C:":%PORT% " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 goto :ready
if %_w% GEQ 30 goto :start_failed
ping -n 2 127.0.0.1 >nul
goto :wait_ready

:ready
if not defined PORT goto :start_failed
rem  从端口反查真正在监听的 PID 记下来，供「关闭」精确结束。
set "SRV_PID="
for /f "tokens=5" %%x in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr /C:"LISTENING"') do (
  if not "%%x"=="0" if not defined SRV_PID set "SRV_PID=%%x"
)
if defined SRV_PID > "%PIDFILE%" echo !SRV_PID! %PORT%
call :start_bot
echo.
echo   启动成功： http://127.0.0.1:%PORT%/
echo.
echo   服务在后台运行，关闭本窗口不影响它。
echo   要停止，请重新打开本程序选择「2 关闭」。
echo.
start "" "http://127.0.0.1:%PORT%/"
pause
goto MENU

:start_failed
echo.
echo   [失败] 等待 30 秒服务仍未就绪。
echo          日志： %LOGDIR%\potato.log
echo.
pause
goto MENU

rem ============================================================================
rem  关闭
rem ============================================================================
rem ============================================================================
rem  关闭
rem  端口固定之后，"结束占用该端口的进程"就够了 —— 不再需要 pid 文件，
rem  也不再需要扫一段端口范围（扫范围正是之前误判的源头）。
rem ============================================================================
:STOP
echo.
set "KILLED=0"
set "KPID="
for /f "tokens=5" %%x in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr /C:"LISTENING"') do if not defined KPID set "KPID=%%x"

if defined KPID (
  if not "!KPID!"=="0" (
    echo   正在结束端口 %PORT% 上的进程 PID !KPID! ...
    taskkill /F /T /PID !KPID! >nul 2>&1
    if not errorlevel 1 set "KILLED=1"
  )
)
rem  pid 文件只是线索，清掉避免下次误判成"还在跑"。
if exist "%PIDFILE%" del /q "%PIDFILE%" >nul 2>&1
call :stop_bot

echo.
if "!KILLED!"=="1" (
  echo   已关闭。
) else (
  echo   没有发现正在运行的服务。
)
echo.
pause
goto MENU
rem ============================================================================
rem  子程序：结束占用目标端口的进程
rem  启动前调用，保证全局只有一个服务实例。
rem  只能"先杀后启"：端口被占时直接启动会失败，而不杀就换端口会多出第二个实例。
rem ============================================================================
:kill_port
set "KPID="
rem  这个 for /f **不能**加 usebackq，加了单引号会变成字面量、命令不执行。
rem  注意 ^| 的转义（原实现就是在这里同时踩了两个坑）。
for /f "tokens=5" %%x in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr /C:"LISTENING"') do if not defined KPID set "KPID=%%x"
if not defined KPID exit /b 0
if "!KPID!"=="0" exit /b 0

echo   端口 %PORT% 已被 PID !KPID! 占用，正在结束它 ...
taskkill /F /T /PID !KPID! >nul 2>&1

rem  等端口真正释放：taskkill 返回时进程可能还没退干净，
rem  立刻启动会撞 "address already in use"（表现为启动后端口没监听上）。
set /a _k=0
:kill_port_wait
netstat -ano | findstr /C:":%PORT% " | findstr /C:"LISTENING" >nul 2>&1
if errorlevel 1 exit /b 0
set /a _k+=1
if !_k! GEQ 15 (
  echo   [警告] 端口 %PORT% 仍被占用，启动可能失败。
  echo          请手动结束该进程后重试。
  exit /b 0
)
ping -n 2 127.0.0.1 >nul
goto :kill_port_wait

rem  子程序：群机器人（飞书）启停
rem  只有它在跑，才能在群里 @机器人 问「状态」。进程自己把 PID 写进 .feishu_ws.pid，
rem  因为 pythonw 没有窗口标题，也没法靠 pythonw.exe 名字区分（那会连带杀掉服务）。
rem ============================================================================
:start_bot
if not exist "%PROJ%\app\feishu_poll.py" exit /b 0
rem  .env 里没开 ENABLE_FEISHU 就别启动，白白占一个进程。
if not exist "%PROJ%\.env" exit /b 0
findstr /i /b /c:"ENABLE_FEISHU=true" "%PROJ%\.env" >nul 2>&1
if errorlevel 1 exit /b 0
rem  已经在跑就别再开一个：同一群两个进程会对同一条消息各回一次。
if exist "%BOTPID%" (
  set "OLD_BOT="
  for /f "usebackq" %%a in ("%BOTPID%") do if not defined OLD_BOT set "OLD_BOT=%%a"
  if defined OLD_BOT (
    tasklist /FI "PID eq !OLD_BOT!" /FI "IMAGENAME eq pythonw.exe" /NH 2>nul | findstr /R /C:"^pythonw.exe" >nul
    if not errorlevel 1 (
      echo   群机器人已在运行（PID !OLD_BOT!）。
      exit /b 0
    )
  )
)
start "" "%PYW%" -m app.feishu_poll
echo   群机器人已启动 —— 在飞书群里 @机器人 问「状态」就能拿到进度。
exit /b 0

:stop_bot
if not exist "%BOTPID%" exit /b 0
set "BOT_PID="
for /f "usebackq" %%a in ("%BOTPID%") do if not defined BOT_PID set "BOT_PID=%%a"
if defined BOT_PID (
  tasklist /FI "PID eq !BOT_PID!" /FI "IMAGENAME eq pythonw.exe" /NH 2>nul | findstr /R /C:"^pythonw.exe" >nul
  if not errorlevel 1 (
    echo   正在结束群机器人 PID !BOT_PID! ...
    taskkill /F /PID !BOT_PID! >nul 2>&1
  )
)
del /q "%BOTPID%" >nul 2>&1
exit /b 0