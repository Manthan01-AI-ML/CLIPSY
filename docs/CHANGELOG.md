# Changelog

---

## 2026-05-12 — Session 1: Launch Sprint Day 1 — Foundations

### New features
- **Forgot-password flow** (end-to-end): `POST /auth/forgot-password` + `POST /auth/reset-password` backend routes; `#forgot-modal` updated with live 2-step flow; `#view-reset-password` SPA view with form; `GET /reset-password` passthrough in FastAPI; Resend email SDK wired with dev console fallback.
- **Variable clip count**: range slider (3–20, default 5) in upload panel; `num_clips` field flows through schema → routes → worker; `pick_clips()` call is no longer hardcoded to 5.

### Changed
- `backend/core/security.py`: added `DISPOSABLE_EMAIL_DOMAINS` frozenset (65 domains) and `is_email_allowed()` function. Blocklist moved here from `auth.py`.
- `backend/api/routes/auth.py`: removed inline blocklist; now calls `is_email_allowed()`; added two new rate-limited endpoints.
- `backend/core/config.py`: added `RESEND_API_KEY`, `EMAIL_FROM`, `FRONTEND_URL` settings.
- `backend/requirements.txt`: added `resend==2.9.0`.

### New files
- `backend/models/password_reset.py` — `PasswordResetToken` SQLAlchemy model
- `backend/services/notifications.py` — `send_password_reset_email()` via Resend SDK
- `docs/SESSION_LOG.md` — session log template + Session 1 entry

---

## 2026-05-12 — Session 1: docs scaffold (earlier in session)

- Created `docs/` folder with 6 documentation files:
  - `PROJECT_PLAN.md` — full project overview, stack, pipeline, features
  - `DECISIONS.md` — architecture decision log (template)
  - `CHANGELOG.md` — this file
  - `DEPLOYMENT.md` — deployment runbook (template)
  - `COSTS.md` — API cost tracking (template)
  - `KNOWN_BUGS.md` — known bugs at session start
