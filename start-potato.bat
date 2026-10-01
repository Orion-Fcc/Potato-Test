@echo off
rem ============================================================================
rem  Potato Test - 一键启动（后台常驻版）
rem
rem  行为：
rem    1. 检查环境（Python / 依赖 / Chromium / .env / 前端），缺什么补什么
rem    2. 如果服务已经在跑 —— 直接打开浏览器，不会重复启动
rem    3. 否则后台启动服务（无窗口常驻），等它就绪后自动打开浏览器
rem    4. 本窗口随即关闭，服务继续在后台运行
rem
rem  停止服务：双击 stop-potato.bat
rem  开机自启：双击 autostart-on.bat（取消：autostart-off.bat）
rem  查看日志：logs\potato.log
rem
rem  参数：
rem    start-potato.bat 19000      指定端口
rem    start-potato.bat silent     静默模式（供开机自启调用，不弹浏览器）
rem
rem  编码：本文件为 GBK，故意不调用 chcp（调了反而乱码）。
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "PIP=%VENV%\Scripts\pip.exe"
set "PIDFILE=%~dp0.potato.pid"
set "LOGDIR=%~dp0logs"

set "SILENT="
set "PORT_START=18080"
set "PORT_MAX=18120"
for %%A in (%*) do (
  if /I "%%A"=="silent" ( set "SILENT=1" ) else ( set "PORT_START=%%A" )
)

if not defined SILENT (
  title Potato Test
  color 0B
  echo.
  echo   ============================================
  echo      Potato Test  -  一键启动
  echo   ============================================
  echo.
)

if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1

rem ---- 已经在运行吗？-------------------------------------------------------
rem  先看 PID 文件而不是先挑端口：服务可能占着一个不是默认值的端口，
rem  如果先挑端口就会误判为"没在跑"，从而启动第二个实例。
call :detect_running
if defined RUNNING_PORT (
  if not defined SILENT (
    echo   [提示] 服务已在运行（端口 %RUNNING_PORT%），直接打开浏览器。
    echo          如需停止，双击 stop-potato.bat
    echo.
    start "" "http://127.0.0.1:%RUNNING_PORT%/"
    timeout /t 3 /nobreak >nul
  )
  endlocal & exit /b 0
)
if exist "%PIDFILE%" del /q "%PIDFILE%" >nul 2>&1

rem ---- 1. Python ------------------------------------------------------------
if not exist "%PY%" goto :make_venv
if not defined SILENT echo   [1/5] Python 就绪。
goto :check_deps

:make_venv
if not defined SILENT echo   [1/5] 正在创建 Python 虚拟环境 .venv ...
where python >nul 2>&1
if errorlevel 1 (
  where py >nul 2>&1
  if errorlevel 1 (
    echo.
    echo   [错误] 找不到 Python。请先安装 Python 3.11 或更高版本：
    echo          https://www.python.org/downloads/
    echo          安装时务必勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b 1
  )
  py -3 -m venv "%VENV%"
) else (
  python -m venv "%VENV%"
)
if not exist "%PY%" (
  echo   [错误] 虚拟环境创建失败。
  pause
  exit /b 1
)
if not defined SILENT echo         完成。

rem ---- 2. 后端依赖 ----------------------------------------------------------
rem  必须把 browser_use / playwright 也查进来：它们是懒加载的，
rem  缺了不会阻止服务启动，只会在点"运行"时炸掉。
:check_deps
"%PY%" -c "import fastapi, sqlalchemy, aiosqlite, uvicorn, sse_starlette, playwright, browser_use" >nul 2>&1
if errorlevel 1 goto :install_deps
if not defined SILENT echo   [2/5] 后端依赖已安装。
goto :check_browser

:install_deps
if not defined SILENT echo   [2/5] 正在安装后端依赖（首次约 5-10 分钟，browser-use 依赖树较大）...
echo.
rem  分批安装：中途断网会整批回滚，一次装几十个包最终可能一个都没留下。
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
    echo   [错误] 安装 %%P 失败。常见原因：网络不通。
    echo          如果你在用代理，请先设置：
    echo            set HTTP_PROXY=http://127.0.0.1:端口
    echo            set HTTPS_PROXY=http://127.0.0.1:端口
    echo          然后重新运行本脚本。
    echo.
    pause
    exit /b 1
  )
)
echo   ---- installing browser-use[video]==0.13.10 (大依赖，耐心等待)
"%PIP%" install --disable-pip-version-check "browser-use[video]==0.13.10"
if errorlevel 1 (
  echo.
  echo   [错误] 安装 browser-use 失败。没有它无法执行任何测试用例。
  echo.
  pause
  exit /b 1
)
if not defined SILENT echo         依赖安装完成。

rem ---- 3. Chromium ----------------------------------------------------------
:check_browser
if exist "%USERPROFILE%\AppData\Local\ms-playwright" (
  if not defined SILENT echo   [3/5] Chromium 已就绪。
  goto :check_env
)
if not defined SILENT echo   [3/5] 正在下载 Chromium（约 150MB，用于执行测试用例）...
"%PY%" -m playwright install chromium
if errorlevel 1 (
  echo   [警告] Chromium 下载失败。服务能启动，但跑用例时会报找不到浏览器。
  echo          联网后手动重试： .venv\Scripts\python -m playwright install chromium
)
if not defined SILENT echo         完成。

rem ---- 4. .env --------------------------------------------------------------
:check_env
if not exist "%~dp0.env" (
  if exist "%~dp0.env.example" (
    if not defined SILENT echo   [4/5] 未找到 .env，正在从 .env.example 复制...
    copy /y "%~dp0.env.example" "%~dp0.env" >nul
  ) else (
    if not defined SILENT echo   [4/5] 未找到 .env，将使用内置默认值。
  )
) else (
  if not defined SILENT echo   [4/5] .env 已存在。
)

rem ---- 5. 前端 ---------------------------------------------------------------
if exist "%~dp0web\dist\index.html" (
  if not defined SILENT echo   [5/5] 前端已构建。
  goto :pick_port
)
rem  静默模式（开机自启）不能卡在交互提示上：此时若有构建产物就用，
rem  没有就只起 API，绝不等待输入。
if defined SILENT (
  echo   [警告] 未发现前端构建产物 web\dist，只启动 API。
  goto :pick_port
)
echo   [5/5] 未发现前端构建产物 web\dist。
echo.
echo         前端需要先构建一次。请选择：
echo           1. 现在构建（需要 Node.js，约 1-3 分钟）
echo           2. 跳过 —— 只启动 API（浏览器访问不到界面）
echo.
set "BUILD="
set /p "BUILD=请输入 1 或 2 [默认 1]: "
if "!BUILD!"=="2" goto :pick_port

where npm >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [警告] 找不到 npm，跳过前端构建。请安装 Node.js 18+ 后重试：
  echo          https://nodejs.org/
  echo.
  goto :pick_port
)
echo.
echo         正在安装前端依赖并构建（首次较慢）...
pushd "%~dp0web"
call npm install --no-audit --no-fund
if errorlevel 1 (
  echo   [警告] npm install 失败，跳过前端构建。
  popd
  goto :pick_port
)
call npm run build
popd
if not exist "%~dp0web\dist\index.html" (
  echo   [警告] 前端构建未产出 web\dist\index.html，界面可能不可用。
)

rem ---- 挑一个空闲端口 --------------------------------------------------------
:pick_port
set "PORT="
set /a _p=%PORT_START%
:find_port
netstat -ano | findstr /C:":!_p! " | findstr /C:"LISTENING" >nul 2>&1
if errorlevel 1 (
  set "PORT=!_p!"
  goto :port_found
)
set /a _p+=1
if !_p! GTR %PORT_MAX% (
  echo   [错误] %PORT_START%-%PORT_MAX% 范围内没有空闲端口。
  echo          请指定其他端口，例如：start-potato.bat 19000
  echo.
  pause
  exit /b 1
)
goto :find_port

:port_found
if not "%PORT%"=="%PORT_START%" (
  if not defined SILENT (
    echo   [提示] 端口 %PORT_START% 已被占用，自动改用 %PORT%。
    echo.
  )
)

rem ---- 后台启动 --------------------------------------------------------------
rem  为什么是 start + pythonw + run_server.py：
rem    pythonw.exe 是 GUI 子系统程序，不分配控制台，所以不会弹出任何窗口；
rem    start 让它独立于本窗口，本窗口关掉它照样活着 —— 这是"一直保持在线"的关键。
rem    日志由 run_server.py 自己写进 logs\potato.log，不需要 cmd 重定向，
rem    也就不必和引号转义、以及外部启动器的环境块问题纠缠。
if not defined SILENT (
  echo.
  echo   ============================================
  echo     正在后台启动 Potato Test ...
  echo   ============================================
  echo.
)
start "" "%PYW%" "%~dp0run_server.py" %PORT%

rem ---- 等它就绪 --------------------------------------------------------------
set /a _w=0
:wait_ready
set /a _w+=1
netstat -ano | findstr /C:":%PORT% " | findstr /C:"LISTENING" >nul 2>&1
if not errorlevel 1 goto :ready
if %_w% GEQ 30 goto :timeout
timeout /t 1 /nobreak >nul
goto :wait_ready

:ready
rem  从端口反查真正在监听的 PID 并记下来，供 stop-potato.bat 精确结束。
rem  比在启动时猜测 PID 更准：这里拿到的就是 uvicorn 那个进程。
set "SRV_PID="
for /f "tokens=5" %%x in ('netstat -ano ^| findstr /C:":%PORT% " ^| findstr /C:"LISTENING"') do (
  if not "%%x"=="0" if not defined SRV_PID set "SRV_PID=%%x"
)
if defined SRV_PID > "%PIDFILE%" echo !SRV_PID! %PORT%
if not defined SILENT (
  echo     启动成功！
  echo.
  echo     界面地址 : http://127.0.0.1:%PORT%/
  echo     停止服务 : 双击 stop-potato.bat
  echo     开机自启 : 双击 autostart-on.bat
  echo     日志位置 : logs\potato.log
  echo.
  echo     本窗口可以关闭，服务会继续在后台运行。
  echo.
  start "" "http://127.0.0.1:%PORT%/"
  timeout /t 4 /nobreak >nul
)
endlocal & exit /b 0

:timeout
echo   [错误] 等待 30 秒后服务仍未就绪。请查看日志：
echo          %LOGDIR%\potato.log
echo.
pause
endlocal & exit /b 1

rem ============================================================================
rem  子程序：判断服务是否已经在跑
rem  依据是 PID 文件里的进程仍然存在，且它记录的端口确实处于监听状态。
rem  两个条件都要满足，否则视为陈旧记录并忽略。
rem ============================================================================
:detect_running
set "RUNNING_PORT="
if not exist "%PIDFILE%" exit /b 0
set "OLD_PID="
set "OLD_PORT="
for /f "usebackq tokens=1,2" %%a in ("%PIDFILE%") do (
  set "OLD_PID=%%a"
  set "OLD_PORT=%%b"
)
if not defined OLD_PID exit /b 0
if not defined OLD_PORT exit /b 0
tasklist /FI "PID eq %OLD_PID%" 2>nul | findstr /C:"%OLD_PID%" >nul
if errorlevel 1 exit /b 0
netstat -ano | findstr /C:":%OLD_PORT% " | findstr /C:"LISTENING" >nul
if errorlevel 1 exit /b 0
set "RUNNING_PORT=%OLD_PORT%"
exit /b 0
