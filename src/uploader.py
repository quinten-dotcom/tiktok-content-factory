from __future__ import annotations

"""
TikTok Uploader — Posts videos to TikTok via multiple methods.

Supports:
1. TikTok Content Posting API (official, recommended)
2. tiktok-uploader Python package (browser automation, free)
3. Upload-Post.com API (third-party, simple)

Also supports cross-posting to Instagram Reels and YouTube Shorts
via third-party services.
"""

import os
import json
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta
import random
from log_config import get_logger

logger = get_logger(__name__)


# ─── TIKTOK CONTENT POSTING API (OFFICIAL) ──────────────────────────────────

class TikTokOfficialUploader:
    """
    Upload via TikTok's official Content Posting API.

    Requires:
    - TikTok Developer Account
    - Approved app with content_posting scope
    - OAuth access token per TikTok account

    Rate limit: 15 uploads per 24-hour rolling window per account.
    """

    BASE_URL = "https://open.tiktokapis.com/v2"

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def upload_video(
        self,
        video_path: str,
        title: str,
        hashtags: list[str] = None,
        schedule_time: datetime | None = None,
        privacy_level: str = "PUBLIC_TO_EVERYONE",
    ) -> dict:
        """
        Upload a video to TikTok.

        Args:
            video_path: Path to the MP4 file
            title: Video caption/description
            hashtags: List of hashtags (without #)
            schedule_time: When to publish (None = immediately)
            privacy_level: PUBLIC_TO_EVERYONE, MUTUAL_FOLLOW_FRIENDS, SELF_ONLY
        """
        # Step 1: Initialize upload
        file_size = os.path.getsize(video_path)

        init_payload = {
            "post_info": {
                "title": self._build_caption(title, hashtags),
                "privacy_level": privacy_level,
                "disable_duet": False,
                "disable_comment": False,
                "disable_stitch": False,
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,
                "total_chunk_count": 1,
            },
        }

        if schedule_time:
            init_payload["post_info"]["schedule_publish_time"] = int(schedule_time.timestamp())

        init_resp = requests.post(
            f"{self.BASE_URL}/post/publish/inbox/video/init/",
            headers=self.headers,
            json=init_payload,
        )

        if init_resp.status_code != 200:
            raise Exception(f"TikTok init failed: {init_resp.status_code} — {init_resp.text}")

        init_data = init_resp.json()["data"]
        upload_url = init_data["upload_url"]
        publish_id = init_data["publish_id"]

        # Step 2: Upload video binary
        with open(video_path, "rb") as f:
            video_data = f.read()

        upload_resp = requests.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{file_size - 1}/{file_size}",
            },
            data=video_data,
        )

        if upload_resp.status_code not in [200, 201]:
            raise Exception(f"TikTok upload failed: {upload_resp.status_code}")

        return {
            "publish_id": publish_id,
            "status": "uploaded",
            "scheduled": schedule_time.isoformat() if schedule_time else "immediate",
        }

    def _build_caption(self, title: str, hashtags: list[str] | None) -> str:
        """Build the TikTok caption with hashtags."""
        caption = title
        if hashtags:
            tag_str = " ".join(f"#{tag.lstrip('#')}" for tag in hashtags)
            caption = f"{title} {tag_str}"
        return caption[:2200]  # TikTok caption limit

    def check_publish_status(self, publish_id: str) -> dict:
        """Check the status of a published video."""
        resp = requests.post(
            f"{self.BASE_URL}/post/publish/status/fetch/",
            headers=self.headers,
            json={"publish_id": publish_id},
        )
        return resp.json()


# ─── TIKTOK-UPLOADER PACKAGE (BROWSER AUTOMATION) ───────────────────────────

class TikTokBrowserUploader:
    """
    Upload via the tiktok-uploader Python package.

    Uses browser automation (Playwright). Free, no API approval needed.
    Requires: pip install tiktok-uploader

    Less reliable than official API but zero setup friction.
    """

    def upload_video(
        self,
        video_path: str,
        description: str,
        cookies_path: str,
        hashtags: list[str] = None,
        schedule_time: datetime | None = None,
    ) -> dict:
        """
        Upload a video using browser automation.

        Args:
            video_path: Path to MP4
            description: Video caption
            cookies_path: Path to TikTok cookies file (exported from browser)
            hashtags: Hashtag list
            schedule_time: When to publish (max 10 days ahead)
        """
        try:
            from tiktok_uploader.upload import upload_video as ttu_upload
            from tiktok_uploader.upload import upload_videos as ttu_upload_batch
        except ImportError:
            raise ImportError("Install tiktok-uploader: pip install tiktok-uploader")

        # Build description with hashtags
        if hashtags:
            tag_str = " ".join(f"#{tag.lstrip('#')}" for tag in hashtags)
            description = f"{description} {tag_str}"

        # Schedule parameter
        schedule = None
        if schedule_time:
            schedule = schedule_time

        result = ttu_upload(
            filename=video_path,
            description=description,
            cookies=cookies_path,
            schedule=schedule,
        )

        return {"status": "uploaded", "method": "browser_automation", "result": str(result)}


# ─── UPLOAD SCHEDULER ────────────────────────────────────────────────────────

MAX_UPLOAD_RETRIES = 3  # Stop retrying after this many failures

class UploadScheduler:
    """
    Manages the upload queue for multiple TikTok accounts.

    Spaces uploads throughout the day with random jitter to appear natural.
    Respects TikTok's 15/day rolling-window rate limit per account.

    Queue lifecycle: queued → scheduled → uploaded (success) or failed (after max retries).
    Completed/failed entries are archived to keep the queue directory clean.
    """

    # Optimal posting times (hours in local time)
    PEAK_HOURS = [7, 9, 11, 13, 15, 17, 19, 20, 21, 22]

    def __init__(self, queue_dir: str = "output/upload_queue"):
        self.queue_dir = Path(queue_dir)
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir = self.queue_dir / "archive"
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.queue_dir / "upload_log.json"

    def _safe_read_json(self, file_path: Path) -> dict | None:
        """Read a JSON file safely — returns None if file is corrupt or being written."""
        try:
            with open(file_path) as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read {file_path.name}: {e}")
            return None

    def _safe_write_json(self, file_path: Path, data: dict):
        """Write JSON atomically — write to temp file then rename to avoid corruption."""
        tmp_path = file_path.with_suffix(".tmp")
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=2)
            tmp_path.replace(file_path)  # Atomic on most filesystems
        except OSError as e:
            logger.error(f"Error writing {file_path.name}: {e}")
            if tmp_path.exists():
                tmp_path.unlink()

    def queue_video(
        self,
        video_path: str,
        account_handle: str,
        title: str,
        description: str,
        hashtags: list[str],
    ) -> dict | None:
        """Add a video to the upload queue. Validates the video file exists and is non-empty."""
        # Validate video file before queuing
        if not os.path.exists(video_path):
            logger.error(f"Cannot queue — video file not found: {video_path}")
            return None
        file_size = os.path.getsize(video_path)
        if file_size < 10000:  # Less than 10KB is definitely broken
            logger.error(f"Cannot queue — video file too small ({file_size} bytes): {video_path}")
            return None

        entry = {
            "video_path": video_path,
            "account": account_handle,
            "title": title,
            "description": description,
            "hashtags": hashtags,
            "queued_at": datetime.now().isoformat(),
            "status": "queued",
            "scheduled_time": None,
            "attempt_count": 0,
            "last_error": None,
        }

        # Save to queue
        queue_file = self.queue_dir / f"{account_handle.lstrip('@')}_{int(time.time())}.json"
        self._safe_write_json(queue_file, entry)
        logger.info(f"Queued: {title} ({file_size // 1024}KB)")

        return entry

    def schedule_daily_uploads(self, account_handle: str, videos_per_day: int = 7) -> list[dict]:
        """
        Assign posting times to queued videos for an account.

        Distributes uploads across optimal hours with random jitter.
        Only schedules FUTURE times — never assigns a time that's already past.
        Processes up to videos_per_day, prioritizing oldest queued videos first.
        """
        account_key = account_handle.lstrip("@")

        # Find queued videos for this account (oldest first)
        queued = []
        for f in sorted(self.queue_dir.glob(f"{account_key}_*.json")):
            entry = self._safe_read_json(f)
            if entry and entry.get("status") == "queued":
                queued.append((f, entry))

        if not queued:
            return []

        # Build list of future posting times
        now = datetime.now().replace(second=0, microsecond=0)
        future_slots = []

        # Today's remaining peak hours
        for hour in self.PEAK_HOURS:
            slot_time = now.replace(hour=hour, minute=30)
            if slot_time > now + timedelta(minutes=15):  # At least 15min in the future
                future_slots.append(slot_time)

        # If not enough slots today, add tomorrow's peak hours
        if len(future_slots) < videos_per_day:
            tomorrow = now + timedelta(days=1)
            for hour in self.PEAK_HOURS:
                slot_time = tomorrow.replace(hour=hour, minute=30)
                future_slots.append(slot_time)

        random.shuffle(future_slots)
        future_slots = sorted(future_slots[:videos_per_day])  # Sort chronologically

        scheduled = []
        for i, (file_path, entry) in enumerate(queued[:videos_per_day]):
            if i >= len(future_slots):
                break

            # Add jitter for natural appearance
            jitter_minutes = random.randint(-12, 12)
            post_time = future_slots[i] + timedelta(minutes=jitter_minutes)
            # Ensure jitter doesn't push into the past
            if post_time <= now:
                post_time = now + timedelta(minutes=5)

            entry["scheduled_time"] = post_time.isoformat()
            entry["status"] = "scheduled"

            self._safe_write_json(file_path, entry)
            scheduled.append(entry)
            logger.info(f"Scheduled: {entry['title'][:40]} → {post_time.strftime('%Y-%m-%d %H:%M')}")

        # Report any orphaned videos that didn't get scheduled
        remaining = len(queued) - len(scheduled)
        if remaining > 0:
            logger.info(f"Note: {remaining} older queued video(s) not scheduled yet (will be picked up next run)")

        return scheduled

    def get_pending_uploads(self, account_handle: str | None = None) -> list[dict]:
        """Get all scheduled uploads that are ready to post (time has passed)."""
        now = datetime.now()
        ready = []

        for f in self.queue_dir.glob("*.json"):
            if f.name == "upload_log.json" or f.suffix == ".tmp":
                continue

            entry = self._safe_read_json(f)
            if not entry:
                continue

            if entry.get("status") != "scheduled":
                continue

            if account_handle and entry.get("account") != account_handle:
                continue

            scheduled_time = entry.get("scheduled_time")
            if not scheduled_time:
                logger.warning(f"Missing scheduled_time in {f.name}, skipping")
                continue
            try:
                scheduled = datetime.fromisoformat(scheduled_time)
            except (ValueError, TypeError):
                logger.warning(f"Invalid scheduled_time in {f.name}, skipping")
                continue

            if scheduled <= now:
                entry["_file_path"] = str(f)
                ready.append(entry)

        return ready

    def mark_uploaded(self, entry: dict, result: dict):
        """Mark a video as successfully uploaded and archive it."""
        file_path = entry.get("_file_path")
        if not file_path or not os.path.exists(file_path):
            return

        entry["status"] = "uploaded"
        entry["uploaded_at"] = datetime.now().isoformat()
        entry["upload_result"] = result
        entry.pop("_file_path", None)  # Don't persist internal field

        # Archive the completed entry (move out of active queue)
        src = Path(file_path)
        dest = self.archive_dir / src.name
        self._safe_write_json(dest, entry)
        try:
            src.unlink()
        except OSError:
            pass

        # Log it
        self._log_upload(entry)
        logger.info(f"Uploaded and archived: {entry.get('title', 'unknown')}")

    def mark_failed(self, entry: dict, error: str):
        """Mark an upload attempt as failed. Archives permanently after max retries."""
        file_path = entry.get("_file_path")
        if not file_path or not os.path.exists(file_path):
            return

        attempt_count = entry.get("attempt_count", 0) + 1
        entry["attempt_count"] = attempt_count
        entry["last_error"] = error
        entry["last_attempt_at"] = datetime.now().isoformat()

        if attempt_count >= MAX_UPLOAD_RETRIES:
            # Max retries reached — archive as permanently failed
            entry["status"] = "failed"
            entry.pop("_file_path", None)
            src = Path(file_path)
            dest = self.archive_dir / src.name
            self._safe_write_json(dest, entry)
            try:
                src.unlink()
            except OSError:
                pass
            self._log_upload(entry)
            logger.error(f"PERMANENTLY FAILED after {attempt_count} attempts: {entry.get('title', 'unknown')} — {error}")
        else:
            # Keep as scheduled for retry — but push scheduled_time forward
            retry_delay = timedelta(minutes=30 * attempt_count)  # 30min, 60min, 90min...
            entry["scheduled_time"] = (datetime.now() + retry_delay).isoformat()
            entry["status"] = "scheduled"
            self._safe_write_json(Path(file_path), entry)
            logger.warning(f"Upload failed (attempt {attempt_count}/{MAX_UPLOAD_RETRIES}), retrying in {30 * attempt_count}min: {error[:100]}")

    def _log_upload(self, entry: dict):
        """Append to the upload log."""
        log = []
        if self.log_path.exists():
            try:
                with open(self.log_path) as f:
                    log = json.load(f)
            except (json.JSONDecodeError, OSError):
                log = []

        log.append({
            "account": entry.get("account", "unknown"),
            "title": entry.get("title", "unknown"),
            "uploaded_at": entry.get("uploaded_at"),
            "status": entry["status"],
            "attempt_count": entry.get("attempt_count", 1),
            "error": entry.get("last_error"),
        })

        self._safe_write_json(self.log_path, log)

    def get_daily_upload_count(self, account_handle: str) -> int:
        """Check how many uploads an account has done in the rolling 24-hour window."""
        if not self.log_path.exists():
            return 0

        try:
            with open(self.log_path) as f:
                log = json.load(f)
        except (json.JSONDecodeError, OSError):
            return 0

        # Use rolling 24-hour window (not calendar day)
        cutoff = datetime.now() - timedelta(hours=24)
        count = 0
        for entry in log:
            if entry.get("account") == account_handle and entry.get("uploaded_at") and entry.get("status") == "uploaded":
                try:
                    upload_time = datetime.fromisoformat(entry["uploaded_at"])
                    if upload_time > cutoff:
                        count += 1
                except (ValueError, TypeError):
                    pass

        return count

    def get_queue_status(self) -> dict:
        """Get a summary of the current queue state — useful for debugging."""
        counts = {"queued": 0, "scheduled": 0, "uploaded": 0, "failed": 0}
        for f in self.queue_dir.glob("*.json"):
            if f.name == "upload_log.json" or f.suffix == ".tmp":
                continue
            entry = self._safe_read_json(f)
            if entry:
                status = entry.get("status", "unknown")
                counts[status] = counts.get(status, 0) + 1
        # Count archived
        for f in self.archive_dir.glob("*.json"):
            entry = self._safe_read_json(f)
            if entry:
                status = entry.get("status", "unknown")
                counts[f"archived_{status}"] = counts.get(f"archived_{status}", 0) + 1
        return counts


if __name__ == "__main__":
    logger.info("Uploader module loaded.")
    logger.info("Supports: TikTok Official API, tiktok-uploader package, Upload-Post API")
    logger.info("Run via pipeline.py for full upload automation.")
