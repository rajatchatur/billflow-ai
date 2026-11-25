"""Deterministic GST and invoice checks.

A format and arithmetic check is NOT verification against the GST portal.
Nothing here contacts GSTN or any government service.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

GSTIN_RE = re.compile(r"^(\d{2})([A-Z]{5}\d{4}[A-Z])([1-9A-Z])Z([0-9A-Z])$")
VALID_STATE_CODES = {f"{i:02d}" for i in range(1, 39)} - {"28"}  # 28 unused; 38 = Ladakh (newer)
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _gstin_checksum(first14: str) -> str:
    total = 0
    for i, ch in enumerate(first14):
        value = _GSTIN_CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += value // 36 + value % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36]


def check_gstin(gstin: str | None) -> list[str]:
    """Return a list of problems; an empty list means the format and checksum are consistent."""
    if not gstin:
        return ["GSTIN is missing"]
    g = gstin.strip().upper()
    if len(g) != 15:
        return [f"GSTIN must be 15 characters (found {len(g)})"]
    m = GSTIN_RE.match(g)
    if not m:
        return ["GSTIN format is invalid (expected 2-digit state code, PAN, entity code, 'Z', checksum)"]
    if m.group(1) not in VALID_STATE_CODES:
        return [f"GSTIN state code {m.group(1)} is not a recognised Indian state code"]
    if _gstin_checksum(g[:14]) != g[14]:
        return ["GSTIN checksum character does not match (possible typo)"]
    return []


def gstin_state_code(gstin: str | None) -> str | None:
    if gstin and len(gstin) >= 2 and gstin[:2].isdigit():
        return gstin[:2]
    return None


def to_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").strip()).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def parse_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalise_invoice_number(number: str | None) -> str | None:
    if not number:
        return None
    cleaned = re.sub(r"[^A-Za-z0-9]", "", number).upper()
    return cleaned or None


@dataclass
class Issue:
    code: str
    severity: str  # "error" | "warning" | "info"
    field: str | None
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_invoice(data: dict[str, Any], tolerance: Decimal, today: date | None = None) -> list[Issue]:
    """Validate a (possibly corrected) invoice dict. Missing values are reported, never filled in."""
    today = today or date.today()
    issues: list[Issue] = []

    def add(code: str, severity: str, field: str | None, message: str) -> None:
        issues.append(Issue(code, severity, field, message))

    if not data.get("invoice_number"):
        add("INVOICE_NUMBER_MISSING", "error", "invoice_number", "Invoice number is missing.")
    inv_date = parse_date(data.get("invoice_date"))
    if data.get("invoice_date") in (None, ""):
        add("INVOICE_DATE_MISSING", "error", "invoice_date", "Invoice date is missing.")
    elif inv_date is None:
        add("INVOICE_DATE_INVALID", "error", "invoice_date", "Invoice date could not be read as a valid date.")
    elif inv_date > today:
        add("INVOICE_DATE_FUTURE", "warning", "invoice_date", "Invoice date is in the future.")

    due = parse_date(data.get("due_date"))
    if inv_date and due and due < inv_date:
        add("DUE_DATE_BEFORE_INVOICE", "warning", "due_date", "Due date is earlier than the invoice date.")

    if not data.get("supplier_name"):
        add("SUPPLIER_NAME_MISSING", "error", "supplier_name", "Supplier name is missing.")
    if not data.get("buyer_name"):
        add("BUYER_NAME_MISSING", "warning", "buyer_name", "Buyer name is missing.")

    for field, label in (("supplier_gstin", "Supplier"), ("buyer_gstin", "Buyer")):
        raw = data.get(field)
        if raw is None or raw == "":
            add("GSTIN_MISSING", "warning", field, f"{label} GSTIN is missing.")
            continue
        for problem in check_gstin(raw):
            add("GSTIN_INVALID", "error", field, f"{label} GSTIN: {problem}.")

    # Amount arithmetic
    amounts = {k: to_decimal(data.get(k)) for k in
               ("subtotal", "discount", "taxable_value", "cgst", "sgst", "igst", "cess", "rounding", "total_amount")}
    for name, value in amounts.items():
        if data.get(name) not in (None, "") and value is None:
            add("AMOUNT_INVALID", "error", name, f"{name.replace('_', ' ').capitalize()} is not a valid number.")

    taxable = amounts["taxable_value"]
    if taxable is not None and amounts["subtotal"] is not None:
        expected = amounts["subtotal"] - (amounts["discount"] or Decimal("0"))
        if abs(expected - taxable) > tolerance:
            add("TAXABLE_MISMATCH", "error", "taxable_value",
                f"Taxable value {taxable} differs from subtotal minus discount ({expected}).")

    cgst, sgst, igst = amounts["cgst"], amounts["sgst"], amounts["igst"]
    if cgst is not None or sgst is not None:
        if cgst is None or sgst is None:
            add("CGST_SGST_UNPAIRED", "warning", "cgst", "CGST and SGST should both be present for intra-state supply.")
        elif cgst != sgst:
            add("CGST_SGST_MISMATCH", "error", "sgst", f"CGST ({cgst}) and SGST ({sgst}) should be equal.")
        if igst not in (None, Decimal("0.00")):
            add("IGST_WITH_CGST_SGST", "error", "igst", "IGST and CGST/SGST should not both be charged on one invoice.")

    tax_total = sum((v for v in (cgst, sgst, igst, amounts["cess"]) if v is not None), Decimal("0"))
    total = amounts["total_amount"]
    if taxable is not None and total is not None:
        expected_total = taxable + tax_total + (amounts["rounding"] or Decimal("0"))
        if abs(expected_total - total) > tolerance:
            add("TOTAL_MISMATCH", "error", "total_amount",
                f"Total {total} differs from taxable + taxes + rounding ({expected_total}).")

    # Line-item arithmetic
    for idx, line in enumerate(data.get("line_items") or [], start=1):
        qty, price = to_decimal(line.get("quantity")), to_decimal(line.get("unit_price"))
        lt = to_decimal(line.get("taxable_value"))
        if qty is not None and price is not None and lt is not None:
            if abs(qty * price - lt) > tolerance:
                add("LINE_TAXABLE_MISMATCH", "warning", f"line_items[{idx}].taxable_value",
                    f"Line {idx}: quantity x unit price ({qty * price}) does not match taxable value ({lt}).")
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
