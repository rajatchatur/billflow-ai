"""Invoice workflow: ingestion, processing, corrections, approval and status transitions.

Original extracted values are kept in extracted_json and never overwritten.
Accountant corrections are stored separately in corrected_json.
"""
from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

from app import audit
from app.auth import CurrentUser
from app.config import Settings
from app.duplicate_detection import HARD, find_duplicates
from app.extraction import ExtractionUnavailable, InvoiceData, LineItem, extract
from app.services.files import ValidatedUpload, store_original, resolve_inside
from app.validation import has_errors, normalise_invoice_number, to_decimal, validate_invoice

HEADER_FIELDS = [
    "supplier_name", "supplier_gstin", "supplier_address", "supplier_state", "buyer_name", "buyer_gstin",
    "buyer_address", "invoice_number", "invoice_date", "due_date", "place_of_supply", "subtotal", "discount",
    "taxable_value", "cgst", "sgst", "igst", "cess", "rounding", "total_amount",
]
AMOUNT_FIELDS = {"subtotal", "discount", "taxable_value", "cgst", "sgst", "igst", "cess", "rounding", "total_amount"}
MAX_TEXT = 300
DECISION_RECORD = {"approve": "approved", "reject": "rejected", "mark_review": "marked_for_review", "reopen": "reopened"}


class InvoiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _effective(row: sqlite3.Row) -> dict[str, Any]:
    raw = row["corrected_json"] or row["extracted_json"]
    return json.loads(raw) if raw else {}


def _lines(conn: sqlite3.Connection, invoice_id: int) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM invoice_line_items WHERE invoice_id = ? ORDER BY position",
                        (invoice_id,)).fetchall()
    return [dict(r) for r in rows]


def _tolerance(settings: Settings) -> Decimal:
    return Decimal(str(settings.tolerance_rupees)).quantize(Decimal("0.01"))


def _issues_for(data: dict[str, Any], settings: Settings) -> list[dict[str, Any]]:
    return [i.as_dict() for i in validate_invoice(data, _tolerance(settings))]


def _write_lines(conn: sqlite3.Connection, org_id: int, invoice_id: int, items: list[dict[str, Any]]) -> None:
    conn.execute("DELETE FROM invoice_line_items WHERE invoice_id = ?", (invoice_id,))
    for pos, item in enumerate(items, start=1):
        conn.execute(
            "INSERT INTO invoice_line_items (organisation_id, invoice_id, position, description, hsn_sac, "
            "quantity, unit_price, gst_rate, taxable_value, tax_amount, line_total) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (org_id, invoice_id, pos, item.get("description"), item.get("hsn_sac"),
             _f(item.get("quantity")), _f(item.get("unit_price")), _f(item.get("gst_rate")),
             _f(item.get("taxable_value")), _f(item.get("tax_amount")), _f(item.get("line_total"))))


def _f(value: Any) -> float | None:
    d = to_decimal(value)
    return float(d) if d is not None else None


def _header_columns(data: dict[str, Any]) -> dict[str, Any]:
    total = to_decimal(data.get("total_amount"))
    number = data.get("invoice_number")
    return {
        "supplier_name": data.get("supplier_name"),
        "supplier_gstin": (data.get("supplier_gstin") or "").strip().upper() or None,
        "invoice_number": number,
        "normalised_invoice_number": normalise_invoice_number(number),
        "invoice_date": data.get("invoice_date"),
        "total_amount": float(total) if total is not None else None,
    }


def _sanitise_text(value: Any, field_name: str) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > MAX_TEXT:
        raise InvoiceError(f"{field_name} is too long (max {MAX_TEXT} characters).")
    if "\x00" in text:
        raise InvoiceError(f"{field_name} contains invalid characters.")
    return text


def _validated_fields(form: dict[str, Any]) -> dict[str, Any]:
    """Accept only known header fields and validate their types. Unknown keys are ignored."""
    clean: dict[str, Any] = {}
    for name in HEADER_FIELDS:
        if name not in form:
            continue
        raw = form.get(name)
        if name in AMOUNT_FIELDS:
            if raw in (None, ""):
                clean[name] = None
                continue
            dec = to_decimal(raw)
            if dec is None:
                raise InvoiceError(f"{name.replace('_', ' ')} must be a number.")
            clean[name] = str(dec)
        else:
            clean[name] = _sanitise_text(raw, name.replace("_", " "))
    return clean


def process_new_document(conn: sqlite3.Connection, settings: Settings, user: CurrentUser,
                         client_id: int, validated: ValidatedUpload) -> int:
    """Store the original file, run extraction and validation, and create the invoice record."""
    org = user.organisation_id
    stored = store_original(settings.upload_dir, validated)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO source_documents (organisation_id, client_company_id, original_filename, stored_filename, "
            "detected_type, size_bytes, sha256, uploaded_by) VALUES (?,?,?,?,?,?,?,?)",
            (org, client_id, validated.original_filename, stored, validated.kind, validated.size_bytes,
             validated.sha256, user.id))
        doc_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO invoices (organisation_id, client_company_id, source_document_id, status) "
            "VALUES (?,?,?, 'processing')", (org, client_id, doc_id))
        invoice_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        audit.record(conn, org, user.id, "document.uploaded", "source_document", doc_id,
                     {"kind": validated.kind, "size_bytes": validated.size_bytes, "client_id": client_id})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    run_extraction(conn, settings, user, invoice_id)
    return invoice_id


def run_extraction(conn: sqlite3.Connection, settings: Settings, user: CurrentUser | None, invoice_id: int) -> None:
    """(Re)run extraction for an invoice whose source document is stored. Never marks success on model output alone."""
    org = conn.execute("SELECT organisation_id FROM invoices WHERE id = ?", (invoice_id,)).fetchone()["organisation_id"]
    row = conn.execute(
        "SELECT i.*, d.stored_filename, d.detected_type, d.sha256 AS file_sha256, d.original_filename "
        "FROM invoices i JOIN source_documents d ON d.id = i.source_document_id WHERE i.id = ?",
        (invoice_id,)).fetchone()
    actor = user.id if user else None
    path = resolve_inside(settings.upload_dir, row["stored_filename"])
    try:
        content = path.read_bytes()
        result = extract(content, row["detected_type"], settings.tesseract_cmd)
        data = result.data.scalar_dict()
        failure = None
        status = {"extracted": "ready_for_approval", "needs_review": "needs_review", "failed": "needs_review"}[result.status]
        extraction_status = result.status
        method = result.method
        confidence = result.overall_confidence
        warnings = list(result.warnings)
        confidence_map = result.confidence
    except ExtractionUnavailable as exc:
        data = InvoiceData().scalar_dict()
        failure = str(exc)
        status, extraction_status, method, confidence = "needs_review", "unavailable", "none", 0.0
        warnings, confidence_map = [failure], {}
    except Exception:  # unexpected parser error: record as failed, do not leak content
        data = InvoiceData().scalar_dict()
        failure = "Processing failed due to an unexpected error. Retry or enter details manually."
        status, extraction_status, method, confidence = "failed", "error", "none", 0.0
        warnings, confidence_map = [failure], {}
        audit.record(conn, org, actor, "extraction.error", "invoice", invoice_id, {"stage": "parse"})

    issues = _issues_for(data, settings)
    header = _header_columns(data)
    dupes = find_duplicates(conn, org, row["client_company_id"], invoice_id, header["supplier_gstin"],
                            header["normalised_invoice_number"], header["invoice_date"],
                            header["total_amount"], row["file_sha256"])
    if (has_errors(validate_invoice(data, _tolerance(settings))) or dupes) and status == "ready_for_approval":
        status = "needs_review"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE invoices SET status=?, extraction_method=?, extraction_status=?, extraction_confidence=?, "
            "extracted_json=?, corrected_json=NULL, supplier_name=?, supplier_gstin=?, invoice_number=?, "
            "normalised_invoice_number=?, invoice_date=?, total_amount=?, validation_json=?, duplicate_of_id=?, "
            "failure_reason=?, updated_at=datetime('now') WHERE id=?",
            (status, method, extraction_status, confidence,
             json.dumps({**data, "_confidence": confidence_map, "_warnings": warnings}, default=str),
             header["supplier_name"], header["supplier_gstin"], header["invoice_number"],
             header["normalised_invoice_number"], header["invoice_date"], header["total_amount"],
             json.dumps({"issues": issues, "duplicates": dupes}, default=str),
             dupes[0]["invoice_id"] if dupes else None, failure, invoice_id))
        _write_lines(conn, org, invoice_id, data.get("line_items") or [])
        audit.record(conn, org, actor, "invoice.extracted", "invoice", invoice_id,
                     {"status": status, "method": method, "extraction_status": extraction_status,
                      "confidence": confidence})
        audit.record(conn, org, actor, "validation.performed", "invoice", invoice_id,
                     {"issue_codes": sorted({i["code"] for i in issues}), "duplicate_candidates": len(dupes)})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def effective_view(conn: sqlite3.Connection, settings: Settings, row: sqlite3.Row) -> dict[str, Any]:
    data = _effective(row)
    stored = json.loads(row["extracted_json"]) if row["extracted_json"] else {}
    return {
        "values": {k: v for k, v in data.items() if not k.startswith("_") and k != "line_items"},
        "original": {k: v for k, v in stored.items() if not k.startswith("_") and k != "line_items"},
        "line_items": _lines(conn, row["id"]),
        "issues": json.loads(row["validation_json"] or "{}").get("issues", []),
        "duplicates": json.loads(row["validation_json"] or "{}").get("duplicates", []),
        "confidence": stored.get("_confidence", {}),
        "warnings": stored.get("_warnings", []),
    }


def save_corrections(conn: sqlite3.Connection, settings: Settings, user: CurrentUser, invoice_id: int,
                     form: dict[str, Any]) -> None:
    user.require("correct")
    row = _get(conn, user.organisation_id, invoice_id)
    if row["status"] in ("approved", "exported", "rejected"):
        raise InvoiceError("Approved, exported or rejected invoices cannot be edited. Reopen the invoice first.", 409)
    changes = _validated_fields(form)
    current = _effective(row)
    base = dict(current) if current else json.loads(row["extracted_json"] or "{}")
    base.pop("_confidence", None)
    base.pop("_warnings", None)
    diff = {k: {"from": base.get(k), "to": v} for k, v in changes.items() if base.get(k) != v}
    merged = {**base, **changes}
    base_lines = _lines(conn, invoice_id)
    lines = base.get("line_items")
    if lines is None:
        lines = [{k: r[k] for k in ("description", "hsn_sac", "quantity", "unit_price", "gst_rate",
                                     "taxable_value", "tax_amount", "line_total")} for r in base_lines]
    merged["line_items"] = lines
    issues = _issues_for(merged, settings)
    header = _header_columns(merged)
    dupes = find_duplicates(conn, user.organisation_id, row["client_company_id"], invoice_id,
                            header["supplier_gstin"], header["normalised_invoice_number"],
                            header["invoice_date"], header["total_amount"], _doc_sha(conn, row))
    blocking = any(i["severity"] == "error" for i in issues) or bool(dupes)
    status = "needs_review" if blocking else "ready_for_approval"
    stored = json.loads(row["extracted_json"]) if row["extracted_json"] else {}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE invoices SET corrected_json=?, status=?, supplier_name=?, supplier_gstin=?, invoice_number=?, "
            "normalised_invoice_number=?, invoice_date=?, total_amount=?, validation_json=?, duplicate_of_id=?, "
            "updated_at=datetime('now') WHERE id=? AND organisation_id=?",
            (json.dumps({**merged, "_confidence": stored.get("_confidence", {}), "_warnings": stored.get("_warnings", [])},
                        default=str),
             status, header["supplier_name"], header["supplier_gstin"], header["invoice_number"],
             header["normalised_invoice_number"], header["invoice_date"], header["total_amount"],
             json.dumps({"issues": issues, "duplicates": dupes}, default=str),
             dupes[0]["invoice_id"] if dupes else None, invoice_id, user.organisation_id))
        audit.record(conn, user.organisation_id, user.id, "field.corrected", "invoice", invoice_id,
                     {"fields": sorted(diff.keys()), "changes": diff})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def _doc_sha(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    r = conn.execute("SELECT sha256 FROM source_documents WHERE id = ?", (row["source_document_id"],)).fetchone()
    return r["sha256"] if r else None


def decide(conn: sqlite3.Connection, settings: Settings, user: CurrentUser, invoice_id: int,
           decision: str, note: str | None) -> str:
    user.require("decide")
    row = _get(conn, user.organisation_id, invoice_id)
    status = row["status"]
    note = _sanitise_text(note, "note") if note else None
    if decision == "approve":
        if status not in ("ready_for_approval", "needs_review"):
            raise InvoiceError(f"An invoice in status '{status}' cannot be approved.", 409)
        issues = json.loads(row["validation_json"] or "{}")
        errors = [i for i in issues.get("issues", []) if i["severity"] == "error"]
        if errors:
            raise InvoiceError("Resolve validation errors before approving: " + "; ".join(e["message"] for e in errors), 409)
        if any(d["kind"] == HARD for d in issues.get("duplicates", [])):
            raise InvoiceError("This invoice is an exact duplicate candidate. Reject it or correct the invoice number first.", 409)
        new_status = "approved"
    elif decision == "reject":
        if status in ("exported",):
            raise InvoiceError("Exported invoices cannot be rejected.", 409)
        new_status = "rejected"
    elif decision == "mark_review":
        if status in ("exported",):
            raise InvoiceError("Exported invoices cannot change review state.", 409)
        new_status = "needs_review"
    elif decision == "reopen":
        user.require("correct")
        if status not in ("approved", "rejected"):
            raise InvoiceError("Only approved or rejected invoices can be reopened.", 409)
        new_status = "needs_review"
    else:
        raise InvoiceError("Unknown decision.")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("UPDATE invoices SET status=?, updated_at=datetime('now') WHERE id=? AND organisation_id=?",
                     (new_status, invoice_id, user.organisation_id))
        conn.execute(
            "INSERT INTO approvals (organisation_id, invoice_id, decision, actor_user_id, note) VALUES (?,?,?,?,?)",
            (user.organisation_id, invoice_id, DECISION_RECORD[decision], user.id, note))
        audit.record(conn, user.organisation_id, user.id, f"invoice.{decision}", "invoice", invoice_id,
                     {"from": status, "to": new_status})
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return new_status


def _get(conn: sqlite3.Connection, org_id: int, invoice_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM invoices WHERE id = ? AND organisation_id = ? AND deleted_at IS NULL",
                       (invoice_id, org_id)).fetchone()
    if row is None:
        raise InvoiceError("Invoice not found.", 404)
    return row


def get_invoice(conn: sqlite3.Connection, org_id: int, invoice_id: int) -> sqlite3.Row:
    return _get(conn, org_id, invoice_id)
