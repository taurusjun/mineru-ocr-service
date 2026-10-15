# ocr-service

exam-go 的 **OCR / 版面能力独立服务**。

## 为什么独立

- MinerU 依赖极重（模型约 4G + 一个本地 VLM 服务），不该跟后端绑在一起
- 可以放到**另一台机器**（甚至 GPU 机），后端只走 HTTP
- 引擎可换（`fast` / `mineru` / 以后的云 OCR），**接口不变**

## 目录

```
.venv/            独立 Python 环境（pymupdf / numpy / scipy / rapidocr / rapid-layout / mineru）
mineru-home/      MINERU_HOME —— MinerU 的模型、配置、文档库（约 2G）
hf-cache/         HF_HOME —— HuggingFace 缓存（约 1.9G，公式/表格模型）
scripts/          两个转换脚本（ingest_pdf.py = fast；mineru_ingest.py = mineru）
server.py         HTTP 服务
run.sh            启动
```

## 接口

```
POST /v1/parse   multipart: file=<PDF>[, engine=fast|mineru|auto]
  → {"markdown": …, "images": [{name, page, b64}], "meta": {engine, tier, recommend, elapsed}}
GET  /v1/images/<name>     取图
GET  /healthz              存活 + 引擎可用性
```

**图用 base64 回传**（不依赖共享文件系统）—— 所以服务放哪台机器都行，后端收到后自己落盘。

## 启动

```sh
./run.sh                 # 默认 127.0.0.1:8100
OCR_SERVICE_PORT=9000 ./run.sh
```

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `MINERU_HOME` | `./mineru-home` | MinerU 的模型/配置/文档库 |
| `HF_HOME` | `./hf-cache` | HuggingFace 缓存 |
| `MINERU_TIER` | `standard` | MinerU 档位（本地只支持 flash / standard 等，见 ROADMAP） |
| `OCR_SERVICE_PORT` | `8100` | 监听端口 |
| `OCR_SERVICE_TIMEOUT` | `7200` | 单次转换最长秒数 |
