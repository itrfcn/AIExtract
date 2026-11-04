"""回填导出：openpyxl 追加行、继承样式、保留合并单元格，另存新文件。"""

from __future__ import annotations

import csv
import io
from copy import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from openpyxl import load_workbook

from core.config import settings
from .normalize import normalize_for_export
from .schema import FieldSpec
from .workbook import effective_last_row, merged_range_at


@dataclass
class ExportResult:
    path: Path
    written_rows: int = 0
    written_cells: int = 0
    skipped_cells: int = 0
    warnings: list[str] = field(default_factory=list)


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = directory / f"{stem}_filled_{stamp}{suffix}"
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem}_filled_{stamp}_{index}{suffix}"
        index += 1
    return candidate


def _export_xlsx(
    table_path: Path,
    sheet: str,
    header_row: int,
    specs: list[FieldSpec],
    rows: list[dict],
    out_dir: Path,
) -> ExportResult:
    wb = load_workbook(table_path, data_only=False, keep_vba=table_path.suffix.lower() == ".xlsm")
    try:
        if sheet not in wb.sheetnames:
            raise ValueError(f"工作表不存在：{sheet}")
        ws = wb[sheet]

        last_row = effective_last_row(ws)
        start_row = max(last_row + 1, header_row + 1)
        has_template = last_row > header_row
        template_row = last_row if has_template else None
        warnings: list[str] = []
        written_cells = 0
        skipped_cells = 0
        written_rows = 0

        for offset, record in enumerate(rows):
            row_index = start_row + offset
            row_written = False
            for spec in specs:
                if not spec.enabled:
                    continue
                value = normalize_for_export(record.get(spec.key), spec)
                if value is None:
                    continue

                col_index = spec.column_index + 1
                merged = merged_range_at(ws, row_index, col_index)
                if merged:
                    skipped_cells += 1
                    warnings.append(f"第 {row_index} 行第 {col_index} 列位于合并单元格 {merged}，已跳过")
                    continue

                dst = ws.cell(row=row_index, column=col_index)
                if template_row:
                    src = ws.cell(row=template_row, column=col_index)
                    try:
                        dst._style = copy(src._style)
                    except Exception:
                        pass

                if spec.type == "date" and isinstance(value, date):
                    dst.value = value
                    dst.number_format = "yyyy-mm-dd"
                elif spec.type == "date" and isinstance(value, str):
                    dst.value = value
                    dst.number_format = "yyyy-mm" if len(value) == 7 else "@"
                elif isinstance(value, (int, float)) and not isinstance(value, bool):
                    dst.value = value
                    if template_row:
                        src_style = ws.cell(row=template_row, column=col_index)
                        if src_style.number_format and "%" in src_style.number_format:
                            dst.number_format = src_style.number_format
                else:
                    dst.value = str(value)
                    if template_row:
                        dst.number_format = ws.cell(row=template_row, column=col_index).number_format
                written_cells += 1
                row_written = True
            if row_written:
                written_rows += 1

        if hasattr(wb, "calculation"):
            try:
                wb.calculation.fullCalcOnLoad = True
            except Exception:
                pass

        out_path = _unique_path(out_dir, table_path.stem, table_path.suffix)
        wb.save(out_path)
        return ExportResult(
            path=out_path, written_rows=written_rows, written_cells=written_cells,
            skipped_cells=skipped_cells, warnings=sorted(set(warnings))[:20],
        )
    finally:
        wb.close()


def _read_csv_rows(path: Path) -> list[list[str]]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig", errors="ignore")
    try:
        dialect = csv.Sniffer().sniff(text[:2000], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [list(row) for row in csv.reader(io.StringIO(text), dialect)]


def _export_csv(
    table_path: Path, header_row: int, specs: list[FieldSpec], rows: list[dict], out_dir: Path
) -> ExportResult:
    existing = _read_csv_rows(table_path)
    header = existing[0] if existing else []
    out_rows = existing[: max(header_row, len(existing))]
    written_cells = 0
    for record in rows:
        row = [""] * len(header)
        for spec in specs:
            if not spec.enabled:
                continue
            value = normalize_for_export(record.get(spec.key), spec)
            if value is None:
                continue
            if spec.column_index >= len(row):
                row.extend([""] * (spec.column_index + 1 - len(row)))
            row[spec.column_index] = str(value)
            written_cells += 1
        out_rows.append(row)

    out_path = _unique_path(out_dir, table_path.stem, ".csv")
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        csv.writer(fh).writerows(out_rows)

    return ExportResult(path=out_path, written_rows=len(rows), written_cells=written_cells,
                        warnings=["CSV 无格式可继承，已按原表头另存为新 CSV"])


def export_rows(
    table_path: str | Path,
    sheet: str,
    header_row: int,
    specs: list[FieldSpec],
    rows: list[dict],
    out_dir: str | Path | None = None,
) -> ExportResult:
    """把结果追加到表格并另存新文件，绝不覆盖原文件。"""
    if not rows:
        raise ValueError("没有可导出的结果行")

    table_path = Path(table_path)
    out_dir = Path(out_dir) if out_dir else settings.output_dir
    if table_path.suffix.lower() == ".csv":
        return _export_csv(table_path, header_row, specs, rows, out_dir)
    if table_path.suffix.lower() not in (".xlsx", ".xlsm"):
        raise ValueError(f"不支持导出到 {table_path.suffix}，请使用 xlsx/csv")
    return _export_xlsx(table_path, sheet, header_row, specs, rows, out_dir)


def export_new_table(
    headers: list[str],
    specs: list[FieldSpec],
    rows: list[dict],
    out_dir: str | Path | None = None,
    stem: str = "extraction",
) -> ExportResult:
    """手动表头模式：直接创建新工作簿写表头+数据，不依赖任何原表。"""
    from openpyxl import Workbook

    if not rows:
        raise ValueError("没有可导出的结果行")

    out_dir = Path(out_dir) if out_dir else settings.output_dir
    wb = Workbook()
    ws = wb.active
    ws.title = "抽取结果"

    # 1. 写表头行
    for col_idx, header in enumerate(headers, 1):
        ws.cell(row=1, column=col_idx, value=header)

    # 2. 写数据行
    col_index = {s.key: s.column_index for s in specs}
    written_cells = 0
    written_rows = 0
    for r_offset, record in enumerate(rows):
        row_written = False
        for spec in specs:
            if not spec.enabled:
                continue
            value = normalize_for_export(record.get(spec.key), spec)
            if value is None:
                continue
            dst = ws.cell(row=2 + r_offset, column=spec.column_index + 1)
            if spec.type == "date" and isinstance(value, date):
                dst.value = value
                dst.number_format = "yyyy-mm-dd"
            elif spec.type == "date" and isinstance(value, str):
                dst.value = value
                dst.number_format = "yyyy-mm" if len(value) == 7 else "@"
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                dst.value = value
            else:
                dst.value = str(value)
            written_cells += 1
            row_written = True
        if row_written:
            written_rows += 1

    out_path = _unique_path(out_dir, stem, ".xlsx")
    wb.save(out_path)
    wb.close()

    return ExportResult(path=out_path, written_rows=written_rows, written_cells=written_cells,
                        skipped_cells=0, warnings=["手动新建表导出，无原表样式可继承"])
