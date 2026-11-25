# Security and privacy

BillFlow AI handles commercially and personally sensitive invoice data. This document separates what is
implemented from what production deployment still needs. **This is a prototype. It has not had an independent
security review or penetration test. Do not use real client data with it until that review is done.**

## Implemented controls

**Authentication and sessions**
- Passwords are hashed with PBKDF2-HMAC-SHA256 (per-password random 16-byte salt, 390,000 iterations by default).
  Plaintext passwords are never stored or logged.
- Failed-login responses use one generic message. A dummy hash is computed for unknown emails, but its cost is lower, so timing differences are reduced rather than removed.
- Five failed attempts for one email lock it for five minutes (in-memory, per process).
- Session cookie: HMAC-SHA256 signed, HttpOnly, SameSite=Strict, expiry embedded and checked. `Secure` flag is
  configurable (`BILLFLOW_COOKIE_SECURE`) and should be `true` behind HTTPS.
- On each request the user's account, active flag and role are re-read from the database. Deactivated users lose access immediately.
- If `BILLFLOW_SESSION_SECRET` is not set, a random secret is generated, so sessions end on every restart.

**CSRF**
- Every state-changing form and endpoint requires a token from a HttpOnly, SameSite=Strict cookie, compared in constant time.

**Authorisation and tenant isolation**
- Roles (owner, administrator, accountant, reviewer) are enforced server-side in each handler.
- Every tenant table has `organisation_id`. Every query filters by the signed-in user's organisation.
  Records from another organisation are treated as not found (a 404 page or a "not found" message), so existence is not revealed.
- Tests cover cross-organisation reads, writes, file downloads, exports, client attachment and audit visibility.

**Files**
- Type check uses magic bytes (`%PDF-`, PNG and JPEG signatures) and must match the extension. Corrupt images are rejected by Pillow.
- Size limit configurable (`BILLFLOW_MAX_UPLOAD_BYTES`, default 10 MB), checked on the header and while streaming.
- Stored under random server-generated names (32 hex characters) in a private directory, mode 0600.
  Original bytes are kept unmodified and hashed (SHA-256).
- Path traversal is blocked: stored names must match a strict pattern and resolve inside the upload directory.
- Downloads require an authenticated session in the same organisation, send `Cache-Control: no-store` and
  `X-Content-Type-Options: nosniff`, and are audited.
- Original filenames are URL-decoded, reduced to a safe character set, and length-limited. They are never used as storage paths.

**Output and browser hardening**
- Jinja2 autoescaping for all HTML output.
- Content-Security-Policy restricts scripts, styles and frames to `'self'`. `X-Frame-Options: DENY`.
- CSV exports neutralise spreadsheet formula injection (cells beginning with `=`, `+`, `-`, `@` are prefixed with an apostrophe).

**Auditability and logging**
- The `audit_events` table records uploads, extraction, validation, corrections (field-level previous and new values),
  decisions, downloads, exports, logins, failed logins, and permission changes.
- Application logs do not contain invoice text, passwords or tokens. The audit table stores metadata for extraction events
  (status, method, counts), not extracted values. Correction events store previous and new values by design, because the
  audit requirement asks for them; this data sits in the database, not in logs.

**Input and data integrity**
- Pydantic schema for extracted data (unknown fields rejected). Money is handled with `Decimal`.
- Original extracted values are preserved; corrections are stored separately.
- Approval is blocked while validation errors or exact duplicates exist, and approved or exported invoices cannot be edited.

**Secrets**
- Configuration comes only from environment variables. `.env` is git-ignored. `.env.example` contains placeholders only.
- No AI provider is configured, so no invoice text leaves the machine.

## Known limitations (not production-ready)

- **No independent security testing.** Automated tests cover the controls above but do not replace a penetration test.
- **Single-process login lockout.** The lockout state lives in memory and resets on restart. Multiple workers do not share it.
- **No rate limiting** beyond login. No CAPTCHA, MFA, or account recovery (forgot-password is not implemented).
- **No CSRF token rotation per session.** The token is tied to a cookie that lives as long as the browser keeps it.
- **Sessions cannot be revoked individually.** Deactivating a user takes effect immediately; logout clears only the current browser.
- **No TLS in the app.** Serve behind HTTPS (for example a reverse proxy) and set `BILLFLOW_COOKIE_SECURE=true`.
- **Encryption at rest is not implemented.** Invoices and the SQLite database are plaintext on disk. Use full-disk encryption (FileVault on macOS) at minimum.
- **No retention or deletion policy is enforced.** The `deleted_at` columns exist but no deletion workflow is built.
- **SQLite** is for local use. It is single-writer and has no row-level security. PostgreSQL with RLS is a production step.
- **Audit log is not tamper-evident.** Rows can be altered by anyone with database access.
- **Uploads are processed synchronously** in the request. Large batches can time out; there is no background queue.
- **Extraction is rule-based** and tuned to text layouts like the synthetic invoices. It will miss or mis-read fields on
  real invoices, which is why every extraction is reviewed by a person before approval.
- **No GSTN verification.** GSTIN checks are format and checksum only. They do not confirm that a GSTIN is registered or active.
- **Tesseract OCR output is less reliable.** It is always flagged for review.
- **Content-Security-Policy** allows inline styles from `'self'` only; no third-party scripts are loaded.

## Data handling guidance

- Use synthetic invoices (`demo_data/`) for demonstrations and testing.
- Keep `uploads/` and `data/` on an encrypted disk and out of backups you do not control.
- Do not paste real invoices into bug reports, logs or chat tools.
- Before real use: independent security review, HTTPS deployment, encrypted storage, a tested backup and restore
  procedure, a retention policy agreed with the firm, and a review of your data-protection obligations under Indian law.
  This document is not legal advice.
