#!/usr/bin/env python3
"""exam-go 的 OCR / 版面服务（独立进程、独立目录、独立环境）。

为什么单独成一个服务，而不是让后端直接 exec 脚本：
  - 依赖极重（MinerU 模型约 4G + 一个本地 VLM 服务），不该跟后端绑在一起
  - 可以放到**另一台机器**（甚至 GPU 机），后端只走 HTTP
  - 引擎可换（fast / mineru / 以后的云 OCR），**接口不变**

契约
----
  POST /v1/parse        multipart: file=<PDF>[, engine=fast|mineru|auto]
    → {
        "markdown": "…（图片写成 ![](images/fig_0.png)，由本服务提供）",
        "images":   [{"name":"fig_0.png","page":1,"b64":"<base64>"}],
        "meta":     {"engine":"mineru","tier":"standard","elapsed":338,"recommend":"fast"}
      }
  GET  /v1/images/<name>    取图（markdown 里的相对路径就走这里）
  GET  /healthz             存活 + 引擎可用性

**图用 base64 回传**（而不是共享文件系统）：这样服务可以放在另一台机器上，
后端收到后自己落盘到 static/images/ingest/<uuid>/。

用法：
    MINERU_HOME=~/PG/ocr-service/models ./run.sh          # 起服务（默认 :8100）
"""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

HERE = Path(__file__).resolve().parent
VENV = HERE / ".venv"
IMG_DIR = Path(os.environ.get("OCR_SERVICE_IMAGES", "/tmp/ocr-service-images"))

app = FastAPI(title="exam-go OCR service")


def _python() -> str:
    p = VENV / "bin" / "python"
    return str(p) if p.exists() else sys.executable


def _mineru() -> str:
    p = VENV / "bin" / "mineru"
    return str(p) if p.exists() else "mineru"


def _run(cmd, timeout=None):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8", errors="replace"), r.stderr.decode("utf-8", errors="replace")


@app.get("/healthz")
def healthz():
    ok, out, _ = _run([_mineru(), "--version"])
    return {"ok": True, "mineru": out.strip() if ok else None, "mineru_home": os.environ.get("MINERU_HOME")}


@app.get("/v1/images/{name}")
def get_image(name: str):
    # 防目录穿越
    safe = os.path.basename(name)
    p = IMG_DIR / safe
    if not p.exists():
        raise HTTPException(404, "no such image")
    return FileResponse(str(p))


@app.post("/v1/parse")
async def parse(file: UploadFile = File(...), engine: str = Form("auto")):
    started = time.time()
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        pdf = Path(td) / "in.pdf"
        pdf.write_bytes(await file.read())
        out = Path(td) / "out.md"
        imgdir = Path(td) / "images"
        imgdir.mkdir()

        # 引擎选择：auto = 先 fast，再按内容门禁的建议决定要不要换 mineru
        chosen = "fast" if engine == "auto" else engine
        code, _, err = _run_parse(pdf, out, imgdir, chosen)
        if code != 0:
            raise HTTPException(500, f"parse 失败：{err[-400:]}")
        recommend = _recommend(out)
        if engine == "auto" and recommend == "mineru":
            code2, _, err2 = _run_parse(pdf, out, imgdir, "mineru")
            if code2 == 0:
                chosen = "mineru"
            else:
                chosen = "fast(门禁建议 mineru 但重跑失败)"

        md = out.read_text(encoding="utf-8")
        images = []
        for p in sorted(imgdir.glob("*.png")):
            (IMG_DIR / p.name).write_bytes(p.read_bytes())
            images.append({
                "name": p.name,
                "page": _page_of(p.name),
                "b64": base64.b64encode(p.read_bytes()).decode(),
            })
        # markdown 里的图片路径改成相对本服务的 URL
        md = re.sub(r"!\[\]\([^)]*/([^/)]+\.png)\)", r"![](images/\1)", md)

    return JSONResponse({
        "markdown": md,
        "images": images,
        "meta": {"engine": chosen, "tier": os.environ.get("MINERU_TIER", "standard"),
                 "recommend": recommend, "elapsed": round(time.time() - started, 1)},
    })


def _page_of(name: str) -> int:
    m = re.search(r"p(\d+)_", name)
    return int(m.group(1)) if m else 0


def _run_parse(pdf: Path, out: Path, imgdir: Path, engine: str):
    script = HERE / "scripts" / ("mineru_ingest.py" if engine == "mineru" else "ingest_pdf.py")
    cmd = [_python(), str(script), str(pdf), "-o", str(out),
           "--image-dir", str(imgdir), "--image-url", "images"]
    if engine == "mineru":
        cmd += ["--mineru", _mineru()]
        if tier := os.environ.get("MINERU_TIER"):
            cmd += ["--tier", tier]
    return _run(cmd, timeout=int(os.environ.get("OCR_SERVICE_TIMEOUT", "7200")))


def _recommend(out: Path) -> str:
    q = Path(str(out) + ".quality.json")
    if q.exists():
        try:
            return json.loads(q.read_text()).get("recommend", "")
        except Exception:
            pass
    return ""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=os.environ.get("OCR_SERVICE_HOST", "127.0.0.1"),
                port=int(os.environ.get("OCR_SERVICE_PORT", "8100")))
