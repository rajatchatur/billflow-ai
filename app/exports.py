"""CSV exports. Only approved invoices are exported; exports never post to accounting software."""
from __future__ import annotations

import csv
import io
from typing import Any

INVOICE_COLUMNS = [
    "invoice_id", "client_company", "supplier_name", "supplier_gstin", "buyer_name", "buyer_gstin",
    "invoice_number", "invoice_date", "due_date", "place_of_supply", "subtotal", "discount",
    "taxable_value", "cgst", "sgst", "igst", "cess", "rounding", "total_amount", "status",
]
LINE_COLUMNS = [
    "invoice_id", "invoice_number", "line_no", "description", "hsn_sac", "quantity",
    "unit_price", "gst_rate", "taxable_value", "tax_amount", "line_total",
]


def _csv(columns: list[str], rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({c: ("" if row.get(c) is None else guard_csv_cell(row.get(c))) for c in columns})
    return buf.getvalue()


def invoices_csv(rows: list[dict[str, Any]]) -> str:
    return _csv(INVOICE_COLUMNS, rows)


def line_items_csv(rows: list[dict[str, Any]]) -> str:
    return _csv(LINE_COLUMNS, rows)


def guard_csv_cell(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in text cells."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value
