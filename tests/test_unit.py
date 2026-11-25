"""Unit tests: password hashing, signed tokens, GSTIN, arithmetic, extraction, file validation, exports."""
from decimal import Decimal
from pathlib import Path

import pytest

from app.exports import guard_csv_cell, invoices_csv
from app.extraction import ExtractionUnavailable, InvoiceData, detect_kind, extract, parse_text
from app.security import hash_password, sign_token, unsign_token, verify_password
from app.services.files import UploadRejected, resolve_inside, safe_original_name, validate_upload
from app.validation import check_gstin, normalise_invoice_number, validate_invoice
from scripts.synthetic_invoices import CASES, SUPPLIER, build_all, build_pdf, make_gstin

TOL = Decimal("1.00")
DEMO = Path(__file__).resolve().parent.parent / "demo_data"


# ---------------- security
def test_password_hash_is_salted_and_verifies():
    h1 = hash_password("a-long-passphrase", 1000)
    h2 = hash_password("a-long-passphrase", 1000)
    assert h1 != h2 and "a-long-passphrase" not in h1
    assert verify_password("a-long-passphrase", h1)
    assert not verify_password("wrong", h1)
    assert not verify_password("anything", "not-a-hash")


def test_signed_token_rejects_tampering_and_expiry():
    tok = sign_token({"uid": 1, "org": 2, "exp": 9_999_999_999}, "secret")
    assert unsign_token(tok, "secret")["uid"] == 1
    assert unsign_token(tok, "other-secret") is None
    assert unsign_token(tok[:-2] + "AA", "secret") is None
    expired = sign_token({"uid": 1, "exp": 1}, "secret")
    assert unsign_token(expired, "secret") is None


# ---------------- GSTIN
def test_known_valid_gstin_passes():
    assert check_gstin("27AAPFU0939F1ZV") == []


def test_gstin_checksum_typo_is_caught():
    assert any("checksum" in p for p in check_gstin("27AAPFU0939F1ZX"))


@pytest.mark.parametrize("bad, fragment", [
    ("27AAPFU0939F1Z", "15 characters"),
    ("", "missing"),
    ("99AAPFU0939F1ZV", "state code"),
    ("27aapfu0939f1zv", None),  # lowercase is normalised before checks
])
def test_gstin_format_problems(bad, fragment):
    problems = check_gstin(bad or None)
    if fragment is None:
        assert problems == [] or any("checksum" in p for p in problems)
    else:
        assert any(fragment in p for p in problems)


def test_synthetic_gstins_are_format_valid():
    assert check_gstin(SUPPLIER["gstin"]) == []
    assert check_gstin(make_gstin("29", "SAMPL9012D")) == []


# ---------------- tax arithmetic
def _base(**over):
    data = {"supplier_name": "S", "invoice_number": "1", "invoice_date": "2026-11-01",
            "supplier_gstin": SUPPLIER["gstin"], "buyer_gstin": make_gstin("27", "DEMOB5678C"), "buyer_name": "B"}
    data.update(over)
    return data


def codes(issues):
    return {i.code for i in issues}


def test_valid_cgst_sgst_arithmetic_has_no_errors():
    data = _base(subtotal="10000", taxable_value="10000", cgst="900", sgst="900", total_amount="11800")
    assert "TOTAL_MISMATCH" not in codes(validate_invoice(data, TOL))
    assert "CGST_SGST_MISMATCH" not in codes(validate_invoice(data, TOL))


def test_discount_and_rounding_within_and_outside_tolerance():
    ok = _base(subtotal="10000", discount="500", taxable_value="9500", cgst="855", sgst="855",
               rounding="0.40", total_amount="11210.40")
    assert not {"TAXABLE_MISMATCH", "TOTAL_MISMATCH"} & codes(validate_invoice(ok, TOL))
    rounded_ok = _base(subtotal="10000", taxable_value="10000", cgst="900", sgst="900", total_amount="11800.60")
    assert "TOTAL_MISMATCH" not in codes(validate_invoice(rounded_ok, TOL))  # 0.60 within Rs 1 tolerance
    off = _base(subtotal="10000", taxable_value="10000", cgst="900", sgst="900", total_amount="11900")
    assert "TOTAL_MISMATCH" in codes(validate_invoice(off, TOL))


def test_cgst_sgst_unequal_and_igst_mixed_are_errors():
    unequal = _base(taxable_value="10000", cgst="900", sgst="800", total_amount="11700")
    assert "CGST_SGST_MISMATCH" in codes(validate_invoice(unequal, TOL))
    mixed = _base(taxable_value="10000", cgst="900", sgst="900", igst="1800", total_amount="13600")
    assert "IGST_WITH_CGST_SGST" in codes(validate_invoice(mixed, TOL))


def test_missing_fields_are_reported_not_invented():
    issues = validate_invoice({"supplier_name": None, "invoice_number": None, "invoice_date": None}, TOL)
    assert {"INVOICE_NUMBER_MISSING", "INVOICE_DATE_MISSING", "SUPPLIER_NAME_MISSING", "GSTIN_MISSING"} <= codes(issues)


def test_invalid_amount_and_date_are_flagged():
    issues = validate_invoice(_base(invoice_date="31-02-2026", total_amount="abc"), TOL)
    assert "INVOICE_DATE_INVALID" in codes(issues) and "AMOUNT_INVALID" in codes(issues)


def test_normalise_invoice_number():
    assert normalise_invoice_number(" sf/2026-0412 ") == "SF20260412"
    assert normalise_invoice_number(None) is None


# ---------------- extraction
@pytest.mark.parametrize("name", list(CASES))
def test_synthetic_pdf_extraction_matches_known_values(name):
    content = build_pdf(__import__("scripts.synthetic_invoices", fromlist=["_lines_for"])._lines_for(CASES[name]))
    result = extract(content, "pdf")
    d = result.data
    assert d.invoice_number == CASES[name]["number"]
    assert d.invoice_date == CASES[name]["date"]
    assert d.total_amount == Decimal(CASES[name]["total"])
    assert d.supplier_name == SUPPLIER["name"]
    assert result.method == "rule_based_pdf_text"
    assert all(0.0 <= v <= 1.0 for v in result.confidence.values())


def test_missing_buyer_gstin_stays_null():
    content = build_pdf(__import__("scripts.synthetic_invoices", fromlist=["_lines_for"])._lines_for(CASES["missing_buyer_gstin"]))
    result = extract(content, "pdf")
    assert result.data.buyer_gstin is None
    assert "buyer_gstin" not in result.confidence


def test_saved_demo_pdfs_exist_and_extract():
    for path in sorted(DEMO.glob("synthetic_*.pdf")):
        if "blank" in path.name:
            continue
        assert extract(path.read_bytes(), "pdf").data.invoice_number


def test_scanned_pdf_without_text_is_reported_unavailable_not_extracted():
    with pytest.raises(ExtractionUnavailable):
        extract(build_all()["blank_scan_like"], "pdf")


def test_image_without_tesseract_reports_unavailable():
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (40, 40), "white").save(buf, "PNG")
    with pytest.raises(ExtractionUnavailable):
        extract(buf.getvalue(), "png", configured_tesseract="/nonexistent/tesseract")


def test_parser_on_garbage_text_returns_failed_status():
    result = parse_text("hello world, nothing invoice-like here")
    assert result.status == "failed"
    assert result.data.invoice_number is None and result.data.total_amount is None


def test_invoice_schema_rejects_unknown_fields():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        InvoiceData(made_up_field="x")


def test_extraction_never_marks_success_on_partial_text():
    result = parse_text("Invoice No: AB1\nTotal: 10.00")
    assert result.status != "extracted"


# ---------------- file validation
def test_detect_kind_uses_content_not_extension():
    assert detect_kind(b"%PDF-1.4 ...") == "pdf"
    assert detect_kind(b"\x89PNG\r\n\x1a\n....") == "png"
    assert detect_kind(b"\xff\xd8\xff\xe0....") == "jpeg"
    assert detect_kind(b"<html>") is None


@pytest.mark.parametrize("name, content, message", [
    ("invoice.pdf", b"", "empty"),
    ("invoice.pdf", b"<script>alert(1)</script>", "signature"),
    ("invoice.exe", b"%PDF-1.4 x", "Only PDF"),
    ("invoice.png", b"%PDF-1.4 x", "does not match"),
    ("invoice.pdf", b"%PDF-1.4 " + b"x" * 3_000_000, "limit"),
])
def test_invalid_uploads_are_rejected(name, content, message):
    with pytest.raises(UploadRejected) as exc:
        validate_upload(name, content, 2 * 1024 * 1024)
    assert message.lower() in str(exc.value).lower()


def test_corrupt_png_rejected():
    with pytest.raises(UploadRejected):
        validate_upload("x.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 30, 10_000)


def test_safe_filenames_and_path_traversal_guard(tmp_path):
    assert ".." not in safe_original_name("../../etc/passwd.pdf")
    assert "/" not in safe_original_name("a/b/c.pdf")
    with pytest.raises(UploadRejected):
        resolve_inside(tmp_path, "../secret.pdf")
    with pytest.raises(UploadRejected):
        resolve_inside(tmp_path, "sub/evil.pdf")


# ---------------- exports
def test_csv_formula_injection_is_neutralised():
    assert guard_csv_cell("=HYPERLINK('x')") == "'=HYPERLINK('x')"
    assert guard_csv_cell("Plain") == "Plain"
    csv_text = invoices_csv([{"supplier_name": "=cmd", "total_amount": "10.00"}])
    assert "'=cmd" in csv_text
    assert csv_text.splitlines()[0].startswith("invoice_id,client_company")


# ---------------- OCR (runs only when the Tesseract executable is installed)
from app.extraction import ocr_available  # noqa: E402


@pytest.mark.skipif(not ocr_available(), reason="Tesseract OCR not installed in this environment")
def test_ocr_on_rendered_invoice_image_is_flagged_for_review():
    import io
    from PIL import Image, ImageDraw, ImageFont
    from scripts.synthetic_invoices import _lines_for as lines_for

    img = Image.new("RGB", (1400, 1000), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    y = 30
    for line in lines_for(CASES["cgst_sgst_valid"]):
        draw.text((30, y), line, fill="black", font=font)
        y += 34
    buf = io.BytesIO()
    img.save(buf, "PNG")
    result = extract(buf.getvalue(), "png")
    assert result.method == "rule_based_ocr"
    assert result.status == "needs_review"  # OCR output is never auto-approved
    assert result.data.invoice_number == "SF/2026/0412"
    assert result.data.total_amount == Decimal("15340.00")
