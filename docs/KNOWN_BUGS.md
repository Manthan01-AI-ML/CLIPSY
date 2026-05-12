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

### BUG-003 — Forgot password: no backend endpoint

**Symptom:** The auth UI has a "Forgot password?" link/flow but there is no backend endpoint to handle it. Clicking through either 404s or silently fails.

**Affected area:** `backend/api/routes/auth.py` — missing `/auth/forgot-password` and `/auth/reset-password` routes  
**Severity:** Medium — blocks self-service password recovery; users have no way to regain access if they forget password  
**Status:** Open  
**Notes:** Need to implement: (1) `POST /auth/forgot-password` — accepts email, sends reset link via email; (2) `POST /auth/reset-password` — accepts token + new password. Requires email service wired up (`backend/services/notifications.py` scaffolded but not live).

---

## Closed

<!-- Move fixed bugs here with fix date and commit/session reference -->

| ID | Title | Fixed | Session/Commit |
|---|---|---|---|
| — | — | — | — |
