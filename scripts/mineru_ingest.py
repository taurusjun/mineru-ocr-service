#!/usr/bin/env python3
"""用 MinerU（standard 档）把 PDF 转成 exam-go 的 markdown。

为什么用它当主线：**结构**明显更好 ——
  - 每个选项天然一行（我们的标注是逐行的，这是关键；flash 档会把整道题压成一段）
  - 标题层级（# / ##）
  - 公式、表格有专门模型
代价：本地 CPU 跑 VLM，**约 5~6 分钟/页**。要快就用 scripts/ingest_pdf.py（秒级）。

做三件事：
  1. 调 `mineru parse --tier standard`
  2. 把 markdown 里的 `![Image block](doc:…/block:N)` 逐块用 `mineru read -f image`
     导出成真图片，改写成 `![](image-url/fig_N.png)`
  3. 输出「每行后补空行」的 markdown —— 前端是 content.split("\\n") 逐行标注的

用法：
    .venv-ingest/bin/python scripts/mineru_ingest.py 试卷.pdf \
        --image-dir static/images/ingest/xxx --image-url /static/images/ingest/xxx -o out.md
"""

import argparse
import os
import re
import subprocess
import sys
import time

# ![Image block](doc:2621aa0/tier:standard/page:1/block:7)
IMG_REF = re.compile(r"!\[[^\]]*\]\((doc:[^)]+)\)")


def ensure_mineru_ready(mineru: str, tier: str) -> None:
    """把 MinerU 的本地服务准备到「能跑指定档位」的状态（幂等）。

    顺序是实测出来的 —— 换机器最容易在这三步上踩：
      1. **清陈旧锁 / endpoint**：目录被搬过、或上次进程被杀，会留下
         `doclib.lock` / `doclib.sock` / `doclib.endpoint.json`，
         报错分别是「home is currently owned by another doclib server process」
         和「endpoint belongs to a different MinerU server instance」。
      2. **server start**：standard/advanced 靠本地 VLM parse-server；没起会报
         「本地 mineru 服务未运行」。
      3. **config set parse_server.local.mode managed**：默认是 disabled，
         会报「Local parse-server is disabled」。**必须在 server start 之后** ——
         服务没起时这条命令本身就会失败。
    """
    home = os.environ.get("MINERU_HOME", "")
    if home:
        for stale in ("doclib.lock", "doclib.sock", "doclib.endpoint.json"):
            p = os.path.join(home, stale)
            if os.path.exists(p):
                try:
                    os.remove(p)
                    print(f"   清掉陈旧文件 {stale}", file=sys.stderr)
                except OSError:
                    pass

    print("   启动本地 mineru 服务…", file=sys.stderr)
    run([mineru, "server", "start"], timeout=900)
    time.sleep(15)

    print("   把 parse-server 设为 managed…", file=sys.stderr)
    run([mineru, "config", "set", "parse_server.local.mode", "managed"], timeout=120)


def run(cmd, timeout=None):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    return r.returncode, r.stdout.decode("utf-8", errors="replace"), r.stderr.decode("utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser(description="MinerU → exam-go markdown")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", required=True)
    ap.add_argument("--image-dir", help="导出的图放哪")
    ap.add_argument("--image-url", help="markdown 里引用的 URL 前缀")
    ap.add_argument("--pages", help="如 1 或 1-3")
    ap.add_argument("--tier", default="standard", choices=["flash", "basic", "standard", "advanced"])
    ap.add_argument("--mineru", default=None, help="mineru 可执行文件路径")
    ap.add_argument("--timeout", type=int, default=5400, help="parse 最长等待秒数")
    args = ap.parse_args()

    mineru = args.mineru or os.environ.get("MINERU_BIN") or "mineru"

    # ⚠️ MINERU_HOME 必须指对，否则 MinerU 会去默认的 ~/.mineru：
    # 那个目录如果不存在，它会**新建一个空的**，配置是默认的 parse_server.local.mode=disabled，
    # 于是 standard 档直接报「Local parse-server is disabled」—— 实测踩过。
    # 默认按脚本位置推：scripts/../mineru-home
    if not os.environ.get("MINERU_HOME"):
        guess = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "mineru-home"))
        if os.path.isdir(guess):
            os.environ["MINERU_HOME"] = guess
            print(f"   MINERU_HOME 未设置，按脚本位置推断：{guess}", file=sys.stderr)

    tmp_md = args.out + ".mineru.raw"
    cmd = [mineru, "parse", args.pdf, "--tier", args.tier, "--output", tmp_md,
           "--wait", str(args.timeout), "--force"]
    if args.pages:
        cmd += ["--pages", args.pages]
    print(f"   调 mineru parse（{args.tier} 档，每页几分钟，请等）…", file=sys.stderr)
    code, out, err = run(cmd, timeout=args.timeout + 300)

    # 撞到「服务没准备好」就按顺序修一遍再重试（每一步都幂等）。
    # 这几步的顺序是实测出来的，换机器最容易踩：
    #   1) 陈旧锁/endpoint —— 目录被搬过、或上次进程被杀，会留下指向旧服务的记录
    #   2) server start    —— standard/advanced 档靠本地 VLM parse-server
    #   3) config set managed —— **必须在 server start 之后**（之前会报「服务未运行」）
    if code != 0 and any(k in err for k in ("服务未运行", "is disabled", "different MinerU server")):
        ensure_mineru_ready(mineru, args.tier)
        code, out, err = run(cmd, timeout=args.timeout + 300)

    if code != 0 or not os.path.exists(tmp_md):
        print(f"   ❌ mineru parse 失败：{err.strip()[-400:]}", file=sys.stderr)
        return 1

    raw = open(tmp_md, encoding="utf-8").read()
    os.remove(tmp_md)

    # 导出图片
    n_img = 0
    if args.image_dir and args.image_url:
        os.makedirs(args.image_dir, exist_ok=True)
        for locator in dict.fromkeys(IMG_REF.findall(raw)):   # 去重、保序
            name = f"fig_{n_img}.png"
            dest = os.path.join(args.image_dir, name)
            code, _, err = run([mineru, "read", locator, "--format", "image", "--output", dest], timeout=120)
            if code == 0 and os.path.exists(dest):
                raw = raw.replace(f"]({locator})", f"]({args.image_url}/{name})")
                n_img += 1
            else:
                print(f"   ⚠️ 导出 {locator} 失败：{err.strip()[-160:]}", file=sys.stderr)
    print(f"   导出图片 {n_img} 张", file=sys.stderr)

    # **标准 markdown**：只去掉 MinerU 的页码注释与多余空行，
    # 不做 exam-go 那种行填充（那是它的后端的事）。
    out_lines = [l.rstrip() for l in raw.split("\n") if not l.startswith("<!-- page")]
    while out_lines and not out_lines[-1].strip():
        out_lines.pop()
    open(args.out, "w", encoding="utf-8").write("\n".join(out_lines).rstrip("\n") + "\n")
    print(f"   已写 {args.out}（{len([l for l in out_lines if l.strip()])} 行）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
