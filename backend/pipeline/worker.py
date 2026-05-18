"""
backend/pipeline/worker.py

Celery worker — Step 12: passes detected language to render, persists
per-clip scoring breakdown (hook/emotion/completeness/shareability) to DB.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from celery import Celery
from sqlalchemy.orm.attributes import flag_modified

from backend.core.config import settings
from backend.core.database import SessionLocal
from backend.models.clip import Clip
from backend.models.user import User
from backend.models.video import VideoJob, JobStatus, SourceType
from backend.pipeline.download import download_youtube, extract_video_meta, DownloadError
from backend.pipeline.transcribe import extract_audio, transcribe_audio, TranscribeError
from backend.pipeline.score import pick_clips, ScoringError
from backend.pipeline.render import render_all_clips, RenderError

logger = logging.getLogger(__name__)


celery_app = Celery(
    "clipwise",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    task_track_started=True,
    worker_max_tasks_per_child=10,
)


def _update_status(job_id: uuid.UUID, new_status: JobStatus, error: str | None = None) -> None:
    with SessionLocal() as db:
        job = db.query(VideoJob).filter(VideoJob.id == job_id).first()
        if not job:
            return
        job.status = new_status
        if error:
            job.error_message = error[:2000]
        if new_status == JobStatus.done:
            job.completed_at = datetime.now(timezone.utc)
        db.commit()
    logger.info(f"[{job_id}] status → {new_status.value}")


def _update_meta(job_id: uuid.UUID, updates: dict) -> None:
    with SessionLocal() as db:
        job = db.query(VideoJob).filter(VideoJob.id == job_id).first()
        if not job:
            return
        meta = dict(job.meta or {})
        meta.update(updates)
        job.meta = meta
        flag_modified(job, "meta")
        db.commit()


def _refund_credit(user_id: uuid.UUID) -> None:
    with SessionLocal() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if user:
            user.credits += 1
            db.commit()
            logger.info(f"Refunded 1 credit to user {user_id}")


@celery_app.task(name="process_video", bind=True, max_retries=0)
def process_video(self, job_id_str: str) -> dict:
    job_id = uuid.UUID(job_id_str)
    logger.info(f"[{job_id}] pipeline started")

    with SessionLocal() as db:
        job = db.query(VideoJob).filter(VideoJob.id == job_id).first()
        if not job:
            return {"ok": False, "error": "job not found"}
        user_id = job.user_id
        source_type = job.source_type
        source_url = job.source_url
        existing_file = job.file_path
        goal = job.goal.value
        meta = job.meta or {}
        # Session WM: snapshot user plan + lifetime count for watermark gating.
        # Loaded once at job start so all clips in the job share the same baseline
        # (incremented atomically after render completes).
        user = db.query(User).filter(User.id == user_id).first()
        user_plan_str = user.plan.value if (user and user.plan) else "free"
        user_lifetime_clips_at_job_start = int(getattr(user, "lifetime_clips_generated", 0) or 0)
        aspect_ratio = meta.get("aspect_ratio", "9:16")
        crop_position = meta.get("crop_position", "center")
        caption_preset = meta.get("caption_preset", settings.DEFAULT_CAPTION_PRESET)
        # Step 14: target platform for tuned clip selection
        platform = meta.get("platform", "all")
        # Session 1: variable clip count (range 3-20, default 5)
        num_clips = int(meta.get("num_clips", 5))
        num_clips = max(3, min(20, num_clips))  # clamp — never trust stored values blindly
        # Session C: post-production
        add_hook_outro = bool(meta.get("add_hook_outro", False))
        remove_silences = bool(meta.get("remove_silences", False))

    logger.info(
        f"[{job_id}] config: goal={goal}, aspect={aspect_ratio}, "
        f"crop={crop_position}, caption={caption_preset}, platform={platform}, "
        f"num_clips={num_clips}, "
        f"add_hook_outro={add_hook_outro}, remove_silences={remove_silences}"
    )

    try:
        # 1. DOWNLOAD
        _update_status(job_id, JobStatus.downloading)
        if source_type == SourceType.youtube:
            video_path = download_youtube(user_id, job_id, source_url)
        else:
            video_path = Path(existing_file)
            if not video_path.exists():
                raise DownloadError(f"Uploaded file missing: {video_path}")

        # Step 13 fix: Persist the video path so re-render can find it later.
        # For YouTube videos, file_path was None until now. For uploads, we overwrite
        # with the absolute path (just in case it was relative or missing).
        with SessionLocal() as db:
            job_for_update = db.query(VideoJob).filter(VideoJob.id == job_id).first()
            if job_for_update:
                job_for_update.file_path = str(video_path)
                db.commit()
        logger.info(f"[{job_id}] persisted video file_path: {video_path}")

        vmeta = extract_video_meta(video_path)
        _update_meta(job_id, vmeta)

        # 2+3. AUDIO + TRANSCRIBE
        _update_status(job_id, JobStatus.transcribing)
        audio_file = extract_audio(video_path, user_id, job_id)
        transcript = transcribe_audio(audio_file, job_id)
        detected_language = transcript.get("language", "en")

        # Stash detected language in meta so UI can show it
        _update_meta(job_id, {"language": detected_language})

        # 4. SCORE
        _update_status(job_id, JobStatus.scoring)
        clips = pick_clips(
            job_id=job_id,
            transcript=transcript,
            goal=goal,
            num_clips=num_clips,  # Session 1: from job meta (was hardcoded 5)
            creator_memory="",
            platform=platform,  # Step 14: platform-aware scoring
        )

        _update_meta(job_id, {
            "render_total": len(clips),
            "render_done": 0,
        })

        # =====================================================================
        # Phase 2C: adaptive keyframes per clip
        #
        # Run the active-speaker pipeline ONCE for the whole source video
        # (face tracking + audio VAD is expensive — full-video analysis is
        # faster than per-clip and produces smoother results across boundaries).
        # Then place_adaptive_keyframes slices per-clip via clip_start/end_sec.
        #
        # If any step fails, log a warning and continue — clips will fall back
        # to static smart_crop face-centering. Auto-keyframes are an enhancement,
        # not a hard requirement.
        # =====================================================================
        logger.info(f"[{job_id}] generating adaptive keyframes for {len(clips)} clips...")
        try:
            from backend.pipeline.audio_activity import analyze_audio_activity
            from backend.pipeline.active_speaker import (
                track_faces_across_frames,
                compute_active_speaker_timeline,
            )
            from backend.pipeline.conversation_pace import place_adaptive_keyframes
            from backend.pipeline.smart_crop import get_video_dimensions

            sw, sh = get_video_dimensions(video_path)
            if sw == 0 or sh == 0:
                raise RuntimeError(f"Could not probe source dimensions: {video_path}")

            # ONE expensive analysis for the entire video
            audio_activity   = analyze_audio_activity(video_path, job_id=str(job_id))
            face_tracking    = track_faces_across_frames(video_path)
            speaker_timeline = compute_active_speaker_timeline(face_tracking, audio_activity)

            for c in clips:
                clip_start = float(c["start"])
                clip_end   = clip_start + float(c["duration"])
                try:
                    keyframes = place_adaptive_keyframes(
                        speaker_timeline,
                        face_tracking,
                        clip_start_sec=clip_start,
                        clip_end_sec=clip_end,
                        source_width=sw,
                        source_height=sh,
                    )
                    if keyframes and len(keyframes) > 0:
                        c["user_crop"] = {
                            "version": 2,
                            "keyframes": keyframes,
                            "zoom": 1.0,
                            "transition": "smooth",
                            "transition_dur": 0.4,  # per-keyframe dur_in overrides this
                        }
                        logger.info(
                            f"[{job_id}] clip #{c.get('rank')}: "
                            f"{len(keyframes)} adaptive keyframes placed"
                        )
                    else:
                        logger.info(
                            f"[{job_id}] clip #{c.get('rank')}: "
                            f"no keyframes generated, will use static smart_crop"
                        )
                except Exception as e:
                    logger.warning(
                        f"[{job_id}] clip #{c.get('rank')}: "
                        f"keyframe generation failed ({e}), using static smart_crop"
                    )

        except Exception as e:
            logger.warning(
                f"[{job_id}] adaptive keyframe pipeline failed ({e}), "
                f"all clips will use static smart_crop"
            )
        # === end Phase 2C insert ===

        # Session C: Generate pre-hook + outro for each clip BEFORE render
        if add_hook_outro:
            logger.info(f"[{job_id}] generating hooks + outros for {len(clips)} clips...")
            from backend.pipeline.intro_outro import generate_intro_outro
            for c in clips:
                # Build a text sample for this clip from the transcript
                clip_start = float(c["start"])
                clip_end = float(c["end"])
                clip_text = " ".join(
                    s.get("text", "").strip()
                    for s in transcript["segments"]
                    if s["end"] > clip_start and s["start"] < clip_end
                )[:2000]
                try:
                    intro_outro = generate_intro_outro(
                        clip_text=clip_text,
                        clip_title=c.get("title", ""),
                        detected_language=detected_language,
                        speaker_hint="",
                        platform=platform,
                    )
                    c["intro_outro"] = intro_outro
                    logger.info(
                        f"[{job_id}] clip #{c['rank']} hook: "
                        f"'{intro_outro.get('hook_line1', '')[:40]}...' "
                        f"(pattern={intro_outro.get('pattern_used', '?')})"
                    )
                except Exception as e:
                    logger.warning(
                        f"[{job_id}] clip #{c.get('rank')}: hook generation failed ({e}), "
                        f"using fallback"
                    )
                    from backend.pipeline.intro_outro import _fallback_intro_outro
                    c["intro_outro"] = _fallback_intro_outro(c.get("title", ""))

        # Session B1: Tag emphasis words + emojis per clip if the selected preset needs it
        from backend.pipeline.render import get_caption_preset
        preset_cfg = get_caption_preset(caption_preset)
        if preset_cfg.get("emphasis_mode"):
            # If preset is Hinglish, enable per-word language tagging
            enable_hinglish = bool(preset_cfg.get("hinglish_mode"))
            logger.info(
                f"[{job_id}] tagging caption emphasis for {len(clips)} clips "
                f"(preset={caption_preset}, hinglish={enable_hinglish})..."
            )
            from backend.pipeline.caption_emphasis import (
                tag_emphasis_and_emojis, flatten_segments_to_words,
            )
            for c in clips:
                clip_start = float(c["start"])
                clip_end = float(c["end"])
                segs_in = [
                    s for s in transcript["segments"]
                    if s["end"] > clip_start and s["start"] < clip_end
                ]
                flat_words = flatten_segments_to_words(segs_in)
                # Only keep words that actually fall within the clip window
                flat_words = [
                    w for w in flat_words
                    if w["end"] > clip_start and w["start"] < clip_end
                ]
                if not flat_words:
                    c["emphasis_data"] = {
                        "emphasis_indices": [], "emoji_map": {},
                        "language_map": {}, "filler_indices": [],
                        "is_hinglish": False,
                    }
                    continue
                try:
                    c["emphasis_data"] = tag_emphasis_and_emojis(
                        clip_words=flat_words,
                        clip_title=c.get("title", ""),
                        detected_language=detected_language,
                        enable_hinglish_mode=enable_hinglish,
                    )
                    ed = c["emphasis_data"]
                    extras = ""
                    if ed.get("is_hinglish"):
                        extras = (
                            f", {len(ed.get('language_map', {}))} lang-tagged, "
                            f"{len(ed.get('filler_indices', []))} fillers"
                        )
                    logger.info(
                        f"[{job_id}] clip #{c['rank']} emphasis: "
                        f"{len(ed.get('emphasis_indices', []))} words"
                        f"{extras}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[{job_id}] clip #{c.get('rank')}: emphasis tagging failed ({e}), "
                        f"rendering without emphasis"
                    )
                    c["emphasis_data"] = {
                        "emphasis_indices": [], "emoji_map": {},
                        "language_map": {}, "filler_indices": [],
                        "is_hinglish": False,
                    }

        # 5. RENDER
        _update_status(job_id, JobStatus.rendering)

        def on_render_progress(done: int, total: int) -> None:
            _update_meta(job_id, {"render_done": done, "render_total": total})
            logger.info(f"[{job_id}] render progress: {done}/{total}")

        # Session WM: defensive call to render_all_clips.
        # If a stale render.py is deployed (without watermark kwargs), we still
        # try to render — without the watermark — and log a clear warning,
        # rather than crashing the job entirely.
        import inspect as _ins
        _render_sig = _ins.signature(render_all_clips)
        _has_wm_args = "user_plan" in _render_sig.parameters

        if not _has_wm_args:
            logger.error(
                f"[{job_id}] STALE render.py DETECTED — "
                f"render_all_clips() does not accept watermark kwargs. "
                f"Watermarking will be SKIPPED for this job. "
                f"Replace backend/pipeline/render.py with the latest version."
            )
            rendered = render_all_clips(
                source_video=video_path,
                clips=clips,
                transcript_segments=transcript["segments"],
                user_id=user_id,
                job_id=job_id,
                aspect_ratio=aspect_ratio,
                crop_position=crop_position,
                caption_preset=caption_preset,
                detected_language=detected_language,
                progress_callback=on_render_progress,
                add_hook_outro=add_hook_outro,
                remove_silences=remove_silences,
            )
        else:
            rendered = render_all_clips(
                source_video=video_path,
                clips=clips,
                transcript_segments=transcript["segments"],
                user_id=user_id,
                job_id=job_id,
                aspect_ratio=aspect_ratio,
                crop_position=crop_position,
                caption_preset=caption_preset,
                detected_language=detected_language,
                progress_callback=on_render_progress,
                # Session C:
                add_hook_outro=add_hook_outro,
                remove_silences=remove_silences,
                # Session WM: free-tier watermark gating
                user_plan=user_plan_str,
                user_lifetime_clips=user_lifetime_clips_at_job_start,
            )

        # Session WM: increment user's lifetime clip counter atomically.
        # Even if the next batch decides differently per the env config, this
        # counter is the source of truth and never resets.
        try:
            with SessionLocal() as db:
                user = db.query(User).filter(User.id == user_id).first()
                if user is not None:
                    current = int(getattr(user, "lifetime_clips_generated", 0) or 0)
                    user.lifetime_clips_generated = current + len(rendered)
                    db.commit()
                    logger.info(
                        f"[{job_id}] user lifetime clips: "
                        f"{current} → {user.lifetime_clips_generated}"
                    )
        except Exception as e:
            logger.warning(f"[{job_id}] could not increment lifetime counter: {e}")

        # 6. Persist clips with detailed scores
        with SessionLocal() as db:
            for c in rendered:
                # Step 14: Extended meta with confidence + comparative reasoning + platform
                clip_meta = {
                    "scores": c.get("scores", {}),
                    "reason": c.get("reason", ""),
                    "language": detected_language,
                    "content_type": c.get("content_type", ""),
                    # NEW Step 14:
                    "confidence": c.get("confidence", 70),
                    "beats_alternatives": c.get("beats_alternatives", ""),
                    "platform_fit": c.get("platform_fit", platform),
                    "platform_target": platform,
                    # NEW Session C: post-production metadata
                    "has_hook_outro": bool(add_hook_outro and c.get("intro_outro")),
                    "has_silence_removed": bool(remove_silences),
                    "intro_outro": c.get("intro_outro") or None,
                    # NEW Session B1: caption emphasis metadata (if preset uses it)
                    "caption_emphasis": c.get("emphasis_data") or None,
                    # NEW Session WM: watermark status (frontend uses this to show badge)
                    "watermarked": bool(c.get("watermarked", False)),
                    "user_crop": c.get("user_crop"),  # Phase 2C: adaptive keyframes from active-speaker pipeline
                }

                # Step 13: Store original transcript segments that fall within this clip
                clip_start = float(c["start"])
                clip_end = float(c["end"])
                # Step 13 fix: clamp segment times to clip boundaries so the
                # stored original_transcript has clean, in-range times.
                # Without this, Whisper segments that straddle clip boundaries
                # leak into the data and cause edit-validation issues.
                original_transcript_slice = []
                for s in transcript["segments"]:
                    s_start = float(s["start"])
                    s_end = float(s["end"])
                    # Include segments that overlap the clip window
                    if s_end <= clip_start or s_start >= clip_end:
                        continue
                    # Clamp to clip range
                    clamped_start = max(clip_start, s_start)
                    clamped_end = min(clip_end, s_end)
                    # Skip segments that became trivially short after clamping
                    if clamped_end - clamped_start < 0.1:
                        continue
                    original_transcript_slice.append({
                        "start": clamped_start,
                        "end": clamped_end,
                        "text": str(s.get("text", "")).strip(),
                        "words": list(s.get("words", [])) if s.get("words") else [],
                    })

                clip_row = Clip(
                    video_job_id=job_id,
                    user_id=user_id,
                    rank=c["rank"],
                    title=c.get("title", "")[:255],
                    hook=c.get("hook"),
                    reason=c.get("reason"),
                    start_sec=c["start"],
                    end_sec=c["end"],
                    duration_sec=c["duration"],
                    emotion=c.get("emotion"),
                    virality_score=c.get("virality_score", 50),
                    file_path=c.get("file_path"),
                    thumbnail_path=c.get("thumbnail_path"),
                )

                if hasattr(Clip, "meta"):
                    clip_row.meta = clip_meta

                # Step 13: Store original transcript so user can edit later
                if hasattr(Clip, "original_transcript"):
                    clip_row.original_transcript = original_transcript_slice

                db.add(clip_row)
            db.commit()

        _update_status(job_id, JobStatus.done)
        logger.info(f"[{job_id}] ✓ pipeline complete — {len(rendered)} clips")
        return {"ok": True, "clips": len(rendered)}

    except (DownloadError, TranscribeError, ScoringError, RenderError) as e:
        msg = f"{type(e).__name__}: {e}"
        logger.error(f"[{job_id}] pipeline failed: {msg}")
        _update_status(job_id, JobStatus.failed, error=msg)
        _refund_credit(user_id)
        return {"ok": False, "error": msg}

    except Exception as e:
        logger.exception(f"[{job_id}] UNEXPECTED error")
        _update_status(job_id, JobStatus.failed, error=f"Internal error: {type(e).__name__}")
        _refund_credit(user_id)
        return {"ok": False, "error": str(e)}


# =============================================================================
# Step 13: Re-render a single clip with user's edited transcript
# =============================================================================
@celery_app.task(name="rerender_clip", bind=True, max_retries=1)
def rerender_clip_task(self, clip_id_str: str) -> dict:
    """
    Re-render ONE clip using its edited_transcript.

    Does NOT call LLM. Does NOT re-transcribe. Just re-renders with new captions.
    Typical runtime: 30-60 seconds.

    Updates on success:
      - clip.file_path (overwritten)
      - clip.thumbnail_path (regenerated)
      - clip.rerender_count += 1
      - clip.needs_rerender = False
    """
    from backend.models.clip import Clip
    from backend.pipeline.download import extract_video_meta
    from backend.pipeline.render import rerender_clip_with_edits, RenderError
    from backend.services.storage import clip_output_dir

    clip_id = uuid.UUID(clip_id_str)
    logger.info(f"[rerender {clip_id}] starting")

    # Load clip + parent job info
    with SessionLocal() as db:
        clip = db.query(Clip).filter(Clip.id == clip_id).first()
        if not clip:
            logger.error(f"[rerender {clip_id}] clip not found")
            return {"ok": False, "error": "clip not found"}

        if not clip.edited_transcript:
            logger.error(f"[rerender {clip_id}] no edited_transcript")
            return {"ok": False, "error": "no edits to render"}

        user_id = clip.user_id
        job_id = clip.video_job_id

        # Get render config from parent job
        job = db.query(VideoJob).filter(VideoJob.id == job_id).first()
        if not job:
            return {"ok": False, "error": "parent job not found"}

        job_meta = job.meta or {}
        aspect_ratio = job_meta.get("aspect_ratio", "9:16")
        crop_position = job_meta.get("crop_position", "center")
        caption_preset = job_meta.get("caption_preset", settings.DEFAULT_CAPTION_PRESET)
        detected_language = job_meta.get("language", "en")
        source_file_path = job.file_path

        # Detach clip from session (we'll re-fetch later)
        db.expunge(clip)

    # Step 13 fix: If file_path is None (legacy YouTube clips created before we
    # started persisting file_path), fall back to the conventional storage location.
    # YouTube downloads land at: /storage/<user_id>/raw/<job_id>.mp4
    from backend.services.storage import _user_dir  # internal helper
    if not source_file_path:
        # Try common extensions in order
        logger.warning(
            f"[rerender {clip_id}] file_path is None in DB, trying conventional paths"
        )
        raw_dir = _user_dir(user_id, "raw")
        for ext in (".mp4", ".webm", ".mkv", ".mov"):
            candidate = raw_dir / f"{job_id}{ext}"
            if candidate.exists():
                source_file_path = str(candidate)
                logger.info(f"[rerender {clip_id}] found source at: {candidate}")
                # Also backfill to DB so future re-renders skip this search
                with SessionLocal() as db:
                    j = db.query(VideoJob).filter(VideoJob.id == job_id).first()
                    if j:
                        j.file_path = source_file_path
                        db.commit()
                break

    if not source_file_path or not Path(source_file_path).exists():
        msg = f"source video missing: {source_file_path}"
        logger.error(f"[rerender {clip_id}] {msg}")
        return {"ok": False, "error": msg}

    try:
        output_dir = clip_output_dir(user_id, job_id)
        output_dir.mkdir(parents=True, exist_ok=True)

        clip_path, thumb_path = rerender_clip_with_edits(
            source=Path(source_file_path),
            clip_row=clip,
            output_dir=output_dir,
            job_id=job_id,
            aspect_ratio=aspect_ratio,
            crop_position=crop_position,
            caption_preset=caption_preset,
            detected_language=detected_language,
        )

        # Update DB with new paths + metadata.
        # Capture values INSIDE the session block so we can log them AFTER
        # the session closes (avoids DetachedInstanceError).
        final_count = 0
        with SessionLocal() as db:
            fresh_clip = db.query(Clip).filter(Clip.id == clip_id).first()
            if fresh_clip:
                fresh_clip.file_path = str(clip_path)
                if thumb_path:
                    fresh_clip.thumbnail_path = str(thumb_path)
                fresh_clip.rerender_count = (fresh_clip.rerender_count or 0) + 1
                fresh_clip.needs_rerender = False
                db.commit()
                # Capture the value NOW, while the attribute is still bound to the session
                final_count = fresh_clip.rerender_count

        logger.info(f"[rerender {clip_id}] ✓ done (count now {final_count})")
        return {"ok": True, "clip_id": str(clip_id), "rerender_count": final_count}

    except RenderError as e:
        msg = str(e)
        logger.error(f"[rerender {clip_id}] failed: {msg}")
        # We keep needs_rerender=True so user can try again
        return {"ok": False, "error": msg}

    except Exception as e:
        logger.exception(f"[rerender {clip_id}] UNEXPECTED error")
        return {"ok": False, "error": str(e)}


# =============================================================================
# Session QUALITY: async export at chosen quality (480p / 720p / 1080p)
# =============================================================================
# We store status in Redis so the API can poll without DB hits.
# Key shape: clipwise:export:{export_id} → JSON {status, progress, error, ...}
# TTL: 1 hour (encoding completes in ≤ 2-3 min realistically; 1h is safe slack)

_EXPORT_STATUS_TTL_SEC = 3600


def _redis_client():
    """Get a redis client. Lazy-imported so worker doesn't crash if redis is down at boot."""
    import redis
    return redis.from_url(settings.REDIS_URL, decode_responses=True)


def _export_status_key(export_id: str) -> str:
    return f"clipwise:export:{export_id}"


def set_export_status(export_id: str, **fields) -> None:
    """Update export status in Redis. Merges with existing fields."""
    try:
        r = _redis_client()
        key = _export_status_key(export_id)
        existing_raw = r.get(key)
        existing = json.loads(existing_raw) if existing_raw else {}
        existing.update(fields)
        r.set(key, json.dumps(existing), ex=_EXPORT_STATUS_TTL_SEC)
    except Exception as e:
        logger.warning(f"[export {export_id}] could not write status: {e}")


def get_export_status(export_id: str) -> dict | None:
    """Read export status from Redis. Returns None if missing/expired."""
    try:
        r = _redis_client()
        raw = r.get(_export_status_key(export_id))
        return json.loads(raw) if raw else None
    except Exception as e:
        logger.warning(f"[export {export_id}] could not read status: {e}")
        return None


@celery_app.task(name="export_clip_quality", bind=True, max_retries=0)
def export_clip_quality(self, export_id: str, clip_id_str: str, quality: str) -> dict:
    """
    Re-encode a clip at the chosen quality. Writes progress to Redis so the API
    can poll for status. Caches output so repeat exports of the same quality
    are instant.
    """
    from backend.pipeline.clip_export import export_clip, ExportError, output_path_for

    set_export_status(
        export_id,
        status="encoding",
        progress=0,
        clip_id=clip_id_str,
        quality=quality,
        started_at=datetime.now(timezone.utc).isoformat(),
    )

    try:
        clip_uuid = uuid.UUID(clip_id_str)
        with SessionLocal() as db:
            clip = db.query(Clip).filter(Clip.id == clip_uuid).first()
            if clip is None or not clip.file_path:
                raise ExportError("Clip not found or not yet rendered")
            clip_path = Path(clip.file_path)

        if not clip_path.exists():
            raise ExportError("Source file missing on disk")

        def on_progress(pct: int):
            set_export_status(export_id, progress=int(pct))

        out_path = export_clip(clip_path, quality, progress_callback=on_progress)

        out_size_mb = out_path.stat().st_size / (1024 * 1024)
        set_export_status(
            export_id,
            status="complete",
            progress=100,
            output_path=str(out_path),
            output_size_mb=round(out_size_mb, 2),
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        logger.info(f"[export {export_id}] complete: {quality} ({out_size_mb:.1f} MB)")
        return {"ok": True, "export_id": export_id, "output": str(out_path)}

    except ExportError as e:
        msg = str(e)
        logger.error(f"[export {export_id}] failed: {msg}")
        set_export_status(export_id, status="failed", error=msg)
        return {"ok": False, "error": msg}

    except Exception as e:
        logger.exception(f"[export {export_id}] UNEXPECTED error")
        set_export_status(export_id, status="failed", error=str(e))
        return {"ok": False, "error": str(e)}