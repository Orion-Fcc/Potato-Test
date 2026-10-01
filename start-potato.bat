@echo off
rem ============================================================================
rem  Potato Test - native Windows launcher (no Docker, no Postgres, no Redis)
rem
rem  PORT: defaults to 18080 and AUTO-AVOIDS a busy port by scanning upward, so a
rem  stale deployment holding 18000 can never block startup again. Override with an
rem  argument:  start-potato.bat 19000
rem
rem  First run  : creates .venv, installs deps, downloads Chromium, inits the SQLite DB.
rem  Later runs : starts instantly.
rem
rem  Encoding note: this file is saved as GBK and deliberately does NOT call
rem  `chcp`. The Chinese text below renders correctly on a zh-CN console as-is;
rem  adding chcp 65001 here actually breaks it.
rem ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

title Potato Test
color 0B

set "VENV=%~dp0.venv"
set "PY=%VENV%\Scripts\python.exe"
set "PIP=%VENV%\Scripts\pip.exe"

rem 18000 is the stock value and is usually taken by an older Docker deployment of
rem this same app. The process holding it is Docker Desktop's port proxy
rem (com.docker.backend.exe), NOT a Python process — which is why "who owns :18000"
rem looks unrelated to Potato Test.
set "PORT_START=18080"
set "PORT_MAX=18120"
if not "%~1"=="" set "PORT_START=%~1"

echo.
echo   ============================================
echo      Potato Test  -  native launcher
echo   ============================================
echo.

rem ---- 0. pick a free port ---------------------------------------------------
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
  echo   [提示] 端口 %PORT_START% 已被占用，自动改用 %PORT%。
  echo          占用者通常是旧的 Docker 部署（Docker Desktop 代理进程）。
  echo.
)

rem ---- 1. Python -------------------------------------------------------------
if not exist "%PY%" goto :make_venv
echo   [1/5] Python 就绪。
goto :check_deps

:make_venv
echo   [1/5] 正在创建 Python 虚拟环境 .venv ...
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
echo         完成。

rem ---- 2. Backend dependencies ----------------------------------------------
rem  browser_use AND playwright are checked here on purpose: they are what actually
rem  execute a test case, and the app imports them lazily, so a missing one does not stop
rem  the server from starting — it only explodes the moment you click "运行". Checking only
rem  the web framework is exactly how you ship a build that boots but cannot run a case.
:check_deps
"%PY%" -c "import fastapi, sqlalchemy, aiosqlite, uvicorn, sse_starlette, playwright, browser_use" >nul 2>&1
if errorlevel 1 goto :install_deps
echo   [2/5] 后端依赖已安装。
goto :check_browser

:install_deps
echo   [2/5] 正在安装后端依赖（首次约 5-10 分钟，browser-use 依赖树较大）...
echo.
rem  Split into small batches on purpose: a dropped connection mid-batch rolls the
rem  whole batch back, and one giant install would silently leave you with nothing.
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
  echo          若在代理环境，请确认 HTTP_PROXY / HTTPS_PROXY 已设置。
  echo.
  pause
  exit /b 1
)
echo         依赖安装完成。

rem ---- 3. Chromium ----------------------------------------------------------
rem  Installing the playwright package does NOT download a browser. Without this step
rem  every run fails with "Executable doesn't exist".
:check_browser
if exist "%USERPROFILE%\AppData\Local\ms-playwright" (
  echo   [3/5] Chromium 已就绪。
  goto :check_env
)
echo   [3/5] 正在下载 Chromium（约 150MB，用于执行测试用例）...
"%PY%" -m playwright install chromium
if errorlevel 1 (
  echo   [警告] Chromium 下载失败。服务能启动，但跑用例时会报找不到浏览器。
  echo          联网后手动重试： .venv\Scripts\python -m playwright install chromium
)
echo         完成。

rem ---- 4. .env --------------------------------------------------------------
:check_env
if not exist "%~dp0.env" (
  if exist "%~dp0.env.example" (
    echo   [4/5] 未找到 .env，正在从 .env.example 复制...
    copy /y "%~dp0.env.example" "%~dp0.env" >nul
  ) else (
    echo   [4/5] 未找到 .env，将使用内置默认值。
  )
) else (
  echo   [4/5] .env 已存在。
)

rem ---- 5. Frontend -----------------------------------------------------------
if exist "%~dp0web\dist\index.html" (
  echo   [5/5] 前端已构建。
  goto :run
)
echo   [5/5] 未发现前端构建产物 web\dist。
echo.
echo         前端需要先构建一次。请选择：
echo           1. 现在构建（需要 Node.js，约 1-3 分钟）
echo           2. 跳过 —— 只启动 API（浏览器访问不到界面）
echo.
set "BUILD="
set /p "BUILD=请输入 1 或 2 [默认 1]: "
if "!BUILD!"=="2" goto :run

where npm >nul 2>&1
if errorlevel 1 (
  echo.
  echo   [警告] 找不到 npm，跳过前端构建。请安装 Node.js 18+ 后重试：
  echo          https://nodejs.org/
  echo.
  goto :run
)
echo.
echo         正在安装前端依赖并构建（首次较慢）...
pushd "%~dp0web"
call npm install --no-audit --no-fund
if errorlevel 1 (
  echo   [警告] npm install 失败，跳过前端构建。
  popd
  goto :run
)
call npm run build
popd
if not exist "%~dp0web\dist\index.html" (
  echo   [警告] 前端构建未产出 web\dist\index.html，界面可能不可用。
)

rem ---- run ----------------------------------------------------------------
:run
echo.
echo   ============================================
echo     正在启动 Potato Test ...
echo   ============================================
echo.
echo     界面地址 : http://127.0.0.1:%PORT%/
echo     API 文档 : http://127.0.0.1:%PORT%/docs
echo     停止服务 : 按 Ctrl+C，或直接关闭本窗口
echo.
echo     提示：本项目默认免登录（AUTH_ENABLED=false），单人使用无需账号。
echo           LLM 配置在 界面 - 系统设置 里改，改完立即生效。
echo.

start "" "http://127.0.0.1:%PORT%/"

"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%

echo.
echo   服务已停止。
pause
