#!/usr/bin/env bash
# Potato Test —— 一键部署 / 启动（Linux, macOS, WSL, Git Bash）
#
#   ./scripts/deploy.sh              # 装好就启动，端口 18080
#   PORT=9000 ./scripts/deploy.sh    # 换端口
#   ./scripts/deploy.sh --with-feishu   # 额外装飞书 SDK 传输（默认不需要）
#
# 这个脚本是幂等的：重复执行只会补齐缺失的部分，不会删掉你已有的 .env 或数据库。
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PORT="${PORT:-18081}"
EXTRAS=""
for arg in "$@"; do
  case "$arg" in
    --with-feishu) EXTRAS="[feishu]" ;;
    -h|--help)
      sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[31m[错误] %s\033[0m\n' "$*" >&2; exit 1; }

say "Potato Test 一键部署"
echo "  项目目录: $ROOT"

# ---------------------------------------------------------------- 1. 找 Python
PY="${PYTHON:-}"
if [ -z "$PY" ]; then
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && \
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  done
fi
[ -n "$PY" ] || die "没找到 Python 3.11+。装一个：https://www.python.org/downloads/"
say "[1/5] Python: $("$PY" -c 'import sys; print(sys.version.split()[0], sys.executable)')"

# ------------------------------------------------------------ 2. 虚拟环境
if [ ! -x ".venv/bin/python" ]; then
  say "[2/5] 创建虚拟环境 .venv"
  "$PY" -m venv .venv
else
  say "[2/5] 复用已有虚拟环境 .venv"
fi
VENV_PY="$ROOT/.venv/bin/python"

# ---------------------------------------------------------------- 3. 依赖
say "[3/5] 安装依赖（首次需要几分钟）"
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install -e ".${EXTRAS}" \
  || die "依赖安装失败 —— 把上面的输出发给维护者。"

# ------------------------------------------------------ 4. 浏览器与前端
say "[4/5] 安装 Chromium（Playwright 用；用例执行必需）"
"$VENV_PY" -m playwright install chromium \
  || echo "  [提示] Chromium 下载失败，用例会起不来浏览器；网络恢复后重跑本脚本即可。"

# 前端：有 Node 就重新构建（保证界面与代码一致），没有就用仓库自带的 web/dist。
if [ -f "web/dist/index.html" ] && ! command -v node >/dev/null 2>&1; then
  echo "  前端：使用仓库自带的 web/dist（本机没有 Node，跳过构建）"
elif command -v node >/dev/null 2>&1; then
  echo "  前端：检测到 Node，重新构建 web/dist"
  ( cd web && { command -v pnpm >/dev/null 2>&1 && pnpm install --silent && pnpm build; } \
      || { npm install --silent && npm run build; } ) \
    || echo "  [提示] 前端构建失败，继续使用仓库自带的 web/dist（界面可能略旧）。"
else
  echo "  [提示] 既没有 web/dist 也没有 Node —— 界面不可用，只有 API。"
fi

# ---------------------------------------------------------------- 5. 配置
say "[5/5] 准备配置文件"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "  已从 .env.example 生成 .env"
else
  echo "  .env 已存在，保持不动"
fi
# 与 Windows 版共用同一个实现：见 scripts/gen_secret_key.py 的注释。
# 它自己判断"已有非空密钥就不动"（重新生成会让库里已存的凭据全部解不开）。
"$VENV_PY" scripts/gen_secret_key.py || echo "  [提示] 密钥生成失败，保存测试凭据时会报错。"

if ! grep -qE '^GATEWAY_API_KEY=.+' .env; then
  echo
  echo "  ------------------------------------------------------------------"
  echo "  注意：.env 里的 GATEWAY_API_KEY 还是空的，测试执行和 AI 判定都会失败。"
  echo "  填好之后再跑本脚本启动。要用的网关地址/模型同样在 .env 里："
  echo "      GATEWAY_BASE_URL / GATEWAY_API_KEY / GATEWAY_MODEL / AGENT_MODEL"
  echo "  ------------------------------------------------------------------"
fi

say "启动服务： http://127.0.0.1:${PORT}"
echo "  日志： tail -f logs/potato.log"
echo "  停止： Ctrl+C"
echo
exec "$VENV_PY" run_server.py "$PORT"
