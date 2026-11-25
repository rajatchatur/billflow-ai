# BillFlow AI (MVP)

Invoice processing and bookkeeping support for Indian CA firms and accounting agencies.
Upload invoices (PDF, JPG, JPEG, PNG), get rule-based field extraction, GST format and arithmetic
checks, duplicate detection, accountant review and correction, approval, and CSV export, with an audit trail.

> **Status: prototype.** It is not production-secure. See `docs/SECURITY.md` for what is and is not in place.
> Use synthetic invoices only until an independent security review has been completed.

## What it does

1. Sign in (organisation-scoped accounts with roles: owner, administrator, accountant, reviewer).
2. Create SME client companies (GSTIN format and checksum checked on entry).
3. Upload one or many invoices (drag and drop or browse). File type is checked from the content.
4. The app reads PDF text (pypdf). Images need Tesseract OCR; without it they are stored and flagged for manual entry.
5. Fields are extracted by **deterministic rules**, not an AI model. Missing fields stay empty.
6. Validation runs: GSTIN format and checksum, invoice number and date, taxable and total arithmetic,
   CGST/SGST and IGST consistency, rounding tolerance, line-item arithmetic.
7. Duplicates are flagged (same file hash, or same client, supplier GSTIN, invoice number, date and total).
8. An accountant corrects values. Original extraction is kept; corrections are stored separately and audited.
9. A reviewer approves or rejects. Approval is blocked by validation errors and exact duplicates.
10. Export approved invoices as CSV (invoice level and line level). Exported invoices are marked as such.
11. Audit history shows every upload, extraction, validation, correction, decision, download and export.

Not included: Tally XML export, WhatsApp or email ingestion, PDF scan OCR, AI extraction, GSTN verification,
e-invoice (IRN) validation, accounting posting, password reset by email, MFA, line-item editing.

## Requirements

- Python 3.11 is the target (Apple Silicon Mac, 8 GB RAM). See "Verified environments" below.
- Python packages: `requirements.txt` (Starlette, Pydantic, Jinja2, pypdf, Pillow, pytesseract, uvicorn, pytest).
- Optional: Tesseract OCR executable (`brew install tesseract` on macOS) for image uploads.
- No Docker, Node.js, Redis, PostgreSQL, or paid API key.

## Setup on macOS (conda)

```bash
conda activate aiagents311
cd billflow-ai
pip install -r requirements.txt
cp .env.example .env
# edit .env: set BILLFLOW_SESSION_SECRET (scripts/make_secret.sh prints one)
# and BILLFLOW_ADMIN_EMAIL / BILLFLOW_ADMIN_PASSWORD (12+ characters)
```

## Run

```bash
conda activate aiagents311
./scripts/start.sh          # reads .env, starts uvicorn on http://127.0.0.1:8000
```

Equivalent direct command:

```bash
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000 and sign in with the owner account from `.env`.
The owner account is created on first startup only if no user exists yet.

Optional demo data (clearly labelled SYNTHETIC):

```bash
python scripts/seed_demo.py     # creates 2 synthetic clients and 6 synthetic invoices
```

## Test

```bash
./scripts/test.sh               # runs pytest over tests/
```

The suite covers authentication and lockout, CSRF, role permissions, organisation isolation, client isolation,
file validation, PDF and OCR extraction, invoice validation, GSTIN checks, tax arithmetic, duplicates,
corrections, approvals, CSV exports, audit records, and invalid or corrupt uploads.

## Synthetic invoices

`scripts/synthetic_invoices.py` generates the PDFs in `demo_data/`, with GSTINs that have valid checksums but
belong to no real taxpayer. Cases: CGST/SGST, IGST, multiple lines, discount with rounding, missing buyer GSTIN,
incorrect total, invalid GSTIN checksum, and a PDF with no text (simulated unreadable scan).

## Project layout

```
app/
  main.py            Starlette routes and page handlers
  auth.py            Sessions, CSRF, role permissions (checked on the server every request)
  security.py        PBKDF2-SHA256 password hashing, HMAC-signed tokens
  database.py        SQLite schema (sqlite3, no ORM)
  extraction.py      Rule-based extraction and OCR path, Pydantic invoice schema
  validation.py      GSTIN, arithmetic, date and line-item checks
  duplicate_detection.py
  exports.py         CSV builders (formula-injection guard)
  audit.py           Audit event writer
  bootstrap.py       First-run owner creation from environment variables
  services/          Upload validation and file storage; invoice workflow; AI-provider interface (no provider)
  templates/         Jinja2 pages        static/   CSS and small upload script
tests/               pytest suite (ASGI test client, no network)
scripts/             start, test, seed, synthetic invoice generator
docs/                SECURITY.md, API.md
demo_data/           synthetic invoice PDFs
```

## Configuration

All configuration is read from environment variables. See `.env.example`. No secret is stored in the repository.

## Verified environments

- The build and test runs were executed in a Linux container with Python 3.13 (the packages that were
  installed there). The code avoids 3.12+ syntax, but a run on Python 3.11 on macOS has not been performed yet.
  Run `./scripts/test.sh` on your Mac before relying on it.
- Tesseract 5.3.4 was present in the build container, so the OCR test ran. Your Mac may use a different version.

## Documentation

- `docs/SECURITY.md`: implemented controls, production limitations, retention notes.
- `docs/API.md`: HTTP routes and their permissions.
- `PROJECT_STATUS.md`: completed work, remaining tasks, latest test results and known limitations.
