"""
backend/pipeline/intro_outro.py

Session C: AI-generated PRE-HOOK and OUTRO for each clip.

Background (from 2026 research):
  - First 3 seconds determine 70%+ of retention
  - "Hook + Problem + Solution + CTA" structure battle-tested across 118,584 viral videos
  - OUTROS on Shorts should NOT be traditional "follow for more!" — they kill the loop
  - Instead, outro should be a CALLBACK that makes viewer rewatch
  - YouTube counts every loop as an additional view (since March 2025)

Architecture:
  - For each clip, call Sonnet with clip's transcript + title
  - Sonnet returns: {hook_line1, hook_line2, outro_line1, outro_line2}
  - Cheap: ~500 input tokens, ~200 output tokens = ~$0.005 per clip
"""
from __future__ import annotations

import json
import logging
from typing import Any

from backend.services.llm import complete_json_with_fallback, LLMError

logger = logging.getLogger(__name__)


# Research-backed pre-hook patterns. Sonnet picks the best one per clip.
_HOOK_PATTERNS = """
Use ONE of these battle-tested patterns (pick whichever fits the clip best):

1. THE MISTAKE
   Line 1: "The #1 [topic] mistake"
   Line 2: "90% of [audience] make it →"

2. THE NUMBER
   Line 1: "3 [things]. 10x your [outcome]."
   Line 2: "[Speaker name/credential]"

3. THE CONTRARIAN
   Line 1: "Everything you know about [topic]"
   Line 2: "is wrong. Here's why →"

4. THE SECRET
   Line 1: "[Speaker] reveals the secret"
   Line 2: "[Topic] insiders don't share"

5. THE CONCRETE CLAIM
   Line 1: "[Specific number/fact/statistic]"
   Line 2: "[Why it matters in 4-6 words]"

6. THE QUESTION
   Line 1: "What if [provocative premise]?"
   Line 2: "[Speaker] has proof →"

7. THE WARNING
   Line 1: "Stop [common behavior]."
   Line 2: "[Speaker] explains why →"

8. THE TEASE
   Line 1: "[Surprising claim]..."
   Line 2: "...until you see this."
"""

# Research-backed outro callbacks. These LOOP the viewer back to start.
_OUTRO_PATTERNS = """
Use ONE of these callback patterns (NOT a traditional CTA):

1. THE CATCH
   Line 1: "Did you catch it?"
   Line 2: "Watch it again ↻"

2. THE QUESTION
   Line 1: "Which [thing] will you try?"
   Line 2: "(Comment below ↓)"

3. THE REPLAY
   Line 1: "Mind = blown?"
   Line 2: "Replay to save it ↻"

4. THE CHALLENGE
   Line 1: "Try this for 7 days."
   Line 2: "Tell me what changed ↻"

5. THE RECAP
   Line 1: "Still confused?"
   Line 2: "Watch from the top ↻"

6. THE COMMITMENT
   Line 1: "Save this clip."
   Line 2: "You'll need it later ↓"

7. THE HOOK-BACK
   Line 1: Directly references the hook from the start
   Line 2: "↻ Watch again to catch it"
"""


def generate_intro_outro(
    clip_text: str,
    clip_title: str,
    detected_language: str = "en",
    speaker_hint: str = "",
    platform: str = "all",
) -> dict[str, str]:
    """
    Generate pre-hook (3s) and outro callback (2s) text for a single clip.

    Returns:
      {
        "hook_line1": str,   # Large text, the attention grabber
        "hook_line2": str,   # Smaller text with arrow, creates curiosity gap
        "outro_line1": str,  # Question or callback
        "outro_line2": str,  # The loop trigger (↻ Watch again, etc.)
        "pattern_used": str, # Which pattern was picked (for debugging)
      }
    """
    lang_directive = ""
    if detected_language == "hi":
        lang_directive = (
            "LANGUAGE: Hindi content. Use Devanagari OR Hinglish for hooks, "
            "whatever feels more natural. Hindi creators often use Hinglish "
            "hooks like 'Sach ye hai...' or 'ek galti jo sab karte hain'. "
            "Use SHORT, punchy phrases. Same for outro."
        )
    elif detected_language == "en":
        lang_directive = (
            "LANGUAGE: English. Use tight, punchy phrases. No weak words like 'maybe' or 'kind of'."
        )
    else:
        lang_directive = (
            "LANGUAGE: Match the clip's primary language. Keep hooks tight and punchy."
        )

    speaker_line = ""
    if speaker_hint:
        speaker_line = f"SPEAKER: {speaker_hint}\n"

    system = (
        "You are an elite short-form video strategist. Your specialty: writing "
        "the FIRST 3 SECONDS of a viral clip and the LAST 2 SECONDS that make "
        "viewers rewatch.\n\n"
        "Your hooks stop the scroll. Your outros trigger the replay loop.\n\n"
        "CRITICAL RULES:\n"
        "- Lines must be SHORT. Line 1: max 6 words. Line 2: max 6 words.\n"
        "- NO hashtags, NO emojis except ↻ ↓ → arrows.\n"
        "- NO generic CTAs like 'follow for more' — they kill the loop.\n"
        "- The outro should make viewers think 'wait, what was that?' and REWATCH.\n"
        "- Be specific to THIS clip's content, not generic."
    )

    user = f"""{lang_directive}
{speaker_line}
CLIP TITLE: {clip_title}

CLIP TRANSCRIPT:
{clip_text[:2000]}

Write a 2-line PRE-HOOK (big white text that appears BEFORE the clip plays)
and a 2-line OUTRO (appears at the very end).

{_HOOK_PATTERNS}

{_OUTRO_PATTERNS}

Return JSON:
{{
  "hook_line1": "Max 6 words. The grabber.",
  "hook_line2": "Max 6 words with → arrow.",
  "outro_line1": "Max 6 words. The callback or question.",
  "outro_line2": "Max 6 words with ↻ or ↓.",
  "pattern_used": "mistake|number|contrarian|secret|claim|question|warning|tease",
  "hook_accent_word": "ONE word from hook_line1 that should be highlighted in coral (or empty if none stands out)"
}}

Rules:
- hook_line1 must NOT end with a period
- hook_line2 MUST end with → (right arrow) to create anticipation
- outro_line2 MUST contain ↻ (replay arrow) or ↓ (down arrow) to trigger the loop
- hook_accent_word should be the most provocative word (e.g., 'mistake', 'wrong', 'secret')
"""

    try:
        response = complete_json_with_fallback(system, user, max_tokens=400)
    except LLMError as e:
        logger.warning(f"Intro/outro generation failed: {e}. Using fallback.")
        return _fallback_intro_outro(clip_title)

    # Validate response
    required = ["hook_line1", "hook_line2", "outro_line1", "outro_line2"]
    if not all(k in response and isinstance(response[k], str) and response[k].strip() for k in required):
        logger.warning(f"LLM returned incomplete intro/outro: {response}. Using fallback.")
        return _fallback_intro_outro(clip_title)

    # Enforce constraints
    hook_line2 = response["hook_line2"].strip()
    if "→" not in hook_line2:
        hook_line2 = hook_line2.rstrip(".") + " →"

    outro_line2 = response["outro_line2"].strip()
    if "↻" not in outro_line2 and "↓" not in outro_line2:
        outro_line2 = outro_line2.rstrip(".") + " ↻"

    result = {
        "hook_line1": response["hook_line1"].strip().rstrip("."),
        "hook_line2": hook_line2,
        "outro_line1": response["outro_line1"].strip(),
        "outro_line2": outro_line2,
        "pattern_used": response.get("pattern_used", "unknown"),
        "hook_accent_word": response.get("hook_accent_word", "").strip(),
    }

    logger.info(
        f"Hook/outro generated ({result['pattern_used']}): "
        f"'{result['hook_line1']}' / '{result['outro_line1']}'"
    )
    return result


def _fallback_intro_outro(clip_title: str) -> dict[str, str]:
    """If LLM fails, use a safe generic pattern. Still better than nothing."""
    # Clean title, cap length
    title_words = clip_title.split()[:4]
    title_short = " ".join(title_words) if title_words else "This clip"
    return {
        "hook_line1": "Don't miss this",
        "hook_line2": f"{title_short} →",
        "outro_line1": "Mind = blown?",
        "outro_line2": "Watch again ↻",
        "pattern_used": "fallback",
        "hook_accent_word": "",
    }
