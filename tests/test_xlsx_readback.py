from datetime import datetime
import zipfile

import pytest

from su_crawler.xlsx_readback import (RecordingWorkbook, WorkbookMismatch, expected_from_rows, verify_cells)
from su_crawler.xlsx_sheets import SheetSpec, replace_sheets

TRICKY = ["_x0041_", "a\r\nb", " lead", "trail ", "=1+1", "x_x005F_y", "\t", "한글 ₩19,900"]


def _write(path, build):
    wb = RecordingWorkbook(path, {"strings_to_formulas": False, "strings_to_urls": False})
    ws = wb.add_worksheet("S")
    build(ws, wb)
    expected = wb.expected_cells()
    wb.close()
    return expected


def test_every_written_value_reads_back_as_excel_shows_it(tmp_path):
    path = tmp_path / "ok.xlsx"

    def build(ws, wb):
        ws.merge_range(0, 0, 0, 3, "Banner")
        for row, text in enumerate(TRICKY, 1):
            ws.write_string(row, 0, text)
        ws.write(1, 1, "Header")
        ws.write_number(1, 2, 0.1 + 0.2)
        ws.write_number(2, 2, 123456789012345)
        ws.write_boolean(3, 2, False)
        ws.write_datetime(4, 2, datetime(2026, 10, 26, 1, 2, 3, 456789), wb.add_format({"num_format": "yyyy-mm-dd"}))
        ws.write_url(5, 2, "https://example.test/a?b=1", None, "shown text")
        ws.write_blank(6, 2, None, wb.add_format({"bold": True}))
        ws.write_string(7, 2, "")
    verify_cells(path, _write(path, build))


def test_a_changed_cell_is_reported_with_its_location(tmp_path):
    path = tmp_path / "changed.xlsx"
    expected = _write(path, lambda ws, wb: [ws.write_string(0, 0, "Name"), ws.write_string(2, 1, "₩19,900")])
    tampered = tmp_path / "tampered.xlsx"
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(tampered, "w") as target:
        for info in source.infolist():
            data = source.read(info)
            if info.filename == "xl/sharedStrings.xml":
                data = data.replace("₩19,900".encode(), "₩19,000".encode())
            target.writestr(info, data)
    with pytest.raises(WorkbookMismatch, match=r"'S' cell B3: wrote '₩19,900', read back '₩19,000'"):
        verify_cells(tampered, expected)


def test_a_cell_the_writer_refused_is_not_published_as_a_gap(tmp_path):
    path = tmp_path / "limit.xlsx"
    expected = _write(path, lambda ws, wb: ws.write_string(1_048_576, 0, "beyond Excel's last row"))
    with pytest.raises(WorkbookMismatch, match="A1048577: wrote .* read back nothing"):
        verify_cells(path, expected)


def test_an_unwritten_value_and_a_missing_sheet_are_mismatches(tmp_path):
    path = tmp_path / "extra.xlsx"
    _write(path, lambda ws, wb: [ws.write_string(0, 0, "a"), ws.write_number(0, 1, 7)])
    with pytest.raises(WorkbookMismatch, match="B1: wrote None, read back 7.0"):
        verify_cells(path, [("S", expected_from_rows([(1, 1, "a")]))])
    with pytest.raises(WorkbookMismatch, match="sheets read back as"):
        verify_cells(path, [("S", expected_from_rows([(1, 1, "a"), (1, 2, 7)])), ("T", expected_from_rows([]))])


def test_export_keeps_no_file_when_the_workbook_does_not_read_back(tmp_path, monkeypatch):
    from xlsxwriter.worksheet import Worksheet
    from test_report import _config
    from su_crawler.report import export_report

    original = Worksheet._write_string

    def corrupt(self, row, col, string, cell_format=None):
        # A writer fault: the stored text differs from the requested text.
        return original(self, row, col, string.replace("Item", "Itme"), cell_format)
    monkeypatch.setattr(Worksheet, "_write_string", corrupt)
    row = {"id": "o1", "task_id": "t", "product_id": "p1", "source_id": "s1", "status": "review",
           "raw_fields": {"name": "Item", "price": "12"}}
    with pytest.raises(WorkbookMismatch, match="wrote 'Item', read back 'Itme'"):
        export_report(_config(tmp_path), {"id": "r"}, [], [row], tmp_path / "out.xlsx")
    assert not list(tmp_path.glob("out*.xlsx"))


@pytest.mark.parametrize("route", ["splice", "openpyxl"])
def test_replaced_sheets_keep_escape_like_text_literal(tmp_path, monkeypatch, route):
    from su_crawler import xlsx_sheets

    def refuse(*_args, **_kwargs):
        if route == "splice":
            raise AssertionError("whole workbook was reloaded")
        raise xlsx_sheets.SpliceUnsupported("synthetic")
    monkeypatch.setattr(xlsx_sheets, "_replace_openpyxl" if route == "splice" else "_splice", refuse)
    path = tmp_path / "splice.xlsx"
    _write(path, lambda ws, wb: ws.write_string(0, 0, "kept"))
    spec = SheetSpec("Audit", [["Text", "Count"], *[[text, index] for index, text in enumerate(TRICKY)]])
    replace_sheets(path, [spec])  # verified inside before the file is replaced
    verify_cells(path, [("Audit", expected_from_rows((r, c, v) for r, c, v, _ in spec.cells()))], all_sheets=False)
