"""Minimal ASGI test client. Used because httpx (required by Starlette's TestClient) is not installed here."""
from __future__ import annotations

import json
from urllib.parse import urlencode, urlsplit


class Response:
    def __init__(self, status: int, headers: list[tuple[bytes, bytes]], body: bytes):
        self.status_code = status
        self.headers = {k.decode().lower(): v.decode() for k, v in headers}
        self._all = headers
        self.content = body
        self.text = body.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)

    @property
    def location(self) -> str | None:
        """Decoded redirect target, so tests can match human-readable messages."""
        from urllib.parse import unquote
        value = self.headers.get("location")
        return unquote(value) if value is not None else None

    def set_cookies(self) -> dict[str, str]:
        out = {}
        for k, v in self._all:
            if k.lower() == b"set-cookie":
                first = v.decode().split(";")[0]
                name, _, value = first.partition("=")
                out[name.strip()] = value
        return out


class ASGIClient:
    def __init__(self, app):
        self.app = app
        self.cookies: dict[str, str] = {}

    def request(self, method: str, url: str, *, form: dict | None = None, body: bytes | None = None,
                headers: dict[str, str] | None = None) -> Response:
        parts = urlsplit(url)
        payload = b""
        hdrs: list[tuple[bytes, bytes]] = []
        if form is not None:
            payload = urlencode(form).encode()
            hdrs.append((b"content-type", b"application/x-www-form-urlencoded"))
        if body is not None:
            payload = body
        if self.cookies:
            hdrs.append((b"cookie", "; ".join(f"{k}={v}" for k, v in self.cookies.items()).encode()))
        for k, v in (headers or {}).items():
            hdrs.append((k.lower().encode(), v.encode("latin-1")))
        hdrs.append((b"content-length", str(len(payload)).encode()))
        hdrs.append((b"host", b"testserver"))
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": method,
                 "scheme": "http", "path": parts.path, "raw_path": parts.path.encode(),
                 "query_string": parts.query.encode(), "root_path": "", "headers": hdrs,
                 "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}
        sent = {"done": False}
        messages: list[dict] = []

        async def receive():
            if not sent["done"]:
                sent["done"] = True
                return {"type": "http.request", "body": payload, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)

        import asyncio
        asyncio.run(self.app(scope, receive, send))
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        resp_headers = next(m["headers"] for m in messages if m["type"] == "http.response.start")
        body_out = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        response = Response(status, resp_headers, body_out)
        for name, value in response.set_cookies().items():
            if value in ("", '""') or "expires" in value.lower():
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = value
        return response

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> Response:
        return self.request("POST", url, **kw)

    def csrf(self) -> str:
        return self.cookies.get("bf_csrf", "")
