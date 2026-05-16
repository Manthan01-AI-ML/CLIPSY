# Changelog

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
