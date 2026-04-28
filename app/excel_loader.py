from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zipfile import ZipFile
import xml.etree.ElementTree as ET

NS = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


def _excel_serial_to_iso(value: float) -> str:
    base = datetime(1899, 12, 30)
    return (base + timedelta(days=float(value))).date().isoformat()


def _coerce_value(header: str, value: str | None):
    if value is None:
        return None

    numeric_headers = {
        "id",
        "supplier_id",
        "product_id",
        "purchase_order_id",
        "quantity",
        "price_per_unit",
    }
    date_headers = {"delivery_date"}

    if header in numeric_headers:
        try:
            number = float(value)
            if number.is_integer():
                return int(number)
            return number
        except ValueError:
            return value

    if header in date_headers:
        try:
            return _excel_serial_to_iso(float(value))
        except ValueError:
            return value

    if value.endswith(".0"):
        try:
            number = float(value)
            if number.is_integer():
                return int(number)
        except ValueError:
            pass

    return value


def load_xlsx_tables(path: str | Path) -> dict[str, list[dict]]:
    path = Path(path)
    with ZipFile(path) as archive:
        names = archive.namelist()

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in names:
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for si in root.findall("a:si", NS):
                shared_strings.append("".join(t.text or "" for t in si.iterfind(".//a:t", NS)))

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
        rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}

        tables: dict[str, list[dict]] = {}

        for sheet in workbook.find("a:sheets", NS):
            name = sheet.attrib["name"]
            rel_id = sheet.attrib["{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"]
            target = "xl/" + rel_map[rel_id]
            xml_root = ET.fromstring(archive.read(target))

            parsed_rows: list[dict[str, str | None]] = []
            max_col = 0

            for row in xml_root.findall(".//a:sheetData/a:row", NS):
                parsed_row: dict[str, str | None] = {}
                for cell in row.findall("a:c", NS):
                    ref = cell.attrib.get("r", "")
                    col = "".join(ch for ch in ref if ch.isalpha())
                    cell_type = cell.attrib.get("t")
                    v = cell.find("a:v", NS)
                    value = v.text if v is not None else None

                    if cell_type == "s" and value is not None:
                        value = shared_strings[int(value)]
                    elif cell_type == "inlineStr":
                        inline = cell.find("a:is", NS)
                        value = "".join(t.text or "" for t in inline.iterfind(".//a:t", NS)) if inline is not None else None

                    parsed_row[col] = value
                    col_number = 0
                    for ch in col:
                        col_number = col_number * 26 + ord(ch) - 64
                    max_col = max(max_col, col_number)

                parsed_rows.append(parsed_row)

            if not parsed_rows:
                tables[name] = []
                continue

            headers: list[str | None] = []
            for idx in range(1, max_col + 1):
                col = ""
                current = idx
                while current:
                    current, remainder = divmod(current - 1, 26)
                    col = chr(65 + remainder) + col
                headers.append(parsed_rows[0].get(col))

            rows: list[dict] = []
            for parsed_row in parsed_rows[1:]:
                row: dict = {}
                has_nonempty = False
                for idx, header in enumerate(headers, start=1):
                    if not header:
                        continue
                    col = ""
                    current = idx
                    while current:
                        current, remainder = divmod(current - 1, 26)
                        col = chr(65 + remainder) + col
                    value = parsed_row.get(col)
                    coerced = _coerce_value(header, value)
                    row[header] = coerced
                    has_nonempty = has_nonempty or coerced is not None
                if has_nonempty:
                    rows.append(row)

            tables[name] = rows

    return tables
