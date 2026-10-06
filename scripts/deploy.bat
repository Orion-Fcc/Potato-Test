@echo off
rem ============================================================================
rem  Potato Test  Windows 一键部署
rem
rem  双击本文件即可：建虚拟环境 -> 装依赖 -> 装 Chromium -> 生成 .env -> 启动服务
rem  幂等：重复运行只会补齐缺失的部分，不会删除已有的 .env 或数据库。
rem
rem  编码必须是 GBK，且不写 chcp（见 docs/deployment.md）。
rem ============================================================================
setlocal enabledelayedexpansion
title Potato Test 一键部署

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
cd /d "%ROOT%"

set "PORT=18081"
set "VENV=%ROOT%\.venv"
set "PYV=%VENV%\Scripts\python.exe"

echo.
echo   ==========================================================
echo    Potato Test 一键部署
echo    目录: %ROOT%
echo   ==========================================================

if not exist "%ROOT%\run_server.py" (
  echo.
  echo   [错误] %ROOT% 下没有 run_server.py。
  echo          本脚本必须放在项目的 scripts\ 目录里。
  pause
  exit /b 1
)

rem ---------------------------------------------------------------- 1. 找 Python
set "PYCMD="
py -3 -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
if not errorlevel 1 set "PYCMD=py -3"
if not defined PYCMD (
  python -c "import sys;sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>&1
  if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
  echo.
  echo   [错误] 没找到 Python 3.11 或更新版本。
  echo          请到 https://www.python.org/downloads/ 安装，
  echo          安装时务必勾选 "Add python.exe to PATH"，然后重跑本脚本。
  pause
  exit /b 1
)
echo.
echo   [1/5] 检查 Python
%PYCMD% -c "import sys;print('        版本', sys.version.split()[0])"
%PYCMD% -c "import sys;print('        路径', sys.executable)"

rem ------------------------------------------------------------ 2. 虚拟环境
echo.
if exist "%PYV%" (
  echo   [2/5] 复用已有虚拟环境 .venv
) else (
  echo   [2/5] 创建虚拟环境 .venv
  %PYCMD% -m venv "%VENV%"
  if not exist "%PYV%" (
    echo   [错误] 创建虚拟环境失败，请把上面的报错发给维护者。
    pause
    exit /b 1
  )
)

rem ---------------------------------------------------------------- 3. 依赖
echo.
echo   [3/5] 安装依赖（首次需要几分钟，请勿关闭窗口）
"%PYV%" -m pip install --upgrade pip --quiet
"%PYV%" -m pip install -e "%ROOT%"
if errorlevel 1 (
  echo.
  echo   [错误] 依赖安装失败。常见原因：网络不通，或公司代理未配置。
  echo          把上面的报错发给维护者。
  pause
  exit /b 1
)

rem ------------------------------------------------------ 4. 浏览器与前端
echo.
echo   [4/5] 安装 Chromium（Playwright 用，执行用例必需）
"%PYV%" -m playwright install chromium
if errorlevel 1 echo   [提示] Chromium 下载失败，用例会起不来浏览器；网络恢复后重跑本脚本即可。

if exist "%ROOT%\web\dist\index.html" (
  echo         前端：使用仓库自带的 web\dist
) else (
  echo         [提示] 没有 web\dist，界面不可用（只有 API）。
  echo                装上 Node.js 后在 web\ 目录执行 pnpm install ^&^& pnpm build。
)

rem ---------------------------------------------------------------- 5. 配置
echo.
echo   [5/5] 准备配置文件
if not exist "%ROOT%\.env" (
  if exist "%ROOT%\.env.example" (
    copy /y "%ROOT%\.env.example" "%ROOT%\.env" >nul
    echo         已从 .env.example 生成 .env
  ) else (
    echo         [警告] 没有 .env.example，将使用内置默认值（LLM 会指向 api.openai.com）。
  )
) else (
  echo         .env 已存在，保持不动
)

rem 密钥的生成/写入交给 scripts\gen_secret_key.py —— 批处理里的引号转义太容易出错，
rem 而这段逻辑错了不会当场报错（直到第一次保存测试凭据才失败）。脚本自己判断
rem "已有非空密钥就不动"，所以这里直接调用即可。
"%PYV%" "%ROOT%\scripts\gen_secret_key.py"

findstr /r /c:"^GATEWAY_API_KEY=." "%ROOT%\.env" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   ------------------------------------------------------------
  echo    注意：.env 里的 GATEWAY_API_KEY 还是空的。
  echo    测试执行与 AI 判定都需要它，填好后再运行本脚本。
  echo    同组的配置项：GATEWAY_BASE_URL / GATEWAY_MODEL / AGENT_MODEL
  echo   ------------------------------------------------------------
)

echo.
echo   启动服务: http://127.0.0.1:%PORT%
echo   日志文件: %ROOT%\logs\potato.log
echo   停止服务: 在本窗口按 Ctrl+C，或直接关闭窗口
echo.
"%PYV%" "%ROOT%\run_server.py" %PORT%

pause
