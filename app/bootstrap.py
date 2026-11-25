"""First-run bootstrap: creates the development organisation and owner from environment variables."""
from __future__ import annotations

import logging
import sqlite3

from app import audit
from app.config import Settings
from app.security import hash_password

log = logging.getLogger("billflow.bootstrap")


def bootstrap(conn: sqlite3.Connection, settings: Settings) -> str:
    """Returns a status string. Never logs passwords or invoice content."""
    count = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
    if count:
        return "existing"
    if not settings.admin_email or not settings.admin_password:
        return "skipped: set BILLFLOW_ADMIN_EMAIL and BILLFLOW_ADMIN_PASSWORD to create the first owner"
    if len(settings.admin_password) < 12:
        return "skipped: BILLFLOW_ADMIN_PASSWORD must be at least 12 characters"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("INSERT INTO organisations (name) VALUES (?)", (settings.admin_org_name,))
        org_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        conn.execute(
            "INSERT INTO users (organisation_id, email, full_name, password_hash, role) VALUES (?,?,?,?,?)",
            (org_id, settings.admin_email, "Firm Owner", hash_password(settings.admin_password, settings.pbkdf2_iterations), "owner"))
        user_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        audit.record(conn, org_id, user_id, "bootstrap.owner_created", "user", user_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    log.info("Created development organisation and owner account.")
    return "created"
