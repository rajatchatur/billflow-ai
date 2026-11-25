"""BillFlow AI web application (Starlette + Jinja2 + sqlite3)."""
from __future__ import annotations

import json
import logging
import secrets
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app import audit
from app.auth import (CSRF_COOKIE, ROLES, CurrentUser, Forbidden, clear_session, current_user,
                      ensure_csrf_cookie, issue_session, verify_csrf)
from app.bootstrap import bootstrap
from app.config import BASE_DIR, Settings
from app.database import connect, init_db, transaction
from app.exports import guard_csv_cell, invoices_csv, line_items_csv
from app.extraction import ocr_available
from app.security import hash_password, verify_password
from app.services import invoices as inv
from app.services.files import UploadRejected, resolve_inside, validate_upload
from app.validation import check_gstin, gstin_state_code

log = logging.getLogger("billflow")
TEMPLATES = Environment(loader=FileSystemLoader(str(BASE_DIR / "app" / "templates")),
                        autoescape=select_autoescape(["html"]))
STATUS_LABELS = {
    "processing": "Processing", "needs_review": "Needs Review", "ready_for_approval": "Ready for Approval",
    "approved": "Approved", "rejected": "Rejected", "exported": "Exported", "failed": "Failed",
}
TEMPLATES.globals["STATUS_LABELS"] = STATUS_LABELS
_failed_logins: dict[str, list[float]] = defaultdict(list)
LOCK_WINDOW, LOCK_MAX = 300, 5


def _db(request: Request) -> sqlite3.Connection:
    return request.app.state.connect()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def render(request: Request, template: str, status: int = 200, user: CurrentUser | None = None,
           conn: sqlite3.Connection | None = None, **ctx: Any) -> HTMLResponse:
    csrf = request.cookies.get(CSRF_COOKIE) or secrets.token_urlsafe(32)
    flash = ctx.pop("flash", None) or request.query_params.get("ok")
    error = ctx.pop("error", None) or request.query_params.get("err")
    html = TEMPLATES.get_template(template).render(
        request=request, user=user, csrf=csrf, roles=ROLES, status_labels=STATUS_LABELS,
        flash=flash, error=error,
        ocr_ready=ocr_available(_settings(request).tesseract_cmd), **ctx)
    response = HTMLResponse(html, status_code=status)
    if request.cookies.get(CSRF_COOKIE) != csrf:
        response.set_cookie(CSRF_COOKIE, csrf, httponly=True, samesite="strict", path="/")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'"
    return response


def _redirect(url: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    params = []
    if ok:
        params.append("ok=" + quote(ok))
    if err:
        params.append("err=" + quote(err))
    return RedirectResponse(url + ("?" + "&".join(params) if params else ""), status_code=303)


async def _form(request: Request) -> dict[str, str]:
    body = (await request.body()).decode("utf-8", errors="replace")
    from urllib.parse import parse_qsl
    return {k: v for k, v in parse_qsl(body, keep_blank_values=True)}


def _auth(request: Request) -> tuple[sqlite3.Connection, CurrentUser | None]:
    conn = _db(request)
    return conn, current_user(request, conn, _settings(request).session_secret)


def _need_login(request: Request) -> RedirectResponse:
    return RedirectResponse("/login", status_code=303)



def _error_page(request: Request, status: int, message: str, user: CurrentUser | None = None,
                conn: sqlite3.Connection | None = None) -> HTMLResponse:
    return render(request, "error.html", status=status, user=user, conn=conn, message=message)


def _csrf_fail(request: Request, user: CurrentUser | None = None) -> HTMLResponse:
    return _error_page(request, 403, "Your form session expired. Reload the page and try again.", user)


def _client_or_404(conn: sqlite3.Connection, org: int, client_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM client_companies WHERE id = ? AND organisation_id = ? AND deleted_at IS NULL",
                        (client_id, org)).fetchone()


# ---------------------------------------------------------------- auth
async def login_page(request: Request) -> Response:
    conn, user = _auth(request)
    if user:
        return RedirectResponse("/dashboard", status_code=303)
    return render(request, "login.html", user=None)


async def login_submit(request: Request) -> Response:
    settings = _settings(request)
    form = await _form(request)
    if not verify_csrf(request, form.get("csrf")):
        return _csrf_fail(request)
    email = form.get("email", "").strip().lower()[:200]
    now = time.time()
    recent = [t for t in _failed_logins[email] if now - t < LOCK_WINDOW]
    _failed_logins[email] = recent
    if len(recent) >= LOCK_MAX:
        return render(request, "login.html", status=429, user=None,
                      error="Too many failed attempts. Wait five minutes and try again.")
    conn = _db(request)
    row = conn.execute("SELECT id, organisation_id, password_hash, is_active FROM users WHERE email = ?",
                       (email,)).fetchone()
    if row and row["is_active"] and verify_password(form.get("password", ""), row["password_hash"]):
        _failed_logins.pop(email, None)
        response = RedirectResponse("/dashboard", status_code=303)
        issue_session(response, row["id"], row["organisation_id"], settings.session_secret,
                      settings.session_max_age_seconds, settings.cookie_secure)
        audit.record(conn, row["organisation_id"], row["id"], "auth.login")
        return response
    # Constant-ish work for unknown users to limit account enumeration by timing.
    if not row:
        hash_password("dummy-password-for-timing", 1000)
    _failed_logins[email].append(now)
    if row:
        audit.record(conn, row["organisation_id"], row["id"], "auth.login_failed")
    return render(request, "login.html", status=401, user=None, error="Email or password is incorrect.")


async def logout(request: Request) -> Response:
    conn, user = _auth(request)
    form = await _form(request)
    if not verify_csrf(request, form.get("csrf")):
        return _csrf_fail(request, user)
    if user:
        audit.record(conn, user.organisation_id, user.id, "auth.logout")
    response = RedirectResponse("/login", status_code=303)
    clear_session(response)
    return response


# ---------------------------------------------------------------- dashboard
async def dashboard(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    org = user.organisation_id
    counts = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) AS n FROM invoices WHERE organisation_id = ? AND deleted_at IS NULL GROUP BY status",
        (org,)).fetchall()}
    total = sum(counts.values())
    dupes = conn.execute("SELECT COUNT(*) AS n FROM invoices WHERE organisation_id = ? AND duplicate_of_id IS NOT NULL "
                         "AND deleted_at IS NULL", (org,)).fetchone()["n"]
    clients = conn.execute("SELECT COUNT(*) AS n FROM client_companies WHERE organisation_id = ? AND deleted_at IS NULL",
                           (org,)).fetchone()["n"]
    conf = conn.execute("SELECT AVG(extraction_confidence) AS c FROM invoices WHERE organisation_id = ? "
                        "AND extraction_confidence IS NOT NULL", (org,)).fetchone()["c"]
    stats = {
        "total": total,
        "processing": counts.get("processing", 0),
        "needs_review": counts.get("needs_review", 0),
        "ready": counts.get("ready_for_approval", 0),
        "approved": counts.get("approved", 0),
        "exported": counts.get("exported", 0),
        "failed": counts.get("failed", 0),
        "duplicates": dupes,
        "clients": clients,
        "avg_confidence": round(conf * 100, 1) if conf is not None else None,
    }
    return render(request, "dashboard.html", user=user, conn=conn, stats=stats)


# ---------------------------------------------------------------- clients
async def clients_page(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    rows = conn.execute("SELECT * FROM client_companies WHERE organisation_id = ? AND deleted_at IS NULL "
                        "ORDER BY name", (user.organisation_id,)).fetchall()
    return render(request, "clients.html", user=user, conn=conn, clients=rows)


async def client_create(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    form = await _form(request)
    if not verify_csrf(request, form.get("csrf")):
        return _csrf_fail(request, user)
    try:
        user.require("manage_clients")
    except Forbidden as exc:
        return _redirect("/clients", err=str(exc))
    name = (form.get("name") or "").strip()
    gstin = (form.get("gstin") or "").strip().upper() or None
    if not name or len(name) > 150:
        return _redirect("/clients", err="Client name is required (max 150 characters).")
    if gstin:
        problems = check_gstin(gstin)
        if problems:
            return _redirect("/clients", err="Client GSTIN: " + problems[0] + ".")
    state_code = gstin_state_code(gstin) or (form.get("state_code") or "").strip() or None
    try:
        with transaction(conn):
            conn.execute(
                "INSERT INTO client_companies (organisation_id, name, gstin, pan, address, state, state_code, accounting_period) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (user.organisation_id, name, gstin, (form.get("pan") or "").strip().upper() or None,
                 (form.get("address") or "").strip() or None, (form.get("state") or "").strip() or None,
                 state_code, (form.get("accounting_period") or "").strip() or None))
            cid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            audit.record(conn, user.organisation_id, user.id, "client.created", "client_company", cid, {"name": name})
    except sqlite3.IntegrityError:
        return _redirect("/clients", err="A client with that name already exists.")
    return _redirect(f"/clients/{cid}", ok="Client company created.")


async def client_detail(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    cid = int(request.path_params["client_id"])
    client = _client_or_404(conn, user.organisation_id, cid)
    if not client:
        return _error_page(request, 404, "Client company not found.", user, conn)
    invoices_rows = conn.execute(
        "SELECT id, invoice_number, invoice_date, supplier_name, total_amount, status, extraction_confidence "
        "FROM invoices WHERE organisation_id = ? AND client_company_id = ? AND deleted_at IS NULL ORDER BY id DESC",
        (user.organisation_id, cid)).fetchall()
    vendors = conn.execute("SELECT DISTINCT supplier_name, supplier_gstin FROM invoices WHERE organisation_id = ? "
                           "AND client_company_id = ? AND supplier_name IS NOT NULL", (user.organisation_id, cid)).fetchall()
    return render(request, "client_detail.html", user=user, conn=conn, client=client, invoices=invoices_rows, vendors=vendors)


# ---------------------------------------------------------------- invoices
async def inbox(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    status = request.query_params.get("status") or ""
    if status and status not in STATUS_LABELS:
        status = ""
    sql = ("SELECT i.id, i.invoice_number, i.invoice_date, i.supplier_name, i.total_amount, i.status, "
           "i.extraction_confidence, i.duplicate_of_id, c.name AS client_name FROM invoices i "
           "JOIN client_companies c ON c.id = i.client_company_id WHERE i.organisation_id = ? AND i.deleted_at IS NULL")
    params: list[Any] = [user.organisation_id]
    if status:
        sql += " AND i.status = ?"
        params.append(status)
    sql += " ORDER BY i.id DESC LIMIT 500"
    rows = conn.execute(sql, params).fetchall()
    return render(request, "invoices.html", user=user, conn=conn, invoices=rows, filter_status=status,
                  title="Invoice inbox")


async def upload_page(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    clients = conn.execute("SELECT id, name FROM client_companies WHERE organisation_id = ? AND deleted_at IS NULL "
                           "ORDER BY name", (user.organisation_id,)).fetchall()
    return render(request, "upload.html", user=user, conn=conn, clients=clients,
                  max_mb=_settings(request).max_upload_bytes // (1024 * 1024))


async def upload_submit(request: Request) -> Response:
    """Raw-body upload. The client sends the file bytes with X-Filename and X-CSRF-Token headers."""
    conn, user = _auth(request)
    if not user:
        return JSONError(401, "Sign in required.")
    if not verify_csrf(request, None):
        return JSONError(403, "Form session expired. Reload the page and try again.")
    try:
        user.require("upload")
    except Forbidden as exc:
        return JSONError(403, str(exc))
    settings = _settings(request)
    try:
        cid = int(request.query_params.get("client_id", "0"))
    except ValueError:
        return JSONError(400, "Choose a valid client company.")
    if not _client_or_404(conn, user.organisation_id, cid):
        return JSONError(404, "Client company not found.")
    length = int(request.headers.get("content-length") or 0)
    if length > settings.max_upload_bytes:
        return JSONError(413, f"File exceeds the {settings.max_upload_bytes // (1024 * 1024)} MB limit.")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.max_upload_bytes:
            return JSONError(413, "File exceeds the upload size limit.")
    try:
        raw_name = unquote(request.headers.get("x-filename") or "")
        validated = validate_upload(raw_name, bytes(body), settings.max_upload_bytes)
    except UploadRejected as exc:
        return JSONError(exc.status, str(exc))
    invoice_id = inv.process_new_document(conn, settings, user, cid, validated)
    row = conn.execute("SELECT status, extraction_status FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    return JSONOk({"invoice_id": invoice_id, "status": row["status"], "extraction_status": row["extraction_status"],
                   "review_url": f"/invoices/{invoice_id}"})


def JSONError(status: int, message: str) -> Response:
    return Response(json.dumps({"error": message}), status_code=status, media_type="application/json")


def JSONOk(payload: dict[str, Any]) -> Response:
    return Response(json.dumps(payload), status_code=201, media_type="application/json")


async def invoice_review(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    invoice_id = int(request.path_params["invoice_id"])
    try:
        row = inv.get_invoice(conn, user.organisation_id, invoice_id)
    except inv.InvoiceError as exc:
        return _error_page(request, exc.status, str(exc), user, conn)
    view = inv.effective_view(conn, _settings(request), row)
    doc = conn.execute("SELECT original_filename, detected_type, sha256, size_bytes, created_at FROM source_documents "
                       "WHERE id = ?", (row["source_document_id"],)).fetchone()
    approvals = conn.execute(
        "SELECT a.decision, a.note, a.created_at, u.full_name FROM approvals a JOIN users u ON u.id = a.actor_user_id "
        "WHERE a.invoice_id = ? AND a.organisation_id = ? ORDER BY a.id DESC", (invoice_id, user.organisation_id)).fetchall()
    dup_rows = []
    for d in view["duplicates"]:
        target = conn.execute("SELECT id, invoice_number, invoice_date, total_amount, status FROM invoices "
                              "WHERE id = ? AND organisation_id = ?", (d["invoice_id"], user.organisation_id)).fetchone()
        if target:
            dup_rows.append({**d, "row": target})
    return render(request, "review.html", user=user, conn=conn, invoice=row, view=view, doc=doc,
                  approvals=approvals, dupes=dup_rows, pdf_ref=f"/invoices/{invoice_id}/file")


async def invoice_corrections(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    invoice_id = int(request.path_params["invoice_id"])
    form = await _form(request)
    if not verify_csrf(request, form.get("csrf")):
        return _csrf_fail(request, user)
    try:
        inv.save_corrections(conn, _settings(request), user, invoice_id, form)
    except (inv.InvoiceError, Forbidden) as exc:
        code = getattr(exc, "status", 403)
        return _redirect(f"/invoices/{invoice_id}", err=str(exc)) if code != 404 else _error_page(request, 404, str(exc), user, conn)
    return _redirect(f"/invoices/{invoice_id}", ok="Corrections saved and validation re-run.")


async def invoice_decision(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    invoice_id = int(request.path_params["invoice_id"])
    form = await _form(request)
    if not verify_csrf(request, form.get("csrf")):
        return _csrf_fail(request, user)
    decision = form.get("decision", "")
    try:
        new_status = inv.decide(conn, _settings(request), user, invoice_id, decision, form.get("note"))
    except (inv.InvoiceError, Forbidden) as exc:
        return _redirect(f"/invoices/{invoice_id}", err=str(exc))
    labels = {"approve": "Invoice approved.", "reject": "Invoice rejected.", "mark_review": "Marked for review.",
              "reopen": "Invoice reopened for review."}
    return _redirect(f"/invoices/{invoice_id}", ok=labels.get(decision, "Updated.") + f" Status: {STATUS_LABELS[new_status]}.")


async def invoice_retry(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    invoice_id = int(request.path_params["invoice_id"])
    form = await _form(request)
    if not verify_csrf(request, form.get("csrf")):
        return _csrf_fail(request, user)
    try:
        user.require("upload")
        row = inv.get_invoice(conn, user.organisation_id, invoice_id)
        if row["status"] in ("approved", "exported"):
            raise inv.InvoiceError("Approved or exported invoices cannot be reprocessed.", 409)
        inv.run_extraction(conn, _settings(request), user, invoice_id)
    except inv.InvoiceError as exc:
        return _redirect(f"/invoices/{invoice_id}", err=str(exc))
    except Forbidden as exc:
        return _redirect(f"/invoices/{invoice_id}", err=str(exc))
    audit.record(conn, user.organisation_id, user.id, "invoice.retry", "invoice", invoice_id)
    return _redirect(f"/invoices/{invoice_id}", ok="Processing re-run.")


async def invoice_file(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    invoice_id = int(request.path_params["invoice_id"])
    try:
        row = inv.get_invoice(conn, user.organisation_id, invoice_id)
    except inv.InvoiceError as exc:
        return _error_page(request, exc.status, str(exc), user, conn)
    doc = conn.execute("SELECT stored_filename, original_filename, detected_type FROM source_documents WHERE id = ? "
                       "AND organisation_id = ?", (row["source_document_id"], user.organisation_id)).fetchone()
    if not doc:
        return _error_page(request, 404, "Document not found.", user, conn)
    try:
        path = resolve_inside(_settings(request).upload_dir, doc["stored_filename"])
    except UploadRejected:
        return _error_page(request, 404, "Document not found.", user, conn)
    if not path.is_file():
        return _error_page(request, 404, "Document file is missing from storage.", user, conn)
    mime = {"pdf": "application/pdf", "png": "image/png", "jpeg": "image/jpeg"}[doc["detected_type"]]
    audit.record(conn, user.organisation_id, user.id, "document.downloaded", "invoice", invoice_id)
    safe_name = doc["original_filename"].replace('"', "")
    return Response(path.read_bytes(), media_type=mime, headers={
        "Content-Disposition": f'inline; filename="{safe_name}"', "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store"})


# ---------------------------------------------------------------- lists
async def duplicates_page(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    rows = conn.execute(
        "SELECT i.id, i.invoice_number, i.invoice_date, i.supplier_name, i.total_amount, i.status, i.validation_json, "
        "c.name AS client_name FROM invoices i JOIN client_companies c ON c.id = i.client_company_id "
        "WHERE i.organisation_id = ? AND i.deleted_at IS NULL AND i.validation_json LIKE '%duplicates%' "
        "AND i.validation_json NOT LIKE '%\"duplicates\": []%' ORDER BY i.id DESC", (user.organisation_id,)).fetchall()
    items = []
    for r in rows:
        dupes = json.loads(r["validation_json"] or "{}").get("duplicates", [])
        if dupes:
            items.append({"row": r, "dupes": dupes})
    return render(request, "duplicates.html", user=user, conn=conn, items=items)


async def exceptions_page(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    rows = conn.execute(
        "SELECT i.id, i.invoice_number, i.supplier_name, i.status, i.failure_reason, i.validation_json, c.name AS client_name "
        "FROM invoices i JOIN client_companies c ON c.id = i.client_company_id WHERE i.organisation_id = ? "
        "AND i.deleted_at IS NULL AND i.status IN ('needs_review','failed') ORDER BY i.id DESC", (user.organisation_id,)).fetchall()
    items = []
    for r in rows:
        issues = json.loads(r["validation_json"] or "{}").get("issues", [])
        items.append({"row": r, "issues": issues})
    return render(request, "exceptions.html", user=user, conn=conn, items=items)


async def vendors_page(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    rows = conn.execute(
        "SELECT supplier_name, supplier_gstin, COUNT(*) AS invoices, SUM(CASE WHEN status IN ('approved','exported') "
        "THEN 1 ELSE 0 END) AS approved, SUM(total_amount) AS total FROM invoices WHERE organisation_id = ? "
        "AND deleted_at IS NULL AND supplier_name IS NOT NULL GROUP BY supplier_name, supplier_gstin ORDER BY supplier_name",
        (user.organisation_id,)).fetchall()
    return render(request, "vendors.html", user=user, conn=conn, vendors=rows)


async def exports_page(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    counts = conn.execute("SELECT COUNT(*) AS n FROM invoices WHERE organisation_id = ? AND status = 'approved'",
                          (user.organisation_id,)).fetchone()["n"]
    return render(request, "exports.html", user=user, conn=conn, approved_count=counts)


async def export_csv(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    form = await _form(request)
    if not verify_csrf(request, form.get("csrf")):
        return _csrf_fail(request, user)
    try:
        user.require("export")
    except Forbidden as exc:
        return _redirect("/exports", err=str(exc))
    kind = form.get("kind", "invoices")
    rows = conn.execute(
        "SELECT i.*, c.name AS client_company FROM invoices i JOIN client_companies c ON c.id = i.client_company_id "
        "WHERE i.organisation_id = ? AND i.status = 'approved' AND i.deleted_at IS NULL ORDER BY i.id",
        (user.organisation_id,)).fetchall()
    if not rows:
        return _redirect("/exports", err="No approved invoices to export.")
    ids = [r["id"] for r in rows]
    if kind == "line_items":
        lines = []
        for r in rows:
            for li in conn.execute("SELECT * FROM invoice_line_items WHERE invoice_id = ? ORDER BY position", (r["id"],)).fetchall():
                lines.append({**{k: guard_csv_cell(li[k]) for k in li.keys()}, "invoice_id": r["id"],
                              "invoice_number": guard_csv_cell(r["invoice_number"]), "line_no": li["position"]})
        body, filename = line_items_csv(lines), "billflow-line-items.csv"
    else:
        payload = []
        for r in rows:
            vals = json.loads(r["corrected_json"] or r["extracted_json"] or "{}")
            payload.append({"invoice_id": r["id"], "client_company": guard_csv_cell(r["client_company"]),
                            **{k: guard_csv_cell(vals.get(k)) for k in
                               ("supplier_name", "supplier_gstin", "buyer_name", "buyer_gstin", "invoice_number",
                                "invoice_date", "due_date", "place_of_supply", "subtotal", "discount", "taxable_value",
                                "cgst", "sgst", "igst", "cess", "rounding", "total_amount")},
                            "status": "exported"})
        body, filename = invoices_csv(payload), "billflow-approved-invoices.csv"
    with transaction(conn):
        # Only the invoice CSV marks invoices as exported; the line-item CSV is a companion download.
        if kind != "line_items":
            conn.execute(f"UPDATE invoices SET status='exported', updated_at=datetime('now') WHERE organisation_id = ? "
                         f"AND id IN ({','.join('?' * len(ids))})", (user.organisation_id, *ids))
            audit.record(conn, user.organisation_id, user.id, "invoice.exported", "invoice", None, {"invoice_ids": ids})
        audit.record(conn, user.organisation_id, user.id, "export.generated", "export", None,
                     {"kind": kind, "invoice_count": len(ids), "invoice_ids": ids})
    return Response(body, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"',
                             "Cache-Control": "no-store"})


async def audit_page(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    rows = conn.execute(
        "SELECT a.created_at, a.action, a.entity_type, a.entity_id, a.details_json, u.email FROM audit_events a "
        "LEFT JOIN users u ON u.id = a.user_id WHERE a.organisation_id = ? ORDER BY a.id DESC LIMIT 300",
        (user.organisation_id,)).fetchall()
    return render(request, "audit.html", user=user, conn=conn, events=rows)


async def users_page(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    rows = conn.execute("SELECT id, email, full_name, role, is_active FROM users WHERE organisation_id = ? ORDER BY id",
                        (user.organisation_id,)).fetchall()
    return render(request, "users.html", user=user, conn=conn, users=rows)


async def user_create(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    form = await _form(request)
    if not verify_csrf(request, form.get("csrf")):
        return _csrf_fail(request, user)
    try:
        user.require("manage_users")
    except Forbidden as exc:
        return _redirect("/users", err=str(exc))
    email = (form.get("email") or "").strip().lower()
    name = (form.get("full_name") or "").strip()
    role = form.get("role", "")
    password = form.get("password", "")
    if role not in ROLES or (role == "owner" and user.role != "owner"):
        return _redirect("/users", err="Choose a valid role. Only an owner can create owners.")
    if "@" not in email or not name or len(password) < 12:
        return _redirect("/users", err="Enter an email, a name and a password of at least 12 characters.")
    try:
        with transaction(conn):
            conn.execute("INSERT INTO users (organisation_id, email, full_name, password_hash, role) VALUES (?,?,?,?,?)",
                         (user.organisation_id, email, name, hash_password(password, _settings(request).pbkdf2_iterations), role))
            uid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            audit.record(conn, user.organisation_id, user.id, "user.permission_changed", "user", uid,
                         {"action": "created", "role": role})
    except sqlite3.IntegrityError:
        return _redirect("/users", err="That email is already registered.")
    return _redirect("/users", ok="User created.")


async def user_role(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    target_id = int(request.path_params["user_id"])
    form = await _form(request)
    if not verify_csrf(request, form.get("csrf")):
        return _csrf_fail(request, user)
    try:
        user.require("manage_users")
    except Forbidden as exc:
        return _redirect("/users", err=str(exc))
    new_role = form.get("role", "")
    action = form.get("action", "role")
    target = conn.execute("SELECT id, role, is_active FROM users WHERE id = ? AND organisation_id = ?",
                          (target_id, user.organisation_id)).fetchone()
    if not target:
        return _error_page(request, 404, "User not found.", user, conn)
    if target["role"] == "owner" and user.role != "owner":
        return _redirect("/users", err="Only an owner can change an owner account.")
    if target_id == user.id and action == "deactivate":
        return _redirect("/users", err="You cannot deactivate your own account.")
    with transaction(conn):
        if action == "deactivate":
            conn.execute("UPDATE users SET is_active = 0, updated_at=datetime('now') WHERE id = ? AND organisation_id = ?",
                         (target_id, user.organisation_id))
            audit.record(conn, user.organisation_id, user.id, "user.permission_changed", "user", target_id,
                         {"action": "deactivated"})
        else:
            if new_role not in ROLES or (new_role == "owner" and user.role != "owner"):
                return _redirect("/users", err="Choose a valid role.")
            conn.execute("UPDATE users SET role = ?, updated_at=datetime('now') WHERE id = ? AND organisation_id = ?",
                         (new_role, target_id, user.organisation_id))
            audit.record(conn, user.organisation_id, user.id, "user.permission_changed", "user", target_id,
                         {"action": "role_changed", "from": target["role"], "to": new_role})
    return _redirect("/users", ok="User updated.")


async def settings_page(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    org = conn.execute("SELECT * FROM organisations WHERE id = ?", (user.organisation_id,)).fetchone()
    return render(request, "settings.html", user=user, conn=conn, org=org, settings=_settings(request))


async def settings_save(request: Request) -> Response:
    conn, user = _auth(request)
    if not user:
        return _need_login(request)
    form = await _form(request)
    if not verify_csrf(request, form.get("csrf")):
        return _csrf_fail(request, user)
    try:
        user.require("settings")
    except Forbidden as exc:
        return _redirect("/settings", err=str(exc))
    name = (form.get("name") or "").strip()
    if not name or len(name) > 150:
        return _redirect("/settings", err="Organisation name is required (max 150 characters).")
    with transaction(conn):
        conn.execute("UPDATE organisations SET name = ?, updated_at = datetime('now') WHERE id = ?", (name, user.organisation_id))
        audit.record(conn, user.organisation_id, user.id, "settings.updated", "organisation", user.organisation_id)
    return _redirect("/settings", ok="Settings saved.")


async def healthz(request: Request) -> Response:
    return PlainTextResponse("ok")


async def root(request: Request) -> Response:
    return RedirectResponse("/dashboard" if current_user(request, _db(request), _settings(request).session_secret) else "/login", status_code=303)


def build_routes() -> list:
    return [
        Mount("/static", app=StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static"),
        Route("/", root), Route("/healthz", healthz),
        Route("/login", login_page), Route("/login", login_submit, methods=["POST"]),
        Route("/logout", logout, methods=["POST"]),
        Route("/dashboard", dashboard),
        Route("/clients", clients_page), Route("/clients", client_create, methods=["POST"]),
        Route("/clients/{client_id:int}", client_detail),
        Route("/invoices", inbox), Route("/invoices/upload", upload_page),
        Route("/invoices/upload", upload_submit, methods=["POST"]),
        Route("/invoices/{invoice_id:int}", invoice_review),
        Route("/invoices/{invoice_id:int}/corrections", invoice_corrections, methods=["POST"]),
        Route("/invoices/{invoice_id:int}/decision", invoice_decision, methods=["POST"]),
        Route("/invoices/{invoice_id:int}/retry", invoice_retry, methods=["POST"]),
        Route("/invoices/{invoice_id:int}/file", invoice_file),
        Route("/duplicates", duplicates_page), Route("/exceptions", exceptions_page),
        Route("/vendors", vendors_page), Route("/exports", exports_page),
        Route("/exports/csv", export_csv, methods=["POST"]),
        Route("/audit", audit_page), Route("/users", users_page), Route("/users", user_create, methods=["POST"]),
        Route("/users/{user_id:int}", user_role, methods=["POST"]),
        Route("/settings", settings_page), Route("/settings", settings_save, methods=["POST"]),
    ]


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings.from_env()
    if not settings.session_secret:
        settings = Settings(**{**settings.__dict__, "session_secret": secrets.token_urlsafe(48)})
        log.warning("BILLFLOW_SESSION_SECRET not set: using a random secret; sessions end on restart.")
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    init_db(settings.database_path)
    conn = connect(settings.database_path)
    status = bootstrap(conn, settings)
    conn.close()
    log.info("Bootstrap: %s", status)

    app = Starlette(routes=build_routes())
    app.state.settings = settings
    app.state.bootstrap_status = status
    app.state.connect = lambda: connect(settings.database_path)
    return app


app = create_app() if __import__("os").environ.get("BILLFLOW_DISABLE_AUTOAPP") != "1" else None
