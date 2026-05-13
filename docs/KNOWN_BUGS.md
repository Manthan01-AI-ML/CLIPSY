# Known Bugs

---

## Open

### BUG-001 — Reframe preview shows black screen

**Symptom:** When the user sets a crop/reframe point in the UI, the preview panel renders black instead of showing the cropped frame.

**Affected area:** Frontend reframe UI → clip preview  
**Severity:** Medium — workflow is broken for reframe; user can't verify crop before rendering  
**Status:** Open  
**Notes:** Rendering itself (the actual exported clip) may work correctly — bug is in the preview path only. Not yet confirmed whether the issue is in canvas drawing, the preview API endpoint, or a CORS/path problem serving the frame thumbnail.

---

### BUG-002 — Export quality lower than expected

**Symptom:** Exported clips look noticeably worse than the source video — visible compression artifacts, blurry captions, or low bitrate.

**Affected area:** `backend/pipeline/render.py` — FFmpeg encoding settings  
**Severity:** High — directly impacts the product's core output quality  
**Status:** Open  
**Notes:** Current render uses `ultrafast` preset for speed. May need to bump to `fast` or `medium` for production exports, or add a quality flag. Also check CRF value and caption font rendering at 9:16 resolution.

---

---

## Closed

| ID | Title | Fixed | Session/Commit |
|---|---|---|---|
| BUG-003 | Forgot password: no backend endpoint | 2026-05-12 | Session 1 |

### BUG-003 — Forgot password: no backend endpoint ✓ FIXED

**Fix (Session 1, 2026-05-12):** Implemented full end-to-end forgot/reset flow. `POST /auth/forgot-password` (3/hour, always 200, email enumeration safe) + `POST /auth/reset-password` (5/hour). `PasswordResetToken` model added with SHA-256 hashed tokens, 15-min expiry, single-use. `send_password_reset_email()` via Resend SDK in `backend/services/notifications.py` with dev console fallback. Frontend `#forgot-modal` now has real 2-step flow; `#view-reset-password` SPA view added. `GET /reset-password` FastAPI passthrough added to `main.py`.

---

## Low priority / Cosmetic

### BUG-004 — `passlib[bcrypt]` listed in requirements but unused

**Symptom:** `backend/requirements.txt` still lists `passlib[bcrypt]` but the codebase uses `bcrypt` directly via `hash_password()` / `verify_password()` in `security.py`. The dependency is redundant dead weight.

**Affected area:** `backend/requirements.txt`  
**Severity:** Low — no functional impact; just installs an extra package  
**Status:** Open  
**Notes:** Safe to remove `passlib[bcrypt]` in a cleanup pass. Confirm no `passlib` imports exist first (`grep -r "passlib" backend/`).
