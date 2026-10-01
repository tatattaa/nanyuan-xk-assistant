#!/usr/bin/env bash
# 南苑抢课助手 · 一键启动（Linux / macOS / Git Bash）
set -e
cd "$(dirname "$0")"

PY="${PY:-python3}"
if [ -x ".venv/Scripts/python.exe" ]; then PY=".venv/Scripts/python.exe"; fi
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; fi

echo "============================================================"
echo "  南苑抢课助手 · 本地 Web 界面"
echo "============================================================"

if ! "$PY" -c "import fastapi, uvicorn, requests" 2>/dev/null; then
  echo "[提示] 正在安装依赖..."
  "$PY" -m pip install -r requirements.txt
fi

PORT="${PORT:-8720}"

echo "[启动] http://127.0.0.1:${PORT}"
# --real：强制打真实教务，顺手清掉可能残留的 XK_SCHOOL_URL
# （想用本地模拟教务请改用：python serve.py --mock）
# （想手机访问可加 --lan，见 serve.py --help）
exec "$PY" serve.py --real --port "$PORT"
