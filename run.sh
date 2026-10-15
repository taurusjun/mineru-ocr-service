#!/usr/bin/env bash
# 启动 OCR 服务（独立进程/目录/环境）。
#
# 模型与缓存都放在本目录下，不碰 ~：
#   MINERU_HOME=~/PG/ocr-service/mineru-home   （MinerU 的模型/配置/文档库）
#   HF_HOME=~/PG/ocr-service/hf-cache          （HuggingFace 缓存）
set -euo pipefail
cd "$(dirname "$0")"

export MINERU_HOME="${MINERU_HOME:-$PWD/mineru-home}"
export HF_HOME="${HF_HOME:-$PWD/hf-cache}"
export OCR_SERVICE_PORT="${OCR_SERVICE_PORT:-8100}"
export MINERU_TIER="${MINERU_TIER:-standard}"

PY="$PWD/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

echo "MINERU_HOME=$MINERU_HOME"
echo "监听 127.0.0.1:$OCR_SERVICE_PORT"
exec "$PY" server.py
