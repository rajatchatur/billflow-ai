"""Upload validation and safe storage. Extensions are checked against real file content."""
from __future__ import annotations

import hashlib
import os
import re
import secrets
from dataclasses import dataclass
from pathlib import Path

from app.extraction import detect_kind

ALLOWED_EXTENSIONS = {".pdf": "pdf", ".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg"}


class UploadRejected(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class ValidatedUpload:
    original_filename: str
    kind: str
    size_bytes: int
    sha256: str
    content: bytes


def safe_original_name(name: str) -> str:
    base = os.path.basename(name.replace("\\", "/")) or "upload"
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", base)[:120].strip(" .") or "upload"
    return base


def validate_upload(filename: str | None, content: bytes, max_bytes: int) -> ValidatedUpload:
    if not content:
        raise UploadRejected("The uploaded file is empty.")
    if len(content) > max_bytes:
        raise UploadRejected(f"File exceeds the {max_bytes // (1024 * 1024)} MB limit.", 413)
    name = safe_original_name(filename or "")
    ext = Path(name).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadRejected("Only PDF, JPG, JPEG and PNG files are accepted.")
    kind = detect_kind(content)
    if kind is None:
        raise UploadRejected("File content is not a PDF, PNG or JPEG (signature check failed).")
    if kind != ALLOWED_EXTENSIONS[ext]:
        raise UploadRejected("File extension does not match the actual file content.")
    if kind in ("png", "jpeg"):
        try:
            from PIL import Image
            import io as _io
            with Image.open(_io.BytesIO(content)) as img:
                img.verify()
        except Exception:
            raise UploadRejected("Image file is corrupt or could not be decoded.")
    return ValidatedUpload(name, kind, len(content), hashlib.sha256(content).hexdigest(), content)


def store_original(upload_dir: Path, validated: ValidatedUpload) -> str:
    """Write the untouched bytes to a private directory under a random, server-generated name."""
    upload_dir.mkdir(parents=True, exist_ok=True)
    ext = {"pdf": ".pdf", "png": ".png", "jpeg": ".jpg"}[validated.kind]
    stored = f"{secrets.token_hex(16)}{ext}"
    path = resolve_inside(upload_dir, stored)
    with open(path, "xb") as fh:
        fh.write(validated.content)
    os.chmod(path, 0o600)
    return stored


def resolve_inside(base: Path, name: str) -> Path:
    """Guard against path traversal: the resolved path must stay inside base."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name) or ".." in name:
        raise UploadRejected("Invalid file reference.", 400)
    candidate = (base / name).resolve()
    if base.resolve() not in candidate.parents:
        raise UploadRejected("Invalid file reference.", 400)
    return candidate
