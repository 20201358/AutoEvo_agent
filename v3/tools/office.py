"""Office 文档工具：读 / 写 .docx（基于 python-docx）。

如果机器上没有 python-docx，工具会自动提示安装方式，而不是直接崩溃。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ._base import register


def _docx_module_or_404():
    """导入 python-docx，缺失时给出明确报错。"""
    try:
        import docx  # type: ignore

        return docx
    except ImportError:
        raise RuntimeError(
            "未安装 python-docx。请先在 venv 里运行 "
            "`pip install python-docx`，然后重试。"
        )


# ============================================================
# 读
# ============================================================

@register(category="office")
def read_docx(path: str, max_chars: int = 20000) -> str:
    """读取 .docx 文件的内容（纯文本 + 段落结构）。

    会保留段落顺序（标题段落前缀 `[H1]` / `[H2]` / `[TITLE]`），
    方便 LLM 理解。但不会还原字体、颜色、表格等复杂排版。

    Args:
        path: .docx 文件路径。
        max_chars: 输出上限字符数（防止超大文档撑爆上下文）。
    """
    try:
        docx = _docx_module_or_404()
    except RuntimeError as e:
        return f"错误：{e}"

    p = Path(path).expanduser().resolve()
    if not p.exists():
        return f"错误：文件不存在 -> {p}"
    if not p.is_file():
        return f"错误：不是文件 -> {p}"
    if p.suffix.lower() != ".docx":
        return f"错误：仅支持 .docx 格式（非 {p.suffix}）"

    try:
        doc = docx.Document(str(p))
    except Exception as e:
        return f"错误：读取失败 -> {type(e).__name__}: {e}"

    lines: list[str] = []
    for para in doc.paragraphs:
        style = (para.style.name or "").lower() if para.style else ""
        text = para.text.strip()
        if not text:
            continue
        if style.startswith("title"):
            lines.append(f"[TITLE] {text}")
        elif "heading 1" in style:
            lines.append(f"[H1] {text}")
        elif "heading 2" in style:
            lines.append(f"[H2] {text}")
        elif "heading 3" in style:
            lines.append(f"[H3] {text}")
        else:
            lines.append(text)

    # 表格
    for ti, table in enumerate(doc.tables):
        lines.append(f"\n[Table #{ti + 1}]")
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            lines.append(" | ".join(cells))
        lines.append("[end Table]")

    body = "\n".join(lines).strip()
    if len(body) > max_chars:
        body = body[:max_chars] + f"\n…(已截断，文档共 {len(body)} 字符)"
    return body or "(空文档)"


@register(category="office")
def docx_summary(path: str, max_paragraphs: int = 30) -> str:
    """抽取 .docx 的目录式摘要（每个标题 + 前若干段）。

    适合用大文档时先调用本工具，再决定要不要 read_docx 全文。

    Args:
        path: .docx 文件路径。
        max_paragraphs: 每个标题块最多展示多少段。
    """
    try:
        docx = _docx_module_or_404()
    except RuntimeError as e:
        return f"错误：{e}"

    p = Path(path).expanduser().resolve()
    if not p.exists() or not p.is_file():
        return f"错误：文件不存在 -> {p}"

    try:
        doc = docx.Document(str(p))
    except Exception as e:
        return f"错误：读取失败 -> {type(e).__name__}: {e}"

    blocks: list[dict[str, Any]] = []
    current: list[str] = []
    current_title: Optional[str] = None

    def flush():
        if current_title.is_blank if hasattr(current_title, "is_blank") else (not current_title):
            return
        blocks.append({"title": current_title, "preview": (current or [])[:max_paragraphs]})

    for para in doc.paragraphs:
        style = (para.style.name or "").lower() if para.style else ""
        text = para.text.strip()
        if not text:
            continue
        if style.startswith("title") or "heading" in style:
            blocks.append({"title": text, "preview": (current or [])[:max_paragraphs]})
            current = []
            current_title = text
        else:
            current.append(text)

    lines = []
    for b in blocks[:50]:
        lines.append(f"## {b['title']}")
        for s in b["preview"][:5]:
            lines.append(f"  {s[:160]}")
    return "\n".join(lines) or "(未识别到标题)"


# ============================================================
# 写
# ============================================================

@register(category="office", requires_confirm=True, risk="medium")
def write_docx(
    path: str,
    content: str,
    title: str = "",
    overwrite: bool = False,
) -> str:
    """把一段 Markdown-ish 文本写成 .docx。

    支持的最简语法：
      - ``# 标题`` / ``## 子标题`` → H1 / H2
      - ``**加粗**`` / ``*斜体*`` → 粗体 / 斜体
      - 空行分段
      - 以 ``- `` 或 ``* `` 开头的行 → 项目符号
      - 以 ``1. `` 开始的连续编号 → 编号列表

    **会覆盖或新建 .docx 文件**，默认要求用户确认。

    Args:
        path: 目标 .docx 路径。
        content: 文档内容。
        title: 文档主标题（不传则取第一行 ``# ...``）。
        overwrite: 已存在时是否覆盖。
    """
    try:
        docx = _docx_module_or_404()
    except RuntimeError as e:
        return f"错误：{e}"

    target = Path(path).expanduser().resolve()
    if not target.suffix:
        target = target.with_suffix(".docx")
    if target.exists() and not overwrite:
        return f"错误：目标已存在 -> {target}（如需覆盖请设 overwrite=true）"

    try:
        from docx import Document
        from docx.shared import Pt
    except ImportError:
        return "错误：python-docx 不可用"

    doc = Document()

    if title:
        doc.add_heading(title, level=0)

    lines = (content or "").replace("\r\n", "\n").split("\n")
    in_numbered = False
    numbered_index = 0

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            doc.add_paragraph("")
            in_numbered = False
            numbered_index = 0
            continue

        # 标题
        if line.startswith("# ") and not title:
            doc.add_heading(line[2:].strip(), level=0)
            continue
        if line.startswith("## "):
            doc.add_heading(line[3:].strip(), level=2)
            continue
        if line.startswith("### "):
            doc.add_heading(line[4:].strip(), level=3)
            continue

        # 列表
        if line.lstrip().startswith(("- ", "* ")):
            doc.add_paragraph(line.lstrip()[2:].strip(), style="List Bullet")
            in_numbered = False
            numbered_index = 0
            continue

        # 编号列表（识别 1. 2. 3. 起始）
        import re as _re

        m = _re.match(r"^\s*(\d+)\.\s+(.+)$", line)
        if m:
            numbered_index += 1
            doc.add_paragraph(m.group(2).strip(), style="List Number")
            in_numbered = True
            continue
        if in_numbered and not line.startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
            in_numbered = False
            numbered_index = 0

        # 普通段落（处理 **bold** / *italic*）
        para = doc.add_paragraph()
        _add_runs(para, line)

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        doc.save(str(target))
    except Exception as e:
        return f"错误：保存失败 -> {type(e).__name__}: {e}"

    return f"已写入 .docx -> {target}"


def _add_runs(para, text: str) -> None:
    """极简的 inline 格式解析：支持 ``**bold**`` 与 ``*italic*``。"""
    import re as _re

    tokens = _re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", text)
    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("**") and tok.endswith("**"):
            run = para.add_run(tok[2:-2])
            run.bold = True
        elif tok.startswith("*") and tok.endswith("*") and len(tok) > 2:
            run = para.add_run(tok[1:-1])
            run.italic = True
        else:
            para.add_run(tok)


def __getattr__(name: str):
    from ._base import get as _get
    r = _get(name)
    if r:
        return r.fn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["read_docx", "docx_summary", "write_docx"]