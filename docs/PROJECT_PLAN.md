# ClipWise — Project Plan

AI-powered video repurposing platform targeting Indian creators. Turns long-form YouTube videos and uploads into viral short-form clips (YouTube Shorts, TikTok, Instagram Reels) with captions, hooks, and platform-aware scoring.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI (Python 3.11) |
| Task Queue | Celery + Redis |
| Database | PostgreSQL 16 (SQLAlchemy ORM) |
| Container | Docker Compose |
| LLM (primary) | Claude Sonnet 4.6 (Anthropic) |
| LLM (fallback) | Groq (llama-3.3-70b) or Gemini |
| Transcription | Whisper via Groq API (or local) |
| Video rendering | FFmpeg |
| Frontend | HTML/CSS/JS SPA served by FastAPI |

---

## Pipeline

```
YouTube URL / File Upload
        ↓
  [download.py] — yt-dlp + SSRF protection
        ↓
  [transcribe.py] — Whisper (word-level timestamps)
        ↓
  [score.py / llm.py] — Two-pass Claude Sonnet 4.6
     Pass 1: full-video scan → 10 candidate moments
     Pass 2: deep scoring → 5 final clips (confidence + platform fit)
     Fallback: deterministic Phase A scorer if LLM fails
        ↓
  [render.py] — FFmpeg: 9:16 crop + captions + smart crop + reframe
        ↓
  Clips stored in /storage/<user_id>/clips/<job_id>/
```

---

## Features Implemented

### Step 11 — File upload + security
- YouTube URL input with SSRF protection (IP allowlist + DNS rebinding check)
- Direct file upload (.mp4/.mov/.mkv/.webm/.avi)
- Magic-byte MIME validation (prevents fake extensions)
- Per-user isolated storage at `/storage/<user_id>/`

### Step 11b — Auth
- JWT auth: access token (15 min) + refresh token (7 days)
- Register / Login / Refresh / Logout endpoints
- Newsletter consent captured at signup

### Step 12 — Captions + Hindi support
- Caption presets: Hormozi, Bold, Minimal, TikTok
- Auto-switches to Noto Sans Devanagari font for Hindi content
- Per-clip scoring breakdown persisted to DB (hook/emotion/completeness/shareability)
- Content-type detection: podcast, tutorial, news, interview, motivational, comedy
- Platform profiles: YouTube Shorts, TikTok, Instagram Reels

### Step 13 — Editable captions
- `original_transcript` (JSONB): Whisper word-level output, immutable
- `edited_transcript` (JSONB): user corrections (text only, reuses original timestamps)
- `needs_rerender` flag: triggers re-render on next export
- `rerender_count`: analytics on how often users edit

### Step 14 — Two-pass LLM clip scoring
- Pass 1: Claude sees full transcript, picks 10 best candidate moments
- Pass 2: deep comparative reasoning, confidence scores, platform-specific tuning
- Safety caps: 80k input tokens max, 4.5k output tokens max
- Cost per video: ~$0.08 (short) to ~$0.21 (4hr podcast)

### Session C — AI-generated hooks/outros
- Pre-hook: 2-line text overlay in first 3 seconds (8 battle-tested patterns)
- Outro: 2-line callback that triggers replay loop (7 patterns)
- Hook accent word: one highlighted word in coral
- Language-aware: English/Hindi/Hinglish variants

### Other pipeline modules
- `smart_crop.py`: auto-detects face/subject position
- `reframe.py`: per-segment keyframe crop with smooth/cut transitions
- `silence.py`: silence detection/removal
- `canvas.py`: background canvas overlay for 9:16 framing
- `watermark.py`: optional branding watermark
- `caption_emphasis.py`: per-word karaoke emphasis styling
- `intro_outro.py`: hook + outro overlay rendering
- `memory.py`: creator memory (tracks creator preferences across jobs)

### Infrastructure
- Rate limiting: slowapi (app-wide + per-route overrides)
- Security headers: CORS lockdown, X-Frame-Options, CSP, HSTS
- Sensitive-data log redaction
- Docker Compose: db + redis + backend + worker
- ngrok support for local development / webhook testing

---

## Goals (clipping intent)

| Goal | What the LLM optimizes for |
|---|---|
| `viral` | Stop-scrolling hooks, emotional peaks, surprise |
| `authority` | Expert framing, confident insights |
| `lead_gen` | Curiosity gaps, incomplete reveals |
| `educational` | Clear lessons, actionable structure |

---

## Planned / Not Yet Started

- Forgot password / password reset flow (backend missing)
- Stripe billing / credit system (credits field exists in User model, not wired)
- Email notifications (notifications.py scaffolded, not live)
- Vimeo + other platform support (commented out in url_security.py)
- Admin dashboard
- Clip analytics (view counts, download counts tracked but no dashboard)
- Mobile app
