# Changelog

---

## 2026-05-18 — Session 7: Phase 2C — Worker Auto-Render + BUG-008 Fix

### Changed
- `backend/pipeline/worker.py` — NEW Phase 2C keyframe-generation step between `pick_clips` and render: runs the full active-speaker pipeline (`analyze_audio_activity → track_faces_across_frames → compute_active_speaker_timeline`) ONCE per source video, then calls `place_adaptive_keyframes` per clip with `clip_start/end_sec` and attaches `user_crop` dict to each clip. Wrapped in try/except — failure falls back gracefully to static smart_crop
- `backend/pipeline/worker.py` — `clip_meta` now persists `user_crop` so the Reframe modal can read auto-generated keyframes on open
- `backend/pipeline/render.py` — `render_all_clips` now reads `clip.get("user_crop")` per clip and passes it to `render_one_clip` (which already accepted the kwarg)
- `backend/pipeline/active_speaker.py` — **BUG-008 fix**: `track_faces_across_frames` dual matching — IoU first (smooth motion), 4×4 spatial-grid + 3s region memory fallback (camera cuts). New constants: `SPATIAL_GRID_W=4`, `SPATIAL_GRID_H=4`, `SPATIAL_REGION_TIMEOUT_SEC=3.0`. New helper: `_spatial_region_id()`. Face track IDs now survive cuts when a speaker stays in the same screen region.

### E2E test results (job `f2f91138`, YouTube source, re-run through full pipeline)

| Rank | Start | Dur | KFs | t=0 face position | Transitions |
|---|---|---|---|---|---|
| 1 | 254.0s | 58s | 2 | (0.465, 0.275) face-located | 1 × 0.30s |
| 2 | 29.0s | 53s | 2 | (0.622, 0.312) face-located | 1 × 0.30s |
| 3 | 110.0s | 50s | 1 | (0.490, 0.292) face-located | static hold |
| 4 | 312.0s | 44s | 2 | (0.632, 0.268) face-located | 1 × 0.30s |
| 5 | 347.0s | 51s | 2 | (0.451, 0.297) face-located | 1 × 0.30s |

5/5 clips rendered. Every clip has `user_crop` in `clip.meta`. All t=0 face-located. Visual review on `clip_01.mp4` (h264 1080×1920, 58s, 17.8 MB) passed.

**Known:** `webrtcvad` is in `requirements.txt` but missing from current container image — `pip install` worked in-place. Permanent fix on next `docker compose build`. Logged as BUG-013.

---

## 2026-05-18 — Session 6: Phase 2B.3 — Cubic Ease-In-Out + Adaptive Transitions

### Changed
- `backend/pipeline/reframe.py` — `_build_piecewise_expr`: replaced linear lerp with cubic ease-in-out (`u<0.5 → 4u³`, `u≥0.5 → 1-(−2u+2)³/2`); pan duration now reads per-segment `transition_dur_in` from keyframe dict, falls back to global `transition_dur`
- `backend/pipeline/reframe.py` — `normalize_user_crop`: preserves optional `transition_dur_in` field when cleaning v2 keyframes
- `backend/pipeline/reframe.py` — `validate_keyframes`: preserves and clamps `transition_dur_in` (0.05–2.0s) per keyframe
- `backend/pipeline/conversation_pace.py` — `place_adaptive_keyframes`: each non-zero keyframe now carries `transition_dur_in` derived from pace at that second (`slow=0.30s`, `medium=0.20s`, `fast=0.12s`)
- `backend/pipeline/conversation_pace.py` — `place_adaptive_keyframes`: t=0 keyframe now uses actual face position at `clip_start_int` (track bbox centre), falls back to largest face via forward scan up to 30s, then `(0.5, 0.5)` last resort (fixes BUG A — was hardcoded to center)
- `backend/pipeline/conversation_pace.py` — `place_adaptive_keyframes`: end-of-clip clamp prevents `transition_dur_in` from overshooting video end
- `backend/pipeline/conversation_pace.py` — new helpers: `_largest_face_center_at_second`, `_has_faces_at_second`

### New files
- `scripts/test_adaptive_transitions.py` — renders 2 MP4s per video (slow + medium/fast segment) using `build_keyframe_crop_filter`; finds best 10s window per pace bucket; applies slice-relative `transition_dur_in` clamp; writes filter strings and `summary.json` to `debug_output_2b3/`

### Test results (5 videos, 10-second clips, 2 renders each)

| Video | Slow window | t=0 position | Keyframes | Transition dist | OK |
|---|---|---|---|---|---|
| 01_single_speaker | [0, 10] | (0.50, 0.26) | 2 | {0.30: 1} | ✓ |
| 02_podcast_2person | [0, 10] | (0.61, 0.38) | 2 | {0.30: 1} | ✓ |
| 03_panel_4person | [0, 10] | (0.50, 0.30) | 1 | — | ✓ |
| 04_screenshare | [0, 10] | (0.50, 0.50) | 1 | — | ✓ |
| 05_lowlight | [0, 10] | (0.51, 0.34) | 1 | — | ✓ |

Fast-segment renders for 01, 02, 03 confirmed correct pace-based durations (`{0.30: 2, 0.20: 1}` on 02 fast).

Two visual review iterations: initial constants `0.45/0.28/0.15` → final `0.30/0.20/0.12` after user tuning. All 10 renders passed visual review.

21 output files in `debug_output_2b3/` (10 MP4s + 10 filter.txt + summary.json).

---

## 2026-05-16 — Session 5: Phase 2B.2 — Conversation Pace Classifier + Adaptive Keyframes

### New files
- `backend/pipeline/conversation_pace.py` — debounced speaker-change detection (`detect_speaker_changes`, `min_hold_seconds=2`), 10-second rolling pace window (`classify_pace_window`, `compute_pace_timeline`), adaptive keyframe placement at change events (`place_adaptive_keyframes`, 1.5s minimum dwell, clip-relative `t`, `x_pct/y_pct` from bbox centre average)
- `scripts/test_conversation_pace.py` — loads Phase 2B.1 JSON outputs, runs all 4 functions, produces 4-row timeline PNG per video + `{stem}_pace.json` + `{stem}_keyframes.json` + `summary.json` in `debug_output_2b2/`

### Changed
- `backend/api/routes/clips.py` — `auto_keyframes_endpoint`: replaced Haar-based `smart_crop.auto_keyframes_from_detection()` with the Phase 2B active-speaker pipeline (`analyze_audio_activity → track_faces_across_frames → compute_active_speaker_timeline → place_adaptive_keyframes`); added `diagnostics` dict to response
- `docs/KNOWN_BUGS.md` — BUG-008 status corrected from "Phase 2B.2 scope" to "Phase 2C scope"

### Test results (5 videos, loading Phase 2B.1 cached JSON)

| Video | Duration | Changes | Slow | Medium | Fast | Keyframes |
|---|---|---|---|---|---|---|
| 01_single_speaker | 482s | 107 | 244s | 238s | 0s | 108 |
| 02_podcast_2person | 706s | 121 | 495s | 211s | 0s | 122 |
| 03_panel_4person | 701s | 169 | 299s | 401s | 1s | 170 |
| 04_screenshare | 266s | 13 | 262s | 4s | 0s | 14 |
| 05_lowlight | 33s | 4 | 27s | 6s | 0s | 5 |

High change counts for videos 1–3 are BUG-008 (track fragmentation on camera cuts) — deferred to Phase 2C. Podcast video correctly alternates x_pct ~0.33 ↔ ~0.67 for left/right speakers.

16 output files pulled to `scripts/debug_output_2b2/` — awaiting user review.

---

## 2026-05-16 — Session 4: Phase 2B.1 — Audio Activity + Active Speaker Detection

### New files
- `backend/pipeline/audio_activity.py` — per-second speech activity from video: audio extraction (ffmpeg), RMS energy per 100ms window, webrtcvad VAD, voice segment collapse, unified `analyze_audio_activity()` entry point
- `backend/pipeline/active_speaker.py` — assign "who is speaking" per second: IoU-based face tracking across frames (adaptive fps: 4/2/1 based on duration), lip movement scoring (mouth Y-variance / 200 px²), `compute_active_speaker_timeline()` per-second pipeline
- `scripts/test_active_speaker.py` — debug script: 3-row matplotlib timeline PNG per video (energy / voice activity / active speaker track), audio.json + tracks.json + speakers.json + summary.json outputs in `debug_output_2b1/`

### Changed
- `backend/requirements.txt` — added `webrtcvad==2.0.10` (compiled from source on Python 3.11, no issues) and `matplotlib>=3.5.0` (was already installed as mediapipe transitive dep, now explicit)

### Fixed (in-session)
- **IndexError in `compute_active_speaker_timeline`**: when a second's frames contained no detected faces, `track_ids_this_second` was empty, bypassing the `len==1` guard and crashing on an empty `sorted_tracks[0]`. Added `len(track_ids) == 0 → "no_faces"` guard.

### MediaPipe keypoint verified (mouth = index 3)
- Confirmed in running container (mediapipe 0.10.35): all 6 keypoints have `label=None`. Positional index 3 is mouth center. Index ordering: [0]=right_eye [1]=left_eye [2]=nose_tip [3]=mouth_center [4]=right_ear [5]=left_ear.

### Test results (all 5 videos complete)

**Audio activity:**
| Video | Duration | Voice % | Segments |
|---|---|---|---|
| 01_single_speaker | 481.6s | 74.7% | 192 |
| 02_podcast_2person | 705.4s | 91.6% | 212 |
| 03_panel_4person | 700.3s | 94.2% | 95 |
| 04_screenshare | 265.9s | 97.9% | 32 |
| 05_lowlight | 32.6s | 57.7% | 13 |

**Active speaker reasoning breakdown:**
| Video | audio_inactive | no_faces | only_face | lip_dominant | largest_face |
|---|---|---|---|---|---|
| 01_single_speaker | 26 | 7 | 298 | 13 | 138 |
| 02_podcast_2person | 4 | 0 | 511 | 75 | 116 |
| 03_panel_4person | 20 | 8 | 383 | 85 | 205 |
| 04_screenshare | 0 | 89 | 115 | 52 | 10 |
| 05_lowlight | 4 | 7 | 13 | 8 | 1 |

5 timeline PNGs generated at 200 DPI in `scripts/debug_output_2b1/` — awaiting user review.

---

## 2026-05-16 — Session 3: Phase 2A.1 — Fix MediaPipe Detection Gaps

### Fixed
- **BUG-006**: Full-range detector was returning the short-range singleton. `_get_full_range_detector()` now loads `blaze_face_full_range.tflite` — a genuinely different model — with `min_suppression_threshold=0.1` (permissive NMS for adjacent panel faces).
- **BUG-007**: Panel/group face detection was broken. New `detect_faces_with_retry()` cascade (short≥0.5→full≥0.3→CLAHE+full≥0.3) and the correct full-range model now detect 3–5 faces per frame in panel content (was 0–1).

### New features
- **`detect_faces_with_retry(frame)`** — cascading multi-pass entry point returning `(faces, detection_path)`. Replaces `detect_faces_smart()` as the canonical function.
- **`enhance_for_detection(frame)`** — CLAHE on LAB L-channel for low-light preprocessing (safety-net fallback; full-range at 0.3 handles all current test videos without it).

### Changed
- `backend/pipeline/face_detection.py` — complete rewrite: two real model URLs, two separate singletons, tiered confidence, no post-filter in `_run_detection()`, updated `detect_faces_smart()` and `legacy_compatible_detect()` to call `detect_faces_with_retry()`.
- `scripts/test_face_detection.py` — complete rewrite: 8 frames at `[5,15,25,40,55,70,85,95]%`, colour-coded path badge per image, CLAHE before/after saved when triggered, per-frame path in `summary.json`.

### New files / directories
- `backend/pipeline/models/` — placeholder directory for Phase 2C model baking; `.tflite` files gitignored, downloaded to `/tmp/mediapipe_models/` at runtime.

### Detection results (Phase 2A.1 test run)
| Video | Frames | Faces | Dominant path |
|---|---|---|---|
| 01_single_speaker (TEDx) | 8/8 | 8 | full×6, short×2 |
| 02_podcast_2person | 8/8 | 8 | full×8 |
| 03_panel_4person | 8/8 | 14 | full×8 |
| 04_screenshare | 8/8 | 5 | full×5, none×3 |
| 05_lowlight | 8/8 | 6 | full×6, none×2 |

---

## 2026-05-15 — Session 2: Phase 2A — MediaPipe Face Detection Foundation

### New features
- **MediaPipe face detection module** (`backend/pipeline/face_detection.py`): Tasks API-based detector with singleton lazy-init, short-range and full-range entry points, auto model selection heuristic (portrait/ultra-wide/landscape), `detect_faces_smart()` convenience wrapper, and `legacy_compatible_detect()` drop-in adapter matching `_FaceDetector.detect_in_frame()` signature for Phase 2B.
- **Visual debug script** (`scripts/test_face_detection.py`): samples 5 frames per test video, annotates bounding boxes + confidence labels + landmarks, writes 25 annotated JPEGs and `summary.json` to `scripts/debug_output/`.
- **Test video set** (`test_videos/`): 5 representative clips (single speaker, 2-person podcast, 4-person panel, screenshare, low-light).

### Changed
- `backend/requirements.txt`: added `mediapipe==0.10.35` (verified on PyPI; latest stable; Python 3.9–3.12 supported).
- `Dockerfile`: added `libgles2 libegl1` to apt-get install (required by MediaPipe Tasks API even in CPU-only mode; not included in `python:3.11-slim`).
- `docker-compose.yml`: added `./test_videos:/app/test_videos:ro` volume to `backend` and `worker` services.

### New files
- `backend/pipeline/face_detection.py` — MediaPipe Tasks API detector (5 functions; smart_crop.py NOT modified)
- `scripts/test_face_detection.py` — Phase 2A visual verification debug script
- `scripts/debug_output/` — 25 annotated JPEGs + summary.json (generated in-session)
- `test_videos/` — 5 test videos for Phase 2A/2B verification

### Docs
- `docs/DECISIONS.md`: MediaPipe Tasks API decision logged (with alternatives considered)
- `docs/KNOWN_BUGS.md`: BUG-005 added and marked Fixed (libGLESv2 missing in Docker)
- `docs/SESSION_LOG.md`: Session 2 entry added

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
