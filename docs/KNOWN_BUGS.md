# Known Bugs

---

## Open

### BUG-008 — High track fragmentation in `track_faces_across_frames()` on cut-heavy content

**Symptom:** `01_single_speaker.mp4` (TEDx talk): 963 frames sampled at 2fps → 346 unique tracks. Expected ≤5 for a single-speaker video. `03_panel_4person.mp4`: 1401 frames → 724 tracks. Track IDs fragment on every camera cut or pan because IoU drops to 0 when the face jumps to a new screen position.

**Affected area:** `backend/pipeline/active_speaker.py` — `track_faces_across_frames()` IoU matching
**Severity:** Medium — active speaker timeline still works (falls back to `only_face`/`largest_face`/`lip_movement_dominant` correctly), but long-term track identity for "who spoke in total for how many seconds" is broken. Each cut creates a new track_id.
**Status:** Open (Phase 2B.2 scope)
**Notes:** Potential fixes: (1) increase `IOU_MATCH_THRESHOLD` tolerance for small movements; (2) use face embedding distance (not practical without adding a dep); (3) assign track IDs by bounding-box spatial region instead of IoU frame-to-frame. Low impact on Phase 2B.1 goal (per-second timeline), but Phase 2C needs better track continuity for clip-level speaker labelling.

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
