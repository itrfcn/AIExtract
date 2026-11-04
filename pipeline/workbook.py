"""表格读取：定位表头、读取列信息、预览、合并单元格检测（只读，不改原文件）。"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet


@dataclass
class ColumnInfo:
    index: int                 # 0-based 列号
    header: str
    letter: str
    merged: bool = False
    sample_values: list[str] = field(default_factory=list)


@dataclass
class TableInfo:
    path: Path
    kind: str                  # xlsx | csv
    sheet_names: list[str]
    sheet: str
    header_row: int            # 1-based
    headers: list[ColumnInfo]
    preview: list[list[str]]
    row_count: int
    data_last_row: int
    warnings: list[str] = field(default_factory=list)


def _cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d") if value.time() == datetime.min.time() else value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def effective_last_row(ws: Worksheet) -> int:
    """去掉尾部空行后的最后一行。"""
    last = ws.max_row or 1
    max_col = max(1, ws.max_column or 1)
    while last > 1 and all(ws.cell(last, col).value in (None, "") for col in range(1, max_col + 1)):
        last -= 1
    return last


def merged_range_at(ws: Worksheet, row: int, col: int) -> str | None:
    for rng in ws.merged_cells.ranges:
        if rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col:
            return str(rng)
    return None


def _score_header_row(ws: Worksheet, row: int, max_col: int) -> float:
    values = [ws.cell(row, col).value for col in range(1, max_col + 1)]
    non_empty = [v for v in values if v not in (None, "")]
    if not non_empty:
        return -1.0
    text_like = sum(1 for v in non_empty if isinstance(v, str) and not _looks_numeric(v))
    ratio = text_like / len(non_empty)
    # 列多、文本多、越靠前越像表头；单格合并标题会被"非空数量"压低
    return len(non_empty) * 2 + ratio * 5 - row * 0.1


def _looks_numeric(value: str) -> bool:
    text = value.strip().replace(",", "")
    try:
        float(text)
        return True
    except ValueError:
        return False


def detect_header_row(ws: Worksheet, max_scan: int = 10) -> int:
    max_col = min(max(1, ws.max_column or 1), 40)
    last = min(max(1, ws.max_row or 1), max_scan)
    best_row, best_score = 1, float("-inf")
    for row in range(1, last + 1):
        score = _score_header_row(ws, row, max_col)
        if score > best_score:
            best_row, best_score = row, score
    return best_row


def _read_csv(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    text = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "utf-16"):
        try:
            text = raw.decode(encoding)
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if text is None:
        text = raw.decode("utf-8", errors="ignore")

    sample = text[:2000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [row for row in csv.reader(io.StringIO(text), dialect)]


def _inspect_csv(path: Path) -> TableInfo:
    rows = _read_csv(path)
    warnings: list[str] = []
    if not rows:
        raise ValueError("CSV 文件为空")
    header_row = 1
    headers: list[ColumnInfo] = []
    for idx, value in enumerate(rows[0]):
        header = (value or "").strip()
        if not header:
            warnings.append(f"第 {idx + 1} 列表头为空，已跳过")
            continue
        samples = [_cell_text(r[idx]) for r in rows[1:6] if idx < len(r) and _cell_text(r[idx])]
        headers.append(ColumnInfo(index=idx, header=header, letter=get_column_letter(idx + 1),
                                  sample_values=samples[:5]))
    preview = [[_cell_text(c) for c in row[: max(1, len(rows[0]))]] for row in rows[1:6]]
    return TableInfo(
        path=path, kind="csv", sheet_names=["CSV"], sheet="CSV", header_row=header_row,
        headers=headers, preview=preview, row_count=max(0, len(rows) - 1),
        data_last_row=len(rows), warnings=warnings,
    )


def inspect_table(path: str | Path, sheet: str | None = None, header_row: int | None = None) -> TableInfo:
    path = Path(path)
    if path.suffix.lower() == ".csv":
        return _inspect_csv(path)

    wb = load_workbook(path, data_only=True)
    try:
        sheet_names = list(wb.sheetnames)
        if not sheet_names:
            raise ValueError("工作簿中没有工作表")
        ws = wb[sheet] if sheet and sheet in sheet_names else wb[sheet_names[0]]
        hrow = header_row or detect_header_row(ws)
        hrow = max(1, min(int(hrow), max(1, ws.max_row or 1)))

        warnings: list[str] = []
        data_last = max(hrow, effective_last_row(ws))
        headers: list[ColumnInfo] = []
        max_col = max(1, ws.max_column or 1)

        for col in range(1, max_col + 1):
            cell = ws.cell(hrow, col)
            header = _cell_text(cell.value)
            if not header:
                if data_last > hrow:
                    warnings.append(f"第 {col} 列表头为空，已跳过该列")
                continue

            merged = merged_range_at(ws, hrow, col)
            if merged and cell.value is not None:
                # 合并表头只在左上角列生效，其余列会被 openpyxl 读成 None
                pass

            samples = []
            for row in range(hrow + 1, min(data_last, hrow + 5) + 1):
                value = _cell_text(ws.cell(row, col).value)
                if value:
                    samples.append(value)

            headers.append(ColumnInfo(index=col - 1, header=header, letter=get_column_letter(col),
                                      merged=bool(merged), sample_values=samples[:5]))

        if not headers:
            raise ValueError(f"第 {hrow} 行未识别到有效表头")

        preview: list[list[str]] = []
        for row in range(hrow + 1, min(data_last, hrow + 5) + 1):
            preview.append([_cell_text(ws.cell(row, col).value) for col in range(1, max_col + 1)])

        merged_headers = [c for c in headers if c.merged]
        for col in merged_headers[:3]:
            warnings.append(f"表头「{col.header}」位于合并单元格，仅映射到该合并区左上角列")

        return TableInfo(
            path=path, kind="xlsx", sheet_names=sheet_names, sheet=ws.title, header_row=hrow,
            headers=headers, preview=preview, row_count=max(0, data_last - hrow),
            data_last_row=data_last, warnings=warnings,
        )
    finally:
        wb.close()
