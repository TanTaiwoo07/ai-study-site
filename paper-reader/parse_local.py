#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地 PDF 解析：输出与 m0_fetch_parse.py 完全一致的 paper.json。

为什么单独一个脚本：arXiv 路径是纯标准库，PDF 路径要 PyMuPDF。
塞在一起会让「只读 arXiv」也被迫装依赖。两条路各走各的，产物格式统一。

用法：
    python parse_local.py "D:/papers/moead.pdf"
    python parse_local.py paper.pdf --out out
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

# 复用 M0 已经验证过的切分、质检、阅读计划逻辑
from m0_fetch_parse import (
    build_segments,
    build_reading_plan,
    print_report,
    run_quality_check,
)


def main():
    parser = argparse.ArgumentParser(description="解析本地 PDF 论文")
    parser.add_argument("pdf", help="PDF 文件路径")
    parser.add_argument("--out", default="out", help="输出目录（默认 out/）")
    args = parser.parse_args()

    started = time.time()
    pdf_path = Path(args.pdf)

    if not pdf_path.is_file():
        print("找不到文件：%s" % pdf_path, file=sys.stderr)
        return 2

    print("[1/4] 读取 PDF")
    print("  %s（%.1f MB）" % (pdf_path.name, pdf_path.stat().st_size / 1024 / 1024))

    try:
        from pdf_parse import parse_pdf
    except ImportError:
        print("缺少依赖。安装：python -m pip install pymupdf", file=sys.stderr)
        return 2

    try:
        print("[2/4] 提取文本与结构")
        meta, pdf_sections = parse_pdf(pdf_path)
    except RuntimeError as exc:
        print("\n  失败：%s" % exc, file=sys.stderr)
        return 1

    # 转成 M0 的 sections 结构。段落用空行连接，
    # build_segments 会再切一次，正好还原段落边界。
    sections = [
        {
            "level": "section",
            "title": item["title"],
            "content": "\n\n".join(item["paragraphs"]),
            "appendix": bool(re.search(r"appendix|supplement", item["title"], re.I)),
        }
        for item in pdf_sections
        if item["paragraphs"]
    ]

    if not sections:
        print("\n  失败：没有切出任何段落，可能是扫描件或排版异常", file=sys.stderr)
        return 1

    print("[3/4] 切分论证单元")
    segments = build_segments(sections)
    if not segments:
        print("\n  失败：切分后没有有效段落", file=sys.stderr)
        return 1

    outline = [
        {
            "level": section["level"],
            "title": section["title"],
            "segments": sum(1 for s in segments if s["section"] == section["title"]),
            "appendix": section.get("appendix", False),
        }
        for section in sections
    ]

    slug = re.sub(r"[^\w\-]+", "_", pdf_path.stem)[:60] or "local"
    paper = {
        "id": "pdf-" + slug,
        "sourceType": "pdf",
        # PDF 里没有 LaTeX 的 label/引用体系，悬空引用检查自动通过
        "meta": meta,
        "map": {"problem": "", "claims": [], "methodSteps": [],
                "experiments": "", "conclusions": [], "limitations": []},
        "outline": outline,
        "segments": segments,
    }

    quality = run_quality_check(paper, set(), set())
    paper["readingPlan"] = build_reading_plan(outline, segments)
    paper["parseQuality"] = {
        "passed": quality["passed"],
        "checks": quality["checks"],
        "stats": quality["stats"],
        "danglingRefs": quality["danglingRefs"],
        "sourceNote": "PDF 解析（非 LaTeX 源码）。公式与表格的还原度低于 arXiv 路径。",
    }

    print("[4/4] 输出")
    out_dir = Path(args.out) / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "paper.json"
    json_path.write_text(
        json.dumps(paper, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print_report(paper, quality)
    print("  JSON：%s" % json_path)
    print("  耗时：%.1f 秒" % (time.time() - started))
    print()
    print("  注意：PDF 路径没有 LaTeX 的公式与表格结构，")
    print("        讲方法细节时不如 arXiv 源码准。能用 arXiv 就别用这条。")

    return 0 if quality["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
