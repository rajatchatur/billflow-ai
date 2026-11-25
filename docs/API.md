# HTTP routes

All state-changing requests are `POST` and require a CSRF token: the `bf_csrf` cookie value must match
the `csrf` form field or the `X-CSRF-Token` header. Pages return HTML; the upload and export endpoints are noted below.
"Any" means any signed-in user of the same organisation. Unauthenticated requests are redirected to `/login`.

| Method | Path | Permission | Purpose |
|---|---|---|---|
| GET | `/healthz` | none | Liveness check (`ok`) |
| GET/POST | `/login` | none | Sign in (5 failed attempts per email lock it for 5 minutes) |
| POST | `/logout` | any | Sign out and clear the session cookie |
| GET | `/dashboard` | any | Live counts for this organisation |
| GET | `/clients` | any | Client list |
| POST | `/clients` | owner, administrator | Create a client (name, optional GSTIN, PAN, state, period) |
| GET | `/clients/{id}` | any | Client detail with its invoices and vendors |
| GET | `/invoices` | any | Inbox, optional `?status=` filter |
| GET | `/invoices/upload` | any | Upload page |
| POST | `/invoices/upload?client_id={id}` | owner, administrator, accountant | Raw-body upload. Headers: `X-Filename` (URL-encoded), `X-CSRF-Token`. Returns JSON 201 with `invoice_id`, `status`, `extraction_status`, or 400/403/404/413 with `error` |
| GET | `/invoices/{id}` | any | Review screen |
| POST | `/invoices/{id}/corrections` | owner, administrator, accountant | Save header corrections; re-runs validation and duplicate checks |
| POST | `/invoices/{id}/decision` | owner, administrator, reviewer | `decision` = `approve`, `reject`, `mark_review`, `reopen` (reopen also needs the correct permission); optional `note` |
| POST | `/invoices/{id}/retry` | owner, administrator, accountant | Re-run extraction on the stored original |
| GET | `/invoices/{id}/file` | any | Stored original file (inline, `no-store`, audited) |
| GET | `/duplicates` | any | Duplicate candidates |
| GET | `/exceptions` | any | Invoices needing review or failed |
| GET | `/vendors` | any | Suppliers seen on invoices |
| GET | `/exports` | any | Export centre |
| POST | `/exports/csv` | owner, administrator, accountant, reviewer | `kind` = `invoices` (marks approved invoices as exported) or `line_items` (does not change status). Approved invoices only |
| GET | `/audit` | any | Audit history for this organisation |
| GET | `/users` | any | Team list |
| POST | `/users` | owner, administrator | Create a user (password 12+ characters). Only owners can create owners |
| POST | `/users/{id}` | owner, administrator | `action` = `role` (with `role`) or `deactivate` |
| GET | `/settings` | any | Read-only configuration view |
| POST | `/settings` | owner, administrator | Rename the organisation |

Permissions are enforced in `app/auth.py` (`PERMISSIONS`) and checked on every request against the role stored in the database.
