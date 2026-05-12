"""
backend/pipeline/canvas.py

Session B2: Canvas wrapper — meme-style vertical composition.

Takes an already-rendered 9:16 clip and wraps it with:
  - Top region: either user text on black OR uploaded thumbnail image
  - Video region: the rendered clip scaled to fit
  - Bottom region: black padding

Final output is always 1080x1920 (9:16) so it matches other platform specs.

Layout ratios (top:video:bottom):
  - default:     20% : 60% : 20%  (balanced meme format, matches user's Image 1)
  - wide_top:    30% : 55% : 15%  (more top space for longer text)
  - image_top:   30% : 55% : 15%  (for thumbnail image)
"""
from __future__ import annotations

import logging
import subprocess
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

OUTPUT_W = 1080
OUTPUT_H = 1920


class CanvasError(Exception):
    pass


def _escape_path_for_filter(p: str) -> str:
    """Escape a path for use in an ffmpeg filter graph (backslashes + colons)."""
    return p.replace("\\", "/").replace(":", "\\:")


def _compute_text_fontsize(text: str, max_w: int,
                            base_size: int = 72, min_size: int = 32) -> int:
    """Estimate a fontsize that fits `text` within `max_w` pixels wide.

    Uses 0.62 avg-glyph-width heuristic (conservative for bold sans-serif).
    ALL CAPS text and narrow max_w get an extra safety margin to avoid overflow.
    """
    if not text:
        return base_size
    # Measure the longest line (split on newlines)
    lines = text.split("\n")
    longest_line = max((len(line.strip()) for line in lines), default=1)
    if longest_line < 1:
        return base_size

    # Check if mostly uppercase — caps letters are wider
    letters = [c for c in text if c.isalpha()]
    is_caps_heavy = letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6
    glyph_w = 0.68 if is_caps_heavy else 0.58

    # Reserve 4% extra horizontal padding for safety (descenders, antialiasing)
    effective_w = max_w * 0.96
    max_size_from_width = int(effective_w / (longest_line * glyph_w))
    return max(min_size, min(base_size, max_size_from_width))


def _pick_canvas_fontfile(text: str, bold: bool = True) -> str | None:
    """Pick a fontfile that supports the text + weight.

    Hindi text → Noto Sans Devanagari.
    Other text → Noto Sans / DejaVu / Liberation.
    bold=True → prefer Bold; bold=False → prefer Regular but fall through if missing.
    """
    has_devanagari = any("\u0900" <= ch <= "\u097F" for ch in text)
    if has_devanagari:
        primary = (
            ["/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf"]
            if bold else
            ["/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf"]
        )
        # Always have the alternate weight as a soft fallback
        secondary = [
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
            "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Bold.ttf",
        ]
    else:
        primary = (
            [
                "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
            ] if bold else [
                "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
            ]
        )
        secondary = [
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    for p in primary + secondary:
        if Path(p).exists():
            return p
    return None


def wrap_clip_with_canvas(
    input_clip: Path,
    output_clip: Path,
    job_id: uuid.UUID,
    rank: int,
    *,
    top_text: str = "",
    thumbnail_image: Path | None = None,
    layout: str = "default",
    # Session FINAL: text-styling controls (only used in text mode)
    text_size: str = "medium",   # "small" | "medium" | "large" — legacy enum
    text_size_mult: float = 1.0, # Session FIX-SLIDER: continuous size multiplier (0.5-2.0)
    text_bold: bool = True,      # whether to use bold weight (default true)
    text_underline: bool = False,  # underline the text
    text_color: str = "white",   # "white" | "red" | "yellow" | "green"
) -> dict:
    """
    Wrap a rendered clip with a canvas composition.

    Args:
      input_clip: the existing rendered 9:16 clip
      output_clip: where to write the new composition
      job_id: for logging
      rank: clip rank (1-5) for logging + temp filenames
      top_text: if non-empty, renders this as white text on black above the video
      thumbnail_image: if provided, overrides top_text and places the image above
                       the video (text is ignored if both supplied)
      layout: "default" (20:60:20) | "wide_top" (30:55:15) | "image_top" (30:55:15)

    Returns stats dict. Raises CanvasError on failure.
    """
    if not input_clip.exists():
        raise CanvasError(f"Input clip missing: {input_clip}")

    # If user provided neither text nor thumbnail, just copy input
    has_text = bool(top_text and top_text.strip())
    has_image = bool(thumbnail_image and thumbnail_image.exists())
    if not has_text and not has_image:
        logger.info(f"[{job_id}] canvas wrap: no text or image — skipping")
        _safe_copy(input_clip, output_clip)
        return {"skipped": True, "reason": "no text or image provided"}

    # Determine layout.
    # Reels/TikTok-native geometry — researched against actual Reels templates:
    #   - 18% top region (≈350px on 1920) — generous text margin, room for 1-3 lines
    #   - 4% bottom safe-zone — Instagram/TikTok HUD overlays the bottom ~50px
    #     during playback (caption icons, profile, like/share). Reserving 4%
    #     keeps the user's burned-in captions ABOVE that HUD strip.
    #   - 78% video region — preserves the FULL source video via CONTAIN scaling,
    #     so burned-in captions in the source NEVER get cropped off.
    if has_image:
        layout = "image_top"
    if layout == "wide_top":
        top_pct = 0.24          # bigger top for prominent headline
        bot_pct = 0.04
    elif layout == "image_top":
        top_pct = 0.28          # need room for thumbnail image
        bot_pct = 0.04
    else:  # default
        top_pct = 0.18          # comfortable margin without dominating frame
        bot_pct = 0.04          # safe-zone for platform HUD overlays

    # All dimensions must be even (h264 requirement). Round to nearest even.
    top_h = int(OUTPUT_H * top_pct) // 2 * 2
    bot_h = int(OUTPUT_H * bot_pct) // 2 * 2
    vid_h = OUTPUT_H - top_h - bot_h
    if vid_h % 2:
        vid_h -= 1              # ensure even
    vid_y = top_h               # video starts right below top region

    logger.info(
        f"[{job_id}] canvas wrap clip #{rank}: layout={layout} "
        f"(top={top_h}px vid={vid_h}px), "
        f"text={'yes' if has_text else 'no'}, image={'yes' if has_image else 'no'}"
    )

    # Build filter graph.
    # Inputs:
    #   [0:v] = input video (any aspect) → scale to fit vid region, keep aspect
    #   [1:v] = (optional) thumbnail image → scale to fit top region
    # We generate the black canvas from a color source and overlay layers on it.
    #
    # Strategy:
    #   1. Create a 1080x1920 black canvas (via color filter)
    #   2. Scale the video to fit vid_h height, keeping aspect ratio, pad to 1080 width
    #   3. Overlay the scaled video onto canvas at y=top_h
    #   4. If thumbnail: scale it to fit within top region, overlay at y=0
    #      Else if text: drawtext in top region, vertically centered
    #
    # We pass text via `textfile=` for safety (no escape headaches).

    # Temp files tracker
    temp_files: list[Path] = []
    txt_file: Path | None = None
    if has_text:
        txt_file = output_clip.parent / f"_canvas_{rank:02d}.txt"
        txt_file.write_text(top_text, encoding="utf-8")
        temp_files.append(txt_file)

    # Build ffmpeg command
    cmd = ["ffmpeg", "-y"]

    # Input 0: the video
    cmd += ["-i", str(input_clip)]

    # Input 1: thumbnail (optional)
    if has_image:
        cmd += ["-loop", "1", "-i", str(thumbnail_image)]

    # Filter complex.
    # CONTAIN scaling — preserves the ENTIRE source video, no cropping.
    # Why this matters: burned-in captions, faces near edges, all stay visible.
    # Trade-off: if the source aspect doesn't match the vid region aspect
    # exactly, small black bands fill the empty space. Since the canvas is
    # already black, these bands are visually seamless with the top/bottom
    # regions — looks intentional, not letterboxed.
    #
    # Mechanism:
    #   scale=W:H:force_original_aspect_ratio=decrease -> fits within bounds (no crop)
    #   pad=W:H:(ow-iw)/2:(oh-ih)/2:color=black        -> centers + fills with black
    # force_divisible_by=2 ensures the scaled dimensions are even (h264 req).
    # Without this, aspect-preserving scale can produce odd widths (e.g. 843)
    # which crash libx264 AND can violate pad's "smaller than input" constraint.
    video_scale = (
        f"[0:v]scale={OUTPUT_W}:{vid_h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
        f"pad={OUTPUT_W}:{vid_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1[vid]"
    )

    # Base canvas (black 1080x1920)
    canvas = f"color=c=black:s={OUTPUT_W}x{OUTPUT_H}:d=1[canvas_base]"

    # Compose the filter chain
    filter_parts = [canvas, video_scale]

    if has_image:
        # Thumbnail: scale to fit within top region (width 1080, height top_h)
        img_scale = (
            f"[1:v]scale={OUTPUT_W}:{top_h}:force_original_aspect_ratio=decrease,"
            f"pad={OUTPUT_W}:{top_h}:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1[thumb]"
        )
        filter_parts.append(img_scale)
        # Overlay thumb at top, video at middle
        overlays = (
            f"[canvas_base][thumb]overlay=0:0[c1];"
            f"[c1][vid]overlay=0:{vid_y}[out]"
        )
        filter_parts.append(overlays)
    elif has_text:
        # Text overlay via drawtext — positioned within the top region only.
        fontfile = _pick_canvas_fontfile(top_text, bold=text_bold)
        if not fontfile:
            raise CanvasError("No suitable fontfile for canvas text")
        txt_path_esc = _escape_path_for_filter(str(txt_file))
        fontfile_esc = _escape_path_for_filter(fontfile)

        # Font size: must fit width AND fit vertically within top_h.
        num_lines = max(1, top_text.count("\n") + 1)
        text_region_h = int(top_h * 0.8)
        max_size_from_height = int(text_region_h / (num_lines * 1.25))
        font_size = _compute_text_fontsize(
            top_text, max_w=int(OUTPUT_W * 0.92),
            base_size=max_size_from_height, min_size=32,
        )
        # Session FIX-SLIDER: prefer continuous text_size_mult; legacy enum kept for fallback
        if text_size_mult and text_size_mult != 1.0:
            size_mult = max(0.5, min(2.0, float(text_size_mult)))
        else:
            size_multipliers = {"small": 0.78, "medium": 1.0, "large": 1.22}
            size_mult = size_multipliers.get(text_size, 1.0)
        # Safety clamp: never let the multiplied size exceed what we know fits the
        # width (i.e. don't let "large" cause overflow on long text). We re-run
        # the width-fitter with no min cap to find the absolute width-cap.
        width_only_cap = _compute_text_fontsize(
            top_text, max_w=int(OUTPUT_W * 0.92),
            base_size=999, min_size=8,
        )
        font_size = max(28, min(int(font_size * size_mult), width_only_cap))

        # Map color names to ffmpeg-friendly hex strings
        # (ffmpeg's drawtext fontcolor accepts named colors AND 0xRRGGBB).
        color_map = {
            "white":  "0xFFFFFF",
            "red":    "0xFF3B30",
            "yellow": "0xFFD60A",
            "green":  "0x34C759",
        }
        ff_color = color_map.get(text_color, "0xFFFFFF")

        # Build drawtext expression.
        drawtext_expr = (
            f"drawtext=fontfile={fontfile_esc}:expansion=none:textfile={txt_path_esc}"
            f":fontcolor={ff_color}:fontsize={font_size}"
            f":x=(w-text_w)/2:y=({top_h}-text_h)/2"
            f":line_spacing=8"
        )

        # Underline: ffmpeg's drawbox can't reference drawtext's `text_w`/`text_h`.
        # Solution: precompute approximate text dimensions ourselves (using the
        # same heuristic as the font-sizer) and draw a fixed-width box at the
        # estimated baseline. Approximate but correct for our use-case (1-2 lines
        # of typical caption text).
        if text_underline:
            # Estimate text width — use longest line × glyph-width heuristic.
            longest_line_chars = max(len(line.strip()) for line in top_text.split("\n"))
            letters = [c for c in top_text if c.isalpha()]
            is_caps_heavy = letters and sum(1 for c in letters if c.isupper()) / len(letters) > 0.6
            glyph_w = 0.62 if is_caps_heavy else 0.54
            approx_text_w = int(longest_line_chars * font_size * glyph_w)
            approx_text_w = min(approx_text_w, int(OUTPUT_W * 0.96))

            # Approximate text height (font_size × num_lines × line factor)
            approx_text_h = int(font_size * num_lines * 1.25)
            line_thickness = max(2, font_size // 22)
            # Y of underline: just below the bottom of the text block
            underline_y = int((top_h - approx_text_h) / 2 + approx_text_h + line_thickness)
            underline_x = int((OUTPUT_W - approx_text_w) / 2)
            # Underline matches the text color
            ff_underline_color = color_map.get(text_color, "0xFFFFFF")
            drawtext_expr = (
                drawtext_expr
                + f",drawbox=x={underline_x}:y={underline_y}"
                + f":w={approx_text_w}:h={line_thickness}:color={ff_underline_color}@0.95:t=fill"
            )

        overlays = (
            f"[canvas_base][vid]overlay=0:{vid_y}[c1];"
            f"[c1]{drawtext_expr}[out]"
        )
        filter_parts.append(overlays)
    else:
        overlays = f"[canvas_base][vid]overlay=0:{vid_y}[out]"
        filter_parts.append(overlays)

    filter_complex = ";".join(filter_parts)

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "0:a?",  # pass through audio from input video
        # Quality-focused encode: CRF 20 is visually lossless for most content,
        # 'fast' preset balances speed (10-20s per clip) with quality.
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        "-loglevel", "error",
        str(output_clip),
    ]

    try:
        subprocess.run(cmd, check=True, timeout=300, capture_output=True)
    except subprocess.CalledProcessError as e:
        stderr_tail = e.stderr.decode()[-500:] if e.stderr else ""
        raise CanvasError(
            f"Canvas wrap failed for clip #{rank}: {stderr_tail}"
        ) from e
    except subprocess.TimeoutExpired:
        raise CanvasError(f"Canvas wrap timeout for clip #{rank}")
    finally:
        for t in temp_files:
            try:
                t.unlink(missing_ok=True)
            except OSError:
                pass

    logger.info(f"[{job_id}] ✓ canvas wrap clip #{rank} → {output_clip}")
    return {
        "skipped": False,
        "layout": layout,
        "had_text": has_text,
        "had_image": has_image,
    }


def _safe_copy(src: Path, dst: Path) -> None:
    """Copy src to dst — uses shutil if ffmpeg remux unavailable."""
    if src.resolve() == dst.resolve():
        return
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-c", "copy",
             "-loglevel", "error", str(dst)],
            check=True, capture_output=True, timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        import shutil
        shutil.copyfile(src, dst)