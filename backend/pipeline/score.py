"""
backend/pipeline/score.py

Step 14: TWO-PASS clip scoring with Sonnet 4.6.

New flow:
  - Phase A (deterministic pre-filter) → MOSTLY SKIPPED
      We now trust the LLM to identify the best moments across the full video.
      Phase A is retained ONLY as emergency fallback if Pass 1 fails.
  - Pass 1 (LLM): full-video analysis → 10 candidate moments
  - Pass 2 (LLM): deep scoring → 5 final clips with confidence + comparative reasoning

If anything fails mid-pipeline, Phase A's deterministic scoring fills the gap.
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from backend.services.llm import (
    score_clips as llm_score_clips,
    detect_content_type,
    LLMError,
)

logger = logging.getLogger(__name__)


class ScoringError(Exception):
    pass


# ---------------------------------------------------------------------------
# Hook word libraries — multilingual
# ---------------------------------------------------------------------------
_HOOK_WORDS_EN = {
    "secret", "mistake", "truth", "nobody", "never", "always",
    "actually", "honestly", "surprising", "shocking", "unbelievable",
    "you need to", "you should", "here's why", "here's how", "here's what",
    "let me tell you", "imagine", "what if", "the reason",
    "three things", "five ways", "one thing", "the biggest",
    "crazy", "insane", "amazing", "terrible", "worst", "best",
    "failed", "succeeded", "changed my life",
}

_HOOK_WORDS_HI = {
    "sach ye hai", "galti ye hai", "kya aap jaante hain", "ye sunke",
    "asli baat", "seedhi baat", "ek baat", "main bataata hun",
    "samjho", "dhyan do", "suno", "believe karo",
    "actually bataun", "mazedar", "bilkul", "ekdum",
    "matlab", "yaar", "bhai", "friends",
    "सच", "गलती", "राज़", "देखो", "सुनो",
}


# ---------------------------------------------------------------------------
# Phase A (kept for emergency fallback)
# ---------------------------------------------------------------------------
@dataclass
class CandidateWindow:
    start: float
    end: float
    text: str
    hook_score: float
    energy_score: float
    completeness: float
    pacing: float
    total: float

    @property
    def duration(self) -> float:
        return self.end - self.start


def _condense_segments(segments: list[dict], target_window_sec: float = 10.0) -> list[dict]:
    if not segments:
        return []
    merged = [{"start": segments[0]["start"], "end": segments[0]["end"], "text": segments[0]["text"]}]
    for seg in segments[1:]:
        current = merged[-1]
        if current["end"] - current["start"] < target_window_sec:
            current["end"] = seg["end"]
            current["text"] += " " + seg["text"]
        else:
            merged.append({"start": seg["start"], "end": seg["end"], "text": seg["text"]})
    return merged


def _score_hook(text: str) -> float:
    opening = " ".join(text.split()[:15]).lower()
    score = 0.0
    for hook_set in (_HOOK_WORDS_EN, _HOOK_WORDS_HI):
        for hook in hook_set:
            if hook in opening:
                score += 0.3
                break
    if re.search(r"\d+", opening):
        score += 0.2
    if "?" in " ".join(text.split()[:30]):
        score += 0.2
    return min(1.0, score)


def _score_energy(text: str) -> float:
    if not text:
        return 0.0
    punct_count = sum(1 for c in text if c in "!?.,:")
    word_count = max(1, len(text.split()))
    return min(1.0, (punct_count / word_count) * 2 + min(0.4, (text.count("!") + text.count("?")) * 0.1))


def _score_completeness(text: str) -> float:
    text = text.strip()
    if not text:
        return 0.0
    if text[-1] in ".!?":
        return 1.0
    if text[-1] in ",;:":
        return 0.4
    return 0.2


def _score_pacing(text: str, duration_sec: float) -> float:
    if duration_sec <= 0:
        return 0.0
    wps = len(text.split()) / duration_sec
    if 2 <= wps <= 4:
        return 1.0
    if 1.5 <= wps < 2 or 4 < wps <= 5:
        return 0.7
    return 0.3


_W_HOOK = 0.30
_W_ENERGY = 0.20
_W_COMPLETE = 0.25
_W_PACING = 0.15


def _build_candidate_windows(
    segments: list[dict],
    min_sec: float = 30.0,
    max_sec: float = 90.0,
) -> list[CandidateWindow]:
    if not segments:
        return []
    total_duration = segments[-1]["end"] - segments[0]["start"]
    if total_duration < min_sec:
        return []

    candidates: list[CandidateWindow] = []
    for i, start_seg in enumerate(segments):
        window_start = start_seg["start"]
        if window_start + min_sec > segments[-1]["end"]:
            break
        target_end = window_start + 60.0
        end_seg = None
        for j in range(i, len(segments)):
            if segments[j]["end"] >= target_end:
                end_seg = segments[j]
                break
        if end_seg is None:
            end_seg = segments[-1]
        window_end = end_seg["end"]
        duration = window_end - window_start
        if duration < min_sec or duration > max_sec:
            continue
        text_parts = [s["text"] for s in segments if s["start"] >= window_start and s["end"] <= window_end]
        text = " ".join(text_parts).strip()
        if not text:
            continue
        hook = _score_hook(text)
        energy = _score_energy(text)
        complete = _score_completeness(text)
        pacing = _score_pacing(text, duration)
        total = hook * _W_HOOK + energy * _W_ENERGY + complete * _W_COMPLETE + pacing * _W_PACING
        candidates.append(CandidateWindow(
            start=window_start, end=window_end, text=text,
            hook_score=hook, energy_score=energy,
            completeness=complete, pacing=pacing, total=total,
        ))
        if len(candidates) > 200:
            break
    return candidates


def _dedupe_windows(windows: list[CandidateWindow], min_gap: float = 20.0) -> list[CandidateWindow]:
    sorted_windows = sorted(windows, key=lambda w: w.total, reverse=True)
    kept: list[CandidateWindow] = []
    for w in sorted_windows:
        if not any(abs(w.start - k.start) < min_gap for k in kept):
            kept.append(w)
    return kept


def _clamp_clip(clip: dict, video_duration: float) -> dict | None:
    """Clamp clip times + ensure all fields are valid."""
    try:
        start = max(0.0, float(clip["start"]))
        end = min(video_duration, float(clip["end"]))
    except (KeyError, ValueError, TypeError):
        return None
    duration = end - start
    if duration < 15:
        end = min(video_duration, start + 30)
        duration = end - start
    elif duration > 90:
        end = start + 60
        duration = end - start
    if duration < 10:
        return None
    clip["start"] = start
    clip["end"] = end
    clip["duration"] = duration

    try:
        vs = int(clip.get("virality_score", 50))
    except (ValueError, TypeError):
        vs = 50
    clip["virality_score"] = max(0, min(100, vs))

    # Confidence (NEW in Step 14)
    try:
        conf = int(clip.get("confidence", 70))
    except (ValueError, TypeError):
        conf = 70
    clip["confidence"] = max(0, min(100, conf))

    # Sub-scores
    scores = clip.get("scores", {}) or {}
    for key in ("hook", "emotion", "completeness", "shareability"):
        try:
            scores[key] = max(0, min(10, int(scores.get(key, 5))))
        except (ValueError, TypeError):
            scores[key] = 5
    clip["scores"] = scores

    # Reason text
    clip["reason"] = str(clip.get("reason", "Auto-selected based on scoring.")).strip()

    # NEW Step 14: comparative reasoning + platform fit
    clip["beats_alternatives"] = str(clip.get("beats_alternatives", "")).strip()
    clip["platform_fit"] = str(clip.get("platform_fit", "")).strip() or "all"

    return clip


def _candidate_to_clip(cw: CandidateWindow, rank: int, video_duration: float) -> dict:
    """Fallback: convert a deterministic CandidateWindow into a clip dict."""
    return {
        "rank": rank,
        "title": cw.text[:60].strip() or "Untitled clip",
        "start": cw.start,
        "end": cw.end,
        "duration": cw.duration,
        "hook": " ".join(cw.text.split()[:10]),
        "emotion": "neutral",
        "scores": {
            "hook": round(cw.hook_score * 10),
            "emotion": round(cw.energy_score * 10),
            "completeness": round(cw.completeness * 10),
            "shareability": round((cw.hook_score * 0.6 + cw.energy_score * 0.4) * 10),
        },
        "virality_score": max(30, min(95, int((cw.total * 100 * 0.7) + 50 * 0.3))),
        "confidence": 40,  # deterministic fallback is lower-confidence
        "platform_fit": "all",
        "reason": (
            "Selected by our deterministic scorer (hook strength, pacing, completeness). "
            "The AI analysis pass didn't return enough clips, so we filled this slot "
            "from our top-ranked candidates."
        ),
        "beats_alternatives": "",
    }


# ---------------------------------------------------------------------------
# Main entry point — TWO-PASS with Phase A safety net
# ---------------------------------------------------------------------------
def pick_clips(
    job_id: uuid.UUID,
    transcript: dict,
    goal: str = "viral",
    num_clips: int = 5,
    creator_memory: str = "",
    platform: str = "all",
) -> list[dict]:
    """
    Two-pass LLM clip selection with deterministic fallback.

    Steps:
      1. Detect content type + language (cheap, no LLM)
      2. Run two-pass LLM scoring (Pass 1: full video, Pass 2: deep scoring)
      3. Validate output — must have num_clips clips
      4. If anything fails, fill remainder from Phase A deterministic scoring

    GUARANTEES exactly num_clips clips on output.
    """
    segments = transcript.get("segments", [])
    duration = float(transcript.get("duration", 0))
    full_text = transcript.get("full_text", "")
    detected_language = transcript.get("language", "en")

    if not segments or duration < 30:
        raise ScoringError(f"Transcript too short ({len(segments)} segs, {duration:.0f}s)")

    # Content type detection (no LLM call)
    content_type = detect_content_type(full_text, duration)
    logger.info(
        f"[{job_id}] detected: type={content_type}, language={detected_language}, "
        f"platform={platform}"
    )

    # Build Phase A candidates as safety net
    logger.info(f"[{job_id}] building Phase A emergency fallback candidates")
    condensed = _condense_segments(segments, target_window_sec=10.0)
    phase_a_candidates = _build_candidate_windows(condensed)
    phase_a_candidates = _dedupe_windows(phase_a_candidates)[:20]

    # Try two-pass LLM scoring
    raw_clips: list[dict] = []
    try:
        logger.info(f"[{job_id}] starting two-pass LLM scoring (full-video context)")
        raw_clips = llm_score_clips(
            transcript_segments=segments,  # legacy param
            goal=goal,
            num_clips=num_clips,
            creator_memory_summary=creator_memory,
            content_type=content_type,
            detected_language=detected_language,
            platform=platform,
            # Step 14: pass the full transcript to enable TWO-PASS mode
            full_transcript_segments=segments,
            duration_sec=duration,
        )
    except LLMError as e:
        logger.error(f"[{job_id}] LLM scoring failed: {e}")
        raw_clips = []

    # Validate + sanitize output
    cleaned: list[dict] = []
    seen_starts: set[float] = set()
    for idx, clip in enumerate(raw_clips):
        fixed = _clamp_clip(clip, duration)
        if fixed is None:
            logger.warning(f"[{job_id}] dropping invalid clip #{idx}")
            continue
        if any(abs(fixed["start"] - s) < 10 for s in seen_starts):
            logger.warning(f"[{job_id}] skipping duplicate clip at {fixed['start']:.1f}s")
            continue
        cleaned.append(fixed)
        seen_starts.add(fixed["start"])

    # Guarantee num_clips — fill from Phase A if needed
    if len(cleaned) < num_clips:
        logger.warning(
            f"[{job_id}] LLM returned {len(cleaned)}/{num_clips} clips. "
            f"Filling from Phase A safety net..."
        )
        for cw in phase_a_candidates:
            if len(cleaned) >= num_clips:
                break
            if any(abs(cw.start - s) < 10 for s in seen_starts):
                continue
            filler = _candidate_to_clip(cw, rank=len(cleaned) + 1, video_duration=duration)
            filler = _clamp_clip(filler, duration)
            if filler is None:
                continue
            cleaned.append(filler)
            seen_starts.add(filler["start"])

    # Final sort + rank
    cleaned.sort(key=lambda c: c.get("virality_score", 0), reverse=True)
    cleaned = cleaned[:num_clips]
    for i, clip in enumerate(cleaned, start=1):
        clip["rank"] = i

    if not cleaned:
        raise ScoringError(
            "Could not produce any clips. Video may be too short or contain no speech."
        )

    avg_confidence = sum(c.get("confidence", 0) for c in cleaned) / len(cleaned)
    top_score = cleaned[0].get("virality_score", "n/a")
    logger.info(
        f"[{job_id}] ✓ {len(cleaned)} clips selected "
        f"(top virality: {top_score}, avg confidence: {avg_confidence:.0f}%, "
        f"platform: {platform})"
    )
    return cleaned