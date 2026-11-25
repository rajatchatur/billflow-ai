"""Creates clearly labelled SYNTHETIC demo clients and uploads synthetic invoices through the real workflow.

Run after the owner account exists (set BILLFLOW_ADMIN_EMAIL / BILLFLOW_ADMIN_PASSWORD first).
Every client name starts with 'SYNTHETIC' and every invoice comes from scripts/synthetic_invoices.py.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.auth import CurrentUser  # noqa: E402
from app.config import Settings  # noqa: E402
from app.bootstrap import bootstrap  # noqa: E402
from app.database import connect, init_db  # noqa: E402
from app.services.files import validate_upload  # noqa: E402
from app.services.invoices import process_new_document  # noqa: E402
from synthetic_invoices import CASES, _lines_for, build_pdf  # noqa: E402

CLIENTS = ["SYNTHETIC Client A - Textiles", "SYNTHETIC Client B - Food Distribution"]


def main() -> int:
    settings = Settings.from_env()
    if not settings.session_secret:
        print("Set BILLFLOW_SESSION_SECRET first (see .env.example).")
        return 1
    init_db(settings.database_path)
    conn = connect(settings.database_path)
    bootstrap(conn, settings)
    owner = conn.execute("SELECT id, organisation_id, email, full_name, role FROM users WHERE role = 'owner' "
                         "ORDER BY id LIMIT 1").fetchone()
    if owner is None:
        print("No owner account yet. Start the app once with BILLFLOW_ADMIN_EMAIL and BILLFLOW_ADMIN_PASSWORD set.")
        return 1
    user = CurrentUser(owner["id"], owner["organisation_id"], owner["email"], owner["full_name"], owner["role"])
    client_ids = []
    for name in CLIENTS:
        row = conn.execute("SELECT id FROM client_companies WHERE organisation_id = ? AND name = ?",
                           (user.organisation_id, name)).fetchone()
        if row is None:
            conn.execute("INSERT INTO client_companies (organisation_id, name, state, accounting_period) VALUES (?,?,?,?)",
                         (user.organisation_id, name, "Maharashtra", "FY 2026-27 (synthetic)"))
            row = conn.execute("SELECT id FROM client_companies WHERE organisation_id = ? AND name = ?",
                               (user.organisation_id, name)).fetchone()
        client_ids.append(row["id"])
    created = 0
    for i, (case_name, case) in enumerate(CASES.items()):
        content = build_pdf(_lines_for(case))
        validated = validate_upload(f"synthetic_{case_name}.pdf", content, settings.max_upload_bytes)
        process_new_document(conn, settings, user, client_ids[i % len(client_ids)], validated)
        created += 1
    print(f"Seeded {created} synthetic invoices across {len(client_ids)} synthetic clients.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
