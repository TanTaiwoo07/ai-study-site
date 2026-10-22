#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PDF 解析：把本地论文 PDF 转成和 arXiv 源码一致的结构。

为什么单独一个模块：arXiv 路径依赖纯标准库，PDF 路径需要 PyMuPDF。
放一起会让「只读 arXiv」也得装依赖。

核心难点是双栏排版：PyMuPDF 按 y 排序会得到「左栏第1行、右栏第1行、左栏第2行…」，
读起来完全乱序。必须按 x 分栏，再各自按 y 排序。
"""

import re
from pathlib import Path

try:
    import pymupdf
except ImportError:
    pymupdf = None


MIN_LINE_CHARS = 4
TITLE_MAX_CHARS = 140

# 章节标题的形态模式。字号规则抓不到 IEEE 这类排版——
# 它用「I. INTRODUCTION」小号大写字母，不加粗，字号和正文几乎一致。
HEADING_PATTERNS = [
    r"^[IVXLC]+\.\s+[A-Z]",                     # I. INTRODUCTION / II. RELATED WORK
    r"^\d+(?:\.\d+)*\.?\s+[A-Z][A-Za-z]",       # 1. Introduction / 3.2 Method
    r"^(?:Abstract|Index Terms|References|Acknowledg\w*|Appendix)\b",
]

# ACL/NAACL 这类排版：章节标题既无编号也不加粗（LaTeX 生成的字体名里没有 Bold），
# 但章节命名高度模式化，用词表兜底命中率很高。
COMMON_SECTION_WORDS = re.compile(
    r"^(?:\d+(?:\.\d+)*\.?\s+)?"
    r"(introduction|related works?|background|preliminar\w+|"
    r"methods?|methodology|approach|model|architecture|"
    r"experiments?|experimental setup|results?|evaluation|"
    r"analysis|ablation studies|discussion|limitations?|"
    r"conclusions?(?: and future work)?|future work|"
    r"references|appendix|acknowledg\w*)$",
    re.I,
)


def _require_pymupdf():
    if pymupdf is None:
        raise RuntimeError(
            "这个功能需要 PyMuPDF。安装：\n"
            "  python -m pip install pymupdf"
        )


def _page_lines(page):
    """
    抽出一页的所有文本行，带版面信息。
    只保留 line 级别——span 级别太碎，block 级别会跨栏。
    """
    lines = []
    data = page.get_text("dict")

    for block in data.get("blocks", []):
        if block.get("type") != 0:      # 0=文本，1=图片
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue

            text = "".join(s.get("text", "") for s in spans).strip()
            if len(text) < MIN_LINE_CHARS:
                continue

            x0, y0, x1, y1 = line["bbox"]
            size = max(s.get("size", 0) for s in spans)
            bold = any("Bold" in s.get("font", "") or "bold" in s.get("font", "")
                       for s in spans)

            lines.append({
                "text": text,
                "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                "size": round(size, 1),
                "bold": bold,
                "page": page.number,
            })

    return lines


def _guess_body_size(lines):
    """正文最常出现的字号就是基准字号"""
    counter = {}
    for line in lines:
        if len(line["text"]) >= 25:
            counter[line["size"]] = counter.get(line["size"], 0) + 1
    if not counter:
        return 10.0
    return max(counter.items(), key=lambda kv: kv[1])[0]


def _is_heading(line, body_size):
    """标题判定：先看形态模式，再看字号/字重"""
    text = line["text"].strip()

    if len(text) > TITLE_MAX_CHARS:
        return False

    # 形态优先：编号标题往往字号不突出，只靠字号会全漏
    for pattern in HEADING_PATTERNS:
        if re.match(pattern, text):
            return True

    # 词表兜底：短行且整行就是一个常见章节名
    if len(text) <= 60 and COMMON_SECTION_WORDS.match(text.strip()):
        return True

    if line["size"] >= body_size + 1.4:
        return True
    if line["bold"] and line["size"] >= body_size + 0.3 and len(text) <= 80:
        return True
    return False


def _order_two_columns(lines, page_width):
    """
    双栏排序：先左栏（按 y），再右栏（按 y）。
    横跨两栏的行（宽度超过页宽 70%）单独插在中间，通常是小节标题。
    """
    mid = page_width / 2
    wide_threshold = page_width * 0.7

    wide = []
    left = []
    right = []

    for line in lines:
        width = line["x1"] - line["x0"]
        if width >= wide_threshold:
            wide.append(line)
        elif (line["x0"] + line["x1"]) / 2 < mid:
            left.append(line)
        else:
            right.append(line)

    left.sort(key=lambda l: l["y0"])
    right.sort(key=lambda l: l["y0"])
    wide.sort(key=lambda l: l["y0"])

    # 横跨行按其 y 位置插回（简单做法：作为左栏的分隔点）
    if not wide:
        return left + right

    # 把横跨行当作分段标记：y 小于它的归前面
    merged = []
    for item in wide:
        head = [l for l in left + right if l["y0"] < item["y0"]]
        merged.extend(sorted(head, key=lambda l: (l["x0"] > mid, l["y0"])))
        merged.append(item)
        left = [l for l in left if l["y0"] >= item["y0"]]
        right = [l for l in right if l["y0"] >= item["y0"]]

    merged.extend(left)
    merged.extend(right)
    return merged


def _merge_paragraphs(lines, body_size):
    """
    把行合并成段落。

    PDF 里没有空行，只能靠「行尾是否补齐」判断：一行明显短于该栏宽度，
    说明是段落最后一行。这是启发式，但对学术论文够用。
    """
    if not lines:
        return []

    # 用行长中位数判断「满行」
    widths = sorted(l["x1"] - l["x0"] for l in lines if len(l["text"]) > 20)
    typical = widths[len(widths) // 2] if widths else 0

    paragraphs = []
    buffer = ""

    for line in lines:
        text = line["text"]

        # 连字符断词：绝大多数英文论文用 -
        if buffer.endswith("-") and text[:1].islower():
            buffer = buffer[:-1] + text
        else:
            buffer = (buffer + " " + text).strip() if buffer else text

        width = line["x1"] - line["x0"]
        is_short = typical > 0 and width < typical * 0.82
        ends_sentence = text.rstrip().endswith((".", "。", "!", "?", ":"))

        if is_short and ends_sentence and len(buffer) >= 40:
            paragraphs.append(buffer)
            buffer = ""

    if buffer and len(buffer) >= 30:
        paragraphs.append(buffer)

    return paragraphs


def parse_pdf(pdf_path):
    """
    返回 (meta, sections)，结构与 arXiv 路径对齐：
        sections = [{level, title, content, appendix}]
    """
    _require_pymupdf()

    path = Path(pdf_path)
    if not path.exists():
        raise RuntimeError("找不到文件：%s" % path)

    doc = pymupdf.open(str(path))
    if len(doc) == 0:
        raise RuntimeError("PDF 没有页面")

    # 1. 收集全文行
    all_lines = []
    page_widths = []
    for page in doc:
        page_widths.append(page.rect.width)
        all_lines.extend(_page_lines(page))

    if not all_lines:
        raise RuntimeError(
            "没有提取到文本。这多半是扫描件（图片型 PDF），需要 OCR。"
        )

    body_size = _guess_body_size(all_lines)

    # 2. 逐页做双栏排序
    ordered = []
    for page_no in range(len(doc)):
        page_lines = [l for l in all_lines if l["page"] == page_no]
        if not page_lines:
            continue
        ordered.extend(_order_two_columns(page_lines, page_widths[page_no]))

    # 3. 切章节：遇到标题行就开新节
    sections = []
    current_title = "（正文）"
    current_lines = []

    for line in ordered:
        if _is_heading(line, body_size):
            if current_lines:
                sections.append({
                    "level": "section",
                    "title": current_title,
                    "lines": current_lines,
                    "appendix": False,
                })
            current_title = line["text"]
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines:
        sections.append({
            "level": "section",
            "title": current_title,
            "lines": current_lines,
            "appendix": False,
        })

    # 4. 段落合并
    for section in sections:
        section["paragraphs"] = _merge_paragraphs(section.pop("lines"), body_size)

    # 5. 元信息
    first_page_text = doc[0].get_text()
    meta = {
        "title": _guess_title(ordered, body_size, doc),
        "authors": [],
        "abstract": _guess_abstract(sections),
        "arxivId": "",
        "version": "",
        "year": _guess_year(first_page_text),
        "venue": "",
        "peerReviewed": True,   # 本地 PDF 多来自会议/期刊
    }

    doc.close()
    return meta, sections


def _guess_title(ordered, body_size, doc):
    """最大字号的那一行，通常就是标题"""
    candidates = [l for l in ordered[:40] if l["size"] >= body_size + 3]
    if not candidates:
        return (doc.metadata or {}).get("title", "") or ""
    biggest = max(candidates, key=lambda l: l["size"])
    same_size = [l for l in candidates if abs(l["size"] - biggest["size"]) < 0.5]
    same_size.sort(key=lambda l: l["y0"])
    return " ".join(l["text"] for l in same_size[:3]).strip()


def _guess_abstract(sections):
    for section in sections:
        if re.search(r"abstract", section["title"], re.I):
            return " ".join(section.get("paragraphs", [])[:2])[:1200]
    return ""


def _guess_year(text):
    """
    只在首页开头找年份。全页找会被参考文献里的年份带偏
    （实测 MOEA/D 那篇被识别成 2024，实际是 2007）。
    """
    head = text[:600]
    match = re.search(r"\b((?:19|20)\d{2})\b", head)
    return int(match.group(1)) if match else None


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("用法: python pdf_parse.py <file.pdf>")
        sys.exit(2)

    meta, sections = parse_pdf(sys.argv[1])
    print(json.dumps({
        "meta": meta,
        "sectionCount": len(sections),
        "sections": [
            {"title": s["title"], "paragraphs": len(s["paragraphs"]),
             "chars": sum(len(p) for p in s["paragraphs"])}
            for s in sections
        ],
    }, ensure_ascii=False, indent=2))
