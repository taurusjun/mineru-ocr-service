#!/usr/bin/env python3
"""PDF → markdown（含插图），供 exam-go 的「文件校正」导入。

和旧的 pdf_to_markdown.py 的区别：**它会抠图**。

为什么需要抠图：考卷里的力学图 / 电路图 / 坐标图 / 公式，硬 OCR 只会得到
`AM`、`DN 7:`、`Nan 3` 这种垃圾，而这些图往往就是题目本身。考卷模型本来就支持图片，
缺的是把图抠出来、并**插到正确的位置**。

做四件事：
  1. 每页取图（**优先用 PDF 内嵌的原图**，没有才按 dpi 渲染 —— 重新渲染等于重采样，白丢像素）
  2. RapidOCR 识别文字（**带坐标**，坐标是后面判断"哪块是图"的关键）
  3. 找插图：版面模型 + 「有墨迹无文字」启发式，两者互补（见 find_figures.py 的说明）
     - 抠出来存到 --image-dir
     - **落在图里的文字行会被丢掉** —— 那些就是 `AM`/`Nan 3` 这类垃圾
  4. 把 `![](url/fig_N.png)` 插到**图上方最近的那一行之后**（人工可在编辑器里挪）

用法：
    .venv-paddle/bin/python scripts/ingest_pdf.py 试卷.pdf \
        --image-dir static/images/ingest/xxx \
        --image-url /static/images/ingest/xxx \
        -o /tmp/out.md
"""

import argparse
import html
import json
import os
import re
import shutil
import sys

import numpy as np
from PIL import Image


# ---------------------------------------------------------------- 取图
def page_image(doc, index, dpi):
    """返回该页的图片路径（临时文件）和来源说明。

    **优先用内嵌的原图**：扫描件本来就是一张图，重新渲染只是重采样，白丢像素
    （实测物理卷内嵌图只有 212~237 dpi，渲染成 400dpi 并没有新信息）。
    """
    page = doc[index]
    imgs = page.get_images(full=True)
    if len(imgs) == 1:
        xref = imgs[0][0]
        pix = page.parent.extract_image(xref)
        if pix and pix.get("image"):
            path = f"/tmp/_page_{index+1}_orig.png"
            with open(path, "wb") as f:
                f.write(pix["image"])
            try:
                im = Image.open(path).convert("RGB")
                if im.width < page.rect.width and im.height < page.rect.height:
                    raise ValueError("内嵌图比页面还小，不像整页扫描")
                im.save(path)
                return path, "内嵌原图"
            except Exception:
                pass
    path = f"/tmp/_page_{index+1}_{dpi}.png"
    page.get_pixmap(dpi=dpi).save(path)
    return path, f"渲染 {dpi}dpi"


# ---------------------------------------------------------------- OCR（带坐标）
def _sort_lines(lines):
    lines.sort(key=lambda l: (round(l["y1"] / 12), l["x1"]))
    return lines


def ocr_rapidocr(img_path):
    """RapidOCR（PP-OCR 模型）：**中文强**，但遇到宽空格（填空线）会把一句话切成几段。"""
    from rapidocr_onnxruntime import RapidOCR

    global _ocr
    try:
        _ocr
    except NameError:
        _ocr = RapidOCR()
    res, _ = _ocr(img_path)
    lines = []
    for item in res or []:
        box, text = item[0], item[1]
        score = float(item[2]) if len(item) > 2 else 1.0
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        lines.append(
            {"text": text, "score": score,
             "x1": min(xs), "y1": min(ys), "x2": max(xs), "y2": max(ys)}
        )
    return _sort_lines(lines)


def ocr_tesseract(img_path, lang="eng", psm=6):
    """tesseract（TSV 输出，带坐标）：**英文散文强**（宽空格不会切断句子），中文弱。"""
    import subprocess

    tess = os.environ.get("TESSERACT") or shutil.which("tesseract") or \
        next((c for c in ("/usr/local/bin/tesseract", "/opt/homebrew/bin/tesseract")
              if os.path.isfile(c)), None)
    if not tess:
        return []
    # ⚠️ 不要用 text=True：tesseract 的 **stderr** 里可能含非 UTF-8 字节
    # （实测英语卷会直接 UnicodeDecodeError 崩掉），自己解码更稳。
    r = subprocess.run([tess, img_path, "stdout", "-l", lang, "--psm", str(psm), "tsv"],
                       capture_output=True)
    if r.returncode != 0:
        return []
    out = r.stdout.decode("utf-8", errors="replace")
    rows = [l.split("\t") for l in out.splitlines()]
    if not rows:
        return []
    header = rows[0]
    idx = {name: i for i, name in enumerate(header)}
    groups = {}
    for row in rows[1:]:
        if len(row) < len(header):
            continue
        text = row[idx["text"]].strip()
        if not text or text == "-1":
            continue
        key = (row[idx["block_num"]], row[idx["par_num"]], row[idx["line_num"]])
        g = groups.setdefault(key, {"texts": [], "x1": 1e9, "y1": 1e9, "x2": 0, "y2": 0})
        g["texts"].append(text)
        x, y, w, h = (int(row[idx[k]]) for k in ("left", "top", "width", "height"))
        g["x1"], g["y1"] = min(g["x1"], x), min(g["y1"], y)
        g["x2"], g["y2"] = max(g["x2"], x + w), max(g["y2"], y + h)
    lines = [{"text": " ".join(g["texts"]), "x1": g["x1"], "y1": g["y1"],
              "x2": g["x2"], "y2": g["y2"]} for g in groups.values()]
    return _sort_lines(lines)


def _join_pieces(a, b, gap, char_w):
    """拼接同一行上的两个碎片。

    **填空线不能直接吞掉** —— 它是内容（学生要填的地方）。
    间隙明显大于一个字宽时，还原成填空标记；否则按中英文习惯补空格。
    """
    if gap >= char_w * 2.5:
        return a + " " + "_" * 6 + " " + b
    if not a or not b:
        return a + b
    la, fb = a[-1], b[0]
    need_space = la.isascii() or fb.isascii()
    return a + (" " if need_space else "") + b


def merge_same_row(lines, page_width, max_gap_ratio=0.15):
    """把**同一视觉行**上的碎片并成一行。

    为什么需要：填空线是宽空格，OCR 会把它当成行边界，于是一句话被切成两行 ——
    英语卷实测：`2. $24 million of` / `was caused by a disaster in Hawaii in 1960.`。

    两个约束：
      - 竖直方向必须明显重叠（才算同一行）
      - 水平间隙不超过页宽的 15%（**两栏版面的栏缝**也在这个量级，
        所以这条对双栏页不保险 —— 双栏需要版面分栏，是另一件事）
    """
    if not lines:
        return lines
    ordered = sorted(lines, key=lambda l: (l["y1"], l["x1"]))
    out = []
    for line in ordered:
        h = max(line["y2"] - line["y1"], 1)
        char_w = max(h * 0.5, 6)          # 粗略的"一个字宽"
        merged = False
        for g in out:
            gh = max(g["y2"] - g["y1"], 1)
            # ⚠️ 不能用「竖直重叠比例」判断同一行：只要有一个框特别矮（坏检测很常见），
            # 阈值就趋近于 0，于是**什么都被算成同一行** —— 实测把物理卷的
            # ①②③④ 四行、甚至选项行都并成了一条，预标注从 40 小题掉到 11 个。
            # 改成比较**中心**：同一行的中心必然很接近。
            if abs((g["y1"] + g["y2"]) / 2 - (line["y1"] + line["y2"]) / 2) > 0.5 * max(gh, h):
                continue
            gap = line["x1"] - g["x2"]
            if not (0 <= gap <= page_width * max_gap_ratio):
                continue
            g["text"] = _join_pieces(g["text"], line["text"], gap, char_w)
            g["x2"] = max(g["x2"], line["x2"])
            g["y1"], g["y2"] = min(g["y1"], line["y1"]), max(g["y2"], line["y2"])
            merged = True
            break
        if not merged:
            out.append(dict(line))
    return sorted(out, key=lambda l: (l["y1"], l["x1"]))


# 一行里中文中夹着连续 3 个以上拉丁字母 → 极可能是串码
# （物理卷实测：`CA RET (HOO 分钟…)`、`—\ UEFA (32 分)` 都是这个形态）
re_garbled = re.compile(r"[\u4e00-\u9fff].*[A-Za-z]{3,}|[A-Za-z]{3,}.*[\u4e00-\u9fff]")


def content_features(lines, specials):
    """按**内容特征**判断该不该换 MinerU。

    为什么不是「质量低于阈值」：实测 fast 的**准确率并不低**（物理卷 CER 1.4%，
    和 MinerU 打平），差的是**结构能力** —— 公式、表格、选项分行。
    所以判据应该是「卷子里有没有 fast 干不了的东西」，不是「识别得准不准」。
    """
    text = "".join(l["text"] for l in lines)
    # 公式特征：数学符号密度
    math_chars = sum(1 for ch in text if ch in "√∑∫∏≈≠≤≥±×÷·°²³₁₂₃₄/")
    math_ratio = math_chars / max(len(text), 1)

    # ⚠️ 版面模型的 equation 框**有误报**（实测英语卷把「Name ____」报成 equation），
    # 所以要求**至少 2 块**才算「有公式」。
    # 另外 math_ratio 在这份样本上是 0 —— 公式符号在 fast 的输出里根本认不出来
    # （都被碾成中文和数字），所以它只能当辅助信号。
    # **这两个阈值是拿 2 份卷子调的，样本太少，换卷子可能要重调。**
    has_equation = specials.count("equation") >= 2 or math_ratio > 0.004
    has_table = "table" in specials
    # 选项没拆干净：某一行里还挤着 3 个以上选项标记
    packed = sum(
        1 for l in lines
        if len(re.findall(r"(?:^|\s)[A-F]\s*[.．、,，]\s*", l["text"])) >= 3
    )
    return {
        "has_equation": has_equation,
        "has_table": has_table,
        "equation_blocks": specials.count("equation"),
        "table_blocks": specials.count("table"),
        "math_ratio": round(math_ratio, 4),
        "packed_option_lines": packed,
        # fast 干不了公式/表格 → 建议换 MinerU；选项没拆干净也建议换
        "recommend": "mineru" if (has_equation or has_table or packed > 0) else "fast",
    }


def quality_report(lines, page_w):
    """给这次识别打个分 —— **不需要标准答案**，只用三个自证信号。

    用途：fast 管线跑完先看分，低于阈值就自动切 MinerU 重跑
    （MinerU 慢但结构好，不该每份都跑）。
    """
    if not lines:
        return {"score": 0.0, "reason": "没有识别出任何文字", "lines": 0}

    scores = [l.get("score", 1.0) for l in lines]
    conf_mean = sum(scores) / len(scores)
    conf_low = sum(1 for x in scores if x < 0.7) / len(scores)

    garbled = sum(1 for l in lines if re_garbled.search(l["text"])) / len(lines)

    structural = sum(
        1 for l in lines
        if re.match(r"^\s*(\d+\s*[.．、]|[A-F]\s*[.．、]|[IVX]+\s*[.．、]|[①-⑳])", l["text"])
    ) / len(lines)

    # 加权：置信度最重要，串码次之，结构产出率作参考
    score = 0.5 * conf_mean + 0.3 * (1 - garbled) + 0.2 * min(structural / 0.25, 1.0)
    return {
        "score": round(score, 3),
        "conf_mean": round(conf_mean, 3),
        "conf_low_ratio": round(conf_low, 3),
        "garbled_ratio": round(garbled, 3),
        "structural_ratio": round(structural, 3),
        "lines": len(lines),
    }


def cjk_ratio(lines):
    text = "".join(l["text"] for l in lines)
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return cjk / len(text)


def ocr_with_boxes(img_path, engine="auto"):
    """选引擎。

    **两个引擎各有所长，不能一刀切**（实测）：
      - 中文：RapidOCR 明显强（物理卷 CER 1.4% vs tesseract 12.3%，圈码 ①②③④ 只有它能认）
      - 英文散文：tesseract 明显强（英语卷里 RapidOCR 会把「1. ____ can happen when…」
        按宽空格切成两行；tesseract 整句一行）
    所以 auto = 先看这一页的中文占比，中文多就用 RapidOCR，否则用 tesseract。
    """
    if engine == "rapidocr":
        return ocr_rapidocr(img_path)
    if engine == "tesseract":
        return ocr_tesseract(img_path)

    rapid = ocr_rapidocr(img_path)
    if cjk_ratio(rapid) >= 0.15:      # 中文页
        return rapid
    tess = ocr_tesseract(img_path)     # 英文页
    return tess or rapid


# ---------------------------------------------------------------- 找图
def detect_figures(img_path, specials=None):
    """版面模型 + 「有墨迹无文字」，两者互补。

    specials 非空时，会把版面模型识别出的 equation / table 数量记进去
    （给「该不该换 MinerU」的内容门禁用）。
    """
    global SPECIALS
    SPECIALS = specials if specials is not None else []
    from scipy import ndimage

    img = np.array(Image.open(img_path).convert("L"))
    H, W = img.shape
    regions = []

    # 方法 1：版面分析模型
    try:
        from rapid_layout import RapidLayout

        out = RapidLayout()(img_path)
        for (x1, y1, x2, y2), name, score in zip(out.boxes, out.class_names, out.scores):
            # 只要 figure / table：
            #  - **equation 不要** —— 公式改由 LaTeX 识别处理（见 --latex），
            #    而且实测版面模型把「Name ____」这种填空线也报成 equation（假阳性）
            #  - 尺寸过滤对版面模型同样要生效（之前漏了，导致小框混进来）
            # equation / table 不当作图（公式改由 LaTeX 或 MinerU 处理），
            # 但要**计数** —— 它们是「该不该换 MinerU」的关键信号：
            # fast 管线对公式和表格没有能力，而 MinerU 有专门模型。
            if name in ("equation", "table") and score >= 0.5:
                SPECIALS.append(name)
                continue
            if name != "figure" or score < 0.5:
                continue
            w, h = x2 - x1, y2 - y1
            if w < 120 or h < 90:
                continue
            if (x2 - x1) * (y2 - y1) > H * W * 0.4:
                continue
            if y1 < H * 0.08 or y2 > H * 0.97:
                continue
            regions.append({"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                            "label": name, "by": "layout"})
    except Exception as e:
        print(f"   （版面分析不可用：{type(e).__name__}）", file=sys.stderr)

    # 方法 2：有墨迹、没有文字
    from rapidocr_onnxruntime import RapidOCR

    ink = img < 160
    text_mask = np.zeros_like(ink)
    res, _ = RapidOCR()(img_path)
    PAD = 8
    for item in res or []:
        xs = [p[0] for p in item[0]]
        ys = [p[1] for p in item[0]]
        text_mask[max(0, int(min(ys)) - PAD):min(H, int(max(ys)) + PAD),
                  max(0, int(min(xs)) - PAD):min(W, int(max(xs)) + PAD)] = True

    cand = ink & ~text_mask
    lab, _ = ndimage.label(ndimage.binary_dilation(cand, iterations=30))
    for i, sl in enumerate(ndimage.find_objects(lab), 1):
        if sl is None:
            continue
        y0, y1, x0, x1 = sl[0].start, sl[0].stop, sl[1].start, sl[1].stop
        h, w = y1 - y0, x1 - x0
        if w < 120 or h < 90:
            continue
        if h / w > 5 or w / h > 10:          # 长条：下划线/分隔线
            continue
        if y0 < H * 0.08 or y1 > H * 0.97:   # 页眉页脚（填空线也是"有墨迹无文字"）
            continue

        # 排除「填空下划线」：它们也是"有墨迹无文字"，但**墨迹只集中在很少的几行**上。
        # 一张真插图是二维结构，墨迹会散布在很多行；一串下划线只有薄薄几行。
        # （英语卷就是这么被骗的：填空句里的下划线膨胀后连成一块。）
        sub = cand[y0:y1, x0:x1]
        ink_rows = int((sub.sum(axis=1) > 0).sum())
        row_spread = ink_rows / max(h, 1)
        if row_spread < 0.15:
            continue
        regions.append({"x1": x0, "y1": y0, "x2": x1, "y2": y1,
                        "label": "figure", "by": "ink"})

    # ⚠️ 版面模型有时会把**整页**框成一个 figure（实测英语卷：一页 50% 的面积），
    # 这种框一旦用来「丢掉落在图里的文字」，就会连正文一起丢掉 ——
    # 实测把「Across」那组线索整段吞了。所以超大的框一律不要。
    MAX_FIG_AREA = H * W * 0.4
    regions = [r for r in regions
               if (r["x2"] - r["x1"]) * (r["y2"] - r["y1"]) <= MAX_FIG_AREA]

    # 合并去重（有重叠优先留版面模型的，类别更可靠）
    def iou(a, b):
        ix = max(0, min(a["x2"], b["x2"]) - max(a["x1"], b["x1"]))
        iy = max(0, min(a["y2"], b["y2"]) - max(a["y1"], b["y1"]))
        if ix <= 0 or iy <= 0:
            return 0.0
        inter = ix * iy
        ua = ((a["x2"] - a["x1"]) * (a["y2"] - a["y1"])
              + (b["x2"] - b["x1"]) * (b["y2"] - b["y1"]) - inter)
        return inter / ua if ua else 0.0

    merged = []
    for r in sorted(regions, key=lambda r: r["by"] != "layout"):
        if any(iou(r, m) >= 0.3 for m in merged):
            continue
        merged.append(r)

    # **收紧外框**：在框内取「非文字墨迹」的实际外接框。
    # 版面模型给的框往往偏大，会把紧邻的正文/选项行一起框进来 ——
    # 实测物理卷第 5 题：透镜图实际到 y≈1820，模型框到 1926，
    # 于是 y≈1882-1922 的**选项行**落在"图内"，被当成图里的标注处理掉了。
    tightened = []
    for r in merged:
        y0, y1 = max(0, int(r["y1"])), min(H, int(r["y2"]))
        x0, x1 = max(0, int(r["x1"])), min(W, int(r["x2"]))
        sub = cand[y0:y1, x0:x1]
        ys, xs = np.nonzero(sub)
        if len(ys) == 0:
            continue
        r = dict(r)
        r["x1"], r["x2"] = x0 + int(xs.min()), x0 + int(xs.max()) + 1
        r["y1"], r["y2"] = y0 + int(ys.min()), y0 + int(ys.max()) + 1
        if r["x2"] - r["x1"] < 120 or r["y2"] - r["y1"] < 60:
            continue
        tightened.append(r)

    tightened.sort(key=lambda r: (r["y1"], r["x1"]))
    return tightened


def inside(line, fig, slack=4):
    """文字行是否落在图区域内（那样它就是图里的标注，是垃圾，要丢掉）。"""
    cx = (line["x1"] + line["x2"]) / 2
    cy = (line["y1"] + line["y2"]) / 2
    return (fig["x1"] - slack <= cx <= fig["x2"] + slack
            and fig["y1"] - slack <= cy <= fig["y2"] + slack)


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="PDF → markdown（含插图）")
    ap.add_argument("pdf")
    ap.add_argument("-o", "--out", required=True, help="输出的 markdown")
    ap.add_argument("--image-dir", help="抠出的图存哪（如 static/images/ingest/xxx）")
    ap.add_argument("--image-url", help="图在 markdown 里引用的 URL 前缀")
    ap.add_argument("--pages", help="只处理这些页，如 1 或 1-3")
    ap.add_argument("--dpi", type=int, default=300, help="需要渲染时的分辨率")
    ap.add_argument("--no-figures", action="store_true", help="只出文字，不抠图")
    ap.add_argument("--engine", default="auto", choices=["auto", "rapidocr", "tesseract"],
                    help="OCR 引擎，默认 auto（按页面语种自动选）")
    ap.add_argument("--title", default="", help="浏览页的标题")
    ap.add_argument("--quality-out", help="把质量评分写成 JSON（后端据此决定要不要换 MinerU）")
    args = ap.parse_args()

    import pymupdf

    pages = None
    if args.pages:
        a, _, b = args.pages.partition("-")
        pages = (int(a), int(b or a))

    doc = pymupdf.open(args.pdf)
    out_lines = []
    gallery = []
    q_reports = []

    for i in range(doc.page_count):
        no = i + 1
        if pages and not (pages[0] <= no <= pages[1]):
            continue
        img_path, how = page_image(doc, i, args.dpi)
        lines = ocr_with_boxes(img_path, args.engine)
        page_w = Image.open(img_path).width
        specials = []
        if not args.no_figures:
            detect_figures(img_path, specials)   # 只为了统计 equation/table
        q = quality_report(lines, page_w)
        q["content"] = content_features(lines, specials)
        q_reports.append(q)
        page_w = Image.open(img_path).width
        before = len(lines)
        lines = merge_same_row(lines, page_w)
        if len(lines) != before:
            print(f"     同视觉行合并：{before} → {len(lines)} 行", file=sys.stderr)
        figs = [] if args.no_figures else detect_figures(img_path)
        eng = args.engine
        if eng == "auto":
            eng = "rapidocr" if cjk_ratio(lines) >= 0.15 else "tesseract"
        print(f"   第 {no} 页：{how}，引擎 {eng}（中文占比 {cjk_ratio(lines):.0%}），"
              f"文字 {len(lines)} 行，插图 {len(figs)} 个", file=sys.stderr)

        im = Image.open(img_path)
        for k, fig in enumerate(figs):
            if args.image_dir:
                os.makedirs(args.image_dir, exist_ok=True)
                name = f"p{no}_fig{k}.png"
                im.crop((int(fig["x1"]), int(fig["y1"]),
                         int(fig["x2"]), int(fig["y2"]))).save(
                    os.path.join(args.image_dir, name))
                fig["file"] = name
                gallery.append({"page": no, "file": name, "label": fig["label"],
                                "by": fig["by"], "w": int(fig["x2"] - fig["x1"]),
                                "h": int(fig["y2"] - fig["y1"])})

        # 丢掉落在图里的文字行（那些是图里的标注，OCR 出来就是垃圾）。
        # ⚠️ 但**结构化行（题号/选项/圈码）绝不能丢** —— 实测物理卷第 5 题的选项
        # 就排在插图那一行附近，`D.` 被误当成"图里的标注"丢掉了。
        # 图里的标注长这样：`④`、`F₁`、`v`，不带题号/选项号。
        def is_structural(text):
            return bool(re.match(r"^\s*(\d+\s*[.．、]|[A-F]\s*[.．、]|[IVX]+\s*[.．、]|[①-⑳])", text))

        kept = [l for l in lines
                if is_structural(l["text"]) or not any(inside(l, f) for f in figs)]

        # 每张图挂到「图的上边界所覆盖的那一行」之后
        attach = {}
        for k, fig in enumerate(figs):
            best, best_dy = None, 1e9
            for idx, l in enumerate(kept):
                # 行的下边界在图的上边界之上 → 候选
                dy = fig["y1"] - l["y2"]
                if dy >= -20 and dy < best_dy:
                    best_dy, best = dy, idx
            attach.setdefault(best if best is not None else -1, []).append(k)

        # **标准 markdown**：不做 exam-go 那种「每行后补空行」的填充 ——
        # 那是 exam-go 的标注约定，由它的后端自己补（BuildContent）。
        # 服务对外只承诺「PDF → markdown + 图片」。
        for idx, l in enumerate(kept):
            out_lines.append(l["text"])
            for k in attach.get(idx, []):
                if args.image_url and figs[k].get("file"):
                    out_lines.append(f"![]({args.image_url}/{figs[k]['file']})")
        for k in attach.get(-1, []):   # 找不到归属的图放页尾
            if args.image_url and figs[k].get("file"):
                out_lines.append(f"![]({args.image_url}/{figs[k]['file']})")

    with open(args.out, "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines).rstrip("\n") + "\n")
    print(f"   已写 {args.out}（{len(out_lines)} 行）", file=sys.stderr)

    if args.quality_out and q_reports:
        worst = min(q["score"] for q in q_reports)
        avg = sum(q["score"] for q in q_reports) / len(q_reports)
        # 任意一页建议换 MinerU，整份就建议换
        rec = "mineru" if any(q["content"]["recommend"] == "mineru" for q in q_reports) else "fast"
        json.dump({"pages": q_reports, "score_min": round(worst, 3),
                   "score_avg": round(avg, 3), "engine": args.engine,
                   "recommend": rec},
                  open(args.quality_out, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        c = q_reports[0]["content"]
        print(f"   质量评分：最差 {worst:.3f} / 平均 {avg:.3f}；"
              f"公式 {c['equation_blocks']} 块 / 表格 {c['table_blocks']} 块 / "
              f"没拆干净的选项行 {c['packed_option_lines']} → **建议 {rec}**",
              file=sys.stderr)

    # 浏览页：让人能一眼核对抠出来的图对不对
    if args.image_dir and gallery:
        rows = "\n".join(
            f'<figure><img src="{html.escape(g["file"])}" loading="lazy">'
            f'<figcaption>第 {g["page"]} 页 · {g["label"]} · {g["w"]}×{g["h"]} · 来源 {g["by"]}'
            f'<br>{html.escape(g["file"])}</figcaption></figure>'
            for g in gallery
        )
        open(os.path.join(args.image_dir, "index.html"), "w", encoding="utf-8").write(
            f"""<!doctype html><meta charset="utf-8">
<title>{html.escape(args.title or '抠图核对')}</title>
<style>
 body{{font-family:-apple-system,sans-serif;margin:24px;background:#fafafa}}
 h1{{font-size:18px}} p{{color:#666;font-size:13px}}
 .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}}
 figure{{margin:0;background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:10px}}
 img{{max-width:100%;display:block;border:1px solid #eee}}
 figcaption{{font-size:12px;color:#666;margin-top:6px;line-height:1.5}}
</style>
<h1>{html.escape(args.title or '抠图核对')}</h1>
<p>共 {len(gallery)} 张。看每张图**裁得对不对、有没有缺边**；有问题告诉我，我调参数。</p>
<div class="grid">{rows}</div>
""")
        print(f"   浏览页 {os.path.join(args.image_dir, 'index.html')}", file=sys.stderr)


if __name__ == "__main__":
    main()
