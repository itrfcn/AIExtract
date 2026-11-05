"""文本来源读取：txt / doc / docx / pdf(文字层+扫描件 OCR) / 图片(OCR) / xlsx。"""

from __future__ import annotations

from pathlib import Path

from . import ocr

_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "utf-16")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}


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


def _clean_doc_text(text: str) -> str:
    """把 Word 二进制里的控制字符转成可读文本。"""
    text = (text.replace("\r", "\n").replace("\x07", "\n")  # 段落 / 单元格结束
                .replace("\x0b", "\n").replace("\x0c", "\n")   # 软换行 / 分页
                .replace("\x1e", "-").replace("\x1f", ""))
    for ch in "\x00\x01\x02\x03\x04\x05\x08\x13\x14\x15":  # 域代码、图形锚点等
        text = text.replace(ch, "")
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line)


def _read_doc(path: Path) -> tuple[str, list[str]]:
    """读取 Word 97-2003（.doc）：解析 OLE 的 WordDocument 流 + piece table。"""
    import struct

    try:
        import olefile
    except ImportError:
        return "", ["读取 .doc 需要 olefile：pip install olefile"]

    if not olefile.isOleFile(str(path)):
        return "", ["不是有效的 .doc（可能被加密或已损坏），请另存为 .docx"]

    with olefile.OleFileIO(str(path)) as ole:
        if not ole.exists("WordDocument"):
            return "", ["缺少 WordDocument 流，可能是其他 OLE 文档"]
        wd = ole.openstream("WordDocument").read()
        if struct.unpack_from("<H", wd, 0x0002)[0] < 0x00C1:
            return "", ["不支持 Word 6/95 及更早版本的 .doc，请另存为 .docx"]
        flags = struct.unpack_from("<H", wd, 0x000A)[0]
        table_name = "1Table" if flags & 0x0200 else "0Table"
        fc_clx = struct.unpack_from("<I", wd, 0x01A2)[0]
        lcb_clx = struct.unpack_from("<I", wd, 0x01A6)[0]
        table = ole.openstream(table_name).read()

    # Clx 里跳过 prc，找到 piece table（clxt=0x02）
    clx = table[fc_clx:fc_clx + lcb_clx]
    pos, pcdt = 0, b""
    while pos < len(clx):
        kind = clx[pos]
        if kind == 1:
            pos += 3 + struct.unpack_from("<H", clx, pos + 1)[0]
        elif kind == 2:
            length = struct.unpack_from("<I", clx, pos + 1)[0]
            pcdt = clx[pos + 5:pos + 5 + length]
            break
        else:
            return "", ["piece table 解析失败，请另存为 .docx"]

    if not pcdt:
        return "", ["未找到文字片段（文档可能为空），请另存为 .docx"]

    count = (len(pcdt) - 4) // 12
    cps = [struct.unpack_from("<I", pcdt, 4 * i)[0] for i in range(count + 1)]
    pcd_off = 4 * (count + 1)
    parts: list[str] = []
    for i in range(count):
        fc = struct.unpack_from("<I", pcdt, pcd_off + 8 * i + 2)[0]
        chars = cps[i + 1] - cps[i]
        if fc & 0x40000000:  # 压缩片段：单字节（中文为 GBK）
            offset = (fc & 0x3FFFFFFF) // 2
            raw = wd[offset:offset + chars]
            try:
                parts.append(raw.decode("gbk"))
            except UnicodeDecodeError:
                parts.append(raw.decode("cp1252", errors="replace"))
        else:  # UTF-16LE 片段
            offset = fc & 0x3FFFFFFF
            parts.append(wd[offset:offset + chars * 2].decode("utf-16-le", errors="replace"))

    text = _clean_doc_text("".join(parts))
    warnings = [] if text else ["doc 中没有可读文字"]
    return text, warnings


def _read_pdf(path: Path) -> tuple[str, list[str]]:
    import pdfplumber

    warnings: list[str] = []
    page_texts: list[str | None] = []
    pending: list[tuple[int, object]] = []  # (页序号, 渲染后的图片)

    with pdfplumber.open(str(path)) as pdf:
        page_texts = [None] * len(pdf.pages)
        for i, page in enumerate(pdf.pages):
            text = (page.extract_text() or "").strip()
            if text:
                page_texts[i] = text
                continue
            if not (len(page.curves) > 0 or len(page.images) > 0):
                continue
            # 没有文字层、但有图形/图片 → 扫描件，渲染成图后识别（vision 可并发）
            try:
                pending.append((i, page.to_image(resolution=200).original))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"第 {i + 1} 页渲染失败：{exc}")

    if pending:
        results = ocr.transcribe_images([image for _, image in pending])
        for (i, _image), (ocr_text, ocr_warnings) in zip(pending, results):
            if ocr_text:
                page_texts[i] = ocr_text
                warnings.append(f"第 {i + 1} 页使用 OCR 识别")
            else:
                warnings.extend(f"第 {i + 1} 页 {w}" for w in ocr_warnings)

    pages = [text for text in page_texts if text]
    text = "\f".join(pages)  # \f 作为分页标记，切分模式 page 依赖它
    if not text:
        warnings.append("PDF 未提取到任何文字：可能是扫描版或纯图片 PDF")
    return text, warnings


def _read_image(path: Path) -> tuple[str, list[str]]:
    if ocr.backend() == "rapidocr" and not ocr.is_available():
        return "", [f"图片需要 OCR：{ocr.ENGINE_HINT}"]
    text, warnings = ocr.ocr_image_file(path)
    if not text and not warnings:
        warnings = ["OCR 未识别到文字"]
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
        return _read_doc(path)
    if suffix in _IMAGE_SUFFIXES:
        return _read_image(path)
    return "", [f"不支持的文本格式：{suffix}"]


def is_supported_text_file(name: str) -> bool:
    return Path(name).suffix.lower() in (
        {".txt", ".md", ".csv", ".doc", ".docx", ".pdf", ".xlsx", ".xlsm"} | _IMAGE_SUFFIXES
    )
