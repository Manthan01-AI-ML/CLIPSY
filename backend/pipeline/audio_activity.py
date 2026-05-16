"""
backend/pipeline/audio_activity.py

Phase 2B.1: Per-second speech activity from a video file.

Public API:
  analyze_audio_activity(video_path, *, job_id="") -> dict
    Returns {"duration_sec", "window_count", "window_ms": 100,
             "energy": list[float], "is_voice": list[bool], "voice_segments": list[tuple]}

  compute_audio_energy(audio_path, window_ms=100) -> list[float]
  detect_voice_activity(audio_path, aggressiveness=2) -> list[bool]

Audio format for webrtcvad: 16kHz, 16-bit signed PCM, mono.
Identical ffmpeg flags to transcribe.py's extract_audio().
"""
from __future__ import annotations

import logging
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2          # bytes — 16-bit PCM
WINDOW_MS = 100           # energy + VAD aggregation window
VAD_FRAME_MS = 30         # only valid webrtcvad frame size that divides cleanly into 100ms


class AudioActivityError(Exception):
    pass


# ---------------------------------------------------------------------------
# Audio extraction
# ---------------------------------------------------------------------------

def extract_audio_track(video_path: Path, output_path: Path) -> Path:
    """
    Extract mono 16kHz 16-bit PCM WAV from video_path → output_path.

    Uses identical ffmpeg flags to transcribe.py so the WAV format is always
    compatible with webrtcvad without resampling.
    Raises AudioActivityError on ffmpeg failure.
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-acodec", "pcm_s16le", "-loglevel", "error",
        str(output_path),
    ]
    try:
        subprocess.run(cmd, check=True, timeout=600, capture_output=True)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode(errors="replace")[:500]
        raise AudioActivityError(
            f"ffmpeg failed extracting audio from {video_path.name}: {stderr}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise AudioActivityError(
            f"ffmpeg timed out extracting audio from {video_path.name}"
        ) from e
    return output_path


# ---------------------------------------------------------------------------
# Audio energy (RMS per 100ms window)
# ---------------------------------------------------------------------------

def compute_audio_energy(audio_path: Path, window_ms: int = WINDOW_MS) -> list[float]:
    """
    Compute RMS energy per window_ms window, normalized to [0, 1].

    Returns a list of floats (one per window). Empty list if audio is silent
    or the WAV has zero samples.
    """
    window_samples = int(SAMPLE_RATE * window_ms / 1000)

    with wave.open(str(audio_path), "rb") as wf:
        assert wf.getsampwidth() == SAMPLE_WIDTH, "Expected 16-bit PCM"
        assert wf.getnchannels() == 1, "Expected mono"
        raw = wf.readframes(wf.getnframes())

    if not raw:
        return []

    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

    # Pad to a multiple of window_samples so reshape is clean
    remainder = len(samples) % window_samples
    if remainder:
        samples = np.concatenate([samples, np.zeros(window_samples - remainder)])

    windows = samples.reshape(-1, window_samples)
    rms = np.sqrt(np.mean(windows ** 2, axis=1))

    max_rms = rms.max()
    if max_rms == 0.0:
        return [0.0] * len(rms)

    return (rms / max_rms).tolist()


# ---------------------------------------------------------------------------
# Voice activity detection (webrtcvad, 30ms frames → 100ms windows)
# ---------------------------------------------------------------------------

def detect_voice_activity(
    audio_path: Path,
    aggressiveness: int = 2,
) -> list[bool]:
    """
    Run webrtcvad on the WAV and aggregate 30ms frames into 100ms windows.

    webrtcvad requires exactly 16kHz, 16-bit signed PCM, mono — guaranteed
    by extract_audio_track(). Frame size is 30ms (960 bytes at 16kHz/16-bit).

    Aggregation: window i covers ms [i×100, (i+1)×100). Frame j (starting at
    j×30ms) belongs to window (j×30)//100. A window is True if >50% of its
    frames are classified as voice.

    aggressiveness: 0–3 (0 = most permissive, 3 = most aggressive filtering).
    2 is a good default for podcast/talking-head content.

    Returns list[bool] aligned with compute_audio_energy() windows.
    """
    try:
        import webrtcvad
    except ImportError as e:
        raise AudioActivityError(
            "webrtcvad not installed. Run: pip install webrtcvad==2.0.10"
        ) from e

    vad = webrtcvad.Vad(aggressiveness)

    with wave.open(str(audio_path), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
        n_frames_total = wf.getnframes()

    frame_samples = int(SAMPLE_RATE * VAD_FRAME_MS / 1000)   # 480 samples
    frame_bytes   = frame_samples * SAMPLE_WIDTH               # 960 bytes

    # Collect per-frame results
    frame_is_voice: list[bool] = []
    offset = 0
    while offset + frame_bytes <= len(raw):
        chunk = raw[offset : offset + frame_bytes]
        try:
            is_speech = vad.is_speech(chunk, sample_rate=SAMPLE_RATE)
        except Exception:
            is_speech = False
        frame_is_voice.append(is_speech)
        offset += frame_bytes

    if not frame_is_voice:
        return []

    # Aggregate frames into WINDOW_MS windows
    # Duration of all frames = len(frame_is_voice) * VAD_FRAME_MS ms
    total_ms = len(frame_is_voice) * VAD_FRAME_MS
    n_windows = (total_ms + WINDOW_MS - 1) // WINDOW_MS  # ceiling

    # Map each frame to its window
    window_voice_count = [0] * n_windows
    window_frame_count = [0] * n_windows
    for j, is_voice in enumerate(frame_is_voice):
        window_idx = (j * VAD_FRAME_MS) // WINDOW_MS
        if window_idx < n_windows:
            window_frame_count[window_idx] += 1
            if is_voice:
                window_voice_count[window_idx] += 1

    result = []
    for i in range(n_windows):
        total = window_frame_count[i]
        if total == 0:
            result.append(False)
        else:
            result.append(window_voice_count[i] / total > 0.5)

    return result


# ---------------------------------------------------------------------------
# Collapse windows → (start_sec, end_sec) segments
# ---------------------------------------------------------------------------

def _collapse_voice_windows(
    is_voice: list[bool],
    window_ms: int = WINDOW_MS,
) -> list[tuple[float, float]]:
    """Collapse adjacent True windows into (start_sec, end_sec) tuples."""
    segments: list[tuple[float, float]] = []
    in_segment = False
    start_ms = 0

    for i, active in enumerate(is_voice):
        ms = i * window_ms
        if active and not in_segment:
            in_segment = True
            start_ms = ms
        elif not active and in_segment:
            in_segment = False
            segments.append((start_ms / 1000.0, ms / 1000.0))

    if in_segment:
        segments.append((start_ms / 1000.0, (len(is_voice) * window_ms) / 1000.0))

    return segments


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def analyze_audio_activity(video_path: Path, *, job_id: str = "") -> dict:
    """
    Extract audio, compute energy and VAD, return unified activity dict.

    Returns:
      {
        "duration_sec": float,
        "window_count": int,
        "window_ms": int (always 100),
        "energy": list[float],        # RMS energy per window, normalized 0–1
        "is_voice": list[bool],       # True = voice active in this window
        "voice_segments": list[tuple] # (start_sec, end_sec) pairs
      }

    Raises AudioActivityError on ffmpeg failure or webrtcvad import failure.
    """
    tag = f"[{job_id}] " if job_id else ""
    tmp_wav = Path(
        tempfile.mktemp(suffix=".wav", prefix="clipwise_aactivity_", dir="/tmp")
    )

    try:
        logger.info(f"{tag}Extracting audio track from {video_path.name} …")
        extract_audio_track(video_path, tmp_wav)

        # Duration from WAV header
        with wave.open(str(tmp_wav), "rb") as wf:
            duration_sec = wf.getnframes() / wf.getframerate()

        logger.info(
            f"{tag}Audio extracted: {duration_sec:.1f}s  "
            f"→ {tmp_wav.stat().st_size // 1024} KB"
        )

        logger.info(f"{tag}Computing audio energy …")
        energy = compute_audio_energy(tmp_wav)

        logger.info(f"{tag}Running webrtcvad (aggressiveness=2) …")
        is_voice = detect_voice_activity(tmp_wav, aggressiveness=2)

        # Align lengths — both are 100ms windows; minor mismatch possible at tail
        n = min(len(energy), len(is_voice))
        energy   = energy[:n]
        is_voice = is_voice[:n]

        voice_segments = _collapse_voice_windows(is_voice)
        voice_frac = sum(is_voice) / n if n > 0 else 0.0

        logger.info(
            f"{tag}{n} windows  |  "
            f"voice={sum(is_voice)}/{n} ({voice_frac:.1%})  |  "
            f"{len(voice_segments)} segment(s)"
        )

        return {
            "duration_sec": round(duration_sec, 3),
            "window_count": n,
            "window_ms": WINDOW_MS,
            "energy": energy,
            "is_voice": is_voice,
            "voice_segments": voice_segments,
        }

    finally:
        try:
            tmp_wav.unlink(missing_ok=True)
        except Exception:
            pass
