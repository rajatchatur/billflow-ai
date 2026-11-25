"""Integration tests through the real Starlette app and SQLite database."""
import json

from app.bootstrap import bootstrap
from app.database import init_db
from app.security import hash_password
from scripts.synthetic_invoices import CASES, _lines_for, build_all, build_pdf
from tests.conftest import OWNER_EMAIL, OWNER_PASSWORD, add_org, add_user, login


def make_client(owner_client, name="Sharma Textiles Pvt Ltd", gstin=None):
    form = {"name": name, "csrf": owner_client.csrf()}
    if gstin:
        form["gstin"] = gstin
    res = owner_client.post("/clients", form=form)
    assert res.status_code == 303, res.text
    return int(res.location.rsplit("/", 1)[1].split("?")[0])


def upload(client, client_id, filename, content):
    return client.post(f"/invoices/upload?client_id={client_id}", body=content,
                       headers={"X-Filename": filename, "X-CSRF-Token": client.csrf()})


def invoice_pdf(case):
    return build_pdf(_lines_for(CASES[case]))


def csrf_form(client, **fields):
    return {**fields, "csrf": client.csrf()}


def invoice_row(db, invoice_id):
    return db.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()


# ---------------- startup and bootstrap
def test_startup_creates_org_and_owner_once(settings, app, db):
    assert db.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 1
    owner = db.execute("SELECT role, organisation_id FROM users WHERE email = ?", (OWNER_EMAIL,)).fetchone()
    assert owner["role"] == "owner"
    assert "password" not in json.dumps(dict(owner)).lower()
    bootstrap_again = bootstrap(db, settings)
    assert bootstrap_again == "existing"


def test_bootstrap_refuses_weak_password(settings):
    from dataclasses import replace
    weak = replace(settings, database_path=settings.database_path.parent / "weak.sqlite3",
                   admin_password="short")
    init_db(weak.database_path)
    from app.database import connect
    conn = connect(weak.database_path)
    assert bootstrap(conn, weak).startswith("skipped")
    assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0


def test_health_and_root_redirect(client):
    assert client.get("/healthz").text == "ok"
    res = client.get("/")
    assert res.status_code == 303 and res.location == "/login"


def test_unauthenticated_pages_redirect_to_login(client):
    for path in ["/dashboard", "/clients", "/invoices", "/exports", "/audit", "/users", "/invoices/1"]:
        res = client.get(path)
        assert res.status_code == 303 and res.location == "/login", path


# ---------------- authentication
def test_login_success_sets_hardened_session_cookie(client):
    res = login(client, OWNER_EMAIL, OWNER_PASSWORD)
    assert res.status_code == 303 and res.location == "/dashboard"
    assert "bf_session" in client.cookies
    raw = [v.decode() for k, v in res._all if k.lower() == b"set-cookie" and b"bf_session" in v][0]
    assert "HttpOnly" in raw and "SameSite=strict" in raw


def test_wrong_password_is_rejected_without_detail(client):
    res = login(client, OWNER_EMAIL, "wrong-password-value")
    assert res.status_code == 401
    assert "Email or password is incorrect" in res.text and "bf_session" not in client.cookies


def test_login_requires_csrf_token(client):
    client.get("/login")
    res = client.post("/login", form={"email": OWNER_EMAIL, "password": OWNER_PASSWORD, "csrf": "forged"})
    assert res.status_code == 403


def test_repeated_failures_lock_the_account_temporarily(client):
    for _ in range(5):
        login(client, "lock@test.example", "not-the-password-1")
    res = login(client, "lock@test.example", "not-the-password-1")
    assert res.status_code == 429


def test_logout_requires_csrf_and_clears_session(owner_client):
    bad = owner_client.post("/logout", form={"csrf": "nope"})
    assert bad.status_code == 403
    ok = owner_client.post("/logout", form={"csrf": owner_client.csrf()})
    assert ok.status_code == 303
    assert owner_client.get("/dashboard").status_code == 303


def test_tampered_session_cookie_is_ignored(owner_client):
    owner_client.cookies["bf_session"] = owner_client.cookies["bf_session"][:-3] + "abc"
    assert owner_client.get("/dashboard").status_code == 303


def test_state_changing_post_without_csrf_is_blocked(owner_client):
    res = owner_client.post("/clients", form={"name": "No Token Client"})
    assert res.status_code == 403


# ---------------- clients
def test_owner_creates_client_and_invalid_gstin_is_rejected(owner_client):
    cid = make_client(owner_client)
    page = owner_client.get(f"/clients/{cid}")
    assert page.status_code == 200 and "Sharma Textiles" in page.text
    bad = owner_client.post("/clients", form=csrf_form(owner_client, name="Bad GST Co", gstin="27AAPFU0939F1ZX"))
    assert "checksum" in bad.location


def test_duplicate_client_name_rejected(owner_client):
    make_client(owner_client, name="Unique Client Co")
    res = owner_client.post("/clients", form=csrf_form(owner_client, name="Unique Client Co"))
    assert "already exists" in res.location


# ---------------- roles
def test_reviewer_cannot_create_client_or_upload(app, settings, client, db):
    org = db.execute("SELECT organisation_id FROM users WHERE email = ?", (OWNER_EMAIL,)).fetchone()["organisation_id"]
    add_user(db, settings, org, "reviewer@test.example", "reviewer")
    login(client, "reviewer@test.example", OWNER_PASSWORD)
    res = client.post("/clients", form=csrf_form(client, name="Reviewer Client"))
    assert res.status_code == 303 and "not allowed" in res.location
    cid = db.execute("SELECT id FROM client_companies WHERE organisation_id = ? LIMIT 1", (org,)).fetchone()
    assert cid is None  # nothing was created
    client.get("/invoices/upload")
    up = upload(client, 1, "x.pdf", b"%PDF-1.4 x")
    assert up.status_code == 403


def test_accountant_cannot_approve_but_reviewer_can(app, settings, client, db, owner_client):
    org = db.execute("SELECT organisation_id FROM users WHERE email = ?", (OWNER_EMAIL,)).fetchone()["organisation_id"]
    add_user(db, settings, org, "acc@test.example", "accountant")
    add_user(db, settings, org, "rev@test.example", "reviewer")
    cid = make_client(owner_client)
    up = upload(owner_client, cid, "good.pdf", invoice_pdf("cgst_sgst_valid"))
    assert up.status_code == 201
    inv_id = up.json()["invoice_id"]
    login(client, "acc@test.example", OWNER_PASSWORD)
    denied = client.post(f"/invoices/{inv_id}/decision", form=csrf_form(client, decision="approve"))
    assert "not allowed" in denied.location
    assert invoice_row(db, inv_id)["status"] == "ready_for_approval"
    login(client, "rev@test.example", OWNER_PASSWORD)
    ok = client.post(f"/invoices/{inv_id}/decision", form=csrf_form(client, decision="approve"))
    assert "approved" in ok.location.lower()
    assert invoice_row(db, inv_id)["status"] == "approved"


# ---------------- ingestion and extraction
def test_full_workflow_upload_correct_approve_export_audit(owner_client, db):
    cid = make_client(owner_client)
    up = upload(owner_client, cid, "SF-0412.pdf", invoice_pdf("cgst_sgst_valid"))
    assert up.status_code == 201, up.text
    body = up.json()
    inv_id = body["invoice_id"]
    assert body["extraction_status"] == "extracted" and body["status"] == "ready_for_approval"

    review = owner_client.get(f"/invoices/{inv_id}")
    assert review.status_code == 200 and "SF/2026/0412" in review.text and "Demo Foods LLP" in review.text

    # Correct the grand total: the original extraction must be kept, the correction stored separately.
    fix = owner_client.post(f"/invoices/{inv_id}/corrections",
                            form=csrf_form(owner_client, invoice_number="SF/2026/0412A", total_amount="15340.00"))
    assert "saved" in fix.location.lower()
    row = invoice_row(db, inv_id)
    original = json.loads(row["extracted_json"])
    corrected = json.loads(row["corrected_json"])
    assert original["invoice_number"] == "SF/2026/0412"
    assert corrected["invoice_number"] == "SF/2026/0412A"
    assert row["normalised_invoice_number"] == "SF20260412A"

    approve = owner_client.post(f"/invoices/{inv_id}/decision", form=csrf_form(owner_client, decision="approve", note="checked"))
    assert "approved" in approve.location.lower()
    assert invoice_row(db, inv_id)["status"] == "approved"


    export = owner_client.post("/exports/csv", form=csrf_form(owner_client, kind="line_items"))
    assert export.status_code == 200 and "Software consulting" in export.text
    assert invoice_row(db, inv_id)["status"] == "approved"  # companion download does not change status
    export = owner_client.post("/exports/csv", form=csrf_form(owner_client, kind="invoices"))
    assert export.status_code == 200
    assert "attachment" in export.headers["content-disposition"]
    lines = export.text.strip().splitlines()
    assert lines[0].startswith("invoice_id,client_company")
    assert "SF/2026/0412A" in export.text and "Synthetic Traders" in export.text
    assert invoice_row(db, inv_id)["status"] == "exported"

    assert invoice_row(db, inv_id)["status"] == "exported"

    audit = owner_client.get("/audit")
    for action in ("document.uploaded", "invoice.extracted", "validation.performed", "field.corrected",
                   "invoice.approve", "export.generated", "invoice.exported"):
        assert action in audit.text, action


def test_correction_changes_are_audited_with_previous_and_new_values(owner_client, db):
    cid = make_client(owner_client)
    inv_id = upload(owner_client, cid, "a.pdf", invoice_pdf("cgst_sgst_valid")).json()["invoice_id"]
    owner_client.post(f"/invoices/{inv_id}/corrections", form=csrf_form(owner_client, buyer_name="Corrected Buyer LLP"))
    event = db.execute("SELECT details_json FROM audit_events WHERE action = 'field.corrected' ORDER BY id DESC LIMIT 1").fetchone()
    details = json.loads(event["details_json"])
    assert details["changes"]["buyer_name"] == {"from": "Demo Foods LLP", "to": "Corrected Buyer LLP"}


def test_export_is_approved_only(owner_client, db):
    cid = make_client(owner_client)
    upload(owner_client, cid, "ok.pdf", invoice_pdf("cgst_sgst_valid"))
    none = owner_client.post("/exports/csv", form=csrf_form(owner_client, kind="invoices"))
    assert "No approved" in none.location
    assert owner_client.get("/exports").status_code == 200


def test_incorrect_total_is_flagged_and_blocks_approval(owner_client, db):
    cid = make_client(owner_client)
    inv_id = upload(owner_client, cid, "bad.pdf", invoice_pdf("incorrect_total")).json()["invoice_id"]
    row = invoice_row(db, inv_id)
    assert row["status"] == "needs_review"
    assert "TOTAL_MISMATCH" in row["validation_json"]
    res = owner_client.post(f"/invoices/{inv_id}/decision", form=csrf_form(owner_client, decision="approve"))
    assert "Resolve validation errors" in res.location
    assert invoice_row(db, inv_id)["status"] == "needs_review"


def test_bad_gstin_checksum_is_flagged(owner_client, db):
    cid = make_client(owner_client)
    inv_id = upload(owner_client, cid, "gst.pdf", invoice_pdf("bad_gstin_checksum")).json()["invoice_id"]
    assert "GSTIN_INVALID" in invoice_row(db, inv_id)["validation_json"]


def test_missing_buyer_gstin_is_warning_not_blocking(owner_client, db):
    cid = make_client(owner_client)
    inv_id = upload(owner_client, cid, "nogst.pdf", invoice_pdf("missing_buyer_gstin")).json()["invoice_id"]
    row = invoice_row(db, inv_id)
    assert "GSTIN_MISSING" in row["validation_json"]
    assert row["status"] == "ready_for_approval"


def test_igst_invoice_extracts_and_validates(owner_client, db):
    cid = make_client(owner_client)
    inv_id = upload(owner_client, cid, "igst.pdf", invoice_pdf("igst_valid")).json()["invoice_id"]
    row = invoice_row(db, inv_id)
    assert row["status"] == "ready_for_approval"
    assert json.loads(row["extracted_json"])["igst"] == "1800.00"


def test_discount_and_rounding_invoice_is_consistent(owner_client, db):
    cid = make_client(owner_client)
    inv_id = upload(owner_client, cid, "dr.pdf", invoice_pdf("discount_rounding")).json()["invoice_id"]
    row = invoice_row(db, inv_id)
    assert row["status"] == "ready_for_approval"
    data = json.loads(row["extracted_json"])
    assert data["discount"] == "500.00" and data["rounding"] == "0.40"


def test_scanned_pdf_without_text_is_manual_entry(owner_client, db):
    cid = make_client(owner_client)
    res = upload(owner_client, cid, "scan.pdf", build_all()["blank_scan_like"])
    assert res.status_code == 201
    row = invoice_row(db, res.json()["invoice_id"])
    assert row["status"] == "needs_review" and row["extraction_status"] == "unavailable"
    assert "no selectable text" in row["failure_reason"].lower()


def test_image_upload_without_tesseract_reports_unavailable(owner_client, db):
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.new("RGB", (60, 60), "white").save(buf, "PNG")
    cid = make_client(owner_client)
    res = upload(owner_client, cid, "scan.png", buf.getvalue())
    assert res.status_code == 201
    row = invoice_row(db, res.json()["invoice_id"])
    assert row["extraction_status"] == "unavailable"
    assert row["status"] == "needs_review"


def test_invalid_file_uploads_are_rejected_over_http(owner_client, db):
    cid = make_client(owner_client)
    assert upload(owner_client, cid, "evil.pdf", b"<html>not a pdf</html>").status_code == 400
    assert upload(owner_client, cid, "fake.png", b"%PDF-1.4 real pdf bytes").status_code == 400
    assert upload(owner_client, cid, "big.pdf", b"%PDF-1.4" + b"0" * (3 * 1024 * 1024)).status_code == 413
    assert upload(owner_client, cid, "empty.pdf", b"").status_code == 400
    assert db.execute("SELECT COUNT(*) AS n FROM source_documents").fetchone()["n"] == 0


def test_duplicate_identical_file_is_flagged_and_approval_blocked(owner_client, db):
    cid = make_client(owner_client)
    first = upload(owner_client, cid, "one.pdf", invoice_pdf("cgst_sgst_valid")).json()["invoice_id"]
    second_res = upload(owner_client, cid, "one-copy.pdf", invoice_pdf("cgst_sgst_valid")).json()
    second = second_res["invoice_id"]
    row = invoice_row(db, second)
    assert row["duplicate_of_id"] == first
    assert "identical_file" in row["validation_json"] or "exact_duplicate" in row["validation_json"]
    res = owner_client.post(f"/invoices/{second}/decision", form=csrf_form(owner_client, decision="approve"))
    assert "exact duplicate" in res.location or "validation errors" in res.location.lower()
    assert invoice_row(db, second)["status"] != "approved"
    assert owner_client.get("/duplicates").status_code == 200


def test_same_number_date_total_different_file_is_exact_duplicate(owner_client, db):
    cid = make_client(owner_client)
    first = upload(owner_client, cid, "a.pdf", invoice_pdf("cgst_sgst_valid")).json()["invoice_id"]
    variant = build_pdf(_lines_for(CASES["cgst_sgst_valid"]) + ["Copy for transporter"])
    second = upload(owner_client, cid, "b.pdf", variant).json()["invoice_id"]
    row = invoice_row(db, second)
    assert row["duplicate_of_id"] == first
    assert '"exact_duplicate"' in row["validation_json"]


def test_upload_filename_header_is_decoded_and_sanitised(owner_client, db):
    cid = make_client(owner_client)
    res = owner_client.post(f"/invoices/upload?client_id={cid}", body=invoice_pdf("cgst_sgst_valid"),
                            headers={"X-Filename": "Invoice%20No%201%20%28final%29.pdf", "X-CSRF-Token": owner_client.csrf()})
    assert res.status_code == 201
    name = db.execute("SELECT original_filename FROM source_documents").fetchone()["original_filename"]
    assert name == "Invoice No 1 _final_.pdf"  # parentheses are replaced by safe-name rules


def test_rejected_invoice_cannot_be_corrected_without_reopening(owner_client, db):
    cid = make_client(owner_client)
    inv_id = upload(owner_client, cid, "rj.pdf", invoice_pdf("cgst_sgst_valid")).json()["invoice_id"]
    owner_client.post(f"/invoices/{inv_id}/decision", form=csrf_form(owner_client, decision="reject"))
    res = owner_client.post(f"/invoices/{inv_id}/corrections", form=csrf_form(owner_client, buyer_name="Changed"))
    assert "Reopen" in res.location
    assert invoice_row(db, inv_id)["status"] == "rejected"


def test_correction_on_approved_invoice_is_refused(owner_client, db):
    cid = make_client(owner_client)
    inv_id = upload(owner_client, cid, "x.pdf", invoice_pdf("cgst_sgst_valid")).json()["invoice_id"]
    owner_client.post(f"/invoices/{inv_id}/decision", form=csrf_form(owner_client, decision="approve"))
    res = owner_client.post(f"/invoices/{inv_id}/corrections", form=csrf_form(owner_client, buyer_name="Sneaky"))
    assert "cannot be edited" in res.location
    assert invoice_row(db, inv_id)["corrected_json"] is None


def test_reject_then_reopen(owner_client, db):
    cid = make_client(owner_client)
    inv_id = upload(owner_client, cid, "r.pdf", invoice_pdf("cgst_sgst_valid")).json()["invoice_id"]
    owner_client.post(f"/invoices/{inv_id}/decision", form=csrf_form(owner_client, decision="reject", note="wrong client"))
    assert invoice_row(db, inv_id)["status"] == "rejected"
    owner_client.post(f"/invoices/{inv_id}/decision", form=csrf_form(owner_client, decision="reopen"))
    assert invoice_row(db, inv_id)["status"] == "needs_review"
    rejection = db.execute("SELECT note FROM approvals WHERE invoice_id = ? AND decision = 'rejected'", (inv_id,)).fetchone()
    assert rejection["note"] == "wrong client"


def test_original_file_download_is_private_and_audited(owner_client, db, settings):
    cid = make_client(owner_client)
    content = invoice_pdf("cgst_sgst_valid")
    inv_id = upload(owner_client, cid, "keep.pdf", content).json()["invoice_id"]
    res = owner_client.get(f"/invoices/{inv_id}/file")
    assert res.status_code == 200 and res.content == content
    assert res.headers["content-type"] == "application/pdf"
    assert "no-store" in res.headers["cache-control"]
    stored = db.execute("SELECT stored_filename FROM source_documents").fetchone()["stored_filename"]
    assert (settings.upload_dir / stored).read_bytes() == content  # stored unmodified
    assert "document.downloaded" in owner_client.get("/audit").text


def test_user_management_and_role_change_audited(owner_client, db):
    res = owner_client.post("/users", form=csrf_form(owner_client, full_name="New Acc", email="new.acc@test.example",
                                                     password="another-long-password", role="accountant"))
    assert "created" in res.location.lower()
    uid = db.execute("SELECT id FROM users WHERE email = 'new.acc@test.example'").fetchone()["id"]
    owner_client.post(f"/users/{uid}", form=csrf_form(owner_client, action="role", role="reviewer"))
    assert db.execute("SELECT role FROM users WHERE id = ?", (uid,)).fetchone()["role"] == "reviewer"
    assert "user.permission_changed" in owner_client.get("/audit").text


def test_weak_password_for_new_user_is_refused(owner_client):
    res = owner_client.post("/users", form=csrf_form(owner_client, full_name="X", email="x@test.example",
                                                     password="short", role="accountant"))
    assert "12 characters" in res.location


def test_owner_cannot_be_deactivated_by_self(owner_client, db):
    uid = db.execute("SELECT id FROM users WHERE email = ?", (OWNER_EMAIL,)).fetchone()["id"]
    res = owner_client.post(f"/users/{uid}", form=csrf_form(owner_client, action="deactivate"))
    assert "cannot deactivate" in res.location


def test_pages_render_for_all_screens(owner_client):
    cid = make_client(owner_client)
    for path in ["/dashboard", "/clients", f"/clients/{cid}", "/invoices", "/invoices/upload", "/duplicates",
                 "/exceptions", "/vendors", "/exports", "/audit", "/users", "/settings"]:
        res = owner_client.get(path)
        assert res.status_code == 200, path
        assert "<h1>" in res.text, path
        assert res.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in res.headers["content-security-policy"]


def test_dashboard_counts_reflect_real_data(owner_client):
    cid = make_client(owner_client)
    upload(owner_client, cid, "a.pdf", invoice_pdf("cgst_sgst_valid"))
    upload(owner_client, cid, "b.pdf", invoice_pdf("incorrect_total"))
    page = owner_client.get("/dashboard").text
    assert "Total invoices" in page
    assert "<strong>2</strong>" in page


# ---------------- organisation isolation
def test_second_organisation_cannot_access_first_organisations_data(app, settings, client, db, owner_client):
    cid = make_client(owner_client, name="Firm A Client")
    inv_id = upload(owner_client, cid, "secret.pdf", invoice_pdf("cgst_sgst_valid")).json()["invoice_id"]
    org_b = add_org(db, "Second Firm")
    add_user(db, settings, org_b, "owner-b@test.example", "owner")
    other = type(client)(app)
    assert login(other, "owner-b@test.example", OWNER_PASSWORD).status_code == 303

    assert other.get(f"/invoices/{inv_id}").status_code == 404
    assert other.get(f"/invoices/{inv_id}/file").status_code == 404
    assert other.get(f"/clients/{cid}").status_code == 404
    assert "Firm A Client" not in other.get("/clients").text
    assert "secret.pdf" not in other.get("/invoices").text
    assert "Synthetic Traders" not in other.get("/vendors").text

    res = other.post(f"/invoices/{inv_id}/corrections", form=csrf_form(other, buyer_name="Hijacked"))
    assert res.status_code == 404 or "not found" in (res.location or "")
    res = other.post(f"/invoices/{inv_id}/decision", form=csrf_form(other, decision="approve"))
    assert "not found" in res.location.lower()
    assert invoice_row(db, inv_id)["status"] == "ready_for_approval"
    export = other.post("/exports/csv", form=csrf_form(other, kind="invoices"))
    assert "No approved" in export.location

    cross_upload = upload(other, cid, "x.pdf", invoice_pdf("igst_valid"))
    assert cross_upload.status_code == 404  # cannot attach invoices to another firm's client


def test_second_organisation_is_not_shown_first_organisations_audit(app, settings, client, db, owner_client):
    make_client(owner_client, name="Audit Scope Client")
    org_b = add_org(db, "Audit Firm B")
    add_user(db, settings, org_b, "owner-c@test.example", "owner")
    other = type(client)(app)
    login(other, "owner-c@test.example", OWNER_PASSWORD)
    assert "Audit Scope Client" not in other.get("/audit").text
    assert OWNER_EMAIL not in other.get("/users").text


def test_login_form_token_from_rendered_html_is_accepted(app, client):
    """Regression: the CSRF value must come from the rendered form, as a browser sends it."""
    import re
    page = client.get("/login")
    match = re.search(r'name="csrf" value="([^"]+)"', page.text)
    assert match, "login form must contain a csrf field"
    assert match.group(1) == client.csrf() and len(match.group(1)) >= 32
    res = client.post("/login", form={"email": OWNER_EMAIL, "password": OWNER_PASSWORD, "csrf": match.group(1)})
    assert res.status_code == 303, res.text


def test_rendered_forms_include_csrf_values(owner_client):
    import re
    page = owner_client.get("/clients")
    assert re.search(r'name="csrf" value="[^"]{32,}"', page.text)
