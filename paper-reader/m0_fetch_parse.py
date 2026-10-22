#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
M0 · arXiv 取源与解析

职责：输入 arXiv ID，输出 paper.json + 人类可读的质量报告。
只依赖标准库，不需要 pip 安装任何东西。

用法：
    python m0_fetch_parse.py 1512.03385
    python m0_fetch_parse.py https://arxiv.org/abs/1512.03385
    python m0_fetch_parse.py 1512.03385 --keep     保留解压出的原始源码

设计原则（对应 PAPER-READER-SPEC §3.6）：
    1. 源码优先：e-print 拿 LaTeX 原始文件，公式保留为 LaTeX，不经过 PDF
    2. 失败可诊断：每一步失败都给出具体原因，不静默吞掉
    3. 质量可量化：四项指标判定是否达标，不达标就该走降级路径
"""

import argparse
import gzip
import io
import json
import re
import shutil
import statistics
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------- 常量

USER_AGENT = "paper-reader-m0/0.1 (personal research tool)"
EPRINT_URL = "https://arxiv.org/e-print/{arxiv_id}"
ABS_URL = "https://arxiv.org/abs/{arxiv_id}"

TIMEOUT = 60

# 质量自检阈值（SPEC §3.6，按 M0 实测两轮校准）
MIN_SECTIONS = 4
MIN_PARA_LEN = 30
MAX_DANGLING_REFS = 2

# 段落指标用「中位数 + 超长占比」，不用平均值：
# 技术报告（如 DINOv3，16 万字）段落天然偏长，平均值会误判；
# 真正要抓的是「切太碎（乱序）」和「切不动（整段没切开）」两种失败。
MEDIAN_PARA_MIN = 50
MEDIAN_PARA_MAX = 1500
OVERSIZED_PARA = 4000
MAX_OVERSIZED_RATIO = 0.10

# 公式密度不再是硬指标：实验型论文（如 ResNet）本身公式极少，
# 用它判定等于惩罚论文类型而不是解析质量。改为只统计、不判定。

# 常见的作者自定义宏，直接换成英文，避免清理时留下空洞。
# 必须用正则 + 负向断言，否则 \vs 会把 \vspace 误伤成 " versus pace"。
CUSTOM_MACROS = [
    (r"\\etal(?![a-zA-Z])", " et al. "),
    (r"\\ie(?![a-zA-Z])", " i.e., "),
    (r"\\eg(?![a-zA-Z])", " e.g., "),
    (r"\\wrt(?![a-zA-Z])", " with respect to "),
    (r"\\vs(?![a-zA-Z])", " versus "),
    (r"\\aka(?![a-zA-Z])", " also known as "),
]


# ---------------------------------------------------------------- 输入解析

def extract_arxiv_id(raw: str) -> tuple[str, str | None]:
    """
    从各种写法里抽出 (基础 ID, 版本号)。

    支持：
        1512.03385
        arXiv:1512.03385
        1512.03385v2
        https://arxiv.org/abs/1512.03385
        https://arxiv.org/pdf/1512.03385v2.pdf
        hep-th/9901001          （2007 年前的老格式）
    """
    text = raw.strip()

    # 去掉 URL 前缀，只留路径部分
    if "arxiv.org/" in text:
        text = text.split("arxiv.org/", 1)[1]
        text = re.sub(r"^(abs|pdf|e-print)/", "", text)
        text = re.sub(r"\.pdf$", "", text)

    # 去掉 arXiv: 前缀
    text = re.sub(r"^arxiv:\s*", "", text, flags=re.I)

    # 新格式 YYMM.NNNN[N]，可带版本号
    match = re.match(r"^(\d{4}\.\d{4,5})(v\d+)?$", text)
    if match:
        return match.group(1), match.group(2)

    # 老格式 archive/YYMMNNN
    match = re.match(r"^([a-z-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?$", text)
    if match:
        return match.group(1), match.group(2)

    raise ValueError(
        "无法识别的 arXiv ID：%r\n"
        "合法写法示例：1512.03385 / arXiv:1512.03385 / "
        "https://arxiv.org/abs/1512.03385" % raw
    )


# ---------------------------------------------------------------- 下载

def download_eprint(arxiv_id: str, dest: Path, cache_dir: Path | None = None) -> Path:
    """
    下载 e-print 压缩包。arXiv 会 302 到实际存储位置，urllib 自动跟随。
    有缓存就直接用——同一篇论文反复调试时能省掉几分钟等待。
    """
    if cache_dir is not None:
        cached = cache_dir / ("%s.tar.gz" % arxiv_id.replace("/", "_"))
        if cached.exists() and cached.stat().st_size > 1024:
            print("  使用缓存 %s（%.1f KB）"
                  % (cached.name, cached.stat().st_size / 1024))
            shutil.copy(cached, dest)
            return dest

    url = EPRINT_URL.format(arxiv_id=arxiv_id)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})

    print("  下载 %s" % url)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = response.read()
            content_type = response.headers.get("Content-Type", "")
            declared = response.headers.get("Content-Length")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError("arXiv 上找不到 %s，检查 ID 是否正确" % arxiv_id)
        raise RuntimeError("下载失败：HTTP %s" % exc.code)
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "网络不通：%s\n"
            "如果是国内网络，先确认能直接打开 https://arxiv.org" % exc.reason
        )

    if len(payload) < 200:
        raise RuntimeError("下载内容异常（仅 %d 字节），可能不是有效源码" % len(payload))

    # 完整性校验：截断的压缩包会在解压时才报错，早发现早重试
    if declared and declared.isdigit() and int(declared) != len(payload):
        raise RuntimeError(
            "下载不完整：应得 %s 字节，实得 %d 字节。请重试"
            % (declared, len(payload))
        )

    dest.write_bytes(payload)
    print("  已下载 %.1f KB（%s）" % (len(payload) / 1024, content_type or "未知类型"))

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy(dest, cache_dir / ("%s.tar.gz" % arxiv_id.replace("/", "_")))

    return dest


def unpack_source(archive: Path, workdir: Path) -> Path:
    """
    解压源码。可能是 tar.gz，也可能是单个 .tex 的 gz。
    返回存放 .tex 的目录。
    """
    source_dir = workdir / "src"
    source_dir.mkdir(parents=True, exist_ok=True)

    # 先按 tar.gz 试
    try:
        with tarfile.open(archive, "r:*") as tar:
            members = [m for m in tar.getmembers() if m.isfile()]
            # 防御：拒绝绝对路径与 .. 穿越
            for member in members:
                if member.name.startswith("/") or ".." in Path(member.name).parts:
                    raise RuntimeError("压缩包内含可疑路径：%s" % member.name)
            tar.extractall(source_dir, filter="data")
        print("  解压 tar 包，含 %d 个文件" % len(members))
        return source_dir
    except tarfile.TarError:
        pass

    # 退回：当作单文件 gz
    print("  不是 tar 包，按单文件 gz 处理")
    with gzip.open(archive, "rb") as handle:
        content = handle.read()
    (source_dir / "main.tex").write_bytes(content)
    return source_dir


# ---------------------------------------------------------------- 主文件定位

def find_main_tex(source_dir: Path) -> Path:
    """
    找主 tex：同时含 \\documentclass 和 \\begin{document} 的那个。
    多个候选时，优先文件更大、位于根目录的。
    """
    candidates = []
    for tex in source_dir.rglob("*.tex"):
        try:
            text = read_text(tex)
        except OSError:
            continue
        if "\\documentclass" in text and "\\begin{document}" in text:
            candidates.append((tex.stat().st_size, len(tex.parts), tex))

    if not candidates:
        raise RuntimeError(
            "没有找到主 tex 文件（需要同时含 \\documentclass 与 \\begin{document}）"
        )

    # 文件越大越可能是主文件；层级越浅越好
    candidates.sort(key=lambda item: (-item[0], item[1]))
    chosen = candidates[0][2]
    print("  主文件：%s" % chosen.relative_to(source_dir))
    return chosen


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- 子文件展开

INPUT_PATTERN = re.compile(r"\\(?:input|include)\s*\{([^}]+)\}")


def resolve_inputs(tex_path: Path, source_dir: Path, depth: int = 0,
                   seen: set | None = None) -> str:
    """
    递归展开 \\input{...} / \\include{...}。

    很多论文（如 ViT）的主 tex 只是骨架，各章节写在 sec/*.tex 里，
    不展开的话只能解析出几百字，等于整篇丢失。
    """
    if seen is None:
        seen = set()

    try:
        key = tex_path.resolve()
    except OSError:
        key = tex_path

    # 防止循环引用与过深嵌套
    if key in seen or depth > 12:
        return ""
    seen.add(key)

    text = read_text(tex_path)

    def replace(match: re.Match) -> str:
        name = match.group(1).strip().strip('"').strip("'")
        if not name.endswith(".tex"):
            name += ".tex"

        # 依次尝试：当前目录、根目录、常见子目录
        for candidate in (
            tex_path.parent / name,
            source_dir / name,
            source_dir / "sections" / name,
            source_dir / "sec" / name,
        ):
            if candidate.is_file():
                expanded = resolve_inputs(candidate, source_dir, depth + 1, seen)
                return "\n\n" + expanded + "\n\n"
        return ""   # 找不到就当空，不让整篇失败

    return INPUT_PATTERN.sub(replace, text)


# ---------------------------------------------------------------- LaTeX 处理

def strip_comments(text: str) -> str:
    """去掉 % 注释，但要放过转义的 \\%"""
    return re.sub(r"(?<!\\)%.*", "", text)


def clean_inline(text: str) -> str:
    """
    把一段 LaTeX 清成可读纯文本。
    公式、引用、图表引用都换成占位标记，不丢信息。
    """
    for pattern, replacement in CUSTOM_MACROS:
        text = re.sub(pattern, replacement, text)

    # 转义字符还原：正文里的 \& \% \$ \# \_ 是字面符号，
    # 不还原会留下反斜杠，而且 \& 会让残渣检测误判（Food\&drink）
    text = re.sub(r"\\([&%$#_])", r"\1", text)

    # 引用与交叉引用：保留标记，便于后续统计悬空引用
    text = re.sub(r"\\cite[a-zA-Z]*\{([^}]*)\}", r"[cite:\1]", text)
    text = re.sub(r"\\ref\{([^}]*)\}", r"[ref:\1]", text)
    text = re.sub(r"\\eqref\{([^}]*)\}", r"[eq:\1]", text)

    # 行内公式保留为 LaTeX 片段
    text = re.sub(r"\$([^$]+)\$", r"⟨\1⟩", text)

    # 格式化命令保留内容
    for _ in range(2):
        text = re.sub(
            r"\\(?:textbf|textit|emph|texttt|textrm|mathrm|mathbf|text|mbox)"
            r"\{([^{}]*)\}",
            r"\1",
            text,
        )

    # 换行、间距命令：带参数的要连参数一起删，否则会留下 "-.5em" 这种碎片
    text = re.sub(r"\\(?:vspace|hspace)\*?\s*\{[^}]*\}", " ", text)
    text = re.sub(r"\\\\(\[[^\]]*\])?", " ", text)
    text = re.sub(r"\\(?:noindent|indent|newline|par|hfill|smallskip|medskip|bigskip)\s*", " ", text)

    # 剩余命令：带一两个参数的，整个删掉
    text = re.sub(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?(\{[^{}]*\})?(\{[^{}]*\})?", " ", text)
    # 兜底：单独的 \ 命令与残余花括号
    text = re.sub(r"\\[a-zA-Z]+", " ", text)
    text = re.sub(r"[{}]", "", text)

    text = text.replace("~", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def extract_braced(text: str, start: int) -> str:
    """
    从 start（指向 { 之后）开始，按花括号配对截取内容。
    用于 \\title{...} 这种可能含嵌套花括号的命令。

    必须跳过转义的 \\{ 与 \\}——LaTeX 里 \\{W_i\\} 表示字面花括号，
    如果照常计数，配对会错位，一路吞到文件末尾（大工程里尤其致命）。
    """
    depth = 1
    index = start
    while index < len(text) and depth > 0:
        char = text[index]
        if char == "\\":
            index += 2      # 跳过转义序列，不参与计数
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
        index += 1
    return text[start : index - 1]


INSTITUTION_HINTS = ("University", "Research", "Institute", "Laboratory",
                     "Laboratories", "Inc.", "Ltd", "College", "School of",
                     "Microsoft", "Google", "Meta", "DeepMind", "OpenAI")

# 姓名形态兜底：有些模板用 \centerline + $^{*}$ 排版，按分隔符切不可靠，
# 直接从原始文本里捞「大写开头 + 空格 + 大写开头」的词组更稳
NAME_PATTERN = re.compile(
    r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{1,2}\.)?\s+[A-Z][a-z]{2,})\b"
)


def remove_braced_command(text: str, command: str) -> str:
    """删除 \\command{...}，按花括号配对定位，能处理嵌套"""
    pattern = re.compile(r"\\" + command + r"\s*\{")
    while True:
        match = pattern.search(text)
        if not match:
            return text
        content = extract_braced(text, match.end())
        end = match.end() + len(content) + 1
        text = text[: match.start()] + " " + text[end:]


def strip_front_matter(body: str) -> str:
    """
    剔除标题区（\\maketitle / \\title / \\author / abstract）。
    这些内容已经单独抽取到 meta，留在正文里会污染第一段。
    """
    text = re.sub(r"\\maketitle", " ", body)
    text = re.sub(
        r"\\begin\{abstract\}.*?\\end\{abstract\}", " ", text, flags=re.S
    )
    for command in ("title", "author", "date"):
        text = remove_braced_command(text, command)
    return text


NEWCOMMAND_PATTERN = re.compile(
    r"\\(?:newcommand|renewcommand|providecommand)\s*\{?\\([a-zA-Z]+)\}?"
    r"\s*(?:\[(\d)\])?\s*\{"
)


def _expand_newcommands(text: str, remove_definitions: bool) -> str:
    """展开实现。remove_definitions=False 时只替换使用处、保留定义原文。"""
    mapping = {}
    spans = []

    for match in NEWCOMMAND_PATTERN.finditer(text):
        name = match.group(1)
        argc = int(match.group(2) or 0)
        body = extract_braced(text, match.end())
        spans.append((match.start(), match.end() + len(body) + 1))
        if argc <= 2:
            mapping[name] = (argc, body)

    # 删除定义本身，避免残留。倒序删除保证前面的坐标仍然有效；
    # 再校验末尾确实是 }，防止定位失准把正文一起吃掉。
    if remove_definitions:
        for start, end in sorted(spans, reverse=True):
            if 0 <= start < end <= len(text) and text[end - 1] == "}":
                text = text[:start] + " " + text[end:]

    # 两轮展开，处理宏里套宏的情况
    for _ in range(2):
        for name, (argc, body) in mapping.items():
            if not name.isalpha():
                continue
            if argc == 1:
                text = re.sub(
                    r"\\" + name + r"\{([^{}]*)\}",
                    lambda m: body.replace("#1", m.group(1)),
                    text,
                )
            elif argc == 0:
                # 必须用 lambda：直接传 body 会被 re.sub 当成模板，
                # 里面的 \x 这类 LaTeX 命令会触发 "bad escape"
                text = re.sub(
                    r"\\" + name + r"(?![a-zA-Z])", lambda m: body, text
                )

    return text


def expand_newcommands(text: str) -> str:
    """
    展开作者自定义宏（\\newcommand{\\ve}[1]{\\mathbf{#1}}）。
    不展开的话，公式里会留下 \\ve{y} 这种模型不认识的记号。

    安全兜底：复杂工程（如 SAM 3，75 个宏）里多次删除定义可能让坐标漂移，
    把 \\begin{document} 一起吃掉。一旦检测到结构被破坏，就退回「只展开不删定义」。
    """
    expanded = _expand_newcommands(text, remove_definitions=True)

    marker = "\\begin{document}"
    if marker in text and marker not in expanded:
        expanded = _expand_newcommands(text, remove_definitions=False)

    return expanded


def extract_metadata(body: str) -> dict:
    """抽标题、作者、摘要"""
    meta = {"title": "", "authors": [], "abstract": "", "rawAuthor": ""}

    match = re.search(r"\\title\s*\{", body)
    if match:
        meta["title"] = clean_inline(extract_braced(body, match.end()))

    # \author 可能带可选参数（\author[*]{...}），也可能一人一条命令
    author_cmds = list(re.finditer(r"\\author(?:\s*\[[^\]]*\])?\s*\{", body))
    if author_cmds:
        raw = " \\and ".join(
            extract_braced(body, cmd.end()) for cmd in author_cmds
        )
        meta["rawAuthor"] = re.sub(r"\s+", " ", raw).strip()[:600]

        # 必须在清理之前按 \and / \\ 切开，否则分隔符会被当成命令删掉。
        # 很多论文用 \qquad 排同一行的多个作者，也要算分隔符。
        parts = re.split(r"\\and\b|\\\\|\\cr\b|\\qquad|\\quad|\\hspace\*?\{[^}]*\}", raw)
        authors = []
        for part in parts:
            name = clean_inline(part)
            if not name or len(name) > 40:
                continue
            if "@" in name or "\\" in name or "{" in name:
                continue
            if re.search(r"\d", name):
                continue
            # 去掉机构行与脚注残留
            if any(hint in name for hint in INSTITUTION_HINTS):
                continue
            if len(name) < 2:
                continue
            authors.append(name)

        # 兜底：按分隔符切不出来时，用姓名形态从原文里捞
        if len(authors) < 2:
            found = NAME_PATTERN.findall(raw)
            cleaned = []
            for name in found:
                if any(hint in name for hint in INSTITUTION_HINTS):
                    continue
                if name not in cleaned:
                    cleaned.append(name)
            if len(cleaned) >= 2:
                authors = cleaned

        meta["authors"] = authors[:12]

    match = re.search(
        r"\\begin\{abstract\}(.*?)\\end\{abstract\}", body, flags=re.S
    )
    if match:
        meta["abstract"] = clean_inline(match.group(1))

    return meta


# ---------------------------------------------------------------- 引用完整性

LABEL_PATTERN = re.compile(r"\\label\{([^}]*)\}")
REF_PATTERN = re.compile(r"\\(?:ref|eqref|autoref|cref|Cref)\{([^}]*)\}")


def collect_labels_and_refs(body: str) -> tuple[set, set]:
    """
    全文层面的 label 与引用。
    悬空判定必须用这个全集，不能只看图表——章节、公式、表格都算。
    """
    labels = {m.group(1).strip() for m in LABEL_PATTERN.finditer(body)}
    refs = set()
    for match in REF_PATTERN.finditer(body):
        for piece in match.group(1).split(","):
            piece = piece.strip()
            if piece:
                refs.add(piece)
    return labels, refs


SECTION_PATTERN = re.compile(
    r"\\(?P<level>section|subsection|subsubsection)\*?\s*\{(?P<title>[^{}]*)\}"
)


def split_sections(body: str) -> list[dict]:
    """
    按 \\section 系列切分。返回 [{level, title, content}]，
    content 是该标题到下一个标题之间的原文。
    """
    matches = list(SECTION_PATTERN.finditer(body))
    if not matches:
        return []

    sections = []
    # 第一个标题之前的内容（通常是摘要和方法预览）单独成节
    preamble = body[: matches[0].start()]
    if len(preamble.strip()) > 200:
        sections.append({
            "level": "section",
            "title": "（正文前）",
            "content": preamble,
        })

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        sections.append({
            "level": match.group("level"),
            "title": clean_inline(match.group("title")),
            "content": body[match.end() : end],
        })
    return sections


FORMULA_ENVIRONMENTS = ("equation", "equation*", "align", "align*",
                        "gather", "gather*", "eqnarray", "eqnarray*",
                        "multline", "multline*")

DISPLAY_FORMULA_PATTERN = re.compile(
    r"\\begin\{(" + "|".join(re.escape(e) for e in FORMULA_ENVIRONMENTS) + r")\}"
    r"(.*?)\\end\{\1\}",
    flags=re.S,
)
BRACKET_FORMULA_PATTERN = re.compile(r"\\\[(.*?)\\\]", flags=re.S)
DOLLAR_FORMULA_PATTERN = re.compile(r"\$\$(.*?)\$\$", flags=re.S)


def extract_formulas(content: str) -> list[dict]:
    """抽显示公式，保留原始 LaTeX"""
    formulas = []
    for pattern in (DISPLAY_FORMULA_PATTERN, BRACKET_FORMULA_PATTERN,
                    DOLLAR_FORMULA_PATTERN):
        for match in pattern.finditer(content):
            raw = match.group(match.lastindex or 0).strip()
            label_match = re.search(r"\\label\{([^}]*)\}", raw)
            formulas.append({
                "raw": re.sub(r"\s+", " ", raw)[:600],
                "label": label_match.group(1) if label_match else "",
            })
    return formulas


FIGURE_PATTERN = re.compile(r"\\begin\{figure\*?\}(.*?)\\end\{figure\*?\}", flags=re.S)

# ---------------------------------------------------------------- 表格

# 外层 table 环境（含 longtable 等变体），可选位置参数 [htbp]
TABLE_ENV_PATTERN = re.compile(
    r"\\begin\{(table\*?|longtable\*?|sidewaystable\*?|wraptable\*?)\}"
    r"\s*(?:\[[^\]]*\])?(.*?)\\end\{\1\}",
    flags=re.S,
)

# tabular 本体，列格式参数可能写成 {lcc} 或 [pos]{lcc}
TABULAR_PATTERN = re.compile(
    r"\\begin\{(tabular\*?|tabularx|longtable|array)\}"
    r"\s*(?:\{[^{}]*\}|\[[^\]]*\]\{[^{}]*\})"
    r"(.*?)\\end\{\1\}",
    flags=re.S,
)

# 表格里只起排版作用的命令，解析时直接丢掉
RULE_PATTERN = re.compile(
    r"\\(?:toprule|midrule|bottomrule|hline"
    r"|cline\{[^}]*\}|cmidrule(?:\([^)]*\))?\{[^}]*\}"
    r"|addlinespace|vspace\*?\{[^}]*\}|arraystretch)\s*"
)

ROW_SPLIT_PATTERN = re.compile(r"\\\\(?:\s*\[[^\]]*\])?|\\cr\b|\\tabularnewline")


def parse_tabular(body: str) -> list[list[str]]:
    """把 tabular 内容切成行列。保留原始行列关系，这是表格唯一的价值。"""
    body = RULE_PATTERN.sub(" ", body)
    rows = []

    for raw_row in ROW_SPLIT_PATTERN.split(body):
        if not raw_row.strip():
            continue
        # 跨列单元格：\multicolumn{2}{c}{text} -> text
        raw_row = re.sub(
            r"\\multicolumn\{(\d+)\}\{[^{}]*\}\{([^{}]*)\}", r"\2", raw_row
        )
        cells = [clean_inline(cell) for cell in raw_row.split("&")]
        if any(cell for cell in cells):
            rows.append(cells)

    return rows


def extract_tables(content: str) -> list[dict]:
    """
    抽表格。表格往往承载论文的核心结果（精度对比），
    但它们的文本如果留在正文里，会变成 "Dataset && IN1k & ..." 这种垃圾段落。
    """
    tables = []
    covered = []

    for match in TABLE_ENV_PATTERN.finditer(content):
        covered.append((match.start(), match.end()))
        block = match.group(2)

        tabular = TABULAR_PATTERN.search(block)
        rows = parse_tabular(tabular.group(2)) if tabular else []

        caption = re.search(r"\\caption\{", block)
        label = re.search(r"\\label\{([^}]*)\}", block)

        tables.append({
            "label": label.group(1) if label else "",
            "caption": clean_inline(extract_braced(block, caption.end()))
                       if caption else "",
            "header": rows[0] if rows else [],
            "rows": rows[1:] if len(rows) > 1 else [],
            "columnCount": max((len(r) for r in rows), default=0),
        })

    # 游离的 tabular（没包在 table 环境里）
    for match in TABULAR_PATTERN.finditer(content):
        if any(start <= match.start() < end for start, end in covered):
            continue
        rows = parse_tabular(match.group(2))
        if not rows:
            continue
        tables.append({
            "label": "",
            "caption": "",
            "header": rows[0],
            "rows": rows[1:],
            "columnCount": max((len(r) for r in rows), default=0),
        })

    return tables


# ---------------------------------------------------------------- 算法

ALGORITHM_PATTERN = re.compile(
    r"\\begin\{(algorithm\*?|algorithmic|algorithm2e)\}"
    r"\s*(?:\[[^\]]*\])?(.*?)\\end\{\1\}",
    flags=re.S,
)


def extract_algorithms(content: str) -> list[dict]:
    """
    抽算法环境。伪代码混在正文里会让段落读起来像乱码，
    单独抽出来按步骤存，讲方法时反而能直接引用。
    """
    algorithms = []

    for match in ALGORITHM_PATTERN.finditer(content):
        block = match.group(2)
        caption = re.search(r"\\caption\{", block)
        label = re.search(r"\\label\{([^}]*)\}", block)

        # 伪代码里以 \\ 分步，去掉控制命令但不能丢条件文本
        body = RULE_PATTERN.sub(" ", block)
        steps = []
        for raw_step in re.split(r"\\\\", body):
            step = clean_inline(raw_step)
            if len(step) >= 4:
                steps.append(step)

        algorithms.append({
            "label": label.group(1) if label else "",
            "caption": clean_inline(extract_braced(block, caption.end()))
                       if caption else "",
            "steps": steps[:60],
        })

    return algorithms


def extract_figures(content: str) -> list[dict]:
    """抽图表：图片路径 + 标题 + label"""
    figures = []
    for match in FIGURE_PATTERN.finditer(content):
        block = match.group(1)
        graphic = re.search(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}", block)
        caption = re.search(r"\\caption\{", block)
        label = re.search(r"\\label\{([^}]*)\}", block)

        figures.append({
            "asset": graphic.group(1) if graphic else "",
            "caption": clean_inline(extract_braced(block, caption.end()))
                       if caption else "",
            "label": label.group(1) if label else "",
        })
    return figures


def build_segments(sections: list[dict]) -> list[dict]:
    """
    把章节内容切成论证单元（这里先用段落近似，M1 再做语义切分）。

    关键：图表、表格、算法、公式必须先抽走再切段。
    否则表格的 "A & B & C" 会被当成正文，讲出来全是垃圾。
    """
    segments = []
    order = 0

    for section in sections:
        content = strip_comments(section["content"])

        figures = extract_figures(content)
        tables = extract_tables(content)
        algorithms = extract_algorithms(content)
        formulas = extract_formulas(content)

        # 依次抽走非正文块。顺序有讲究：先删 table 环境，
        # 再删游离的 tabular，否则 table 的结束标签会残留。
        prose = FIGURE_PATTERN.sub(" ", content)
        prose = TABLE_ENV_PATTERN.sub(" ", prose)
        prose = TABULAR_PATTERN.sub(" ", prose)
        prose = ALGORITHM_PATTERN.sub(" ", prose)
        prose = DISPLAY_FORMULA_PATTERN.sub(" ", prose)
        prose = BRACKET_FORMULA_PATTERN.sub(" ", prose)
        prose = DOLLAR_FORMULA_PATTERN.sub(" ", prose)

        # 按空行切段
        raw_paragraphs = re.split(r"\n\s*\n", prose)
        paragraphs = []
        for raw in raw_paragraphs:
            text = clean_inline(raw)
            if len(text) >= MIN_PARA_LEN:
                paragraphs.append(text)

        section_start = len(segments)

        for paragraph in paragraphs:
            order += 1
            segments.append({
                "id": "seg-%03d" % order,
                "order": order,
                "section": section["title"],
                "level": section["level"],
                "type": "",          # 功能标签留到 M1 由模型标注
                "text": paragraph,
                "summary": "",       # 滚动摘要留到 M1
                "figureRefs": re.findall(r"\[ref:([^\]]+)\]", paragraph),
            })

        # 非正文对象挂到该章节的第一个段落上，便于后续按位置关联
        if len(segments) > section_start:
            anchor = segments[section_start]
            if figures:
                anchor["figures"] = figures
            if tables:
                anchor["tables"] = tables
            if algorithms:
                anchor["algorithms"] = algorithms
            if formulas:
                anchor["formulas"] = formulas

    return segments


# ---------------------------------------------------------------- 质量自检

def is_table_residue(text: str) -> bool:
    """
    判断一段是不是表格残渣。

    表格残渣的特征不是「& 多」，而是「被 & 切成很多很短的片段」——单元格通常
    只有几个词。正文里的 "Food & drink" 只有两三个片段，不会被误判。
    """
    cells = text.split("&")
    if len(cells) < 4:
        return False
    short_cells = sum(1 for cell in cells if len(cell.strip()) <= 12)
    return short_cells >= len(cells) * 0.6


def run_quality_check(paper: dict, all_labels: set, all_refs: set) -> dict:
    """
    四项指标：章节数、段落中位数、超长段落占比、悬空引用。
    公式数只统计不判定——实验型论文天然公式少，用它当门槛没有意义。
    """
    segments = paper["segments"]
    sections = {s["section"] for s in segments}

    lengths = sorted(len(s["text"]) for s in segments) or [0]
    median_len = statistics.median(lengths)
    oversized_ratio = sum(1 for x in lengths if x > OVERSIZED_PARA) / max(len(lengths), 1)

    formula_count = sum(len(s.get("formulas", [])) for s in segments)
    figure_count = sum(len(s.get("figures", [])) for s in segments)
    table_count = sum(len(s.get("tables", [])) for s in segments)
    # 有数据行的表格才算真解析出来；只有表头或完全空的，要如实统计
    tables_with_rows = sum(
        1 for s in segments for t in s.get("tables", []) if t.get("rows")
    )
    algorithm_count = sum(len(s.get("algorithms", [])) for s in segments)

    # 正文污染检测：真正的表格残渣是「多个很短的 & 分隔片段」，
    # 不能只看 & 数量——正文里的 "Food & drink" 也算 &。
    dirty = sum(1 for s in segments if is_table_residue(s["text"]))

    # 悬空引用：引用了全文任何位置都不存在的 label
    dangling = all_refs - all_labels

    checks = [
        {
            "name": "章节数",
            "value": len(sections),
            "threshold": ">= %d" % MIN_SECTIONS,
            "pass": len(sections) >= MIN_SECTIONS,
        },
        {
            "name": "段落中位数",
            "value": round(median_len, 1),
            "threshold": "%d ~ %d 字符" % (MEDIAN_PARA_MIN, MEDIAN_PARA_MAX),
            "pass": MEDIAN_PARA_MIN <= median_len <= MEDIAN_PARA_MAX,
        },
        {
            "name": "超长段落",
            "value": "%.1f%%" % (oversized_ratio * 100),
            "threshold": "> %d 字符的段落 <= %.0f%%"
                         % (OVERSIZED_PARA, MAX_OVERSIZED_RATIO * 100),
            "pass": oversized_ratio <= MAX_OVERSIZED_RATIO,
        },
        {
            "name": "表格残渣",
            "value": dirty,
            "threshold": "== 0",
            "pass": dirty == 0,
        },
        {
            "name": "悬空引用",
            "value": len(dangling),
            "threshold": "<= %d" % MAX_DANGLING_REFS,
            "pass": len(dangling) <= MAX_DANGLING_REFS,
        },
    ]

    passed = all(item["pass"] for item in checks)

    return {
        "passed": passed,
        "checks": checks,
        "stats": {
            "segments": len(segments),
            "sections": len(sections),
            "formulas": formula_count,
            "figures": figure_count,
            "tables": table_count,
            "tablesWithRows": tables_with_rows,
            "algorithms": algorithm_count,
            "labels": len(all_labels),
            "refs": len(all_refs),
            "chars": sum(lengths),
            "medianPara": round(median_len, 1),
            "dirtyParagraphs": dirty,
        },
        "danglingRefs": sorted(dangling)[:10],
    }


# ---------------------------------------------------------------- 输出

def print_report(paper: dict, quality: dict) -> None:
    meta = paper["meta"]
    print()
    print("=" * 68)
    print("  解析报告")
    print("=" * 68)
    print("  标题   %s" % (meta["title"] or "（未识别）"))
    print("  作者   %s" % (", ".join(meta["authors"][:4]) or "（未识别）"))
    print("  ID     %s%s" % (meta["arxivId"], meta.get("version") or ""))
    print("  来源   LaTeX 源码")
    print()
    print("  章节树")
    for item in paper.get("outline", []):
        # 一级章节顶格，子章节缩进；0 段的章节通常是纯标题（内容全在子节里）
        indent = "" if item["level"] == "section" else "  "
        note = "" if item["segments"] else "   ← 无直接正文"
        print("%s· %s（%d 段）%s"
              % (indent, item["title"], item["segments"], note))

    print()
    print("  质量自检")
    for item in quality["checks"]:
        mark = "[通过]" if item["pass"] else "[不达标]"
        print("  %-8s %-10s %-10s 阈值 %s"
              % (mark, item["name"], item["value"], item["threshold"]))

    stats = quality["stats"]
    print()
    print("  统计   段落 %d · 章节 %d · 公式 %d · 图 %d · 表 %d · 算法 %d · 正文 %d 字"
          % (stats["segments"], stats["sections"], stats["formulas"],
             stats["figures"], stats.get("tables", 0),
             stats.get("algorithms", 0), stats["chars"]))
    if quality["danglingRefs"]:
        print("  悬空   %s" % ", ".join(quality["danglingRefs"]))
    print()
    verdict = "可以进入 M1（模型标注与讲解）" if quality["passed"] \
              else "不达标，需要走降级路径（HTML 或视觉兜底）"
    print("  结论   %s" % verdict)
    print("=" * 68)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="M0：从 arXiv 取源并解析为 paper.json"
    )
    parser.add_argument("arxiv", help="arXiv ID 或论文链接")
    parser.add_argument("--out", default="out", help="输出目录（默认 out/）")
    parser.add_argument("--cache", default="cache",
                        help="源码缓存目录（默认 cache/；传空字符串禁用）")
    parser.add_argument("--keep", action="store_true", help="保留下载与解压的原始文件")
    args = parser.parse_args()

    started = time.time()

    print("[1/5] 解析输入")
    try:
        arxiv_id, version = extract_arxiv_id(args.arxiv)
    except ValueError as exc:
        print("  失败：%s" % exc, file=sys.stderr)
        return 2
    print("  ID = %s%s" % (arxiv_id, version or ""))

    out_dir = Path(args.out) / arxiv_id.replace("/", "_")
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache) if args.cache else None
    workdir = Path(tempfile.mkdtemp(prefix="paper_reader_"))

    try:
        print("[2/5] 取源")
        archive = download_eprint(arxiv_id, workdir / "eprint.tar.gz", cache_dir)
        source_dir = unpack_source(archive, workdir)

        print("[3/5] 定位主 tex")
        main_tex = find_main_tex(source_dir)

        print("[4/5] 解析")
        # 先展开 \input 子文件：主 tex 可能只是骨架
        full_source = resolve_inputs(main_tex, source_dir)
        raw = strip_comments(full_source)
        # 宏定义通常写在导言区（\begin{document} 之前），必须对全文展开，
        # 否则正文里的 \ve{y} 这类记号会原样留下
        raw = expand_newcommands(raw)

        body_match = re.search(r"\\begin\{document\}(.*?)\\end\{document\}", raw, flags=re.S)
        if not body_match:
            raise RuntimeError("主 tex 里没有 \\begin{document}...\\end{document}")
        body = body_match.group(1)

        meta = extract_metadata(body)

        # 不少模板把 \title / \author 写在导言区（\begin{document} 之前），
        # 正文里找不到时回退到全文再抽一次
        if not meta["title"] or not meta["authors"]:
            front = extract_metadata(raw)
            if not meta["title"]:
                meta["title"] = front["title"]
            if not meta["authors"]:
                meta["authors"] = front["authors"]
                meta["rawAuthor"] = meta["rawAuthor"] or front["rawAuthor"]
        meta.update({
            "arxivId": arxiv_id,
            "version": version or "",
            "year": None,
            "venue": "",
            "peerReviewed": False,
        })

        # 正文：剔除标题区（宏已在全文层面展开过）
        body_clean = strip_front_matter(body)

        sections = split_sections(body_clean)
        if not sections:
            raise RuntimeError("没有识别到任何 \\section，可能是不常见的排版方式")

        segments = build_segments(sections)
        if not segments:
            raise RuntimeError("切分后没有有效段落，检查清理规则是否吃掉了正文")

        # 章节树单独记一份，包含没有直接段落的纯标题章节
        outline = [
            {
                "level": section["level"],
                "title": section["title"],
                "segments": sum(1 for s in segments if s["section"] == section["title"]),
            }
            for section in sections
        ]

        # 引用完整性在全文档层面判定
        all_labels, all_refs = collect_labels_and_refs(body)

        paper = {
            "id": "arxiv-%s%s" % (arxiv_id, version or ""),
            "sourceType": "latex",
            "meta": meta,
            "map": {"problem": "", "claims": [], "methodSteps": [],
                    "experiments": "", "conclusions": [], "limitations": []},
            "outline": outline,
            "segments": segments,
        }
        quality = run_quality_check(paper, all_labels, all_refs)
        paper["parseQuality"] = {
            "passed": quality["passed"],
            "checks": quality["checks"],
            "stats": quality["stats"],
            "danglingRefs": quality["danglingRefs"],
        }

        print("[5/5] 输出")
        json_path = out_dir / "paper.json"
        json_path.write_text(
            json.dumps(paper, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report_path = out_dir / "report.txt"
        print_report(paper, quality)

        # 报告同时落盘，便于对比多篇论文
        report_buffer = io.StringIO()
        original_stdout = sys.stdout
        try:
            sys.stdout = report_buffer
            print_report(paper, quality)
        finally:
            sys.stdout = original_stdout
        report_path.write_text(report_buffer.getvalue(), encoding="utf-8")

        if args.keep:
            kept = out_dir / "source"
            if kept.exists():
                shutil.rmtree(kept)
            shutil.copytree(source_dir, kept)
            print("  原始源码已保留：%s" % kept)

        print("  JSON：%s" % json_path)
        print("  耗时：%.1f 秒" % (time.time() - started))
        return 0 if quality["passed"] else 1

    except RuntimeError as exc:
        print("\n  失败：%s" % exc, file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
