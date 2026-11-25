"""Rule-based invoice extraction.

IMPORTANT: this is a deterministic rule-based parser (regular expressions over
PDF text or OCR text). It is NOT an autonomous AI model. Every value it returns
carries a confidence score and a method label. Fields that cannot be found are
returned as None; nothing is inferred or invented.
"""
from __future__ import annotations

import io
import re
import shutil
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

METHOD_PDF_TEXT = "rule_based_pdf_text"
METHOD_OCR = "rule_based_ocr"
MANUAL_REVIEW_THRESHOLD = 0.80

FILE_KINDS = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg"}


# ---------------------------------------------------------------------------
# Structured output schema (validated with Pydantic)
# ---------------------------------------------------------------------------
class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str | None = None
    hsn_sac: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    gst_rate: Decimal | None = None
    taxable_value: Decimal | None = None
    tax_amount: Decimal | None = None
    line_total: Decimal | None = None


class InvoiceData(BaseModel):
    """Canonical invoice record. Every field is optional; missing means null."""
    model_config = ConfigDict(extra="forbid")
    supplier_name: str | None = None
    supplier_gstin: str | None = None
    supplier_address: str | None = None
    supplier_state: str | None = None
    buyer_name: str | None = None
    buyer_gstin: str | None = None
    buyer_address: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    due_date: str | None = None
    place_of_supply: str | None = None
    subtotal: Decimal | None = None
    discount: Decimal | None = None
    taxable_value: Decimal | None = None
    cgst: Decimal | None = None
    sgst: Decimal | None = None
    igst: Decimal | None = None
    cess: Decimal | None = None
    rounding: Decimal | None = None
    total_amount: Decimal | None = None
    line_items: list[LineItem] = Field(default_factory=list)

    def scalar_dict(self) -> dict[str, Any]:
        """Serialisable dict of header fields plus line items, using strings for Decimals."""
        out: dict[str, Any] = {}
        for name in type(self).model_fields:
            if name == "line_items":
                continue
            value = getattr(self, name)
            out[name] = str(value) if isinstance(value, Decimal) else value
        out["line_items"] = [
            {k: (str(v) if isinstance(v, Decimal) else v) for k, v in item.model_dump().items()}
            for item in self.line_items
        ]
        return out


@dataclass
class ExtractionResult:
    data: InvoiceData
    method: str
    confidence: dict[str, float]          # per-field confidence, only for fields that were found
    page_refs: dict[str, int]             # field -> page number where it was found
    overall_confidence: float
    status: str                           # "extracted" | "needs_review" | "failed"
    warnings: list[str] = field(default_factory=list)


class ExtractionUnavailable(Exception):
    """Raised when a file type cannot be read in this environment (e.g. OCR not installed)."""


# ---------------------------------------------------------------------------
# Text acquisition
# ---------------------------------------------------------------------------
def detect_kind(content: bytes) -> str | None:
    """Detect the real file type from its leading bytes. Extensions are never trusted."""
    if content.startswith(b"%PDF-"):
        return "pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    return None


def tesseract_path(configured: str = "") -> str | None:
    if configured:
        return configured if shutil.which(configured) or _exists(configured) else None
    return shutil.which("tesseract")


def _exists(path: str) -> bool:
    import os
    return os.path.isfile(path)


def ocr_available(configured: str = "") -> bool:
    if tesseract_path(configured) is None:
        return False
    try:
        import pytesseract  # noqa: F401
    except ImportError:
        return False
    return True


def pdf_pages_text(content: bytes) -> list[str]:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    return [(page.extract_text() or "") for page in reader.pages]


def image_text(content: bytes, configured_tesseract: str = "") -> str:
    if not ocr_available(configured_tesseract):
        raise ExtractionUnavailable(
            "Scanned-image extraction is unavailable: the Tesseract OCR executable was not found. "
            "Enter the invoice details manually or install Tesseract."
        )
    import pytesseract
    from PIL import Image

    cmd = tesseract_path(configured_tesseract)
    if cmd:
        pytesseract.pytesseract.tesseract_cmd = cmd
    image = Image.open(io.BytesIO(content))
    image.load()
    return pytesseract.image_to_string(image)


# ---------------------------------------------------------------------------
# Rule patterns
# ---------------------------------------------------------------------------
DATE_TOKEN = r"(\d{1,2}[-/.]\d{1,2}[-/.]\d{4}|\d{4}-\d{2}-\d{2}|\d{1,2}[ -][A-Za-z]{3,9}[ -]\d{4})"
AMOUNT = r"(-?[\d,]+\.\d{2})"
GSTIN_TOKEN = r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z])\b"
LINE_RE = re.compile(
    r"^(?P<desc>[A-Za-z][A-Za-z0-9 .,&()/\-]*?)\s+(?P<hsn>\d{4,8})\s+(?P<qty>\d+(?:\.\d+)?)\s+"
    r"(?P<price>[\d,]+\.\d{2})\s+(?P<taxable>[\d,]+\.\d{2})\s+(?P<rate>\d{1,2}(?:\.\d+)?)%\s+"
    r"(?P<tax>[\d,]+\.\d{2})\s+(?P<total>[\d,]+\.\d{2})\s*$",
    re.MULTILINE,
)


def _search(pattern: str, text: str, flags: int = re.IGNORECASE | re.MULTILINE):
    return re.search(pattern, text, flags)


def _amount_after(label: str, text: str) -> Decimal | None:
    m = _search(rf"^\s*{label}\s*(?:@\s*[\d.]+\s*%)?\s*[:\-]?\s*(?:Rs\.?|INR|₹)?\s*{AMOUNT}\s*$", text)
    if not m:
        return None
    return Decimal(m.group(1).replace(",", ""))


def _value_after(label: str, text: str) -> str | None:
    m = _search(rf"^\s*{label}\s*[:\-]\s*(.+?)\s*$", text)
    return m.group(1).strip() if m else None


def _page_of(pages: list[str], pattern: str) -> int | None:
    for i, page in enumerate(pages, start=1):
        if re.search(pattern, page, re.IGNORECASE | re.MULTILINE):
            return i
    return None


def parse_text(text: str, pages: list[str] | None = None) -> ExtractionResult:
    """Parse invoice text into the canonical schema with per-field confidence."""
    pages = pages or [text]
    data = InvoiceData()
    conf: dict[str, float] = {}
    refs: dict[str, int] = {}
    warnings: list[str] = []

    def put(name: str, value: Any, score: float, pattern: str) -> None:
        if value in (None, ""):
            return
        setattr(data, name, value)
        conf[name] = score
        page = _page_of(pages, pattern)
        if page:
            refs[name] = page

    # Header fields
    m = _search(r"Invoice\s*(?:No\.?|Number|#)\s*[:\-]?\s*([A-Za-z0-9/\-_.]+)", text)
    if m:
        put("invoice_number", m.group(1), 0.95, r"Invoice\s*(No|Number|#)")
    m = _search(rf"Invoice\s*Date\s*[:\-]?\s*{DATE_TOKEN}", text)
    if m:
        put("invoice_date", m.group(1), 0.95, r"Invoice\s*Date")
    m = _search(rf"Due\s*Date\s*[:\-]?\s*{DATE_TOKEN}", text)
    if m:
        put("due_date", m.group(1), 0.95, r"Due\s*Date")
    put("place_of_supply", _value_after(r"Place\s*of\s*Supply", text), 0.9, r"Place\s*of\s*Supply")

    # Parties: the supplier section is the text before the buyer heading, the buyer section after it.
    split = _search(r"^\s*(Bill\s*To|Buyer|Billed\s*To|Customer)\b.*$", text)
    supplier_part = text[: split.start()] if split else text
    buyer_part = text[split.start():] if split else ""
    if not split:
        warnings.append("Buyer section heading not found; buyer fields were not extracted.")

    sup_gstins = re.findall(GSTIN_TOKEN, supplier_part)
    if sup_gstins:
        put("supplier_gstin", sup_gstins[0], 0.9, GSTIN_TOKEN)
    buy_gstins = re.findall(GSTIN_TOKEN, buyer_part)
    if buy_gstins:
        put("buyer_gstin", buy_gstins[0], 0.9, GSTIN_TOKEN)

    sup_name = _value_after(r"(?:Supplier|Seller|From|Sold\s*By)", supplier_part)
    if not sup_name:
        for line in supplier_part.splitlines():
            clean = line.strip()
            if clean and not re.search(r"tax\s*invoice|invoice|gstin|pan|phone|email|address", clean, re.I):
                sup_name = clean
                break
    put("supplier_name", sup_name, 0.7 if sup_name else 0.0, r"^" + re.escape(sup_name or "") if sup_name else "x^")

    buy_name = _value_after(r"(?:Buyer|Bill\s*To|Billed\s*To|Customer)", buyer_part)
    if buy_name and buy_name.lower().startswith("gstin"):
        buy_name = None
    if buy_name:
        put("buyer_name", buy_name, 0.7, re.escape(buy_name))

    # Amounts
    fields = {
        "subtotal": r"Sub\s*-?\s*total",
        "discount": r"Discount",
        "taxable_value": r"Taxable\s*(?:Value|Amount)",
        "cgst": r"CGST",
        "sgst": r"SGST",
        "igst": r"IGST",
        "cess": r"Cess",
        "rounding": r"Round(?:ing)?\s*(?:Off|Adj(?:ustment)?)?",
    }
    for name, label in fields.items():
        value = _amount_after(label, text)
        if value is not None:
            put(name, value, 0.95, label)

    total_matches = re.findall(rf"^\s*(?:Grand\s+Total|Total\s+Amount(?:\s+Payable)?|Amount\s+Payable|Total)\s*[:\-]?\s*(?:Rs\.?|INR|₹)?\s*{AMOUNT}\s*$",
                               text, re.IGNORECASE | re.MULTILINE)
    if total_matches:
        put("total_amount", Decimal(total_matches[-1].replace(",", "")), 0.9, r"Grand\s+Total|Total")

    # Line items
    items: list[LineItem] = []
    for lm in LINE_RE.finditer(text):
        g = lm.groupdict()
        items.append(LineItem(
            description=g["desc"].strip(),
            hsn_sac=g["hsn"],
            quantity=Decimal(g["qty"]),
            unit_price=Decimal(g["price"].replace(",", "")),
            taxable_value=Decimal(g["taxable"].replace(",", "")),
            gst_rate=Decimal(g["rate"]),
            tax_amount=Decimal(g["tax"].replace(",", "")),
            line_total=Decimal(g["total"].replace(",", "")),
        ))
    data.line_items = items
    if items:
        conf["line_items"] = 0.9
    else:
        warnings.append("No line items matched the rule-based table pattern.")

    # Derive status: never mark extracted just because text was read.
    core = ["invoice_number", "invoice_date", "supplier_name", "total_amount"]
    missing_core = [c for c in core if c not in conf]
    if len(missing_core) == len(core):
        status = "failed"
    elif missing_core:
        status = "needs_review"
        warnings.append("Missing core fields: " + ", ".join(missing_core) + ".")
    else:
        status = "extracted"
    overall = round(sum(conf.values()) / len(conf), 3) if conf else 0.0
    if overall < MANUAL_REVIEW_THRESHOLD and status == "extracted":
        status = "needs_review"
        warnings.append("Average confidence is below the manual-review threshold.")
    return ExtractionResult(data=data, method=METHOD_PDF_TEXT, confidence=conf, page_refs=refs,
                            overall_confidence=overall, status=status, warnings=warnings)


def extract(content: bytes, kind: str, configured_tesseract: str = "") -> ExtractionResult:
    """Extract an invoice from a validated file. Raises ExtractionUnavailable when OCR is required but absent."""
    if kind == "pdf":
        pages = pdf_pages_text(content)
        text = "\n".join(pages)
        if not text.strip():
            raise ExtractionUnavailable(
                "This PDF has no selectable text (it looks scanned). Scanned-PDF OCR is not supported in "
                "this MVP; enter the invoice details manually."
            )
        result = parse_text(text, pages)
        return result
    text = image_text(content, configured_tesseract)
    if not text.strip():
        raise ExtractionUnavailable("OCR ran but produced no readable text. Enter the invoice details manually.")
    result = parse_text(text)
    result.method = METHOD_OCR
    result.warnings.insert(0, "OCR text is less reliable than PDF text; review every field.")
    result.overall_confidence = round(result.overall_confidence * 0.9, 3)
    if result.status == "extracted":
        result.status = "needs_review"
    return result
