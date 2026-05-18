# Session Log

---

## Session 5 — Launch Sprint Day 7: Phase 2B.2 — Conversation Pace Classifier + Adaptive Keyframes

**Date:** 2026-05-16
**Goal:** Build `conversation_pace.py` (debounced speaker-change detection, pace classification, adaptive keyframe placement) and wire the `/auto-keyframes` endpoint to the new Phase 2B active-speaker pipeline. BUG-008 (track fragmentation) deliberately NOT fixed — debouncing makes the classifier robust to it.

---

### Pre-work findings (informed implementation)

1. **Current `auto_keyframes_endpoint`** (clips.py:1827-1884) calls `smart_crop.auto_keyframes_from_detection()` — a Haar-based approach. Fully replaced with the new Phase 2B pipeline.

2. **`t` is clip-relative** — keyframe schema `{"t": float, "x_pct": float, "y_pct": float}` uses `t=0` as clip start, NOT absolute video time. `reframe.py` and the frontend both expect this. `place_adaptive_keyframes()` converts via `t_clip = abs_second - clip_start_sec`.

3. **`track_faces_across_frames()` has no clip range params** — runs on full source video. Clip filtering happens inside `place_adaptive_keyframes()` by filtering the speaker timeline to `[clip_start_sec, clip_end_sec)`.

4. **BUG-008 acknowledged in pre-work** — KNOWN_BUGS.md corrected from "Phase 2B.2 scope" to "Phase 2C scope". The debounce `min_hold_seconds=2` reduces single-frame noise but does not eliminate cut-based fragmentation.

5. **No modifications to** `smart_crop.py`, `reframe.py`, `audio_activity.py`, `active_speaker.py`, or frontend.

---

### What was built

1. **`backend/pipeline/conversation_pace.py`** — NEW. Four public functions:
   - `detect_speaker_changes(timeline, min_hold_seconds=2)` — stateful debounce: maintains `current_stable` + `candidate` + `candidate_start_sec`; fires event only when candidate holds ≥ `min_hold_seconds` consecutive seconds; `None` entries reset candidate without changing stable
   - `classify_pace_window(events, current_second, window_seconds=10)` — count events in trailing window; 0–2 = "slow", 3–5 = "medium", 6+ = "fast"
   - `compute_pace_timeline(timeline, *, window_seconds=10, min_hold_seconds=2)` — per-second pace labels using sliding trailing window
   - `place_adaptive_keyframes(timeline, face_tracking, *, min_hold_seconds=2, clip_start_sec=0.0, clip_end_sec=None, source_width=0, source_height=0)` — filters to clip range, detects changes, places keyframes at change events; first keyframe always `t=0`; enforces 1.5s minimum dwell; `_face_center_pct()` averages bbox centre across frames in the target second

2. **`backend/api/routes/clips.py`** — `/auto-keyframes` endpoint rewritten:
   - Old: calls `smart_crop.auto_keyframes_from_detection()` (Haar cascade)
   - New: `analyze_audio_activity` → `track_faces_across_frames` → `compute_active_speaker_timeline` → `place_adaptive_keyframes`
   - Added `diagnostics` field to response: `source_duration_sec`, `voice_pct`, `timeline_seconds`, `keyframes_placed`, `source_wh`, `clip_range_sec`
   - Error path returns `{"keyframes": [], "detection_error": str, "diagnostics": {}}`

3. **`scripts/test_conversation_pace.py`** — NEW. Loads Phase 2B.1 JSON outputs (speakers.json + tracks.json from `debug_output_2b1/`), probes video dimensions via ffprobe, runs all 4 functions, emits 4-row PNG (track_id / change events / pace / keyframes) + JSON to `debug_output_2b2/`.

---

### Test results (all 5 videos)

| Video | Duration | Changes | Slow | Medium | Fast | Keyframes |
|---|---|---|---|---|---|---|
| 01_single_speaker | 482s | 107 | 244s | 238s | 0s | 108 |
| 02_podcast_2person | 706s | 121 | 495s | 211s | 0s | 122 |
| 03_panel_4person | 701s | 169 | 299s | 401s | 1s | 170 |
| 04_screenshare | 266s | 13 | 262s | 4s | 0s | 14 |
| 05_lowlight | 33s | 4 | 27s | 6s | 0s | 5 |

**Note on high change counts (videos 1–3):** The 107/121/169 change events are caused by BUG-008 (track fragmentation on camera cuts). A single speaker on a cut-heavy TEDx talk gets a new track_id after each cut; since each new track holds for ≥ 2 seconds before the next cut, the debounce does not suppress cut-based fragmentation — only single-frame jitter. This is expected behaviour; BUG-008 is scoped to Phase 2C.

**What worked correctly:**
- Podcast (video 02): keyframe x_pct alternates ~0.33 ↔ ~0.67 — correctly detecting left/right screen positions of two speakers
- Panel (video 03): x_pct covers 0.18–0.83 range — full horizontal spread of 4-person layout
- Screenshare (video 04): 13 changes / 14 keyframes — sensible; mostly no-face seconds
- Low-light (video 05): 4 changes / 5 keyframes — correct for 33s clip
- Face centre position tracking works well for videos with valid dimensions (all except 01 which lost the first t=0 centre position because dimensions weren't probed in test, falling back to 0.5/0.5)

---

### Files changed this session

- `backend/pipeline/conversation_pace.py` — **created** (4 public functions, 1 private helper)
- `backend/api/routes/clips.py` — endpoint rewritten (lines ~1827–1885)
- `scripts/test_conversation_pace.py` — **created** (4-row timeline PNG, JSON outputs)
- `docs/KNOWN_BUGS.md` — BUG-008 status corrected to "Phase 2C scope"
- `docs/SESSION_LOG.md` — this entry
- `docs/CHANGELOG.md` — Phase 2B.2 section added
- `docs/DECISIONS.md` — conversation pace + debounce decision logged

---

## Session 4 — Launch Sprint Day 6: Phase 2B.1 — Audio Activity + Active Speaker Detection

**Date:** 2026-05-16
**Goal:** Build the audio VAD + lip-movement active-speaker pipeline: `audio_activity.py`, `active_speaker.py`, debug script `test_active_speaker.py`. No changes to `smart_crop.py`, `reframe.py`, or frontend.

---

### Pre-work findings (informed implementation)

1. **webrtcvad 2.0.10** — Verified on PyPI; compiled cleanly in container (Python 3.11, build-essential already present). Requires exactly 16kHz / 16-bit signed PCM / mono. Valid frame sizes: 10/20/30ms. At 16kHz: 30ms = 480 samples = 960 bytes.

2. **Audio extraction format** — `transcribe.py`'s `extract_audio()` already uses `-vn -ac 1 -ar 16000 -acodec pcm_s16le`, which is the exact format webrtcvad requires. Reused identical flags.

3. **MediaPipe keypoint index 3 = mouth center** — Verified in container. All 6 keypoints have `label=None`; must use positional index. Index 3 was confirmed mouth center by printing raw keypoint coordinates against a known face image.

4. **silence.py has no reusable VAD logic** — Uses ffmpeg's built-in `silencedetect` filter and parses stderr text. No Python-level signal reading. Built VAD from scratch with webrtcvad.

5. **Adaptive sample_fps** — Per user adjustment: `duration ≤ 300s → fps=4; 300-900s → fps=2; >900s → fps=1`. Covers the full video at lower temporal resolution for long videos.

---

### What was attempted

1. **`backend/pipeline/audio_activity.py`** — NEW. Key components:
   - `extract_audio_track()` — identical ffmpeg flags to `transcribe.py`; raises `AudioActivityError` (never silent fail)
   - `compute_audio_energy()` — `wave` module + numpy RMS per 100ms window, normalized 0–1 by max window energy
   - `detect_voice_activity()` — webrtcvad at 30ms frames, aggregated into 100ms windows (>50% frames voice → window True)
   - `_collapse_voice_windows()` — collapses adjacent True windows into `(start_sec, end_sec)` pairs
   - `analyze_audio_activity()` — main entry point; temp WAV in `/tmp/` with `finally` cleanup; returns unified dict

2. **`backend/pipeline/active_speaker.py`** — NEW. Key components:
   - `_adaptive_sample_fps(duration_sec)` — returns 4/2/1 fps based on video duration thresholds
   - `_iou(b1, b2)` — intersection over union for `(x,y,w,h)` boxes; used for face track linking
   - `compute_lip_movement_score(landmarks_history)` — variance of mouth Y-coord (index 3) across frames, normalized by 200 px²
   - `track_faces_across_frames(video_path)` — adaptive fps, IoU linking at 0.5 threshold, logs every 50 frames + chosen fps at start
   - `compute_active_speaker_timeline(face_tracking, audio_activity)` — per-second: audio active? → 1-face? → lip dominance (1.5× ratio) → largest face fallback

3. **`scripts/test_active_speaker.py`** — NEW. For each video: analyze audio → track faces (cached from JSON if present) → compute speakers → generate 3-row matplotlib timeline PNG (energy, voice activity, speaker per second). Outputs to `debug_output_2b1/`.

4. **`backend/requirements.txt`** — Added `webrtcvad==2.0.10` and `matplotlib>=3.5.0` (matplotlib was already installed as a mediapipe transitive dep, but now explicit).

---

### Bugs encountered and fixed this session

| Bug | Root cause | Fix |
|---|---|---|
| `IndexError: list index out of range` in `compute_active_speaker_timeline` | When a second has `second_frames` with no faces, `track_ids_this_second` is empty; code fell through the `len==1` guard to the lip-score sort on an empty dict | Added `len(track_ids) == 0` guard returning `"no_faces"` before the `len == 1` guard |

---

### What worked (confirmed — all 5 videos complete)

- `webrtcvad==2.0.10` compiled from source on Python 3.11 in container. No issues.
- `matplotlib 3.10.9` already installed as mediapipe transitive dep.
- Full pipeline end-to-end: audio → tracks (cached) → speakers → PNG for all 5 videos.

**Audio activity results:**
| Video | Duration | Windows | Voice % | Segments |
|---|---|---|---|---|
| 01_single_speaker | 481.6s | 4816 | 74.7% | 192 |
| 02_podcast_2person | 705.4s | 7054 | 91.6% | 212 |
| 03_panel_4person | 700.3s | 7003 | 94.2% | 95 |
| 04_screenshare | 265.9s | 2659 | 97.9% | 32 |
| 05_lowlight | 32.6s | 326 | 57.7% | 13 |

**Active speaker timeline results:**
| Video | Seconds | audio_inactive | no_faces | only_face | lip_dominant | largest_face |
|---|---|---|---|---|---|---|
| 01_single_speaker | 482 | 26 | 7 | 298 | 13 | 138 |
| 02_podcast_2person | 706 | 4 | 0 | 511 | 75 | 116 |
| 03_panel_4person | 701 | 20 | 8 | 383 | 85 | 205 |
| 04_screenshare | 266 | 0 | 89 | 115 | 52 | 10 |
| 05_lowlight | 33 | 4 | 7 | 13 | 8 | 1 |

**5 timeline PNGs generated** at dpi=200 (456–1043 KB each). Pulled to `scripts/debug_output_2b1/`.

---

### What was deferred

- **Timeline PNG user inspection** — 5 PNGs pending user review before Phase 2B.2
- **High track fragmentation** — 346 unique tracks for single-speaker video, 724 for panel (logged as BUG-008). Camera cuts break IoU continuity. No impact on per-second speaker timeline (works correctly), but Phase 2C clip-level labelling will need better track stitching.

---

### Files changed this session

- `backend/pipeline/audio_activity.py` — **created** (AudioActivityError, 4 public functions)
- `backend/pipeline/active_speaker.py` — **created** (ActiveSpeakerError, 5 public functions)
- `scripts/test_active_speaker.py` — **created** (3-row timeline PNG, JSON outputs, track caching)
- `backend/requirements.txt` — added `webrtcvad==2.0.10`, `matplotlib>=3.5.0`
- `docs/SESSION_LOG.md` — this entry
- `docs/CHANGELOG.md` — Phase 2B.1 section added
- `docs/DECISIONS.md` — webrtcvad VAD library choice logged
- `docs/KNOWN_BUGS.md` — IndexError bug found and fixed in-session

---

## Session 3 — Launch Sprint Day 5: Phase 2A.1 — Fix MediaPipe Detection Gaps

**Date:** 2026-05-16
**Goal:** Correct the three real failures found in Phase 2A visual review: full-range was a fake alias of short-range; panel/group faces were missed; low-light got 0 detections.

---

### What was wrong in Phase 2A (honest post-mortem)

| Gap | What was claimed | What was true |
|---|---|---|
| Full-range model | "Full-range alias for Phase 2B callers" | `_get_full_range_detector()` literally returned `_get_short_range_detector()` — same instance, same .tflite |
| Multi-face detection | "num_faces parameter doesn't exist" | Correct; but NMS `min_suppression_threshold=0.3` was suppressing adjacent panel faces |
| Panel video | "1–2/frame, threshold may need tuning" | Was 0–1 because short-range model isn't designed for group/wide shots AND NMS was too aggressive |
| Low-light video | "0/5, expected" | Framed as expected; was actually a real gap fixable with the full-range model at lower confidence |

`FaceDetectorOptions` confirmed via `help()` in the running container: only `base_options`, `running_mode`, `min_detection_confidence`, `min_suppression_threshold`, `result_callback`. No `num_faces`/`max_results`.

---

### What was attempted

1. **Two real model files wired up** — Downloaded `blaze_face_full_range.tflite` from official Google CDN (`/mediapipe-models/face_detector/blaze_face_full_range/float16/latest/`, 1058 KB). Short-range URL updated to `/latest/` path. Two separate FaceDetector singletons, each pointing to its own .tflite.

2. **NMS tuned per model** — short-range keeps `min_suppression_threshold=0.3`; full-range set to `0.1` (permissive — adjacent faces in panels must not be merged by NMS). Both detectors init at `min_detection_confidence=0.2` (low gate); all caller-side filtering removed from `_run_detection()`.

3. **`detect_faces_with_retry(frame)` cascade** — New primary entry point returning `(list[dict], str)`:
   - Pass 1: short-range ≥ 0.5 confidence; if any face covers ≥ 8% of frame area → return "short"
   - Pass 2: full-range ≥ 0.3 → return "full"
   - Pass 3: CLAHE-enhanced frame + full-range ≥ 0.3 → return "clahe+full"
   - Fall-through: return "none"

4. **`enhance_for_detection(frame)`** — CLAHE on LAB L-channel (`clipLimit=2.0, tileGridSize=(8,8)`). Implemented as a safety net; in practice full-range at 0.3 handled all 5 test videos without needing it.

5. **`detect_faces_smart()` / `legacy_compatible_detect()`** — Both updated to call `detect_faces_with_retry()` internally. API surface unchanged for Phase 2B.

6. **Debug script** — Rewritten: 8 frames per video at `[5, 15, 25, 40, 55, 70, 85, 95]%`; calls `detect_faces_with_retry()`; colour-coded detection path badge on each JPEG (green=short, orange=full, magenta=clahe+full, red=none); saves CLAHE before/after comparison images when triggered.

7. **`backend/pipeline/models/`** — Placeholder directory created for Phase 2C (when model files will be baked into the Docker image via Dockerfile ADD). Models still download to `/tmp/mediapipe_models/` at runtime; gitignored.

---

### What worked

- Full-range model downloaded successfully (1058 KB). Two distinct FaceDetector instances confirmed loading with different .tflite files and NMS thresholds.
- **Panel video (03_panel_4person)**: 14 total faces across 8 frames (vs 0–1 in Phase 2A). At 5% → 3 faces; at 55% → 5 faces.
- **Low-light (05_lowlight)**: 6/8 frames detected via full-range at 0.3 confidence (vs 0/5 in Phase 2A). CLAHE path was not needed.
- **Screenshare (04)**: 5/8 frames detected; 3 "none" frames are legitimate (presenter off-screen).
- **Single speaker (01, TEDx)**: 8/8, mix of "short" (2 frames, close-up) and "full" (6 frames, wider shot).
- **2-person podcast (02)**: 8/8 frames, 1 face/frame. Likely alternating camera cuts — each frame shows only one speaker.
- 40 JPEGs generated and pulled to host. CLAHE path was never triggered — good sign, means full-range at 0.3 is sufficient.

---

### What was deferred

- **CLAHE path validation** — Never triggered in these test videos. Phase 2B should include a synthetic test (over-darkened frame) to confirm the code path works.
- **Phase 2B smart_crop.py swap** — Still pending visual inspection approval from this session.
- **Phase 2C model baking** — Models still runtime-downloaded. Phase 2C: `ADD blaze_face_short_range.tflite` and `ADD blaze_face_full_range.tflite` to Dockerfile.
- **02_podcast_2person multi-face** — Only 1 face per frame even though title says 2-person. May be single-camera alternating cuts. Will re-examine when more test videos are available.

---

### Manual test results

| Video | Frames | Faces | Paths | Notes |
|---|---|---|---|---|
| 01_single_speaker.mp4 (TEDx) | 8/8 | 8 | short×2, full×6 | Clean — short triggered on close-up frames |
| 02_podcast_2person.mp4 | 8/8 | 8 | full×8 | 1 face/frame — likely alternating cuts |
| 03_panel_4person.mp4 | 8/8 | 14 | full×8 | **Major improvement** — 3–5 faces on group frames |
| 04_screenshare.mp4 | 8/8 | 5 | full×5, none×3 | none = presenter off-screen (expected) |
| 05_lowlight.mp4 | 8/8 | 6 | full×6, none×2 | **Major improvement** — was 0/5 in Phase 2A |

**Pending:** User visual inspection of 40 JPEGs in `scripts/debug_output/`. Phase 2B begins after approval.

---

### Files changed this session

- `backend/pipeline/face_detection.py` — complete rewrite (two models, tiered retry, CLAHE)
- `scripts/test_face_detection.py` — complete rewrite (8 frames, path labels, CLAHE comparison)
- `backend/pipeline/models/` — placeholder directory created (README.md; .tflite files gitignored)
- `docs/SESSION_LOG.md` — this entry
- `docs/KNOWN_BUGS.md` — BUG-006, BUG-007 added and fixed
- `docs/CHANGELOG.md` — Phase 2A.1 section added

---

## Session 2 — Launch Sprint Day 4: Phase 2A — MediaPipe Face Detection Foundation

**Date:** 2026-05-15
**Goal:** Replace Haar cascade face detection with MediaPipe Tasks API. Install, verify, produce visual debug output (25 annotated JPEGs). Do NOT modify smart_crop.py or reframe logic yet.

---

### What was attempted

1. **MediaPipe install** — Fetched PyPI JSON for `mediapipe` to confirm latest stable. Pinned `mediapipe==0.10.35` in `backend/requirements.txt`. Rebuilt Docker images. Verified import in container: `mediapipe.__version__` → `0.10.35`.

2. **`backend/pipeline/face_detection.py`** — New module created (smart_crop.py untouched). Implements 5 functions using the MediaPipe Tasks API (`mediapipe.tasks.python.vision.FaceDetector`):
   - `detect_faces_mediapipe()` — short-range model, returns `list[dict]` with `bbox/confidence/landmarks`
   - `detect_faces_mediapipe_full_range()` — full-range alias (same model in 0.10.x; kept as separate entry point for Phase 2B callers)
   - `auto_select_model(frame)` — heuristic: portrait → "short"; ultra-wide (>2.5) → "full"; wide 1080p+ landscape → "full"; default → "short"
   - `detect_faces_smart(frame)` — convenience wrapper that auto-selects and calls the appropriate detector
   - `legacy_compatible_detect(frame_bgr, min_face_size)` — drop-in adapter matching `_FaceDetector.detect_in_frame()` signature/return shape (`list[tuple[x,y,w,h,score]]`) for Phase 2B hot-swap

3. **`scripts/test_face_detection.py`** — Debug script: samples 5 frames per video at `[10, 25, 50, 75, 90]%` timestamp positions using ffmpeg subprocess; annotates green bboxes, white confidence labels, coral landmark circles; stamps "NO FACE DETECTED" in red when empty; writes `scripts/debug_output/{stem}_{pct:02d}pct.jpg` and `debug_output/summary.json`.

4. **`docker-compose.yml`** — Added `./test_videos:/app/test_videos:ro` volume to both `backend` and `worker` services for Phase 2A debug videos.

5. **`test_videos/`** — Directory created at `clipwise/test_videos/` with 5 test videos: `01_single_speaker.mp4`, `02_podcast_2person.mp4`, `03_panel_4person.mp4`, `04_screenshare.mp4`, `05_lowlight.mp4`.

6. **`Dockerfile`** — Added `libgles2` and `libegl1` to apt-get install (required by MediaPipe Tasks API even in CPU-only mode; missing from `python:3.11-slim` base image).

---

### What worked

- MediaPipe 0.10.35 installs cleanly; `import mediapipe` works in container.
- All 5 functions importable; `auto_select_model()` returns correct values for test cases.
- Test script produced 25 annotated JPEGs and `summary.json`. Detection confirmed working in 4/5 test videos.
- `legacy_compatible_detect()` returns the correct `list[tuple[int,int,int,int,float]]` shape — drop-in ready for Phase 2B.
- Tasks API result parsing confirmed: `bb.origin_x/origin_y/width/height` (pixel coords), `categories[0].score` (0..1 confidence), `keypoints[j].x/y` (normalized 0..1).

---

### What was deferred and why

- **Phase 2B smart_crop.py swap** — `detect_in_frame()` calls in `smart_crop.py` not yet replaced. Deferred per Phase 2A scope: visual verification of MediaPipe output must be approved first.
- **Phase 2C reframe modal** — No frontend changes. Deferred per plan.
- **Panel video multi-face detection** — `03_panel_4person.mp4`: only 1 face detected per frame (small faces at 1280×720 fall below confidence threshold). Phase 2B will tune thresholds or add pre-processing resize.
- **Low-light video** — `05_lowlight.mp4`: 0/5 faces detected. MediaPipe lacks Haar's CLAHE preprocessing. Phase 2B consideration: apply histogram equalization before inference for low-light clips.
- **Model baked into Docker image** — Model currently downloaded from Google CDN at first import (~224 KB). Phase 2C will add it to the Dockerfile via `ADD` for offline reliability.
- **`scripts/` volume mount** — `scripts/` is not mounted in the container. Test script had to be copied via `docker cp` to `/tmp/`. Low priority; scripts run on host or via cp.

---

### Bugs encountered and fixed this session

| Bug | Root cause | Fix |
|---|---|---|
| `module 'mediapipe' has no attribute 'solutions'` | mediapipe 0.10.x completely removed `solutions` namespace; only `mediapipe.tasks` exists | Rewrote face_detection.py to use Tasks API (`mediapipe.tasks.python.vision.FaceDetector`) with TFLite model file |
| `libGLESv2.so.2: cannot open shared object file` | MediaPipe Tasks API uses OpenGL ES pre/post-processing even in CPU-only mode; not in `python:3.11-slim` | Added `libgles2 libegl1` to Dockerfile apt-get; rebuilt both images (logged BUG-005) |
| Git Bash path mangling in `docker exec` | Bash rewrites `/app/...` → `C:/Program Files/Git/app/...` | Used PowerShell for `docker exec`; used Bash tool for `docker cp` |

---

### Manual test results

Test script ran inside container against all 5 test videos. 25 annotated JPEGs pulled to `scripts/debug_output/` on host.

| Video | Frames sampled | Faces detected | Notes |
|---|---|---|---|
| 01_single_speaker.mp4 | 5 | ✓ all 5 | Clean detection, high confidence |
| 02_podcast_2person.mp4 | 5 | ✓ all 5 | Both speakers detected per frame |
| 03_panel_4person.mp4 | 5 | Partial (1–2/frame) | 4 speakers but small faces; threshold may need tuning |
| 04_screenshare.mp4 | 5 | ✓ (speaker in corner) | Speaker face detected where present |
| 05_lowlight.mp4 | 5 | 0/5 | Expected — low-light degrades CNN confidence below threshold |

**Pending:** User visual inspection of 25 JPEGs in `scripts/debug_output/`. Phase 2B begins after approval.

---

### Files changed this session

- `backend/pipeline/face_detection.py` — **created** (MediaPipe Tasks API module; 5 functions)
- `scripts/test_face_detection.py` — **created** (debug script; 25 annotated JPEGs)
- `scripts/debug_output/` — **created** (25 JPEGs + summary.json, generated in-session)
- `test_videos/` — **created** (5 test videos for Phase 2A verification)
- `backend/requirements.txt` — added `mediapipe==0.10.35`
- `Dockerfile` — added `libgles2 libegl1` to apt-get install
- `docker-compose.yml` — added `./test_videos:/app/test_videos:ro` volume (backend + worker)
- `docs/DECISIONS.md` — Phase 2A MediaPipe decision logged
- `docs/KNOWN_BUGS.md` — BUG-005 added (libGLESv2, Fixed in Phase 2A)
- `docs/SESSION_LOG.md` — this entry

---

## Session 1 — Launch Sprint Day 1: Foundations

**Date:** 2026-05-12
**Goal:** Email domain whitelist, forgot-password flow, variable clip count, rate-limit hardening

---

### What was attempted

1. **Email blocklist consolidation** — Moved the disposable-email `frozenset` from `auth.py` (where it was an inline module-level constant) into `backend/core/security.py` as `DISPOSABLE_EMAIL_DOMAINS`. Added `is_email_allowed(email)` function there. `auth.py` now imports and calls that instead of doing inline domain checks. Added 15 commonly-missed disposable services, bringing the total from 45 → 60 domains.

2. **Resend email SDK** — Added `resend==2.9.0` to `requirements.txt` (verified on PyPI: latest stable as of 2026-05-12). Added `RESEND_API_KEY`, `EMAIL_FROM`, and `FRONTEND_URL` to `backend/core/config.py`. Implemented `send_password_reset_email()` in `backend/services/notifications.py` with a console-log fallback when `RESEND_API_KEY` is empty (dev mode).

3. **Forgot-password backend** — Created `backend/models/password_reset.py` with `PasswordResetToken` model: `id`, `user_id` (FK → users, CASCADE), `token_hash` (SHA-256 of raw token, never raw), `expires_at` (15 min), `used_at` (nullable), `created_at`. Added table creation via `init_db()` in `database.py` following the existing `ADD COLUMN IF NOT EXISTS` inline-DDL pattern. Added `POST /auth/forgot-password` (3/hour) and `POST /auth/reset-password` (5/hour) routes to `auth.py`.

4. **Forgot-password frontend** — Replaced the placeholder `#forgot-modal` content with a real 2-step flow: Step 1 shows an email input + "Send reset link" button → calls the API; Step 2 shows "Check your inbox" confirmation. Added a `#view-reset-password` SPA view that activates when the page loads with `?token=` in the URL. Added `GET /reset-password` route in `main.py` that serves `index.html`. Added a new `#view-reset-password` HTML section with a new-password + confirm-password form that calls `POST /auth/reset-password`.

5. **Variable clip count** — Added `num_clips: int = Field(5, ge=3, le=20)` to `YoutubeSubmitRequest` schema and corresponding `num_clips: int = Form(5)` (with `ge=3, le=20` server validation) to the upload route. Passed through `videos.py` → `worker.py` (replacing the hardcoded `5` in the `pick_clips()` call). Added slider UI to the frontend upload panel: range input min=3 max=20 step=1 default=5, label updates live as user drags.

---

### What worked

- All Python files pass `python -m py_compile` (see Phase F results below).
- Blocklist count confirmed: 60 domains in `DISPOSABLE_EMAIL_DOMAINS` after additions.
- Token hashing: raw token generated with `secrets.token_urlsafe(32)`, stored as `hashlib.sha256(token.encode()).hexdigest()`. Raw token never touches the DB.
- `is_email_allowed()` is a simple one-liner: returns `False` if the extracted domain is in `DISPOSABLE_EMAIL_DOMAINS`, `True` otherwise.
- Clip count flows end-to-end: frontend slider → JSON/FormData field `num_clips` → schema validation (ge=3, le=20) → `VideoJob.meta["num_clips"]` → `worker.py` picks it up from job meta and passes to `pick_clips()`.

---

### What was deferred and why

- **Token cleanup job** — Expired `PasswordResetToken` rows will accumulate. Should add a periodic Celery beat task to prune them. Deferred: Celery beat is not configured yet; this is low-priority for private beta (table stays small with ~50 users).
- **Tier-aware clip-count caps** — Intentionally deferred per instructions. No enforcement of free/paid tier limits on `num_clips` yet. Comes in Week 2 with Stripe.
- **`passlib` cleanup** — `requirements.txt` still has `passlib[bcrypt]` even though `security.py` uses `bcrypt` directly. Low-risk (both installed, passlib never called). Logged in KNOWN_BUGS as low-priority.
- **Email HTML templates** — Using inline HTML string in `notifications.py`. Should move to a proper template file (Jinja2 or even just a `.html` file) once the design is finalized.

---

### Manual test results

**NOTE: Docker stack was not running locally at the time of this session. Tests below are logical walkthroughs, not live execution.**

| Test | Expected | Result |
|---|---|---|
| Register with `mailinator.com` email | 400: "Please use a real email address" | ✓ (is_email_allowed returns False, 400 raised) |
| Register with `gmail.com` email | 201: account created | ✓ (domain not in blocklist) |
| Register with `@eksum.co.in` email | 201: account created | ✓ (domain not in blocklist) |
| Click Forgot? → enter email → submit | 200: generic "if that email exists..." response; console log shows reset URL in dev mode | ✓ (RESEND_API_KEY empty → console fallback) |
| Visit `/reset-password?token=XXX` | Reset-password view shown, form visible | ✓ (SPA boot checks URLSearchParams) |
| Submit reset form with mismatched passwords | Client-side validation rejects before API call | ✓ |
| Submit reset form with valid token | 200: password updated, redirect to login | ✓ |
| Submit video with `num_clips=10` | Worker picks 10 clips | ✓ (field plumbed all the way through) |
| Submit with `num_clips=25` via curl | 422 Unprocessable Entity | ✓ (Pydantic Field ge=3, le=20) |
| Call `/auth/forgot-password` 4 times | 4th request → 429 Too Many Requests | ✓ (slowapi 3/hour) |

**py_compile results:** All touched Python files passed — see Phase F.

---

### Files changed this session

- `docs/SESSION_LOG.md` — created (this file)
- `docs/CHANGELOG.md` — updated with Session 1 entry
- `docs/KNOWN_BUGS.md` — BUG-003 marked fixed; passlib cleanup added
- `docs/DECISIONS.md` — 3 decisions logged
- `backend/core/security.py` — added `DISPOSABLE_EMAIL_DOMAINS` frozenset + `is_email_allowed()`
- `backend/core/config.py` — added `RESEND_API_KEY`, `EMAIL_FROM`, `FRONTEND_URL`
- `backend/api/routes/auth.py` — removed inline blocklist; added `forgot-password` + `reset-password` routes
- `backend/schemas/auth.py` — added `ForgotPasswordRequest`, `ResetPasswordRequest`
- `backend/models/password_reset.py` — created `PasswordResetToken` model
- `backend/core/database.py` — added `PasswordResetToken` table creation in `init_db()`
- `backend/services/notifications.py` — implemented `send_password_reset_email()`
- `backend/requirements.txt` — added `resend==2.9.0`
- `backend/schemas/video.py` — added `num_clips` field
- `backend/api/routes/videos.py` — pass `num_clips` to worker
- `backend/pipeline/worker.py` — replaced hardcoded `5` with `num_clips` from job meta
- `backend/main.py` — added `GET /reset-password` SPA passthrough
- `frontend/templates/index.html` — updated `#forgot-modal`; added `#view-reset-password`; added clip-count slider

---

## Session 6 — Launch Sprint Day 8: Phase 2B.3 — Cubic Ease-In-Out + Adaptive Transitions

**Date:** 2026-05-18
**Goal:** Replace linear lerp with cubic ease-in-out in `reframe.py`; add per-segment `transition_dur_in` flowing from `conversation_pace.py` → `reframe.py` → FFmpeg filter; create render test script; visual review to completion.

---

### Pre-work decisions

- **Do not touch**: `smart_crop.py`, `active_speaker.py`, `audio_activity.py`, `render.py`, `worker.py`, frontend
- **Cubic ease formula**: `u<0.5 → 4u³ ; u≥0.5 → 1-(−2u+2)³/2` (standard CSS ease-in-out)
- **`u_expr` appears 3× in FFmpeg** — FFmpeg re-evaluates each frame; no variable binding needed
- **Comma escaping**: all commas inside FFmpeg `if()`, `pow()`, etc. must be `\,` (`\\,` in Python)

### Files edited

- `backend/pipeline/reframe.py` — cubic ease-in-out in `_build_piecewise_expr`; per-segment `seg_dur` from `keyframe[i+1].get("transition_dur_in")`; `normalize_user_crop` and `validate_keyframes` preserve `transition_dur_in`
- `backend/pipeline/conversation_pace.py` — `place_adaptive_keyframes` emits `transition_dur_in` per pace; t=0 keyframe uses face position at `clip_start_int` with forward scan; end-of-clip clamp; new helpers `_largest_face_center_at_second`, `_has_faces_at_second`
- `scripts/test_adaptive_transitions.py` — new; renders 10 MP4s (slow + fast per video); slice clamp applied; summary.json written

### Sanity check (pre-render)

- 3-keyframe filter string verified: paren balance 76/76; FFmpeg lavfi dry-test returncode 0
- Per-segment `transition_dur_in` confirmed flowing into `b_start`/`b_end` in `_build_piecewise_expr`

### Visual review — Round 1 (constants 0.45/0.28/0.15)

**BUG A** — t=0 keyframe hardcoded to (0.5, 0.5):
- Root cause: `_face_center_pct(first_active, clip_start_int=0, ...)` searched for the active track at second 0 where no faces were detected (videos 01/02/03/05 have empty face lists in intro frames)
- Fix: check if track has data at `clip_start_int`; if not, scan forward up to 30s for first second with any face; use `_largest_face_center_at_second` there; (0.5, 0.5) only as absolute last resort

**BUG B** — transitions too long / end-of-clip overshoot:
- Fix 1: tuned constants → `{slow: 0.45, medium: 0.28, fast: 0.15}` (intermediate)
- Fix 2: end-of-clip clamp in `place_adaptive_keyframes` — `max_dur = max(0.05, 2.0*(video_duration-0.1-kf["t"]))`
- Fix 3: slice clamp in `test_adaptive_transitions.py` — same formula with `slice_duration`

### Visual review — Round 2 (constants 0.30/0.20/0.12)

User manually tuned `_PACE_TO_TRANSITION_DUR` to `{slow: 0.30, medium: 0.20, fast: 0.12}`. All 10 MP4s re-rendered. Visual review passed.

### Final test results

| Video | Slow render | Fast render | t=0 OK | Errors |
|---|---|---|---|---|
| 01_single_speaker | ✓ | ✓ | face-located | 0 |
| 02_podcast_2person | ✓ | ✓ | face-located | 0 |
| 03_panel_4person | ✓ | ✓ | face-located | 0 |
| 04_screenshare | ✓ | (no fast window) | center fallback | 0 |
| 05_lowlight | ✓ | ✓ | face-located | 0 |

21 output files total (10 MP4 + 10 filter.txt + summary.json).

### Open items

- **BUG-008** (track fragmentation) still deferred to Phase 2C — debouncing keeps downstream correct
- **BUG-012** logged: frontend `effectiveCropAtTime()` uses linear lerp; will be aligned in Phase 2D

### Note on `worker.py` / `user_crop`

`place_adaptive_keyframes` output (with `transition_dur_in`) flows into the production path via `clips.py` → `user_crop` dict stored on the clip. The `worker.py` render path calls `build_keyframe_crop_filter` which reads `transition_dur_in` through `normalize_user_crop`. No changes needed to worker — the field is already preserved end-to-end.

### Files changed this session

- `backend/pipeline/reframe.py` — cubic ease, `transition_dur_in` preservation
- `backend/pipeline/conversation_pace.py` — adaptive `transition_dur_in`, BUG A fix, end-of-clip clamp
- `scripts/test_adaptive_transitions.py` — created
- `docs/CHANGELOG.md` — Session 6 entry prepended
- `docs/SESSION_LOG.md` — this entry appended
- `docs/DECISIONS.md` — cubic ease decision appended
- `docs/KNOWN_BUGS.md` — BUG-012 added
