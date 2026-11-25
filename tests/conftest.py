import os
import sqlite3
import sys
from pathlib import Path

import pytest

# Prevent importing app.main from creating a default database during tests.
os.environ["BILLFLOW_DISABLE_AUTOAPP"] = "1"

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.config import Settings  # noqa: E402
from app.database import connect  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security import hash_password  # noqa: E402
from tests.asgi_client import ASGIClient  # noqa: E402

OWNER_EMAIL = "owner@test.example"
OWNER_PASSWORD = "correct-horse-battery-1"
TEST_SECRET = "test-secret-" + "x" * 40


@pytest.fixture()
def settings(tmp_path):
    return Settings(
        database_path=tmp_path / "db" / "test.sqlite3",
        upload_dir=tmp_path / "uploads",
        session_secret=TEST_SECRET,
        admin_email=OWNER_EMAIL,
        admin_password=OWNER_PASSWORD,
        admin_org_name="Test CA Firm",
        pbkdf2_iterations=1000,  # fast for tests only; production default is 390000
        max_upload_bytes=2 * 1024 * 1024,
        tesseract_cmd="/nonexistent/tesseract-for-tests",
    )


@pytest.fixture()
def app(settings):
    return create_app(settings)


@pytest.fixture()
def client(app):
    return ASGIClient(app)


@pytest.fixture()
def db(settings, app):
    conn = connect(settings.database_path)
    yield conn
    conn.close()


def login(client: ASGIClient, email: str, password: str):
    client.get("/login")  # sets the CSRF cookie
    return client.post("/login", form={"email": email, "password": password, "csrf": client.csrf()})


def add_user(db: sqlite3.Connection, settings: Settings, org_id: int, email: str, role: str,
             password: str = OWNER_PASSWORD) -> int:
    db.execute("INSERT INTO users (organisation_id, email, full_name, password_hash, role) VALUES (?,?,?,?,?)",
               (org_id, email, email.split("@")[0], hash_password(password, settings.pbkdf2_iterations), role))
    return db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()["id"]


def add_org(db: sqlite3.Connection, name: str) -> int:
    db.execute("INSERT INTO organisations (name) VALUES (?)", (name,))
    return db.execute("SELECT id FROM organisations WHERE name = ?", (name,)).fetchone()["id"]


@pytest.fixture()
def owner_client(client):
    res = login(client, OWNER_EMAIL, OWNER_PASSWORD)
    assert res.status_code == 303, res.text
    return client
