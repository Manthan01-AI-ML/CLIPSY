# Known Bugs

---

## Open

### BUG-015 — CSP blocks Google Fonts on reset-password page

**Symptom:** Browser console on `http://localhost:8000/reset-password?token=X` logs: `"Loading the stylesheet 'https://fonts.googleapis.com/css2?...' violates the following Content Security Policy directive: 'style-src 'self' 'unsafe-inline''"`.

**Affected area:** `backend/core/security.py` — CSP `style-src` / `font-src` directives
**Severity:** Low — cosmetic only. External fonts (Instrument Serif, Geist, Geist Mono) don't load; browser falls back to system fonts. No functional break.
**Status:** Open (Phase 2D.2 scope)
**Fix path:** Add `https://fonts.googleapis.com https://fonts.gstatic.com` to the `style-src` and `font-src` directives in the CSP middleware.

---

### BUG-016 — Reset-password inputs not wrapped in `<form>`

**Symptom:** Browser console warns: `"Password field is not contained in a form"` (twice — for `#reset-new-password` + `#reset-confirm-password`).

**Affected area:** `frontend/templates/index.html` — `#reset-pw-form-wrap` section
**Severity:** Low — usability nuisance. Password managers (1Password, Chrome, Safari) may not offer to save the new password. Form does not submit on Enter key press.
**Status:** Open (Phase 2D.2 scope)
**Fix path:** Wrap the reset-password inputs in `<form id="reset-pw-form" onsubmit="return false;">` with `autocomplete="new-password"` attributes. Wire Enter key → submit button click.

---

### BUG-008 — High track fragmentation in `track_faces_across_frames()` on cut-heavy content ✓ RESOLVED

**Symptom:** `01_single_speaker.mp4` (TEDx talk): 963 frames sampled at 2fps → 346 unique tracks. Expected ≤5 for a single-speaker video. `03_panel_4person.mp4`: 1401 frames → 724 tracks. Track IDs fragment on every camera cut or pan because IoU drops to 0 when the face jumps to a new screen position.

**Affected area:** `backend/pipeline/active_speaker.py` — `track_faces_across_frames()` IoU matching
**Severity:** Medium — active speaker timeline still works (falls back to `only_face`/`largest_face`/`lip_movement_dominant` correctly), but long-term track identity for "who spoke in total for how many seconds" is broken. Each cut creates a new track_id.
**Status:** RESOLVED in Phase 2C (Session 7, 2026-05-18). Spatial-region fallback added to `active_speaker.track_faces_across_frames`. IoU matching primary; 4×4 grid + 3s region memory fallback for camera cuts.
**Fix:** Added `_spatial_region_id()` helper (maps bbox center to `(col, row)` in 4×4 grid), `region_track_history: dict[tuple[int,int], tuple[int,float]]` in the tracking loop. When IoU < 0.5, looks up the face's grid cell in `region_track_history` — if the region was occupied within 3.0s, reuses the prior track_id instead of allocating a new one.

---

### BUG-013 — Docker image missing `webrtcvad` ✓ RESOLVED

**Symptom:** Worker container's Python env doesn't have `webrtcvad` installed, even though `requirements.txt` lists it as `webrtcvad==2.0.10`. Phase 2C E2E test required `pip install webrtcvad==2.0.10` in-place for the running container. Without it, `analyze_audio_activity` raises `ImportError` and the Phase 2C adaptive keyframe block catches it, logs a warning, and falls back to static smart_crop — adaptive keyframes silently disabled.

**Affected area:** `backend/pipeline/audio_activity.py` — `import webrtcvad` at call time
**Severity:** Medium — graceful degradation works, but adaptive keyframes are disabled until fix. No user-visible error; only log evidence.
**Status:** RESOLVED 2026-05-20 — `docker compose build --no-cache backend worker` run in Phase 2D.2 step 1. `webrtcvad 2.0.10`, `resend 2.9.0`, `mediapipe 0.10.35` all verified in fresh container. Day 11 deploy unblocked.

---

### BUG-017 — Orphaned clip files on disk after legacy DELETE

**Symptom:** 37 legacy clips deleted from DB in Phase 2D.2 step 1. Their rendered MP4 files on `/storage/.../clips/` were not removed — disk space not freed, files unreachable via any API endpoint.

**Affected area:** `/storage` volume — clip subdirectories for deleted job IDs
**Severity:** Low — no functional impact, disk waste only (~0.5–1 GB estimated)
**Status:** Open (post-beta cleanup scope)
**Fix path:** Walk `/storage` tree, collect all clip file paths, cross-reference against `clips.file_path` in DB, `rm` orphans. ~30 min script.

---

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
| BUG-013 | Docker image missing `webrtcvad` (C extension not compiled into image) | 2026-05-20 | Session 9 |
| BUG-C1 | `/auto-keyframes` re-runs full pipeline on every call (~10 min); no cache check | 2026-05-20 | Session 9 |
| BUG-009 | `IndexError` in `compute_active_speaker_timeline` for seconds with frames but no faces | 2026-05-16 | Session 4 |
| BUG-003 | Forgot password: no backend endpoint | 2026-05-12 | Session 1 |
| BUG-005 | MediaPipe requires `libgles2` + `libegl1` in Docker | 2026-05-15 | Session 2 |
| BUG-006 | Full-range detector was an alias of short-range (same model) | 2026-05-16 | Session 3 |
| BUG-007 | Panel/group faces missed — wrong model + aggressive NMS | 2026-05-16 | Session 3 |

### BUG-009 — IndexError in `compute_active_speaker_timeline` for seconds with frames but no faces ✓ FIXED

**Symptom:** When a video second had sampled frames in `face_tracking` (so `second_frames` was non-empty) but all those frames returned zero detections, `track_ids_this_second` ended up empty. The code only guarded against `len(track_ids) == 1`, not `len == 0`, so it fell through to `sorted(lip_scores.items())` on an empty dict, then crashed on `sorted_tracks[0]`.

**Affected area:** `backend/pipeline/active_speaker.py` — `compute_active_speaker_timeline()`
**Severity:** High — caused 4/5 test videos to crash at Step 3. Video 2 happened to have no such seconds and passed.
**Status:** Fixed (Phase 2B.1, Session 4, 2026-05-16)
**Fix:** Added guard `if len(track_ids) == 0: timeline.append({..., "reasoning": "no_faces"}); continue` before the existing `len == 1` guard.

---

### BUG-006 — Full-range detector was a fake alias of short-range ✓ FIXED

**Symptom:** `detect_faces_mediapipe_full_range()` claimed to use a different model suited for group/panel content, but `_get_full_range_detector()` literally returned `_get_short_range_detector()` — same Python object, same `.tflite` file. Routing to "full" via `auto_select_model()` had zero effect.

**Affected area:** `backend/pipeline/face_detection.py` — `_get_full_range_detector()`
**Severity:** High — panel/group detection was completely broken; results identical to short-range
**Status:** Fixed (Phase 2A.1, Session 3, 2026-05-16)
**Fix:** Downloaded `blaze_face_full_range.tflite` (1058 KB) from Google's official CDN. Created a separate singleton with `min_suppression_threshold=0.1` (permissive NMS for adjacent faces). Short-range retains `min_suppression_threshold=0.3`.

---

### BUG-007 — Panel/group faces missed: wrong model + aggressive NMS ✓ FIXED

**Symptom:** `03_panel_4person.mp4` returned 0–1 face per frame. Session 2 logged this as "threshold may need tuning" but the real causes were: (1) short-range model is optimized for ≤2m selfie distance and misses faces at group-shot scale; (2) `min_suppression_threshold=0.3` merges adjacent faces in tight panel layouts.

**Affected area:** `backend/pipeline/face_detection.py` — model selection + NMS config
**Severity:** High — any content with > 1 speaker in frame was effectively broken
**Status:** Fixed (Phase 2A.1, Session 3, 2026-05-16)
**Fix:** `detect_faces_with_retry()` cascade: short-range fires only for large close-up faces (≥8% frame area); everything else falls to full-range at 0.3 confidence with NMS=0.1. Panel video now returns 3–5 faces per frame.

---

### BUG-003 — Forgot password: no backend endpoint ✓ FIXED

**Fix (Session 1, 2026-05-12):** Implemented full end-to-end forgot/reset flow. `POST /auth/forgot-password` (3/hour, always 200, email enumeration safe) + `POST /auth/reset-password` (5/hour). `PasswordResetToken` model added with SHA-256 hashed tokens, 15-min expiry, single-use. `send_password_reset_email()` via Resend SDK in `backend/services/notifications.py` with dev console fallback. Frontend `#forgot-modal` now has real 2-step flow; `#view-reset-password` SPA view added. `GET /reset-password` FastAPI passthrough added to `main.py`.

### BUG-005 — MediaPipe requires `libgles2` + `libegl1` in Docker (Phase 2A)

**Symptom:** `mediapipe.tasks.python.vision.FaceDetector.create_from_options()` raises `libGLESv2.so.2: cannot open shared object file: No such file or directory` in the container even when running CPU-only inference. All 25 debug images are generated but show "NO FACE DETECTED" on every frame.

**Affected area:** `Dockerfile` — missing system packages  
**Severity:** Blocker for Phase 2A face detection — detection silently fails  
**Status:** Fixed (Phase 2A) — `libgles2` and `libegl1` added to Dockerfile apt-get install; rebuilt images  
**Notes:** MediaPipe Tasks API uses OpenGL ES for pre/post-processing even in CPU-only mode. The `python:3.11-slim` base image does not include Mesa GL libraries. Fix: add `libgles2 libegl1` to the apt-get RUN layer. This is a one-time infrastructure fix; the libs are small (~5 MB combined).

---

---

## Low priority / Cosmetic

### BUG-004 — `passlib[bcrypt]` listed in requirements but unused

**Symptom:** `backend/requirements.txt` still lists `passlib[bcrypt]` but the codebase uses `bcrypt` directly via `hash_password()` / `verify_password()` in `security.py`. The dependency is redundant dead weight.

**Affected area:** `backend/requirements.txt`  
**Severity:** Low — no functional impact; just installs an extra package  
**Status:** Open  
**Notes:** Safe to remove `passlib[bcrypt]` in a cleanup pass. Confirm no `passlib` imports exist first (`grep -r "passlib" backend/`).

---

### BUG-011 — "Speaker change events" in `conversation_pace.py` are stable-run boundaries, not semantic speaker turns

**Symptom:** On cut-heavy single-speaker footage (e.g. `01_single_speaker.mp4` TEDx talk), `detect_speaker_changes()` reports 107 events. Ground truth = 1 speaker. The ≥2-second debounce filter correctly rejects sub-2s flicker, but each camera cut produces a new track_id (BUG-008) that does hold ≥2s, so each cut is counted as a change.

**Affected area:** `backend/pipeline/conversation_pace.py` — semantic interpretation only; mechanical behavior is correct.
**Severity:** Low — does NOT affect keyframe placement or transition timing (cuts DO warrant re-centering, which is the actual downstream use).
**Status:** Expected behavior, pending BUG-008 fix in Phase 2C. After BUG-008 is fixed, the same code will report semantically correct speaker turn counts.
**Notes:** Do NOT use "change event count" as a proxy for "number of speakers" or "conversation turn count" in any future code until BUG-008 is closed.

---

### BUG-012 — Frontend crop preview uses linear lerp; backend uses cubic ease-in-out

**Symptom:** After Phase 2B.3, `backend/pipeline/reframe.py` produces cubic ease-in-out pans in the rendered MP4. The frontend preview (`effectiveCropAtTime()` in the reframe UI JavaScript) still interpolates between keyframes with a linear `t` ramp. The preview will look slightly different from the rendered export — the pan will appear to snap harder at boundaries in the preview than in the final output.

**Affected area:** Frontend reframe preview JavaScript — `effectiveCropAtTime()` or equivalent interpolation function.
**Severity:** Low — affects preview fidelity only; rendered output is correct. Users may notice the preview motion feels slightly more abrupt than the export.
**Status:** Open — Phase 2D scope.
**Notes:** Fix: replicate the cubic ease-in-out formula in JavaScript and read `transition_dur_in` from the keyframe payload when building the preview timeline. The formula is `u < 0.5 ? 4*u**3 : 1 - Math.pow(-2*u+2, 3)/2` where `u` is the normalized progress within the pan window.
