#!/usr/bin/env bash
# 为「PDF → markdown」准备 Python 环境（只需要跑一次）。
#
# 为什么要固化一个 venv：导入接口是 Go 调的，如果每次请求都 `uv run --with pymupdf`，
# 那每来一个文件都要去查/装包，第一次还得联网。建一次 venv 更稳、更快。
#
# 用法：./scripts/setup_ingest.sh
set -euo pipefail
cd "$(dirname "$0")/.."

VENV=".venv-ingest"

if [ ! -x "$VENV/bin/python" ]; then
  echo "== 建虚拟环境 $VENV"
  if command -v uv >/dev/null 2>&1; then
    uv venv "$VENV"
  else
    python3 -m venv "$VENV"
  fi
fi

echo "== 安装 PyMuPDF / numpy / pillow（图像预处理用）"
# 官方源在这台机器上时通时断（实测 TLS handshake eof），探测一下，不通就换清华镜像。
# 可用 PIP_INDEX_URL 显式指定。
INDEX="${PIP_INDEX_URL:-}"
if [ -z "$INDEX" ]; then
  if ! curl -s -o /dev/null -m 8 -w '%{http_code}' https://pypi.org/simple/ 2>/dev/null | grep -q 200; then
    INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
    echo "   pypi.org 不通，改用镜像 $INDEX"
  fi
fi
# pymupdf 取图；numpy/pillow/scipy 做图像处理与连通域；
# rapidocr-onnxruntime 是 PP-OCR 模型（中文强）；rapid-layout 是版面分析（找插图/公式区）
PKGS=(pymupdf numpy pillow scipy rapidocr-onnxruntime rapid-layout)
ARGS=()
[ -n "$INDEX" ] && ARGS+=(--index-url "$INDEX")
if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$VENV/bin/python" ${ARGS[@]+"${ARGS[@]}"} "${PKGS[@]}"
else
  "$VENV/bin/pip" install ${ARGS[@]+"${ARGS[@]}"} "${PKGS[@]}"
fi

echo "== 自检"
"$VENV/bin/python" -c "import pymupdf, numpy, PIL; print('   pymupdf', pymupdf.__version__, '| numpy', numpy.__version__, '| pillow', PIL.__version__, 'OK')"
if command -v tesseract >/dev/null 2>&1; then
  echo "   $(tesseract --version 2>&1 | head -1) OK"
  tesseract --list-langs 2>/dev/null | grep -qE '^(chi_sim|eng)$' \
    && echo "   中文/英文语言包 OK" \
    || echo "   ⚠️ 缺 chi_sim 或 eng 语言包，扫描页 OCR 会失败"
else
  echo "   ⚠️ 没找到 tesseract —— 只有带文本层的 PDF 能转，扫描页会失败"
fi
echo
echo "完成。后端会自动使用 $VENV/bin/python（可用 INGEST_PYTHON 覆盖）。"
