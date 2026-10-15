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

# ![Image block](doc:2621aa0/tier:standard/page:1/block:7)
IMG_REF = re.compile(r"!\[[^\]]*\]\((doc:[^)]+)\)")


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

    # ⚠️ standard/advanced 档需要**本地 parse-server**（VLM）。没起会直接报
    # 「本地 mineru 服务未运行」—— 实测踩过：服务被关掉后 mineru 引擎整个失败。
    # 所以这里自动把它拉起来再重试一次（幂等：已在跑时 start 不会重复起）。
    if code != 0 and "服务未运行" in err:
        print("   本地 mineru 服务没在跑，自动启动…", file=sys.stderr)
        run([mineru, "server", "start"], timeout=600)
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

    # 转成「一行一个标注单元」
    out_lines = []
    for line in raw.split("\n"):
        line = line.rstrip()
        if line.startswith("<!-- page"):     # MinerU 的页码注释，对标注没用
            continue
        if not line.strip():
            continue
        out_lines.append(line)
        out_lines.append("")
    open(args.out, "w", encoding="utf-8").write("\n".join(out_lines).rstrip("\n") + "\n")
    print(f"   已写 {args.out}（{len([l for l in out_lines if l.strip()])} 行）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
