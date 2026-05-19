# Architecture & Design Decisions

A log of significant choices made during development — what we picked, what we rejected, and why.

---

## Template

```
## [YYYY-MM-DD] Decision Title

**Decision:** What we decided.

**Alternatives considered:** What else we looked at.

**Reason:** Why we chose this over the alternatives.

**Consequences:** Trade-offs or follow-on implications.
```

---

<!-- Add decisions below this line -->

## [2026-05-12] Email filtering: blocklist-only (no whitelist + regex)

**Decision:** Reject only emails whose domain appears in `DISPOSABLE_EMAIL_DOMAINS` frozenset in `security.py`. Any domain not in the list — including custom company domains like `@eksum.co.in` — is allowed through.

**Alternatives considered:**
- Whitelist of known-good providers (gmail, outlook, yahoo, etc.) — would block all company/custom domains by default, hostile to B2B users.
- Regex validation on domain structure — adds complexity with no real security benefit; determined bad actors can use real-looking domains anyway.
- Third-party API (e.g., Kickbox, ZeroBounce) for real-time disposable detection — adds latency, cost, and an external dependency at signup.

**Reason:** The goal is to reduce spam signups for the private beta, not to be a fortress. Blocking 65 known disposable providers catches the vast majority of throwaway accounts. Whitelisting would break legitimate users with non-standard domains before the product even launches.

**Consequences:** Motivated bad actors with a custom domain bypass the check. Acceptable for a 50-user private beta — revisit if spam becomes a real problem at scale.

---

## [2026-05-12] Transactional email: Resend over SendGrid / Mailgun

**Decision:** Use the Resend SDK (`resend==2.9.0`) for sending password reset emails.

**Alternatives considered:**
- **SendGrid** — industry standard but heavyweight SDK, requires API key with specific sender verification, free tier is 100 emails/day (enough) but UX is older.
- **Mailgun** — solid, but EU data-residency setup is awkward; free tier expires after trial.
- **SMTP directly (smtplib)** — zero dependencies but no deliverability infrastructure, likely to hit spam filters.
- **AWS SES** — cheapest at scale but setup involves domain verification, DKIM/SPF DNS records, and sandbox mode approval; overkill for 50 users.

**Reason:** Resend has the simplest Python SDK (`resend.Emails.send({...})`), modern developer experience, built-in domain verification, and a generous free tier (3,000 emails/month). API key is a single env var. Dev fallback (print to console when `RESEND_API_KEY` is empty) means local dev works with zero setup.

**Consequences:** Resend is a newer provider — less battle-tested than SendGrid at enterprise scale. Acceptable trade-off for a startup at beta stage.

---

## [2026-05-15] Face detection: MediaPipe Tasks API over Haar cascades / face_recognition / YOLO

**Decision:** Replace the existing 4-cascade Haar ensemble in `smart_crop.py` with MediaPipe face detection via the Tasks API (`mediapipe.tasks.python.vision.FaceDetector`). Implementation lives in the new `backend/pipeline/face_detection.py` module.

**Alternatives considered:**
- **Haar cascades (current)** — already implemented; zero extra deps. But high false-positive rate (logos, textures), profile detection is unreliable, requires 3-pass ensemble + skin-tone filter to be usable, still misses tilted/occluded faces.
- **`face_recognition` (dlib-based)** — excellent accuracy, face embedding + comparison built-in. Rejected: requires compiling dlib (`cmake`, build tools, 5+ min Docker build), binaries aren't available for Python 3.11 ARM builds, extremely slow on CPU (~800ms/frame vs ~30ms for MediaPipe).
- **YOLO (Ultralytics YOLOv8-face)** — state of the art, detects faces at all angles + distances. Rejected: largest binary (~6 MB model, 200 MB PyTorch install), GPU strongly recommended for real-time speed, overkill for our single-speaker podcast use case.
- **InsightFace (RetinaFace)** — strong contender, handles occlusion well. Rejected: 150+ MB onnxruntime models, complex setup, no official Python 3.11 wheels for CPU-only.
- **OpenCV DNN + ResNet SSD** — fast and accurate for frontal faces, single dep. Rejected: weaker on profile/tilted faces compared to MediaPipe, less maintained.

**Reason:** MediaPipe Tasks API wins on all axes for our use case:
- **Free** — Google-maintained, Apache 2.0 license
- **Fast** — ~30ms/frame CPU inference (vs ~200ms Haar ensemble, ~800ms dlib)
- **Python 3.11 supported** — official wheels on PyPI (0.10.35 confirmed)
- **No GPU needed** — CPU inference works well for our 1-4 speaker scenarios
- **Profile + frontal** — single model handles multiple face orientations without a separate profile cascade pass
- **Lower false positive rate** — neural model vs Haar's pattern matching on backgrounds, logos, etc.
- **Simple install** — one pip line, model file lazy-downloaded at first use (~224 KB TFLite)

**Consequences:**
- `mediapipe==0.10.35` adds ~79 MB of transitive deps (onnxruntime, opencv-contrib-python, matplotlib, protobuf) to the Docker image. Acceptable.
- MediaPipe Tasks API requires `libgles2` + `libegl1` system packages (OpenGL ES) even in CPU-only mode. Added to Dockerfile.
- The `mediapipe.solutions` namespace from 0.9.x is **completely gone** in 0.10.x — only `mediapipe.tasks` exists. The module was rewritten accordingly.
- Model file is downloaded at first import from Google CDN (~224 KB). Phase 2C will bake the model into the Docker image via Dockerfile ADD.

---

## [2026-05-16] Voice Activity Detection: webrtcvad over silero-vad / pyannote / custom energy threshold

**Decision:** Use `webrtcvad==2.0.10` (Google's WebRTC VAD Python binding) for per-100ms voice/silence classification.

**Alternatives considered:**
- **Custom energy threshold** — already have `compute_audio_energy()`; could just threshold at 0.05. Rejected: energy thresholds fire on music, background noise, and room tone. A talking-head clip with music intro gives 100% "voice" on energy alone. webrtcvad uses a statistical speech model that ignores non-speech sounds.
- **silero-vad** — state-of-the-art neural VAD (~1.8 MB model), PyTorch-based, excellent accuracy, works on 8kHz or 16kHz. Rejected: adds PyTorch as a dependency (~700 MB Docker layer). We're deliberately avoiding PyTorch — faster-whisper uses CTranslate2 specifically for this reason. Not worth the layer cost for a VAD pass.
- **pyannote.audio** — full speaker diarization (VAD + speaker ID in one). Rejected: requires Hugging Face token + model acceptance agreement, complex pipeline setup, and adds several hundred MB of deps. Far more than we need for the binary "is someone talking" classification.
- **ffmpeg silencedetect filter** — already used in `silence.py`. Rejected: designed for detecting long silent gaps (e.g. `d=0.8` means 0.8s minimum silence). Too coarse for 100ms windowing; can't get per-window boolean output from the text-parsing approach.

**Reason:** webrtcvad is a single C extension with no heavy dependencies. It's the same VAD algorithm used in WebRTC (Chrome, Firefox, Zoom) — battle-tested for telephone/podcast audio. 30ms frame size at 16kHz is low-latency and aligns neatly with our 100ms window (3 frames per window, >50% = voice). Aggressiveness 2 is the recommended middle ground for podcast/talking-head content.

**Consequences:** webrtcvad requires exactly 16kHz / 16-bit signed PCM / mono. Our ffmpeg extraction already produces this format (matching `transcribe.py`). The C extension must compile from source — confirmed working with build-essential on Python 3.11.

---

## [2026-05-16] Active speaker: lip movement + IoU tracking over diarization or speaker embedding

**Decision:** Determine the active speaker per second using (1) IoU-based face track IDs across sampled frames, (2) lip Y-coordinate variance as a lip movement proxy, and (3) a 1.5× dominance ratio to declare a clear winner before falling back to largest-face.

**Alternatives considered:**
- **Speaker diarization (pyannote.audio)** — maps audio segments to speaker IDs. Rejected: adds PyTorch + Hugging Face deps (see webrtcvad decision). Also can't tell WHICH face is speaking — just that there are N speakers. Would need to be fused with face tracking anyway.
- **Speaker embedding distance (speechbrain / x-vectors)** — cluster audio segments by speaker embedding, then match clusters to faces. Rejected: same dependency problem, and requires enough audio per speaker to build a reliable embedding.
- **Mouth open/close detection** — landmark-based: measure vertical distance between upper/lower lip keypoints. Rejected: MediaPipe FaceDetector only provides 6 keypoints (eye, eye, nose, mouth-center, ear, ear). No separate upper/lower lip points. Would need FaceLandmarker (468 landmarks) which is a different Tasks API task class.
- **Active Audio Zone** — divide screen into regions, assign audio energy to screen zones, match to face regions. Rejected: only makes sense for static multi-camera setups, not for talking-head/podcast cuts.

**Reason:** Lip movement via Y-coordinate variance is a reliable proxy for "is this face speaking" without any additional model or dependency. Mouth center moves down when speaking; the variance across recent frames captures this signal. The 200 px² normalizer (≈14px std dev = clearly talking) is calibrated for 720p–1080p footage where faces typically occupy 10–30% of frame height.

**Consequences:** The approach degrades for: (1) off-camera speakers (face not visible), (2) speakers who speak without visible jaw movement (rare), (3) very fast cuts where IoU breaks track continuity (high track count, forces `only_face` fallback). For single-speaker talking-head content the `only_face` path handles it perfectly. Multi-speaker content with active lip movement works well.

---

## [2026-05-16] Adaptive sample_fps: duration-based (4/2/1) over fixed frame cap

**Decision:** `track_faces_across_frames()` samples at 4fps for videos ≤300s, 2fps for 300–900s, 1fps for >900s.

**Alternatives considered:**
- **Fixed cap (1200 frames max)** — initial proposal; truncates long videos so the tail is never analyzed. A 20-minute podcast would only cover the first 5 minutes at 4fps. Rejected by user.
- **Fixed 1fps always** — covers full video for any length but misses inter-second speaker switches for short clips.
- **Frame count-based decimation** — sample every N frames until total = target count. Equivalent to a duration-based rate, just expressed differently.

**Reason:** User explicitly changed from fixed-cap to duration-based adaptive: short content gets high temporal resolution for smooth lip tracking; long content accepts lower resolution to keep run time reasonable while still covering the full timeline. Covers content from 30-second clips to feature-length (1hr+) videos.

**Consequences:** A 30-minute video at 1fps = 1800 frames = ~36 minutes of face tracking. For Phase 2B production use, the output of `track_faces_across_frames()` should be cached after first computation.

---

## [2026-05-12] Password reset token expiry: 15 minutes

**Decision:** Password reset tokens expire 15 minutes after issuance. Tokens are single-use and hashed (SHA-256) in the database — raw token only ever exists in the email link.

**Alternatives considered:**
- **1 hour** — common default but gives a wider attack window if the email is compromised or forwarded.
- **24 hours** — maximises user convenience but unacceptable security posture for a credential reset action.
- **5 minutes** — tighter window, but users on slow email servers or who get distracted may find links expired before they act.

**Reason:** 15 minutes is the industry consensus for password reset links (NIST SP 800-63B spirit, OWASP recommendation). Short enough to limit exposure if an email is intercepted; long enough that a user who opens the email immediately has no trouble. Single-use invalidation means replaying a link after first use does nothing.

**Consequences:** Users who don't click within 15 minutes must request a new link. The `forgot-password` route invalidates previous unexpired tokens before creating a new one, so users can self-serve a fresh link immediately without confusion.

---

## [2026-05-16] Pace classification: debounced speaker-change events over raw track-id diffs

**Decision:** `detect_speaker_changes()` uses a two-state debounce (current_stable + candidate + candidate_start_sec) requiring a new track_id to hold for ≥ 2 consecutive seconds before firing a change event. The rolling window pace classifier (`classify_pace_window`, 10s window) operates over these debounced events, not raw per-second track_id diffs.

**Alternatives considered:**
- **Raw per-second diff** — compare `timeline[i]["active_track_id"] != timeline[i-1]["active_track_id"]` and count transitions. Rejected: BUG-008 (track fragmentation on camera cuts) creates a new track_id per cut; a single-speaker TEDx talk with cuts every 5s produces ~96 raw transitions over 480s. Pace would classify as "fast" constantly.
- **Sliding window count only (no debounce)** — count unique track_ids in a 10s window. Still fragmentation-sensitive; a window spanning 3 camera cuts has 3 different track_ids even with one physical speaker.
- **Speaker embedding clustering** — group track_ids by audio/visual similarity before counting changes. Correct approach for Phase 2C, but adds a dependency and complexity. BUG-008 needs proper fixing first.
- **Fixed gap filter** — only count a change if the previous different track_id was ≥ N seconds ago. Equivalent to debouncing but one-sided (doesn't require candidate to *hold*). Rejected: a one-second interjection would still fire; the candidate-hold approach requires the new speaker to be consistently present.

**Reason:** The 2-second hold requirement filters single-frame detection noise and mitigates some cut-based fragmentation (cuts followed immediately by another cut don't qualify). It can't eliminate cut-based fragmentation entirely — a speaker at a cut position who genuinely holds their new track_id for 2+ seconds will still generate an event — but that's the correct boundary case (Phase 2C's BUG-008 fix will solve the underlying tracking).

**Consequences:** For single-speaker videos with camera cuts (01_single_speaker: 346 tracks → 107 debounced events), change counts are still elevated but correct given the fragmentation level. Multi-speaker videos with actual conversational turns (02_podcast_2person) correctly alternate x_pct ~0.33 ↔ ~0.67. Pace counts inflate proportionally to cut density; "fast" pace only fires when changes exceed 6 per 10s window, which does not happen in current test set. Phase 2C (BUG-008 fix) will dramatically reduce spurious change events.

Additional consequence (logged 2026-05-16, post visual review): On cut-heavy single-speaker footage, debounced change events count camera-cut-induced track_id transitions, not semantic speaker turns. This is acceptable because the downstream uses (keyframe re-centering, transition speed adaptation) treat cuts and real speaker changes identically — both warrant a new keyframe. The pace classifier's "change count" must NOT be interpreted as "speaker turn count" until BUG-008 (face track fragmentation) is fixed in Phase 2C. See BUG-011.

---

## [2026-05-18] Crop pan easing: cubic ease-in-out over linear lerp

**Decision:** `_build_piecewise_expr` in `reframe.py` interpolates between crop positions using cubic ease-in-out (`u<0.5 → 4u³ ; u≥0.5 → 1-(−2u+2)³/2`), with per-segment pan duration read from the keyframe's optional `transition_dur_in` field.

**Alternatives considered:**
- **Linear lerp (previous implementation)** — `x_curr + (x_next - x_curr) * u`. Simple but produces mechanical, robotic motion that accelerates and decelerates abruptly at boundary frames.
- **Sine ease-in-out** — smoother feel, mathematically equivalent for most content but not natively expressible in FFmpeg's filter expression language without trig functions. FFmpeg's `sin()` is available but produces very similar results to cubic for short pans. Rejected in favor of simpler polynomial.
- **Ease-in only / ease-out only** — asymmetric easing would make pans feel like they snap to or away from the subject. Wrong for camera-reframe semantics where both start and end should be gentle.
- **Fixed global `transition_dur`** — initial implementation used a single constant for all segments. Rejected: fast-paced conversation segments with 0.12s pans feel punchy and appropriate; slow segments with 0.30s pans feel deliberate. A single value would either be too slow for fast content or too fast for slow content.

**Reason:** Cubic ease-in-out is the standard CSS/animation easing curve. It produces a "camera operator" feel where the pan accelerates gently out of the resting frame and decelerates into the next one. FFmpeg's filter expression language can evaluate it per-frame without any external function calls — the `u_expr` string is duplicated 3× in the ease expression (FFmpeg has no variable binding), which is fine since each evaluation is a few arithmetic ops. Per-segment `transition_dur_in` flows from `conversation_pace.py` (derived from pace classification: 0.30s slow / 0.20s medium / 0.12s fast) all the way through `normalize_user_crop`, `validate_keyframes`, and into `_build_piecewise_expr`.

**Consequences:**
- Pan duration values (0.30/0.20/0.12) were tuned through two rounds of visual review. They are the right starting point but may need per-content-type adjustment in Phase 2D.
- Frontend `effectiveCropAtTime()` still uses linear lerp for preview rendering. The preview will not match the rendered output exactly. Logged as BUG-012; Phase 2D scope.
- `transition_dur_in` is an optional keyframe field — absence falls back to global `transition_dur` (default 0.4s). Legacy v1 crops and manually-placed keyframes without the field continue to work correctly.

---

## [2026-05-18] Worker auto-render generates adaptive keyframes by default

**Decision:** `worker.py` runs the Phase 2B active-speaker pipeline ONCE per source video (between `pick_clips` and render), generates per-clip keyframes via `place_adaptive_keyframes`, and attaches a `user_crop` v2 dict to each clip dict. `render_all_clips` passes `user_crop` through to `render_one_clip`. The `user_crop` is also persisted to `clip.meta` so the Reframe modal can read auto-generated keyframes when the user opens it.

**Alternatives considered:**
- **Per-clip pipeline analysis** — run `track_faces_across_frames` once per clip. Rejected: 5× redundant audio extraction + face tracking work; also loses cross-boundary smoothness (face tracking state is lost between clips).
- **Lazy/on-demand keyframe generation at render time** — generate inside `render_one_clip`. Rejected: clips render in parallel (threadpool); would 5× duplicate analysis and introduce race conditions on shared tracking state.
- **Manual-only via `/auto-keyframes` endpoint** — skip worker integration entirely. Rejected: defeats the purpose of building the pipeline. Auto-keyframes should be the default, with manual override as an enhancement.

**Reason:** Full-video analysis once is faster wall-clock than per-clip, and face tracking state accumulates correctly across the full timeline. Attaching `user_crop` to the clip dict is the natural integration point — all downstream functions already accept it. The try/except wrapper means adaptive keyframes are an enhancement, not a hard requirement; any pipeline failure is logged and clips fall back to static smart_crop.

**Consequences:** `process_video` task now does meaningful CPU work before render (~30–60s for full-video face tracking at adaptive `sample_fps`). For a 1-hour podcast at 1fps, this is ~3600 frames of inference. This is acceptable given the quality improvement; Phase 2D may add caching to avoid re-analysis on re-render.

---

## [2026-05-18] BUG-008 fix: spatial-region grid as IoU fallback for cut-robust tracking

**Decision:** `track_faces_across_frames` retains IoU matching as primary (handles smooth motion), and adds a 4×4 spatial-grid region memory as fallback when IoU < 0.5. The region memory (`region_track_history`) maps each grid cell to `(track_id, last_seen_timestamp)` with a 3.0s timeout. A face that appears in a region where a track was seen within 3s keeps that track ID even if IoU = 0.

**Alternatives considered:**
- **Pure IoU only** — the pre-fix state. Fails on every camera cut (IoU = 0 between frames). Produced 346 tracks for a single-speaker video.
- **Face embedding similarity (FaceNet / ArcFace)** — ground truth identity matching. Rejected: adds a ~50 MB model, GPU strongly desirable, adds significant inference time per face. Overkill for our use case.
- **Optical flow (Lucas-Kanade)** — track keypoints across frames. Rejected: fails on hard cuts (no optical flow at the cut boundary), slow on CPU for per-frame tracking.
- **3×3 grid** — too coarse. Two speakers in a side-by-side podcast layout often land in the same cell.
- **5×5 grid** — too fine. Normal head movements (±50px in 1080p) can shift a face between adjacent cells, creating false new track IDs.
- **4×4 grid** — chosen. Aligns with typical 2-person side-by-side and 4-person panel layouts; head movements within a cell don't trigger false splits.
- **Timeout 1s** — too short. Cut-and-return patterns (presenter + slides + presenter) often show 2–3s of non-face content between appearances.
- **Timeout 5s** — too long. A different speaker could occupy the same region for 5s and inherit the wrong track ID.
- **Timeout 3.0s** — chosen based on empirical review of BUG-008 test fixtures (01_single_speaker cuts average ~1.5s, so 3s covers a cut + brief cutaway without spanning long scene changes).

**Reason:** Spatial region is a cheap, dependency-free proxy for face identity that covers the dominant failure mode (hard cuts). IoU handles the 90% case (smooth motion); spatial handles the 10% case (cuts). No new dependencies.

**Consequences:** Track ID count drops significantly on cut-heavy content. False positives possible if two different speakers occupy the same screen region within 3s (e.g. presenter walks past a fixed camera and someone else steps in) — tolerable, since adaptive transitions smooth any resulting pan. BUG-011 (change event counts) will improve naturally as track fragmentation decreases.

---

## [2026-05-19] Email lookup: server-side lowercase normalize over CITEXT / client-side block

**Decision:** All three auth endpoints (`register`, `login`, `forgot-password`) normalize the submitted email via `payload.email.strip().lower()` before any DB query. Existing rows backfilled via `UPDATE users SET email = LOWER(email)`.

**Alternatives considered:**
- **Block uppercase typing client-side** — user's initial request. Rejected: breaks password managers (which autofill as saved, including mixed case), paste from email clients, mobile keyboard autocomplete. Users with pre-existing mixed-case saved credentials silently lose access.
- **Case-insensitive SQL** (`func.lower(User.email) == normalized`) — equivalent to the chosen approach but skips the `users.email` B-tree index on every lookup. Rejected at scale; single-equality comparison on a pre-lowercased column stays indexed.
- **PostgreSQL `CITEXT` extension** — transparent case-insensitive column type. Rejected: adds an extension dependency and a schema migration. Not worth the complexity for a simple normalization that handles all the same cases.
- **Unique constraint on lowercased column** — add a shadow `email_lower` column. Rejected: two columns to keep in sync, no meaningful benefit over just normalizing at write time.

**Reason:** Lowercase normalization before storage is the universal standard (Google, Slack, GitHub, Stripe). All emails are stored lowercase; all queries are lowercase; no case mismatch is possible. Defense-in-depth: client also lowercases before submitting, but server-side normalization is the authoritative path. One-time data migration handles legacy rows.

**Consequences:** Users cannot register two accounts differing only in letter case (e.g., `Alice@example.com` vs `alice@example.com`). Acceptable — RFC 5321 permits it but no real provider uses it. If a user somehow has a truly case-sensitive email provider (essentially nonexistent in practice), they cannot use Clipsy.

---

## [2026-05-19] Forgot-password response: enumeration-safe generic message

**Decision:** `POST /auth/forgot-password` always returns HTTP 200 with `{"message": "If that email is registered, a reset link is on its way."}`, regardless of whether the email exists in the DB. Backend logs the outcome (matched / no-match) but never surfaces it to the client.

**Alternatives considered:**
- **"This email isn't in our system"** on no-match — user's initial request. Rejected: email enumeration is one of the most common account reconnaissance steps. An attacker submitting a list of emails can build a confirmed-user list by reading the response. This is the threat OWASP lists under A07 (Identification and Authentication Failures) and A02 (Cryptographic Failures via information disclosure).
- **Different HTTP status codes** (404 vs 200) — same enumeration problem, just at the status layer.
- **Captcha after N attempts** — rate limiting already covers this (3/hour per IP). Captcha would add friction without eliminating enumeration since an attacker can still test 3 emails/hour per IP.

**Reason:** Email enumeration prevention is standard practice across all reputable services (Google, GitHub, Apple, Stripe). The same 200 + generic message regardless of outcome is the OWASP-recommended pattern. The 3/hour rate limit prevents rapid scraping.

**Consequences:** A legitimate user who mistypes their email gets no signal it's wrong. Mitigated by the `#forgot-step-2` success message hint: *"Make sure you used the same email you signed up with, and check your spam folder."* The rate limit (3/hour) also means a user who fat-fingers their email can quickly retry without being blocked.
