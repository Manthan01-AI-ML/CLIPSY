"""
backend/pipeline/render.py

Production render with CAPTION PRESETS + Hindi Devanagari support (Step 12).

Key changes from Step 11:
  - Auto-switches font for Hindi content (Noto Sans Devanagari)
  - Falls back to default preset font for non-Hindi content
  - Preset system unchanged (Hormozi, Bold, Minimal, TikTok)
  - Speed optimizations unchanged (ultrafast + threads)
"""
from __future__ import annotations

import logging
import subprocess
import uuid
from pathlib import Path

from backend.services.storage import clip_output_dir

logger = logging.getLogger(__name__)


class RenderError(Exception):
    pass


# =============================================================================
# CAPTION PRESETS
# =============================================================================
# Colors in ASS are BGR hex: &HAABBGGRR
CAPTION_PRESETS = {
    "hormozi": {
        "font": "Montserrat",
        "font_hindi": "Noto Sans Devanagari",   # NEW: font override for Hindi content
        "font_size": 54,
        "primary_color": "&H00FFFFFF",
        "secondary_color": "&H0000E1FF",
        "outline_color": "&H00000000",
        "back_color": "&H00000000",
        "bold": 1,
        "outline_width": 4,
        "shadow": 2,
        "alignment": 2,
        "margin_v": 160,
        "use_karaoke": True,
        "one_word_per_line": False,
    },
    "bold": {
        "font": "Arial Black",
        "font_hindi": "Noto Sans Devanagari",
        "font_size": 46,
        "primary_color": "&H00FFFFFF",
        "secondary_color": "&H00FFFFFF",
        "outline_color": "&H00000000",
        "back_color": "&H00000000",
        "bold": 1,
        "outline_width": 5,
        "shadow": 3,
        "alignment": 2,
        "margin_v": 140,
        "use_karaoke": False,
        "one_word_per_line": False,
    },
    "minimal": {
        "font": "Arial",
        "font_hindi": "Noto Sans Devanagari",
        "font_size": 36,
        "primary_color": "&H00FFFFFF",
        "secondary_color": "&H00FFFFFF",
        "outline_color": "&H00000000",
        "back_color": "&H80000000",
        "bold": 0,
        "outline_width": 1,
        "shadow": 1,
        "alignment": 2,
        "margin_v": 100,
        "use_karaoke": False,
        "one_word_per_line": False,
    },
    "tiktok": {
        "font": "Arial Black",
        "font_hindi": "Noto Sans Devanagari",
        "font_size": 50,
        "primary_color": "&H00FFFFFF",
        "secondary_color": "&H00FFE100",
        "outline_color": "&H00000000",
        "back_color": "&H00000000",
        "bold": 1,
        "outline_width": 4,
        "shadow": 2,
        "alignment": 2,
        "margin_v": 180,
        "use_karaoke": True,
        "one_word_per_line": True,
    },
    # ==========================================================================
    # Session B1: Modern viral caption presets (Submagic/Opus-grade)
    # These use emphasis_mode=True — render path calls an alt builder
    # ==========================================================================
    "viral": {
        "font": "Montserrat",
        "font_hindi": "Noto Sans Devanagari",
        "font_size": 58,
        "emphasis_size": 76,               # larger for emphasized words
        "primary_color": "&H00FFFFFF",     # white base text
        "emphasis_color": "&H003E5FFF",    # coral emphasis (#FF5F3E → BGR flip)
        "outline_color": "&H00000000",
        "back_color": "&H00000000",
        "bold": 1,
        "outline_width": 5,
        "shadow": 3,
        "alignment": 2,
        "margin_v": 200,
        "emphasis_mode": True,             # triggers new builder
        "style_name": "viral",
        "words_per_card": 4,               # 4 words on screen at a time
        "pop_scale": True,                 # words pop in with scale animation
        "emoji_enabled": True,
    },
    "clean": {
        "font": "Montserrat",
        "font_hindi": "Noto Sans Devanagari",
        "font_size": 50,
        "emphasis_size": 60,
        "primary_color": "&H00F4F1EA",     # cream
        "emphasis_color": "&H003E5FFF",    # coral
        "outline_color": "&H00000000",
        "back_color": "&H00000000",
        "bold": 1,
        "outline_width": 4,
        "shadow": 2,
        "alignment": 2,
        "margin_v": 180,
        "emphasis_mode": True,
        "style_name": "clean",
        "words_per_card": 4,               # a bit more text per card for readability
        "pop_scale": False,                # smooth fade-in instead
        "emoji_enabled": False,            # clean preset skips emojis
    },
    "dynamic": {
        "font": "Montserrat",
        "font_hindi": "Noto Sans Devanagari",
        "font_size": 52,
        "emphasis_size": 82,               # big size jump for max contrast
        "primary_color": "&H00F4F1EA",
        "emphasis_color": "&H003E5FFF",
        "outline_color": "&H00000000",
        "back_color": "&H00000000",
        "bold": 1,
        "outline_width": 5,
        "shadow": 3,
        "alignment": 2,
        "margin_v": 180,
        "emphasis_mode": True,
        "style_name": "dynamic",
        "words_per_card": 4,
        "pop_scale": True,
        "emoji_enabled": True,
    },
    # ==========================================================================
    # NEW: Hinglish-aware preset.
    # When `hinglish_mode=True` AND the clip is detected as code-switched,
    # the renderer uses the language_map to color words by language:
    #   - English words: white
    #   - Hindi words (Devanagari OR Roman): coral (the accent color)
    #   - Filler words: dimmed gray (de-emphasized but still readable)
    #   - Numbers: white but bold (stands out as a stat)
    # Plus emphasis words get the bigger size + accent color.
    # If the clip is NOT detected as Hinglish, falls back to "clean" preset behavior.
    # ==========================================================================
    "hinglish": {
        "font": "Montserrat",
        "font_hindi": "Noto Sans Devanagari",
        "font_size": 56,
        "emphasis_size": 70,
        "primary_color": "&H00F4F1EA",          # cream-white for English
        "emphasis_color": "&H003E5FFF",         # coral (FF5F3E in BGR) for emphasis
        "hindi_color": "&H003E5FFF",            # coral for Hindi words (visual rhythm)
        "filler_color": "&H006B7280",           # muted gray for fillers (dim but readable)
        "outline_color": "&H00000000",
        "back_color": "&H00000000",
        "bold": 1,
        "outline_width": 5,
        "shadow": 3,
        "alignment": 2,
        "margin_v": 180,
        "emphasis_mode": True,
        "hinglish_mode": True,                  # triggers language-aware rendering
        "style_name": "hinglish",
        "words_per_card": 4,
        "pop_scale": False,                     # Hinglish reads cleaner without pop
        "emoji_enabled": False,                 # libass can't render color emojis
    },
}


def get_caption_preset(name: str) -> dict:
    return CAPTION_PRESETS.get(name, CAPTION_PRESETS["hormozi"])


def _is_devanagari(text: str) -> bool:
    """Return True if text contains Devanagari characters."""
    return any("\u0900" <= c <= "\u097F" for c in text)


def _pick_font(preset: dict, detected_language: str, sample_text: str) -> str:
    """
    Decide which font to use:
      - If language is 'hi' OR text contains Devanagari → use Hindi font
      - Otherwise → preset's default font
    """
    lang = (detected_language or "").lower()
    needs_devanagari = lang in ("hi", "hin", "hindi") or _is_devanagari(sample_text)
    if needs_devanagari:
        return preset.get("font_hindi", "Noto Sans Devanagari")
    return preset["font"]


# =============================================================================
# Video/quality config
# =============================================================================
CRF = 26

ASPECT_RATIOS = {
    "9:16": (1080, 1920),
    "1:1":  (1080, 1080),
    "16:9": (1920, 1080),
}
VALID_CROP_POSITIONS = {"center", "top", "bottom", "left", "right"}


def _build_scale_crop_filter(out_w: int, out_h: int, position: str) -> str:
    scale = (
        f"scale=w='if(gt(a,{out_w}/{out_h}),-2,{out_w})':"
        f"h='if(gt(a,{out_w}/{out_h}),{out_h},-2)'"
    )
    if position == "center":
        crop_x, crop_y = "(iw-ow)/2", "(ih-oh)/2"
    elif position == "top":
        crop_x, crop_y = "(iw-ow)/2", "0"
    elif position == "bottom":
        crop_x, crop_y = "(iw-ow)/2", "ih-oh"
    elif position == "left":
        crop_x, crop_y = "0", "(ih-oh)/2"
    elif position == "right":
        crop_x, crop_y = "iw-ow", "(ih-oh)/2"
    else:
        crop_x, crop_y = "(iw-ow)/2", "(ih-oh)/2"
    return f"{scale},crop={out_w}:{out_h}:{crop_x}:{crop_y}"


# =============================================================================
# ASS subtitle generation
# =============================================================================
def _seconds_to_ass_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
            .replace("{", "\\{")
            .replace("}", "\\}")
    )


def _build_ass_subtitle_file(
    segments_in_clip: list[dict],
    clip_start: float,
    output_path: Path,
    video_width: int,
    video_height: int,
    preset: dict,
    font_name: str,
    # Session B1: per-clip emphasis map (indices into flat word list)
    emphasis_data: dict | None = None,
) -> None:
    """Generate ASS subtitle file with preset styling. font_name is chosen per language.

    Session B1: If preset has emphasis_mode=True, we use a new builder that
    supports per-word emphasis styling + emoji injection.
    """

    # Session B1: emphasis_mode takes a different header (has Emphasis style row)
    if preset.get("emphasis_mode"):
        _build_ass_emphasis_file(
            segments_in_clip, clip_start, output_path,
            video_width, video_height, preset, font_name,
            emphasis_data or {"emphasis_indices": [], "emoji_map": {}},
        )
        return

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{font_name},{preset['font_size']},{preset['primary_color']},{preset['secondary_color']},{preset['outline_color']},{preset['back_color']},{preset['bold']},0,0,0,100,100,0,0,1,{preset['outline_width']},{preset['shadow']},{preset['alignment']},60,60,{preset['margin_v']},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    events = []
    if preset.get("one_word_per_line"):
        events.extend(_build_one_word_events(segments_in_clip, clip_start, preset))
    else:
        events.extend(_build_phrase_events(segments_in_clip, clip_start, preset))

    output_path.write_text(header + "\n".join(events), encoding="utf-8")


def _build_ass_emphasis_file(
    segments_in_clip: list[dict],
    clip_start: float,
    output_path: Path,
    video_width: int,
    video_height: int,
    preset: dict,
    font_name: str,
    emphasis_data: dict,
) -> None:
    """
    Session B1: Build ASS file for the 'viral', 'clean', 'dynamic' presets.

    Approach (Submagic-style):
      - Group words by SEGMENT (natural phrase boundary) — full context stays on screen
      - Within a segment, words reveal progressively word-by-word via karaoke (\\k)
      - If a segment has too many words for readability, chunk into sub-groups
        of `words_per_card` words (so long run-on segments still look clean)
      - Emphasis words get a different style (coral, bigger)
      - Emojis appear right after their word
      - `pop_scale=True` adds \\fad() entrance for each progressive reveal

    This produces captions that look like Submagic: phrase stays on screen with
    natural context, individual words pop/highlight as spoken, emphasis stands out.
    """
    emphasis_indices = set(emphasis_data.get("emphasis_indices", []))
    emoji_map = emphasis_data.get("emoji_map", {})  # keys are string indices

    # Hinglish mode: per-word language tagging
    is_hinglish = bool(preset.get("hinglish_mode")) and bool(emphasis_data.get("is_hinglish"))
    language_map: dict[int, str] = emphasis_data.get("language_map", {}) or {}
    # Normalize keys to int (Sonnet may return str-keyed dict)
    language_map = {int(k): str(v) for k, v in language_map.items() if str(v)}
    filler_indices = set(emphasis_data.get("filler_indices", []))

    base_size = int(preset["font_size"])
    emph_size = int(preset.get("emphasis_size", base_size))

    primary = preset["primary_color"]
    emphasis_color = preset["emphasis_color"]
    hindi_color = preset.get("hindi_color", emphasis_color)
    filler_color = preset.get("filler_color", "&H00808080")  # default gray
    outline = preset["outline_color"]
    bold = preset["bold"]
    outline_w = preset["outline_width"]
    shadow = preset["shadow"]
    alignment = preset["alignment"]
    margin_v = preset["margin_v"]

    # ASS V4+ Style format. We define multiple styles so words can be re-styled
    # mid-line via {\rStyleName} inline tags.
    style_lines = [
        # Default = English/neutral words: cream-white at base size
        f"Style: Default,{font_name},{base_size},{primary},&H000000FF,{outline},&H00000000,{bold},0,0,0,100,100,0,0,1,{outline_w},{shadow},{alignment},60,60,{margin_v},1",
        # Emphasis = "money words": coral, BIGGER, always bold
        f"Style: Emphasis,{font_name},{emph_size},{emphasis_color},&H000000FF,{outline},&H00000000,1,0,0,0,100,100,0,0,1,{outline_w},{shadow},{alignment},60,60,{margin_v},1",
    ]
    if is_hinglish:
        # Hindi = Hindi words (Devanagari OR Roman): coral, base size — visually
        # signals "this is the Hindi part" without making it bigger (which would
        # break rhythm with surrounding English words).
        style_lines.append(
            f"Style: Hindi,{font_name},{base_size},{hindi_color},&H000000FF,{outline},&H00000000,{bold},0,0,0,100,100,0,0,1,{outline_w},{shadow},{alignment},60,60,{margin_v},1"
        )
        # Filler = "matlab", "you know", "umm": muted gray, slightly smaller
        # so the eye glides past them and focuses on real words.
        filler_size = max(int(base_size * 0.85), 32)
        style_lines.append(
            f"Style: Filler,{font_name},{filler_size},{filler_color},&H000000FF,{outline},&H00000000,0,0,0,0,100,100,0,0,1,{outline_w},{shadow},{alignment},60,60,{margin_v},1"
        )
        # Number = "₹500", "10x", "90%": white but bold — stands out as a stat
        style_lines.append(
            f"Style: Number,{font_name},{base_size},{primary},&H000000FF,{outline},&H00000000,1,0,0,0,100,100,0,0,1,{outline_w},{shadow},{alignment},60,60,{margin_v},1"
        )

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {video_width}\n"
        f"PlayResY: {video_height}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(style_lines) + "\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    words_per_card = int(preset.get("words_per_card", 3))
    pop_scale = bool(preset.get("pop_scale", False))
    emoji_enabled = bool(preset.get("emoji_enabled", False))

    # Build flat word list per SEGMENT. Emphasis indices are global across all segments.
    # Rendering uses a sliding window: for each word W spoken, show W plus previous
    # (words_per_card - 1) words FROM THE SAME SEGMENT. This prevents words from being
    # orphaned at card boundaries (the "erectile disfunction → erectile only" bug).
    flat_words: list[dict] = []
    segment_slices: list[tuple[int, int]] = []  # (start_abs, end_abs_exclusive) per segment

    for seg in segments_in_clip:
        seg_words_src = seg.get("words") or []
        seg_words: list[dict] = []
        if seg_words_src:
            for w in seg_words_src:
                wt = str(w.get("word", "")).strip()
                if not wt:
                    continue
                seg_words.append({
                    "word": wt,
                    "start": float(w.get("start", seg.get("start", 0))),
                    "end": float(w.get("end", seg.get("end", 0))),
                })
        else:
            text = str(seg.get("text", "")).strip()
            if not text:
                continue
            parts = text.split()
            if not parts:
                continue
            s = float(seg.get("start", 0))
            e = float(seg.get("end", s + 1))
            per = (e - s) / max(1, len(parts))
            for j, p in enumerate(parts):
                seg_words.append({
                    "word": p,
                    "start": s + j * per,
                    "end": s + (j + 1) * per,
                })

        if not seg_words:
            continue

        start_abs = len(flat_words)
        flat_words.extend(seg_words)
        segment_slices.append((start_abs, len(flat_words)))

    if not flat_words or not segment_slices:
        output_path.write_text(header, encoding="utf-8")
        return

    events = []

    # Session B2 fix: SLIDING WINDOW. For each spoken word at abs index `j`:
    #   - window = max(seg_start, j - words_per_card + 1) .. j inclusive
    #   - this guarantees: (a) current word always visible (no cut-off bug),
    #                      (b) last N words always visible when possible (no orphan cards),
    #                      (c) never crosses a sentence boundary (clean segment break).
    for seg_start_abs, seg_end_abs in segment_slices:
        for j in range(seg_start_abs, seg_end_abs):
            # Window start: N words back, but not before this segment's start
            win_start = max(seg_start_abs, j - words_per_card + 1)

            pieces = []
            for k in range(win_start, j + 1):
                kw = flat_words[k]
                word_text = _escape_ass_text(kw["word"])
                is_emph = k in emphasis_indices

                # Pick the per-word style. Priority:
                #   1. Emphasis (always wins — these are the "money words")
                #   2. Number (in Hinglish mode — make stats stand out)
                #   3. Filler (in Hinglish mode — dim weak words)
                #   4. Hindi (in Hinglish mode — Devanagari OR Roman Hindi)
                #   5. Default (English / neutral / no Hinglish data)
                if is_emph:
                    style_tag = "Emphasis"
                elif is_hinglish:
                    lang = language_map.get(k, "")
                    if lang == "number":
                        style_tag = "Number"
                    elif lang == "filler" or k in filler_indices:
                        style_tag = "Filler"
                    elif lang in ("hindi_dev", "hindi_rom"):
                        style_tag = "Hindi"
                    else:
                        style_tag = "Default"
                else:
                    style_tag = "Default"

                if style_tag != "Default":
                    piece = f"{{\\r{style_tag}}}{word_text}{{\\rDefault}}"
                else:
                    piece = word_text
                pieces.append(piece)

            line_text = " ".join(pieces)

            if pop_scale:
                line_text = "{\\fad(90,0)}" + line_text

            # Time window: this line visible from word j's start until:
            #   - next word j+1's start (still in same segment), or
            #   - last word's end + 0.25s buffer (if j is the segment's last word)
            line_start_abs = flat_words[j]["start"]
            if j + 1 < seg_end_abs:
                line_end_abs = flat_words[j + 1]["start"]
            else:
                line_end_abs = flat_words[j]["end"] + 0.25

            line_start_rel = max(0, line_start_abs - clip_start)
            line_end_rel = max(line_start_rel + 0.08, line_end_abs - clip_start)

            events.append(
                f"Dialogue: 0,"
                f"{_seconds_to_ass_time(line_start_rel)},"
                f"{_seconds_to_ass_time(line_end_rel)},"
                f"Default,,0,0,0,,{line_text}"
            )

    output_path.write_text(header + "\n".join(events), encoding="utf-8")


def _build_phrase_events(segments_in_clip: list[dict], clip_start: float, preset: dict) -> list[str]:
    events = []
    for seg in segments_in_clip:
        seg_text = seg["text"].strip()
        if not seg_text:
            continue
        local_start = max(0, seg["start"] - clip_start)
        local_end = max(local_start + 0.1, seg["end"] - clip_start)
        words = seg.get("words", [])

        if preset.get("use_karaoke") and words:
            parts = []
            for w in words:
                w_text = _escape_ass_text(w.get("word", "").strip())
                if not w_text:
                    continue
                w_dur_cs = int(max(10, (w["end"] - w["start"]) * 100))
                parts.append(f"{{\\kf{w_dur_cs}}}{w_text}")
            ass_text = " ".join(parts)
        else:
            ass_text = _escape_ass_text(seg_text)

        events.append(
            f"Dialogue: 0,"
            f"{_seconds_to_ass_time(local_start)},"
            f"{_seconds_to_ass_time(local_end)},"
            f"Default,,0,0,0,,{ass_text}"
        )
    return events


def _build_one_word_events(segments_in_clip: list[dict], clip_start: float, preset: dict) -> list[str]:
    events = []
    for seg in segments_in_clip:
        words = seg.get("words", [])
        if not words:
            seg_text = seg["text"].strip()
            if not seg_text:
                continue
            local_start = max(0, seg["start"] - clip_start)
            local_end = max(local_start + 0.1, seg["end"] - clip_start)
            events.append(
                f"Dialogue: 0,"
                f"{_seconds_to_ass_time(local_start)},"
                f"{_seconds_to_ass_time(local_end)},"
                f"Default,,0,0,0,,{_escape_ass_text(seg_text)}"
            )
            continue

        for w in words:
            w_text = w.get("word", "").strip()
            if not w_text:
                continue
            ws = max(0, w["start"] - clip_start)
            we = max(ws + 0.15, w["end"] - clip_start)
            # Only uppercase for Latin text; leave Devanagari as-is
            display_text = w_text if _is_devanagari(w_text) else w_text.upper()
            events.append(
                f"Dialogue: 0,"
                f"{_seconds_to_ass_time(ws)},"
                f"{_seconds_to_ass_time(we)},"
                f"Default,,0,0,0,,{_escape_ass_text(display_text)}"
            )
    return events


# =============================================================================
# Render single clip
# =============================================================================
def render_one_clip(
    source: Path,
    clip: dict,
    transcript_segments: list[dict],
    output_dir: Path,
    job_id: uuid.UUID,
    aspect_ratio: str = "9:16",
    crop_position: str = "center",
    caption_preset: str = "hormozi",
    detected_language: str = "en",
    user_crop: dict | None = None,
    fast_mode: bool = False,   # Step 13: single-clip re-render uses this
    # Session C additions:
    intro_outro: dict | None = None,     # {hook_line1, hook_line2, outro_line1, outro_line2, hook_accent_word}
    remove_silences: bool = False,       # apply silence removal before rendering
    # Session B1: per-clip emphasis map (for viral/clean/dynamic presets)
    emphasis_data: dict | None = None,
    # Session WM: apply free-tier watermark
    apply_watermark_flag: bool = False,
) -> tuple[Path, Path | None]:
    rank = clip["rank"]
    start = float(clip["start"])
    duration = float(clip["duration"])

    out_w, out_h = ASPECT_RATIOS.get(aspect_ratio, ASPECT_RATIOS["9:16"])
    if crop_position not in VALID_CROP_POSITIONS:
        crop_position = "center"

    # Session SC: face-aware smart cropping. For podcast / talking-head content,
    # this prevents the speaker's head from being chopped off when cropping
    # 16:9 → 9:16. Falls back to fixed `crop_position` if no faces are detected.
    try:
        from backend.pipeline.smart_crop import (
            build_smart_crop_filter, get_video_dimensions,
        )
        sw, sh = get_video_dimensions(source)
        if sw > 0 and sh > 0:
            scale_crop = build_smart_crop_filter(
                source_video=source,
                clip_start=start, clip_duration=duration,
                source_width=sw, source_height=sh,
                out_w=out_w, out_h=out_h,
                fallback_position=crop_position,
                user_crop=user_crop,
            )
        else:
            scale_crop = _build_scale_crop_filter(out_w, out_h, crop_position)
    except Exception as e:
        logger.warning(
            f"[{job_id}] clip #{rank}: smart crop failed ({e}), "
            f"using fixed crop_position={crop_position}"
        )
        scale_crop = _build_scale_crop_filter(out_w, out_h, crop_position)

    preset = get_caption_preset(caption_preset)

    clip_file = output_dir / f"clip_{rank:02d}.mp4"
    thumb_file = output_dir / f"clip_{rank:02d}_thumb.jpg"
    subtitle_file = output_dir / f"clip_{rank:02d}.ass"

    # Temp intermediate files (cleaned up at end)
    core_file = output_dir / f"_clip_{rank:02d}_core.mp4"           # before silence removal + concat
    tightened_file = output_dir / f"_clip_{rank:02d}_tight.mp4"     # after silence removal
    hook_file = output_dir / f"_clip_{rank:02d}_hook.mp4"
    outro_file = output_dir / f"_clip_{rank:02d}_outro.mp4"

    segments_in_clip = [
        s for s in transcript_segments
        if s["end"] > start and s["start"] < start + duration
    ]

    # Pick the right font based on language + actual text content
    sample_text = " ".join(s.get("text", "") for s in segments_in_clip[:5])
    font_name = _pick_font(preset, detected_language, sample_text)

    _build_ass_subtitle_file(
        segments_in_clip, start, subtitle_file,
        out_w, out_h, preset, font_name,
        emphasis_data=emphasis_data,
    )

    # scale_crop already computed above (smart-crop or fallback).
    ass_path = str(subtitle_file).replace("\\", "/").replace(":", "\\:")
    vf = f"{scale_crop},ass='{ass_path}'"

    # Step 13: Fast mode for re-renders — use all CPU cores + slightly higher CRF.
    # Normal rendering uses 4 threads (5 clips render in parallel would otherwise
    # saturate CPU). Re-render processes ONE clip so we can use "0" (= all cores).
    threads = "0" if fast_mode else "4"
    crf_value = 28 if fast_mode else CRF   # CRF 28 is ~25% faster with near-identical quality

    # Render the core clip — target is core_file if we'll concat, else direct to clip_file
    core_target = core_file if (intro_outro or remove_silences) else clip_file

    cmd = [
        "ffmpeg", "-y",
        "-threads", threads,
        "-ss", f"{start:.2f}",
        "-i", str(source),
        "-t", f"{duration:.2f}",
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "fastdecode",
        "-crf", str(crf_value),
        "-c:a", "aac",
        "-b:a", "96k",
        "-movflags", "+faststart",
        "-loglevel", "error",
        str(core_target),
    ]

    logger.info(
        f"[{job_id}] rendering clip #{rank} "
        f"({start:.1f}s +{duration:.1f}s, {aspect_ratio}/{crop_position}, "
        f"preset={caption_preset}, font={font_name}"
        f"{', FAST' if fast_mode else ''}"
        f"{', +hook/outro' if intro_outro else ''}"
        f"{', -silences' if remove_silences else ''})"
    )
    try:
        subprocess.run(cmd, check=True, timeout=300, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise RenderError(f"ffmpeg clip #{rank} failed: {e.stderr.decode()[:500]}") from e
    except subprocess.TimeoutExpired:
        raise RenderError(f"ffmpeg clip #{rank} timeout")

    try:
        subtitle_file.unlink(missing_ok=True)
    except OSError:
        pass

    # ============================================================
    # Session C: Silence removal pass
    # ============================================================
    pre_concat_file = core_target  # the file to feed into hook/outro concat
    if remove_silences and core_target != clip_file:
        try:
            from backend.pipeline.silence import remove_silences as _remove_silences_impl
            stats = _remove_silences_impl(core_file, tightened_file, job_id)
            if not stats.get("skipped"):
                pre_concat_file = tightened_file
                logger.info(
                    f"[{job_id}] clip #{rank}: silence removal reduced "
                    f"{stats['original_duration']:.1f}s → {stats['new_duration']:.1f}s "
                    f"({stats['reduction_pct']}% tighter)"
                )
            else:
                logger.info(f"[{job_id}] clip #{rank}: silence removal skipped ({stats.get('reason', '')})")
        except Exception as e:
            logger.warning(f"[{job_id}] clip #{rank}: silence removal errored ({e}), continuing without")

    # ============================================================
    # Session C: Pre-hook + outro cards (concat)
    # ============================================================
    if intro_outro:
        try:
            _render_hook_card(
                intro_outro=intro_outro,
                output_file=hook_file,
                out_w=out_w, out_h=out_h,
                duration_sec=3.0,
                job_id=job_id, rank=rank,
                font_name=font_name,
            )
            _render_outro_card(
                intro_outro=intro_outro,
                output_file=outro_file,
                out_w=out_w, out_h=out_h,
                duration_sec=2.0,
                job_id=job_id, rank=rank,
                font_name=font_name,
            )
            _concat_three(
                hook_file, pre_concat_file, outro_file,
                output_file=clip_file,
                out_w=out_w, out_h=out_h,
                job_id=job_id, rank=rank,
            )
        except Exception as e:
            logger.warning(
                f"[{job_id}] clip #{rank}: hook/outro failed ({e}), "
                f"falling back to core clip only"
            )
            # Fallback: if concat failed but we have the core, use that as output
            if pre_concat_file.exists() and pre_concat_file != clip_file:
                import shutil
                try:
                    shutil.copyfile(pre_concat_file, clip_file)
                except OSError:
                    pass
    elif pre_concat_file != clip_file:
        # No hook/outro but silence was removed — copy tightened to final
        import shutil
        try:
            shutil.copyfile(pre_concat_file, clip_file)
        except OSError as e:
            raise RenderError(f"failed to finalize silence-removed clip: {e}") from e

    # Clean up intermediates
    for temp in (core_file, tightened_file, hook_file, outro_file):
        try:
            if temp.exists() and temp != clip_file:
                temp.unlink(missing_ok=True)
        except OSError:
            pass

    # ============================================================
    # Session WM: Free-tier watermark (last stage before thumbnail)
    # ============================================================
    # Watermark is applied AFTER all composition (captions, silence removal, hook/outro)
    # so it sits on top of everything in the final corner. Applied in-place by writing
    # to a temp file and atomically replacing clip_file.
    if apply_watermark_flag:
        try:
            from backend.pipeline.watermark import apply_watermark, WatermarkError
            wm_temp = clip_file.with_suffix(".wm_pending.mp4")
            apply_watermark(
                input_clip=clip_file,
                output_clip=wm_temp,
                job_id=job_id,
                rank=rank,
            )
            # Atomic replace: move temp over original
            import shutil
            shutil.move(str(wm_temp), str(clip_file))
            logger.info(f"[{job_id}] clip #{rank}: watermark applied")
        except WatermarkError as e:
            # Don't fail the whole render if watermark fails — just log and continue
            # with the clean clip. Better to ship a clean clip than fail the job.
            logger.warning(
                f"[{job_id}] clip #{rank}: watermark failed ({e}), "
                f"shipping clip without watermark"
            )
        except Exception as e:
            logger.warning(
                f"[{job_id}] clip #{rank}: unexpected watermark error ({e}), "
                f"shipping clip without watermark"
            )

    # Thumbnail (pulled from clip midpoint of FINAL file)
    try:
        final_duration = _get_video_duration(clip_file)
    except Exception:
        final_duration = duration
    thumb_cmd = [
        "ffmpeg", "-y", "-ss", f"{final_duration/2:.2f}",
        "-i", str(clip_file), "-frames:v", "1",
        "-q:v", "3", "-loglevel", "error", str(thumb_file),
    ]
    try:
        subprocess.run(thumb_cmd, check=True, timeout=30, capture_output=True)
    except subprocess.CalledProcessError:
        logger.warning(f"[{job_id}] thumbnail failed for clip #{rank}")
        thumb_file = None

    return clip_file, thumb_file


# =============================================================================
# Session C: Hook card, outro card, concat helpers
# =============================================================================
def _get_video_duration(video_file: Path) -> float:
    """Get video duration via ffprobe. Used for accurate thumbnail midpoint."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(video_file)],
        check=True, capture_output=True, timeout=30, text=True,
    )
    return float(result.stdout.strip())


def _escape_drawtext(s: str) -> str:
    """
    Escape a string for use in ffmpeg drawtext filter `text=` parameter.

    drawtext expects the value to be safe within a filter-graph expression.
    Characters requiring escape inside single-quoted text:
      - backslash        → \\\\
      - single quote     → (we drop these entirely for safety)
      - colon            → \\:
      - comma            → \\,
      - square brackets  → \\[ \\]
      - percent          → \\% (drawtext treats % as strftime start otherwise)
    """
    s = s.replace("\\", "\\\\")
    s = s.replace("'", "")            # drop single quotes (safest)
    s = s.replace(":", "\\:")
    s = s.replace(",", "\\,")
    s = s.replace("[", "\\[")
    s = s.replace("]", "\\]")
    s = s.replace("%", "\\%")
    return s


def _compute_text_fontsize(text: str, out_w: int, max_width_ratio: float = 0.88,
                            base_size: int = 110, min_size: int = 40) -> int:
    """
    Choose a fontsize such that the text fits within `out_w * max_width_ratio`.
    Rough heuristic: average glyph width ≈ fontsize * 0.52 (for serif italic).
    """
    max_px = int(out_w * max_width_ratio)
    char_count = max(1, len(text))
    # Avg glyph width for serif italic ~= 0.52 * fontsize
    # So: char_count * 0.52 * fontsize <= max_px
    max_size_from_width = int(max_px / (char_count * 0.52))
    return max(min_size, min(base_size, max_size_from_width))


# Font path candidates — we try Noto first (handles Hindi + Unicode arrows best),
# then DejaVu (shipped in most Debian-based Docker images), then others.
_HOOK_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSerif-Italic.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSerifItalic.ttf",
]
_HOOK_FONT_HINDI_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSerifDevanagari-Regular.ttf",
]


def _pick_hook_fontfile(text: str) -> str | None:
    """Pick a fontfile that supports the given text. Returns None if nothing works."""
    has_devanagari = any("\u0900" <= ch <= "\u097F" for ch in text)
    candidates = _HOOK_FONT_HINDI_CANDIDATES if has_devanagari else _HOOK_FONT_CANDIDATES
    for path in candidates:
        if Path(path).exists():
            return path
    # Last resort: try the other set
    for path in (_HOOK_FONT_CANDIDATES if has_devanagari else _HOOK_FONT_HINDI_CANDIDATES):
        if Path(path).exists():
            return path
    return None


def _render_hook_card(
    intro_outro: dict,
    output_file: Path,
    out_w: int, out_h: int,
    duration_sec: float,
    job_id: uuid.UUID, rank: int,
    font_name: str = "",
) -> None:
    """
    Render a 3-second pre-hook card using textfile= for zero-escape text.
    """
    line1 = intro_outro.get("hook_line1", "").strip()
    line2 = intro_outro.get("hook_line2", "").strip()

    fontfile = _pick_hook_fontfile(line1 + " " + line2)
    if not fontfile:
        raise RenderError("No suitable fontfile found for hook card")

    l1_size = _compute_text_fontsize(line1, out_w, max_width_ratio=0.88,
                                     base_size=int(out_h * 0.055), min_size=48)
    l2_size = _compute_text_fontsize(line2, out_w, max_width_ratio=0.80,
                                     base_size=int(out_h * 0.030), min_size=30)

    l1_y = int(out_h * 0.44)
    l2_y = int(out_h * 0.54)

    # Write line texts to temp files - ffmpeg reads them literally, no escaping needed.
    txt_dir = output_file.parent
    l1_txt = txt_dir / f"_hook_{rank:02d}_l1.txt"
    l2_txt = txt_dir / f"_hook_{rank:02d}_l2.txt"
    l1_txt.write_text(line1, encoding="utf-8")
    l2_txt.write_text(line2, encoding="utf-8")

    try:
        # Build filter chain. textfile= syntax means NO escaping needed for content.
        # Only path escaping: drawtext needs : and \ escaped in filter syntax.
        def _escape_path(p: str) -> str:
            return p.replace("\\", "/").replace(":", "\\:")

        fontfile_esc = _escape_path(fontfile)
        l1_path_esc = _escape_path(str(l1_txt))
        l2_path_esc = _escape_path(str(l2_txt))

        chain = (
            f"color=c=black:s={out_w}x{out_h}:d={duration_sec}:r=30,"
            f"drawtext=fontfile={fontfile_esc}:expansion=none:textfile={l1_path_esc}"
            f":fontcolor=0xF4F1EA:fontsize={l1_size}"
            f":x=(w-text_w)/2:y={l1_y}"
            f":alpha='if(lt(t\\,0.3)\\,t/0.3\\,1)',"
            f"drawtext=fontfile={fontfile_esc}:expansion=none:textfile={l2_path_esc}"
            f":fontcolor=0xFF5F3E:fontsize={l2_size}"
            f":x=(w-text_w)/2:y={l2_y}"
            f":alpha='if(lt(t\\,0.5)\\,0\\,min(1\\,(t-0.5)/0.3))'"
        )

        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", chain,
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration_sec}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            "-t", f"{duration_sec}",
            "-loglevel", "error",
            str(output_file),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=60, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise RenderError(
                f"hook card render failed for #{rank}: {e.stderr.decode()[:400]}"
            ) from e
    finally:
        # Clean up temp text files
        for t in (l1_txt, l2_txt):
            try:
                t.unlink(missing_ok=True)
            except OSError:
                pass


def _render_outro_card(
    intro_outro: dict,
    output_file: Path,
    out_w: int, out_h: int,
    duration_sec: float,
    job_id: uuid.UUID, rank: int,
    font_name: str = "",
) -> None:
    """
    Render a 2-second outro card using textfile= for zero-escape text.
    """
    line1 = intro_outro.get("outro_line1", "").strip()
    line2 = intro_outro.get("outro_line2", "").strip()

    fontfile = _pick_hook_fontfile(line1 + " " + line2)
    if not fontfile:
        raise RenderError("No suitable fontfile found for outro card")

    l1_size = _compute_text_fontsize(line1, out_w, max_width_ratio=0.85,
                                     base_size=int(out_h * 0.045), min_size=42)
    l2_size = _compute_text_fontsize(line2, out_w, max_width_ratio=0.80,
                                     base_size=int(out_h * 0.032), min_size=30)

    l1_y = int(out_h * 0.44)
    l2_y = int(out_h * 0.54)

    txt_dir = output_file.parent
    l1_txt = txt_dir / f"_outro_{rank:02d}_l1.txt"
    l2_txt = txt_dir / f"_outro_{rank:02d}_l2.txt"
    l1_txt.write_text(line1, encoding="utf-8")
    l2_txt.write_text(line2, encoding="utf-8")

    try:
        def _escape_path(p: str) -> str:
            return p.replace("\\", "/").replace(":", "\\:")

        fontfile_esc = _escape_path(fontfile)
        l1_path_esc = _escape_path(str(l1_txt))
        l2_path_esc = _escape_path(str(l2_txt))

        chain = (
            f"color=c=black:s={out_w}x{out_h}:d={duration_sec}:r=30,"
            f"drawtext=fontfile={fontfile_esc}:expansion=none:textfile={l1_path_esc}"
            f":fontcolor=0xF4F1EA:fontsize={l1_size}"
            f":x=(w-text_w)/2:y={l1_y}"
            f":alpha='if(lt(t\\,0.2)\\,t/0.2\\,1)',"
            f"drawtext=fontfile={fontfile_esc}:expansion=none:textfile={l2_path_esc}"
            f":fontcolor=0xFF5F3E:fontsize={l2_size}"
            f":x=(w-text_w)/2:y={l2_y}"
            f":alpha='if(lt(t\\,0.4)\\,0\\,min(1\\,(t-0.4)/0.25))'"
        )

        cmd = [
            "ffmpeg", "-y",
            "-f", "lavfi", "-i", chain,
            "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={duration_sec}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "96k",
            "-t", f"{duration_sec}",
            "-loglevel", "error",
            str(output_file),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=60, capture_output=True)
        except subprocess.CalledProcessError as e:
            raise RenderError(
                f"outro card render failed for #{rank}: {e.stderr.decode()[:400]}"
            ) from e
    finally:
        for t in (l1_txt, l2_txt):
            try:
                t.unlink(missing_ok=True)
            except OSError:
                pass


def _concat_three(
    hook: Path, core: Path, outro: Path,
    output_file: Path,
    out_w: int, out_h: int,
    job_id: uuid.UUID, rank: int,
) -> None:
    """
    Concatenate three videos: hook → core → outro.
    Uses concat filter (not demuxer) because the three have different encodes.
    Re-encodes output to keep format consistent.
    """
    cmd = [
        "ffmpeg", "-y",
        "-i", str(hook),
        "-i", str(core),
        "-i", str(outro),
        "-filter_complex",
        # normalize all three to out_w x out_h, stereo 44100 audio, then concat
        f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v0];"
        f"[1:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v1];"
        f"[2:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease,"
        f"pad={out_w}:{out_h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30[v2];"
        f"[0:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a0];"
        f"[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a1];"
        f"[2:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a2];"
        f"[v0][a0][v1][a1][v2][a2]concat=n=3:v=1:a=1[outv][outa]",
        "-map", "[outv]", "-map", "[outa]",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        "-loglevel", "error",
        str(output_file),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=180, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise RenderError(
            f"concat hook+core+outro failed for #{rank}: {e.stderr.decode()[:500]}"
        ) from e


def render_all_clips(
    source_video: Path,
    clips: list[dict],
    transcript_segments: list[dict],
    user_id: uuid.UUID,
    job_id: uuid.UUID,
    aspect_ratio: str = "9:16",
    crop_position: str = "center",
    caption_preset: str = "hormozi",
    detected_language: str = "en",
    progress_callback=None,
    # Session C:
    add_hook_outro: bool = False,     # apply pre-hook + outro card to every clip
    remove_silences: bool = False,    # apply silence removal to every clip
    # Session WM: free-tier watermark gating
    user_plan: str = "free",          # "free" | "starter" | "pro" | "agency"
    user_lifetime_clips: int = 0,     # how many clips this user has generated BEFORE this job
) -> list[dict]:
    """Render all clips. Language-aware font selection.

    Watermark policy:
      - For each clip in this job, we compute its lifetime index
        (= user_lifetime_clips + this clip's 0-based index in the job)
      - Pass that to should_apply_watermark() to decide.
      - This means if user has 0 lifetime clips and we render 5,
        clip[0] is lifetime #0 (free), clip[1] is #1 (watermarked, with default threshold=1).
    """
    from backend.pipeline.watermark import should_apply_watermark
    output_dir = clip_output_dir(user_id, job_id)
    output_dir.mkdir(parents=True, exist_ok=True)

    total = len(clips)
    results = []

    for idx, clip in enumerate(clips, start=1):
        try:
            # Session C: per-clip intro_outro must have been populated upstream (in worker)
            intro_outro = clip.get("intro_outro") if add_hook_outro else None
            # Session B1: per-clip emphasis data (populated upstream if preset needs it)
            emphasis_data = clip.get("emphasis_data")
            # Session WM: this clip's position in the job (0-based) determines
            # whether it gets the "free preview" treatment. Combined with lifetime
            # count for anti-abuse backstop.
            wm_flag = should_apply_watermark(
                user_plan=user_plan,
                user_clips_lifetime=user_lifetime_clips + (idx - 1),
                clip_index_in_job=(idx - 1),
            )
            clip_path, thumb_path = render_one_clip(
                source_video, clip, transcript_segments, output_dir, job_id,
                aspect_ratio=aspect_ratio,
                crop_position=crop_position,
                caption_preset=caption_preset,
                detected_language=detected_language,
                intro_outro=intro_outro,
                remove_silences=remove_silences,
                emphasis_data=emphasis_data,
                apply_watermark_flag=wm_flag,
            )
            clip["file_path"] = str(clip_path)
            # Track in clip metadata so frontend can show "Free Preview" / "Watermarked"
            clip["watermarked"] = wm_flag
            clip["thumbnail_path"] = str(thumb_path) if thumb_path else None
            results.append(clip)
            if progress_callback:
                try:
                    progress_callback(len(results), total)
                except Exception as e:
                    logger.warning(f"[{job_id}] progress callback failed: {e}")
        except RenderError as e:
            logger.error(f"[{job_id}] skipping clip #{clip.get('rank')}: {e}")
            continue

    if not results:
        raise RenderError("All clips failed to render")

    logger.info(f"[{job_id}] rendered {len(results)}/{len(clips)} clips")
    return results


# =============================================================================
# Step 13: Single-clip re-render with edited transcript
# =============================================================================
def _merge_edits_with_original_words(
    original_segments: list[dict],
    edited_segments: list[dict],
) -> list[dict]:
    """
    Merge user's edited text with original Whisper word-level timestamps.

    Strategy:
      - Edited segments preserve their start/end times (from original, unchanged)
      - Text is replaced with user's version
      - Word-level timing: we re-distribute the ORIGINAL word timings proportionally
        across the edited words, so karaoke highlighting still works
      - This keeps sync tight even when users correct a few words

    Returns segments in the same format render expects.
    """
    merged = []

    # Map original segments by their time range for quick lookup
    for edited in edited_segments:
        edit_start = float(edited["start"])
        edit_end = float(edited["end"])
        edit_text = edited["text"].strip()

        # Find the matching original segment (closest start time)
        best_match = None
        best_delta = float("inf")
        for orig in original_segments:
            orig_start = float(orig.get("start", 0))
            delta = abs(orig_start - edit_start)
            if delta < best_delta:
                best_delta = delta
                best_match = orig

        new_seg = {
            "start": edit_start,
            "end": edit_end,
            "text": edit_text,
        }

        # If we have word timings from original, distribute them across new words
        if best_match and best_match.get("words") and edit_text:
            orig_words = best_match["words"]
            new_words_text = edit_text.split()

            if orig_words and new_words_text:
                # Total time span
                span_start = float(orig_words[0]["start"])
                span_end = float(orig_words[-1]["end"])
                total_span = max(0.1, span_end - span_start)

                # Distribute evenly based on word character length (approximates duration)
                char_counts = [max(1, len(w)) for w in new_words_text]
                total_chars = sum(char_counts)

                new_words = []
                cursor = span_start
                for i, word_text in enumerate(new_words_text):
                    word_duration = total_span * (char_counts[i] / total_chars)
                    new_words.append({
                        "word": word_text,
                        "start": cursor,
                        "end": cursor + word_duration,
                    })
                    cursor += word_duration

                new_seg["words"] = new_words

        merged.append(new_seg)

    return merged


def rerender_clip_with_edits(
    source: Path,
    clip_row,                    # SQLAlchemy Clip object (has start, end, etc.)
    output_dir: Path,
    job_id: uuid.UUID,
    aspect_ratio: str = "9:16",
    crop_position: str = "center",
    caption_preset: str = "hormozi",
    detected_language: str = "en",
) -> tuple[Path, Path | None]:
    """
    Re-render a SINGLE clip using edited_transcript from the DB.

    Returns (clip_file_path, thumbnail_path).
    Overwrites the existing clip file in place (same filename).

    Called by worker.rerender_clip_task — NOT by user directly.
    """
    original = list(clip_row.original_transcript or [])
    edited = list(clip_row.edited_transcript or [])

    if not edited:
        raise RenderError(
            f"Clip {clip_row.id} has no edited_transcript. Nothing to re-render."
        )

    # Build the transcript that the renderer will use
    merged_segments = _merge_edits_with_original_words(original, edited)

    if not merged_segments:
        raise RenderError(
            f"Clip {clip_row.id} produced 0 segments after merging edits."
        )

    # Build a fake "clip dict" in the format render_one_clip expects
    clip_dict = {
        "rank": clip_row.rank,
        "start": clip_row.start_sec,
        "end": clip_row.end_sec,
        "duration": clip_row.duration_sec,
        "title": clip_row.title,
        "hook": clip_row.hook,
    }

    logger.info(
        f"[rerender {clip_row.id}] {len(edited)} edited segments, "
        f"merged {len(merged_segments)}, preset={caption_preset}, lang={detected_language}"
    )

    # Session B1: If the target preset uses emphasis_mode, re-tag emphasis on the
    # edited transcript. Otherwise the rerender would produce captions without
    # emphasis even though the user picked viral/clean/dynamic.
    emphasis_data = None
    preset = get_caption_preset(caption_preset)
    if preset.get("emphasis_mode"):
        try:
            from backend.pipeline.caption_emphasis import (
                tag_emphasis_and_emojis, flatten_segments_to_words,
            )
            flat_words = flatten_segments_to_words(merged_segments)
            if flat_words:
                emphasis_data = tag_emphasis_and_emojis(
                    clip_words=flat_words,
                    clip_title=clip_row.title or "",
                    detected_language=detected_language,
                    enable_hinglish_mode=bool(preset.get("hinglish_mode")),
                )
        except Exception as e:
            logger.warning(
                f"[rerender {clip_row.id}] emphasis tagging failed ({e}), "
                f"rendering without emphasis"
            )

    return render_one_clip(
        source=source,
        clip=clip_dict,
        transcript_segments=merged_segments,
        output_dir=output_dir,
        job_id=job_id,
        aspect_ratio=aspect_ratio,
        crop_position=crop_position,
        caption_preset=caption_preset,
        detected_language=detected_language,
        emphasis_data=emphasis_data,
        fast_mode=True,   # Step 13: use all CPU cores for single-clip re-render
    )