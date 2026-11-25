"""Authentication, sessions, CSRF and server-side role checks.

Session cookie: HMAC-signed, HttpOnly, SameSite=Strict, with expiry. The user's
role and active flag are re-read from the database on every request, so a
revoked or downgraded user loses access immediately.
CSRF: a random token cookie (HttpOnly) must match a form field or X-CSRF-Token header.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import Response

from app.security import new_csrf_token, safe_compare, sign_token, unsign_token

SESSION_COOKIE = "bf_session"
CSRF_COOKIE = "bf_csrf"

ROLES = ("owner", "administrator", "accountant", "reviewer")
PERMISSIONS: dict[str, frozenset[str]] = {
    "manage_clients": frozenset({"owner", "administrator"}),
    "manage_users": frozenset({"owner", "administrator"}),
    "settings": frozenset({"owner", "administrator"}),
    "upload": frozenset({"owner", "administrator", "accountant"}),
    "correct": frozenset({"owner", "administrator", "accountant"}),
    "decide": frozenset({"owner", "administrator", "reviewer"}),
    "export": frozenset({"owner", "administrator", "accountant", "reviewer"}),
    "view": frozenset(ROLES),
}


class Forbidden(Exception):
    pass


@dataclass(frozen=True)
class CurrentUser:
    id: int
    organisation_id: int
    email: str
    full_name: str
    role: str

    def can(self, permission: str) -> bool:
        return self.role in PERMISSIONS.get(permission, frozenset())

    def require(self, permission: str) -> None:
        if not self.can(permission):
            raise Forbidden(f"Your role ({self.role}) is not allowed to perform this action.")


def issue_session(response: Response, user_id: int, org_id: int, secret: str,
                  max_age: int, secure: bool) -> None:
    token = sign_token({"uid": user_id, "org": org_id, "exp": time.time() + max_age,
                        "nonce": secrets.token_hex(8)}, secret)
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True,
                        samesite="strict", secure=secure, path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", samesite="strict")


def current_user(request: Request, conn: sqlite3.Connection, secret: str) -> CurrentUser | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    payload = unsign_token(token, secret)
    if not payload:
        return None
    row = conn.execute(
        "SELECT u.id, u.organisation_id, u.email, u.full_name, u.role, u.is_active, o.id AS org_ok "
        "FROM users u JOIN organisations o ON o.id = u.organisation_id WHERE u.id = ?",
        (payload.get("uid"),)).fetchone()
    if not row or not row["is_active"] or row["organisation_id"] != payload.get("org"):
        return None
    return CurrentUser(row["id"], row["organisation_id"], row["email"], row["full_name"], row["role"])


def csrf_token(request: Request) -> str:
    existing = request.cookies.get(CSRF_COOKIE)
    return existing if existing and len(existing) >= 32 else new_csrf_token()


def ensure_csrf_cookie(request: Request, response: Response) -> str:
    token = csrf_token(request)
    if request.cookies.get(CSRF_COOKIE) != token:
        response.set_cookie(CSRF_COOKIE, token, httponly=True, samesite="strict", path="/")
    return token


def verify_csrf(request: Request, form_value: str | None) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    supplied = form_value or request.headers.get("x-csrf-token", "")
    return bool(cookie) and bool(supplied) and safe_compare(cookie, supplied)
