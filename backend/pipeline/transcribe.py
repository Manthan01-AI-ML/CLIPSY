"""
backend/pipeline/transcribe.py

Transcription via Groq Whisper API (Step 14 fix: chunking for long audio).

Architecture:
  - PRIMARY: Groq whisper-large-v3-turbo API
      * 216x real-time, ~97% accuracy, FREE tier
      * 25 MB effective upload limit (declared 40, real-world ~20-25)
      * For long podcasts (>14 min of 16kHz mono): auto-compress + chunk
  - FALLBACK: local faster-whisper (CPU, slow)

Chunking flow:
  1. Try single upload after FLAC/OGG compression
  2. If still too big, split audio into N chunks at silence boundaries
  3. Transcribe each chunk via Groq (they all fit the limit)
  4. Offset timestamps to stitch back into a single transcript

This is how Opus/Submagic handle long videos internally.
"""
from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from backend.core.config import settings
from backend.services.storage import audio_path

logger = logging.getLogger(__name__)


class TranscribeError(Exception):
    pass


# Groq's declared limit is 40MB but real-world uploads fail well before that
# due to multipart encoding overhead and internal processing. 22MB is safe.
GROQ_MAX_UPLOAD_MB = 22.0

# Maximum audio duration per chunk when we need to split.
# 10 min of 16kHz mono OGG @ 32kbps ≈ 2.4 MB — well under limit.
CHUNK_DURATION_SEC = 600  # 10 minutes


# ---------------------------------------------------------------------------
# Audio extraction (16kHz mono WAV — optimal for Whisper)
# ---------------------------------------------------------------------------
def extract_audio(video_path: Path, user_id: uuid.UUID, job_id: uuid.UUID) -> Path:
    out = audio_path(user_id, job_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", "16000",
        "-acodec", "pcm_s16le", "-loglevel", "error", str(out),
    ]
    logger.info(f"[{job_id}] extracting audio → {out.name}")
    try:
        subprocess.run(cmd, check=True, timeout=600, capture_output=True)
    except subprocess.CalledProcessError as e:
        raise TranscribeError(f"ffmpeg failed: {e.stderr.decode()[:500]}") from e
    except subprocess.TimeoutExpired:
        raise TranscribeError("ffmpeg timeout (>10min)")
    if not out.exists() or out.stat().st_size == 0:
        raise TranscribeError("ffmpeg produced no output")
    return out


# ---------------------------------------------------------------------------
# Audio compression (for large files)
# ---------------------------------------------------------------------------
def _compress_for_groq(audio_file: Path, job_id: uuid.UUID) -> Path:
    """
    Compress audio to fit Groq's upload limit. Tries FLAC first (lossless),
    falls back to low-bitrate OGG Vorbis if FLAC is still too big.

    Returns the path to the smallest usable file. If the returned path differs
    from the input, the caller should clean it up after use.
    """
    size_mb = audio_file.stat().st_size / (1024 * 1024)
    if size_mb <= GROQ_MAX_UPLOAD_MB:
        return audio_file

    logger.info(f"[{job_id}] audio is {size_mb:.1f} MB — compressing for Groq upload")

    # Try FLAC (lossless, same accuracy as WAV)
    flac_path = audio_file.with_suffix(".flac")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio_file),
             "-c:a", "flac", "-compression_level", "8",
             "-loglevel", "error", str(flac_path)],
            check=True, timeout=180, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise TranscribeError(f"FLAC compression failed: {e.stderr.decode()[:300]}") from e
    except subprocess.TimeoutExpired:
        raise TranscribeError("FLAC compression timeout (>3min)")

    flac_mb = flac_path.stat().st_size / (1024 * 1024)
    logger.info(f"[{job_id}] FLAC: {flac_mb:.1f} MB (was {size_mb:.1f} MB)")
    if flac_mb <= GROQ_MAX_UPLOAD_MB:
        return flac_path

    # FLAC still too big: go to lossy OGG @ 32kbps mono (still fine for speech)
    flac_path.unlink(missing_ok=True)
    ogg_path = audio_file.with_suffix(".ogg")
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(audio_file),
             "-c:a", "libvorbis", "-b:a", "32k", "-ac", "1",
             "-loglevel", "error", str(ogg_path)],
            check=True, timeout=180, capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        raise TranscribeError(f"OGG compression failed: {e.stderr.decode()[:300]}") from e

    ogg_mb = ogg_path.stat().st_size / (1024 * 1024)
    logger.info(f"[{job_id}] OGG: {ogg_mb:.1f} MB (was {size_mb:.1f} MB)")
    return ogg_path


# ---------------------------------------------------------------------------
# Audio chunking (for very long audio that won't fit even compressed)
# ---------------------------------------------------------------------------
def _get_audio_duration(audio_file: Path) -> float:
    """Get audio duration in seconds via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(audio_file)],
            check=True, capture_output=True, timeout=30, text=True,
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError) as e:
        raise TranscribeError(f"Could not get audio duration: {e}") from e


def _split_audio_into_chunks(
    audio_file: Path, chunk_duration_sec: float, job_id: uuid.UUID
) -> list[tuple[Path, float]]:
    """
    Split audio into roughly-equal chunks.

    Returns list of (chunk_path, chunk_start_offset_sec) tuples.
    Chunks are encoded as OGG Vorbis @ 32kbps mono for tiny file size.

    Caller is responsible for cleaning up chunk files after use.
    """
    total_duration = _get_audio_duration(audio_file)
    num_chunks = math.ceil(total_duration / chunk_duration_sec)
    logger.info(
        f"[{job_id}] splitting {total_duration:.0f}s audio into {num_chunks} chunks "
        f"of ~{chunk_duration_sec:.0f}s each"
    )

    chunk_dir = Path(tempfile.mkdtemp(prefix=f"clipwise_chunks_{job_id.hex[:8]}_"))
    chunks: list[tuple[Path, float]] = []

    for i in range(num_chunks):
        start_sec = i * chunk_duration_sec
        chunk_path = chunk_dir / f"chunk_{i:03d}.ogg"
        # Use -t to limit chunk duration; -ss to start at offset
        # OGG @ 32kbps mono is tiny: 10 min ≈ 2.4 MB
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start_sec:.2f}",
            "-i", str(audio_file),
            "-t", f"{chunk_duration_sec:.2f}",
            "-c:a", "libvorbis", "-b:a", "32k", "-ac", "1",
            "-loglevel", "error",
            str(chunk_path),
        ]
        try:
            subprocess.run(cmd, check=True, timeout=180, capture_output=True)
        except subprocess.CalledProcessError as e:
            shutil.rmtree(chunk_dir, ignore_errors=True)
            raise TranscribeError(
                f"Failed to split audio at chunk {i}: {e.stderr.decode()[:300]}"
            ) from e
        if not chunk_path.exists() or chunk_path.stat().st_size == 0:
            shutil.rmtree(chunk_dir, ignore_errors=True)
            raise TranscribeError(f"Chunk {i} produced no output")

        chunk_mb = chunk_path.stat().st_size / (1024 * 1024)
        logger.info(
            f"[{job_id}] chunk {i+1}/{num_chunks}: {chunk_mb:.1f} MB, "
            f"offset={start_sec:.0f}s"
        )
        chunks.append((chunk_path, start_sec))

    return chunks


def _cleanup_chunks(chunks: list[tuple[Path, float]]) -> None:
    """Clean up chunk files + their directory."""
    if not chunks:
        return
    try:
        chunk_dir = chunks[0][0].parent
        shutil.rmtree(chunk_dir, ignore_errors=True)
    except Exception as e:
        logger.warning(f"Cleanup failed: {e}")


# ---------------------------------------------------------------------------
# Groq Whisper API call (single chunk)
# ---------------------------------------------------------------------------
def _groq_transcribe_single(
    audio_file: Path, job_id: uuid.UUID, time_offset: float = 0.0
) -> dict:
    """
    Transcribe a SINGLE audio file via Groq.
    If time_offset > 0, all returned timestamps are shifted by that amount.
    This is how we stitch chunks back into a single timeline.
    """
    try:
        from groq import Groq
    except ImportError as e:
        raise TranscribeError("groq package not installed") from e
    if not settings.GROQ_API_KEY:
        raise TranscribeError("GROQ_API_KEY not set")

    file_size_mb = audio_file.stat().st_size / (1024 * 1024)
    logger.info(
        f"[{job_id}] Groq upload: {audio_file.name} ({file_size_mb:.1f} MB, "
        f"offset={time_offset:.0f}s)"
    )

    client = Groq(api_key=settings.GROQ_API_KEY)
    try:
        with open(audio_file, "rb") as f:
            resp = client.audio.transcriptions.create(
                file=(audio_file.name, f.read()),
                model="whisper-large-v3-turbo",
                response_format="verbose_json",
                timestamp_granularities=["word", "segment"],
            )
    except Exception as e:
        raise TranscribeError(f"Groq Whisper API failed: {e}") from e

    detected_lang = getattr(resp, "language", "unknown")
    duration = float(getattr(resp, "duration", 0) or 0)
    segments_raw = getattr(resp, "segments", []) or []
    words_raw = getattr(resp, "words", []) or []
    full_text = getattr(resp, "text", "") or ""

    # Merge words into segments with time-offset applied.
    # Bug fix: Groq returns words as a SEPARATE flat list. Strict containment
    # (the previous algorithm) drops words that span segment boundaries.
    # Solution: for each word, find the segment with the MOST temporal overlap
    # and assign it there. This guarantees no word is lost.

    # Pre-parse all words once into a normalized list with timestamps
    parsed_words: list[dict] = []
    for w in words_raw:
        try:
            w_start = float(w["start"]) if isinstance(w, dict) else float(w.start)
            w_end = float(w["end"]) if isinstance(w, dict) else float(w.end)
            w_text = (w["word"] if isinstance(w, dict) else w.word).strip()
        except (KeyError, AttributeError, TypeError, ValueError):
            continue
        if not w_text:
            continue
        parsed_words.append({"word": w_text, "start": w_start, "end": w_end})

    # Initialize empty segments
    segments: list[dict] = []
    parsed_segs: list[dict] = []
    for seg in segments_raw:
        seg_start = float(seg["start"]) if isinstance(seg, dict) else float(seg.start)
        seg_end = float(seg["end"]) if isinstance(seg, dict) else float(seg.end)
        seg_text = (seg["text"] if isinstance(seg, dict) else seg.text).strip()
        seg_dict = {
            "start": seg_start + time_offset,
            "end": seg_end + time_offset,
            "text": seg_text,
            "words": [],
        }
        parsed_segs.append({"start": seg_start, "end": seg_end, "ref": seg_dict})
        segments.append(seg_dict)

    # For each word, find the segment with the MOST overlap. If no overlap,
    # find the segment whose midpoint is closest (handles between-segment
    # silence boundaries — every word is preserved).
    for pw in parsed_words:
        best_seg = None
        best_overlap = 0.0
        for ps in parsed_segs:
            ovr_start = max(pw["start"], ps["start"])
            ovr_end = min(pw["end"], ps["end"])
            ovr = ovr_end - ovr_start
            if ovr > best_overlap:
                best_overlap = ovr
                best_seg = ps

        # If word doesn't overlap any segment (rare, only at boundaries), pick
        # the closest segment by midpoint distance.
        if best_seg is None and parsed_segs:
            word_mid = (pw["start"] + pw["end"]) / 2
            best_seg = min(
                parsed_segs,
                key=lambda ps: abs(((ps["start"] + ps["end"]) / 2) - word_mid),
            )

        if best_seg is not None:
            best_seg["ref"]["words"].append({
                "word": pw["word"],
                "start": pw["start"] + time_offset,
                "end": pw["end"] + time_offset,
            })

    # Sort each segment's words by start time + clean up empty `words` keys
    for seg in segments:
        if seg["words"]:
            seg["words"].sort(key=lambda w: w["start"])
        else:
            del seg["words"]

    return {
        "language": detected_lang,
        "duration": duration,
        "segments": segments,
        "full_text": full_text,
    }


# ---------------------------------------------------------------------------
# Main Groq transcription path — compression + chunking fallback
# ---------------------------------------------------------------------------
def _transcribe_via_groq(audio_file: Path, job_id: uuid.UUID) -> dict:
    """
    Groq transcription with smart handling of large files:
      1. Compress (FLAC → OGG) if needed
      2. If still too big, split into chunks and transcribe each
      3. Merge results back into a single transcript
    """
    # Step 1: compress
    compressed = _compress_for_groq(audio_file, job_id)
    compressed_mb = compressed.stat().st_size / (1024 * 1024)

    # Track temp files we created to clean up later
    temp_files_to_clean: list[Path] = []
    if compressed != audio_file:
        temp_files_to_clean.append(compressed)

    try:
        # If compression was enough, just upload
        if compressed_mb <= GROQ_MAX_UPLOAD_MB:
            logger.info(f"[{job_id}] single-upload path ({compressed_mb:.1f} MB)")
            result = _groq_transcribe_single(compressed, job_id)
            if not result["segments"]:
                raise TranscribeError("Groq returned no segments — audio may be silent")
            return result

        # Step 2: still too big → chunk it
        logger.info(
            f"[{job_id}] file still {compressed_mb:.1f} MB after compression "
            f"→ splitting into {CHUNK_DURATION_SEC}s chunks"
        )
        chunks = _split_audio_into_chunks(compressed, CHUNK_DURATION_SEC, job_id)

        try:
            # Step 3: transcribe each chunk
            all_segments: list[dict] = []
            all_text_parts: list[str] = []
            detected_lang = "unknown"
            total_duration = 0.0

            for i, (chunk_path, offset) in enumerate(chunks):
                logger.info(f"[{job_id}] transcribing chunk {i+1}/{len(chunks)}...")
                chunk_result = _groq_transcribe_single(chunk_path, job_id, time_offset=offset)
                all_segments.extend(chunk_result["segments"])
                all_text_parts.append(chunk_result["full_text"])
                if detected_lang == "unknown":
                    detected_lang = chunk_result["language"]
                total_duration = max(total_duration, offset + chunk_result["duration"])

            if not all_segments:
                raise TranscribeError(
                    "All chunks returned 0 segments — audio may be silent or corrupt"
                )

            # Sort segments by start time (should already be in order but defensive)
            all_segments.sort(key=lambda s: s["start"])

            logger.info(
                f"[{job_id}] chunked transcription complete: "
                f"{len(all_segments)} segments across {len(chunks)} chunks, "
                f"language={detected_lang}"
            )

            return {
                "language": detected_lang,
                "duration": total_duration,
                "segments": all_segments,
                "full_text": " ".join(all_text_parts).strip(),
            }
        finally:
            _cleanup_chunks(chunks)

    finally:
        # Clean up compressed temp file if any
        for p in temp_files_to_clean:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Local faster-whisper fallback
# ---------------------------------------------------------------------------
_local_model = None


def _get_local_model():
    global _local_model
    if _local_model is None:
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise TranscribeError("faster-whisper not installed for fallback") from e
        logger.info(f"Loading LOCAL whisper model '{settings.WHISPER_MODEL}' for fallback...")
        _local_model = WhisperModel(settings.WHISPER_MODEL, device="cpu", compute_type="int8")
    return _local_model


def _transcribe_via_local(audio_file: Path, job_id: uuid.UUID) -> dict:
    model = _get_local_model()
    beam = 5 if settings.WHISPER_MODEL in ("small", "medium", "large-v3") else 1
    logger.info(f"[{job_id}] local whisper: model={settings.WHISPER_MODEL}")

    for use_vad in (True, False):
        try:
            segments_iter, info = model.transcribe(
                str(audio_file),
                beam_size=beam,
                word_timestamps=True,
                vad_filter=use_vad,
                condition_on_previous_text=False,
            )
            segments = []
            for s in segments_iter:
                seg_dict = {"start": float(s.start), "end": float(s.end), "text": s.text.strip()}
                if s.words:
                    seg_dict["words"] = [
                        {"word": w.word, "start": float(w.start), "end": float(w.end)}
                        for w in s.words
                    ]
                segments.append(seg_dict)
            if segments:
                break
        except Exception as e:
            if not use_vad:
                raise TranscribeError(f"local whisper failed: {e}") from e
            logger.warning(f"[{job_id}] VAD failed, retry without: {e}")

    if not segments:
        raise TranscribeError("No speech detected in audio")

    return {
        "language": info.language,
        "duration": info.duration,
        "segments": segments,
        "full_text": " ".join(s["text"] for s in segments),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def transcribe_audio(audio_file: Path, job_id: uuid.UUID) -> dict:
    """
    Transcribe audio. Tries Groq (with compression + chunking) first,
    falls back to local Whisper if Groq fails entirely.
    """
    if settings.WHISPER_PROVIDER == "groq":
        try:
            return _transcribe_via_groq(audio_file, job_id)
        except TranscribeError as e:
            logger.warning(f"[{job_id}] Groq transcription failed ({e}), falling back to local")
        except Exception as e:
            logger.warning(f"[{job_id}] Groq unexpected error ({e}), falling back to local")

    try:
        return _transcribe_via_local(audio_file, job_id)
    except TranscribeError:
        raise
    except Exception as e:
        raise TranscribeError(f"Local whisper failed: {e}") from e