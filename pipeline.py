#!/usr/bin/env python3
from __future__ import annotations
"""
TikTok Content Factory — Main Pipeline

This is the master orchestrator. Run it to:
1. Generate scripts for all configured apps
2. Generate images for each video
3. Generate voiceovers
4. Assemble final videos
5. QA review via Claude Vision
6. Queue for TikTok upload

Usage:
    # Generate + assemble videos for one app
    python pipeline.py generate --app config/example_app.json

    # Generate for all apps in config/
    python pipeline.py generate --all

    # Upload queued videos
    python pipeline.py upload

    # Full daily run (generate + upload)
    python pipeline.py daily

    # Onboard a new app interactively
    python pipeline.py onboard

    # Review a single video
    python pipeline.py review --video output/videos/some_video.mp4
"""

import os
import sys
import json
import argparse
import time
import requests
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

# Load environment
load_dotenv()

# Add src to path — check both locations (direct src/ and nested tiktok-content-factory/src/)
_src_dir = Path(__file__).parent / "src"
_nested_src_dir = Path(__file__).parent / "tiktok-content-factory" / "src"
if _src_dir.exists():
    sys.path.insert(0, str(_src_dir))
elif _nested_src_dir.exists():
    sys.path.insert(0, str(_nested_src_dir))
else:
    sys.path.insert(0, str(_src_dir))
    sys.path.insert(0, str(_nested_src_dir))

from log_config import get_logger
logger = get_logger("pipeline")

from script_generator import load_app_config, generate_scripts, save_scripts
from image_generator import (
    generate_images_for_script,
    generate_reference_image,
)
from voice_generator import generate_voiceover_for_script
from subtitle_generator import (
    generate_word_timestamps,
    group_words_into_lines,
    calculate_slide_timings,
    save_subtitle_data,
)
from video_assembler import assemble_video, extract_key_frames
from qa_reviewer import review_video, save_review
from uploader import UploadScheduler


# ─── CONFIGURATION ───────────────────────────────────────────────────────────

VIDEOS_PER_DAY = int(os.environ.get("VIDEOS_PER_DAY", 7))
VOICE_ENGINE = os.environ.get("VOICE_ENGINE", "elevenlabs")
IMAGE_ENGINE = os.environ.get("IMAGE_ENGINE", "flux_schnell")
QA_THRESHOLD = float(os.environ.get("QA_THRESHOLD", 7.0))
MAX_RETRIES = 2

OUTPUT_BASE = Path("output")
REFERENCE_IMAGES_DIR = OUTPUT_BASE / "reference_images"


# ─── GENERATE PIPELINE ──────────────────────────────────────────────────────

def generate_for_app(config_path: str, count: int | None = None) -> list[str]:
    """
    Run the full generation pipeline for a single app.

    Steps:
    1. Load app config
    2. Generate scripts (Claude API)
    3. For each script:
       a. Generate images (Flux via fal.ai)
       b. Generate voiceover (ElevenLabs or Kokoro)
       c. Generate subtitles (Whisper)
       d. Assemble video (MoviePy)
       e. QA review (Claude Vision)
       f. Queue for upload

    Returns list of video file paths.
    """
    app_config = load_app_config(config_path)
    app_name = app_config["app_name"]
    num_videos = count or VIDEOS_PER_DAY

    logger.info(f"\n{'='*60}")
    logger.info(f"  GENERATING {num_videos} VIDEOS FOR: {app_name}")
    logger.info(f"{'='*60}")

    # Setup output dirs
    today = datetime.now().strftime("%Y-%m-%d")
    app_slug = app_name.lower().replace(" ", "_")
    base_dir = OUTPUT_BASE / app_slug / today
    scripts_dir = base_dir / "scripts"
    videos_dir = base_dir / "videos"

    # ─── Step 1: Ensure reference images exist for each persona ──────
    logger.info("\n[1/6] Checking persona reference images...")
    REFERENCE_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

    for persona in app_config["personas"]:
        ref_path = REFERENCE_IMAGES_DIR / f"{app_slug}_{persona['id']}.png"
        if not ref_path.exists():
            logger.info(f"  Generating reference image for {persona['name']}...")
            generate_reference_image(persona, str(ref_path))
        else:
            logger.info(f"  Reference image exists for {persona['name']}")

    # ─── Step 2: Generate scripts ────────────────────────────────────
    logger.info(f"\n[2/6] Generating {num_videos} scripts...")
    scripts = generate_scripts(app_config, count=num_videos)
    script_paths = save_scripts(scripts, str(scripts_dir))
    logger.info(f"  Generated {len(scripts)} scripts")

    # ─── Step 3-6: Process each script ───────────────────────────────
    video_paths = []
    scheduler = UploadScheduler()

    for i, script in enumerate(scripts):
        logger.info(f"\n--- Video {i+1}/{len(scripts)}: {script.get('title', 'Untitled')} ---")

        video_dir = base_dir / f"video_{i:03d}"
        video_dir.mkdir(parents=True, exist_ok=True)

        success = False
        for attempt in range(MAX_RETRIES + 1):
            if attempt > 0:
                logger.warning(f"  Retry {attempt}/{MAX_RETRIES}...")

            try:
                video_path = _process_single_video(
                    script=script,
                    app_config=app_config,
                    video_dir=video_dir,
                    video_index=i,
                    videos_dir=videos_dir,
                    app_slug=app_slug,
                )

                if video_path:
                    video_paths.append(video_path)

                    # Queue for upload (validates file before adding)
                    queued = scheduler.queue_video(
                        video_path=video_path,
                        account_handle=app_config["tiktok_handle"],
                        title=script.get("title", ""),
                        description=script.get("description", ""),
                        hashtags=script.get("hashtags", []),
                    )
                    if queued:
                        success = True
                    else:
                        logger.warning(f"  WARNING: Video created but failed queue validation")
                    break

            except Exception as e:
                logger.error(f"  ERROR: {e}", exc_info=True)
                if attempt == MAX_RETRIES:
                    logger.error(f"  SKIPPING video after {MAX_RETRIES} retries")

        if not success:
            logger.error(f"  FAILED: Could not generate video {i+1}")

    # Schedule uploads
    logger.info(f"\n[DONE] Generated {len(video_paths)}/{len(scripts)} videos for {app_name}")
    scheduled = scheduler.schedule_daily_uploads(
        app_config["tiktok_handle"],
        videos_per_day=num_videos,
    )
    logger.info(f"  Scheduled {len(scheduled)} uploads for today")

    return video_paths


def _process_single_video(
    script: dict,
    app_config: dict,
    video_dir: Path,
    video_index: int,
    videos_dir: Path,
    app_slug: str,
) -> str | None:
    """Process a single video through the full pipeline."""

    persona = script.get("persona", app_config["personas"][0])
    persona_id = persona.get("id", "default")

    # ─── Step 3: Generate images ─────────────────────────────────
    logger.info(f"  [3/6] Generating images...")
    images_dir = video_dir / "images"

    ref_path = REFERENCE_IMAGES_DIR / f"{app_slug}_{persona_id}.png"
    ref_image = str(ref_path) if ref_path.exists() else None

    image_paths = generate_images_for_script(
        script=script,
        output_dir=str(images_dir),
        app_config=app_config,
        reference_image_path=ref_image,
        engine=IMAGE_ENGINE,
    )

    # ─── Step 4: Generate voiceover ──────────────────────────────
    logger.info(f"  [4/6] Generating voiceover...")
    voice_dir = video_dir / "audio"
    voiceover_path = generate_voiceover_for_script(
        script=script,
        output_dir=str(voice_dir),
        engine=VOICE_ENGINE,
    )

    # ─── Step 5: Generate subtitles + timing ─────────────────────
    logger.info(f"  [5/6] Generating subtitles...")
    if voiceover_path:
        words = generate_word_timestamps(voiceover_path)
        lines = group_words_into_lines(words)
        slide_timings = calculate_slide_timings(words, script)
    else:
        # No voiceover (trending sound format) — equal timing
        words, lines = [], []
        slide_timings = [
            {"slide_index": i, "start": i * 3.0, "end": (i + 1) * 3.0}
            for i in range(len(script["slides"]))
        ]

    subtitle_data = {
        "words": words,
        "lines": lines,
        "slide_timings": slide_timings,
    }
    save_subtitle_data(words, lines, slide_timings, str(video_dir))

    # ─── Step 6: Assemble video ──────────────────────────────────
    logger.info(f"  [6/6] Assembling video...")
    videos_dir.mkdir(parents=True, exist_ok=True)

    timestamp = int(time.time())
    video_filename = f"{app_slug}_{persona_id}_{video_index:03d}_{timestamp}.mp4"
    video_path = str(videos_dir / video_filename)

    assemble_video(
        script=script,
        image_paths=image_paths,
        voiceover_path=voiceover_path,
        subtitle_data=subtitle_data,
        output_path=video_path,
        text_style=app_config.get("text_style"),
    )

    # ─── QA Review ───────────────────────────────────────────────
    if QA_THRESHOLD > 0:
        logger.info(f"  [QA] Reviewing video quality...")
        frames_dir = video_dir / "qa_frames"
        frame_paths = extract_key_frames(video_path, str(frames_dir))

        review = review_video(
            frame_paths=frame_paths,
            script=script,
            app_config=app_config,
            threshold=QA_THRESHOLD,
        )

        save_review(review, str(video_dir / "qa_review.json"))
        score = review.get("overall_score", 0)
        passed = review.get("pass", False)

        logger.info(f"  [QA] Score: {score}/10 — {'PASS' if passed else 'FAIL'}")

        if not passed:
            # Keep the video in the vault (don't delete!) — rename to mark as failed
            failed_path = video_path.replace(".mp4", "_FAILED.mp4")
            try:
                os.rename(video_path, failed_path)
                logger.warning(f"  [QA] Video below threshold ({QA_THRESHOLD}). Saved to vault as: {Path(failed_path).name}")
            except OSError:
                logger.warning(f"  [QA] Video below threshold ({QA_THRESHOLD}). Kept at: {Path(video_path).name}")
            return None

    return video_path


# ─── UPLOAD PIPELINE ─────────────────────────────────────────────────────────

def upload_pending():
    """Upload all videos that are scheduled and ready."""
    scheduler = UploadScheduler()
    pending = scheduler.get_pending_uploads()

    if not pending:
        logger.info("No pending uploads ready.")
        return

    # Show queue status for visibility
    status = scheduler.get_queue_status()
    logger.info(f"\n[UPLOAD] Queue status: {status}")
    logger.info(f"[UPLOAD] Found {len(pending)} videos ready to upload:")

    for entry in pending:
        account = entry["account"]
        title = entry.get("title", "Untitled")
        attempt = entry.get("attempt_count", 0) + 1

        # Check daily limit (rolling 24h window)
        daily_count = scheduler.get_daily_upload_count(account)
        if daily_count >= 15:
            logger.warning(f"  SKIP {account}: Daily limit reached ({daily_count}/15 in last 24h)")
            continue

        logger.info(f"  Uploading (attempt {attempt}): {title} → {account}")

        # Validate video file still exists
        video_path = entry["video_path"]
        if not os.path.exists(video_path):
            scheduler.mark_failed(entry, f"Video file not found: {video_path}")
            continue

        if os.path.getsize(video_path) < 10000:
            scheduler.mark_failed(entry, f"Video file too small ({os.path.getsize(video_path)} bytes)")
            continue

        try:
            import requests as req

            api_key = os.environ.get("UPLOADPOST_API_KEY")
            if not api_key:
                logger.warning(f"    SKIP: Upload-Post API key not configured. Set UPLOADPOST_API_KEY in .env")
                # Don't mark as failed — this is a config issue, not a video issue
                continue

            with open(video_path, "rb") as f:
                files = {"video": f}
                headers = {"Authorization": f"Bearer {api_key}"}
                response = req.post(
                    "https://app.upload-post.com/api/upload",
                    files=files,
                    headers=headers,
                    timeout=300,
                )

            if response.status_code in (200, 201):
                result = response.json()
                scheduler.mark_uploaded(entry, result)
                logger.info(f"    Done! ({daily_count + 1}/15 in last 24h)")
            else:
                error_msg = f"Upload-Post returned {response.status_code}: {response.text[:200]}"
                scheduler.mark_failed(entry, error_msg)

        except requests.exceptions.Timeout:
            scheduler.mark_failed(entry, "Upload timed out after 300 seconds")
        except requests.exceptions.ConnectionError as e:
            scheduler.mark_failed(entry, f"Connection error: {e}")
        except Exception as e:
            scheduler.mark_failed(entry, f"Unexpected error: {e}")


# ─── DAILY RUN ───────────────────────────────────────────────────────────────

def daily_run():
    """Full daily pipeline: generate for all apps, then upload."""
    config_dir = Path("config")
    configs = list(config_dir.glob("*.json"))

    if not configs:
        logger.info("No app configs found in config/")
        return

    logger.info(f"\n{'#'*60}")
    logger.info(f"  DAILY RUN: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"  Apps: {len(configs)} | Videos/app: {VIDEOS_PER_DAY}")
    logger.info(f"  Total videos: {len(configs) * VIDEOS_PER_DAY}")
    logger.info(f"{'#'*60}")

    all_videos = []
    for config_path in configs:
        try:
            videos = generate_for_app(str(config_path))
            all_videos.extend(videos)
        except Exception as e:
            logger.error(f"\nERROR processing {config_path.name}: {e}", exc_info=True)
            continue

    logger.info(f"\n{'='*60}")
    logger.info(f"  GENERATION COMPLETE")
    logger.info(f"  Total videos created: {len(all_videos)}")
    logger.info(f"{'='*60}")

    # Now upload any that are ready
    logger.info("\nStarting uploads...")
    upload_pending()


# ─── ONBOARD NEW APP ────────────────────────────────────────────────────────

def onboard_app():
    """Interactive setup for a new app."""
    logger.info("\n=== ONBOARD NEW APP ===\n")

    app_name = input("App name: ")
    app_desc = input("One-line description: ")
    niche = input("Niche (e.g., productivity, fitness, finance): ")
    tiktok_handle = input("TikTok handle (e.g., @myapp): ")

    logger.info("\nContent pillars (what topics should videos cover)?")
    logger.info("Enter one per line, empty line to finish:")
    pillars = []
    while True:
        p = input("  > ")
        if not p:
            break
        pillars.append(p)

    # Generate config from template
    template_path = Path("config/example_app.json")
    if template_path.exists():
        with open(template_path) as f:
            config = json.load(f)
    else:
        config = {}

    config["app_name"] = app_name
    config["app_description"] = app_desc
    config["niche"] = niche
    config["tiktok_handle"] = tiktok_handle
    config["content_pillars"] = pillars if pillars else [f"{niche} tips", f"{niche} hacks", "app demos"]

    # Save config
    slug = app_name.lower().replace(" ", "_")
    config_path = f"config/{slug}.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    logger.info(f"\nConfig saved: {config_path}")
    logger.info("\nGenerating 3 sample videos for review...")

    generate_for_app(config_path, count=3)

    logger.info(f"\nDone! Review the sample videos in output/{slug}/")
    logger.info("If they look good, the app is ready for daily generation.")


# ─── CLI ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="TikTok Content Factory")
    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Generate
    gen_parser = subparsers.add_parser("generate", help="Generate videos")
    gen_parser.add_argument("--app", help="Path to app config JSON")
    gen_parser.add_argument("--all", action="store_true", help="Generate for all apps")
    gen_parser.add_argument("--count", type=int, help="Override video count")

    # Upload
    subparsers.add_parser("upload", help="Upload queued videos")

    # Daily
    subparsers.add_parser("daily", help="Full daily run (generate + upload)")

    # Onboard
    subparsers.add_parser("onboard", help="Set up a new app")

    args = parser.parse_args()

    if args.command == "generate":
        if args.all:
            for config_path in Path("config").glob("*.json"):
                generate_for_app(str(config_path), count=args.count)
        elif args.app:
            generate_for_app(args.app, count=args.count)
        else:
            parser.error("Specify --app <config.json> or --all")

    elif args.command == "upload":
        upload_pending()

    elif args.command == "daily":
        daily_run()

    elif args.command == "onboard":
        onboard_app()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
