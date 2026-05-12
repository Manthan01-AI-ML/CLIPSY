"""
backend/services/llm.py

LLM abstraction — Step 14: TWO-PASS clip selection with Sonnet 4.6.

Pass 1 (analyze_full_video):
  - Sees the ENTIRE transcript as one document
  - Identifies the 10 best moments across the full video
  - Understands topic arcs, emotional peaks, standalone-ness
  - Returns 10 candidate ranges with rich per-candidate analysis

Pass 2 (score_candidates_deep):
  - Deep reasoning on those 10 candidates
  - Comparative: "this beats that because..."
  - Platform-aware: tunes for YouTube Shorts / TikTok / Reels
  - Diversity check: avoids redundant clips
  - Returns 5 final clips with confidence + detailed reasoning

Safety caps retained for both passes.
"""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from backend.core.config import settings

logger = logging.getLogger(__name__)


# =============================================================================
# Providers
# =============================================================================
class LLMProvider(ABC):
    @abstractmethod
    def complete_json(self, system: str, user: str, max_tokens: int = 2048) -> dict: ...
    @property
    @abstractmethod
    def name(self) -> str: ...


class LLMError(Exception):
    pass


class GeminiProvider(LLMProvider):
    name = "gemini"
    def __init__(self) -> None:
        if not settings.GEMINI_API_KEY:
            raise LLMError("GEMINI_API_KEY not set in .env")
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise LLMError("google-generativeai not installed") from e
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self._genai = genai
        self._model_name = settings.GEMINI_MODEL

    def complete_json(self, system: str, user: str, max_tokens: int = 2048) -> dict:
        model = self._genai.GenerativeModel(
            model_name=self._model_name,
            system_instruction=system,
            generation_config={
                "temperature": 0.7,
                "max_output_tokens": max_tokens,
                "response_mime_type": "application/json",
            },
        )
        try:
            response = model.generate_content(user)
            text = response.text
        except Exception as e:
            raise LLMError(f"Gemini API call failed: {e}") from e
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"Gemini returned invalid JSON: {text[:200]}") from e


class GroqProvider(LLMProvider):
    name = "groq"
    def __init__(self) -> None:
        if not settings.GROQ_API_KEY:
            raise LLMError("GROQ_API_KEY not set in .env")
        try:
            from groq import Groq
        except ImportError as e:
            raise LLMError("groq not installed") from e
        self._client = Groq(api_key=settings.GROQ_API_KEY)
        self._model = settings.GROQ_MODEL

    def complete_json(self, system: str, user: str, max_tokens: int = 2048) -> dict:
        try:
            resp = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system + "\n\nReturn only valid JSON. No preamble, no markdown."},
                    {"role": "user", "content": user},
                ],
                temperature=0.7,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            )
            text = resp.choices[0].message.content or ""
        except Exception as e:
            raise LLMError(f"Groq API call failed: {e}") from e
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"Groq returned invalid JSON: {text[:200]}") from e


class ClaudeProvider(LLMProvider):
    name = "claude"

    # SAFETY CAPS — prevents accidental cost blowup.
    # Step 14: For Sonnet 4.6 (~$3 input / $15 output per M tok),
    # we scale these because two-pass needs more headroom.
    # Full-video analysis of a 4-hour podcast = ~30,000 input tokens = $0.09.
    MAX_INPUT_TOKENS = 80_000       # worst case: ~$0.24 input per call
    MAX_OUTPUT_TOKENS = 4_500       # worst case: ~$0.068 output per call

    def __init__(self) -> None:
        if not settings.ANTHROPIC_API_KEY:
            raise LLMError("ANTHROPIC_API_KEY not set in .env")
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise LLMError("anthropic not installed") from e
        self._client = Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._model = settings.CLAUDE_MODEL

    def complete_json(self, system: str, user: str, max_tokens: int = 2048) -> dict:
        # Cap output tokens
        max_tokens = min(max_tokens, self.MAX_OUTPUT_TOKENS)

        # Estimate input tokens (1 tok ≈ 4 chars)
        estimated_input = (len(system) + len(user)) // 4
        if estimated_input > self.MAX_INPUT_TOKENS:
            raise LLMError(
                f"Prompt too large ({estimated_input} tokens estimated > {self.MAX_INPUT_TOKENS} cap). "
                f"Refusing to send. This usually means the video is way too long."
            )

        try:
            msg = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system + "\n\nRespond with valid JSON only. No preamble, no markdown.",
                messages=[{"role": "user", "content": user}],
            )
            # Log actual token usage + cost estimate
            if hasattr(msg, "usage"):
                in_t = getattr(msg.usage, "input_tokens", 0)
                out_t = getattr(msg.usage, "output_tokens", 0)
                # Sonnet pricing: $3 input / $15 output per MTok
                cost = (in_t / 1_000_000 * 3.00) + (out_t / 1_000_000 * 15.00)
                logger.info(
                    f"Claude({self._model}) call: in={in_t} out={out_t} tokens, "
                    f"cost≈${cost:.4f}"
                )
            text = "".join(
                block.text for block in msg.content if getattr(block, "type", "") == "text"
            )
        except Exception as e:
            raise LLMError(f"Claude API call failed: {e}") from e
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError(f"Claude returned invalid JSON: {text[:200]}") from e


class MockProvider(LLMProvider):
    name = "mock"
    def complete_json(self, system: str, user: str, max_tokens: int = 2048) -> dict:
        return {"clips": [{"rank": 1, "title": "Mock", "start": 10, "end": 55,
                           "hook": "mock", "reason": "mock", "emotion": "neutral",
                           "virality_score": 50, "confidence": 70,
                           "scores": {"hook": 5, "emotion": 5, "completeness": 5, "shareability": 5}}]}


_PROVIDERS: dict[str, type[LLMProvider]] = {
    "gemini": GeminiProvider, "groq": GroqProvider,
    "claude": ClaudeProvider, "mock": MockProvider,
}


def get_provider(name: str | None = None) -> LLMProvider:
    name = name or settings.LLM_PROVIDER
    if name not in _PROVIDERS:
        raise LLMError(f"Unknown provider: {name}")
    return _PROVIDERS[name]()


def complete_json_with_fallback(system: str, user: str, max_tokens: int = 3000) -> dict:
    providers = [settings.LLM_PROVIDER]
    if settings.LLM_FALLBACK_PROVIDER and settings.LLM_FALLBACK_PROVIDER != settings.LLM_PROVIDER:
        providers.append(settings.LLM_FALLBACK_PROVIDER)
    last_error: Exception | None = None
    for p in providers:
        try:
            provider = get_provider(p)
            logger.info(f"Calling LLM provider: {provider.name}")
            return provider.complete_json(system, user, max_tokens)
        except LLMError as e:
            logger.warning(f"Provider {p} failed: {e}")
            last_error = e
            continue
    raise LLMError(f"All LLM providers failed. Last error: {last_error}")


# =============================================================================
# CONTENT-TYPE DETECTION (cheap, no LLM call)
# =============================================================================
_TYPE_SIGNATURES = {
    "podcast": {
        "must_have_any": [
            "welcome to", "today's guest", "host", "let's get into",
            "check out", "subscribe", "links in the description",
            "this episode", "episode", "aaj ke episode",
        ],
        "boost": ["interview", "conversation", "chat with", "joining me",
                  "tell us about", "what's your take", "you mentioned"],
    },
    "tutorial": {
        "must_have_any": [
            "how to", "step 1", "step one", "first,", "next,", "let me show",
            "in this video", "tutorial", "lesson", "learn",
            "aaj main batauga", "aaj seekhenge",
        ],
        "boost": ["here's how", "the way to do this", "i'll walk you through",
                  "demonstration", "example", "this is important"],
    },
    "news": {
        "must_have_any": [
            "breaking", "reports", "according to", "sources say",
            "today we're", "officials", "announced", "statement",
            "reporter", "correspondent", "news mein",
        ],
        "boost": ["confirmed", "denied", "allegedly", "investigation",
                  "developments", "latest"],
    },
    "interview": {
        "must_have_any": [
            "so tell me", "what do you think", "how did you", "walk us through",
            "my guest", "joining us", "let me ask",
        ],
        "boost": ["question", "answer", "in your experience", "would you say"],
    },
    "motivational": {
        "must_have_any": [
            "don't give up", "believe in yourself", "you can do", "never quit",
            "success is", "achieve your", "mindset", "discipline",
            "mehnat karo", "kabhi mat chodo",
        ],
        "boost": ["greatness", "destiny", "legend", "champion", "hustle"],
    },
    "comedy": {
        "must_have_any": [
            "funny thing is", "joke", "punchline", "hilarious",
            "laugh", "lmao", "comedy", "mazak",
        ],
        "boost": ["so ridiculous", "cracks me up", "you can't make this up"],
    },
}


def detect_content_type(full_text: str, duration_sec: float) -> str:
    """Classify content type from transcript features. No LLM call."""
    if not full_text:
        return "general"

    text_lower = full_text.lower()
    text_sample = text_lower[:3000]

    scores: dict[str, int] = {}
    for content_type, sigs in _TYPE_SIGNATURES.items():
        score = 0
        for phrase in sigs["must_have_any"]:
            if phrase in text_sample:
                score += 3
        for phrase in sigs["boost"]:
            if phrase in text_sample:
                score += 1
        if score > 0:
            scores[content_type] = score

    if not scores:
        if duration_sec > 1800:
            return "podcast"
        if duration_sec < 300:
            return "news"
        return "general"

    return max(scores.items(), key=lambda kv: kv[1])[0]


# =============================================================================
# PLATFORM-SPECIFIC CLIP STRATEGIES
# =============================================================================
_PLATFORM_PROFILES = {
    "youtube_shorts": {
        "ideal_length": "45-75 seconds",
        "hook_critical_seconds": 5,
        "pacing": "Structured: strong hook → build → payoff. Viewers watch longer than TikTok.",
        "tone": "Educational or story-driven. Viewers come looking for substance.",
        "ending": "Clear resolution or quotable closer. Many viewers watch to end.",
    },
    "tiktok": {
        "ideal_length": "20-45 seconds",
        "hook_critical_seconds": 3,
        "pacing": "Fast, punchy. No setup. Deliver the value immediately.",
        "tone": "Casual, authentic, entertainment-first. High energy.",
        "ending": "Snappy punchline or hook for comments ('what would you do?').",
    },
    "instagram_reels": {
        "ideal_length": "30-60 seconds",
        "hook_critical_seconds": 3,
        "pacing": "Visual and aesthetic. Reels viewers want polish.",
        "tone": "Polished, relatable. Less raw than TikTok.",
        "ending": "Satisfying close. Reel should feel complete on loop.",
    },
    "all": {
        "ideal_length": "30-60 seconds",
        "hook_critical_seconds": 3,
        "pacing": "Balanced — hook fast, deliver value, end clean.",
        "tone": "Adaptable across platforms.",
        "ending": "Complete thought with some takeaway value.",
    },
}


# =============================================================================
# CONTENT-TYPE STRATEGIES (retained, slightly refined)
# =============================================================================
_CLIPPING_STRATEGIES = {
    "podcast": (
        "Best clips are STORY SNIPPETS, contrarian takes, 'aha' moments from the guest. "
        "Avoid intros, sponsor reads, rambling tangents. "
        "A great podcast clip makes viewers think 'I need to hear this whole episode.'"
    ),
    "tutorial": (
        "Best clips are SELF-CONTAINED TIPS. Avoid 'step 1 of 10' fragments. "
        "Strong clips show a BEFORE→AFTER or a surprising technique. "
        "Look for: 'here's the trick', 'the secret is', 'most people miss this'."
    ),
    "news": (
        "Deliver the KEY FACT or REVELATION tight. "
        "Avoid preamble or 'more after the break'. Prefer concrete numbers, names, events."
    ),
    "interview": (
        "Best clips are the GUEST'S strongest answer, not the interviewer's question. "
        "Emotional beats, surprising admissions, controversial opinions. "
        "Prefer clips where the guest pauses, reflects, then says something real."
    ),
    "motivational": (
        "Best clips END with a quotable, shareable line. "
        "Build on emotion — struggle, breakthrough, resolution. "
        "Avoid generic affirmations. Prefer specific, visceral wisdom."
    ),
    "comedy": (
        "Start at setup, end RIGHT AFTER the punchline. "
        "Best clips make viewers laugh within first 10 seconds. "
        "Prefer tight bits over rambling anecdotes."
    ),
    "general": (
        "Look for moments of peak emotion, insight, or surprise. "
        "Self-contained clips that work without the full video. "
        "Prefer moments that trigger curiosity, recognition, or 'I didn't know that.'"
    ),
}


def _language_directive(detected_language: str) -> str:
    lang = (detected_language or "").lower()
    if lang in ("hi", "hin", "hindi"):
        return (
            "LANGUAGE: Hindi content detected. "
            "Titles should be in Hindi if the clip text is Hindi (use Devanagari script). "
            "Reasoning should be in English (easier for the creator to read). "
            "Hindi hook patterns: 'sach ye hai', 'asli baat', 'main bataata hun', "
            "emotional storytelling."
        )
    if lang in ("en", "eng", "english"):
        return (
            "LANGUAGE: English content detected. "
            "Focus on strong English hooks: questions, surprising claims, specific numbers."
        )
    return (
        "LANGUAGE: Mixed/code-switching content (likely Hinglish) detected. "
        "Both Hindi and English creators use this. "
        "Titles should match whatever script dominates the clip. Reasoning in English."
    )


# =============================================================================
# PASS 1: Full-video analysis → identifies top candidate moments
# =============================================================================
def analyze_full_video(
    full_transcript_segments: list[dict[str, Any]],
    content_type: str,
    detected_language: str,
    platform: str,
    duration_sec: float,
    num_candidates: int = 10,
) -> list[dict]:
    """
    PASS 1: Analyze the ENTIRE video and identify top N candidate moments.

    Returns list of candidate dicts:
      {
        "start": float, "end": float,
        "title": str,
        "topic": str,        # the theme of this moment
        "why_notable": str,  # one-sentence hook description
        "initial_score": int # 0-100, rough first impression
      }
    """
    strategy = _CLIPPING_STRATEGIES.get(content_type, _CLIPPING_STRATEGIES["general"])
    platform_profile = _PLATFORM_PROFILES.get(platform, _PLATFORM_PROFILES["all"])
    lang_directive = _language_directive(detected_language)

    # Build a compact full-video representation: time-anchored transcript chunks
    # For very long videos (>1hr), we downsample segments to keep within token budget
    segments_to_send = full_transcript_segments
    if duration_sec > 3600:
        # For 1hr+ videos, merge every 2-3 Whisper segments into ~15-20s chunks
        merged = []
        buffer = None
        for s in full_transcript_segments:
            if buffer is None:
                buffer = {"start": s["start"], "end": s["end"], "text": s["text"]}
            elif s["end"] - buffer["start"] < 20:
                buffer["end"] = s["end"]
                buffer["text"] += " " + s["text"]
            else:
                merged.append(buffer)
                buffer = {"start": s["start"], "end": s["end"], "text": s["text"]}
        if buffer:
            merged.append(buffer)
        segments_to_send = merged

    transcript_text = "\n".join(
        f"[{seg['start']:.0f}s-{seg['end']:.0f}s] {seg['text']}"
        for seg in segments_to_send
    )

    system = (
        "You are an elite short-form video strategist for Indian creators. "
        "You've studied what makes clips go viral on YouTube Shorts, TikTok, and Reels. "
        "You understand hook strength, emotional pacing, curiosity gaps, story arcs, and "
        "the specific differences between platforms.\n\n"
        "YOUR JOB in this pass: Read the FULL transcript of a video and identify the "
        f"TOP {num_candidates} moments that could become great short-form clips. "
        "Think of yourself like a documentary editor scanning raw footage — you're looking "
        "for the beats where something genuinely interesting happens: a surprising claim, "
        "an emotional peak, a quotable insight, a contrarian take, a story with a payoff.\n\n"
        f"IMPORTANT: Return EXACTLY {num_candidates} candidates (or fewer only if the "
        "video genuinely doesn't have enough moments, e.g., very short video)."
    )

    user = f"""{lang_directive}

CONTENT TYPE: {content_type.upper()}
{strategy}

TARGET PLATFORM: {platform.upper()}
- Ideal length: {platform_profile['ideal_length']}
- Hook must land in: first {platform_profile['hook_critical_seconds']} seconds
- Pacing: {platform_profile['pacing']}
- Ending style: {platform_profile['ending']}

VIDEO DURATION: {duration_sec:.0f} seconds

Read the full transcript below. Identify the top {num_candidates} candidate moments.
For each, define clean START and END timestamps — these should be self-contained clips
that would make sense to someone who never saw the full video.

Return JSON:
{{
  "candidates": [
    {{
      "start": 145.3,
      "end": 208.7,
      "title": "Punchy title describing the clip (max 60 chars)",
      "topic": "The core topic of this moment (e.g., 'discipline vs motivation')",
      "why_notable": "Single sentence: what makes THIS moment stand out from the rest of the video",
      "initial_score": 75
    }}
  ]
}}

Rules for selecting candidates:
1. Each candidate must be self-contained — makes sense without the full video context
2. Diverse topics — don't pick 5 candidates all about the same thing
3. Length should align with the target platform's sweet spot
4. Skip intros, sponsor reads, transitions, and rambling tangents
5. START must be at a natural beat (not mid-sentence)
6. END should land on a complete thought, ideally a quotable line

TRANSCRIPT:
{transcript_text}
"""

    # Sonnet needs more output budget for rich analysis of 10 candidates
    response = complete_json_with_fallback(system, user, max_tokens=3500)
    candidates = response.get("candidates", [])
    if not isinstance(candidates, list):
        raise LLMError(f"Pass 1: Expected 'candidates' as list, got: {type(candidates)}")
    return candidates


# =============================================================================
# PASS 2: Deep scoring with comparative reasoning + confidence + platform tuning
# =============================================================================
def score_candidates_deep(
    candidates: list[dict[str, Any]],
    full_transcript_segments: list[dict[str, Any]],
    content_type: str,
    detected_language: str,
    platform: str,
    goal: str = "viral",
    num_clips: int = 5,
    creator_memory_summary: str = "",
) -> list[dict]:
    """
    PASS 2: Deep reasoning on Pass 1's candidates. Comparative, platform-tuned.

    Returns exactly num_clips clips with full schema:
      {
        rank, title, start, end, hook, emotion,
        virality_score (0-100),
        confidence (0-100),          # NEW: how sure we are this works
        platform_fit,                 # NEW: which platform it's best for
        scores: {hook, emotion, completeness, shareability},
        reason: "3-5 sentence detailed explanation",
        beats_alternatives: "why this was chosen over similar candidates"
      }
    """
    goal_prompts = {
        "viral": (
            "Which clips would make someone STOP SCROLLING on Reels/Shorts/TikTok? "
            "Strong hooks in first 3 seconds, emotional peaks, surprise."
        ),
        "authority": (
            "Which clips make the speaker sound most EXPERT? "
            "Confident statements, clear insights, authoritative framing."
        ),
        "lead_gen": (
            "Which clips create CURIOSITY GAPS that drive clicks? "
            "Incomplete reveals that make viewers want more."
        ),
        "educational": (
            "Which clips deliver the CLEAREST lesson in 60 seconds? "
            "Complete concepts, actionable insights, clear structure."
        ),
    }
    goal_framing = goal_prompts.get(goal, goal_prompts["viral"])
    strategy = _CLIPPING_STRATEGIES.get(content_type, _CLIPPING_STRATEGIES["general"])
    platform_profile = _PLATFORM_PROFILES.get(platform, _PLATFORM_PROFILES["all"])
    lang_directive = _language_directive(detected_language)

    # For each candidate, pull the full transcript segments within its time range
    # so Pass 2 has the actual text to reason about
    enriched_candidates = []
    for cand in candidates:
        c_start = float(cand.get("start", 0))
        c_end = float(cand.get("end", 0))
        segs_in_range = [
            s for s in full_transcript_segments
            if s["end"] > c_start and s["start"] < c_end
        ]
        transcript_text = " ".join(s["text"].strip() for s in segs_in_range)
        enriched_candidates.append({
            **cand,
            "full_text": transcript_text,
        })

    candidates_json = json.dumps(enriched_candidates, indent=2, ensure_ascii=False)

    system = (
        "You are an elite short-form video strategist for Indian creators. "
        "You're now in the FINAL SELECTION round — you've already narrowed a long video "
        "down to candidates, and now you need to pick the top 5 with expert-level reasoning.\n\n"
        "YOUR JOB: Score and rank these candidates. Be ruthlessly comparative. "
        "If two candidates make similar points, pick the better-executed one and drop the other. "
        "Give each chosen clip a CONFIDENCE score (0-100) — how sure you are this will perform. "
        "Be honest: mark clips as low-confidence if they're weak. This helps the creator "
        "know which clips to post first vs. which to skip or improve.\n\n"
        f"CRITICAL: Return EXACTLY {num_clips} clips (no fewer, no more)."
    )

    user = f"""{lang_directive}

CONTENT TYPE: {content_type.upper()}
{strategy}

TARGET PLATFORM: {platform.upper()}
- Ideal length: {platform_profile['ideal_length']}
- Hook must land in: first {platform_profile['hook_critical_seconds']} seconds
- Pacing: {platform_profile['pacing']}
- Ending: {platform_profile['ending']}

GOAL: {goal.upper()}
{goal_framing}

CREATOR PREFERENCES: {creator_memory_summary or "New creator, no prior data."}

Below are {len(enriched_candidates)} candidates from Pass 1 analysis, each with:
  - start/end timestamps
  - title (tentative)
  - topic
  - why_notable
  - initial_score
  - full_text (actual transcript for that range)

Your job: Pick the best {num_clips} and score them deeply.

For each of the 4 sub-scores (0-10):
  - hook:          Does the first 3 seconds grab attention?
  - emotion:       Does it trigger surprise, curiosity, humor, inspiration?
  - completeness:  Does it end on a complete thought (not cut mid-sentence)?
  - shareability:  Would a viewer DM this to a friend?

Then compute:
  - virality_score (0-100): overall weighted judgment
  - confidence    (0-100): how sure you are this will perform.
    • 80+ = confident this will work for target platform
    • 60-79 = good bet, decent odds
    • 40-59 = interesting but risky
    • <40 = weak, creator should consider skipping

Return JSON with EXACTLY {num_clips} clips:
{{
  "clips": [
    {{
      "rank": 1,
      "title": "Refined, compelling title (max 60 chars)",
      "start": 145.3,
      "end": 208.7,
      "hook": "The exact opening line that grabs attention",
      "emotion": "curiosity|surprise|inspiration|humor|urgency|empathy",
      "scores": {{
        "hook": 9,
        "emotion": 8,
        "completeness": 9,
        "shareability": 8
      }},
      "virality_score": 85,
      "confidence": 82,
      "platform_fit": "{platform}",
      "reason": "3-5 sentences. Be SPECIFIC: cite actual words/moments from the clip. NOT 'strong hook' — instead 'opens with the concrete claim that 90% of founders fail, which creates instant curiosity'. Mention why it fits {platform} specifically.",
      "beats_alternatives": "1-2 sentences: why this was chosen over similar candidates. E.g., 'Picked over candidate #4 because that one made the same point but rambled; this one lands in 35s with a cleaner payoff.'"
    }}
  ]
}}

RULES:
1. EXACTLY {num_clips} clips, ranked by virality_score descending
2. No two clips should be about the same topic — ensure topic diversity
3. Confidence should be honest — if the candidates are weak, use low confidence
4. Length should fit {platform}'s sweet spot ({platform_profile['ideal_length']})
5. start/end can be refined from the candidate's original values (tighten hooks, trim tails)

CANDIDATES:
{candidates_json}
"""

    response = complete_json_with_fallback(system, user, max_tokens=4500)
    clips = response.get("clips", [])
    if not isinstance(clips, list):
        raise LLMError(f"Pass 2: Expected 'clips' as list, got: {type(clips)}")
    return clips


# =============================================================================
# LEGACY ENTRY POINT — kept for backwards compat with score.py
# Now internally calls the two-pass flow.
# =============================================================================
def score_clips(
    transcript_segments: list[dict[str, Any]],
    goal: str = "viral",
    num_clips: int = 5,
    creator_memory_summary: str = "",
    content_type: str = "general",
    detected_language: str = "en",
    platform: str = "all",
    full_transcript_segments: list[dict[str, Any]] | None = None,
    duration_sec: float = 0,
) -> list[dict]:
    """
    Main entry point. Internally runs two-pass if Sonnet is configured.

    `transcript_segments` = candidates (legacy), may be same as full_transcript.
    `full_transcript_segments` = the full video transcript (optional, for Pass 1).

    If full_transcript_segments is provided, runs TWO-PASS.
    Otherwise, falls back to single-pass for backwards compat.
    """
    # If we have the full transcript, do two-pass
    if full_transcript_segments and duration_sec > 0:
        logger.info(
            f"Two-pass clip selection: {len(full_transcript_segments)} full-video segments, "
            f"{duration_sec:.0f}s total, platform={platform}"
        )

        # Pass 1: full-video analysis
        logger.info("Pass 1: analyzing full video...")
        candidates = analyze_full_video(
            full_transcript_segments=full_transcript_segments,
            content_type=content_type,
            detected_language=detected_language,
            platform=platform,
            duration_sec=duration_sec,
            num_candidates=10,
        )
        logger.info(f"Pass 1 returned {len(candidates)} candidates")

        if not candidates:
            raise LLMError("Pass 1 returned 0 candidates — video may be empty/silent")

        # Pass 2: deep scoring
        logger.info("Pass 2: deep scoring with confidence + platform tuning...")
        clips = score_candidates_deep(
            candidates=candidates,
            full_transcript_segments=full_transcript_segments,
            content_type=content_type,
            detected_language=detected_language,
            platform=platform,
            goal=goal,
            num_clips=num_clips,
            creator_memory_summary=creator_memory_summary,
        )
        logger.info(f"Pass 2 returned {len(clips)} final clips")
        return clips

    # Legacy single-pass fallback
    logger.info("Single-pass scoring (no full_transcript provided)")
    return _legacy_single_pass(
        transcript_segments=transcript_segments,
        goal=goal,
        num_clips=num_clips,
        creator_memory_summary=creator_memory_summary,
        content_type=content_type,
        detected_language=detected_language,
    )


def _legacy_single_pass(
    transcript_segments: list[dict[str, Any]],
    goal: str,
    num_clips: int,
    creator_memory_summary: str,
    content_type: str,
    detected_language: str,
) -> list[dict]:
    """Legacy single-pass scoring — kept for safety if two-pass setup fails."""
    goal_prompts = {
        "viral": "Which clips would STOP SCROLLING on short-form platforms? Strong hooks, emotion, surprise.",
        "authority": "Which clips sound most EXPERT? Confident insights.",
        "lead_gen": "Which clips create CURIOSITY GAPS?",
        "educational": "Which clips deliver CLEAR lessons in 60s?",
    }
    goal_framing = goal_prompts.get(goal, goal_prompts["viral"])
    strategy = _CLIPPING_STRATEGIES.get(content_type, _CLIPPING_STRATEGIES["general"])
    lang_directive = _language_directive(detected_language)

    system = (
        "You are an elite short-form video strategist. "
        f"You MUST return EXACTLY {num_clips} clips with 4 sub-scores each."
    )
    transcript_text = "\n".join(
        f"[{seg['start']:.1f}s-{seg['end']:.1f}s] {seg['text']}"
        for seg in transcript_segments
    )
    user = f"""{lang_directive}

CONTENT TYPE: {content_type.upper()}
{strategy}

GOAL: {goal.upper()}
{goal_framing}

Creator preferences: {creator_memory_summary or "New creator."}

Pick the top {num_clips} clips from these {len(transcript_segments)} candidates.

Return JSON:
{{
  "clips": [
    {{
      "rank": 1, "title": "...", "start": 0, "end": 0,
      "hook": "...", "emotion": "curiosity|surprise|...",
      "scores": {{"hook": 8, "emotion": 9, "completeness": 7, "shareability": 9}},
      "virality_score": 87, "confidence": 75,
      "reason": "3-5 sentences, specific to THIS clip"
    }}
  ]
}}

CANDIDATES:
{transcript_text}
"""
    response = complete_json_with_fallback(system, user, max_tokens=3500)
    return response.get("clips", [])