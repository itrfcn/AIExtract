"""表格读取与导出：表头定位、格式保留、合并单元格、原文件不被修改。"""

from __future__ import annotations

import hashlib
from datetime import date

import pytest
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from pipeline.export import export_rows
from pipeline.schema import map_headers
from pipeline.workbook import inspect_table, merged_range_at


@pytest.fixture()
def styled_xlsx(tmp_path):
    path = tmp_path / "styled.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "人员信息"
    ws.merge_cells("A1:G1")
    ws["A1"] = "员工信息登记表"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A1"].alignment = Alignment(horizontal="center")

    headers = ["姓名", "年龄", "性别", "兴趣爱好", "出生日期", "公司", "备注"]
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col, value=header)
        cell.font = Font(bold=True, color="1F4E79")
        cell.fill = PatternFill("solid", fgColor="D9E2F3")

    ws.append(["陈晨", 29, "女", "瑜伽、阅读", date(1996, 5, 18), "云启科技", "已入职"])
    ws["E3"].number_format = "yyyy-mm-dd"
    ws["A3"].fill = PatternFill("solid", fgColor="FFF2CC")
    ws["A2"].alignment = Alignment(horizontal="center")
    ws.column_dimensions["A"].width = 14
    ws.freeze_panes = "A3"

    other = wb.create_sheet("填写说明")
    other["A1"] = "不要动这个 sheet"
    wb.save(path)
    return path


def test_inspect_detects_header_and_columns(styled_xlsx):
    info = inspect_table(styled_xlsx)
    assert info.sheet == "人员信息"
    assert info.sheet_names == ["人员信息", "填写说明"]
    assert info.header_row == 2, "合并标题行不应被当成表头"
    assert [c.header for c in info.headers] == ["姓名", "年龄", "性别", "兴趣爱好", "出生日期", "公司", "备注"]
    assert info.row_count == 1
    assert info.headers[0].sample_values == ["陈晨"]


def test_export_preserves_format_and_original(styled_xlsx, tmp_path):
    original_hash = hashlib.sha256(styled_xlsx.read_bytes()).hexdigest()
    specs = map_headers(["姓名", "年龄", "性别", "兴趣爱好", "出生日期", "公司", "备注"],
                        use_llm=False)
    rows = [{"name": "张伟", "age": 32, "gender": "男", "hobbies": ["徒步", "摄影"],
             "birth_date": "1993-04-12", "company": "字节跳动", "notes": "产品经理"}]

    result = export_rows(styled_xlsx, "人员信息", 2, specs, rows, out_dir=tmp_path / "out")

    assert result.path.exists()
    assert result.written_rows == 1
    assert result.written_cells == 7
    assert hashlib.sha256(styled_xlsx.read_bytes()).hexdigest() == original_hash, "原文件必须保持不变"

    wb = load_workbook(result.path, data_only=False)
    ws = wb["人员信息"]

    # 合并标题仍然存在
    assert "A1:G1" in {str(r) for r in ws.merged_cells.ranges}
    assert merged_range_at(ws, 1, 1) == "A1:G1"

    # 其他 sheet 未被破坏
    assert wb["填写说明"]["A1"].value == "不要动这个 sheet"

    # 新行写到了第 4 行，且继承了模板行样式
    assert ws["A4"].value == "张伟"
    assert ws["B4"].value == 32
    assert ws["D4"].value == "徒步、摄影"
    assert ws["E4"].value.date() == date(1993, 4, 12)
    assert ws["E4"].number_format == "yyyy-mm-dd"
    assert ws["A4"].fill.fgColor.rgb == ws["A3"].fill.fgColor.rgb
    wb.close()


def test_export_skips_merged_target_cell(tmp_path):
    path = tmp_path / "merged.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["姓名", "年龄"])
    ws.append(["陈晨", 29])
    ws.merge_cells("A3:B3")  # 数据区下方预留的空合并区，追加行会落在这里
    wb.save(path)
    wb.close()

    specs = map_headers(["姓名", "年龄"], use_llm=False)
    result = export_rows(path, "S", 1, specs, [{"name": "张伟", "age": 32}], out_dir=tmp_path / "out")

    assert result.skipped_cells == 2
    assert result.written_cells == 0
    assert any("合并单元格" in w for w in result.warnings)
    written = load_workbook(result.path)
    assert "A3:B3" in {str(r) for r in written["S"].merged_cells.ranges}
    assert written["S"]["A3"].value is None
    written.close()
