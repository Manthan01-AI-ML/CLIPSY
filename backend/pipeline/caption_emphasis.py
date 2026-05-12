"""
backend/pipeline/caption_emphasis.py

AI-driven caption emphasis + Hinglish-aware language tagging.

Two-mode tagger:
  1. Standard emphasis mode: returns word indices to highlight
  2. Hinglish mode: in addition to emphasis, returns per-word LANGUAGE tags
     (english | hindi_dev | hindi_rom | filler | number | neutral) so the
     renderer can color-code by language — making code-switched content
     visually rhythmic.

Why this matters:
  Indian creators' content is 60-80% code-switched ("yaar I'm telling you,
  matlab seriously bro"). Rendering all words in one color flattens the
  distinct rhythm of Hinglish. Coloring English vs Hindi words differently
  makes captions feel native to how India actually talks — something Opus
  Clip / Submagic do NOT do.

Cost:
  - Standard emphasis: ~$0.005/clip (300 tokens)
  - Hinglish mode: ~$0.008/clip (500 tokens — denser per-word labels)

Safety:
  - Every Sonnet response is validated against a strict allowlist
  - Out-of-range indices ignored, unknown language labels silently dropped
  - Fallback to empty emphasis (clip still renders, just without styling)
"""
from __future__ import annotations

import logging
import re
from typing import Any, Literal

from backend.services.llm import complete_json_with_fallback, LLMError

logger = logging.getLogger(__name__)


# Per-word language labels we accept. Anything else from Sonnet → discarded.
ALLOWED_LANG_LABELS = {
    "english",     # Pure English: "the", "important", "always"
    "hindi_dev",   # Hindi in Devanagari: "बात", "सच्ची", "पैसा"
    "hindi_rom",   # Hindi transliterated to Roman: "yaar", "bhai", "matlab", "paisa"
    "filler",      # Filler/discourse marker: "umm", "actually", "haan", "acha", "arre"
    "number",      # Numeric: "10x", "90%", "₹500", "2024"
    "neutral",     # Punctuation, brand names, things that defy categorization
}

# Indian-specific Hindi fillers in Roman script. Their PRESENCE signals Hinglish.
# These are uniquely Indian-Hindi — no English speaker says "yaar" or "matlab".
HINDI_ROMAN_FILLERS = {
    "matlab", "yaar", "yar", "bhai", "arre", "arrey",
    "achha", "acha", "haan", "ha", "hai",
    "toh", "to", "na", "nahi", "nahin",
    "kya", "kyun", "kyu", "kyon",
    "wo", "woh", "yeh", "ye",
    "samjho", "samjha", "dekho", "suno",
    "bas", "abhi", "phir", "fir",
    "kuch", "kuchh", "thoda", "thodi",
}

# English fillers — tagged AS fillers when found, but don't signal Hinglish
# (because pure English content has them too).
ENGLISH_FILLERS = {
    "umm", "uhh", "uh", "hmm",
    "actually", "basically", "literally", "obviously", "honestly",
    "like", "okay", "ok", "right",
    "you know", "i mean", "sort of", "kind of",
}

# Combined: any of these gets a "filler" language label (used in tagging)
ALL_FILLERS_ROMAN = HINDI_ROMAN_FILLERS | {
    f for f in ENGLISH_FILLERS if " " not in f  # only single-word fillers for word-level matching
}

# Backward-compat alias (some callers may import this name)
INDIAN_FILLERS_ROMAN = ALL_FILLERS_ROMAN


def _normalize_word(w: str) -> str:
    """Strip punctuation for matching."""
    return re.sub(r"[^\w]", "", w).lower()


def _has_devanagari(text: str) -> bool:
    """Cheap deterministic check: does string contain Hindi characters?"""
    return any("\u0900" <= ch <= "\u097F" for ch in text)


def _looks_like_number(w: str) -> bool:
    """Detect numeric tokens like '90%', '10x', '₹500', '2024'."""
    cleaned = w.strip("₹$%.,")
    if not cleaned:
        return False
    # Has at least one digit and is mostly numeric
    digits = sum(c.isdigit() for c in cleaned)
    return digits > 0 and digits >= len(cleaned) // 2


def _quick_hinglish_signal(words: list[dict[str, Any]]) -> dict[str, float]:
    """
    Cheap deterministic pre-check: is this content likely Hinglish?

    Returns ratios:
      - devanagari_ratio: % of words written in Devanagari
      - hindi_filler_ratio: % matching Hindi-only fillers ("yaar", "matlab")
                            — strong Hinglish signal (English speakers don't say these)
      - english_alpha_ratio: % of words that look like Roman ASCII
    """
    if not words:
        return {"devanagari_ratio": 0, "hindi_filler_ratio": 0, "english_alpha_ratio": 0}

    n = len(words)
    dev_count = 0
    hindi_filler_count = 0
    eng_alpha_count = 0

    for w in words:
        word_text = str(w.get("word", "")).strip()
        if not word_text:
            continue
        if _has_devanagari(word_text):
            dev_count += 1
        norm = _normalize_word(word_text)
        # ONLY count Hindi-specific fillers as the signal
        if norm in HINDI_ROMAN_FILLERS:
            hindi_filler_count += 1
        if word_text.isascii() and any(c.isalpha() for c in word_text):
            eng_alpha_count += 1

    return {
        "devanagari_ratio": dev_count / n,
        "hindi_filler_ratio": hindi_filler_count / n,
        "english_alpha_ratio": eng_alpha_count / n,
    }


def _is_hinglish_content(words: list[dict[str, Any]]) -> bool:
    """
    Determine if the clip's transcript looks like Hinglish.

    Hinglish signature:
      - Has Hindi-Roman fillers ("yaar", "matlab", "bhai") above 3% threshold
      - OR mixed Roman+Devanagari roughly balanced
      - Pure English (no Hindi fillers, no Devanagari) → NOT Hinglish
      - Pure Devanagari (no Roman) → NOT Hinglish (it's pure Hindi)
    """
    sig = _quick_hinglish_signal(words)

    # Strong signal: Hindi-only fillers present (English doesn't have these)
    if sig["hindi_filler_ratio"] >= 0.03:  # 3%+ of words are "yaar"/"matlab"/etc
        return True

    # Mixed scripts: Devanagari + Roman both present
    if 0.05 <= sig["devanagari_ratio"] <= 0.6 and sig["english_alpha_ratio"] >= 0.3:
        return True

    return False


def tag_emphasis_and_emojis(
    clip_words: list[dict[str, Any]],
    clip_title: str = "",
    detected_language: str = "en",
    enable_hinglish_mode: bool = False,
) -> dict[str, Any]:
    """
    Identify which words to emphasize. In Hinglish mode, also tags each word's language.

    Args:
        clip_words: [{"word": "yaar", "start": 1.2, "end": 1.5}, ...]
        clip_title: clip title for prompt context
        detected_language: hint from Whisper ("en" | "hi" | etc.)
        enable_hinglish_mode: if True, run the per-word language tagger
            (auto-disabled if content is detected as pure English or pure Hindi)

    Returns:
        {
          "emphasis_indices": [3, 7, 12],
          "emoji_map": {},                            # always empty (back-compat)
          "language_map": {0: "english", 1: "hindi_rom", ...},  # only if hinglish_mode
          "filler_indices": [4, 9],                   # words to dim/de-emphasize
          "is_hinglish": True,                        # whether we detected code-switching
        }
    """
    # Empty fallback shape
    empty = {
        "emphasis_indices": [],
        "emoji_map": {},
        "language_map": {},
        "filler_indices": [],
        "is_hinglish": False,
    }
    if not clip_words:
        return empty

    # Build a compact indexed transcript: "0:Yaar 1:I 2:was 3:matlab ..."
    indexed_words = []
    for i, w in enumerate(clip_words):
        word_text = str(w.get("word", "")).strip()
        if not word_text:
            continue
        indexed_words.append(f"{i}:{word_text}")

    if not indexed_words:
        return empty

    transcript_indexed = " ".join(indexed_words)
    if len(transcript_indexed) > 4500:
        transcript_indexed = transcript_indexed[:4500]

    # Decide: are we ACTUALLY in Hinglish territory?
    # User can request hinglish_mode but content might be pure English/Hindi.
    use_hinglish = enable_hinglish_mode and _is_hinglish_content(clip_words)

    if use_hinglish:
        result = _tag_hinglish(clip_words, transcript_indexed, clip_title)
    else:
        result = _tag_emphasis_only(clip_words, transcript_indexed, clip_title, detected_language)

    return result


def _tag_emphasis_only(
    clip_words: list[dict[str, Any]],
    transcript_indexed: str,
    clip_title: str,
    detected_language: str,
) -> dict[str, Any]:
    """Standard emphasis-only tagger (unchanged from prior version)."""
    lang_directive = ""
    if detected_language == "hi":
        lang_directive = (
            "LANGUAGE: Hindi/Hinglish content. Pick the most impactful Hindi/English "
            "words ('sach', 'galti', 'paisa', 'never', 'always') for emphasis."
        )

    system = (
        "You are a viral short-form video caption stylist. Top creators highlight "
        "15-25% of words in each clip — the 'money words' that carry emotion, "
        "surprise, or specific claims. You replicate this instinct."
    )
    user = f"""{lang_directive}
CLIP TITLE: {clip_title}

WORD-INDEXED TRANSCRIPT:
{transcript_indexed}

Pick the word indices that should be EMPHASIZED — the ones that carry weight.
GOOD: specific numbers, power verbs, superlatives, emotional triggers, the CORE noun/verb.
SKIP: articles, prepositions, filler ("you know", "like", "um"), generic verbs.

Target 15-25% of words. NOT more — that's visual noise.

Return JSON:
{{
  "emphasis_indices": [3, 7, 12, 18]
}}
"""

    try:
        response = complete_json_with_fallback(system, user, max_tokens=300)
    except LLMError as e:
        logger.warning(f"Emphasis tagging failed: {e}. Empty fallback.")
        return {
            "emphasis_indices": [], "emoji_map": {},
            "language_map": {}, "filler_indices": [], "is_hinglish": False,
        }

    return {
        "emphasis_indices": _validate_indices(
            response.get("emphasis_indices"), len(clip_words)
        ),
        "emoji_map": {},
        "language_map": {},
        "filler_indices": [],
        "is_hinglish": False,
    }


def _tag_hinglish(
    clip_words: list[dict[str, Any]],
    transcript_indexed: str,
    clip_title: str,
) -> dict[str, Any]:
    """
    Hinglish-aware tagger. For EACH word, ask Sonnet to label:
      - language: english | hindi_dev | hindi_rom | filler | number | neutral
      - is_emphasis: should this word be highlighted?

    Then derive emphasis_indices + filler_indices + language_map from response.
    """
    # Cheap deterministic priors we can give Sonnet to nudge accuracy
    devanagari_examples = []
    filler_examples = []
    for i, w in enumerate(clip_words[:50]):  # sample first 50 for prompt brevity
        word_text = str(w.get("word", "")).strip()
        if _has_devanagari(word_text):
            devanagari_examples.append(f"{i}:{word_text}")
        if _normalize_word(word_text) in INDIAN_FILLERS_ROMAN:
            filler_examples.append(f"{i}:{word_text}")

    prior_hints = []
    if devanagari_examples:
        prior_hints.append(f"DEVANAGARI WORDS (definitely hindi_dev): {', '.join(devanagari_examples[:8])}")
    if filler_examples:
        prior_hints.append(f"LIKELY FILLERS (Indian discourse markers): {', '.join(filler_examples[:8])}")
    prior_hint_block = "\n".join(prior_hints) if prior_hints else ""

    system = (
        "You are a Hinglish caption stylist for Indian short-form video creators. "
        "You analyze code-switched Indian content and label every word so the "
        "renderer can color-code English vs Hindi vs filler words separately, "
        "creating the visual rhythm that makes Hinglish captions feel native.\n\n"
        "You are precise: every word index in the transcript MUST appear exactly "
        "once in your output. Do not skip words. Do not invent indices."
    )

    user = f"""TASK: For each word in the transcript, output:
  1. Its language label
  2. Whether it should be emphasized (highlighted)

CLIP TITLE: {clip_title}

WORD-INDEXED TRANSCRIPT (one word per "INDEX:word" pair):
{transcript_indexed}

{prior_hint_block}

LANGUAGE LABELS (use exactly these, lowercase):
  - english     → English word: "the", "important", "absolutely"
  - hindi_dev   → Hindi in Devanagari script: "बात", "सच", "पैसा"
  - hindi_rom   → Hindi transliterated to Roman: "yaar", "bhai", "paisa", "matlab", "kuch"
  - filler      → Discourse marker / weak word that breaks rhythm:
                  "umm", "uhh", "actually", "literally", "haan", "acha", "arre",
                  "you know", "I mean", "like" (when used as filler)
  - number      → Numeric: "10x", "90%", "₹500", "2024", "three"
  - neutral     → Punctuation, brand names, English words that resist categorization

EMPHASIS RULES:
  - Mark 15-25% of words as emphasis (the "money words")
  - GOOD candidates: specific numbers, power verbs ("destroys", "exposes"),
    superlatives ("only", "never"), strong claims, emotional triggers
  - NEVER emphasize fillers
  - For Hinglish: when a Hindi word interrupts an English sentence (or vice versa),
    that switch-point word is OFTEN the emotional emphasis ("yaar this is INSANE"
    → emphasize "yaar" and "INSANE")

Return JSON in this EXACT shape:
{{
  "words": [
    {{"i": 0, "lang": "hindi_rom", "emph": false}},
    {{"i": 1, "lang": "english", "emph": false}},
    {{"i": 2, "lang": "english", "emph": true}},
    ...
  ]
}}

Every index from 0 to N-1 (where N = total word count) must appear exactly once.
"""

    try:
        # Hinglish mode needs more output tokens because every word gets a label
        response = complete_json_with_fallback(system, user, max_tokens=2500)
    except LLMError as e:
        logger.warning(f"Hinglish tagging failed: {e}. Falling back to deterministic.")
        return _deterministic_hinglish_fallback(clip_words)

    return _parse_hinglish_response(response, clip_words)


def _parse_hinglish_response(
    response: dict, clip_words: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate + sanitize Sonnet's per-word output. Fall back to deterministic on issues."""
    raw_words = response.get("words", [])
    if not isinstance(raw_words, list) or not raw_words:
        return _deterministic_hinglish_fallback(clip_words)

    n = len(clip_words)
    language_map: dict[int, str] = {}
    emphasis_indices: set[int] = set()
    filler_indices: set[int] = set()

    for entry in raw_words:
        if not isinstance(entry, dict):
            continue
        try:
            idx = int(entry.get("i", -1))
        except (ValueError, TypeError):
            continue
        if not (0 <= idx < n):
            continue

        lang = str(entry.get("lang", "")).strip().lower()
        if lang not in ALLOWED_LANG_LABELS:
            lang = "neutral"  # safe default
        language_map[idx] = lang

        if lang == "filler":
            filler_indices.add(idx)
        elif bool(entry.get("emph", False)):
            emphasis_indices.add(idx)

    # Coverage check: if Sonnet missed words, use deterministic priors for them
    for i, w in enumerate(clip_words):
        if i not in language_map:
            word_text = str(w.get("word", "")).strip()
            if _has_devanagari(word_text):
                language_map[i] = "hindi_dev"
            elif _looks_like_number(word_text):
                language_map[i] = "number"
            elif _normalize_word(word_text) in INDIAN_FILLERS_ROMAN:
                language_map[i] = "filler"
                filler_indices.add(i)
            else:
                language_map[i] = "english"  # safe default for unlabeled Roman text

    # Sanity: cap emphasis at 30% (don't let Sonnet over-emphasize)
    if len(emphasis_indices) > n * 0.30:
        # Keep only the first 30% (preserves Sonnet's ordering preference)
        emphasis_indices = set(sorted(emphasis_indices)[: int(n * 0.30)])

    logger.info(
        f"Hinglish tagged: {len(emphasis_indices)} emphasis, "
        f"{len(filler_indices)} fillers, "
        f"langs: {dict.fromkeys(language_map.values())}"
    )

    return {
        "emphasis_indices": sorted(emphasis_indices),
        "emoji_map": {},
        "language_map": language_map,
        "filler_indices": sorted(filler_indices),
        "is_hinglish": True,
    }


def _deterministic_hinglish_fallback(
    clip_words: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    If Sonnet fails, produce reasonable Hinglish styling using deterministic rules.
    Worse than Sonnet but better than nothing — and no extra API cost.
    """
    language_map: dict[int, str] = {}
    filler_indices: list[int] = []

    for i, w in enumerate(clip_words):
        word_text = str(w.get("word", "")).strip()
        norm = _normalize_word(word_text)

        if _has_devanagari(word_text):
            language_map[i] = "hindi_dev"
        elif _looks_like_number(word_text):
            language_map[i] = "number"
        elif norm in INDIAN_FILLERS_ROMAN:
            language_map[i] = "filler"
            filler_indices.append(i)
        else:
            language_map[i] = "english"  # default for ambiguous Roman words

    return {
        "emphasis_indices": [],  # deterministic emphasis is too unreliable to attempt
        "emoji_map": {},
        "language_map": language_map,
        "filler_indices": filler_indices,
        "is_hinglish": True,
    }


def _validate_indices(raw, n: int) -> list[int]:
    """Validate a list of indices: must be int, in range, deduped, sorted."""
    if not isinstance(raw, list):
        return []
    valid: set[int] = set()
    for idx in raw:
        try:
            i = int(idx)
            if 0 <= i < n:
                valid.add(i)
        except (ValueError, TypeError):
            continue
    return sorted(valid)


def flatten_segments_to_words(segments: list[dict]) -> list[dict]:
    """
    Given segments (some with word-level timestamps), return a flat ordered
    list of words with timings.
    """
    out = []
    for seg in segments:
        words = seg.get("words") or []
        if words:
            for w in words:
                word_text = str(w.get("word", "")).strip()
                if not word_text:
                    continue
                out.append({
                    "word": word_text,
                    "start": float(w.get("start", seg.get("start", 0))),
                    "end": float(w.get("end", seg.get("end", 0))),
                })
        else:
            text = str(seg.get("text", "")).strip()
            if not text:
                continue
            seg_words = text.split()
            if not seg_words:
                continue
            seg_start = float(seg.get("start", 0))
            seg_end = float(seg.get("end", seg_start + 1))
            per = (seg_end - seg_start) / len(seg_words)
            for j, w in enumerate(seg_words):
                out.append({
                    "word": w,
                    "start": seg_start + j * per,
                    "end": seg_start + (j + 1) * per,
                })
    return out