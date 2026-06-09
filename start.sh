#!/usr/bin/env bash
# ============================================================================
# 慧研 · 一键启动脚本
# 用法: bash start.sh [--frontend-only|--backend-only]
# ============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

FRONTEND_ONLY=false
BACKEND_ONLY=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --frontend-only) FRONTEND_ONLY=true ;;
    --backend-only)  BACKEND_ONLY=true ;;
    -h|--help)
      echo "用法: bash start.sh [选项]"
      echo "  --frontend-only  仅启动前端 :3000"
      echo "  --backend-only   仅启动后端 :8888"
      exit 0
      ;;
    *) echo "未知选项: $1"; exit 1 ;;
  esac
  shift
done

GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m'
step() { echo -e "${BLUE}[*]${NC} $1"; }
ok()   { echo -e "${GREEN}[✓]${NC} $1"; }
fail() { echo -e "${RED}[✗]${NC} $1"; }
missing() {
  echo ""
  echo -e "${RED}============================================${NC}"
  echo -e "${RED}  缺少运行环境${NC}"
  echo -e "${RED}============================================${NC}"
  echo ""
  echo "  请根据你的系统安装对应环境："
  echo ""
  echo "  Node.js 18+:  https://nodejs.org/"
  echo "  Python  3.10+: https://www.python.org/downloads/"
  echo ""
  echo "  安装后重新运行 bash start.sh"
  echo ""
  exit 1
}

echo ""
echo "============================================"
echo " 慧研 · 一键启动"
echo "============================================"
echo ""

# ============================================================================
# 1. Node.js 检测
# ============================================================================
if ! $BACKEND_ONLY; then
  step "检测 Node.js..."
  if command -v node &>/dev/null && node -e "process.exit(parseInt(process.version.slice(1))>=20?0:1)" 2>/dev/null; then
    ok "Node.js $(node --version)"
  elif command -v node &>/dev/null; then
    fail "Node.js $(node --version) 版本过低，需要 20.9+"
    missing
  else
    fail "未检测到 Node.js"
    missing
  fi

  # pnpm (通过 corepack 启用)
  if ! command -v pnpm &>/dev/null; then
    step "启用 pnpm..."
    corepack enable 2>/dev/null || true
    corepack prepare pnpm@9 --activate 2>/dev/null || {
      npm install -g pnpm@9 2>/dev/null || {
        fail "pnpm 启用失败，请确认 Node.js >= 18"
        exit 1
      }
    }
  fi
  ok "pnpm $(pnpm --version)"
fi

# ============================================================================
# 2. Python 检测
# ============================================================================
if ! $FRONTEND_ONLY; then
  step "检测 Python..."
  if command -v python3 &>/dev/null && python3 -c "import sys; sys.exit(0 if sys.version_info>=(3,10) else 1)" 2>/dev/null; then
    ok "Python $(python3 --version 2>&1)"
  elif command -v python3 &>/dev/null; then
    fail "Python $(python3 --version 2>&1) 版本过低，需要 3.10+"
    missing
  else
    fail "未检测到 Python 3"
    missing
  fi

  # venv
  if [ ! -d "$ROOT_DIR/server/.venv" ]; then
    step "创建 Python venv..."
    python3 -m venv "$ROOT_DIR/server/.venv"
    ok "venv 创建完成"
  fi
  source "$ROOT_DIR/server/.venv/bin/activate"

  if python3 -c "import fastapi, uvicorn" 2>/dev/null; then
    ok "Python 依赖已就绪"
  else
    step "安装 Python 依赖..."
    pip install -q -r "$ROOT_DIR/server/requirements.txt"
    ok "Python 依赖安装完成"
  fi
fi

# ============================================================================
# 3. 启动
# ============================================================================
if ! $BACKEND_ONLY; then
  if [ ! -f "$ROOT_DIR/app-dist/.env.local" ]; then
    printf 'NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8888/api\n' > "$ROOT_DIR/app-dist/.env.local"
  fi
fi

cleanup() {
  echo ""
  step "正在停止服务..."
  jobs -p | xargs -r kill 2>/dev/null || true
}
trap cleanup INT TERM EXIT

echo ""
echo "============================================"
echo " 启动服务"
echo "============================================"
echo ""

if $FRONTEND_ONLY; then
  step "启动前端 → http://127.0.0.1:3000"
  HOSTNAME=127.0.0.1 node "$ROOT_DIR/app-dist/server.js" &
elif $BACKEND_ONLY; then
  step "启动后端 → http://127.0.0.1:8888"
  PYTHONPATH="$ROOT_DIR/server" uvicorn app.main:app --host 127.0.0.1 --port 8888 &
else
  step "启动后端 → http://127.0.0.1:8888"
  PYTHONPATH="$ROOT_DIR/server" uvicorn app.main:app --host 127.0.0.1 --port 8888 &
  sleep 2
  step "启动前端 → http://127.0.0.1:3000"
  HOSTNAME=127.0.0.1 node "$ROOT_DIR/app-dist/server.js" &
fi

echo ""
echo "============================================"
if ! $BACKEND_ONLY; then echo " 前端 : http://127.0.0.1:3000"; fi
if ! $FRONTEND_ONLY; then
  echo " 后端 : http://127.0.0.1:8888"
  echo " 文档 : http://127.0.0.1:8888/docs"
fi
echo " 按 Ctrl+C 停止"
echo "============================================"
wait
