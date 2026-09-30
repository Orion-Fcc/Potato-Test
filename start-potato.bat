@echo off
rem ============================================================================
rem  Potato Test - native Windows launcher (no Docker, no Postgres, no Redis)
rem
rem  First run  : creates .venv, installs backend deps, initialises the SQLite DB.
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

set "PORT=18000"
if not "%~1"=="" set "PORT=%~1"

echo.
echo   ============================================
echo      Potato Test  -  native launcher
echo   ============================================
echo.

rem ---- 1. Python -------------------------------------------------------------
if not exist "%PY%" goto :make_venv
goto :have_python

:make_venv
echo   [1/4] 正在创建 Python 虚拟环境 .venv ...
"%PY%" --version >nul 2>&1
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

:have_python
echo   [1/4] Python 就绪。

rem ---- 2. Backend dependencies ----------------------------------------------
"%PY%" -c "import fastapi, sqlalchemy, aiosqlite, uvicorn, sse_starlette" >nul 2>&1
if errorlevel 1 goto :install_deps
echo   [2/4] 后端依赖已安装。
goto :check_env

:install_deps
echo   [2/4] 正在安装后端依赖（首次约 2-5 分钟）...
echo.
rem  Split into small batches on purpose: a dropped connection mid-batch rolls the
rem  whole batch back, and one giant install would silently leave you with nothing.
"%PIP%" install --disable-pip-version-check --upgrade pip
for %%P in (
  "fastapi uvicorn[standard]"
  "sqlalchemy[asyncio] aiosqlite pydantic-settings"
  "httpx openai sse-starlette python-multipart"
  "cryptography bcrypt pyjwt openpyxl"
  "playwright"
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
echo         依赖安装完成。

rem ---- 3. .env --------------------------------------------------------------
:check_env
if not exist "%~dp0.env" (
  if exist "%~dp0.env.example" (
    echo   [3/4] 未找到 .env，正在从 .env.example 复制...
    copy /y "%~dp0.env.example" "%~dp0.env" >nul
  ) else (
    echo   [3/4] 未找到 .env（将使用内置默认值）。
    goto :check_web
  )
) else (
  echo   [3/4] .env 已存在。
)

:check_web
rem ---- 4. Frontend -----------------------------------------------------------
if exist "%~dp0web\dist\index.html" (
  echo   [4/4] 前端已构建。
  goto :run
)
echo   [4/4] 未发现前端构建产物 web\dist。
echo.
echo         前端需要先构建一次。请选择：
echo           1. 现在构建（需要 Node.js，约 1-3 分钟）
echo           2. 跳过 —— 只启动 API（浏览器访问 /api/... 可用，看不到界面）
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
echo     提示：本项目默认免登录（AUTH_ENABLED=false）。
echo           LLM 配置在 界面 - 系统设置 里改，改完立即生效。
echo.

start "" "http://127.0.0.1:%PORT%/"

"%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%

echo.
echo   服务已停止。
pause
