from __future__ import annotations

"""
Remotion Stock Video Assembler — Renders TikTok videos using stock footage + voiceover.

Engine options: "remotion_stock" and "hybrid"
- Fetches stock video clips from Pixabay based on script tags
- Mixes stock clips with screen recordings when available
- Renders via Remotion with the StockVideoNarration template
- Outputs 9:16 MP4 with word-by-word subtitles

Hybrid mode:
- Stock footage slides: full-screen, cinematic warm grade
- App recording slides: phone frame, clean grade, spring entrance
- Mode-switch transitions: flash frame between stock <-> app

Same interface as video_assembler.py and remotion_assembler.py:
  assemble_video(script, image_paths, voiceover_path, subtitle_data, ...) -> video_path
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from log_config import get_logger

from stock_video_fetcher import fetch_best_clip

logger = get_logger(__name__)


# ── Remotion project location ────────────────────────────────────
_src_parent = Path(__file__).parent.parent / "remotion"
_cwd_based = Path.cwd() / "remotion"
if _src_parent.exists() and (_src_parent / "package.json").exists():
    REMOTION_DIR = _src_parent
elif _cwd_based.exists() and (_cwd_based / "package.json").exists():
    REMOTION_DIR = _cwd_based
else:
    REMOTION_DIR = _src_parent


def _build_props(
    script: dict,
    image_paths: list[str],
    voiceover_path: str | None,
    subtitle_data: dict,
    stock_clips: list[dict | None],
    app_config: dict,
) -> dict:
    """
    Build Remotion VideoProps for the StockVideoNarration template.

    Priority per slide:
    1. Stock video clip (if fetched) -> contentType: "stock"
    2. Screen recording (if source is video_footage:) -> contentType: "app_recording"
    3. AI-generated image (fallback) -> contentType: "ai_image"
    """
    slides = []
    slide_timings = subtitle_data.get("slide_timings", [])

    for i, slide in enumerate(script.get("slides", [])):
        slide_type = slide.get("slide_type", "value")

        # Duration from subtitle timings
        if i < len(slide_timings):
            timing = slide_timings[i]
            duration = timing.get("end", 0) - timing.get("start", 0)
        else:
            duration = slide.get("duration_seconds", 3)
        duration = max(duration, 1.5)

        slide_data = {
            "type": slide_type,
            "overlayText": slide.get("voiceover", ""),
            "duration": round(duration, 2),
        }

        # Determine content type and source
        source = slide.get("source", "ai_generated")

        # Priority 1: Stock video clip
        if i < len(stock_clips) and stock_clips[i] is not None:
            clip = stock_clips[i]
            clip_path = Path(clip["path"])
            if clip_path.exists():
                slide_data["videoClipPath"] = str(clip_path.resolve())
                slide_data["contentType"] = "stock"

        # Priority 2: Screen recording / existing video footage
        elif slide.get("_footage_path") or slide.get("_is_video_clip"):
            footage_path = slide.get("_footage_path", "")
            if footage_path and os.path.exists(footage_path):
                slide_data["videoClipPath"] = str(Path(footage_path).resolve())
                slide_data["contentType"] = "app_recording"
                # Pass clip_start/clip_end for precise trimming
                cs = slide.get("clip_start")
                ce = slide.get("clip_end")
                if cs is not None:
                    slide_data["clipStartSeconds"] = float(cs)
                if ce is not None:
                    slide_data["clipEndSeconds"] = float(ce)

        # No video found — use dark gradient (Remotion template handles this)
        if "videoClipPath" not in slide_data:
            slide_data["contentType"] = "stock"  # Still renders as full-screen with gradient fallback

        slides.append(slide_data)

    # Word timings for subtitles
    word_timings = []
    for w in subtitle_data.get("words", []):
        word_timings.append({
            "word": str(w.get("word", "")),
            "start": round(float(w.get("start", 0)), 3),
            "end": round(float(w.get("end", 0)), 3),
        })

    # Brand colors
    text_style = app_config.get("text_style", {})
    brand_colors = {
        "primary": text_style.get("highlight_color", "#4FC3F7"),
        "secondary": text_style.get("subtitle_bg_color", "#1a1a2e").replace("rgba(0,0,0,0.85)", "#1a1a2e"),
        "accent": text_style.get("highlight_color", "#FFD700"),
    }

    return {
        "templateId": app_config.get("_remotion_template", "stock-narration"),
        "slides": slides,
        "audioPath": str(Path(voiceover_path).resolve()) if voiceover_path else "",
        "wordTimings": word_timings,
        "brandColors": brand_colors,
        "appName": app_config.get("app_name", ""),
        "appHandle": app_config.get("tiktok_handle", ""),
        "backgroundMusicPath": app_config.get("_music_path", ""),
        "backgroundMusicVolume": app_config.get("tuning", {}).get("music_volume", 0.08),
    }


def assemble_video_remotion_stock(
    script: dict,
    image_paths: list[str],
    voiceover_path: str | None,
    subtitle_data: dict,
    output_path: str,
    app_config: dict,
) -> str:
    """
    Assemble a video using stock footage + Remotion.

    1. Fetch stock video clips for each slide (based on script source tags)
    2. Build Remotion props with contentType tagging
    3. Copy/trim/compress assets into Remotion public/
    4. Render via Remotion CLI
    5. Return output path

    Same interface as assemble_video_moviepy / assemble_video_remotion.
    """
    logger.info("Fetching stock video clips...")

    # ── Step 1: Fetch stock clips ─────────────────────────────────
    stock_dir = Path(output_path).parent / "stock_clips"
    stock_dir.mkdir(parents=True, exist_ok=True)

    # Broader fallback queries when specific ones fail
    _FALLBACK_QUERIES = [
        "cooking kitchen food", "woman smiling happy", "grocery store shopping",
        "family dinner table", "healthy meal plate", "phone app scrolling",
        "morning routine kitchen", "vegetables cutting board", "person walking city",
    ]

    stock_clips = []
    for i, slide in enumerate(script.get("slides", [])):
        source = slide.get("source", "ai_generated")

        if source.startswith("stock:") or source.startswith("stock_video:"):
            query = source.split(":", 1)[1].strip()
            logger.info(f"Slide {i}: fetching stock for '{query}'")
            clip = fetch_best_clip(
                query=query,
                output_dir=str(stock_dir),
                orientation="vertical",
                ai_tag=True,
            )
            # Retry with simplified query if first attempt fails
            if not clip and len(query.split()) > 2:
                simple_query = " ".join(query.split()[:2])
                logger.info(f"Slide {i}: retrying with simpler query '{simple_query}'")
                clip = fetch_best_clip(query=simple_query, output_dir=str(stock_dir), orientation="vertical", ai_tag=True)
            # Last resort: use a generic fallback query
            if not clip:
                import random as _rnd
                fallback = _rnd.choice(_FALLBACK_QUERIES)
                logger.info(f"Slide {i}: trying fallback query '{fallback}'")
                clip = fetch_best_clip(query=fallback, output_dir=str(stock_dir), orientation="vertical", ai_tag=True)

            stock_clips.append(clip)  # Could still be None but very unlikely now
        else:
            stock_clips.append(None)

    clips_attempted = sum(1 for s in script.get("slides", []) if s.get("source", "").startswith("stock"))
    clips_found = sum(1 for c in stock_clips if c is not None)
    logger.info(f"Found {clips_found}/{clips_attempted} stock clips")

    # ── Step 2: Build Remotion props ──────────────────────────────
    props = _build_props(
        script=script,
        image_paths=image_paths,
        voiceover_path=voiceover_path,
        subtitle_data=subtitle_data,
        stock_clips=stock_clips,
        app_config=app_config,
    )

    # ── Copy assets into Remotion's public/ so staticFile() can find them ──
    import random as _rnd
    render_id = f"stock_{os.getpid()}_{_rnd.randint(1000,9999)}"
    asset_dir = REMOTION_DIR / "public" / render_id
    asset_dir.mkdir(parents=True, exist_ok=True)

    # Copy video clips and images — compress/trim videos for Chrome compatibility
    for si, slide in enumerate(props["slides"]):
        for key in ("videoClipPath", "imagePath"):
            src = slide.get(key, "")
            if src and Path(src).exists() and not src.startswith("public/"):
                src_size = Path(src).stat().st_size
                is_video = src.lower().endswith((".mov", ".mp4", ".avi", ".mkv"))

                # Check if this clip needs trimming (clip_start/clip_end)
                clip_start = slide.get("clipStartSeconds")
                clip_end = slide.get("clipEndSeconds")
                needs_trim = is_video and clip_start is not None and clip_end is not None and clip_end > clip_start

                if is_video and (src_size > 5 * 1024 * 1024 or needs_trim):
                    # Compress and/or trim video with ffmpeg
                    dest_name = f"slide_{si}_processed.mp4"
                    dest = asset_dir / dest_name
                    ffmpeg_cmd = ["ffmpeg", "-y"]
                    # Trim if clip_start/clip_end specified
                    if needs_trim:
                        ffmpeg_cmd += ["-ss", str(clip_start), "-to", str(clip_end)]
                        logger.info(f"Trimming slide {si}: {clip_start:.1f}s-{clip_end:.1f}s")
                    ffmpeg_cmd += ["-i", src, "-vf", "scale=720:-2",
                                   "-c:v", "libx264", "-crf", "23", "-preset", "ultrafast",
                                   "-an", str(dest)]
                    try:
                        ff_result = subprocess.run(ffmpeg_cmd, capture_output=True, timeout=120)
                        if ff_result.returncode == 0 and dest.exists() and dest.stat().st_size > 1000:
                            logger.info(f"Processed slide {si}: {src_size//1024//1024}MB -> {dest.stat().st_size//1024//1024}MB")
                            slide.pop("clipStartSeconds", None)
                            slide.pop("clipEndSeconds", None)
                        else:
                            logger.warning(f"ffmpeg failed for slide {si} (rc={ff_result.returncode}), copying original")
                            if dest.exists():
                                dest.unlink()
                            shutil.copy2(src, dest)
                    except Exception as ff_err:
                        logger.error(f"ffmpeg error slide {si}: {ff_err}")
                        shutil.copy2(src, dest)
                else:
                    dest_name = f"slide_{si}_{Path(src).name}"
                    dest = asset_dir / dest_name
                    shutil.copy2(src, dest)
                slide[key] = f"public/{render_id}/{dest_name}"

    # Copy voiceover audio
    if props.get("audioPath") and Path(props["audioPath"]).exists():
        audio_dest = asset_dir / "voiceover.wav"
        shutil.copy2(props["audioPath"], audio_dest)
        props["audioPath"] = f"public/{render_id}/voiceover.wav"

    logger.info(f"Assets copied to public/{render_id}/")

    # Log content type mix for hybrid debugging
    type_counts = {}
    for s in props["slides"]:
        ct = s.get("contentType", "unknown")
        type_counts[ct] = type_counts.get(ct, 0) + 1
    logger.info(f"Content mix: {type_counts}")

    # Write props to temp file for Remotion CLI
    props_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="remotion_stock_"
    )
    json.dump(props, props_file, default=str)
    props_file.close()

    logger.info(f"Props written to {props_file.name}")

    # ── Step 3: Render via Remotion CLI ───────────────────────────
    slide_duration = sum(s["duration"] for s in props["slides"])

    # Ensure video is at least as long as the voiceover audio
    audio_duration = 0
    if voiceover_path and os.path.exists(voiceover_path):
        try:
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", voiceover_path],
                capture_output=True, text=True, timeout=10,
            )
            audio_duration = float(probe.stdout.strip())
        except Exception:
            pass

    total_duration = max(slide_duration, audio_duration + 0.5)
    if audio_duration > slide_duration:
        # Extend last slide to cover remaining audio
        gap = total_duration - slide_duration
        props["slides"][-1]["duration"] = round(props["slides"][-1]["duration"] + gap, 2)
        logger.info(f"Extended last slide by {gap:.1f}s to match voiceover ({audio_duration:.1f}s)")

    total_frames = int(total_duration * 30)

    cmd = [
        "npx", "remotion", "render",
        "TikTokVideo",
        "--output", str(Path(output_path).resolve()),
        "--props", props_file.name,
        "--codec", "h264",
        "--image-format", "jpeg",
        "--jpeg-quality", "90",
        "--concurrency", "2",
        "--timeout", "300000",
    ]

    logger.info(f"Rendering {total_duration:.1f}s video ({total_frames} frames)...")

    render_success = False
    try:
        result = subprocess.run(
            cmd,
            cwd=str(REMOTION_DIR),
            capture_output=True,
            text=True,
            timeout=600,
            env=os.environ.copy(),
        )

        if result.returncode != 0:
            logger.error(f"Render FAILED:\n{result.stderr[-500:]}")
            raise RuntimeError(f"Remotion render failed: {result.stderr[-300:]}")

        # Verify output file exists and is valid
        if os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
            render_success = True
            logger.info(f"Render complete: {output_path} ({os.path.getsize(output_path)//1024}KB)")
        else:
            raise RuntimeError(f"Render output missing or too small: {output_path}")

    finally:
        # Clean up temp props file
        try:
            os.unlink(props_file.name)
        except OSError:
            pass
        # Only clean up assets AFTER confirming render succeeded
        if render_success:
            try:
                shutil.rmtree(asset_dir, ignore_errors=True)
            except OSError:
                pass

    return output_path


def extract_key_frames(video_path: str, output_dir: str, num_frames: int = 5) -> list[str]:
    """Extract key frames from rendered video for QA review."""
    import subprocess
    from pathlib import Path

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Get video duration
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
        capture_output=True, text=True, timeout=10,
    )
    try:
        duration = float(probe.stdout.strip())
    except (ValueError, AttributeError):
        duration = 10.0

    frame_paths = []
    for i in range(num_frames):
        t = (i + 0.5) * duration / num_frames
        fp = str(out / f"frame_{i:02d}.jpg")
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(t), "-i", video_path,
             "-frames:v", "1", "-q:v", "2", fp],
            capture_output=True, timeout=15,
        )
        if os.path.exists(fp):
            frame_paths.append(fp)

    return frame_paths
