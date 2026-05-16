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
