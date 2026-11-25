"""Configuration loaded exclusively from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = "BillFlow AI"
    database_path: Path = field(default_factory=lambda: BASE_DIR / "data" / "billflow.sqlite3")
    upload_dir: Path = field(default_factory=lambda: BASE_DIR / "uploads")
    session_secret: str = ""
    session_max_age_seconds: int = 8 * 60 * 60
    cookie_secure: bool = False
    max_upload_bytes: int = 10 * 1024 * 1024
    tesseract_cmd: str = ""
    tolerance_rupees: float = 1.00
    admin_email: str = ""
    admin_password: str = ""
    admin_org_name: str = "Demo CA Firm"
    pbkdf2_iterations: int = 390_000

    @classmethod
    def from_env(cls) -> "Settings":
        db = os.environ.get("BILLFLOW_DATABASE_PATH")
        up = os.environ.get("BILLFLOW_UPLOAD_DIR")
        return cls(
            database_path=Path(db) if db else BASE_DIR / "data" / "billflow.sqlite3",
            upload_dir=Path(up) if up else BASE_DIR / "uploads",
            session_secret=os.environ.get("BILLFLOW_SESSION_SECRET", ""),
            session_max_age_seconds=_env_int("BILLFLOW_SESSION_MAX_AGE", 8 * 60 * 60),
            cookie_secure=_env_bool("BILLFLOW_COOKIE_SECURE", False),
            max_upload_bytes=_env_int("BILLFLOW_MAX_UPLOAD_BYTES", 10 * 1024 * 1024),
            tesseract_cmd=os.environ.get("BILLFLOW_TESSERACT_CMD", ""),
            tolerance_rupees=float(os.environ.get("BILLFLOW_TOLERANCE_RUPEES", "1.00")),
            admin_email=os.environ.get("BILLFLOW_ADMIN_EMAIL", ""),
            admin_password=os.environ.get("BILLFLOW_ADMIN_PASSWORD", ""),
            admin_org_name=os.environ.get("BILLFLOW_ORG_NAME", "Demo CA Firm"),
            pbkdf2_iterations=_env_int("BILLFLOW_PBKDF2_ITERATIONS", 390_000),
        )
