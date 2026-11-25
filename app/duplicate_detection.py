"""Duplicate candidate detection. Every query is scoped to one organisation and client company."""
from __future__ import annotations

import sqlite3
from typing import Any

HARD = "exact_duplicate"          # same supplier GSTIN, invoice number, date and total
SOFT = "possible_duplicate"       # same supplier GSTIN and invoice number, but date or total differs
FILE = "identical_file"           # same file bytes already uploaded for this client


def find_duplicates(conn: sqlite3.Connection, organisation_id: int, client_company_id: int,
                    invoice_id: int | None, supplier_gstin: str | None, normalised_number: str | None,
                    invoice_date: str | None, total_amount: float | None,
                    file_sha256: str | None) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if file_sha256:
        rows = conn.execute(
            "SELECT i.id FROM invoices i JOIN source_documents d ON d.id = i.source_document_id "
            "WHERE i.organisation_id = ? AND i.client_company_id = ? AND d.sha256 = ? "
            "AND i.deleted_at IS NULL AND (? IS NULL OR i.id != ?)",
            (organisation_id, client_company_id, file_sha256, invoice_id, invoice_id)).fetchall()
        for r in rows:
            found.append({"invoice_id": r["id"], "kind": FILE, "reason": "The same file has already been uploaded for this client."})
    if supplier_gstin and normalised_number:
        rows = conn.execute(
            "SELECT id, invoice_date, total_amount FROM invoices "
            "WHERE organisation_id = ? AND client_company_id = ? AND supplier_gstin = ? "
            "AND normalised_invoice_number = ? AND deleted_at IS NULL AND status != 'rejected' "
            "AND (? IS NULL OR id != ?)",
            (organisation_id, client_company_id, supplier_gstin, normalised_number, invoice_id, invoice_id)).fetchall()
        for r in rows:
            same_date = r["invoice_date"] == invoice_date
            same_total = r["total_amount"] is not None and total_amount is not None and abs(r["total_amount"] - total_amount) < 0.005
            if same_date and same_total:
                kind, reason = HARD, "Same supplier GSTIN, invoice number, date and total."
            else:
                kind, reason = SOFT, "Same supplier GSTIN and invoice number, but the date or total differs. Review both."
            if not any(f["invoice_id"] == r["id"] for f in found):
                found.append({"invoice_id": r["id"], "kind": kind, "reason": reason})
    return found
