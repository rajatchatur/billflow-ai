"""Audit trail. Records metadata and field names only, never invoice contents or secrets."""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def record(conn: sqlite3.Connection, organisation_id: int, user_id: int | None, action: str,
           entity_type: str | None = None, entity_id: int | None = None,
           details: dict[str, Any] | None = None) -> None:
    conn.execute(
        "INSERT INTO audit_events (organisation_id, user_id, action, entity_type, entity_id, details_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (organisation_id, user_id, action, entity_type, entity_id,
         json.dumps(details, default=str, sort_keys=True) if details else None),
    )
