#!/usr/bin/env bash
# 一键启动（macOS / Linux）
# 首次运行会自动建虚拟环境、装依赖、生成示例数据；之后直接起服务。
set -euo pipefail
cd "$(dirname "$0")"

PY="${PYTHON:-python3}"

if [ ! -d ".venv" ]; then
  echo "[INFO] 创建虚拟环境 .venv"
  "$PY" -m venv .venv
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q -r requirements.txt
fi

if [ ! -f "data/ride_hailing.db" ]; then
  echo "[INFO] 未发现数据库，生成示例数据（约 30 秒）..."
  ./.venv/bin/python scripts/generate_data.py
fi

echo "[INFO] Web UI : http://127.0.0.1:8001/"
echo "[INFO] API 文档: http://127.0.0.1:8001/apiview  (离线，不依赖外网 CDN)"
exec ./.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
