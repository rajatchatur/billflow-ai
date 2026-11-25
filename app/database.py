"""SQLite persistence using the standard-library sqlite3 module.

Every tenant-owned table carries organisation_id. Repository helpers in this
module always take an organisation_id so that cross-tenant reads are impossible
by construction at the query level.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS organisations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('owner','administrator','accountant','reviewer')),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_users_org ON users(organisation_id);

CREATE TABLE IF NOT EXISTS client_companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    gstin TEXT,
    pan TEXT,
    address TEXT,
    state TEXT,
    state_code TEXT,
    accounting_period TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (organisation_id, name)
);
CREATE INDEX IF NOT EXISTS ix_clients_org ON client_companies(organisation_id);

CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    client_company_id INTEGER NOT NULL REFERENCES client_companies(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    gstin TEXT,
    address TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (organisation_id, client_company_id, name)
);
CREATE INDEX IF NOT EXISTS ix_vendors_org ON vendors(organisation_id);

CREATE TABLE IF NOT EXISTS source_documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    client_company_id INTEGER NOT NULL REFERENCES client_companies(id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    stored_filename TEXT NOT NULL UNIQUE,
    detected_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    uploaded_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_docs_org_hash ON source_documents(organisation_id, sha256);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    client_company_id INTEGER NOT NULL REFERENCES client_companies(id) ON DELETE CASCADE,
    source_document_id INTEGER NOT NULL UNIQUE REFERENCES source_documents(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK (status IN ('processing','needs_review','ready_for_approval','approved','rejected','exported','failed')),
    extraction_method TEXT,
    extraction_status TEXT,
    extraction_confidence REAL,
    extracted_json TEXT,
    corrected_json TEXT,
    supplier_name TEXT,
    supplier_gstin TEXT,
    invoice_number TEXT,
    normalised_invoice_number TEXT,
    invoice_date TEXT,
    total_amount REAL,
    validation_json TEXT,
    duplicate_of_id INTEGER REFERENCES invoices(id) ON DELETE SET NULL,
    failure_reason TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_invoices_org_status ON invoices(organisation_id, status);
CREATE INDEX IF NOT EXISTS ix_invoices_dupe ON invoices(organisation_id, client_company_id, supplier_gstin, normalised_invoice_number, invoice_date);

CREATE TABLE IF NOT EXISTS invoice_line_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    description TEXT,
    hsn_sac TEXT,
    quantity REAL,
    unit_price REAL,
    gst_rate REAL,
    taxable_value REAL,
    tax_amount REAL,
    line_total REAL,
    UNIQUE (invoice_id, position)
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    invoice_id INTEGER NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
    decision TEXT NOT NULL CHECK (decision IN ('approved','rejected','marked_for_review','reopened')),
    actor_user_id INTEGER NOT NULL REFERENCES users(id),
    note TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_approvals_invoice ON approvals(invoice_id);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    organisation_id INTEGER NOT NULL REFERENCES organisations(id) ON DELETE CASCADE,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id INTEGER,
    details_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_audit_org ON audit_events(organisation_id, created_at);
"""


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(path: Path) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
