"""Generates SYNTHETIC Indian GST invoices for demos and tests. No real party, GSTIN or amount is used.

Every GSTIN is generated here with a valid checksum but belongs to no real taxpayer.
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.validation import _gstin_checksum  # noqa: E402


def make_gstin(state: str, pan_part: str, entity: str = "1") -> str:
    base = f"{state}{pan_part}{entity}Z"
    return base + _gstin_checksum(base)


SUPPLIER = {"name": "Synthetic Traders Pvt Ltd", "gstin": make_gstin("27", "SYNTH1234A"),
            "address": "Plot 12, Demo Industrial Area, Pune 411001", "state": "Maharashtra"}
BUYER_SAME_STATE = {"name": "Demo Foods LLP", "gstin": make_gstin("27", "DEMOB5678C"),
                    "address": "Shop 4, Sample Market, Mumbai 400001"}
BUYER_OTHER_STATE = {"name": "Sample Retail Pvt Ltd", "gstin": make_gstin("29", "SAMPL9012D"),
                     "address": "42 Test Road, Bengaluru 560001"}

ITEMS_TWO = [
    ("Software consulting", "998313", "2", "5000.00", "10000.00", "18", "1800.00", "11800.00"),
    ("Annual maintenance", "998314", "1", "3000.00", "3000.00", "18", "540.00", "3540.00"),
]


def _lines_for(case: dict) -> list[str]:
    buyer = case.get("buyer", BUYER_SAME_STATE)
    lines = ["TAX INVOICE", SUPPLIER["name"], f"GSTIN: {case.get('supplier_gstin', SUPPLIER['gstin'])}",
             SUPPLIER["address"], f"Supplier State: {SUPPLIER['state']}",
             f"Invoice No: {case['number']}", f"Invoice Date: {case['date']}", f"Due Date: {case['due']}",
             f"Place of Supply: {case['pos']}", "Bill To: " + buyer["name"]]
    if buyer.get("gstin") and not case.get("omit_buyer_gstin"):
        lines.append(f"GSTIN: {buyer['gstin']}")
    lines.append(buyer["address"])
    lines.append("Description HSN Qty Unit Price Taxable GST% Tax Total")
    for row in case["items"]:
        desc, hsn, qty, price, taxable, rate, tax, total = row
        lines.append(f"{desc} {hsn} {qty} {price} {taxable} {rate}% {tax} {total}")
    lines.append(f"Subtotal: {case['subtotal']}")
    if case.get("discount"):
        lines.append(f"Discount: {case['discount']}")
    lines.append(f"Taxable Value: {case['taxable']}")
    if case.get("cgst"):
        lines.append(f"CGST @ 9%: {case['cgst']}")
        lines.append(f"SGST @ 9%: {case['sgst']}")
    if case.get("igst"):
        lines.append(f"IGST @ 18%: {case['igst']}")
    if case.get("rounding"):
        lines.append(f"Rounding Off: {case['rounding']}")
    lines.append(f"Grand Total: {case['total']}")
    return lines


def build_pdf(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=A4)
    pdf.setFont("Helvetica", 10)
    y = 800
    for line in lines:
        pdf.drawString(40, y, line)
        y -= 16
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


CASES: dict[str, dict] = {
    "cgst_sgst_valid": dict(number="SF/2026/0412", date="14-11-2026", due="14-12-2026", pos="Maharashtra",
                            items=ITEMS_TWO, subtotal="13000.00", taxable="13000.00",
                            cgst="1170.00", sgst="1170.00", total="15340.00"),
    "igst_valid": dict(number="SF/2026/0413", date="15-11-2026", due="15-12-2026", pos="Karnataka",
                       buyer=BUYER_OTHER_STATE, items=[ITEMS_TWO[0]], subtotal="10000.00", taxable="10000.00",
                       igst="1800.00", total="11800.00"),
    "discount_rounding": dict(number="SF/2026/0414", date="16-11-2026", due="16-12-2026", pos="Maharashtra",
                              items=[ITEMS_TWO[0]], subtotal="10000.00", discount="500.00",
                              taxable="9500.00", cgst="855.00", sgst="855.00", rounding="0.40", total="11210.40"),
    "missing_buyer_gstin": dict(number="SF/2026/0415", date="17-11-2026", due="17-12-2026", pos="Maharashtra",
                                buyer={"name": "Walk-in Customer", "address": "Pune"}, omit_buyer_gstin=True,
                                items=[ITEMS_TWO[1]], subtotal="3000.00", taxable="3000.00",
                                cgst="270.00", sgst="270.00", total="3540.00"),
    "incorrect_total": dict(number="SF/2026/0416", date="18-11-2026", due="18-12-2026", pos="Maharashtra",
                            items=[ITEMS_TWO[1]], subtotal="3000.00", taxable="3000.00",
                            cgst="270.00", sgst="270.00", total="3900.00"),
    "bad_gstin_checksum": dict(number="SF/2026/0417", date="19-11-2026", due="19-12-2026", pos="Maharashtra",
                               supplier_gstin="27AAAAA0000A1Z5", items=[ITEMS_TWO[1]], subtotal="3000.00",
                               taxable="3000.00", cgst="270.00", sgst="270.00", total="3540.00"),
}


def build_all() -> dict[str, bytes]:
    out = {name: build_pdf(_lines_for(case)) for name, case in CASES.items()}
    out["blank_scan_like"] = build_pdf([])  # PDF with no selectable text: simulates an unreadable scan
    return out


if __name__ == "__main__":
    dest = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "demo_data"
    dest.mkdir(parents=True, exist_ok=True)
    for name, data in build_all().items():
        (dest / f"synthetic_{name}.pdf").write_bytes(data)
        print("wrote", dest / f"synthetic_{name}.pdf")
