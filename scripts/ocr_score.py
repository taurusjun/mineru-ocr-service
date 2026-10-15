#!/usr/bin/env python3
"""给 OCR 结果打分：与**人工基准**逐行比，算字符错误率（CER）。

为什么需要它：判断"哪个引擎更好"不该靠感觉。人工基准由**看得懂原文的人**提供
（这份卷子只有用户知道正确答案），脚本负责算数。

用法：
  python scripts/ocr_score.py --gt 基准.txt --hyp 引擎输出.txt --label tesseract
  python scripts/ocr_score.py --gt 基准.txt --hyp-a a.txt --hyp-b b.txt   # 两个一起比

它做的事：
  1. 两边都去掉空行、去掉标点/空白（标点本来就是 OCR 的重灾区，先不混进分数）
  2. 按顺序逐行算 CER（difflib 的字符级相似度）
  3. 打印总 CER、以及最差的那几行 —— 那才是真正要修的
"""

import argparse
import difflib
import re
import sys

# 归一化时去掉的字符：空白 + 中英标点。括号也去掉，因为 OCR 常把（）和()搞混
_PUNCT = re.compile(r"[\s，。、；：？！“”‘’（）()\[\]【】《》〈〉,.!?;:\"'`~·…—\-_/\\|]+")


def norm(s: str) -> str:
    return _PUNCT.sub("", s)


def load(path: str):
    with open(path, encoding="utf-8") as f:
        return [l.strip() for l in f if l.strip()]


def score(gt_lines, hyp_lines, limit=0):
    """整段比对。

    ⚠️ 不能逐行比：OCR 的换行位置和人工基准几乎不可能一致（一个题干可能被拆成两行），
    逐行比出来的分数全是"对齐误差"，没有意义（第一版就踩了这个）。
    所以把两边归一化后**拼成一整段**，算字符级差异，并列出差异最集中的几段。
    """
    g = norm("".join(gt_lines))
    h = norm("".join(hyp_lines))
    # 基准通常只覆盖一页的一部分，而引擎会输出整页 —— 不截断的话，多出来的内容全被算成错误。
    # limit>0 时只比两边的前 limit 个字符（默认取基准长度），保证两边比的是同一段。
    if limit > 0:
        g, h = g[:limit], h[:limit]
    elif limit < 0:
        pass
    if not g:
        return 0.0, [], 0, 0
    sm = difflib.SequenceMatcher(None, g, h)
    cer = 1.0 - sm.ratio()

    # 找出差异最集中的片段（replace 块），按长度排序
    diffs = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        if tag == "replace" or tag == "delete":
            diffs.append((max(i2 - i1, j2 - j1), g[i1:i2], h[j1:j2]))
    diffs.sort(reverse=True)
    return cer, diffs, len(g), len(h)


def report(label, gt_lines, hyp_lines, worst=6, limit=0):
    cer, diffs, ng, nh = score(gt_lines, hyp_lines, limit)
    scope = f"只比前 {limit} 字" if limit > 0 else "全文比"
    print(f"   {label}: 总 CER {cer*100:.1f}%   （{scope}；基准 {len(norm(''.join(gt_lines)))} 字 / 输出 {len(norm(''.join(hyp_lines)))} 字）")
    if diffs:
        print(f"      差异最大的 {min(worst, len(diffs))} 段：")
        for _, g, h in diffs[:worst]:
            print(f"        基准: {g[:44]!r}")
            print(f"        输出: {h[:44]!r}")
    return cer


def main():
    ap = argparse.ArgumentParser(description="OCR 结果打分（对照人工基准）")
    ap.add_argument("--gt", required=True, help="人工基准文件")
    ap.add_argument("--hyp", help="待打分的输出")
    ap.add_argument("--hyp-a", help="对比 A")
    ap.add_argument("--hyp-b", help="对比 B")
    ap.add_argument("--label", default="输出")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    ap.add_argument(
        "--limit", type=int, default=-1,
        help="只比前 N 个字（-1=按基准长度自动，0=全文比）",
    )
    args = ap.parse_args()

    gt = load(args.gt)
    if not gt:
        sys.exit("基准文件是空的")

    def lim(hyp):
        if args.limit >= 0:
            return args.limit
        return len(norm("".join(gt)))  # 默认按基准长度截断

    if args.hyp:
        report(args.label, gt, load(args.hyp), limit=lim(None))
    if args.hyp_a and args.hyp_b:
        ca = report(args.label_a, gt, load(args.hyp_a), limit=lim(None))
        cb = report(args.label_b, gt, load(args.hyp_b), limit=lim(None))
        if ca < cb:
            print(f"   → {args.label_a} 更好（CER 低 {(cb-ca)*100:.1f} 个百分点）")
        elif cb < ca:
            print(f"   → {args.label_b} 更好（CER 低 {(ca-cb)*100:.1f} 个百分点）")
        else:
            print("   → 打平")


if __name__ == "__main__":
    main()
