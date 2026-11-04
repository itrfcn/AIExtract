"""文本来源读取：txt / docx / pdf(文字层) / xlsx。"""

from __future__ import annotations

from pathlib import Path

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16")


def _read_txt(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in _ENCODINGS:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def _read_docx(path: Path) -> tuple[str, list[str]]:
    from docx import Document

    warnings: list[str] = []
    doc = Document(str(path))
    lines = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                lines.append("，".join(cells))
    if not lines:
        warnings.append("docx 中没有可读文字")
    return "\n".join(lines), warnings


def _read_pdf(path: Path) -> tuple[str, list[str]]:
    import pdfplumber

    warnings: list[str] = []
    pages: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = (page.extract_text() or "").strip()
            if text:
                pages.append(text)
            elif len(page.curves) > 0 or len(page.images) > 0:
                warnings.append(f"第 {i} 页没有文字层（疑似扫描件），当前 MVP 不支持 OCR")
    text = "\n\n".join(pages)
    if not text:
        warnings.append("PDF 未提取到任何文字：可能是扫描版或纯图片 PDF")
    return text, warnings


def _read_xlsx(path: Path) -> tuple[str, list[str]]:
    from openpyxl import load_workbook

    warnings: list[str] = []
    wb = load_workbook(path, read_only=True, data_only=True)
    lines: list[str] = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            cells = [str(v).strip() for v in row if v is not None and str(v).strip()]
            if cells:
                lines.append("，".join(cells))
    wb.close()
    if not lines:
        warnings.append("xlsx 文本来源中没有可读内容")
    return "\n".join(lines), warnings


def read_text_file(path: Path) -> tuple[str, list[str]]:
    """读取文本来源，返回 (文本, 警告列表)。"""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".csv"):
        text = _read_txt(path)
        warnings = [] if text.strip() else ["文本文件为空"]
        return text, warnings
    if suffix == ".docx":
        return _read_docx(path)
    if suffix == ".pdf":
        return _read_pdf(path)
    if suffix in (".xlsx", ".xlsm"):
        return _read_xlsx(path)
    if suffix == ".doc":
        return "", ["不支持旧版 .doc，请另存为 .docx"]
    return "", [f"不支持的文本格式：{suffix}"]


def is_supported_text_file(name: str) -> bool:
    return Path(name).suffix.lower() in {".txt", ".md", ".csv", ".docx", ".pdf", ".xlsx", ".xlsm"}
