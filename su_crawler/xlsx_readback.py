"""Re-read a written workbook and compare every cell with what was written.

A report is published only after its staged file reads back as intended:
the same sheets in the same order, the same value in every written cell, and
no value the writer did not put there. The worksheet XML is read the way
Excel reads it (shared and inline strings, ``_xHHHH_`` escapes, numbers),
not through openpyxl, whose reader drops part of that escaping.
"""
from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
import math
from pathlib import Path
from posixpath import join as _pjoin, normpath as _pnormpath
import re
from typing import Any, Iterable
from xml.etree import ElementTree
import zipfile

from xlsxwriter.workbook import Workbook
from xlsxwriter.worksheet import Worksheet

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")
_REF = re.compile(r"([A-Z]+)(\d+)")
_SEEN = object()  # an expected cell already matched once


class WorkbookMismatch(ValueError):
    """The staged workbook does not hold what the writer wrote."""


def excel_literal(text: str) -> str:
    """Escape text for a worksheet XML part so Excel reads back exactly ``text``.

    Text shaped like an ``_xHHHH_`` escape would be decoded, and a carriage
    return would be folded into a line feed by the XML parser.
    """
    return _ESCAPE.sub(r"_x005F_x\1_", text).replace("\r", "_x000D_")


def _unescape(text: str) -> str:
    return _ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)


def _scalar(value: Any) -> Any:
    """The value a cell read back holds: text, float, bool or None for blank."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    return value


class _Expected:
    """Cells of one sheet as zero-based rows of column lists."""

    def __init__(self) -> None:
        self.rows: dict[int, list[Any]] = {}

    def put(self, row: int, col: int, value: Any) -> None:
        cells = self.rows.setdefault(row, [])
        if len(cells) <= col:
            cells.extend([None] * (col + 1 - len(cells)))
        cells[col] = _scalar(value)


class RecordingWorksheet(Worksheet):
    """An xlsxwriter worksheet that keeps the value of every cell it writes.

    Only the plain write calls the exporters use are recorded. A value that
    xlsxwriter refuses (row out of range, text over the cell limit) stays
    expected, so the read-back reports it instead of publishing a gap.
    """

    def __init__(self) -> None:
        super().__init__()
        self.expected = _Expected()

    def write(self, row, col, *args):
        token = args[0] if args else None
        if token is None or isinstance(token, str):
            if not token:
                return self.write_blank(row, col, None, *args[1:])
            return self.write_string(row, col, *args)
        if isinstance(token, bool):
            return self.write_boolean(row, col, *args)
        if isinstance(token, (int, float, Decimal)):
            return self.write_number(row, col, *args)
        if isinstance(token, (datetime, date, time)):
            return self.write_datetime(row, col, *args)
        raise TypeError(f"unsupported cell value for a verified workbook: {type(token).__name__}")

    def write_string(self, row, col, string, cell_format=None):
        self.expected.put(row, col, string)
        return super().write_string(row, col, string, cell_format)

    def write_number(self, row, col, number, cell_format=None):
        self.expected.put(row, col, number)
        return super().write_number(row, col, number, cell_format)

    def write_boolean(self, row, col, boolean, cell_format=None):
        self.expected.put(row, col, bool(boolean))
        return super().write_boolean(row, col, boolean, cell_format)

    def write_blank(self, row, col, blank, cell_format=None):
        self.expected.put(row, col, None)
        return super().write_blank(row, col, blank, cell_format)

    def write_datetime(self, row, col, date, cell_format=None):
        # Excel stores the date as its serial number; expect that number.
        self.expected.put(row, col, self._convert_date_time(date))
        return super().write_datetime(row, col, date, cell_format)

    def write_url(self, row, col, url, cell_format=None, string=None, tip=None):
        result = super().write_url(row, col, url, cell_format, string, tip)
        if result >= 0:  # A refused link writes nothing; callers fall back to text.
            self.expected.put(row, col, url if string is None else string)
        return result

    def merge_range(self, first_row, first_col, last_row, last_col, data, cell_format=None):
        result = super().merge_range(first_row, first_col, last_row, last_col, data, cell_format)
        for row in range(first_row, last_row + 1):
            for col in range(first_col, last_col + 1):
                self.expected.put(row, col, None)
        self.expected.put(first_row, first_col, data)
        return result


class RecordingWorkbook(Workbook):
    """An xlsxwriter workbook whose sheets record what they write."""

    worksheet_class = RecordingWorksheet

    def expected_cells(self) -> list[tuple[str, _Expected]]:
        return [(sheet.name, sheet.expected) for sheet in self.worksheets()]


def expected_from_rows(rows: Iterable[tuple[int, int, Any]]) -> _Expected:
    """Expected cells from one-based (row, column, value) triples."""
    expected = _Expected()
    for row, col, value in rows:
        expected.put(row - 1, col - 1, value)
    return expected


def _part(target: str) -> str:
    return target[1:] if target.startswith("/") else _pnormpath(_pjoin("xl", target))


def _text(node: ElementTree.Element) -> str:
    # Rich-text runs concatenate; phonetic hints (rPh) are not cell text.
    if node.tag == _MAIN + "t":
        return node.text or ""
    return "".join(_text(child) for child in node if child.tag != _MAIN + "rPh")


def _sheets(archive: zipfile.ZipFile) -> tuple[list[tuple[str, str]], list[str]]:
    rels = ElementTree.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {rel.get("Id"): rel for rel in rels.iter(_PKG_REL + "Relationship")}
    workbook = ElementTree.fromstring(archive.read("xl/workbook.xml"))
    sheets = []
    for sheet in workbook.iter(_MAIN + "sheet"):
        rel = targets.get(sheet.get(_REL + "id"))
        if rel is None:
            raise WorkbookMismatch(f"sheet {sheet.get('name')!r} has no worksheet part")
        sheets.append((sheet.get("name"), _part(rel.get("Target"))))
    strings: list[str] = []
    for rel in targets.values():
        if rel.get("Type", "").endswith("/sharedStrings"):
            with archive.open(_part(rel.get("Target"))) as stream:
                for _, node in ElementTree.iterparse(stream):
                    if node.tag == _MAIN + "si":
                        strings.append(_unescape(_text(node)))
                        node.clear()
    return sheets, strings


def _column(letters: str) -> int:
    index = 0
    for letter in letters:
        index = index * 26 + ord(letter) - 64
    return index - 1


def _cell_value(node: ElementTree.Element, strings: list[str]) -> Any:
    kind = node.get("t", "n")
    if node.find(_MAIN + "f") is not None:
        return ("formula", node.findtext(_MAIN + "f"))
    if kind == "inlineStr":
        inline = node.find(_MAIN + "is")
        return _scalar(_unescape(_text(inline))) if inline is not None else None
    raw = node.findtext(_MAIN + "v")
    if raw is None:
        return None
    if kind == "s":
        return _scalar(strings[int(raw)])
    if kind == "b":
        return raw == "1"
    if kind in {"str", "e"}:
        return (kind, raw)
    return float(raw)


def _same(expected: Any, actual: Any) -> bool:
    if isinstance(expected, float) and isinstance(actual, float):
        # xlsxwriter stores 16 significant digits.
        return math.isclose(actual, expected, rel_tol=1e-15, abs_tol=0.0)
    return type(expected) is type(actual) and expected == actual


def _short(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _ref(row: int, col: int) -> str:
    letters, col = "", col + 1
    while col:
        col, rest = divmod(col - 1, 26)
        letters = chr(65 + rest) + letters
    return f"{letters}{row + 1}"


def _cells(stream, name: str, strings: list[str]):
    """Yield zero-based (row, column, value) for every cell element."""
    for _, node in ElementTree.iterparse(stream):
        if node.tag == _MAIN + "row":
            node.clear()
            continue
        if node.tag != _MAIN + "c":
            continue
        match = _REF.fullmatch(node.get("r") or "")
        if match is None:
            raise WorkbookMismatch(f"sheet {name!r}: cell without a plain reference")
        yield int(match.group(2)) - 1, _column(match.group(1)), _cell_value(node, strings)


def _compare_sheet(stream, name: str, expected: _Expected, strings: list[str]) -> None:
    rows = expected.rows
    for row, col, got in _cells(stream, name, strings):
        cells = rows.get(row)
        want = cells[col] if cells is not None and col < len(cells) else None
        if want is _SEEN:
            raise WorkbookMismatch(f"sheet {name!r} cell {_ref(row, col)} appears twice")
        if want is None and got is None:
            continue
        if want is None or got is None or not _same(want, got):
            raise WorkbookMismatch(f"sheet {name!r} cell {_ref(row, col)}: wrote {_short(want)}, "
                                   f"read back {_short(got)}")
        cells[col] = _SEEN
    for row, cells in rows.items():
        for col, want in enumerate(cells):
            if want is not None and want is not _SEEN:
                raise WorkbookMismatch(f"sheet {name!r} cell {_ref(row, col)}: wrote {_short(want)}, "
                                       f"read back nothing")


def read_values(path: str | Path) -> dict[str, dict[int, list[Any]]]:
    """Each sheet's values as Excel shows them, as zero-based rows of columns.

    Text is unescaped, numbers (dates included) are floats and a formula is
    ``("formula", text)``.
    """
    values: dict[str, dict[int, list[Any]]] = {}
    with zipfile.ZipFile(path) as archive:
        sheets, strings = _sheets(archive)
        for name, part in sheets:
            cells = _Expected()
            with archive.open(part) as stream:
                for row, col, value in _cells(stream, name, strings):
                    cells.put(row, col, value)
            values[name] = cells.rows
    return values


def verify_cells(path: str | Path, expected: list[tuple[str, _Expected]], *, all_sheets: bool = True) -> None:
    """Raise WorkbookMismatch unless ``path`` reads back exactly as ``expected``.

    With ``all_sheets`` the workbook must hold exactly these sheets in this
    order; otherwise only the named sheets are compared and must exist.
    Expected cells are consumed by the comparison.
    """
    with zipfile.ZipFile(path) as archive:
        sheets, strings = _sheets(archive)
        names = [name for name, _ in sheets]
        wanted = [name for name, _ in expected]
        if all_sheets and names != wanted:
            raise WorkbookMismatch(f"sheets read back as {names}, wrote {wanted}")
        parts = dict(sheets)
        for name, cells in expected:
            if name not in parts:
                raise WorkbookMismatch(f"sheet {name!r} is missing")
            with archive.open(parts[name]) as stream:
                _compare_sheet(stream, name, cells, strings)
