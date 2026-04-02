#!/usr/bin/env python3
from __future__ import annotations
"""
TikTok Content Factory — Web Dashboard V2

Apple-clean design with full pipeline visibility.
Run this file and open the URL in your browser.
"""

import os
import sys
import io
import json
import time
import threading
import sqlite3
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template_string, request, jsonify, send_file
from dotenv import load_dotenv
from src.log_config import get_logger

load_dotenv()

logger = get_logger("app")

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

app = Flask(__name__)

# ─── DIRECTORIES ─────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
# Use Railway Volume for persistent data if available, else fall back to local
DATA_DIR = Path(os.environ.get("DATA_DIR", str(BASE_DIR / "data")))
DATA_DIR.mkdir(exist_ok=True)
UPLOAD_QUEUE_PATH = DATA_DIR / "upload_queue.json"
CONFIG_DIR = DATA_DIR / "config"
OUTPUT_DIR = DATA_DIR / "output"
IMAGE_LIBRARY_DIR = DATA_DIR / "image_library"
CONFIG_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
# Also persist .env in data dir so API keys survive deploys
PERSISTENT_ENV = DATA_DIR / ".env"
if PERSISTENT_ENV.exists() and not (BASE_DIR / ".env").exists():
    import shutil
    shutil.copy2(str(PERSISTENT_ENV), str(BASE_DIR / ".env"))
    load_dotenv(override=True)

# ─── TikTok Safety Limits ───
MAX_POSTS_PER_ACCOUNT_PER_DAY = 4
MIN_HOURS_BETWEEN_POSTS = 2
API_RATE_LIMIT_PER_MINUTE = 5
UPLOAD_QUEUE_CHECK_INTERVAL_MINUTES = 15
PEAK_HOURS = [(7, 9), (12, 13), (18, 21)]
AIGC_WATERMARK_TEXT = "AI-Generated"
AIGC_WATERMARK_OPACITY = 0.5
AIGC_DESCRIPTION_SUFFIX = "\n\n#AIgenerated #ai #aiart\nCreated with AI tools"
MAX_RETRY_ATTEMPTS = 3
RATE_LIMIT_BACKOFF_SECONDS = [30, 60, 120]

# ─── STATE ───────────────────────────────────────────────────────────────────
JOBS_FILE = DATA_DIR / "jobs.json"

def _load_jobs():
    if JOBS_FILE.exists():
        try:
            with open(JOBS_FILE) as f:
                loaded = json.load(f)
            cutoff = time.time() - 86400
            return {k: v for k, v in loaded.items() if v.get("_timestamp", 0) > cutoff}
        except Exception:
            return {}
    return {}

def _save_jobs():
    try:
        with open(JOBS_FILE, "w") as f:
            json.dump(jobs, f, default=str)
    except Exception:
        pass

jobs = _load_jobs()
# Mark interrupted jobs from previous session
for _jid, _jdata in jobs.items():
    if _jdata.get("status") == "running":
        _jdata["status"] = "interrupted"
        _jdata["message"] = "Server restarted - job was interrupted. Re-deploy to continue."
_save_jobs()
pipeline_stages = {}  # Track per-video pipeline stage

# ─── SQLite Database — Agency System ──────────────────────────────────────────
VIDEO_DB_PATH = str(DATA_DIR / "videos.db")


def get_db():
    """Get thread-safe SQLite connection."""
    conn = sqlite3.connect(VIDEO_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def query_db(query, args=(), one=False):
    """Execute query and return results as dicts."""
    conn = get_db()
    try:
        rows = conn.execute(query, args).fetchall()
        conn.commit()
        return (dict(rows[0]) if rows else None) if one else [dict(r) for r in rows]
    finally:
        conn.close()


def _init_db():
    conn = sqlite3.connect(VIDEO_DB_PATH)

    # ─── Creatives table (AI copywriter bots) ───
    conn.execute("""CREATE TABLE IF NOT EXISTS creatives (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        specialty TEXT,
        avatar_color TEXT DEFAULT '#00e5ff',
        writing_config TEXT,
        visual_config TEXT,
        voice_config TEXT,
        pacing_config TEXT,
        color_grade TEXT,
        status TEXT DEFAULT 'active',
        cloned_from TEXT,
        created_at TEXT,
        updated_at TEXT
    )""")

    # ─── Apps table (products being promoted) ───
    conn.execute("""CREATE TABLE IF NOT EXISTS apps (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        description TEXT,
        tiktok_handle TEXT,
        tiktok_account_id TEXT,
        app_store_url TEXT,
        play_store_url TEXT,
        link_in_bio_url TEXT,
        ica TEXT,
        content_pillars TEXT,
        cta_variations TEXT,
        hashtags TEXT,
        personas TEXT,
        color_dot TEXT DEFAULT '#a855f7',
        videos_per_day INTEGER DEFAULT 1,
        image_engine TEXT DEFAULT 'flux_2_pro',
        voice_engine TEXT DEFAULT 'elevenlabs',
        status TEXT DEFAULT 'active',
        created_at TEXT,
        updated_at TEXT
    )""")

    # ─── Videos table (upgrade existing) ───
    conn.execute("""CREATE TABLE IF NOT EXISTS videos (
        id TEXT PRIMARY KEY,
        app_slug TEXT,
        title TEXT,
        persona TEXT,
        file_path TEXT,
        status TEXT DEFAULT 'ready',
        qa_score REAL,
        duration_seconds REAL,
        slide_count INTEGER,
        cost_total REAL DEFAULT 0,
        file_size_bytes INTEGER DEFAULT 0,
        created_at TEXT,
        published_at TEXT,
        tiktok_account TEXT,
        views INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0,
        creative_id TEXT,
        app_id TEXT,
        thumbnail_path TEXT,
        script_json TEXT,
        qa_details TEXT,
        cost_breakdown TEXT,
        upload_status TEXT DEFAULT 'pending',
        tiktok_post_id TEXT,
        error_log TEXT
    )""")

    # Add playbook column to creatives
    _add_column(conn, "creatives", "playbook", "TEXT DEFAULT ''")

    # Add user_feedback column to videos (approve/reject + notes)
    _add_column(conn, "videos", "user_feedback", "TEXT")
    _add_column(conn, "videos", "user_approved", "INTEGER")
    _add_column(conn, "videos", "post_type", "TEXT DEFAULT 'link_in_bio'")

    # Add new columns to existing videos table if they don't exist
    _add_column(conn, "videos", "creative_id", "TEXT")
    _add_column(conn, "videos", "app_id", "TEXT")
    _add_column(conn, "videos", "thumbnail_path", "TEXT")
    _add_column(conn, "videos", "script_json", "TEXT")
    _add_column(conn, "videos", "qa_details", "TEXT")
    _add_column(conn, "videos", "cost_breakdown", "TEXT")
    _add_column(conn, "videos", "upload_status", "TEXT DEFAULT 'pending'")
    _add_column(conn, "videos", "tiktok_post_id", "TEXT")
    _add_column(conn, "videos", "error_log", "TEXT")

    # ─── Uploaded media (screenshots, footage) ───
    conn.execute("""CREATE TABLE IF NOT EXISTS uploaded_media (
        id TEXT PRIMARY KEY,
        app_id TEXT NOT NULL,
        file_path TEXT NOT NULL,
        media_type TEXT NOT NULL,
        original_filename TEXT,
        description TEXT,
        file_size_bytes INTEGER,
        created_at TEXT
    )""")

    # ─── Generation logs ───
    # ─── Jobs table (persistent deploy state) ───
    conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
        job_id TEXT PRIMARY KEY,
        app_id TEXT,
        creative_id TEXT,
        video_count INTEGER,
        status TEXT DEFAULT 'running',
        message TEXT,
        videos_completed INTEGER DEFAULT 0,
        pipeline_state TEXT,
        agent_state TEXT,
        activity_log TEXT,
        cost_breakdown TEXT,
        brief TEXT,
        started_at TEXT,
        completed_at TEXT,
        error_message TEXT
    )""")

    conn.execute("""CREATE TABLE IF NOT EXISTS generation_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        app_id TEXT,
        creative_id TEXT,
        video_count INTEGER,
        status TEXT,
        cost_total REAL DEFAULT 0,
        cost_breakdown TEXT,
        started_at TEXT,
        completed_at TEXT,
        error_message TEXT
    )""")

    conn.commit()
    conn.close()

    # Auto-migrate templates → creatives + config → apps
    _migrate_data()

    # Print table counts
    conn2 = sqlite3.connect(VIDEO_DB_PATH)
    counts = {}
    for tbl in ["creatives", "apps", "videos", "uploaded_media", "generation_logs"]:
        counts[tbl] = conn2.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    conn2.close()
    logger.info(f"DB tables: {counts}")


def _add_column(conn, table, column, col_type):
    """Safely add a column if it doesn't exist."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
    except sqlite3.OperationalError:
        pass  # Column already exists


def _migrate_data():
    """Migrate templates → creatives and config → apps if tables are empty."""
    from datetime import datetime
    conn = sqlite3.connect(VIDEO_DB_PATH)
    now = datetime.now().isoformat()

    # ─── Migrate apps from config/*.json ───
    app_count = conn.execute("SELECT COUNT(*) FROM apps").fetchone()[0]
    if app_count == 0:
        for cfg_file in sorted(CONFIG_DIR.glob("*.json")):
            if cfg_file.name == "example_app.json":
                continue
            try:
                with open(cfg_file) as f:
                    cfg = json.load(f)
                app_id = cfg_file.stem
                conn.execute(
                    "INSERT OR IGNORE INTO apps (id, name, description, tiktok_handle, app_store_url, play_store_url, link_in_bio_url, ica, content_pillars, cta_variations, hashtags, personas, image_engine, voice_engine, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (app_id, cfg.get("app_name", app_id), cfg.get("app_description", ""),
                     cfg.get("tiktok_handle", ""), cfg.get("app_store_url", ""), cfg.get("play_store_url", ""),
                     cfg.get("link_in_bio_url", ""),
                     json.dumps(cfg.get("ica", {})), json.dumps(cfg.get("content_pillars", [])),
                     json.dumps(cfg.get("cta_variations", [])), json.dumps(cfg.get("hashtag_sets", {})),
                     json.dumps(cfg.get("personas", [])),
                     cfg.get("image_engine", "flux_2_pro"), cfg.get("voice_engine", "elevenlabs"), now, now)
                )
                logger.info(f"Migrated app: {app_id}")
            except Exception as e:
                logger.error(f"Failed to migrate app {cfg_file.name}: {e}")

    # ─── Migrate templates → creatives ───
    creative_count = conn.execute("SELECT COUNT(*) FROM creatives").fetchone()[0]
    tpl_dir = DATA_DIR / "templates"
    if creative_count == 0 and tpl_dir.exists():
        for tpl_file in sorted(tpl_dir.glob("*.json")):
            try:
                with open(tpl_file) as f:
                    tpl = json.load(f)
                cid = tpl.get("id", tpl_file.stem)
                name = tpl.get("name", cid)
                persona = tpl.get("persona", {})
                sc = tpl.get("script_config", {})
                vc = tpl.get("voice_config", {})
                ic = tpl.get("image_config", {})
                cc = tpl.get("caption_config", {})
                vv = tpl.get("video_config", {})
                qa = tpl.get("qa_config", {})

                writing_config = json.dumps({
                    "identity": sc.get("writer_identity", ""),
                    "psychology": sc.get("psychology", ""),
                    "methodology": sc.get("methodology", ""),
                    "structure": tpl.get("structure_prompt", ""),
                    "tone": sc.get("tone", ""),
                    "vocabulary": sc.get("vocabulary", ""),
                    "banned_words": sc.get("banned_words", ""),
                    "hook_bank": sc.get("hook_bank", []),
                    "examples": sc.get("examples", ""),
                    "content_angle": sc.get("content_angle", ""),
                    "model": sc.get("model", "claude-sonnet-4-20250514"),
                    "energy": tpl.get("energy", "medium"),
                    "slide_count": tpl.get("slide_count", 3),
                    "persona_name": persona.get("name", ""),
                    "persona_archetype": persona.get("archetype", ""),
                    "persona_description": persona.get("description", ""),
                    "persona_image_prefix": persona.get("image_prompt_prefix", ""),
                })

                visual_config = json.dumps({
                    "style_suffix": ic.get("style_suffix", ""),
                    "avoid_in_images": ic.get("negative_prompt", ""),
                    "use_screenshots_for_demos": ic.get("use_screenshots_for_demos", True),
                })

                voice_config_json = json.dumps({
                    "elevenlabs_voice_id": vc.get("elevenlabs_voice_id", ""),
                    "voice_name": "",
                    "speaking_speed": vc.get("speaking_speed", 1.0),
                    "stability": vc.get("stability", 0.3),
                    "style": vc.get("style", 0.45),
                    "similarity": vc.get("similarity", 0.8),
                })

                pacing_config = json.dumps({
                    "ken_burns_zoom": vv.get("ken_burns_zoom", 1.05),
                    "ken_burns_pan": vv.get("ken_burns_pan", 15),
                    "crossfade_duration": vv.get("crossfade_duration", 0.3),
                    "music_volume": vv.get("music_volume", 0.12),
                    "subtitle_font_size": cc.get("subtitle_font_size", 56),
                    "highlight_color": cc.get("highlight_color", "#FFD700"),
                    "qa_threshold": qa.get("threshold", 7.0),
                    "auto_regenerate": qa.get("auto_regenerate", False),
                    "max_retries": qa.get("max_attempts", 3),
                })

                color_grade = json.dumps({
                    "warmth": vv.get("warmth", 1.08),
                    "contrast": vv.get("contrast", 1.05),
                    "saturation": vv.get("saturation", 0.95),
                    "vignette_strength": 0.15,
                })

                conn.execute(
                    "INSERT OR IGNORE INTO creatives (id, name, specialty, writing_config, visual_config, voice_config, pacing_config, color_grade, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (cid, name, persona.get("archetype", ""), writing_config, visual_config,
                     voice_config_json, pacing_config, color_grade, now, now)
                )
                logger.info(f"Migrated creative: {cid} ({name})")
            except Exception as e:
                logger.error(f"Failed to migrate template {tpl_file.name}: {e}")

    conn.commit()
    conn.close()


_init_db()


def _backfill_apps():
    """Ensure all config JSON files have corresponding database rows."""
    conn = sqlite3.connect(VIDEO_DB_PATH)
    existing_ids = set(row[0] for row in conn.execute("SELECT id FROM apps").fetchall())
    added = 0
    for config_file in CONFIG_DIR.glob("*.json"):
        slug = config_file.stem
        if slug in existing_ids or slug == "example_app":
            continue
        try:
            config = json.loads(config_file.read_text())
            conn.execute("""
                INSERT OR IGNORE INTO apps (id, name, description, tiktok_handle, status, created_at)
                VALUES (?, ?, ?, ?, 'active', ?)
            """, (
                slug,
                config.get("app_name", slug),
                config.get("app_description", ""),
                config.get("tiktok_handle", ""),
                datetime.now().isoformat()
            ))
            added += 1
        except Exception as e:
            logger.error(f"Backfill skip {config_file}: {e}")
    conn.commit()
    conn.close()
    if added:
        logger.info(f"App backfill: added {added} apps")


def _backfill_videos():
    """Scan output directories for videos not yet in the database."""
    conn = sqlite3.connect(VIDEO_DB_PATH)
    existing_ids = set(row[0] for row in conn.execute("SELECT id FROM videos").fetchall())
    added = 0
    if not OUTPUT_DIR.exists():
        conn.close()
        return
    for app_dir in OUTPUT_DIR.iterdir():
        if not app_dir.is_dir():
            continue
        app_slug = app_dir.name
        for mp4 in app_dir.rglob("*.mp4"):
            video_id = mp4.stem
            if video_id in existing_ids:
                continue
            # Skip stock clips, temp files, and processed fragments
            if video_id.startswith("pixabay_") or "stock_clips" in str(mp4) or "_processed" in video_id or "_compressed" in video_id:
                continue
            try:
                file_size = mp4.stat().st_size
                created = datetime.fromtimestamp(mp4.stat().st_mtime).isoformat()
                video_work_dir = mp4.parent.parent  # date dir
                # Video filename: app_000_ts.mp4 — extract index (000)
                title = video_id
                persona = ""
                parts = video_id.split("_")
                script_idx = None
                for p in parts:
                    if p.isdigit() and len(p) == 3:
                        script_idx = p
                        break
                # Try to find QA score from the video's own qa_review.json
                qa_score = 0
                if script_idx:
                    specific_qa = video_work_dir / f"video_{script_idx}" / "qa_review.json"
                    if specific_qa.exists():
                        try:
                            qa = json.loads(specific_qa.read_text())
                            qa_score = qa.get("overall_score", 0)
                        except Exception:
                            pass
                if script_idx:
                    scripts_dir = video_work_dir / "scripts"
                    if scripts_dir.exists():
                        for sf in scripts_dir.glob(f"script_{script_idx}_*.json"):
                            try:
                                sd = json.loads(sf.read_text())
                                title = sd.get("title", video_id)
                                persona = sd.get("persona", {}).get("name", "") if isinstance(sd.get("persona"), dict) else sd.get("persona_id", "")
                                break
                            except Exception:
                                pass
                # Check for thumbnail
                thumb = str(mp4).replace(".mp4", "_thumb.jpg")
                thumb_path = thumb if os.path.exists(thumb) else ""
                conn.execute("""
                    INSERT OR IGNORE INTO videos (id, app_slug, app_id, title, persona, file_path, status,
                    qa_score, slide_count, file_size_bytes, created_at, thumbnail_path)
                    VALUES (?, ?, ?, ?, ?, ?, 'ready', ?, 0, ?, ?, ?)
                """, (video_id, app_slug, app_slug, title, persona, str(mp4), qa_score, file_size, created, thumb_path))
                added += 1
            except Exception as e:
                logger.error(f"Backfill skip {mp4}: {e}")
    conn.commit()
    conn.close()
    if added:
        logger.info(f"Video backfill: added {added} videos")


_backfill_apps()
_backfill_videos()


# ─── Database Helper Functions ────────────────────────────────────────────────

def get_creative(creative_id):
    """Get creative by id, parse JSON blobs."""
    row = query_db("SELECT * FROM creatives WHERE id=?", (creative_id,), one=True)
    if not row:
        return None
    for key in ["writing_config", "visual_config", "voice_config", "pacing_config", "color_grade"]:
        if row.get(key):
            try:
                row[key] = json.loads(row[key])
            except (json.JSONDecodeError, TypeError):
                row[key] = {}
    return row


def update_agent_identity(creative_id: str, feedback: str, source: str = "user") -> str | None:
    """Merge feedback into the agent's identity prompt using Claude.

    Args:
        creative_id: The creative/agent ID
        feedback: What went wrong or what to improve
        source: "user" (from vault review/script reject) or "qa" (from AI QA failure)

    Returns:
        The new identity prompt, or None on failure
    """
    creative = get_creative(creative_id)
    if not creative:
        return None
    wc = creative.get("writing_config", {}) or {}
    current_identity = wc.get("identity", "")

    try:
        import anthropic
        client = anthropic.Anthropic()
        source_label = "The USER" if source == "user" else "The AI quality reviewer"
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": f"""You maintain the writing style prompt for a TikTok script-writing AI agent.

CURRENT PROMPT:
{current_identity or "(empty — no prompt set yet)"}

{source_label} gave this feedback on a recent video:
"{feedback}"

Update the prompt to address this feedback. Rules:
- Keep the core voice and personality intact
- Add specific guidance to prevent the issue from recurring
- If the feedback is about copy quality, hooks, or CTAs — weave it naturally into the prompt
- If the feedback contradicts something in the current prompt, the feedback wins
- Keep the prompt concise (under 500 words) — don't let it bloat
- Return ONLY the updated prompt text, nothing else"""
            }]
        )
        new_identity = response.content[0].text.strip()
        # Save back to DB
        wc["identity"] = new_identity
        conn = get_db()
        conn.execute("UPDATE creatives SET writing_config=?, updated_at=? WHERE id=?",
                     (json.dumps(wc), time.strftime("%Y-%m-%dT%H:%M:%S"), creative_id))
        conn.commit()
        conn.close()
        logger.info(f"Agent identity updated ({source}): {new_identity[:80]}...")
        return new_identity
    except Exception as e:
        logger.error(f"Failed to update agent identity: {e}")
        return None


def get_all_creatives(status="active"):
    """Get all creatives with parsed JSON and stats."""
    rows = query_db("SELECT * FROM creatives WHERE status=? ORDER BY name", (status,))
    for row in rows:
        for key in ["writing_config", "visual_config", "voice_config", "pacing_config", "color_grade"]:
            if row.get(key):
                try:
                    row[key] = json.loads(row[key])
                except (json.JSONDecodeError, TypeError):
                    row[key] = {}
        # Add stats from videos table (split by pass/fail)
        stats = query_db(
            """SELECT COUNT(*) as total_videos,
                      SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) as passed_videos,
                      SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed_videos,
                      COALESCE(AVG(qa_score),0) as avg_qa,
                      COALESCE(SUM(cost_total),0) as total_spent
               FROM videos WHERE creative_id=?""",
            (row["id"],), one=True
        ) or {}
        row["video_count"] = stats.get("passed_videos", 0) or 0
        row["failed_count"] = stats.get("failed_videos", 0) or 0
        row["total_attempts"] = stats.get("total_videos", 0) or 0
        row["avg_qa"] = round(stats.get("avg_qa", 0), 1)
        row["total_spent"] = round(stats.get("total_spent", 0), 2)
    return rows


def save_creative(creative_data):
    """Insert or update creative, serialize JSON blobs."""
    from datetime import datetime
    cid = creative_data.get("id")
    if not cid:
        return
    now = datetime.now().isoformat()
    data = dict(creative_data)
    for key in ["writing_config", "visual_config", "voice_config", "pacing_config", "color_grade"]:
        if key in data and isinstance(data[key], dict):
            data[key] = json.dumps(data[key])
    conn = get_db()
    conn.execute("""INSERT OR REPLACE INTO creatives
        (id, name, specialty, avatar_color, writing_config, visual_config, voice_config, pacing_config, color_grade, playbook, status, cloned_from, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM creatives WHERE id=?), ?),?)""",
        (cid, data.get("name", ""), data.get("specialty", ""), data.get("avatar_color", "#00e5ff"),
         data.get("writing_config", "{}"), data.get("visual_config", "{}"), data.get("voice_config", "{}"),
         data.get("pacing_config", "{}"), data.get("color_grade", "{}"),
         data.get("playbook", ""),
         data.get("status", "active"), data.get("cloned_from"),
         cid, now, now))
    conn.commit()
    conn.close()


def get_app_db(app_id):
    """Get app by id, parse JSON blobs."""
    row = query_db("SELECT * FROM apps WHERE id=?", (app_id,), one=True)
    if not row:
        return None
    for key in ["ica", "content_pillars", "cta_variations", "hashtags", "personas"]:
        if row.get(key):
            try:
                row[key] = json.loads(row[key])
            except (json.JSONDecodeError, TypeError):
                row[key] = {} if key in ("ica", "hashtags") else []
    # Add video count
    stats = query_db("SELECT COUNT(*) as cnt FROM videos WHERE app_slug=?", (app_id,), one=True) or {}
    row["video_count"] = stats.get("cnt", 0)
    return row


def get_all_apps_db(status="active"):
    """Get all apps with parsed JSON and video counts."""
    rows = query_db("SELECT * FROM apps WHERE status=? ORDER BY name", (status,))
    for row in rows:
        for key in ["ica", "content_pillars", "cta_variations", "hashtags", "personas"]:
            if row.get(key):
                try:
                    row[key] = json.loads(row[key])
                except (json.JSONDecodeError, TypeError):
                    row[key] = {} if key in ("ica", "hashtags") else []
        stats = query_db("SELECT COUNT(*) as cnt FROM videos WHERE app_slug=?", (row["id"],), one=True) or {}
        row["video_count"] = stats.get("cnt", 0)
    return rows


def save_app_db(app_data):
    """Insert or update app, serialize JSON blobs."""
    from datetime import datetime
    aid = app_data.get("id")
    if not aid:
        return
    now = datetime.now().isoformat()
    data = dict(app_data)
    for key in ["ica", "content_pillars", "cta_variations", "hashtags", "personas"]:
        if key in data and isinstance(data[key], (dict, list)):
            data[key] = json.dumps(data[key])
    conn = get_db()
    conn.execute("""INSERT OR REPLACE INTO apps
        (id, name, description, tiktok_handle, tiktok_account_id, app_store_url, play_store_url, link_in_bio_url,
         ica, content_pillars, cta_variations, hashtags, personas, color_dot, videos_per_day, image_engine, voice_engine,
         status, created_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,COALESCE((SELECT created_at FROM apps WHERE id=?), ?),?)""",
        (aid, data.get("name", ""), data.get("description", ""), data.get("tiktok_handle", ""),
         data.get("tiktok_account_id"), data.get("app_store_url", ""), data.get("play_store_url", ""),
         data.get("link_in_bio_url", ""),
         data.get("ica", "{}"), data.get("content_pillars", "[]"), data.get("cta_variations", "[]"),
         data.get("hashtags", "{}"), data.get("personas", "[]"),
         data.get("color_dot", "#a855f7"), data.get("videos_per_day", 1),
         data.get("image_engine", "flux_2_pro"), data.get("voice_engine", "elevenlabs"),
         data.get("status", "active"), aid, now, now))
    conn.commit()
    conn.close()


def get_dashboard_stats():
    """Return real-time dashboard stats."""
    from datetime import datetime, timedelta
    now = datetime.now()
    week_ago = (now - timedelta(days=7)).isoformat()
    two_weeks_ago = (now - timedelta(days=14)).isoformat()
    today = now.strftime("%Y-%m-%d")
    month_start = now.strftime("%Y-%m-01")

    conn = get_db()
    try:
        vw = conn.execute("SELECT COUNT(*) FROM videos WHERE status='ready' AND created_at > ?", (week_ago,)).fetchone()[0]
        vlw = conn.execute("SELECT COUNT(*) FROM videos WHERE status='ready' AND created_at > ? AND created_at <= ?", (two_weeks_ago, week_ago)).fetchone()[0]
        # Use generation_logs for cost (doesn't change when videos are deleted)
        ct = conn.execute("SELECT COALESCE(SUM(cost_total),0) FROM generation_logs WHERE started_at LIKE ?", (today + "%",)).fetchone()[0]
        cm = conn.execute("SELECT COALESCE(SUM(cost_total),0) FROM generation_logs WHERE started_at >= ?", (month_start,)).fetchone()[0]
        qa_w = conn.execute("SELECT COALESCE(AVG(qa_score),0) FROM videos WHERE created_at > ? AND qa_score > 0", (week_ago,)).fetchone()[0]
        qa_lw = conn.execute("SELECT COALESCE(AVG(qa_score),0) FROM videos WHERE created_at > ? AND created_at <= ? AND qa_score > 0", (two_weeks_ago, week_ago)).fetchone()[0]
        err_today = conn.execute("SELECT COUNT(*) FROM generation_logs WHERE status='error' AND started_at LIKE ?", (today + "%",)).fetchone()[0]
        last_err = conn.execute("SELECT error_message, started_at FROM generation_logs WHERE status='error' ORDER BY started_at DESC LIMIT 1").fetchone()
        # QA stats by post type
        pt_rows = conn.execute("""
            SELECT COALESCE(post_type,'link_in_bio') as pt, COUNT(*) as cnt,
                   COALESCE(AVG(qa_score),0) as avg_qa,
                   SUM(CASE WHEN status='ready' THEN 1 ELSE 0 END) as passed,
                   SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) as failed
            FROM videos WHERE qa_score > 0 GROUP BY pt
        """).fetchall()
    finally:
        conn.close()

    qa_by_type = {}
    for row in pt_rows:
        qa_by_type[row[0]] = {"count": row[1], "avg_qa": round(row[2], 1), "passed": row[3], "failed": row[4]}

    return {
        "videos_this_week": vw,
        "videos_last_week": vlw,
        "credits_today": round(ct, 2),
        "credits_this_month": round(cm, 2),
        "avg_qa_score": round(qa_w, 1),
        "avg_qa_last_week": round(qa_lw, 1),
        "error_count_today": err_today,
        "last_error": {"message": last_err[0], "timestamp": last_err[1]} if last_err else None,
        "qa_by_post_type": qa_by_type,
    }


def get_vault_videos(app_id=None, creative_id=None, min_qa=None, search=None, status=None, post_type=None, limit=50, offset=0):
    """Filtered video query for vault screen."""
    query = "SELECT * FROM videos WHERE 1=1"
    params = []
    if status:
        query += " AND status=?"
        params.append(status)
    if post_type:
        query += " AND post_type=?"
        params.append(post_type)
    if app_id:
        query += " AND (app_slug=? OR app_id=?)"
        params.extend([app_id, app_id])
    if creative_id:
        query += " AND creative_id=?"
        params.append(creative_id)
    if min_qa is not None:
        query += " AND qa_score >= ?"
        params.append(min_qa)
    if search:
        query += " AND (title LIKE ? OR persona LIKE ?)"
        params.extend([f"%{search}%", f"%{search}%"])
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return query_db(query, params)


def get_billing_data():
    """Return monthly cost breakdowns."""
    conn = get_db()
    try:
        rows = conn.execute("""
            SELECT strftime('%Y-%m', created_at) as month,
                   COUNT(*) as video_count,
                   COALESCE(SUM(cost_total), 0) as total_cost,
                   COALESCE(AVG(qa_score), 0) as avg_qa
            FROM videos
            WHERE created_at IS NOT NULL
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
        """).fetchall()
    finally:
        conn.close()
    return [{"month": r[0], "video_count": r[1], "total_cost": round(r[2], 2), "avg_qa": round(r[3], 1)} for r in rows]


# ─── Job State (DB-backed) ────────────────────────────────────────────────────

def save_job_to_db(job_id, data):
    """Save full job state to database."""
    conn = get_db()
    conn.execute("""INSERT OR REPLACE INTO jobs
        (job_id, app_id, creative_id, video_count, status, message, videos_completed,
         pipeline_state, agent_state, activity_log, cost_breakdown, brief, started_at, completed_at, error_message)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (job_id, data.get("app_id", ""), data.get("creative_id", ""), data.get("video_count", data.get("total_count", 0)),
         data.get("status", "running"), data.get("message", ""),
         data.get("videos_created", data.get("videos_completed", 0)),
         json.dumps(data.get("pipeline_stages", {})),
         json.dumps(data.get("agents", {})),
         json.dumps(data.get("activity_log", [])),
         json.dumps(data.get("cost", {})),
         data.get("brief", ""),
         data.get("started_at", time.strftime("%Y-%m-%dT%H:%M:%S")),
         data.get("completed_at"),
         data.get("error_message")))
    conn.commit()
    conn.close()


def load_job_from_db(job_id):
    """Load job state from database, parse JSON blobs."""
    row = query_db("SELECT * FROM jobs WHERE job_id=?", (job_id,), one=True)
    if not row:
        return None
    for key in ["pipeline_state", "agent_state", "activity_log", "cost_breakdown"]:
        if row.get(key):
            try:
                row[key] = json.loads(row[key])
            except (json.JSONDecodeError, TypeError):
                row[key] = {} if key in ("pipeline_state", "agent_state", "cost_breakdown") else []
    # Map DB columns to expected frontend keys
    row["pipeline_stages"] = row.pop("pipeline_state", {})
    row["agents"] = row.pop("agent_state", {})
    row["cost"] = row.pop("cost_breakdown", {})
    row["total_count"] = row.get("video_count", 0)
    row["total"] = row.get("video_count", 0)
    row["videos_created"] = row.get("videos_completed", 0)
    return row


def get_active_jobs():
    """Get all running jobs."""
    rows = query_db("SELECT job_id FROM jobs WHERE status='running'")
    return [load_job_from_db(r["job_id"]) for r in rows if r]


def get_job_history(limit=20):
    """Get recent completed/failed jobs."""
    return query_db("SELECT job_id, app_id, creative_id, video_count, status, videos_completed, cost_breakdown, started_at, completed_at, error_message FROM jobs WHERE status IN ('done','error') ORDER BY started_at DESC LIMIT ?", (limit,))


def update_job_field(job_id, **kwargs):
    """Update specific fields on a job."""
    if job_id not in jobs:
        return
    # Update in-memory
    for k, v in kwargs.items():
        jobs[job_id][k] = v
    # Persist to DB
    try:
        save_job_to_db(job_id, jobs[job_id])
    except Exception:
        pass


# Legacy compatibility wrappers
def _db_insert_video(video_data):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO videos
        (id, app_slug, app_id, title, persona, creative_id, file_path, status, qa_score,
         duration_seconds, slide_count, cost_total, cost_breakdown, file_size_bytes,
         created_at, thumbnail_path, script_json, qa_details, upload_status, post_type,
         video_engine, voice_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (video_data.get("id"), video_data.get("app_slug"), video_data.get("app_slug"),
         video_data.get("title"), video_data.get("persona"), video_data.get("creative_id", ""),
         video_data.get("file_path"), video_data.get("status", "ready"), video_data.get("qa_score"),
         video_data.get("duration_seconds"), video_data.get("slide_count"), video_data.get("cost_total"),
         video_data.get("cost_breakdown", "{}"), video_data.get("file_size_bytes"),
         video_data.get("created_at"), video_data.get("thumbnail_path", ""),
         video_data.get("script_json", ""), video_data.get("qa_details", "{}"),
         video_data.get("upload_status", "pending"), video_data.get("post_type", "link_in_bio"),
         video_data.get("video_engine", "standard"), video_data.get("voice_id", ""))
    )
    conn.commit()
    conn.close()

def _db_get_videos(app_slug=None, limit=50):
    return get_vault_videos(app_id=app_slug, limit=limit)

def _db_delete_video(video_id):
    conn = get_db()
    conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
    conn.commit()
    conn.close()

def _db_get_stats(app_slug=None):
    conn = get_db()
    if app_slug:
        row = conn.execute("SELECT COUNT(*) as count, COALESCE(SUM(cost_total),0) as total_cost, COALESCE(AVG(qa_score),0) as avg_qa, COALESCE(SUM(views),0) as total_views FROM videos WHERE app_slug=?", (app_slug,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) as count, COALESCE(SUM(cost_total),0) as total_cost, COALESCE(AVG(qa_score),0) as avg_qa, COALESCE(SUM(views),0) as total_views FROM videos").fetchone()
    conn.close()
    return {"count": row[0], "total_cost": round(row[1], 2), "avg_qa": round(row[2], 1), "total_views": row[3]}


def _load_upload_queue():
    if UPLOAD_QUEUE_PATH.exists():
        try:
            with open(UPLOAD_QUEUE_PATH) as f:
                return json.load(f)
        except Exception as e:
            logger.debug(f"Could not load upload queue: {e}")
            return []
    return []

def _save_upload_queue(queue):
    UPLOAD_QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(UPLOAD_QUEUE_PATH, "w") as f:
        json.dump(queue, f, indent=2)

def _get_posts_today(app_slug):
    queue = _load_upload_queue()
    from datetime import datetime, timedelta
    today = datetime.now().strftime("%Y-%m-%d")
    return sum(1 for e in queue if e.get("app_slug") == app_slug and e.get("status") == "uploaded" and e.get("uploaded_at", "").startswith(today))

def _get_last_post_time(app_slug):
    queue = _load_upload_queue()
    uploaded = [e for e in queue if e.get("app_slug") == app_slug and e.get("status") == "uploaded"]
    if not uploaded:
        return None
    uploaded.sort(key=lambda e: e.get("uploaded_at", ""), reverse=True)
    return uploaded[0].get("uploaded_at")

def _calculate_next_upload_time(app_slug):
    from datetime import datetime, timedelta
    import random as _rand
    now = datetime.now()
    posts_today = _get_posts_today(app_slug)

    if posts_today >= MAX_POSTS_PER_ACCOUNT_PER_DAY:
        # Schedule for tomorrow's first peak window
        tomorrow = now + timedelta(days=1)
        return tomorrow.replace(hour=PEAK_HOURS[0][0], minute=_rand.randint(0, 30), second=0).isoformat()

    # Find next peak hour window
    for start_h, end_h in PEAK_HOURS:
        if now.hour < end_h:
            target_hour = max(now.hour + MIN_HOURS_BETWEEN_POSTS, start_h)
            if target_hour < end_h:
                jitter = _rand.randint(-15, 15)
                target = now.replace(hour=target_hour, minute=max(0, min(59, _rand.randint(0, 59) + jitter)), second=0)
                if target > now:
                    return target.isoformat()

    # No peak window left today — schedule tomorrow
    tomorrow = now + timedelta(days=1)
    first_peak = PEAK_HOURS[0]
    return tomorrow.replace(hour=first_peak[0], minute=_rand.randint(0, 30), second=0).isoformat()

def _add_to_upload_queue(app_slug, video_path, title="", description="", hashtags=None):
    from datetime import datetime
    import uuid
    queue = _load_upload_queue()
    entry = {
        "id": str(uuid.uuid4())[:8],
        "app_slug": app_slug,
        "video_path": video_path,
        "title": title,
        "description": (description or "") + AIGC_DESCRIPTION_SUFFIX,
        "hashtags": hashtags or [],
        "scheduled_time": _calculate_next_upload_time(app_slug),
        "status": "queued",
        "attempts": 0,
        "last_error": None,
        "created_at": datetime.now().isoformat(),
        "uploaded_at": None,
    }
    queue.append(entry)
    _save_upload_queue(queue)
    return entry


def _process_upload_queue():
    """Background task: upload queued videos whose scheduled time has passed."""
    import requests as _req
    from datetime import datetime
    while True:
        try:
            time.sleep(UPLOAD_QUEUE_CHECK_INTERVAL_MINUTES * 60)
            queue = _load_upload_queue()
            now = datetime.now().isoformat()
            api_key = os.environ.get("UPLOADPOST_API_KEY")
            if not api_key:
                continue

            uploads_this_minute = 0
            for entry in queue:
                if entry["status"] != "queued":
                    continue
                if entry["scheduled_time"] > now:
                    continue
                if uploads_this_minute >= API_RATE_LIMIT_PER_MINUTE:
                    break
                if _get_posts_today(entry["app_slug"]) >= MAX_POSTS_PER_ACCOUNT_PER_DAY:
                    continue

                # Per-app token from config, fallback to global
                _cfg_path = CONFIG_DIR / f"{entry['app_slug']}.json"
                _per_app_key = None
                if _cfg_path.exists():
                    try:
                        with open(_cfg_path) as _cf:
                            _ac = json.load(_cf)
                        _per_app_key = (_ac.get("tiktok", {}).get("upload_post_token") or "").strip()
                    except Exception as e:
                        logger.debug(f"Could not load per-app token from {_cfg_path}: {e}")
                upload_key = _per_app_key or api_key

                # Upload
                try:
                    video_path = entry["video_path"]
                    if not os.path.exists(video_path):
                        entry["status"] = "failed"
                        entry["last_error"] = "File not found"
                        continue

                    with open(video_path, "rb") as f:
                        files = {"video": f}
                        data_fields = {
                            "description": entry.get("description", ""),
                            "ai_generated": "true",
                        }
                        resp = _req.post(
                            "https://app.upload-post.com/api/upload",
                            files=files,
                            data=data_fields,
                            headers={"Authorization": f"Bearer {upload_key}"},
                            timeout=300,
                        )

                    if resp.status_code in (200, 201):
                        entry["status"] = "uploaded"
                        entry["uploaded_at"] = datetime.now().isoformat()
                        uploads_this_minute += 1
                    elif resp.status_code == 429:
                        entry["attempts"] += 1
                        backoff_idx = min(entry["attempts"] - 1, len(RATE_LIMIT_BACKOFF_SECONDS) - 1)
                        entry["last_error"] = f"Rate limited (429). Backing off {RATE_LIMIT_BACKOFF_SECONDS[backoff_idx]}s"
                        if entry["attempts"] >= MAX_RETRY_ATTEMPTS:
                            entry["status"] = "failed"
                    else:
                        entry["attempts"] += 1
                        entry["last_error"] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                        if entry["attempts"] >= MAX_RETRY_ATTEMPTS:
                            entry["status"] = "failed"
                except Exception as e:
                    entry["attempts"] += 1
                    entry["last_error"] = str(e)[:200]
                    if entry["attempts"] >= MAX_RETRY_ATTEMPTS:
                        entry["status"] = "failed"

            _save_upload_queue(queue)
        except Exception:
            pass


# ─── DASHBOARD HTML ──────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>J.A.R.V.I.S. — Content Command</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@400;500;700;900&family=Inter:wght@300;400;500;600;700;800&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
html{font-size:clamp(14px,1.1vw,18px)}
:root{--bg:#08080d;--bg2:#0e0e15;--bg3:#13131d;--bg4:#1c1c2e;--cyan:#00e5ff;--cyan-d:rgba(0,229,255,.1);--cyan-g:rgba(0,229,255,.3);--text:#e4e4ec;--dim:#8888a0;--muted:#4a4a5e;--border:#1c1c2e;--green:#22c55e;--yellow:#eab308;--red:#ef4444;--hud:'Orbitron',monospace;--ui:'Inter',sans-serif}
body{background:var(--bg);color:var(--text);font-family:var(--ui);min-height:100vh}
body::before{content:'';position:fixed;inset:0;background:linear-gradient(rgba(0,229,255,.015) 1px,transparent 1px),linear-gradient(90deg,rgba(0,229,255,.015) 1px,transparent 1px);background-size:60px 60px;pointer-events:none;z-index:0}
::selection{background:var(--cyan-d);color:var(--cyan)}
::-webkit-scrollbar{width:5px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:rgba(0,229,255,.1);border-radius:3px}

/* Nav */
nav{position:fixed;top:0;left:0;right:0;height:56px;background:rgba(8,8,13,.95);backdrop-filter:blur(16px);border-bottom:1px solid var(--border);display:flex;align-items:center;padding:0 24px;z-index:100}
.logo{font-family:var(--hud);font-size:15px;font-weight:700;letter-spacing:3px;color:var(--cyan);margin-right:32px}
.nav-items{display:flex;gap:0}
.nav-items a{text-decoration:none;color:var(--muted);font-size:12px;font-weight:600;padding:18px 16px;border-bottom:2px solid transparent;cursor:pointer;transition:.15s}
.nav-items a:hover{color:var(--dim)}
.nav-items a.on{color:var(--cyan);border-bottom-color:var(--cyan)}
.nav-right{margin-left:auto;display:flex;align-items:center;gap:12px}
.avatar{width:32px;height:32px;border-radius:50%;background:var(--cyan-d);border:1px solid rgba(0,229,255,.2);display:flex;align-items:center;justify-content:center;font-family:var(--hud);font-size:11px;color:var(--cyan);font-weight:700}
.user-name{font-size:12px;color:var(--dim)}

/* Layout */
.page{padding:72px 24px 32px;max-width:1400px;margin:0 auto;position:relative;z-index:1;display:none}
.page.on{display:block}

/* Cards */
.card{background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:18px}
.card-title{font-family:var(--hud);font-size:10px;letter-spacing:1.5px;color:var(--muted);text-transform:uppercase;margin-bottom:12px}
.stat-val{font-size:28px;font-weight:800;color:var(--text)}
.stat-sub{font-size:11px;color:var(--dim);margin-top:4px}
.delta{display:inline-block;padding:2px 6px;border-radius:4px;font-size:10px;font-weight:600;margin-left:6px}
.delta.up{background:rgba(34,197,94,.1);color:var(--green)}
.delta.down{background:rgba(239,68,68,.1);color:var(--red)}
.delta.flat{background:rgba(136,136,160,.1);color:var(--dim)}

/* Buttons */
.btn{padding:8px 16px;border-radius:8px;border:none;font-size:12px;font-weight:600;cursor:pointer;transition:.15s}
.btn-primary{background:var(--cyan);color:var(--bg);box-shadow:0 0 16px var(--cyan-g)}
.btn-primary:hover{box-shadow:0 0 24px var(--cyan-g);transform:translateY(-1px)}
.btn-ghost{background:transparent;border:1px solid var(--border);color:var(--dim)}
.btn-ghost:hover{border-color:rgba(0,229,255,.2);color:var(--text)}
.btn-sm{padding:5px 10px;font-size:11px}

/* Forms */
.field{margin-bottom:14px}
.field-label{font-size:10px;font-weight:600;letter-spacing:1px;text-transform:uppercase;color:var(--muted);margin-bottom:6px}
.input{width:100%;padding:8px 12px;border-radius:8px;border:1px solid var(--border);background:var(--bg2);color:var(--text);font-size:13px;outline:none;font-family:var(--ui)}
.input:focus{border-color:var(--cyan)}
textarea.input{min-height:80px;resize:vertical}
select.input{cursor:pointer}
input[type=range]{-webkit-appearance:none;width:100%;height:3px;background:var(--bg4);border-radius:2px;outline:none;margin:6px 0}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--cyan);cursor:pointer;box-shadow:0 0 6px var(--cyan-g)}

/* Grid */
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
.grid-4{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
@media(max-width:1024px){.grid-4{grid-template-columns:1fr 1fr}.grid-3{grid-template-columns:1fr 1fr}}
@media(max-width:768px){.grid-4,.grid-3,.grid-2{grid-template-columns:1fr}}

/* Two-panel */
.panels{display:grid;grid-template-columns:280px 1fr;gap:0;min-height:calc(100vh - 100px)}
.panel-left{border-right:1px solid var(--border);padding-right:0;overflow-y:auto;max-height:calc(100vh - 80px)}
.panel-right{padding-left:20px;overflow-y:auto;max-height:calc(100vh - 80px)}

/* List items */
.list-item{padding:12px 16px;cursor:pointer;border-bottom:1px solid rgba(28,28,46,.5);transition:.1s}
.list-item:hover{background:rgba(0,229,255,.03)}
.list-item.on{background:rgba(0,229,255,.06);border-left:2px solid var(--cyan)}
.list-item-name{font-size:13px;font-weight:600}
.list-item-sub{font-size:11px;color:var(--dim);margin-top:2px}

/* Tabs */
.tabs{display:flex;gap:0;border-bottom:1px solid var(--border);margin-bottom:16px}
.tab-btn{padding:10px 16px;font-size:12px;color:var(--muted);cursor:pointer;border-bottom:2px solid transparent;font-weight:500;transition:.15s}
.tab-btn:hover{color:var(--dim)}
.tab-btn.on{color:var(--cyan);border-bottom-color:var(--cyan)}
.tab-content{display:none}.tab-content.on{display:block}

/* QA badge */
.qa{padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;font-family:var(--hud)}
.qa-pass{background:rgba(34,197,94,.12);color:var(--green)}
.qa-warn{background:rgba(234,179,8,.12);color:var(--yellow)}
.qa-fail{background:rgba(239,68,68,.12);color:var(--red)}

/* Chart */
.chart-bar{display:flex;align-items:flex-end;gap:8px;height:120px;padding:0 4px}
.chart-col{flex:1;display:flex;flex-direction:column;align-items:center;gap:4px}
.chart-col .bar{width:100%;border-radius:4px 4px 0 0;transition:height .3s;min-height:2px}
.chart-col .label{font-size:10px;color:var(--muted)}

/* Progress steps */
.steps{display:flex;gap:0;align-items:center}
.step{display:flex;align-items:center;gap:6px;padding:6px 10px;font-size:11px;color:var(--muted)}
.step.done{color:var(--green)}.step.active{color:var(--cyan)}.step.waiting{color:var(--muted)}
.step-arrow{color:var(--muted);font-size:10px}

/* Vault card */
.vault-card{background:var(--bg3);border:1px solid var(--border);border-radius:10px;overflow:hidden;cursor:pointer;transition:.15s}
.vault-card:hover{border-color:rgba(0,229,255,.2);transform:translateY(-2px)}
.vault-thumb{aspect-ratio:9/16;background:var(--bg2);position:relative;overflow:hidden}
.vault-thumb video{width:100%;height:100%;object-fit:cover}
.vault-info{padding:10px}
.vault-title{font-size:12px;font-weight:600;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.vault-meta{font-size:10px;color:var(--dim);margin-top:4px}

/* Toast */
.toast{position:fixed;top:70px;right:20px;padding:12px 18px;border-radius:8px;font-size:12px;z-index:200;animation:slideIn .2s ease}
.toast-success{background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.3);color:var(--green)}
.toast-error{background:rgba(239,68,68,.15);border:1px solid rgba(239,68,68,.3);color:var(--red)}
@keyframes slideIn{from{opacity:0;transform:translateX(20px)}to{opacity:1;transform:translateX(0)}}

/* Modal */
.modal-bg{position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:300;display:flex;align-items:center;justify-content:center}
.modal-box{background:var(--bg3);border:1px solid var(--border);border-radius:12px;padding:24px;width:90%;max-width:480px}
</style>
</head>
<body>

<nav>
  <div class="logo">J.A.R.V.I.S.</div>
  <div class="nav-items">
    <a class="on" onclick="navigate('dashboard',this)">Dashboard</a>
    <a onclick="navigate('creatives',this)">Agents</a>
    <a onclick="navigate('apps',this)">Apps</a>
    <a onclick="navigate('vault',this)">Vault</a>
    <a onclick="navigate('settings',this)">Settings</a>
  </div>
  <div class="nav-right">
    <div class="avatar">Q</div>
    <span class="user-name">Quinten</span>
  </div>
</nav>

<!-- ═══ DASHBOARD ═══ -->
<div class="page on" id="pg-dashboard">
  <div class="grid-4" id="stat-cards" style="margin-bottom:16px">
    <div class="card"><div class="card-title">Videos This Week</div><div class="stat-val" id="s-videos">...</div><div id="s-videos-delta"></div></div>
    <div class="card"><div class="card-title">Credits Used</div><div class="stat-val" id="s-credits">...</div><div class="stat-sub" id="s-credits-sub"></div></div>
    <div class="card"><div class="card-title">Avg QA Score</div><div class="stat-val" id="s-qa">...</div><div id="s-qa-delta"></div></div>
    <div class="card"><div class="card-title">Errors Today</div><div class="stat-val" id="s-errors">...</div><div class="stat-sub" id="s-errors-sub"></div></div>
  </div>

  <div class="grid-2" style="margin-bottom:16px">
    <!-- Deploy -->
    <div class="card">
      <div class="card-title">Deploy</div>
      <div class="field"><div class="field-label">App</div><select class="input" id="d-app"></select></div>
      <div class="field"><div class="field-label">Agent</div><select class="input" id="d-creative"></select></div>
      <div class="field"><div class="field-label">Length</div><select class="input" id="d-duration"><option value="short">Short (8-15s)</option><option value="medium" selected>Medium (15-25s)</option><option value="long">Long (25-40s)</option></select></div>
      <div class="field"><div class="field-label">Video Engine</div><select class="input" id="d-engine" onchange="onEngineChange()"><option value="standard">Standard (Screen Recordings)</option><option value="hybrid">Hybrid (Pixabay + App Recordings)</option></select></div>
      <div class="field" id="d-template-field" style="display:none"><div class="field-label">Remotion Template</div><select class="input" id="d-template"><option value="stock-narration">Stock Narration (default)</option><option value="text-slam">Text Slam B-Roll</option><option value="hormozi-style">Hormozi Style</option><option value="cinematic-ai">Cinematic AI</option><option value="chaos-energy">Chaos Energy</option><option value="pov-storytelling">POV Storytelling</option><option value="quick-tips">Quick Tips</option><option value="luxury-minimal">Luxury Minimal</option><option value="split-before-after">Split Before/After</option><option value="screen-demo">Screen Recording Demo</option><option value="trending-text">Trending Text Only</option></select></div>
      <div class="field" id="d-avatar-field" style="display:none"><div class="field-label">Avatar</div><select class="input" id="d-avatar"><option>Loading...</option></select></div>
      <div class="field-label" style="margin-bottom:6px">Content Mix</div>
      <div id="d-mix" style="margin-bottom:8px"></div>
      <div style="display:flex;gap:6px;margin-bottom:8px">
        <select class="input" id="d-add-type" style="flex:1;font-size:11px"><option value="link_in_bio">Link in Bio</option><option value="follower">Follower</option><option value="value">Value</option><option value="trust">Trust</option><option value="engagement">Engagement</option><option value="trending">Trending</option></select>
        <button class="btn btn-ghost btn-sm" onclick="addToMix()" style="font-size:11px">+ Add</button>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <span style="font-size:11px;color:var(--dim)" id="d-estimate">0 videos · Est. $0.00 · ~0 min</span>
        <button class="btn btn-ghost btn-sm" onclick="clearMix()" style="font-size:10px;color:var(--muted)">Clear</button>
      </div>
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px">
        <span style="font-size:11px;color:var(--dim)">QA Target</span>
        <input type="number" id="d-train-target" value="7.0" min="5" max="10" step="0.5" style="width:55px;padding:3px 6px;font-size:12px;background:var(--bg2);border:1px solid var(--border);border-radius:6px;color:var(--text);text-align:center">
        <span style="font-size:9px;color:var(--muted)">You approve copy, AI retries until this score</span>
      </div>
      <button class="btn btn-primary" style="width:100%;font-family:var(--hud);letter-spacing:1px" onclick="deploy()" id="d-btn">⚡ DEPLOY</button>
    </div>
    <!-- Progress -->
    <div class="card" id="progress-card">
      <div class="card-title">Production Status</div>
      <div id="prod-status" style="color:var(--muted);font-size:12px">No active production. Deploy to start.</div>
    </div>
  </div>

  <div class="grid-2" style="margin-bottom:16px">
    <div class="card"><div class="card-title">Recent Output</div><div id="recent-videos" style="font-size:12px;color:var(--muted)">Loading...</div></div>
    <div class="card"><div class="card-title">Recent Activity</div><div id="recent-activity" style="font-size:12px;color:var(--muted)">Loading...</div></div>
  </div>

  <div class="card">
    <div class="card-title">This Week</div>
    <div class="chart-bar" id="weekly-chart"></div>
  </div>

  <div class="card">
    <div class="card-title">QA by Post Type</div>
    <div id="qa-by-type" style="font-size:12px;color:var(--muted)">Loading...</div>
  </div>
</div>

<!-- ═══ CREATIVES ═══ -->
<div class="page" id="pg-creatives">
  <div class="panels">
    <div class="panel-left" id="creative-list">Loading...</div>
    <div class="panel-right" id="creative-detail"><div style="color:var(--muted);padding:40px;text-align:center">Select an agent</div></div>
  </div>
</div>

<!-- ═══ APPS ═══ -->
<div class="page" id="pg-apps">
  <div class="panels">
    <div class="panel-left" id="app-list">Loading...</div>
    <div class="panel-right" id="app-detail"><div style="color:var(--muted);padding:40px;text-align:center">Select an app</div></div>
  </div>
</div>

<!-- ═══ VAULT ═══ -->
<div class="page" id="pg-vault">
  <div style="display:flex;gap:10px;align-items:center;margin-bottom:12px;flex-wrap:wrap">
    <select class="input" style="width:160px" id="v-app" onchange="loadVault()"><option value="">All Apps</option></select>
    <select class="input" style="width:160px" id="v-creative" onchange="loadVault()"><option value="">All Agents</option></select>
    <select class="input" style="width:120px" id="v-status" onchange="loadVault()"><option value="">All Status</option><option value="ready">Ready</option><option value="failed">Failed</option></select>
    <select class="input" style="width:120px" id="v-post-type" onchange="loadVault()"><option value="">All Types</option><option value="link_in_bio">Link in Bio</option><option value="follower">Follower</option><option value="value">Value</option><option value="trust">Trust</option><option value="engagement">Engagement</option><option value="trending">Trending</option></select>
    <select class="input" style="width:120px" id="v-qa" onchange="loadVault()"><option value="">All QA</option><option value="8">8+</option><option value="7">7+</option></select>
    <input class="input" style="width:200px" placeholder="Search..." id="v-search" oninput="debounce(loadVault,300)()">
  </div>
  <!-- Bulk action bar -->
  <div id="vault-bulk-bar" style="display:none;gap:8px;align-items:center;margin-bottom:12px;padding:8px 12px;background:var(--bg3);border-radius:8px;border:1px solid var(--border);flex-wrap:wrap">
    <label style="font-size:12px;color:var(--dim);cursor:pointer"><input type="checkbox" id="v-select-all" onchange="vaultToggleAll(this.checked)" style="margin-right:4px;accent-color:var(--cyan)"> Select All</label>
    <span id="v-sel-count" style="font-size:12px;color:var(--cyan);font-weight:600;margin-left:4px"></span>
    <div style="flex:1"></div>
    <button onclick="vaultBulk('queue')" style="padding:4px 12px;font-size:11px;color:#22c55e;background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.2);border-radius:6px;cursor:pointer">Add to Queue</button>
    <button onclick="vaultBulk('approve')" style="padding:4px 12px;font-size:11px;color:var(--cyan);background:var(--cyan-d);border:1px solid rgba(0,229,255,0.2);border-radius:6px;cursor:pointer">Approve All</button>
    <button onclick="vaultBulk('reject')" style="padding:4px 12px;font-size:11px;color:var(--yellow);background:rgba(234,179,8,0.1);border:1px solid rgba(234,179,8,0.2);border-radius:6px;cursor:pointer">Reject All</button>
    <button onclick="vaultBulk('delete')" style="padding:4px 12px;font-size:11px;color:var(--red);background:rgba(239,68,68,0.1);border:1px solid rgba(239,68,68,0.2);border-radius:6px;cursor:pointer">Delete All</button>
    <button onclick="vaultClearSel()" style="padding:4px 12px;font-size:11px;color:var(--muted);background:transparent;border:1px solid var(--border);border-radius:6px;cursor:pointer">Cancel</button>
  </div>
  <div class="grid-3" id="vault-grid" style="margin-bottom:16px">Loading...</div>
  <div id="vault-pages" style="display:flex;justify-content:center;gap:6px;margin-bottom:8px"></div>
  <div style="font-size:11px;color:var(--muted);text-align:center" id="vault-footer"></div>
</div>

<!-- ═══ SETTINGS ═══ -->
<div class="page" id="pg-settings">
  <div style="max-width:600px">
    <div class="card" style="margin-bottom:16px">
      <div class="card-title">API Keys</div>
      <div class="field"><div class="field-label">Anthropic (Claude)</div><input class="input" type="password" id="key-anthropic" placeholder="sk-ant-..."></div>
      <div class="field"><div class="field-label">fal.ai (Images)</div><input class="input" type="password" id="key-fal" placeholder="fal key..."></div>
      <div class="field"><div class="field-label">ElevenLabs (Voice)</div><input class="input" type="password" id="key-eleven" placeholder="elevenlabs key..."></div>
      <div class="field"><div class="field-label">D-ID (Talking Heads)</div><input class="input" type="password" id="key-did" placeholder="D-ID key..."></div>
      <div class="field"><div class="field-label">Mirage Video API</div><input class="input" type="password" id="key-mirage" placeholder="Mirage API key (api.mirage.app)..."></div>
      <button class="btn btn-primary" onclick="saveKeys()">Save Keys</button>
    </div>
    <div class="card">
      <div class="card-title">Billing</div>
      <div id="billing-info" style="font-size:12px;color:var(--dim)">Loading...</div>
    </div>
  </div>
</div>

<div id="toast-area"></div>
<div id="modal-area"></div>

<script>
// ─── State ───
let deployCount = 1;
let currentJobId = null;
let pollTimer = null;
let allApps = [];
let allCreatives = [];
let selectedCreativeId = null;
let selectedAppId = null;

// ─── Navigation ───
function navigate(page, el) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('on'));
  document.getElementById('pg-' + page).classList.add('on');
  document.querySelectorAll('.nav-items a').forEach(a => a.classList.remove('on'));
  if (el) el.classList.add('on');
  if (page === 'dashboard') loadDashboard();
  if (page === 'creatives') loadCreativesList();
  if (page === 'apps') loadAppsList();
  if (page === 'vault') loadVault();
  if (page === 'settings') loadSettings();
}

// ─── Utilities ───
function formatTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const diff = Math.floor((now - d) / 1000);
  if (diff < 60) return 'just now';
  if (diff < 3600) return Math.floor(diff/60) + 'm ago';
  if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
  return Math.floor(diff/86400) + 'd ago';
}
function qaClass(score) { return score >= 7.5 ? 'qa-pass' : score >= 6 ? 'qa-warn' : 'qa-fail'; }
function showToast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast toast-' + (type||'success');
  t.textContent = msg;
  document.getElementById('toast-area').appendChild(t);
  setTimeout(() => t.remove(), 4000);
}
let _debounceTimers = {};
function debounce(fn, ms) { return function() { clearTimeout(_debounceTimers[fn]); _debounceTimers[fn] = setTimeout(fn, ms); }; }

// ─── Dashboard ───
async function loadDashboard() {
  // Stats
  try {
    const r = await fetch('/api/dashboard/stats');
    const d = (await r.json()).data || {};
    document.getElementById('s-videos').textContent = d.videos_this_week || 0;
    const vd = d.videos_delta || 0;
    document.getElementById('s-videos-delta').innerHTML = vd !== 0 ? '<span class="delta ' + (vd > 0 ? 'up' : 'down') + '">' + (vd > 0 ? '+' : '') + vd + '</span>' : '';
    document.getElementById('s-credits').textContent = '$' + (d.credits_this_month || 0).toFixed(2);
    document.getElementById('s-credits-sub').textContent = '$' + (d.credits_today || 0).toFixed(2) + ' today';
    document.getElementById('s-qa').textContent = (d.avg_qa_score || 0).toFixed(1);
    const qd = d.avg_qa_delta || 0;
    document.getElementById('s-qa-delta').innerHTML = qd !== 0 ? '<span class="delta ' + (qd > 0 ? 'up' : 'down') + '">' + (qd > 0 ? '+' : '') + qd.toFixed(1) + '</span>' : '';
    document.getElementById('s-errors').textContent = d.errors_today || 0;
    document.getElementById('s-errors-sub').textContent = d.last_error ? d.last_error.message.substring(0,60) : 'No errors';
    // QA by post type chart
    const qbt = d.qa_by_post_type || {};
    const ptTypes = ['link_in_bio','follower','value','trust','engagement','trending'];
    const ptEl = document.getElementById('qa-by-type');
    if (Object.keys(qbt).length === 0) { ptEl.innerHTML = '<span style="color:var(--dim)">No data yet — deploy videos to see stats</span>'; }
    else {
      let ptHtml = '';
      ptTypes.forEach(pt => {
        const data = qbt[pt];
        if (!data) return;
        const pct = Math.round(data.avg_qa * 10);
        const color = postTypeColors[pt] || 'var(--cyan)';
        const passRate = data.count > 0 ? Math.round(data.passed / data.count * 100) : 0;
        ptHtml += '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">' +
          '<span style="width:80px;font-size:11px;color:' + color + '">' + (postTypeLabels[pt]||pt) + '</span>' +
          '<div style="flex:1;background:var(--bg2);height:20px;border-radius:4px;overflow:hidden;position:relative">' +
            '<div style="height:100%;width:' + pct + '%;background:' + color + '30;border-radius:4px"></div>' +
            '<span style="position:absolute;left:8px;top:2px;font-size:10px;color:var(--text)">' + data.avg_qa + ' avg</span>' +
          '</div>' +
          '<span style="font-size:10px;color:var(--dim);width:70px;text-align:right">' + data.count + ' videos · ' + passRate + '%</span>' +
        '</div>';
      });
      ptEl.innerHTML = ptHtml || '<span style="color:var(--dim)">No data yet</span>';
    }
  } catch(e) {}

  // Dropdowns
  try {
    const ar = await fetch('/api/apps'); const aj = await ar.json(); const ad = aj.data || aj;
    allApps = Array.isArray(ad) ? ad : [];
    document.getElementById('d-app').innerHTML = allApps.map(a => '<option value="' + (a.slug||a.id||'') + '">' + (a.app_name||a.name||'Unnamed') + '</option>').join('') || '<option>No apps</option>';
  } catch(e) { console.error('Apps load error:', e); }
  try {
    const cr = await fetch('/api/creatives'); const cj = await cr.json(); const cd = cj.data || cj;
    allCreatives = cd;
    document.getElementById('d-creative').innerHTML = cd.map(c => '<option value="' + c.id + '">' + c.name + (c.avg_qa ? ' — ' + c.avg_qa + ' QA' : '') + '</option>').join('') || '<option>No agents</option>';
  } catch(e) {}

  // Recent videos
  try {
    const r = await fetch('/api/dashboard/recent-videos');
    const d = (await r.json()).data || [];
    document.getElementById('recent-videos').innerHTML = d.length ? d.map(v =>
      '<div style="display:flex;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid var(--border)">' +
        '<div style="width:32px;height:32px;border-radius:6px;background:var(--bg2);display:flex;align-items:center;justify-content:center;font-size:14px">🎬</div>' +
        '<div style="flex:1;min-width:0"><div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + (v.title||'Untitled') + '</div>' +
        '<div style="font-size:10px;color:var(--dim)">' + (v.app_name||'') + '</div></div>' +
        (v.status === 'failed' ? '<span style="font-size:9px;padding:2px 5px;border-radius:4px;background:rgba(255,59,48,0.2);color:#ff3b30;font-weight:700">FAIL</span>' : '') +
        (v.qa_score ? '<span class="qa ' + qaClass(v.qa_score) + '">' + v.qa_score.toFixed(1) + '</span>' : '') +
        '<span style="font-size:10px;color:var(--muted)">' + formatTime(v.created_at) + '</span>' +
      '</div>'
    ).join('') : '<div style="color:var(--muted)">No videos yet</div>';
  } catch(e) {}

  // Activity
  try {
    const r = await fetch('/api/dashboard/activity');
    const d = (await r.json()).data || [];
    document.getElementById('recent-activity').innerHTML = d.length ? d.map(a =>
      '<div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid var(--border)">' +
        '<div style="width:6px;height:6px;border-radius:50%;background:' + (a.type==='error' ? 'var(--red)' : 'var(--green)') + ';flex-shrink:0"></div>' +
        '<div style="flex:1;font-size:11px">' + (a.message||'') + '</div>' +
        '<span style="font-size:10px;color:var(--muted)">' + formatTime(a.timestamp) + '</span>' +
      '</div>'
    ).join('') : '<div style="color:var(--muted)">No activity yet</div>';
  } catch(e) {}

  // Chart
  try {
    const r = await fetch('/api/dashboard/weekly-chart');
    const d = (await r.json()).data || [];
    const max = Math.max(...d.map(x => x.count), 1);
    document.getElementById('weekly-chart').innerHTML = d.map(x => {
      const h = Math.max(4, (x.count / max) * 100);
      const c = x.avg_qa >= 7.5 ? 'var(--green)' : x.avg_qa >= 6 ? 'var(--yellow)' : 'var(--muted)';
      return '<div class="chart-col"><div class="bar" style="height:' + h + '%;background:' + c + '" title="' + x.count + ' videos, ' + x.avg_qa + ' QA"></div><div class="label">' + x.day + '</div></div>';
    }).join('');
  } catch(e) {}
}

// ─── Deploy ───
const postTypeLabels = {link_in_bio:'Link in Bio',follower:'Follower',value:'Value',trust:'Trust',engagement:'Engagement',trending:'Trending'};
const postTypeColors = {link_in_bio:'#4fc3f7',follower:'#ab47bc',value:'#66bb6a',trust:'#ffa726',engagement:'#ef5350',trending:'#26c6da'};
let contentMix = [{type:'link_in_bio'}];
function renderMix() {
  const el = document.getElementById('d-mix');
  el.innerHTML = contentMix.map((m,i) => '<div style="display:flex;align-items:center;gap:8px;padding:4px 8px;margin-bottom:4px;background:var(--bg2);border-radius:6px;border-left:3px solid ' + (postTypeColors[m.type]||'var(--cyan)') + '"><span style="font-size:11px;color:var(--text);flex:1">' + (postTypeLabels[m.type]||m.type) + '</span><button class="btn btn-ghost btn-sm" onclick="removeMix(' + i + ')" style="font-size:10px;color:var(--muted);padding:2px 6px">✕</button></div>').join('');
  deployCount = contentMix.length;
  document.getElementById('d-estimate').textContent = contentMix.length + ' video' + (contentMix.length!==1?'s':'') + ' · Est. $' + (contentMix.length * 0.5).toFixed(2) + ' · ~' + (contentMix.length * 2) + ' min';
}
function addToMix() { if (contentMix.length >= 10) return; contentMix.push({type: document.getElementById('d-add-type').value}); renderMix(); }
function removeMix(i) { contentMix.splice(i, 1); renderMix(); }
function clearMix() { contentMix = []; renderMix(); }
renderMix();
async function onEngineChange() {
  const eng = document.getElementById('d-engine').value;
  const avatarField = document.getElementById('d-avatar-field');
  if (eng === 'captions_ai') {
    avatarField.style.display = '';
    try {
      const r = await fetch('/api/captions-ai/creators');
      const d = await r.json();
      const creators = d.data || [];
      document.getElementById('d-avatar').innerHTML = creators.map(c =>
        '<option value="' + c.name + '">' + c.name + '</option>'
      ).join('') || '<option>No avatars (check API key)</option>';
    } catch(e) { document.getElementById('d-avatar').innerHTML = '<option>Error loading</option>'; }
  } else { avatarField.style.display = 'none'; }
  // Show template dropdown for hybrid/remotion engines
  const tplField = document.getElementById('d-template-field');
  tplField.style.display = (eng === 'hybrid' || eng === 'remotion_stock') ? '' : 'none';
}
async function deploy() {
  const app = document.getElementById('d-app').value;
  const creative = document.getElementById('d-creative').value;
  if (!app) { showToast('Select an app', 'error'); return; }
  document.getElementById('d-btn').disabled = true;
  document.getElementById('d-btn').textContent = 'DEPLOYING...';
  try {
    const duration = document.getElementById('d-duration').value;
    const videoEngine = document.getElementById('d-engine').value;
    const avatar = document.getElementById('d-avatar').value;
    if (contentMix.length === 0) { showToast('Add at least one post type', 'error'); return; }
    const qaTarget = parseFloat(document.getElementById('d-train-target').value || 7.0);
    const remotionTemplate = document.getElementById('d-template').value;
    const r = await fetch('/api/generate/' + app, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({creative_id: creative, count: contentMix.length, target_duration: duration, video_engine: videoEngine, remotion_template: remotionTemplate, captions_ai_avatar: avatar, training_mode: true, training_target: qaTarget, content_mix: contentMix.map(m => m.type)}) });
    const d = await r.json();
    if (d.job_id) { currentJobId = d.job_id; pollTimer = setInterval(() => pollJob(d.job_id), 2000); }
    else { showToast(d.error || 'Deploy failed', 'error'); }
  } catch(e) { showToast('Deploy error: ' + e, 'error'); }
  document.getElementById('d-btn').disabled = false;
  document.getElementById('d-btn').textContent = '⚡ DEPLOY';
}

async function pollJob(jid) {
  try {
    const r = await fetch('/api/job/' + jid);
    const resp = await r.json();
    const j = resp.data || resp;  // Handle both {success,data} and flat formats
    const stages = j.pipeline_stages || {};
    const total = j.total_count || j.total || j.video_count || 1;
    const done = j.videos_created || j.videos_completed || j.completed || 0;
    const pct = Math.round(done / total * 100);
    const cost = j.cost || j.cost_breakdown || {};
    const costTotal = typeof cost === 'object' ? (cost.total || 0) : 0;

    // Build steps display
    const stageNames = {script:'Script', images:'Images', voice:'Voice', edit:'Edit', qa:'QA', ready:'Ready'};
    let stepsHtml = '<div class="steps" style="margin-bottom:10px">';
    Object.entries(stageNames).forEach(([k,v], i) => {
      const items = (stages[k]||[]).length;
      const cls = items >= total ? 'done' : items > 0 ? 'active' : 'waiting';
      stepsHtml += (i > 0 ? '<span class="step-arrow">→</span>' : '') + '<span class="step ' + cls + '">' + (cls==='done'?'✅':cls==='active'?'🔵':'⏳') + ' ' + v + (cls==='active'?' '+items+'/'+total:'') + '</span>';
    });
    stepsHtml += '</div>';

    document.getElementById('prod-status').innerHTML = stepsHtml +
      '<div style="background:var(--bg2);height:6px;border-radius:3px;overflow:hidden;margin-bottom:8px"><div style="height:100%;width:' + pct + '%;background:var(--cyan);border-radius:3px;transition:width .3s"></div></div>' +
      '<div style="display:flex;justify-content:space-between;font-size:11px;color:var(--dim)"><span>' + (j.message||'Processing...') + '</span><span>$' + costTotal.toFixed(2) + ' spent</span></div>';

    // Script preview mode
    if (j.status === 'awaiting_script_approval') {
      const scripts = j.preview_scripts || [];
      const slideColors = {hook:'#4fc3f7',demo:'#66bb6a',cta:'#ffa726'};
      let html = '<div style="margin-bottom:14px;display:flex;align-items:baseline;gap:10px"><span style="color:var(--cyan);font-family:var(--hud);font-size:14px;letter-spacing:1px">SCRIPT PREVIEW</span><span style="font-size:12px;color:var(--dim)">Review before generating images · $' + costTotal.toFixed(2) + ' so far</span></div>';
      scripts.forEach((s, i) => {
        const ptColor = postTypeColors[s.post_type] || 'var(--cyan)';
        html += '<div style="background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:12px">';
        // Title
        html += '<div style="font-family:var(--hud);font-size:15px;color:var(--text);margin-bottom:4px">' + s.title + '</div>';
        // Metadata line
        html += '<div style="font-size:11px;color:var(--dim);margin-bottom:12px;display:flex;gap:8px;align-items:center">';
        html += '<span style="padding:2px 8px;border-radius:4px;background:' + ptColor + '20;color:' + ptColor + ';font-size:10px;font-weight:600">' + (postTypeLabels[s.post_type]||s.post_type) + '</span>';
        html += '<span>' + s.persona + '</span><span style="color:var(--border)">|</span><span>' + s.video_style + '</span></div>';
        // Hook
        html += '<div style="font-size:14px;color:var(--cyan);margin-bottom:14px;font-weight:600">Hook: "' + (s.hook_text||'') + '"</div>';
        // Slides
        (s.slides||[]).forEach((sl, si) => {
          const stColor = slideColors[sl.slide_type] || 'var(--dim)';
          const srcLabel = sl.source && sl.source !== 'ai_generated' ? sl.source.replace('video_footage:','').replace('app_screenshot:','') : '';
          html += '<div style="padding:10px 0 10px 14px;border-left:3px solid ' + stColor + ';margin-bottom:6px">';
          // Slide header
          html += '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">';
          html += '<span style="font-size:12px;font-weight:700;color:' + stColor + ';text-transform:uppercase;letter-spacing:0.5px">' + (si+1) + '. ' + (sl.slide_type||'slide') + '</span>';
          if (srcLabel) html += '<span style="font-size:10px;padding:1px 6px;border-radius:4px;background:rgba(102,187,106,0.15);color:#66bb6a">video: ' + srcLabel + '</span>';
          html += '</div>';
          // Voiceover — full text, wrapping
          html += '<div style="font-size:13px;color:var(--text);line-height:1.5;padding-left:2px">"' + (sl.voiceover||'<em style=color:var(--dim)>no voiceover</em>') + '"</div>';
          html += '</div>';
        });
        // Caption
        html += '<div style="font-size:11px;color:var(--dim);margin-top:8px;padding-top:8px;border-top:1px solid var(--border);line-height:1.4">' + (s.description||'') + '</div>';
        html += '</div>';
      });
      html += '<div style="display:flex;gap:10px;margin-top:14px">';
      html += '<button class="btn btn-primary" style="flex:1;font-family:var(--hud);padding:10px;font-size:13px;letter-spacing:1px" onclick="approveScripts()">APPROVE & GENERATE</button>';
      html += '<button class="btn btn-ghost" style="flex:1;padding:10px;font-size:12px" onclick="rejectScripts()">REJECT & REDEPLOY</button>';
      html += '</div>';
      document.getElementById('prod-status').innerHTML = html;
      return;
    }

    if (j.status === 'done' || j.status === 'error') {
      clearInterval(pollTimer); pollTimer = null; currentJobId = null;
      if (j.status === 'done') { showToast('Done! ' + done + ' videos created · $' + costTotal.toFixed(2), 'success'); loadDashboard(); }
      else showToast('Error: ' + (j.error_message || j.message), 'error');
    }
  } catch(e) {}
}

// ─── Script Approval ───
async function approveScripts() {
  if (!currentJobId) return;
  try {
    const r = await fetch('/api/job/' + currentJobId + '/approve-scripts', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' });
    const d = await r.json();
    if (d.success) { showToast('Scripts approved — generating videos...', 'success'); }
    else { showToast(d.error || 'Approval failed', 'error'); }
  } catch(e) { showToast('Error: ' + e, 'error'); }
}
async function rejectScripts() {
  if (!currentJobId) return;
  const feedback = prompt('What should the agent do differently? (optional)');
  if (feedback === null) return; // user cancelled prompt
  try {
    const r = await fetch('/api/job/' + currentJobId + '/reject-scripts', { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({feedback: feedback || ''}) });
    const d = await r.json();
    showToast('Regenerating with feedback...', 'info');
    document.getElementById('prod-status').innerHTML = '<div style="color:var(--muted);font-size:12px">Updating agent prompt and regenerating scripts...</div>';
    // Don't clear poll — job continues with regenerated scripts
  } catch(e) { showToast('Error: ' + e, 'error'); }
}

// ─── Creatives ───
async function loadCreativesList() {
  try {
    const r = await fetch('/api/creatives');
    const d = (await r.json()).data || [];
    allCreatives = d;
    document.getElementById('creative-list').innerHTML = d.map(c =>
      '<div class="list-item' + (selectedCreativeId === c.id ? ' on' : '') + '" onclick="selectCreative(\'' + c.id + '\')">' +
        '<div style="display:flex;align-items:center;gap:10px">' +
          '<div style="width:32px;height:32px;border-radius:50%;background:' + (c.avatar_color||'var(--cyan)') + '20;color:' + (c.avatar_color||'var(--cyan)') + ';display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px">' + (c.name||'?')[0] + '</div>' +
          '<div><div class="list-item-name">' + c.name + '</div><div class="list-item-sub">' + (c.specialty||'') + '</div></div>' +
        '</div>' +
        '<div style="margin-top:4px;display:flex;gap:6px">' +
          '<span style="font-size:10px;color:var(--dim)">' + (c.video_count||0) + ' videos' + (c.failed_count ? ' · ' + c.failed_count + ' failed' : '') + '</span>' +
          (c.avg_qa ? '<span class="qa ' + qaClass(c.avg_qa) + '">' + c.avg_qa + '</span>' : '') +
        '</div>' +
      '</div>'
    ).join('') +
    '<div class="list-item" onclick="createCreative()" style="text-align:center;color:var(--cyan);font-size:12px">+ New Agent</div>';
  } catch(e) { document.getElementById('creative-list').innerHTML = '<div style="padding:16px;color:var(--red)">Failed to load</div>'; }
}

async function selectCreative(id) {
  selectedCreativeId = id;
  loadCreativesList();
  try {
    const r = await fetch('/api/creatives/' + id);
    const c = (await r.json()).data;
    if (!c) return;
    const wc = c.writing_config || {};
    const vc = c.voice_config || {};
    const vis = c.visual_config || {};
    const pc = c.pacing_config || {};
    const cg = c.color_grade || {};
    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
    document.getElementById('creative-detail').innerHTML =
      '<div style="display:flex;align-items:center;gap:14px;margin-bottom:16px">' +
        '<div style="width:48px;height:48px;border-radius:50%;background:' + (c.avatar_color||'var(--cyan)') + '20;color:' + (c.avatar_color||'var(--cyan)') + ';display:flex;align-items:center;justify-content:center;font-weight:800;font-size:18px">' + (c.name||'?')[0] + '</div>' +
        '<div style="flex:1"><input id="creative-name-input" value="' + esc(c.name) + '" style="font-size:18px;font-weight:700;background:transparent;border:1px solid var(--border);border-radius:6px;padding:4px 8px;color:var(--text);width:100%;font-family:var(--ui)" /><input id="creative-specialty-input" value="' + esc(c.specialty) + '" placeholder="Specialty..." style="font-size:12px;color:var(--dim);background:transparent;border:1px solid var(--border);border-radius:4px;padding:2px 6px;width:100%;font-family:var(--ui);margin-top:4px" /></div>' +
        '<div style="display:flex;gap:6px;flex-direction:column"><button class="btn btn-primary btn-sm" onclick="saveCreativeProfile()">Save</button><button class="btn btn-ghost btn-sm" onclick="cloneCreative(\'' + id + '\')">Clone</button></div>' +
      '</div>' +
      '<div style="display:flex;gap:16px;margin-bottom:16px">' +
        '<div style="font-size:11px"><strong>' + (c.video_count||0) + '</strong> <span style="color:var(--dim)">passed</span>' + (c.failed_count ? ' · <strong style="color:var(--red)">' + c.failed_count + '</strong> <span style="color:var(--dim)">failed</span>' : '') + '</div>' +
        '<div style="font-size:11px"><strong>' + (c.avg_qa||0) + '</strong> <span style="color:var(--dim)">avg QA</span></div>' +
        '<div style="font-size:11px"><strong>$' + (c.total_spent||0) + '</strong> <span style="color:var(--dim)">spent</span></div>' +
      '</div>' +
      '<div class="field" style="margin-top:16px"><div class="field-label">Agent Prompt</div><p style="font-size:10px;color:var(--dim);margin-bottom:6px">This is the agent\'s personality. How they write, what style, tone, structure — everything in one place.</p><textarea class="input" id="agent-prompt" style="min-height:200px">' + esc(wc.identity || '') + '</textarea></div>' +
      '<div style="border-top:1px solid var(--border);padding-top:16px;margin-top:16px">' +
        '<div class="field-label" style="margin-bottom:12px">Voice</div>' +
        '<div class="field"><div class="field-label">ElevenLabs Voice ID</div><input class="input" id="agent-voice-id" value="' + esc(vc.elevenlabs_voice_id) + '"></div>' +
        '<div class="field"><div class="field-label">Speed (' + (vc.speaking_speed||1.0) + ') <span style="color:var(--dim);font-weight:400">— 0.8 slower, 1.0 normal, 1.2 faster</span></div><input type="range" min="0.7" max="1.2" step="0.05" value="' + (vc.speaking_speed||1.0) + '" id="agent-speed"></div>' +
      '</div>' +
      '<button class="btn btn-primary" style="margin-top:16px;width:100%" onclick="saveAgent()">Save Agent</button>';
  } catch(e) { document.getElementById('creative-detail').innerHTML = '<div style="color:var(--red);padding:20px">Failed to load</div>'; }
}

async function saveAgent() {
  if (!selectedCreativeId) return;
  const name = document.getElementById('creative-name-input')?.value?.trim();
  const specialty = document.getElementById('creative-specialty-input')?.value?.trim();
  const prompt = document.getElementById('agent-prompt')?.value || '';
  const voiceId = document.getElementById('agent-voice-id')?.value?.trim() || '';
  const speed = parseFloat(document.getElementById('agent-speed')?.value || 1.0);
  if (!name) { showToast('Name is required', 'error'); return; }
  const data = {
    name, specialty,
    writing_config: { identity: prompt },
    voice_config: { elevenlabs_voice_id: voiceId, speaking_speed: speed }
  };
  const r = await fetch('/api/creatives/' + selectedCreativeId, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  if (r.ok) { showToast('Saved!', 'success'); selectCreative(selectedCreativeId); loadCreativesList(); } else { const err = await r.json().catch(() => ({})); showToast('Save failed: ' + (err.error || r.statusText), 'error'); }
}

// Keep saveCreativeProfile for the top Save button
async function saveCreativeProfile() { saveAgent(); }

async function cloneCreative(id) {
  await fetch('/api/creatives/' + id + '/clone', {method:'POST'});
  loadCreativesList();
  showToast('Agent cloned');
}

async function createCreative() {
  const name = prompt('Agent name:');
  if (!name) return;
  await fetch('/api/creatives', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name})});
  loadCreativesList();
}

// ─── Apps ───
async function loadAppsList() {
  try {
    const r = await fetch('/api/apps');
    let d = await r.json();
    d = d.data || d;
    allApps = Array.isArray(d) ? d : [];
    document.getElementById('app-list').innerHTML = allApps.map(a =>
      '<div class="list-item' + (selectedAppId === (a.slug||a.id||'') ? ' on' : '') + '" onclick="selectApp(\'' + (a.slug||a.id||'') + '\')">' +
        '<div class="list-item-name">' + (a.app_name||a.name||'Unnamed') + '</div>' +
        '<div class="list-item-sub">' + (a.tiktok_handle||'') + ' · ' + (a.video_count||0) + ' videos</div>' +
      '</div>'
    ).join('') +
    '<div class="list-item" onclick="createApp()" style="text-align:center;color:var(--cyan);font-size:12px">+ New App</div>';
  } catch(e) {}
}

async function selectApp(id) {
  selectedAppId = id;
  loadAppsList();
  try {
    const r = await fetch('/api/apps/' + id + '/config');
    const a = await r.json();
    document.getElementById('app-detail').innerHTML =
      '<h2 style="font-size:18px;font-weight:700;margin-bottom:16px">' + (a.app_name||id) + '</h2>' +
      '<div class="field"><div class="field-label">Description</div><textarea class="input" id="ae-desc">' + (a.app_description||'') + '</textarea></div>' +
      '<div class="field"><div class="field-label">TikTok Handle</div><input class="input" id="ae-handle" value="' + (a.tiktok_handle||'') + '"></div>' +
      '<div class="field"><div class="field-label">Link in Bio</div><input class="input" id="ae-link" value="' + (a.link_in_bio_url||'') + '"></div>' +
      '<button class="btn btn-primary" onclick="saveApp(\'' + id + '\')" style="margin-bottom:24px">Save App</button>' +
      // Media uploads section
      '<div style="border-top:1px solid var(--border);padding-top:16px;margin-top:8px">' +
        '<h3 style="font-size:14px;font-weight:700;margin-bottom:12px;color:var(--cyan)">App Media</h3>' +
        '<p style="font-size:11px;color:var(--dim);margin-bottom:12px">Upload screenshots and screen recordings. AI will use these in your videos for authentic app demos.</p>' +
        '<div style="display:flex;gap:8px;margin-bottom:12px">' +
          '<label class="btn btn-primary" style="cursor:pointer;font-size:12px;padding:6px 12px"><input type="file" accept="image/*" multiple style="display:none" onchange="uploadAppMedia(\'' + id + '\', this.files, \'screenshot\')">Upload Screenshots</label>' +
          '<label class="btn btn-primary" style="cursor:pointer;font-size:12px;padding:6px 12px;background:var(--bg2);border:1px solid var(--border)"><input type="file" accept="video/*" multiple style="display:none" onchange="uploadAppMedia(\'' + id + '\', this.files, \'video_footage\')">Upload Videos</label>' +
          '<button class="btn btn-ghost" style="font-size:11px;padding:6px 12px" onclick="autoLabelAll(\'' + id + '\')">Auto-Label All</button>' +
        '</div>' +
        '<div id="app-media-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(100px,1fr));gap:8px">Loading media...</div>' +
      '</div>';
    loadAppMedia(id);
  } catch(e) {}
}

async function saveApp(id) {
  const data = { tiktok_handle: document.getElementById('ae-handle').value, link_in_bio_url: document.getElementById('ae-link').value };
  const r = await fetch('/api/apps/' + id + '/config', {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  if (r.ok) { showToast('App saved!', 'success'); } else { const err = await r.json().catch(() => ({})); showToast('Save failed: ' + (err.error || r.statusText), 'error'); }
}

async function createApp() {
  const name = prompt('App name:');
  if (!name) return;
  const fd = new FormData(); fd.append('name', name);
  await fetch('/api/apps', {method:'POST', body:fd});
  showToast('Creating app...');
  setTimeout(loadAppsList, 3000);
}

async function uploadAppMedia(appId, files, mediaType) {
  if (!files || !files.length) return;
  for (const file of files) {
    const fd = new FormData();
    fd.append('file', file);
    fd.append('media_type', mediaType);
    try {
      const r = await fetch('/api/apps/' + appId + '/media', {method:'POST', body:fd});
      if (r.ok) { showToast('Uploaded: ' + file.name, 'success'); } else { const e = await r.json().catch(()=>({})); showToast('Upload failed: ' + (e.error||r.statusText), 'error'); }
    } catch(e) { showToast('Upload error: ' + e, 'error'); }
  }
  loadAppMedia(appId);
}

async function loadAppMedia(appId) {
  const el = document.getElementById('app-media-list');
  if (!el) return;
  try {
    const r = await fetch('/api/apps/' + appId + '/media');
    const d = await r.json();
    const items = d.data || d || [];
    if (!items.length) { el.innerHTML = '<div style="color:var(--muted);font-size:11px;grid-column:1/-1">No media uploaded yet. Upload screenshots and recordings to make your videos more authentic.</div>'; return; }
    el.innerHTML = items.map(m => {
      const isVideo = m.media_type === 'video_footage';
      return '<div style="border-radius:6px;overflow:hidden;background:var(--bg2)">' +
        '<div style="aspect-ratio:9/16;position:relative;overflow:hidden;cursor:pointer" onclick="openMediaViewer(\'/api/media-file/' + m.file_path.split('/output/').pop() + '\',' + isVideo + ')">' +
          (isVideo ? '<video src="/api/media-file/' + m.file_path.split('/output/').pop() + '" style="width:100%;height:100%;object-fit:cover" muted preload="metadata" onloadeddata="this.currentTime=0.5"></video>' :
          '<img src="/api/media-file/' + m.file_path.split('/output/').pop() + '" style="width:100%;height:100%;object-fit:cover" onerror="this.style.display=\'none\'">') +
          '<button onclick="deleteAppMedia(\'' + appId + '\',\'' + m.id + '\')" style="position:absolute;top:2px;right:2px;background:rgba(0,0,0,0.6);border:none;color:#ff3b30;font-size:10px;padding:2px 5px;border-radius:3px;cursor:pointer">x</button>' +
          '<div style="position:absolute;top:2px;left:2px;background:rgba(0,0,0,0.6);color:var(--cyan);font-size:8px;padding:2px 5px;border-radius:3px">' + (isVideo?'Video':'Screenshot') + '</div>' +
        '</div>' +
        '<input value="' + (m.description||'').replace(/"/g,'&quot;') + '" placeholder="' + (isVideo ? 'Describe what happens: 0-3s opening app, 3-8s tapping feature...' : 'What does this screen show?') + '" ' +
          'onblur="updateMediaLabel(\'' + appId + '\',\'' + m.id + '\',this.value)" ' +
          'style="width:100%;padding:4px 6px;font-size:10px;background:var(--bg3);border:none;border-top:1px solid var(--border);color:var(--text);font-family:var(--ui)">' +
      '</div>';
    }).join('');
  } catch(e) { el.innerHTML = '<div style="color:var(--red);font-size:11px">Failed to load media</div>'; }
}

function openMediaViewer(src, isVideo) {
  const bg = document.createElement('div');
  bg.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.9);z-index:999;display:flex;align-items:center;justify-content:center;cursor:zoom-out;overflow:auto';
  bg.onclick = () => bg.remove();
  if (isVideo) {
    const v = document.createElement('video');
    v.src = src; v.controls = true; v.autoplay = true;
    v.style.cssText = 'max-height:90vh;max-width:90vw;border-radius:8px';
    v.onclick = e => e.stopPropagation();
    bg.appendChild(v);
  } else {
    const img = document.createElement('img');
    img.src = src;
    img.style.cssText = 'max-height:90vh;max-width:none;border-radius:8px;cursor:grab';
    img.onclick = e => e.stopPropagation();
    // Allow horizontal scrolling on wide images
    const wrap = document.createElement('div');
    wrap.style.cssText = 'overflow-x:auto;max-width:90vw';
    wrap.onclick = e => e.stopPropagation();
    wrap.appendChild(img);
    bg.appendChild(wrap);
  }
  document.body.appendChild(bg);
}

async function autoLabelAll(appId) {
  showToast('AI is analyzing your media...');
  try {
    const r = await fetch('/api/apps/' + appId + '/media');
    const items = (await r.json()).data || [];
    let labeled = 0;
    for (const m of items) {
      if (m.description || m.media_type === 'video_footage') continue; // skip already labeled and videos
      try {
        const lr = await fetch('/api/apps/' + appId + '/media/' + m.id + '/auto-label', {method:'POST'});
        const ld = await lr.json();
        if (ld.success) labeled++;
      } catch(e) {}
    }
    showToast(labeled ? labeled + ' items labeled!' : 'All items already labeled', 'success');
    loadAppMedia(appId);
  } catch(e) { showToast('Error: ' + e, 'error'); }
}

async function updateMediaLabel(appId, mediaId, label) {
  await fetch('/api/apps/' + appId + '/media/' + mediaId, {method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({description: label})});
}

async function deleteAppMedia(appId, mediaId) {
  if (!confirm('Delete this media?')) return;
  await fetch('/api/apps/' + appId + '/media/' + mediaId, {method:'DELETE'});
  loadAppMedia(appId);
  showToast('Deleted');
}

async function reviewVideo(videoId, creativeId, approved) {
  if (approved) {
    const whatWorked = prompt('What made this video good? (helps the agent repeat success)');
    if (whatWorked === null) whatWorked = '';
    await fetch('/api/vault/' + videoId + '/review', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({approved: true, feedback: whatWorked, creative_id: creativeId})});
    showToast('Approved! Agent is learning what works.', 'success');
    loadVault();
    return;
  }
  const feedback = prompt('What went wrong? Be specific — this teaches the agent:');
  if (feedback === null) return;
  const r = await fetch('/api/vault/' + videoId + '/review', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({approved: false, feedback: feedback, creative_id: creativeId})});
  if (r.ok) {
    showToast('Feedback saved — agent is learning', 'success');
    loadVault();
  } else { showToast('Error saving feedback', 'error'); }
}

async function deleteVideo(videoId) {
  if (!confirm('Delete this video?')) return;
  try {
    const r = await fetch('/api/vault/' + videoId, {method:'DELETE'});
    if (r.ok) { showToast('Deleted'); loadVault(); loadDashboard(); } else showToast('Delete failed', 'error');
  } catch(e) { showToast('Error: ' + e, 'error'); }
}

// ─── Vault ───
let vaultPage = 1;
const vaultSel = new Set();

function vaultUpdateBulk() {
  const bar = document.getElementById('vault-bulk-bar');
  const cnt = document.getElementById('v-sel-count');
  if (vaultSel.size > 0) { bar.style.display = 'flex'; cnt.textContent = vaultSel.size + ' selected'; }
  else { bar.style.display = 'none'; }
}
function vaultToggleSel(id, checked) { if (checked) vaultSel.add(id); else vaultSel.delete(id); vaultUpdateBulk(); }
function vaultToggleAll(checked) { document.querySelectorAll('.v-cb').forEach(cb => { cb.checked = checked; vaultToggleSel(cb.dataset.vid, checked); }); }
function vaultClearSel() { vaultSel.clear(); document.querySelectorAll('.v-cb').forEach(cb => cb.checked = false); const sa = document.getElementById('v-select-all'); if(sa) sa.checked = false; vaultUpdateBulk(); }

async function vaultBulk(action) {
  if (!vaultSel.size) return;
  if (action === 'delete' && !confirm('Delete ' + vaultSel.size + ' videos?')) return;
  const ids = [...vaultSel];
  let ok = 0;
  for (const id of ids) {
    try {
      if (action === 'delete') {
        const r = await fetch('/api/vault/' + id, {method:'DELETE'}); if(r.ok) ok++;
      } else if (action === 'queue') {
        const r = await fetch('/api/vault/' + id + '/queue', {method:'POST'}); if(r.ok) ok++;
      } else {
        const approved = action === 'approve';
        const r = await fetch('/api/vault/' + id + '/review', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({approved})}); if(r.ok) ok++;
      }
    } catch(e) {}
  }
  showToast(ok + '/' + ids.length + ' ' + action + (action.endsWith('e')?'d':'ed'));
  vaultClearSel(); loadVault();
}

async function loadVault(page) {
  if (page) vaultPage = page;
  const params = new URLSearchParams();
  params.set('per_page', '20');
  params.set('page', String(vaultPage));
  const app = document.getElementById('v-app')?.value; if (app) params.set('app_id', app);
  const creative = document.getElementById('v-creative')?.value; if (creative) params.set('creative_id', creative);
  const qa = document.getElementById('v-qa')?.value; if (qa) params.set('min_qa', qa);
  const vstatus = document.getElementById('v-status')?.value; if (vstatus) params.set('status', vstatus);
  const vpt = document.getElementById('v-post-type')?.value; if (vpt) params.set('post_type', vpt);
  const search = document.getElementById('v-search')?.value; if (search) params.set('search', search);
  try {
    // Populate filter dropdowns
    if (!document.getElementById('v-app').options.length || document.getElementById('v-app').options.length <= 1) {
      const ar = await fetch('/api/apps'); let ad = await ar.json(); ad = ad.data || ad;
      document.getElementById('v-app').innerHTML = '<option value="">All Apps</option>' + (Array.isArray(ad)?ad:[]).map(a => '<option value="' + (a.slug||a.id||'') + '">' + (a.app_name||a.name||'Unnamed') + '</option>').join('');
    }
    if (!document.getElementById('v-creative').options.length || document.getElementById('v-creative').options.length <= 1) {
      const cr = await fetch('/api/creatives'); const cd = (await cr.json()).data || [];
      document.getElementById('v-creative').innerHTML = '<option value="">All Agents</option>' + cd.map(c => '<option value="' + c.id + '">' + c.name + '</option>').join('');
    }
    const r = await fetch('/api/vault?' + params.toString());
    const d = (await r.json()).data || {};
    const items = d.items || [];
    const total = d.total || items.length;
    document.getElementById('vault-grid').innerHTML = items.length ? items.map(v =>
      '<div class="vault-card">' +
        '<div class="vault-thumb" style="position:relative">' +
          '<input type="checkbox" class="v-cb" data-vid="' + v.id + '" ' + (vaultSel.has(v.id)?'checked':'') + ' onchange="vaultToggleSel(\'' + v.id + '\',this.checked)" style="position:absolute;top:6px;left:6px;z-index:10;width:16px;height:16px;cursor:pointer;accent-color:var(--cyan)">' +
          (v.url ? '<video src="' + v.url + '" preload="metadata" onloadeddata="this.currentTime=0.5" onclick="if(this.paused){this.muted=false;this.play()}else{this.pause()}" controls></video>' : '') +
          (v.video_engine ? '<span style="position:absolute;bottom:6px;left:6px;font-size:9px;padding:2px 6px;border-radius:4px;background:rgba(0,0,0,0.7);color:var(--cyan)">' + v.video_engine + '</span>' : '') +
        '</div>' +
        '<div class="vault-info">' +
          '<div class="vault-title">' + (v.title||'Untitled') + '</div>' +
          '<div class="vault-meta">' + (v.app_name||v.app_slug||'') + (v.creative_name ? ' · ' + v.creative_name : '') + (v.post_type ? ' · <span style="color:' + (postTypeColors[v.post_type]||'var(--cyan)') + '">' + (postTypeLabels[v.post_type]||v.post_type) + '</span>' : '') + '</div>' +
          '<div style="display:flex;gap:6px;margin-top:4px;align-items:center">' +
            (v.status === 'failed' ? '<span style="font-size:9px;padding:2px 6px;border-radius:4px;background:rgba(255,59,48,0.2);color:#ff3b30;font-weight:700">FAILED</span>' : '') +
            (v.qa_score ? '<span class="qa ' + qaClass(v.qa_score) + '" onclick="event.stopPropagation();showQA(\'' + v.id + '\')" style="cursor:pointer" title="Click for QA details">' + v.qa_score.toFixed(1) + '</span>' : '') +
            (v.cost_total ? '<span style="font-size:10px;color:var(--cyan)">$' + v.cost_total.toFixed(2) + '</span>' : '') +
            '<span style="font-size:10px;color:var(--muted)">' + formatTime(v.created_at) + '</span>' +
          '</div>' +
          '<div id="qa-detail-' + v.id + '" style="display:none;margin-top:4px;padding:6px;background:var(--bg2);border-radius:6px;font-size:10px;line-height:1.5;max-height:200px;overflow-y:auto"></div>' +
          '<div style="display:flex;gap:4px;margin-top:6px">' +
            '<button onclick="event.stopPropagation();queueVideo(\'' + v.id + '\')" style="flex:1;padding:4px;font-size:10px;color:#22c55e;background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.2);border-radius:6px;cursor:pointer">Add to Queue</button>' +
            (v.user_approved === 1 ? '<span style="padding:4px 8px;font-size:10px;color:var(--green)">Approved</span>' :
             v.user_approved === 0 ? '<span style="padding:4px 8px;font-size:10px;color:var(--red)">Rejected</span>' :
             '<button onclick="event.stopPropagation();reviewVideo(\'' + v.id + '\',\'' + (v.creative_id||'') + '\',true)" style="padding:4px 8px;font-size:10px;color:var(--green);background:rgba(34,197,94,0.1);border:1px solid rgba(34,197,94,0.2);border-radius:6px;cursor:pointer">Approve</button>' +
             '<button onclick="event.stopPropagation();reviewVideo(\'' + v.id + '\',\'' + (v.creative_id||'') + '\',false)" style="padding:4px 8px;font-size:10px;color:var(--red);background:rgba(255,59,48,0.1);border:1px solid rgba(255,59,48,0.2);border-radius:6px;cursor:pointer">Reject</button>') +
          '</div>' +
          '<button onclick="event.stopPropagation();deleteVideo(\'' + v.id + '\')" style="width:100%;margin-top:4px;padding:3px;font-size:9px;color:var(--muted);background:transparent;border:1px solid var(--border);border-radius:6px;cursor:pointer">Delete</button>' +
        '</div>' +
      '</div>'
    ).join('') : '<div style="color:var(--muted);grid-column:1/-1;text-align:center;padding:40px">No videos found</div>';
    // Pagination
    const totalPages = Math.max(1, Math.ceil(total / 20));
    const pgDiv = document.getElementById('vault-pages');
    if (totalPages > 1) {
      let pgHtml = '';
      for (let p = 1; p <= totalPages; p++) {
        const active = p === vaultPage;
        pgHtml += '<button onclick="loadVault(' + p + ')" style="padding:4px 10px;font-size:12px;border-radius:6px;cursor:pointer;border:1px solid ' + (active?'var(--cyan)':'var(--border)') + ';background:' + (active?'var(--cyan-d)':'transparent') + ';color:' + (active?'var(--cyan)':'var(--muted)') + '">' + p + '</button>';
      }
      pgHtml += '<span style="font-size:11px;color:var(--muted);margin-left:8px">' + total + ' videos</span>';
      pgDiv.innerHTML = pgHtml;
    } else { pgDiv.innerHTML = total ? '<span style="font-size:11px;color:var(--muted)">' + total + ' videos</span>' : ''; }
    // Stats footer
    try {
      const sr = await fetch('/api/vault/stats');
      const ss = (await sr.json()).data || {};
      document.getElementById('vault-footer').textContent = (ss.total_videos||0) + ' videos · ' + (ss.total_images||0) + ' images · $' + (ss.total_spent||0).toFixed(2) + ' spent · ' + ((ss.total_storage_bytes||0)/1024/1024/1024).toFixed(1) + ' GB';
    } catch(e) {}
  } catch(e) { document.getElementById('vault-grid').innerHTML = '<div style="color:var(--red);grid-column:1/-1">Failed to load vault</div>'; }
}

async function queueVideo(videoId) {
  try {
    const r = await fetch('/api/vault/' + videoId + '/queue', {method:'POST'});
    if (r.ok) { showToast('Added to queue'); loadVault(); }
    else showToast('Failed to queue', 'error');
  } catch(e) { showToast('Error: ' + e.message, 'error'); }
}

async function showQA(videoId) {
  const el = document.getElementById('qa-detail-' + videoId);
  if (!el) return;
  if (el.style.display !== 'none') { el.style.display = 'none'; return; }
  el.style.display = 'block';
  el.innerHTML = '<span style="color:var(--muted)">Loading...</span>';
  try {
    const r = await fetch('/api/vault/' + videoId + '/qa');
    const d = await r.json();
    if (!d.success || !d.data) { el.innerHTML = '<span style="color:var(--muted)">No QA data</span>'; return; }
    const qa = d.data;
    let html = '<div style="margin-bottom:4px;font-weight:600;color:var(--cyan)">QA Score: ' + (qa.overall_score||0).toFixed(1) + '/10</div>';
    if (qa.issues && qa.issues.length) {
      html += '<div style="color:var(--red);margin-bottom:4px"><b>Issues:</b></div>';
      qa.issues.forEach(iss => { html += '<div style="color:#ff9999;padding-left:8px">• ' + iss + '</div>'; });
    }
    if (qa.suggestions && qa.suggestions.length) {
      html += '<div style="color:var(--yellow);margin-top:4px"><b>Suggestions:</b></div>';
      qa.suggestions.forEach(s => { html += '<div style="color:#ffe099;padding-left:8px">• ' + s + '</div>'; });
    }
    if (qa.frame_scores && qa.frame_scores.length) {
      html += '<div style="margin-top:6px;color:var(--dim)"><b>Frames:</b></div>';
      qa.frame_scores.forEach(fs => {
        html += '<div style="padding-left:8px;color:var(--dim)">F' + fs.frame + ': visual=' + (fs.visual||'?') + ' text=' + (fs.text_readable||'?') + (fs.notes ? ' — <i>' + fs.notes.substring(0,80) + '...</i>' : '') + '</div>';
      });
    }
    el.innerHTML = html;
  } catch(e) { el.innerHTML = '<span style="color:var(--red)">Error loading QA</span>'; }
}

// ─── Settings ───
async function loadSettings() {
  try {
    const r = await fetch('/api/settings');
    const s = await r.json();
    // Just show key status
  } catch(e) {}
  try {
    const r = await fetch('/api/billing');
    const b = (await r.json()).data || {};
    const tm = b.this_month || {};
    const lm = b.last_month || {};
    document.getElementById('billing-info').innerHTML =
      '<div style="margin-bottom:8px"><strong>This Month:</strong> $' + (tm.total||0).toFixed(2) + '</div>' +
      '<div style="margin-bottom:4px;font-size:11px">Scripts: $' + (tm.breakdown?.scripts||0).toFixed(3) + ' · Images: $' + (tm.breakdown?.images||0).toFixed(3) + ' · Voice: $' + (tm.breakdown?.voice||0).toFixed(3) + ' · QA: $' + (tm.breakdown?.qa||0).toFixed(3) + '</div>' +
      '<div><strong>Last Month:</strong> $' + (lm.total||0).toFixed(2) + '</div>';
  } catch(e) {}
}

async function saveKeys() {
  const data = {};
  const a = document.getElementById('key-anthropic').value.trim(); if(a) data.anthropic = a;
  const f = document.getElementById('key-fal').value.trim(); if(f) data.fal = f;
  const e = document.getElementById('key-eleven').value.trim(); if(e) data.elevenlabs = e;
  const d = document.getElementById('key-did').value.trim(); if(d) data.did = d;
  const mir = document.getElementById('key-mirage').value.trim(); if(mir) data.mirage = mir;
  await fetch('/api/settings/keys', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)});
  showToast('Keys saved!');
}

// ─── Init ───
loadDashboard();
</script>
</html>"""


# ─── API ROUTES ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/apps", methods=["GET"])
def list_apps():
    apps_list = []
    for config_file in sorted(CONFIG_DIR.glob("*.json")):
        if config_file.name == "example_app.json":
            continue
        try:
            raw = config_file.read_text().strip()
            if not raw or not raw.startswith("{"):
                logger.warning(f"Skipping corrupted config: {config_file}")
                continue
            config = json.loads(raw)
            slug = config_file.stem
            video_count = 0
            app_output = OUTPUT_DIR / slug
            if app_output.exists():
                video_count = len(list(app_output.rglob("*.mp4")))
            config["slug"] = slug
            config["video_count"] = video_count
            apps_list.append(config)
        except Exception as e:
            logger.error(f"Error loading {config_file}: {e}")
    return jsonify(apps_list)


@app.route("/api/apps", methods=["POST"])
def create_app():
    """Start async config generation — accepts FormData with optional file uploads."""
    import base64 as b64mod

    name = request.form.get("name", "").strip()
    if not name:
        return jsonify({"error": "App name is required"}), 400

    # Process uploaded files into folder_context
    folder_context = None
    uploaded_files = request.files.getlist("files")
    if uploaded_files:
        documents = []
        images = []
        for f in uploaded_files:
            fname = f.filename or ""
            ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            if ext in ("txt", "md", "json", "csv", "html", "rtf"):
                try:
                    content = f.read().decode("utf-8", errors="ignore")[:2000]
                    documents.append({"filename": fname.split("/")[-1], "content": content})
                except Exception as e:
                    logger.debug(f"Could not read file {fname}: {e}")
            elif ext in ("docx",):
                try:
                    from zipfile import ZipFile
                    import re as _re
                    data = f.read()
                    with ZipFile(io.BytesIO(data)) as zf:
                        xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
                    text = _re.sub(r"<[^>]+>", " ", xml)
                    text = " ".join(text.split())[:3000]
                    documents.append({"filename": fname.split("/")[-1], "content": text})
                except Exception as e:
                    logger.warning(f"Could not read docx {fname}: {e}")
            elif ext in ("pdf",):
                try:
                    data = f.read()
                    # Basic PDF text extraction
                    text = data.decode("latin-1", errors="ignore")
                    # Extract text between stream markers (rough but works for simple PDFs)
                    import re as _re
                    chunks = _re.findall(r"\((.*?)\)", text)
                    extracted = " ".join(chunks)[:2000]
                    if len(extracted) > 50:
                        documents.append({"filename": fname.split("/")[-1], "content": extracted})
                except Exception as e:
                    logger.debug(f"Could not extract text from PDF {fname}: {e}")
            elif ext in ("png", "jpg", "jpeg", "gif", "webp"):
                try:
                    data = f.read()
                    if len(data) < 5 * 1024 * 1024:  # Skip files over 5MB
                        images.append({"filename": fname.split("/")[-1], "base64": b64mod.b64encode(data).decode("utf-8")})
                except Exception as e:
                    logger.debug(f"Could not read image {fname}: {e}")
        if documents or images:
            folder_context = {"documents": documents[:5], "images": images[:3]}

    job_id = f"config_{name.lower().replace(' ','_')}_{int(time.time())}"
    jobs[job_id] = {"status": "running", "message": "Generating strategy with AI..."}

    def _generate(jid, app_name, ctx):
        try:
            from config_generator import generate_app_config, save_app_config
            jobs[jid]["message"] = "Creating personas, hashtags, content strategy..."

            # Auto-generate description from context
            desc = f"{app_name} app"
            if ctx and ctx.get("documents"):
                # Use first doc content as context hint
                first_doc = ctx["documents"][0]["content"][:200]
                desc = f"{app_name} — {first_doc[:100]}"

            config = generate_app_config(app_name, desc, folder_context=ctx)
            path = save_app_config(config, str(CONFIG_DIR))
            jobs[jid] = {"status": "done", "message": "App created!", "config": config, "config_path": path}
        except Exception as e:
            error_msg = str(e)
            if "credit balance" in error_msg.lower() or "billing" in error_msg.lower():
                error_msg = "Anthropic API has no credits. Add credits at console.anthropic.com"
            elif "authentication" in error_msg.lower() or "api key" in error_msg.lower():
                error_msg = "Anthropic API key is invalid or missing. Check Settings."
            elif "overloaded" in error_msg.lower():
                error_msg = "Anthropic API is overloaded. Try again in a minute."
            jobs[jid] = {"status": "error", "message": error_msg}

    thread = threading.Thread(target=_generate, args=(job_id, name, folder_context), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/api/config-status/<job_id>", methods=["GET"])
def config_status(job_id):
    """Poll config generation progress."""
    if job_id not in jobs:
        return jsonify({"status": "not_found"}), 404
    return jsonify(jobs[job_id])


@app.route("/api/apps/<slug>", methods=["DELETE"])
def delete_app(slug):
    config_path = CONFIG_DIR / f"{slug}.json"
    if config_path.exists():
        os.remove(config_path)
    return jsonify({"status": "deleted"})


@app.route("/api/apps/<slug>/screenshots", methods=["POST"])
def upload_screenshots(slug):
    """Upload app screenshots for use in video generation."""
    screenshots_dir = OUTPUT_DIR / slug / "app_screenshots"
    screenshots_dir.mkdir(parents=True, exist_ok=True)

    uploaded = request.files.getlist("screenshots[]") or request.files.getlist("screenshots")
    saved = []
    for f in uploaded:
        fname = (f.filename or "screenshot.png").split("/")[-1].replace(" ", "_")
        save_path = screenshots_dir / fname
        f.save(str(save_path))
        saved.append(fname)

    return jsonify({"saved": saved, "total": len(list(screenshots_dir.glob("*")))})


@app.route("/api/apps/<slug>/screenshots", methods=["GET"])
def list_screenshots(slug):
    """List available app screenshots."""
    screenshots_dir = OUTPUT_DIR / slug / "app_screenshots"
    if not screenshots_dir.exists():
        return jsonify({"screenshots": []})
    files = [f.name for f in screenshots_dir.iterdir()
             if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    return jsonify({"screenshots": sorted(files)})


@app.route("/api/apps/<slug>/config", methods=["GET"])
def get_app_config(slug):
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404
    with open(config_path) as f:
        config = json.load(f)
    config["slug"] = slug
    return jsonify(config)


@app.route("/api/apps/<slug>/config", methods=["PUT"])
def update_app_config(slug):
    """Update specific fields in an app's config."""
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404
    with open(config_path) as f:
        config = json.load(f)

    data = request.json or {}

    # Editable fields
    editable = [
        "tiktok_handle", "tiktok_account_id", "videos_per_day",
        "image_engine", "voice_engine", "qa_threshold",
        "app_store_url", "play_store_url", "link_in_bio_url",
        "tiktok",
    ]
    updated = []
    for field in editable:
        if field in data:
            val = data[field]
            # Type coercions
            if field == "videos_per_day":
                val = int(val) if val else 7
            elif field == "qa_threshold":
                val = float(val) if val else 7.0
            config[field] = val
            updated.append(field)

    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    # Also sync to database
    try:
        save_app_db({
            "id": slug,
            "name": config.get("app_name", slug),
            "description": config.get("app_description", ""),
            "tiktok_handle": config.get("tiktok_handle", ""),
            "tiktok_account_id": config.get("tiktok_account_id", ""),
            "app_store_url": config.get("app_store_url", ""),
            "play_store_url": config.get("play_store_url", ""),
            "link_in_bio_url": config.get("link_in_bio_url", ""),
            "ica": config.get("ica", {}),
            "content_pillars": config.get("content_pillars", []),
            "cta_variations": config.get("cta_variations", []),
            "hashtags": config.get("hashtag_sets", config.get("hashtags", {})),
            "personas": config.get("personas", []),
            "videos_per_day": config.get("videos_per_day", 1),
            "image_engine": config.get("image_engine", "flux_2_pro"),
            "voice_engine": config.get("voice_engine", "elevenlabs"),
            "status": "active",
        })
    except Exception as e:
        logger.warning(f"Failed to sync app to database: {e}")

    return jsonify({"status": "saved", "updated": updated})


@app.route("/api/generate/<slug>", methods=["POST"])
def start_generation(slug):
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404
    # Use per-app videos_per_day if set, else request body, else default 7
    with open(config_path) as f:
        app_cfg = json.load(f)
    data = request.json or {}
    count = data.get("count", app_cfg.get("videos_per_day", 7))
    target_duration = data.get("target_duration", "medium")
    template_id = data.get("template_id")  # NEW
    creative_id = data.get("creative_id")
    # If creative_id provided, load from DB and convert to template format for backwards compat
    if creative_id and not template_id:
        creative = get_creative(creative_id)
        if creative:
            # Store creative_id in job for tracking
            pass  # The _run_generation already handles template_id
            template_id = creative_id  # Use creative ID as template ID (they share the same storage after migration)
    job_id = f"{slug}_{int(time.time())}"
    jobs[job_id] = {
        "status": "running",
        "message": "Starting generation...",
        "videos_created": 0,
        "completed": 0,
        "total": count,
        "total_count": count,
        "pipeline_stages": {"script": [], "images": [], "voice": [], "edit": [], "qa": [], "ready": []},
        "agents": {
            "script_writer": {"status": "idle", "progress": f"0/{count}", "current_task": "", "log": []},
            "image_artist": {"status": "idle", "progress": "0/0", "current_task": "", "log": []},
            "voice_actor": {"status": "idle", "progress": "0/0", "current_task": "", "log": []},
            "video_editor": {"status": "idle", "progress": f"0/{count}", "current_task": "", "log": []},
            "qa_reviewer": {"status": "idle", "progress": f"0/{count}", "current_task": "", "log": []},
            "publisher": {"status": "idle", "progress": f"0/{count}", "current_task": "", "log": []},
        },
        "template_id": template_id or "random",
        "creative_id": creative_id or template_id,
        "target_duration": target_duration,
        "video_engine": data.get("video_engine", "standard"),
        "remotion_template": data.get("remotion_template", "stock-narration"),
        "captions_ai_avatar": data.get("captions_ai_avatar", "Kate"),
        "post_type": data.get("post_type", "link_in_bio"),
        "content_mix": data.get("content_mix", ["link_in_bio"]),
        "training_mode": data.get("training_mode", False),
        "training_target": float(data.get("training_target", 7.5)),
        "script_preview": data.get("script_preview", False),
        "errors": [],
        "activity_log": [],
        "cost": {"scripts": 0, "images": 0, "voiceovers": 0, "qa": 0, "total": 0},
        "_timestamp": time.time(),
        "_script_approval_event": threading.Event(),
    }
    _save_jobs()
    jobs[job_id]["app_id"] = slug
    jobs[job_id]["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    save_job_to_db(job_id, jobs[job_id])
    # Log to generation_logs
    conn = get_db()
    conn.execute("INSERT INTO generation_logs (job_id, app_id, creative_id, video_count, status, started_at) VALUES (?,?,?,?,?,?)",
        (job_id, slug, creative_id or template_id, count, "running", time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    conn.close()
    thread = threading.Thread(target=_run_generation, args=(str(config_path), count, job_id, creative_id or template_id), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/api/job/<job_id>", methods=["GET"])
def get_job_status(job_id):
    if job_id in jobs:
        # Filter out non-serializable internal keys
        _skip = {"_script_approval_event", "_approved_scripts", "_reject_feedback", "_current_playbook", "_regenerate_scripts"}
        safe = {k: v for k, v in jobs[job_id].items() if k not in _skip}
        return jsonify({"success": True, "data": safe})
    # Try DB
    db_job = load_job_from_db(job_id)
    if db_job:
        return jsonify({"success": True, "data": db_job})
    return jsonify({"success": False, "status": "not_found"}), 404

@app.route("/api/job/<job_id>/approve-scripts", methods=["POST"])
def approve_scripts(job_id):
    """Approve (or skip) scripts and resume video generation."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    if jobs[job_id].get("status") != "awaiting_script_approval":
        return jsonify({"error": "Job is not awaiting script approval"}), 400
    data = request.json or {}
    # If user sent edited scripts, store them for the pipeline to pick up
    if data.get("scripts"):
        jobs[job_id]["_approved_scripts"] = data["scripts"]
    # Signal the waiting thread to continue
    evt = jobs[job_id].get("_script_approval_event")
    if evt:
        evt.set()
    return jsonify({"success": True, "message": "Scripts approved, generating videos..."})

@app.route("/api/job/<job_id>/reject-scripts", methods=["POST"])
def reject_scripts(job_id):
    """Reject scripts, update agent identity with feedback, auto-regenerate."""
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
    if jobs[job_id].get("status") != "awaiting_script_approval":
        return jsonify({"error": "Job is not awaiting script approval"}), 400
    data = request.json or {}
    feedback = data.get("feedback", "")
    creative_id = jobs[job_id].get("creative_id")
    # Update agent identity with feedback
    if feedback and creative_id:
        jobs[job_id]["message"] = "Updating agent prompt with your feedback..."
        update_agent_identity(creative_id, feedback, source="user")
    # Signal thread to REGENERATE (not continue)
    jobs[job_id]["_regenerate_scripts"] = True
    jobs[job_id]["_reject_feedback"] = feedback
    evt = jobs[job_id].get("_script_approval_event")
    if evt:
        evt.set()
    return jsonify({"success": True, "message": "Regenerating scripts with feedback..."})

@app.route("/api/jobs/active", methods=["GET"])
def api_active_jobs():
    active = get_active_jobs()
    return jsonify({"success": True, "data": active})

@app.route("/api/jobs/history", methods=["GET"])
def api_job_history():
    limit = request.args.get("limit", 20, type=int)
    history = get_job_history(limit)
    return jsonify({"success": True, "data": history})


@app.route("/api/videos", methods=["GET"])
def list_videos():
    videos = []
    for app_dir in OUTPUT_DIR.iterdir():
        if not app_dir.is_dir() or app_dir.name in ("reference_images", "upload_queue"):
            continue
        for mp4 in app_dir.rglob("*.mp4"):
            rel_path = mp4.relative_to(OUTPUT_DIR)
            videos.append({
                "filename": mp4.name,
                "title": mp4.stem.replace("_", " ").title(),
                "app": app_dir.name.replace("_", " ").title(),
                "app_slug": app_dir.name,
                "path": str(mp4),
                "url": f"/api/video-file/{rel_path}",
                "created": mp4.stat().st_mtime,
                "file_size": mp4.stat().st_size,
                "file_size_mb": round(mp4.stat().st_size / (1024*1024), 1),
            })
    videos.sort(key=lambda v: v["created"], reverse=True)
    return jsonify(videos[:50])



@app.route("/api/videos/<path:filename>", methods=["DELETE"])
def delete_video(filename):
    """Delete a video file and free storage."""
    for mp4 in OUTPUT_DIR.rglob("*.mp4"):
        if mp4.name == filename:
            size = mp4.stat().st_size
            mp4.unlink()
            # Also delete thumbnail if exists
            thumb = mp4.with_suffix('.jpg')
            if thumb.exists():
                thumb.unlink()
            return jsonify({"status": "deleted", "freed_bytes": size, "freed_mb": round(size / (1024*1024), 1)})
    return jsonify({"error": "Video not found"}), 404


@app.route("/api/videos/bulk-delete", methods=["POST"])
def bulk_delete_videos():
    """Delete multiple videos."""
    data = request.json or {}
    filenames = data.get("filenames", [])
    total_freed = 0
    deleted = 0
    for fname in filenames:
        for mp4 in OUTPUT_DIR.rglob("*.mp4"):
            if mp4.name == fname:
                total_freed += mp4.stat().st_size
                mp4.unlink()
                thumb = mp4.with_suffix('.jpg')
                if thumb.exists():
                    thumb.unlink()
                deleted += 1
                break
    return jsonify({"deleted": deleted, "freed_bytes": total_freed, "freed_mb": round(total_freed / (1024*1024), 1)})


@app.route("/api/storage", methods=["GET"])
def get_storage():
    """Get storage usage for video output directory."""
    total_size = 0
    video_count = 0
    for mp4 in OUTPUT_DIR.rglob("*.mp4"):
        total_size += mp4.stat().st_size
        video_count += 1
    import shutil
    disk = shutil.disk_usage(str(DATA_DIR))
    return jsonify({
        "videos_size_bytes": total_size,
        "videos_size_gb": round(total_size / (1024**3), 2),
        "video_count": video_count,
        "disk_total_gb": round(disk.total / (1024**3), 1),
        "disk_used_gb": round(disk.used / (1024**3), 1),
        "disk_free_gb": round(disk.free / (1024**3), 1),
        "disk_pct": round(disk.used / disk.total * 100, 1),
    })


@app.route("/api/video-file/<path:filepath>")
def serve_video(filepath):
    """Serve a video file from the output directory."""
    from flask import send_from_directory
    full_path = OUTPUT_DIR / filepath
    if not full_path.exists() or not str(full_path.resolve()).startswith(str(OUTPUT_DIR.resolve())):
        return "Not found", 404
    return send_from_directory(str(full_path.parent), full_path.name, mimetype="video/mp4")


@app.route("/api/thumbnail/<path:filepath>")
def serve_thumbnail(filepath):
    """Serve a thumbnail image from the output directory."""
    from flask import send_from_directory
    full_path = OUTPUT_DIR / filepath
    if not full_path.exists() or not str(full_path.resolve()).startswith(str(OUTPUT_DIR.resolve())):
        return "", 404
    return send_from_directory(str(full_path.parent), full_path.name, mimetype="image/jpeg")


@app.route("/api/upload/<slug>", methods=["POST"])
def upload_video_to_tiktok(slug):
    """Upload a specific video to TikTok via Upload-Post.com."""
    import requests

    # Try per-app token first, fall back to global
    config_path = CONFIG_DIR / f"{slug}.json"
    per_app_token = None
    if config_path.exists():
        with open(config_path) as _f:
            _cfg = json.load(_f)
        per_app_token = (_cfg.get("tiktok", {}).get("upload_post_token") or "").strip()
    api_key = per_app_token or os.environ.get("UPLOADPOST_API_KEY")
    if not api_key:
        return jsonify({"error": "Upload-Post API key not configured. Set it in Settings."}), 400

    data = request.json or {}
    video_filename = data.get("filename")
    if not video_filename:
        return jsonify({"error": "Video filename required"}), 400

    # Find the video file
    video_path = OUTPUT_DIR / slug / video_filename
    if not video_path.exists():
        # Also check subdirectories (e.g., YYYY-MM-DD structure)
        for candidate in (OUTPUT_DIR / slug).rglob("*.mp4"):
            if candidate.name == video_filename:
                video_path = candidate
                break
        else:
            return jsonify({"error": f"Video {video_filename} not found"}), 404

    if not video_path.stat().st_size > 0:
        return jsonify({"error": "Video file is empty"}), 400

    try:
        # Post to Upload-Post.com
        with open(video_path, "rb") as f:
            files = {"video": f}
            data_fields = {
                "description": data.get("description", "") + AIGC_DESCRIPTION_SUFFIX,
                "ai_generated": "true",
            }
            headers = {"Authorization": f"Bearer {api_key}"}
            response = requests.post(
                "https://app.upload-post.com/api/upload",
                files=files,
                data=data_fields,
                headers=headers,
                timeout=300,
            )

        if response.status_code not in (200, 201):
            return jsonify({
                "error": f"Upload-Post API returned {response.status_code}",
                "details": response.text,
            }), 400

        result = response.json()
        return jsonify({
            "status": "success",
            "message": "Video uploaded to TikTok",
            "upload_post_response": result,
        })

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Upload failed: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Error: {str(e)}"}), 500


@app.route("/api/auto-upload/<slug>", methods=["POST"])
def auto_upload_all_videos(slug):
    """Upload all ready videos for an app to TikTok."""
    import requests

    # Try per-app token first, fall back to global
    config_path = CONFIG_DIR / f"{slug}.json"
    per_app_token = None
    if config_path.exists():
        with open(config_path) as _f:
            _cfg = json.load(_f)
        per_app_token = (_cfg.get("tiktok", {}).get("upload_post_token") or "").strip()
    api_key = per_app_token or os.environ.get("UPLOADPOST_API_KEY")
    if not api_key:
        return jsonify({"error": "Upload-Post API key not configured"}), 400

    # Find all videos for this app
    app_output = OUTPUT_DIR / slug
    if not app_output.exists():
        return jsonify({"error": f"App {slug} not found"}), 404

    videos = list(app_output.rglob("*.mp4"))
    if not videos:
        return jsonify({"message": "No videos found for this app", "uploaded": []}), 200

    uploaded = []
    failed = []

    for video_path in videos:
        try:
            with open(video_path, "rb") as f:
                files = {"video": f}
                headers = {"Authorization": f"Bearer {api_key}"}
                response = requests.post(
                    "https://app.upload-post.com/api/upload",
                    files=files,
                    headers=headers,
                    timeout=300,
                )

            if response.status_code in (200, 201):
                uploaded.append(video_path.name)
            else:
                failed.append({
                    "filename": video_path.name,
                    "error": f"Status {response.status_code}",
                })
        except Exception as e:
            failed.append({"filename": video_path.name, "error": str(e)})

    return jsonify({
        "status": "complete",
        "uploaded": uploaded,
        "failed": failed,
    })


# ─── CREATIVES API (Agency System) ────────────────────────────────────────────

@app.route("/api/creatives", methods=["GET"])
def api_list_creatives():
    try:
        creatives = get_all_creatives()
        return jsonify({"success": True, "data": creatives})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/creatives", methods=["POST"])
def api_create_creative():
    from datetime import datetime
    import uuid
    data = request.json or {}
    name = data.get("name", "New Creative")
    specialty = data.get("specialty", "")
    clone_from = data.get("clone_from")

    cid = name.lower().replace(" ", "_").replace("-", "_")
    cid = "".join(c for c in cid if c.isalnum() or c == "_")[:30]
    cid = cid + "_" + str(uuid.uuid4())[:4]

    if clone_from:
        source = get_creative(clone_from)
        if not source:
            return jsonify({"success": False, "error": "Source creative not found"}), 404
        creative = dict(source)
        creative["id"] = cid
        creative["name"] = name or (source["name"] + " (Copy)")
        creative["cloned_from"] = clone_from
        creative["status"] = "active"
    else:
        creative = {
            "id": cid,
            "name": name,
            "specialty": specialty,
            "avatar_color": "#00e5ff",
            "writing_config": {
                "identity": "", "psychology": "", "methodology": "", "structure": "",
                "tone": "casual, conversational", "vocabulary": "", "banned_words": "revolutionize, streamline, leverage, optimize",
                "hook_bank": [], "examples": "", "content_angle": "",
                "model": "claude-sonnet-4-20250514", "energy": "medium", "slide_count": 3,
                "persona_name": "", "persona_archetype": "", "persona_description": "", "persona_image_prefix": "",
            },
            "visual_config": {"style_suffix": "shot on iPhone, natural lighting, candid, 9:16 portrait", "avoid_in_images": "", "use_screenshots_for_demos": True},
            "voice_config": {"elevenlabs_voice_id": "", "voice_name": "", "speaking_speed": 1.0, "stability": 0.3, "style": 0.45, "similarity": 0.8},
            "pacing_config": {"ken_burns_zoom": 1.05, "ken_burns_pan": 15, "crossfade_duration": 0.3, "music_volume": 0.12, "subtitle_font_size": 56, "highlight_color": "#FFD700", "qa_threshold": 7.0, "auto_regenerate": False, "max_retries": 3},
            "color_grade": {"warmth": 1.08, "contrast": 1.05, "saturation": 0.95, "vignette_strength": 0.15},
            "status": "active",
        }

    save_creative(creative)
    return jsonify({"success": True, "data": get_creative(cid)})

@app.route("/api/creatives/<creative_id>", methods=["GET"])
def api_get_creative(creative_id):
    c = get_creative(creative_id)
    if not c:
        return jsonify({"success": False, "error": "Creative not found"}), 404
    return jsonify({"success": True, "data": c})

@app.route("/api/creatives/<creative_id>", methods=["PUT"])
def api_update_creative(creative_id):
    existing = get_creative(creative_id)
    if not existing:
        return jsonify({"success": False, "error": "Creative not found"}), 404
    data = request.json or {}
    # Merge JSON blob fields instead of replacing entirely
    for key in ["writing_config", "visual_config", "voice_config", "pacing_config", "color_grade"]:
        if key in data and isinstance(data[key], dict) and isinstance(existing.get(key), dict):
            merged = dict(existing[key])
            merged.update(data[key])
            data[key] = merged
    # Update simple fields
    for key in ["name", "specialty", "avatar_color", "status", "playbook"]:
        if key in data:
            existing[key] = data[key]
    for key in ["writing_config", "visual_config", "voice_config", "pacing_config", "color_grade"]:
        if key in data:
            existing[key] = data[key]
    save_creative(existing)
    return jsonify({"success": True, "data": get_creative(creative_id)})

@app.route("/api/creatives/<creative_id>", methods=["DELETE"])
def api_delete_creative(creative_id):
    existing = get_creative(creative_id)
    if not existing:
        return jsonify({"success": False, "error": "Creative not found"}), 404
    conn = get_db()
    conn.execute("UPDATE creatives SET status='archived', updated_at=? WHERE id=?", (time.strftime("%Y-%m-%dT%H:%M:%S"), creative_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/creatives/<creative_id>/clone", methods=["POST"])
def api_clone_creative(creative_id):
    source = get_creative(creative_id)
    if not source:
        return jsonify({"success": False, "error": "Creative not found"}), 404
    import uuid
    new_id = creative_id + "_copy_" + str(uuid.uuid4())[:4]
    clone = dict(source)
    clone["id"] = new_id
    clone["name"] = source["name"] + " (Copy)"
    clone["cloned_from"] = creative_id
    clone["status"] = "active"
    save_creative(clone)
    return jsonify({"success": True, "data": get_creative(new_id)})

# ─── DASHBOARD STATS (Real Data) ─────────────────────────────────────────────

@app.route("/api/dashboard/stats", methods=["GET"])
def api_dashboard_stats():
    try:
        stats = get_dashboard_stats()
        stats["videos_delta"] = stats["videos_this_week"] - stats["videos_last_week"]
        stats["avg_qa_delta"] = round(stats["avg_qa_score"] - stats["avg_qa_last_week"], 1)
        stats["errors_today"] = stats.pop("error_count_today")
        return jsonify({"success": True, "data": stats})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/dashboard/recent-videos", methods=["GET"])
def api_recent_videos():
    try:
        videos = get_vault_videos(limit=10)
        # Enrich with app and creative names
        for v in videos:
            if v.get("app_slug"):
                app_data = get_app_db(v["app_slug"])
                v["app_name"] = app_data["name"] if app_data else v["app_slug"]
            if v.get("creative_id"):
                c = get_creative(v["creative_id"])
                v["creative_name"] = c["name"] if c else v["creative_id"]
        return jsonify({"success": True, "data": videos})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/dashboard/activity", methods=["GET"])
def api_dashboard_activity():
    try:
        logs = query_db("SELECT * FROM generation_logs ORDER BY started_at DESC LIMIT 20")
        activity = []
        for log in logs:
            app_name = ""
            creative_name = ""
            if log.get("app_id"):
                a = get_app_db(log["app_id"])
                app_name = a["name"] if a else log["app_id"]
            if log.get("creative_id"):
                c = get_creative(log["creative_id"])
                creative_name = c["name"] if c else log["creative_id"]
            activity.append({
                "timestamp": log.get("started_at") or log.get("completed_at"),
                "type": "error" if log.get("status") == "error" else "generation",
                "message": log.get("error_message") or f"Generated {log.get('video_count', 0)} videos",
                "app_name": app_name,
                "creative_name": creative_name,
                "status": log.get("status"),
            })
        return jsonify({"success": True, "data": activity})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/dashboard/weekly-chart", methods=["GET"])
def api_weekly_chart():
    from datetime import datetime, timedelta
    try:
        days = []
        day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        now = datetime.now()
        for i in range(6, -1, -1):
            d = now - timedelta(days=i)
            date_str = d.strftime("%Y-%m-%d")
            row = query_db("SELECT COUNT(*) as cnt, COALESCE(AVG(qa_score),0) as avg_qa FROM videos WHERE created_at LIKE ?", (date_str + "%",), one=True) or {}
            days.append({"day": day_names[d.weekday()], "count": row.get("cnt", 0), "avg_qa": round(row.get("avg_qa", 0), 1)})
        return jsonify({"success": True, "data": days})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ─── VAULT (Filtered Video Query) ────────────────────────────────────────────

@app.route("/api/vault", methods=["GET"])
def api_vault():
    try:
        app_id = request.args.get("app_id")
        creative_id = request.args.get("creative_id")
        min_qa = request.args.get("min_qa", type=float)
        search = request.args.get("search")
        status = request.args.get("status")  # "ready", "failed", or None for all
        post_type = request.args.get("post_type")
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 24, type=int)
        offset = (page - 1) * per_page

        items = get_vault_videos(app_id=app_id, creative_id=creative_id, min_qa=min_qa, search=search, status=status, post_type=post_type, limit=per_page, offset=offset)

        # Enrich with names
        for v in items:
            if v.get("app_slug"):
                a = get_app_db(v["app_slug"])
                v["app_name"] = a["name"] if a else v["app_slug"]
            if v.get("creative_id"):
                c = get_creative(v["creative_id"])
                v["creative_name"] = c["name"] if c else ""
            # Add URL for video playback
            if v.get("file_path"):
                try:
                    fp = Path(v["file_path"]).resolve()
                    rel = fp.relative_to(OUTPUT_DIR.resolve())
                    v["url"] = f"/api/video-file/{rel}"
                except (ValueError, Exception):
                    # Path might be relative already — try stripping known prefixes
                    fp_str = v["file_path"]
                    for prefix in [str(OUTPUT_DIR) + "/", "data/output/", str(OUTPUT_DIR.resolve()) + "/"]:
                        if fp_str.startswith(prefix):
                            v["url"] = "/api/video-file/" + fp_str[len(prefix):]
                            break
                    else:
                        v["url"] = ""

        # Total count for pagination
        total_row = query_db("SELECT COUNT(*) as cnt FROM videos", one=True) or {}
        total = total_row.get("cnt", 0)

        return jsonify({"success": True, "data": {"items": items, "total": total, "page": page, "pages": max(1, (total + per_page - 1) // per_page)}})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/vault/<video_id>/review", methods=["POST"])
def api_review_video(video_id):
    """User approves or rejects a video with feedback. Updates agent playbook on reject."""
    try:
        data = request.json or {}
        approved = data.get("approved", True)
        feedback = data.get("feedback", "")
        creative_id = data.get("creative_id", "")

        # Save feedback to video
        conn = get_db()
        conn.execute("UPDATE videos SET user_approved=?, user_feedback=? WHERE id=?",
                     (1 if approved else 0, feedback, video_id))

        # If not in data, try to get creative_id from the video record
        if not creative_id:
            row = conn.execute("SELECT creative_id FROM videos WHERE id=?", (video_id,)).fetchone()
            creative_id = row[0] if row else ""

        conn.commit()
        conn.close()

        # Update agent identity with user feedback
        if feedback and creative_id:
            try:
                source = "user"
                prefix = "User APPROVED and said: " if approved else "User REJECTED and said: "
                new_identity = update_agent_identity(creative_id, prefix + feedback, source=source)
                if new_identity:
                    return jsonify({"success": True, "prompt_updated": True})
            except Exception as e:
                logger.error(f"Identity update error: {e}")
                return jsonify({"success": True, "prompt_updated": False, "error": str(e)})

        return jsonify({"success": True, "playbook_updated": False})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/vault/<video_id>/qa", methods=["GET"])
def api_video_qa(video_id):
    """Return QA review details for a video."""
    try:
        row = query_db("SELECT qa_details FROM videos WHERE id=?", (video_id,), one=True)
        if not row or not row.get("qa_details"):
            return jsonify({"success": False, "error": "No QA data"})
        qa = json.loads(row["qa_details"]) if isinstance(row["qa_details"], str) else row["qa_details"]
        return jsonify({"success": True, "data": qa})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/vault/<video_id>", methods=["DELETE"])
def api_delete_video(video_id):
    """Delete a video from the database and optionally from disk."""
    try:
        conn = get_db()
        row = conn.execute("SELECT file_path FROM videos WHERE id=?", (video_id,)).fetchone()
        conn.execute("DELETE FROM videos WHERE id=?", (video_id,))
        conn.commit()
        conn.close()
        # Try to delete file from disk too
        if row and row[0]:
            try:
                os.remove(row[0])
            except OSError:
                pass
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/vault/<video_id>/queue", methods=["POST"])
def api_queue_video(video_id):
    """Add a video to the upload queue."""
    try:
        conn = get_db()
        conn.execute("UPDATE videos SET upload_status='queued' WHERE id=?", (video_id,))
        conn.commit()

        # Also copy the file to the upload_queue directory
        row = conn.execute("SELECT file_path FROM videos WHERE id=?", (video_id,)).fetchone()
        conn.close()
        if row and row[0] and os.path.exists(row[0]):
            queue_dir = OUTPUT_DIR / "upload_queue"
            queue_dir.mkdir(parents=True, exist_ok=True)
            dest = queue_dir / Path(row[0]).name
            if not dest.exists():
                import shutil
                shutil.copy2(row[0], str(dest))

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/vault/stats", methods=["GET"])
def api_vault_stats():
    try:
        conn = get_db()
        videos = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        spent = conn.execute("SELECT COALESCE(SUM(cost_total),0) FROM videos").fetchone()[0]
        # Count images in image library
        images = 0
        lib_dir = DATA_DIR / "image_library"
        if lib_dir.exists():
            for d in lib_dir.iterdir():
                if d.is_dir():
                    images += len(list(d.glob("*.jpg"))) + len(list(d.glob("*.png")))
        # Storage
        storage = 0
        for mp4 in OUTPUT_DIR.rglob("*.mp4"):
            storage += mp4.stat().st_size
        conn.close()
        return jsonify({"success": True, "data": {"total_videos": videos, "total_images": images, "total_spent": round(spent, 2), "total_storage_bytes": storage}})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ─── BILLING ──────────────────────────────────────────────────────────────────

@app.route("/api/billing", methods=["GET"])
def api_billing():
    from datetime import datetime
    try:
        now = datetime.now()
        this_month = now.strftime("%Y-%m")
        last_month_dt = now.replace(day=1) - __import__("datetime").timedelta(days=1)
        last_month = last_month_dt.strftime("%Y-%m")

        def month_breakdown(month_prefix):
            rows = query_db("SELECT cost_breakdown FROM videos WHERE created_at LIKE ? AND cost_breakdown IS NOT NULL", (month_prefix + "%",))
            total = 0; scripts = 0; images = 0; voice = 0; qa = 0
            for r in rows:
                try:
                    cb = json.loads(r["cost_breakdown"]) if isinstance(r["cost_breakdown"], str) else r["cost_breakdown"]
                    if cb:
                        scripts += cb.get("scripts", 0)
                        images += cb.get("images", 0)
                        voice += cb.get("voiceovers", cb.get("voice", 0))
                        qa += cb.get("qa", 0)
                except Exception as e:
                    logger.debug(f"Could not parse cost breakdown: {e}")
            total_row = query_db("SELECT COALESCE(SUM(cost_total),0) as t FROM videos WHERE created_at LIKE ?", (month_prefix + "%",), one=True)
            total = (total_row or {}).get("t", 0)
            return {"total": round(total, 2), "breakdown": {"scripts": round(scripts, 3), "images": round(images, 3), "voice": round(voice, 3), "qa": round(qa, 3)}}

        return jsonify({"success": True, "data": {"this_month": month_breakdown(this_month), "last_month": month_breakdown(last_month)}})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ─── UPLOADED MEDIA ───────────────────────────────────────────────────────────

@app.route("/api/apps/<app_id>/media", methods=["POST"])
def api_upload_media(app_id):
    import uuid as _uuid
    f = request.files.get("file")
    if not f:
        return jsonify({"success": False, "error": "No file uploaded"}), 400

    media_dir = OUTPUT_DIR / app_id / "media"
    media_dir.mkdir(parents=True, exist_ok=True)

    mid = str(_uuid.uuid4())[:8]
    orig = (f.filename or "upload").split("/")[-1].replace(" ", "_")
    filename = f"{mid}_{orig}"
    save_path = media_dir / filename
    f.save(str(save_path))

    mime = f.content_type or ""
    media_type = "video_footage" if "video" in mime else "screenshot"

    conn = get_db()
    conn.execute("INSERT INTO uploaded_media (id, app_id, file_path, media_type, original_filename, file_size_bytes, created_at) VALUES (?,?,?,?,?,?,?)",
        (mid, app_id, str(save_path), media_type, orig, save_path.stat().st_size, time.strftime("%Y-%m-%dT%H:%M:%S")))
    conn.commit()
    conn.close()

    return jsonify({"success": True, "data": {"id": mid, "file_path": str(save_path), "media_type": media_type, "original_filename": orig}})

@app.route("/api/apps/<app_id>/media", methods=["GET"])
def api_list_media(app_id):
    try:
        media = query_db("SELECT * FROM uploaded_media WHERE app_id=? ORDER BY created_at DESC", (app_id,))
        return jsonify({"success": True, "data": media})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route("/api/apps/<app_id>/media/<media_id>", methods=["PUT"])
def api_update_media(app_id, media_id):
    """Update media description/label."""
    data = request.json or {}
    conn = get_db()
    conn.execute("UPDATE uploaded_media SET description=? WHERE id=? AND app_id=?",
                 (data.get("description", ""), media_id, app_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})


@app.route("/api/apps/<app_id>/media/<media_id>/auto-label", methods=["POST"])
def api_auto_label_media(app_id, media_id):
    """Use Claude Vision to automatically describe a screenshot."""
    try:
        import anthropic, base64
        conn = get_db()
        row = conn.execute("SELECT file_path, media_type FROM uploaded_media WHERE id=? AND app_id=?", (media_id, app_id)).fetchone()
        conn.close()
        if not row:
            return jsonify({"success": False, "error": "Media not found"}), 404

        file_path = row[0]
        media_type = row[1]

        if media_type == "video_footage":
            # For video, extract a frame first
            try:
                from moviepy import VideoFileClip
                clip = VideoFileClip(file_path)
                frame = clip.get_frame(min(2.0, clip.duration / 2))
                clip.close()
                from PIL import Image as _PILImg
                import io
                img = _PILImg.fromarray(frame)
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=80)
                img_data = buf.getvalue()
            except Exception as e:
                return jsonify({"success": False, "error": f"Could not extract frame: {e}"}), 500
        else:
            with open(file_path, "rb") as f:
                img_data = f.read()

        img_b64 = base64.b64encode(img_data).decode("utf-8")
        suffix = "jpeg" if media_type == "video_footage" else file_path.split(".")[-1].lower()
        if suffix == "png":
            media_t = "image/png"
        else:
            media_t = "image/jpeg"

        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=100,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_t, "data": img_b64}},
                    {"type": "text", "text": "Describe this app screenshot in 8-15 words. Focus on what screen/feature is shown and what the user can do. Be specific. Example: 'Weekly meal planner with breakfast, lunch, dinner slots for each day'. Just the description, nothing else."}
                ]
            }]
        )
        description = response.content[0].text.strip().strip('"').strip("'")

        # Save to DB
        conn = get_db()
        conn.execute("UPDATE uploaded_media SET description=? WHERE id=? AND app_id=?", (description, media_id, app_id))
        conn.commit()
        conn.close()

        return jsonify({"success": True, "description": description})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/apps/<app_id>/media/<media_id>", methods=["DELETE"])
def api_delete_media(app_id, media_id):
    row = query_db("SELECT * FROM uploaded_media WHERE id=? AND app_id=?", (media_id, app_id), one=True)
    if not row:
        return jsonify({"success": False, "error": "Media not found"}), 404
    # Delete file
    fpath = Path(row["file_path"])
    if fpath.exists():
        os.remove(str(fpath))
    conn = get_db()
    conn.execute("DELETE FROM uploaded_media WHERE id=?", (media_id,))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/api/media-file/<path:filepath>")
def serve_media_file(filepath):
    from flask import send_from_directory
    full_path = OUTPUT_DIR / filepath
    if not full_path.exists():
        return "Not found", 404
    return send_from_directory(str(full_path.parent), full_path.name)

# ─── TEMPLATES (Legacy — backwards compat) ───────────────────────────────────

TEMPLATES_DIR = DATA_DIR / "templates"
TEMPLATES_DIR.mkdir(exist_ok=True)
PERSONA_PHOTOS_DIR = DATA_DIR / "persona_photos"


@app.route("/api/templates", methods=["GET"])
def list_templates():
    templates = []
    for f in sorted(TEMPLATES_DIR.glob("*.json")):
        try:
            with open(f) as fh:
                templates.append(json.load(fh))
        except Exception:
            pass
    return jsonify(templates)


@app.route("/api/templates/generate-with-ai", methods=["POST"])
def generate_template_with_ai():
    """AI Template Wizard — generate a complete template from a short brief."""
    import anthropic
    data = request.json or {}
    style = data.get("style", "before_after")
    custom_desc = data.get("custom_style_description", "")
    persona_desc = data.get("persona_description", "")
    energy = data.get("energy", "medium")
    app_slug = data.get("app_slug", "")
    app_context = data.get("app_context", "")
    references = data.get("references", "")
    duration_pref = data.get("duration_preference", "medium")

    # Load app info if specified
    app_info = ""
    if app_slug:
        cfg_path = CONFIG_DIR / f"{app_slug}.json"
        if cfg_path.exists():
            with open(cfg_path) as f:
                acfg = json.load(f)
            app_info = f"\nTARGET APP:\n- Name: {acfg.get('app_name','')}\n- Description: {acfg.get('app_description','')}\n- Features: {', '.join(acfg.get('content_pillars',[]))}\n- Context: {app_context}"

    dur_map = {"short": "10-15 seconds", "medium": "15-25 seconds", "long": "25-35 seconds"}
    style_map = {
        "before_after": "Before & After transformation — show pain then solution",
        "storytelling": "POV storytelling — immersive narrative arc",
        "shock_value": "Shock value — bold claim, fast cuts, maximum attention",
        "quick_demo": "Quick demo — hook, show the app, CTA",
        "listicle": "Listicle — numbered tips or reasons",
        "custom": custom_desc or "custom style",
    }

    prompt = f"""You are an expert TikTok content strategist. Generate a COMPLETE creative template as JSON.

BRIEF:
- Style: {style_map.get(style, style)}
- Persona: {persona_desc or 'create one that fits the style'}
- Energy: {energy}
- Duration: {dur_map.get(duration_pref, '15-25 seconds')}
- References: {references or 'none'}
{app_info}

Return ONLY valid JSON with ALL these fields (no markdown, no explanation):
{{
  "name": "creative name, max 40 chars",
  "description": "1-2 sentences about this template",
  "tags": ["3-5 tags"],
  "slide_count": 3,
  "energy": "{energy}",
  "structure_prompt": "detailed instructions for each slide — emotions, pacing, what viewer should think",
  "persona_name": "realistic full name",
  "persona_age": "age",
  "persona_role": "short archetype like 'The Reluctant Fitness Bro'",
  "persona_description": "2-3 sentences — physical appearance, clothing, vibe",
  "persona_image_prefix": "Portrait photo of a [age] [ethnicity] [gender] with [hair], [eyes], natural skin",
  "script_model": "claude-sonnet-4-20250514",
  "script_system_prompt": "3-5 paragraph system prompt for the script writer — comprehensive",
  "script_writer_identity": "1-2 paragraphs about WHO this writer is",
  "script_psychology": "specific psychological principles used",
  "script_methodology": "framework: PAS, AIDA, hook-value-CTA, etc.",
  "script_tone": "short tone description",
  "script_vocabulary": "catchphrases, slang this persona uses",
  "script_banned_words": "corporate/cringe words to avoid",
  "script_content_angle": "strategic content approach",
  "script_hook_bank": ["5-8 diverse hook templates"],
  "script_examples": "1-2 complete example scripts",
  "voice_speed": 1.0,
  "voice_stability": 0.4,
  "voice_similarity": 0.75,
  "voice_style": 0.3,
  "music_mood": "auto",
  "music_volume": 0.12,
  "image_style_suffix": "photography style suffix",
  "image_negative_prompt": "what to avoid",
  "use_screenshots_for_demo": true,
  "caption_font_size": 72,
  "caption_position": "lower-third",
  "caption_highlight": true,
  "caption_highlight_color": "#FFD700",
  "caption_bg_pill": true,
  "caption_bg_opacity": 0.6,
  "ken_burns_intensity": 0.05,
  "transition_style": "crossfade",
  "transition_duration": 0.3,
  "vignette_strength": 0.3,
  "qa_threshold": 7.0,
  "qa_auto_regenerate": true,
  "qa_max_attempts": 3
}}

RULES:
- script_system_prompt must be substantial (3-5 paragraphs)
- script_hook_bank needs 5-8 DIVERSE hooks
- script_examples should be 1-2 COMPLETE mini scripts
- All numbers must match the energy (high = faster speed, hard cuts, higher Ken Burns)
- Persona should feel like a real person
- Return ONLY the JSON object, nothing else"""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        # Parse JSON
        import re
        # Strip markdown if present
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            text = match.group(0)
        template_data = json.loads(text)
        # Add defaults for missing fields
        template_data.setdefault("duration_range", [10, template_data.get("slide_count", 3) * 5])
        template_data.setdefault("talking_heads_enabled", False)
        return jsonify({"success": True, "template": template_data})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)[:200]})


@app.route("/api/templates", methods=["POST"])
def create_template():
    data = request.json or {}
    tid = data.get("id") or data.get("name", "custom").lower().replace(" ", "_")
    tid = "".join(c for c in tid if c.isalnum() or c == "_")
    data["id"] = tid
    data.setdefault("times_used", 0)
    data.setdefault("avg_qa_score", 0)
    path = TEMPLATES_DIR / f"{tid}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return jsonify({"status": "saved", "id": tid})


@app.route("/api/templates/<template_id>", methods=["PUT"])
def update_template(template_id):
    tpl_path = TEMPLATES_DIR / f"{template_id}.json"
    if not tpl_path.exists():
        return jsonify({"error": "Template not found"}), 404
    with open(tpl_path) as f:
        tpl = json.load(f)
    data = request.json or {}
    # Update all fields from request
    for key in data:
        tpl[key] = data[key]
    with open(tpl_path, "w") as f:
        json.dump(tpl, f, indent=2)
    return jsonify({"status": "saved"})


@app.route("/api/templates/<template_id>", methods=["DELETE"])
def delete_template(template_id):
    tpl_path = TEMPLATES_DIR / f"{template_id}.json"
    if tpl_path.exists():
        os.remove(str(tpl_path))
    return jsonify({"status": "deleted"})


@app.route("/api/templates/<template_id>/duplicate", methods=["POST"])
def duplicate_template(template_id):
    tpl_path = TEMPLATES_DIR / f"{template_id}.json"
    if not tpl_path.exists():
        return jsonify({"error": "Template not found"}), 404
    with open(tpl_path) as f:
        tpl = json.load(f)
    import uuid
    new_id = template_id + "_copy_" + str(uuid.uuid4())[:4]
    tpl["id"] = new_id
    tpl["name"] = tpl["name"] + " (Copy)"
    tpl["times_used"] = 0
    new_path = TEMPLATES_DIR / f"{new_id}.json"
    with open(new_path, "w") as f:
        json.dump(tpl, f, indent=2)
    return jsonify({"status": "duplicated", "id": new_id})


@app.route("/api/templates/<template_id>/persona-photo/upload", methods=["POST"])
def upload_persona_photo(template_id):
    """Upload a reference photo for a template's persona."""
    import uuid
    photo_dir = PERSONA_PHOTOS_DIR / template_id
    photo_dir.mkdir(parents=True, exist_ok=True)
    f = request.files.get("photo")
    if not f:
        return jsonify({"error": "No photo"}), 400
    photo_id = str(uuid.uuid4())[:8]
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in (f.filename or "") else "png"
    filename = f"{photo_id}.{ext}"
    save_path = photo_dir / filename
    f.save(str(save_path))
    # Add to template
    tpl_path = TEMPLATES_DIR / f"{template_id}.json"
    if tpl_path.exists():
        with open(tpl_path) as fh:
            tpl = json.load(fh)
        photos = tpl.get("persona_photos", [])
        is_first = len(photos) == 0
        photos.append({
            "id": photo_id,
            "filename": filename,
            "url": f"/api/persona-photos/{template_id}/{filename}",
            "label": f"Photo {len(photos)+1}",
            "is_primary": is_first,
            "source": "uploaded",
        })
        tpl["persona_photos"] = photos
        if is_first:
            tpl["persona_primary_photo"] = photo_id
        with open(tpl_path, "w") as fh:
            json.dump(tpl, fh, indent=2)
    return jsonify({"id": photo_id, "url": f"/api/persona-photos/{template_id}/{filename}"})

@app.route("/api/templates/<template_id>/persona-photo/generate", methods=["POST"])
def generate_persona_photos(template_id):
    """Generate 4 AI reference photos for the persona."""
    tpl_path = TEMPLATES_DIR / f"{template_id}.json"
    if not tpl_path.exists():
        return jsonify({"error": "Template not found"}), 404
    with open(tpl_path) as f:
        tpl = json.load(f)
    prefix = tpl.get("persona", {}).get("image_prompt_prefix", "")
    if not prefix:
        prefix = tpl.get("persona_image_prefix", "portrait photo of a person")
    # Generate 4 variations
    import requests as _req
    key = os.environ.get("FAL_KEY")
    if not key:
        return jsonify({"error": "FAL_KEY not set"}), 400
    variants = [
        f"{prefix}, front-facing portrait, neutral expression, studio lighting, 9:16",
        f"{prefix}, slight smile, natural lighting, looking at camera, 9:16",
        f"{prefix}, three-quarter view, soft lighting, 9:16",
        f"{prefix}, candid portrait, warm lighting, 9:16",
    ]
    results = []
    for prompt in variants:
        try:
            resp = _req.post(
                "https://fal.run/fal-ai/flux-pro/v1.1-ultra",
                headers={"Authorization": f"Key {key}", "Content-Type": "application/json"},
                json={"prompt": prompt, "aspect_ratio": "9:16", "safety_tolerance": "5", "output_format": "jpeg", "num_images": 1},
                timeout=180,
            )
            data = resp.json()
            if "images" in data and data["images"]:
                results.append({"url": data["images"][0]["url"], "prompt": prompt})
        except Exception as e:
            results.append({"url": None, "error": str(e)[:100]})
    return jsonify({"options": results})

@app.route("/api/templates/<template_id>/persona-photo/save-generated", methods=["POST"])
def save_generated_persona_photo(template_id):
    """Save a generated photo to the persona gallery."""
    import uuid, requests as _req
    data = request.json or {}
    image_url = data.get("url")
    if not image_url:
        return jsonify({"error": "No URL"}), 400
    photo_dir = PERSONA_PHOTOS_DIR / template_id
    photo_dir.mkdir(parents=True, exist_ok=True)
    photo_id = str(uuid.uuid4())[:8]
    filename = f"{photo_id}.jpg"
    # Download
    resp = _req.get(image_url, timeout=60)
    with open(photo_dir / filename, "wb") as f:
        f.write(resp.content)
    # Add to template
    tpl_path = TEMPLATES_DIR / f"{template_id}.json"
    if tpl_path.exists():
        with open(tpl_path) as fh:
            tpl = json.load(fh)
        photos = tpl.get("persona_photos", [])
        is_first = len(photos) == 0
        photos.append({
            "id": photo_id,
            "filename": filename,
            "url": f"/api/persona-photos/{template_id}/{filename}",
            "label": f"Generated {len(photos)+1}",
            "is_primary": is_first,
            "source": "generated",
        })
        tpl["persona_photos"] = photos
        if is_first:
            tpl["persona_primary_photo"] = photo_id
        with open(tpl_path, "w") as fh:
            json.dump(tpl, fh, indent=2)
    return jsonify({"id": photo_id, "url": f"/api/persona-photos/{template_id}/{filename}"})

@app.route("/api/templates/<template_id>/persona-photo/<photo_id>/primary", methods=["PUT"])
def set_primary_persona_photo(template_id, photo_id):
    tpl_path = TEMPLATES_DIR / f"{template_id}.json"
    if not tpl_path.exists():
        return jsonify({"error": "Not found"}), 404
    with open(tpl_path) as f:
        tpl = json.load(f)
    for p in tpl.get("persona_photos", []):
        p["is_primary"] = (p["id"] == photo_id)
    tpl["persona_primary_photo"] = photo_id
    with open(tpl_path, "w") as f:
        json.dump(tpl, f, indent=2)
    return jsonify({"status": "ok"})

@app.route("/api/templates/<template_id>/persona-photo/<photo_id>", methods=["DELETE"])
def delete_persona_photo(template_id, photo_id):
    tpl_path = TEMPLATES_DIR / f"{template_id}.json"
    if not tpl_path.exists():
        return jsonify({"error": "Not found"}), 404
    with open(tpl_path) as f:
        tpl = json.load(f)
    photos = tpl.get("persona_photos", [])
    photo = next((p for p in photos if p["id"] == photo_id), None)
    if photo:
        # Delete file
        fpath = PERSONA_PHOTOS_DIR / template_id / photo["filename"]
        if fpath.exists():
            os.remove(str(fpath))
        photos = [p for p in photos if p["id"] != photo_id]
        tpl["persona_photos"] = photos
        with open(tpl_path, "w") as f:
            json.dump(tpl, f, indent=2)
    return jsonify({"status": "deleted"})

@app.route("/api/persona-photos/<template_id>/<filename>")
def serve_persona_photo(template_id, filename):
    from flask import send_from_directory
    photo_dir = PERSONA_PHOTOS_DIR / template_id
    return send_from_directory(str(photo_dir), filename)


@app.route("/api/talking-head/test-d-id", methods=["POST"])
def test_d_id():
    data = request.json or {}
    api_key = data.get("api_key", "")
    if not api_key:
        return jsonify({"valid": False, "error": "No API key provided"})
    from src.talking_head_generator import DIDProvider
    provider = DIDProvider(api_key)
    result = provider.test_connection()
    return jsonify(result)

@app.route("/api/talking-head/providers", methods=["GET"])
def talking_head_providers():
    return jsonify({
        "d-id": {"available": True, "needs_key": True, "key_configured": bool(os.environ.get("DID_API_KEY"))},
        "kling": {"available": True, "needs_key": False, "note": "Uses fal.ai API key"},
    })

@app.route("/api/system", methods=["GET"])
def system_status():
    import shutil
    disk = shutil.disk_usage(str(DATA_DIR))
    return jsonify({
        "disk_used_gb": round(disk.used / (1024**3), 1),
        "disk_free_gb": round(disk.free / (1024**3), 1),
        "disk_pct": round(disk.used / disk.total * 100, 1),
        "api_keys": {
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "fal": bool(os.environ.get("FAL_KEY")),
            "elevenlabs": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "uploadpost": bool(os.environ.get("UPLOADPOST_API_KEY")),
        },
        "active_jobs": sum(1 for j in jobs.values() if j.get("status") == "running"),
        "total_videos": sum(1 for _ in OUTPUT_DIR.rglob("*.mp4")) if OUTPUT_DIR.exists() else 0,
    })


@app.route("/api/performance", methods=["GET"])
def get_performance():
    """Aggregate performance stats from generated videos."""
    video_count = 0
    total_cost = 0.0
    for app_dir in OUTPUT_DIR.iterdir():
        if app_dir.is_dir() and app_dir.name not in ("reference_images", "upload_queue"):
            video_count += len(list(app_dir.rglob("*.mp4")))
    avg_cost = 0.28  # estimated per-video cost with premium APIs
    return jsonify({
        "total_views": 0,
        "engagement_rate": 0,
        "videos_made": video_count,
        "total_spent": round(video_count * avg_cost, 2),
        "cost_per_view": 0,
        "top_videos": [],
        "weekly_views": [0, 0, 0, 0, 0, 0, 0],
    })


@app.route("/api/analytics/<slug>", methods=["GET"])
def get_analytics(slug):
    """Get TikTok analytics for an app."""
    # Check if TikTok access token exists
    token = os.environ.get("TIKTOK_ACCESS_TOKEN")
    if not token:
        return jsonify({"connected": False, "message": "Connect TikTok account in Settings"})
    # Placeholder — would call TikTok API here
    return jsonify({"connected": True, "views": 0, "likes": 0, "comments": 0, "shares": 0, "engagement_rate": 0, "top_videos": []})


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify({
        "videos_per_day": int(os.environ.get("VIDEOS_PER_DAY", 7)),
        "image_engine": os.environ.get("IMAGE_ENGINE", "flux_2_pro"),
        "voice_engine": os.environ.get("VOICE_ENGINE", "elevenlabs"),
        "qa_threshold": float(os.environ.get("QA_THRESHOLD", 7.0)),
        "keys": {
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "fal": bool(os.environ.get("FAL_KEY")),
            "elevenlabs": bool(os.environ.get("ELEVENLABS_API_KEY")),
            "uploadpost": bool(os.environ.get("UPLOADPOST_API_KEY")),
            "did": bool(os.environ.get("DID_API_KEY")),
        },
        "deploy_hook": bool(os.environ.get("RAILWAY_DEPLOY_HOOK")),
    })


@app.route("/api/settings/keys", methods=["POST"])
def save_keys():
    """Save API keys — writes to .env and sets in current process."""
    data = request.json or {}
    env_path = BASE_DIR / ".env"

    # Read existing .env
    existing = {}
    if env_path.exists():
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    existing[k.strip()] = v.strip()

    # Update with new keys (only if non-empty)
    key_map = {
        "anthropic": "ANTHROPIC_API_KEY",
        "fal": "FAL_KEY",
        "elevenlabs": "ELEVENLABS_API_KEY",
        "uploadpost": "UPLOADPOST_API_KEY",
        "did": "DID_API_KEY",
        "mirage": "MIRAGE_API_KEY",
        "tiktok_access_token": "TIKTOK_ACCESS_TOKEN",
        "qa_threshold": "QA_THRESHOLD",
    }
    updated = []
    for field, env_var in key_map.items():
        val = data.get(field, "").strip()
        if val:
            existing[env_var] = val
            os.environ[env_var] = val
            updated.append(field)

    # Write .env back (to both locations for persistence)
    for path in [env_path, PERSISTENT_ENV]:
        with open(path, "w") as f:
            for k, v in existing.items():
                f.write(f"{k}={v}\n")

    return jsonify({"status": "saved", "updated": updated})


@app.route("/api/deploy", methods=["POST"])
def trigger_deploy():
    """Download latest code from GitHub (as zip) and restart. No git required."""
    import urllib.request
    import zipfile
    import io
    import shutil

    zip_url = "https://github.com/quinten-dotcom/tiktok-content-factory/archive/refs/heads/master.zip"
    # Directories to preserve (user data, configs, generated content)
    PRESERVE = {".env", "config", "output", "_update_temp", "__pycache__", ".git", "nixpacks.toml"}

    try:
        # Download the repo as a zip
        response = urllib.request.urlopen(zip_url, timeout=60)
        zip_data = response.read()

        # Extract to temp directory
        temp_dir = BASE_DIR / "_update_temp"
        if temp_dir.exists():
            shutil.rmtree(str(temp_dir))

        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            zf.extractall(str(temp_dir))

        # Find the extracted top-level directory (e.g. "tiktok-content-factory-master/")
        extracted_dirs = [d for d in temp_dir.iterdir() if d.is_dir()]
        if not extracted_dirs:
            shutil.rmtree(str(temp_dir))
            return jsonify({"status": "error", "message": "Downloaded zip was empty."})

        extracted = extracted_dirs[0]

        # Copy files over, preserving user data
        updated_files = []
        for item in extracted.iterdir():
            if item.name in PRESERVE:
                continue
            dest = BASE_DIR / item.name
            try:
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(str(dest))
                    shutil.copytree(str(item), str(dest))
                else:
                    shutil.copy2(str(item), str(dest))
                updated_files.append(item.name)
            except Exception as e:
                logger.warning(f"Could not update {item.name}: {e}")

        # Cleanup temp
        shutil.rmtree(str(temp_dir))

        if not updated_files:
            return jsonify({"status": "ok", "message": "Already on latest version."})

        # Restart by exiting — Railway will auto-restart the process
        def delayed_restart():
            time.sleep(2)
            os._exit(0)

        threading.Thread(target=delayed_restart, daemon=True).start()
        return jsonify({"status": "ok", "message": f"Updated {len(updated_files)} files! Restarting in 2 seconds."})

    except Exception as e:
        # Cleanup on error
        temp_dir = BASE_DIR / "_update_temp"
        if temp_dir.exists():
            import shutil
            shutil.rmtree(str(temp_dir))
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/deploy-upload", methods=["POST"])
def deploy_upload():
    """Accept a zip file upload and deploy it directly. Skips GitHub entirely."""
    import zipfile
    import io
    import shutil

    if "file" not in request.files:
        return jsonify({"status": "error", "message": "No file uploaded"}), 400

    uploaded = request.files["file"]
    if not uploaded.filename.endswith(".zip"):
        return jsonify({"status": "error", "message": "Must be a .zip file"}), 400

    PRESERVE = {".env", "config", "output", "_update_temp", "__pycache__", ".git", "nixpacks.toml"}

    try:
        zip_data = uploaded.read()
        temp_dir = BASE_DIR / "_update_temp"
        if temp_dir.exists():
            shutil.rmtree(str(temp_dir))

        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            zf.extractall(str(temp_dir))

        # Find top-level: could be flat or nested in one folder
        items = list(temp_dir.iterdir())
        if len(items) == 1 and items[0].is_dir():
            source = items[0]
        else:
            source = temp_dir

        updated_files = []
        for item in source.iterdir():
            if item.name in PRESERVE:
                continue
            dest = BASE_DIR / item.name
            try:
                if item.is_dir():
                    if dest.exists():
                        shutil.rmtree(str(dest))
                    shutil.copytree(str(item), str(dest))
                else:
                    shutil.copy2(str(item), str(dest))
                updated_files.append(item.name)
            except Exception as e:
                logger.warning(f"Could not update {item.name}: {e}")

        shutil.rmtree(str(temp_dir))

        if not updated_files:
            return jsonify({"status": "ok", "message": "No new files to update."})

        def delayed_restart():
            time.sleep(2)
            os._exit(0)

        threading.Thread(target=delayed_restart, daemon=True).start()
        return jsonify({"status": "ok", "message": f"Updated {len(updated_files)} files! Restarting in 2 seconds."})

    except Exception as e:
        temp_dir = BASE_DIR / "_update_temp"
        if temp_dir.exists():
            shutil.rmtree(str(temp_dir))
        return jsonify({"status": "error", "message": str(e)})


@app.route("/api/upload-queue", methods=["GET"])
def get_upload_queue():
    return jsonify(_load_upload_queue())

@app.route("/api/upload-queue/<entry_id>/cancel", methods=["POST"])
def cancel_upload(entry_id):
    queue = _load_upload_queue()
    queue = [e for e in queue if not (e["id"] == entry_id and e["status"] == "queued")]
    _save_upload_queue(queue)
    return jsonify({"status": "cancelled"})

@app.route("/api/upload-queue/<entry_id>/reschedule", methods=["POST"])
def reschedule_upload(entry_id):
    data = request.json or {}
    new_time = data.get("scheduled_time")
    queue = _load_upload_queue()
    for e in queue:
        if e["id"] == entry_id:
            e["scheduled_time"] = new_time or _calculate_next_upload_time(e["app_slug"])
            break
    _save_upload_queue(queue)
    return jsonify({"status": "rescheduled"})

@app.route("/api/upload-queue/pause-all", methods=["POST"])
def pause_uploads():
    """Pause all queued uploads by setting status to 'paused'."""
    queue = _load_upload_queue()
    paused = 0
    for e in queue:
        if e["status"] == "queued":
            e["status"] = "paused"
            paused += 1
    _save_upload_queue(queue)
    return jsonify({"paused": paused})

@app.route("/api/upload-queue/resume-all", methods=["POST"])
def resume_uploads():
    """Resume all paused uploads."""
    queue = _load_upload_queue()
    resumed = 0
    for e in queue:
        if e["status"] == "paused":
            e["status"] = "queued"
            resumed += 1
    _save_upload_queue(queue)
    return jsonify({"resumed": resumed})

@app.route("/api/captions-ai/creators", methods=["GET"])
def api_captions_ai_creators():
    """List available Captions.ai Creator avatars."""
    try:
        from captions_ai import list_creators
        result = list_creators()
        creators = [{"name": name, "thumbnail": result.get("thumbnails", {}).get(name, {}).get("imageUrl", "")}
                     for name in result.get("supportedCreators", [])]
        return jsonify({"success": True, "data": creators})
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "data": []}), 200


@app.route("/api/image-library/stats", methods=["GET"])
def image_library_stats():
    """Get image recycling stats."""
    if not IMAGE_LIBRARY_DIR.exists():
        return jsonify({"total_images": 0, "storage_mb": 0, "savings": 0})
    total = 0
    size = 0
    for app_dir in IMAGE_LIBRARY_DIR.iterdir():
        if app_dir.is_dir():
            for img in app_dir.glob("*.png"):
                total += 1
                size += img.stat().st_size
            for img in app_dir.glob("*.jpg"):
                total += 1
                size += img.stat().st_size
    return jsonify({
        "total_images": total,
        "storage_mb": round(size / (1024*1024), 1),
        "estimated_savings": round(total * 0.045, 2),
    })

@app.route("/api/accounts/health", methods=["GET"])
def accounts_health():
    from datetime import datetime
    health = []
    for config_file in sorted(CONFIG_DIR.glob("*.json")):
        if config_file.name == "example_app.json":
            continue
        try:
            with open(config_file) as f:
                cfg = json.load(f)
            slug = config_file.stem
            posts_today = _get_posts_today(slug)
            last_post = _get_last_post_time(slug)
            risk = "low"
            if posts_today >= MAX_POSTS_PER_ACCOUNT_PER_DAY:
                risk = "high"
            elif posts_today >= 3:
                risk = "medium"
            health.append({
                "slug": slug,
                "name": cfg.get("app_name", slug),
                "handle": cfg.get("tiktok_handle", ""),
                "posts_today": posts_today,
                "max_posts": MAX_POSTS_PER_ACCOUNT_PER_DAY,
                "last_post": last_post,
                "risk": risk,
            })
        except Exception as e:
            logger.debug(f"Could not check health for {config_file}: {e}")
    return jsonify(health)

@app.route("/api/tuning/<slug>", methods=["GET"])
def get_tuning(slug):
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404
    with open(config_path) as f:
        config = json.load(f)
    # Return tuning + text_style + color_grade (all tunable settings)
    return jsonify({
        "tuning": config.get("tuning", {}),
        "text_style": config.get("text_style", {}),
        "color_grade": config.get("color_grade", {}),
    })

@app.route("/api/tuning/<slug>", methods=["PUT"])
def save_tuning(slug):
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404
    with open(config_path) as f:
        config = json.load(f)

    # Save version snapshot before overwriting
    from datetime import datetime
    history_dir = DATA_DIR / "tuning_history" / slug
    history_dir.mkdir(parents=True, exist_ok=True)
    version_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot = {
        "version_id": version_id,
        "timestamp": datetime.now().isoformat(),
        "tuning": config.get("tuning", {}),
        "text_style": config.get("text_style", {}),
        "color_grade": config.get("color_grade", {}),
    }
    with open(history_dir / f"{version_id}.json", "w") as f:
        json.dump(snapshot, f, indent=2)

    # Now apply the new settings
    data = request.json or {}
    if "tuning" in data:
        config["tuning"] = data["tuning"]
    if "text_style" in data:
        config["text_style"] = data["text_style"]
    if "color_grade" in data:
        config["color_grade"] = data["color_grade"]
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    return jsonify({"status": "saved", "version_id": version_id})

@app.route("/api/tuning/<slug>/history", methods=["GET"])
def get_tuning_history(slug):
    history_dir = DATA_DIR / "tuning_history" / slug
    if not history_dir.exists():
        return jsonify([])
    versions = []
    for f in sorted(history_dir.glob("*.json"), reverse=True):
        try:
            with open(f) as fh:
                v = json.load(fh)
            versions.append({"version_id": v["version_id"], "timestamp": v["timestamp"]})
        except Exception as e:
            logger.debug(f"Could not load tuning history {f}: {e}")
    return jsonify(versions[:20])  # Last 20 versions

@app.route("/api/tuning/<slug>/restore/<version_id>", methods=["POST"])
def restore_tuning(slug, version_id):
    history_path = DATA_DIR / "tuning_history" / slug / f"{version_id}.json"
    if not history_path.exists():
        return jsonify({"error": "Version not found"}), 404
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404
    with open(history_path) as f:
        snapshot = json.load(f)
    with open(config_path) as f:
        config = json.load(f)
    config["tuning"] = snapshot.get("tuning", {})
    config["text_style"] = snapshot.get("text_style", {})
    config["color_grade"] = snapshot.get("color_grade", {})
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    return jsonify({"status": "restored", "version_id": version_id})

@app.route("/api/personas/<slug>", methods=["GET"])
def get_personas(slug):
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404
    with open(config_path) as f:
        config = json.load(f)
    personas = config.get("personas", [])
    # Add reference image info
    ref_dir = OUTPUT_DIR / "reference_images"
    for p in personas:
        ref_path = ref_dir / f"{slug}_{p['id']}.png"
        p["has_reference_image"] = ref_path.exists()
        p["reference_image_url"] = f"/api/persona-image/{slug}/{p['id']}" if ref_path.exists() else None
    return jsonify(personas)

@app.route("/api/personas/<slug>/<persona_id>", methods=["PUT"])
def update_persona(slug, persona_id):
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404
    with open(config_path) as f:
        config = json.load(f)
    data = request.json or {}
    personas = config.get("personas", [])
    for i, p in enumerate(personas):
        if p["id"] == persona_id:
            # Update allowed fields
            for key in ["name", "archetype", "description", "image_prompt_prefix", "voice_config", "writing_style"]:
                if key in data:
                    personas[i][key] = data[key]
            break
    config["personas"] = personas
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
    return jsonify({"status": "saved"})

@app.route("/api/persona-image/<slug>/<persona_id>", methods=["GET"])
def get_persona_image(slug, persona_id):
    from flask import send_from_directory
    ref_dir = OUTPUT_DIR / "reference_images"
    ref_path = ref_dir / f"{slug}_{persona_id}.png"
    if not ref_path.exists():
        return "Not found", 404
    return send_from_directory(str(ref_dir), f"{slug}_{persona_id}.png", mimetype="image/png")

@app.route("/api/persona-image/<slug>/<persona_id>", methods=["POST"])
def upload_persona_image(slug, persona_id):
    ref_dir = OUTPUT_DIR / "reference_images"
    ref_dir.mkdir(parents=True, exist_ok=True)
    f = request.files.get("photo")
    if not f:
        return jsonify({"error": "No photo uploaded"}), 400
    save_path = ref_dir / f"{slug}_{persona_id}.png"
    f.save(str(save_path))
    return jsonify({"status": "saved", "url": f"/api/persona-image/{slug}/{persona_id}"})

@app.route("/api/tuning/ab-test", methods=["POST"])
def start_ab_test():
    """Start an A/B test — generate 1 video with config A and 1 with config B."""
    data = request.json or {}
    slug = data.get("app_slug")
    if not slug:
        return jsonify({"error": "app_slug required"}), 400
    # For now, just generate 2 videos — the frontend will compare QA scores
    # Config A = current saved config, Config B = the modified settings sent in the request
    job_id = f"ab_{slug}_{int(time.time())}"
    # Start generation of 2 videos
    config_path = str(CONFIG_DIR / f"{slug}.json")
    thread = threading.Thread(target=_run_generation, args=(config_path, 2, job_id), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id, "status": "started", "test_type": "ab"})


def _log_activity(job_id: str, agent: str, action: str):
    """Append to the job's activity log."""
    from datetime import datetime as _dt
    if job_id in jobs:
        jobs[job_id].setdefault("activity_log", []).append({
            "time": _dt.now().strftime("%H:%M:%S"),
            "agent": agent,
            "action": action,
        })


def _log_error(job_id: str, video: str, stage: str, agent: str, error: str):
    """Append to both the job's error list and activity log."""
    from datetime import datetime as _dt
    if job_id in jobs:
        jobs[job_id].setdefault("errors", []).append({
            "video": video, "stage": stage, "agent": agent,
            "error": error, "timestamp": _dt.now().isoformat(),
        })
        _log_activity(job_id, agent, f"ERROR on \"{video}\": {error[:120]}")


def _run_generation(config_path: str, count: int, job_id: str, template_id=None):
    """Background generation with pipeline stage tracking."""
    try:
        from script_generator import load_app_config, generate_scripts, save_scripts
        from image_generator import generate_images_for_script, generate_reference_image
        from voice_generator import generate_voiceover_for_script
        from subtitle_generator import (
            generate_word_timestamps, group_words_into_lines,
            calculate_slide_timings, save_subtitle_data,
        )
        from video_assembler import assemble_video, extract_key_frames, generate_thumbnail

        app_config = load_app_config(config_path)
        app_name = app_config["app_name"]
        app_slug = app_name.lower().replace(" ", "_")
        app_slug = "".join(c for c in app_slug if c.isalnum() or c == "_")

        today = datetime.now().strftime("%Y-%m-%d")
        base_dir = OUTPUT_DIR / app_slug / today
        videos_dir = base_dir / "videos"

        voice_engine = app_config.get("voice_engine", os.environ.get("VOICE_ENGINE", "elevenlabs"))
        image_engine = app_config.get("image_engine", os.environ.get("IMAGE_ENGINE", "flux_2_pro"))
        tuning = app_config.get("tuning", {})

        # Load creative from DB (primary) or template file (fallback)
        creative = {}
        template = {}
        # Pick a random agent if none specified
        if not template_id or template_id == "random":
            try:
                import random as _rng
                all_creatives = query_db("SELECT id, name FROM creatives WHERE status='active' OR status IS NULL")
                if all_creatives:
                    picked = _rng.choice(all_creatives)
                    template_id = picked["id"]
                    logger.info(f"[AGENT] Randomly picked: {picked['name']} ({template_id})")
            except Exception as e:
                logger.debug(f"Could not pick random agent: {e}")

        if template_id:
            # Try DB first (creative system)
            creative = get_creative(template_id) or {}
            if not creative:
                # Fallback to template file
                tpl_path = TEMPLATES_DIR / f"{template_id}.json"
                if tpl_path.exists():
                    with open(tpl_path) as f:
                        template = json.load(f)

        # Build settings from creative (DB) or template (file fallback)
        if creative:
            wc = creative.get("writing_config", {}) or {}
            vc = creative.get("voice_config", {}) or {}
            vis = creative.get("visual_config", {}) or {}
            pc = creative.get("pacing_config", {}) or {}
            cg = creative.get("color_grade", {}) or {}
            voice_config_override = vc
            script_config = wc
            image_config = vis
            caption_config = {"subtitle_font_size": pc.get("subtitle_font_size", 56), "highlight_color": pc.get("highlight_color", "#FFD700")}
            video_config = {"ken_burns_zoom": pc.get("ken_burns_zoom", 1.05), "ken_burns_pan": pc.get("ken_burns_pan", 15), "crossfade_duration": pc.get("crossfade_duration", 0.3), "music_volume": pc.get("music_volume", 0.12)}
            video_config.update(cg)
            qa_config = {"threshold": pc.get("qa_threshold", 7.0), "auto_regenerate": pc.get("auto_regenerate", False)}
            persona_config = {"name": wc.get("persona_name", ""), "archetype": wc.get("persona_archetype", ""), "image_prompt_prefix": wc.get("persona_image_prefix", ""), "description": wc.get("persona_description", "")}
            structure_prompt = wc.get("structure", "")
            _log_activity(job_id, "Pipeline", f"Using creative: {creative.get('name', template_id)}")
        else:
            voice_config_override = template.get("voice_config", {})
            script_config = template.get("script_config", {})
            image_config = template.get("image_config", {})
            caption_config = template.get("caption_config", {})
            video_config = template.get("video_config", {})
            qa_config = template.get("qa_config", {})
            persona_config = template.get("persona", {})
            structure_prompt = template.get("structure_prompt", "")

        stages = jobs[job_id]["pipeline_stages"]

        # Reference images
        jobs[job_id]["message"] = "Checking persona reference images..."
        ref_dir = OUTPUT_DIR / "reference_images"
        ref_dir.mkdir(parents=True, exist_ok=True)
        for persona in app_config.get("personas", []):
            ref_path = ref_dir / f"{app_slug}_{persona['id']}.png"
            if not ref_path.exists():
                jobs[job_id]["message"] = f"Generating reference for {persona['name']}..."
                generate_reference_image(persona, str(ref_path))

        # Discover available app screenshots (from both app_screenshots/ and uploaded media)
        screenshots_dir = OUTPUT_DIR / app_slug / "app_screenshots"
        available_screenshots = []
        if screenshots_dir.exists():
            available_screenshots = [f.stem for f in screenshots_dir.iterdir()
                                     if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
        # Also add uploaded screenshots from media/ dir (with descriptions for Claude)
        screenshot_descriptions = {}
        try:
            ss_rows = query_db("SELECT original_filename, file_path, description FROM uploaded_media WHERE app_id=? AND media_type='screenshot'", (app_slug,))
            for sr in ss_rows:
                name = sr.get("original_filename", Path(sr["file_path"]).name)
                desc = sr.get("description", "")
                if name not in available_screenshots:
                    available_screenshots.append(name)
                if desc:
                    screenshot_descriptions[name] = desc
        except Exception:
            pass
        # Also add descriptions for old app_screenshots by filename
        for ss in available_screenshots:
            if ss not in screenshot_descriptions:
                screenshot_descriptions[ss] = ss.replace("_", " ")  # e.g. "meal_plan" → "meal plan"

        # Discover available video footage from uploaded_media DB (with descriptions + durations)
        available_footage = []
        footage_descriptions = {}
        footage_durations = {}
        footage_files = []
        try:
            footage_rows = query_db("SELECT * FROM uploaded_media WHERE app_id=? AND media_type='video_footage'", (app_slug,))
            for fm in footage_rows:
                name = fm.get("original_filename", Path(fm["file_path"]).name)
                available_footage.append(name)
                footage_files.append(fm)
                if fm.get("description"):
                    footage_descriptions[name] = fm["description"]
                # Get actual video duration
                try:
                    from moviepy import VideoFileClip as _VFC_dur
                    _c = _VFC_dur(fm["file_path"])
                    footage_durations[name] = _c.duration
                    _c.close()
                except Exception:
                    pass
        except Exception:
            pass

        # Descriptions will be added to enriched_config below

        # Scripts
        jobs[job_id]["message"] = f"Writing {count} scripts..."
        jobs[job_id]["agents"]["script_writer"]["status"] = "working"
        jobs[job_id]["agents"]["script_writer"]["current_task"] = f"Writing {count} scripts with Claude Sonnet..."
        script_model = script_config.get("model", "claude-sonnet-4-20250514")
        # Inject creative data into app_config so all stages can access it
        enriched_config = dict(app_config)
        creative_writing = {
            "structure": structure_prompt,
            "identity": script_config.get("identity", script_config.get("writer_identity", "")),
            "psychology": script_config.get("psychology", ""),
            "methodology": script_config.get("methodology", ""),
            "tone": script_config.get("tone", ""),
            "vocabulary": script_config.get("vocabulary", ""),
            "banned_words": script_config.get("banned_words", ""),
            "content_angle": script_config.get("content_angle", ""),
            "hook_bank": script_config.get("hook_bank", []),
            "examples": script_config.get("examples", ""),
            "energy": script_config.get("energy", (creative or template).get("energy", "medium")),
            "model": script_config.get("model", "claude-sonnet-4-20250514"),
        }
        # Support both _creative and _template keys for backwards compat
        enriched_config["_creative"] = {"writing": creative_writing, "visual": image_config, "voice": voice_config_override, "pacing": video_config}
        enriched_config["_template"] = {
            "structure_prompt": structure_prompt,
            "writer_identity": creative_writing.get("identity", ""),
            "psychology": creative_writing.get("psychology", ""),
            "methodology": creative_writing.get("methodology", ""),
            "tone": creative_writing.get("tone", ""),
            "vocabulary": creative_writing.get("vocabulary", ""),
            "banned_words": creative_writing.get("banned_words", ""),
            "content_angle": creative_writing.get("content_angle", ""),
            "hook_bank": creative_writing.get("hook_bank", []),
            "examples": creative_writing.get("examples", ""),
            "energy": creative_writing.get("energy", "medium"),
            "image_style_suffix": image_config.get("style_suffix", ""),
        }
        # Duration controls — deploy-time override > creative pacing > default
        deploy_duration = jobs.get(job_id, {}).get("target_duration", "")
        if deploy_duration:
            enriched_config["_target_duration"] = deploy_duration
        elif creative:
            enriched_config["_target_duration"] = pc.get("target_duration", "medium")
        else:
            enriched_config["_target_duration"] = "medium"
        if creative:
            enriched_config["_max_slides"] = pc.get("max_slides", 4)
        duration_to_slides = {"short": 3, "medium": 4, "long": 5}
        if not enriched_config.get("_max_slides"):
            enriched_config["_max_slides"] = duration_to_slides.get(enriched_config["_target_duration"], 4)
        enriched_config["_available_footage"] = available_footage
        enriched_config["_screenshot_descriptions"] = screenshot_descriptions
        enriched_config["_footage_descriptions"] = footage_descriptions
        enriched_config["_footage_durations"] = footage_durations
        enriched_config["_footage_files"] = footage_files
        # Collect actual file paths for Vision analysis
        screenshot_file_paths = []
        if screenshots_dir.exists():
            screenshot_file_paths = [str(f) for f in screenshots_dir.iterdir() if f.suffix.lower() in (".png", ".jpg", ".jpeg")][:4]
        if not screenshot_file_paths:
            # Fall back to uploaded media paths
            try:
                for sr in query_db("SELECT file_path FROM uploaded_media WHERE app_id=? AND media_type='screenshot' LIMIT 4", (app_slug,)):
                    screenshot_file_paths.append(sr["file_path"])
            except Exception:
                pass
        enriched_config["_screenshot_paths"] = screenshot_file_paths

        video_engine = jobs.get(job_id, {}).get("video_engine", "standard")

        # Set hybrid/stock mode flags for script generator
        if video_engine == "hybrid":
            enriched_config["_hybrid_mode"] = True
            enriched_config["_stock_mode"] = True
            logger.info(f"[HYBRID] Mixed mode enabled — {len(footage_files)} app recordings available")
        elif video_engine == "remotion_stock":
            enriched_config["_stock_mode"] = True
            logger.info(f"[STOCK] Stock-only mode enabled")

        # Generate scripts — one per post type in content mix
        content_mix = jobs.get(job_id, {}).get("content_mix", ["link_in_bio"] * count)
        scripts = []
        for mix_i, pt in enumerate(content_mix):
            enriched_config["_post_type"] = pt
            batch = generate_scripts(enriched_config, count=1, model=script_model, available_screenshots=available_screenshots or None)
            for s in batch:
                s["_post_type"] = pt  # Tag each script with its post type
            scripts.extend(batch)
            jobs[job_id]["agents"]["script_writer"]["progress"] = f"{mix_i+1}/{len(content_mix)}"
            jobs[job_id]["message"] = f"Writing script {mix_i+1}/{len(content_mix)} ({pt.replace('_',' ')})..."
        scripts_dir = base_dir / "scripts"
        save_scripts(scripts, str(scripts_dir))
        jobs[job_id]["agents"]["script_writer"]["status"] = "done"
        jobs[job_id]["agents"]["script_writer"]["progress"] = f"{len(scripts)}/{count}"
        jobs[job_id]["agents"]["script_writer"]["current_task"] = f"Wrote {len(scripts)} scripts"
        real_script_cost = sum(s.get("_cost_script", 0.015) for s in scripts)
        jobs[job_id]["cost"]["scripts"] = round(real_script_cost, 4)
        jobs[job_id]["cost"]["total"] = round(sum(v for k, v in jobs[job_id]["cost"].items() if k != "total"), 3)
        _log_activity(job_id, "Script Writer", f"Wrote {len(scripts)} scripts")

        # ─── Script Preview: pause for user approval (with reject-regenerate loop) ───
        _attempt_number = jobs.get(job_id, {}).get("_attempt_number", 0)
        _show_preview = jobs.get(job_id, {}).get("script_preview", True) and _attempt_number == 0
        while _show_preview:
            # Build preview data for frontend
            preview_scripts = []
            for si, s in enumerate(scripts):
                preview_scripts.append({
                    "index": si,
                    "title": s.get("title", f"Video {si+1}"),
                    "post_type": s.get("_post_type", "link_in_bio"),
                    "hook_text": s.get("hook_text", ""),
                    "video_style": s.get("video_style", ""),
                    "persona": s.get("persona", {}).get("name", ""),
                    "mood": s.get("mood", ""),
                    "slides": [
                        {
                            "slide_type": sl.get("slide_type", ""),
                            "source": sl.get("source", "ai_generated"),
                            "voiceover": sl.get("voiceover", ""),
                            "image_prompt": sl.get("image_prompt", ""),
                            "duration_seconds": sl.get("duration_seconds", 2),
                        }
                        for sl in s.get("slides", [])
                    ],
                    "description": s.get("description", ""),
                })
            jobs[job_id]["status"] = "awaiting_script_approval"
            jobs[job_id]["message"] = f"✏️ {len(scripts)} script(s) ready for review"
            jobs[job_id]["preview_scripts"] = preview_scripts
            # Reset the event for this round
            jobs[job_id]["_script_approval_event"] = threading.Event()
            jobs[job_id]["_regenerate_scripts"] = False
            _save_jobs()
            # Wait for user to approve or reject
            approved = jobs[job_id]["_script_approval_event"].wait(timeout=1800)
            if not approved:
                jobs[job_id]["status"] = "error"
                jobs[job_id]["message"] = "Script approval timed out (30 min)"
                _save_jobs()
                return
            # Check if user rejected and wants regeneration
            if jobs[job_id].get("_regenerate_scripts"):
                jobs[job_id]["status"] = "running"
                jobs[job_id]["message"] = "Regenerating scripts with updated prompt..."
                _save_jobs()
                # Reload creative to get updated identity
                _fresh_creative = get_creative(template_id) if template_id else None
                if _fresh_creative:
                    _fresh_wc = _fresh_creative.get("writing_config", {}) or {}
                    enriched_config["_creative"]["writing"]["identity"] = _fresh_wc.get("identity", "")
                    enriched_config["_template"]["writer_identity"] = _fresh_wc.get("identity", "")
                # Regenerate scripts
                content_mix = jobs.get(job_id, {}).get("content_mix", ["link_in_bio"] * count)
                scripts = []
                for mix_i, pt in enumerate(content_mix):
                    enriched_config["_post_type"] = pt
                    batch = generate_scripts(enriched_config, count=1, model=script_model, available_screenshots=available_screenshots or None)
                    for s in batch:
                        s["_post_type"] = pt
                    scripts.extend(batch)
                save_scripts(scripts, str(scripts_dir))
                continue  # loop back to show preview again
            # User approved — proceed
            if jobs[job_id].get("_approved_scripts"):
                scripts = jobs[job_id]["_approved_scripts"]
                save_scripts(scripts, str(scripts_dir))
            break
        jobs[job_id]["status"] = "running"
        jobs[job_id]["message"] = "Scripts approved — generating images..."
        _save_jobs()

        # Add all to script stage
        for s in scripts:
            stages["script"].append({"title": s.get("title", "Untitled"), "persona": s.get("persona", {}).get("name", "")})

        videos_created = 0
        videos_failed = 0
        video_engine = jobs.get(job_id, {}).get("video_engine", "standard")
        captions_ai_avatar = jobs.get(job_id, {}).get("captions_ai_avatar", "Kate")

        for i, script in enumerate(scripts):
            title = script.get("title", f"Video {i+1}")
            persona_name = script.get("persona", {}).get("name", "")

            # ─── Mirage Video path (portrait + audio → talking head + captions) ───
            if video_engine == "mirage":
                stages["script"] = [x for x in stages["script"] if x["title"] != title]
                stages["voice"].append({"title": title, "persona": persona_name})
                jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — generating voice"
                jobs[job_id]["agents"]["voice_actor"]["status"] = "working"

                try:
                    # Step 1: Generate voiceover with ElevenLabs
                    video_dir = base_dir / f"video_{i:03d}"
                    video_dir.mkdir(parents=True, exist_ok=True)
                    voiceover_path = generate_voiceover_for_script(
                        script=script, output_dir=str(video_dir / "audio"), engine=voice_engine,
                        voice_tuning=voice_config_override or tuning,
                    )
                    _log_activity(job_id, "Voice Actor", f"Recorded voiceover for \"{title}\"")

                    # Step 2: Generate portrait image with Flux
                    stages["voice"] = [x for x in stages["voice"] if x["title"] != title]
                    stages["images"].append({"title": title, "persona": persona_name})
                    jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — generating portrait"
                    jobs[job_id]["agents"]["image_artist"]["status"] = "working"

                    persona = script.get("persona", app_config.get("personas", [{}])[0])
                    ref_path = ref_dir / f"{app_slug}_{persona.get('id', 'default')}.png"
                    ref_image = str(ref_path) if ref_path.exists() else None
                    # Generate just 1 portrait image for the talking head
                    portrait_script = {"slides": [script["slides"][0]], "title": title, "persona": script.get("persona")}
                    portrait_paths = generate_images_for_script(
                        script=portrait_script, output_dir=str(video_dir / "images"),
                        app_config=app_config, reference_image_path=ref_image, engine=image_engine,
                        screenshots_dir=str(screenshots_dir) if screenshots_dir.exists() else None,
                    )
                    portrait_path = portrait_paths[0] if portrait_paths else None
                    _log_activity(job_id, "Image Artist", f"Generated portrait for \"{title}\"")

                    if not portrait_path or not voiceover_path:
                        raise Exception("Missing portrait image or voiceover audio")

                    # Step 3: Send to Mirage API
                    stages["images"] = [x for x in stages["images"] if x["title"] != title]
                    stages["edit"].append({"title": title, "persona": persona_name})
                    jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — Mirage generating talking head"
                    jobs[job_id]["agents"]["video_editor"]["status"] = "working"
                    jobs[job_id]["agents"]["video_editor"]["current_task"] = f"Mirage generating \"{title}\""

                    from mirage_video import generate_talking_head
                    videos_dir.mkdir(parents=True, exist_ok=True)
                    ts = int(time.time())
                    video_filename = f"{app_slug}_{i:03d}_{ts}.mp4"
                    video_path = str(videos_dir / video_filename)

                    generate_talking_head(portrait_path, voiceover_path, video_path)
                    _log_activity(job_id, "Video Editor", f"Mirage talking head ready: \"{title}\"")

                    # Step 4: Add captions via Mirage (if available)
                    try:
                        from mirage_video import caption_video
                        captioned_path = video_path.replace(".mp4", "_captioned.mp4")
                        jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — adding captions"
                        caption_video(video_path, captioned_path)
                        import shutil
                        shutil.move(captioned_path, video_path)
                        _log_activity(job_id, "Video Editor", f"Captions added to \"{title}\"")
                    except Exception as cap_err:
                        logger.warning(f"Mirage caption step skipped: {cap_err}")

                    # Generate thumbnail
                    thumb_path = video_path.replace(".mp4", "_thumb.jpg")
                    try:
                        generate_thumbnail(video_path, thumb_path)
                    except Exception:
                        thumb_path = ""

                    stages["edit"] = [x for x in stages["edit"] if x["title"] != title]
                    stages["ready"].append({"title": title, "persona": persona_name, "progress": "Ready"})
                    jobs[job_id]["agents"]["video_editor"]["progress"] = f"{i+1}/{count}"

                    per_video_cost = round(0.045 + 0.03 + 0.10, 3)  # image + voice + mirage
                    try:
                        _db_insert_video({
                            "id": Path(video_path).stem,
                            "app_slug": app_slug,
                            "title": title,
                            "persona": persona_name,
                            "creative_id": template_id or "",
                            "file_path": video_path,
                            "status": "ready",
                            "qa_score": 0,
                            "duration_seconds": script.get("duration_seconds", 0),
                            "slide_count": len(script.get("slides", [])),
                            "cost_total": per_video_cost,
                            "file_size_bytes": os.path.getsize(video_path) if os.path.exists(video_path) else 0,
                            "created_at": datetime.now().isoformat(),
                            "thumbnail_path": thumb_path or "",
                            "script_json": json.dumps(script),
                            "upload_status": "pending",
                            "post_type": script.get("_post_type", "link_in_bio"),
                        })
                    except Exception as db_err:
                        logger.warning(f"DB insert failed: {db_err}")

                    videos_created += 1
                    jobs[job_id]["videos_created"] = videos_created

                except Exception as e:
                    logger.error(f"Mirage error on video {i+1}: {e}", exc_info=True)
                    _log_error(job_id, title, "edit", "Video Editor", str(e))
                    for stage_name in stages:
                        stages[stage_name] = [x for x in stages[stage_name] if x.get("title") != title]

                continue

            # ─── Captions.ai Creator path (talking head from script) ───
            if video_engine == "captions_ai":
                stages["script"] = [x for x in stages["script"] if x["title"] != title]
                stages["edit"].append({"title": title, "persona": captions_ai_avatar, "progress": "Generating..."})
                jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — Captions.ai Creator"
                jobs[job_id]["agents"]["video_editor"]["status"] = "working"
                jobs[job_id]["agents"]["video_editor"]["current_task"] = f"Captions.ai generating \"{title}\" with {captions_ai_avatar}"
                _log_activity(job_id, "Video Editor", f"Sending \"{title}\" to Captions.ai Creator ({captions_ai_avatar})")

                try:
                    from captions_ai import generate_creator_video
                    # Build full script text from all slides
                    voiceover_parts = []
                    for slide in script.get("slides", []):
                        vo = slide.get("voiceover", "")
                        if vo:
                            voiceover_parts.append(vo)
                    full_script = " ".join(voiceover_parts)
                    if not full_script:
                        full_script = script.get("description", title)

                    videos_dir.mkdir(parents=True, exist_ok=True)
                    ts = int(time.time())
                    video_filename = f"{app_slug}_{i:03d}_{ts}.mp4"
                    video_path = str(videos_dir / video_filename)

                    generate_creator_video(full_script, video_path, creator_name=captions_ai_avatar)

                    # Generate thumbnail
                    thumb_path = video_path.replace(".mp4", "_thumb.jpg")
                    try:
                        generate_thumbnail(video_path, thumb_path)
                    except Exception:
                        thumb_path = ""

                    stages["edit"] = [x for x in stages["edit"] if x["title"] != title]
                    stages["ready"].append({"title": title, "persona": captions_ai_avatar, "progress": "Ready"})
                    jobs[job_id]["agents"]["video_editor"]["progress"] = f"{i+1}/{count}"
                    _log_activity(job_id, "Video Editor", f"Captions.ai video ready: \"{title}\"")

                    # Insert into database
                    per_video_cost = 0.10  # estimate for Captions.ai
                    try:
                        _db_insert_video({
                            "id": Path(video_path).stem,
                            "app_slug": app_slug,
                            "title": title,
                            "persona": captions_ai_avatar,
                            "creative_id": template_id or "",
                            "file_path": video_path,
                            "status": "ready",
                            "qa_score": 0,
                            "duration_seconds": script.get("duration_seconds", 0),
                            "slide_count": len(script.get("slides", [])),
                            "cost_total": per_video_cost,
                            "file_size_bytes": os.path.getsize(video_path) if os.path.exists(video_path) else 0,
                            "created_at": datetime.now().isoformat(),
                            "thumbnail_path": thumb_path or "",
                            "script_json": json.dumps(script),
                            "upload_status": "pending",
                            "post_type": script.get("_post_type", "link_in_bio"),
                        })
                    except Exception as db_err:
                        logger.warning(f"DB insert failed: {db_err}")

                    videos_created += 1
                    jobs[job_id]["videos_created"] = videos_created

                except Exception as e:
                    logger.error(f"Captions.ai error on video {i+1}: {e}", exc_info=True)
                    _log_error(job_id, title, "edit", "Video Editor", str(e))
                    stages["edit"] = [x for x in stages["edit"] if x["title"] != title]

                continue  # Skip standard pipeline for this video

            # ─── Standard pipeline (AI images + voice + assembly) ───
            # Move from script to images
            stages["script"] = [x for x in stages["script"] if x["title"] != title]
            stages["images"].append({"title": title, "persona": persona_name, "progress": "Generating..."})
            jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — images"
            n_slides = len(script.get("slides", []))
            jobs[job_id]["agents"]["image_artist"]["status"] = "working"
            jobs[job_id]["agents"]["image_artist"]["current_task"] = f"Generating {n_slides} images for \"{title}\""

            video_dir = base_dir / f"video_{i:03d}"
            video_dir.mkdir(parents=True, exist_ok=True)

            try:
                persona = script.get("persona", app_config.get("personas", [{}])[0])
                ref_path = ref_dir / f"{app_slug}_{persona.get('id', 'default')}.png"
                ref_image = str(ref_path) if ref_path.exists() else None

                # Skip image generation for hybrid/stock engines (all visuals come from video)
                if video_engine in ("hybrid", "remotion_stock"):
                    image_paths = [""] * len(script.get("slides", []))
                    _log_activity(job_id, "Image Artist", f"Skipped images for \"{title}\" (hybrid engine uses video)")
                else:
                    image_paths = generate_images_for_script(
                        script=script, output_dir=str(video_dir / "images"),
                        app_config=app_config, reference_image_path=ref_image, engine=image_engine,
                        screenshots_dir=str(screenshots_dir) if screenshots_dir.exists() else None,
                    )
                    jobs[job_id]["cost"]["images"] = round(jobs[job_id]["cost"].get("images", 0) + len(image_paths) * 0.045, 3)
                    jobs[job_id]["cost"]["total"] = round(sum(v for k, v in jobs[job_id]["cost"].items() if k != "total"), 3)
                    _log_activity(job_id, "Image Artist", f"Generated {len(image_paths)} images for \"{title}\"")

                # Move to voice
                stages["images"] = [x for x in stages["images"] if x["title"] != title]
                stages["voice"].append({"title": title, "persona": persona_name})
                jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — voiceover"
                jobs[job_id]["agents"]["image_artist"]["progress"] = f"{i+1}/{count}"
                jobs[job_id]["agents"]["voice_actor"]["status"] = "working"
                jobs[job_id]["agents"]["voice_actor"]["current_task"] = f"Recording voiceover for \"{title}\""

                voiceover_result = generate_voiceover_for_script(
                    script=script, output_dir=str(video_dir / "audio"), engine=voice_engine,
                    voice_tuning=voice_config_override or tuning,
                )

                # Move to edit
                stages["voice"] = [x for x in stages["voice"] if x["title"] != title]
                stages["edit"].append({"title": title, "persona": persona_name})
                jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — assembling"
                jobs[job_id]["agents"]["voice_actor"]["progress"] = f"{i+1}/{count}"
                # Real voice cost: ~$0.30 per 1000 chars
                vo_chars = sum(len(s.get("voiceover", "")) for s in script.get("slides", []))
                vo_cost = round((vo_chars / 1000) * 0.30, 4)
                jobs[job_id]["cost"]["voiceovers"] = round(jobs[job_id]["cost"].get("voiceovers", 0) + vo_cost, 4)
                jobs[job_id]["cost"]["total"] = round(sum(v for k, v in jobs[job_id]["cost"].items() if k != "total"), 3)
                _log_activity(job_id, "Voice Actor", f"Recorded voiceover for \"{title}\"" if voiceover_result else f"Skipped voiceover for \"{title}\" (trending sound)")
                jobs[job_id]["agents"]["video_editor"]["status"] = "working"
                jobs[job_id]["agents"]["video_editor"]["current_task"] = f"Assembling \"{title}\""

                # Build slide timings from per-slide audio durations
                if isinstance(voiceover_result, dict):
                    slide_audio = voiceover_result.get("slide_audio", [])
                    voiceover_path = voiceover_result.get("full_path")
                else:
                    # Backwards compat — old format returned just a path
                    slide_audio = []
                    voiceover_path = voiceover_result

                if slide_audio:
                    # Per-slide timing: each slide's duration = its audio duration (min 2s for images)
                    slide_timings = []
                    current_time = 0.0
                    for sa in slide_audio:
                        idx = sa["slide_index"]
                        audio_dur = sa["duration"]
                        # Image slides: use audio duration but min 2s
                        slide_dur = max(2.0, audio_dur) if audio_dur > 0 else 2.5
                        slide_timings.append({"slide_index": idx, "start": round(current_time, 3), "end": round(current_time + slide_dur, 3)})
                        current_time += slide_dur
                    # Generate word timestamps per-slide for subtitles
                    all_words = []
                    all_lines = []
                    time_offset = 0.0
                    for sa in slide_audio:
                        if sa["path"]:
                            try:
                                w = generate_word_timestamps(sa["path"])
                                # Offset timestamps by slide start time
                                for word in w:
                                    word["start"] += time_offset
                                    word["end"] += time_offset
                                all_words.extend(w)
                                sl = group_words_into_lines(w)
                                all_lines.extend(sl)
                            except Exception:
                                pass
                        time_offset += slide_timings[sa["slide_index"]]["end"] - slide_timings[sa["slide_index"]]["start"]
                    words, lines = all_words, all_lines
                elif voiceover_path:
                    words = generate_word_timestamps(voiceover_path)
                    lines = group_words_into_lines(words)
                    slide_timings = calculate_slide_timings(words, script)
                else:
                    words, lines = [], []
                    slide_timings = [{"slide_index": j, "start": j * 3.0, "end": (j + 1) * 3.0} for j in range(len(script["slides"]))]

                subtitle_data = {"words": words, "lines": lines, "slide_timings": slide_timings}
                videos_dir.mkdir(parents=True, exist_ok=True)
                ts = int(time.time())
                video_filename = f"{app_slug}_{i:03d}_{ts}.mp4"
                video_path = str(videos_dir / video_filename)

                mood = script.get("mood", "energetic")

                # ── Hybrid/Stock engine path: Remotion + stock footage ──
                if video_engine in ("remotion_stock", "hybrid"):
                    # Resolve video_footage: sources to local file paths
                    # Prefer enriched DB data (has original filenames + descriptions)
                    footage_files = app_config.get("_footage_files", [])
                    if not footage_files:
                        # Fallback: scan filesystem if DB data not available
                        media_dir = OUTPUT_DIR / app_slug / "media"
                        if media_dir.exists():
                            footage_files = [
                                {"file_path": str(f), "original_filename": f.name}
                                for f in media_dir.iterdir()
                                if f.suffix.lower() in (".mov", ".mp4", ".avi", ".mkv")
                            ]

                    def _normalize_name(name):
                        """Normalize filename for fuzzy matching — lowercase, strip extension, normalize separators."""
                        import os as _os
                        name = _os.path.splitext(name)[0].lower().strip()
                        # Normalize separators: underscores, hyphens, spaces all become spaces
                        for ch in ("_", "-", ".", "  "):
                            name = name.replace(ch, " ")
                        return name.strip()

                    def _match_footage(clip_name, footage_files):
                        """Match a script's clip_name to available footage. Returns (file_path, match_type) or (None, None)."""
                        clip_lower = clip_name.lower().strip()
                        clip_norm = _normalize_name(clip_name)

                        # Pass 1: Exact case-insensitive substring match on original filename
                        for fm in footage_files:
                            orig = fm.get("original_filename", "")
                            if clip_lower in orig.lower() or orig.lower() in clip_lower:
                                return fm["file_path"], "exact"

                        # Pass 2: Case-insensitive substring match on full file path
                        for fm in footage_files:
                            if clip_lower in fm["file_path"].lower():
                                return fm["file_path"], "path"

                        # Pass 3: Normalized fuzzy match (strips extensions, normalizes separators)
                        for fm in footage_files:
                            orig_norm = _normalize_name(fm.get("original_filename", ""))
                            if clip_norm in orig_norm or orig_norm in clip_norm:
                                return fm["file_path"], "fuzzy"

                        # Pass 4: Word overlap — if most words in clip_name appear in the filename
                        clip_words = set(clip_norm.split())
                        if clip_words:
                            best_score = 0
                            best_path = None
                            for fm in footage_files:
                                orig_words = set(_normalize_name(fm.get("original_filename", "")).split())
                                overlap = len(clip_words & orig_words)
                                score = overlap / len(clip_words)
                                if score > best_score and score >= 0.5:
                                    best_score = score
                                    best_path = fm["file_path"]
                            if best_path:
                                return best_path, f"word_overlap({best_score:.0%})"

                        return None, None

                    for slide_idx, slide in enumerate(script.get("slides", [])):
                        src = slide.get("source", "")
                        if src.startswith("video_footage:"):
                            clip_name = src.replace("video_footage:", "").strip()
                            matched_path, match_type = _match_footage(clip_name, footage_files)

                            if matched_path:
                                slide["_footage_path"] = matched_path
                                slide["_is_video_clip"] = True
                                logger.info(f"[FOOTAGE] Slide {slide_idx}: '{clip_name}' -> matched ({match_type})")
                            elif footage_files:
                                # Fallback: use random available footage — but LOG it clearly
                                import random as _rand
                                fallback = _rand.choice(footage_files)
                                slide["_footage_path"] = fallback["file_path"]
                                slide["_is_video_clip"] = True
                                logger.warning(f"[FOOTAGE] Slide {slide_idx}: '{clip_name}' NOT FOUND in {len(footage_files)} recordings. Using random fallback: {fallback.get('original_filename', 'unknown')}")
                            else:
                                logger.warning(f"[FOOTAGE] Slide {slide_idx}: '{clip_name}' requested but NO recordings available!")

                    from remotion_stock_assembler import assemble_video_remotion_stock
                    # Pass selected Remotion template
                    app_config["_remotion_template"] = jobs.get(job_id, {}).get("remotion_template", "stock-narration")
                    assemble_video_remotion_stock(
                        script=script, image_paths=image_paths, voiceover_path=voiceover_path,
                        subtitle_data=subtitle_data, output_path=video_path,
                        app_config=app_config,
                    )
                else:
                    # ── Standard MoviePy assembly ──
                    # Merge caption_config into text_style (template overrides app_config)
                    merged_text_style = app_config.get("text_style") or {}
                    if caption_config:
                        merged_text_style = {**merged_text_style, **caption_config}
                    # Build tuning from video_config (template overrides app_config)
                    merged_tuning = {**tuning, **video_config} if video_config else tuning
                    assemble_video(
                        script=script, image_paths=image_paths, voiceover_path=voiceover_path,
                        subtitle_data=subtitle_data, output_path=video_path,
                        text_style=merged_text_style, mood=mood,
                        tuning=merged_tuning,
                    )

                # Generate thumbnail
                thumb_path = video_path.replace(".mp4", "_thumb.jpg")
                try:
                    generate_thumbnail(video_path, thumb_path)
                except Exception:
                    thumb_path = ""

                # Move to QA
                stages["edit"] = [x for x in stages["edit"] if x["title"] != title]
                stages["qa"].append({"title": title, "persona": persona_name})
                jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — QA review"
                jobs[job_id]["agents"]["video_editor"]["progress"] = f"{i+1}/{count}"
                _log_activity(job_id, "Video Editor", f"Assembled \"{title}\"")
                jobs[job_id]["agents"]["qa_reviewer"]["status"] = "working"
                jobs[job_id]["agents"]["qa_reviewer"]["current_task"] = f"Reviewing \"{title}\""

                # Run QA review via Claude Vision
                qa_passed = True
                qa_score = 0
                review = {}
                qa_threshold = qa_config.get("threshold", float(os.environ.get("QA_THRESHOLD", 7.0)))
                # Training mode overrides threshold with target score
                _training = jobs.get(job_id, {}).get("training_mode", False)
                if _training:
                    qa_threshold = jobs.get(job_id, {}).get("training_target", 7.5)
                try:
                    from qa_reviewer import review_video as qa_review, save_review as qa_save
                    from video_assembler import extract_key_frames as qa_extract

                    qa_frames_dir = video_dir / "qa_frames"
                    frame_paths = qa_extract(video_path, str(qa_frames_dir))
                    review = qa_review(
                        frame_paths=frame_paths,
                        script=script,
                        app_config=app_config,
                        threshold=qa_threshold,
                        post_type=script.get("_post_type", enriched_config.get("_post_type", "link_in_bio")),
                    )
                    qa_save(review, str(video_dir / "qa_review.json"))
                    qa_score = review.get("overall_score", 0)
                    qa_passed = review.get("pass", False)
                    jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — QA score: {qa_score}/10"

                    jobs[job_id]["agents"]["qa_reviewer"]["progress"] = f"{i+1}/{count}"
                    real_qa_cost = review.get("_cost_qa", 0.02)
                    jobs[job_id]["cost"]["qa"] = round(jobs[job_id]["cost"].get("qa", 0) + real_qa_cost, 4)
                    jobs[job_id]["cost"]["total"] = round(sum(v for k, v in jobs[job_id]["cost"].items() if k != "total"), 3)
                    _log_activity(job_id, "QA Reviewer", f"Scored \"{title}\" {qa_score}/10 — {'PASS' if qa_passed else 'FAIL'}")
                    if not qa_passed:
                        logger.warning(f"QA FAIL for {title}: {qa_score}/10 (threshold: {qa_threshold}) — keeping file for review")
                        # Auto-update agent identity with QA feedback
                        try:
                            qa_issues = review.get("issues", [])
                            qa_suggestions = review.get("suggestions", [])
                            if qa_issues and template_id:
                                feedback_text = f"QA FAILED ({qa_score}/10). Issues: " + "; ".join(qa_issues[:3])
                                if qa_suggestions:
                                    feedback_text += ". Suggestions: " + "; ".join(qa_suggestions[:2])
                                update_agent_identity(template_id, feedback_text, source="qa")
                                _log_activity(job_id, "QA Reviewer", f"Auto-updated agent prompt with QA feedback")
                        except Exception as pb_err:
                            logger.error(f"Agent identity auto-update failed: {pb_err}")
                except Exception as qa_err:
                    logger.error(f"QA review error (continuing anyway): {qa_err}", exc_info=True)
                    qa_passed = True  # Don't block on QA errors

                stages["qa"] = [x for x in stages["qa"] if x["title"] != title]

                # Calculate per-video cost (real where available, $0.045/image for Flux)
                img_cost = 0 if video_engine in ("hybrid", "remotion_stock") else round(len(image_paths) * 0.045, 4)
                script_cost_per = script.get("_cost_script", 0.015)
                real_qa_cost = review.get("_cost_qa", 0.02) if review else 0.02
                per_video_cost = round(img_cost + vo_cost + script_cost_per + real_qa_cost, 4)
                # Get voice_id from the agent's voice config
                _voice_id_used = ""
                if template_id:
                    try:
                        _cr = get_creative(template_id)
                        if _cr:
                            import json as _j
                            _vc = _j.loads(_cr.get("voice_config", "{}")) if isinstance(_cr.get("voice_config"), str) else _cr.get("voice_config", {})
                            _voice_id_used = _vc.get("elevenlabs_voice_id", "")
                    except Exception:
                        pass

                if qa_passed:
                    stages["ready"].append({"title": title, "persona": persona_name, "progress": "Ready"})
                    # Insert video into database
                    try:
                        _db_insert_video({
                            "id": Path(video_path).stem,
                            "app_slug": app_slug,
                            "title": title,
                            "persona": persona_name,
                            "creative_id": template_id or "",
                            "file_path": video_path,
                            "status": "ready",
                            "qa_score": qa_score,
                            "duration_seconds": script.get("duration_seconds", 0),
                            "slide_count": len(script.get("slides", [])),
                            "cost_total": per_video_cost,
                            "file_size_bytes": os.path.getsize(video_path) if os.path.exists(video_path) else 0,
                            "created_at": datetime.now().isoformat(),
                            "thumbnail_path": thumb_path or "",
                            "script_json": json.dumps(script),
                            "qa_details": json.dumps(review),
                            "cost_breakdown": json.dumps({"images": img_cost, "voice": vo_cost, "scripts": script_cost_per, "qa": real_qa_cost}),
                            "upload_status": "pending",
                            "post_type": script.get("_post_type", "link_in_bio"),
                            "video_engine": video_engine,
                            "voice_id": _voice_id_used,
                        })
                    except Exception as db_err:
                        logger.warning(f"Failed to insert video into DB: {db_err}")
                    # Add to upload queue with smart scheduling
                    try:
                        _add_to_upload_queue(
                            app_slug, video_path,
                            title=title,
                            description=script.get("description", ""),
                            hashtags=script.get("hashtags", []),
                        )
                        _log_activity(job_id, "Publisher", f"Queued \"{title}\" for upload at scheduled time")
                    except Exception as q_err:
                        logger.warning(f"Failed to queue upload: {q_err}")
                else:
                    # Insert failed video into database for tracking
                    try:
                        _db_insert_video({
                            "id": Path(video_path).stem,
                            "app_slug": app_slug,
                            "title": title,
                            "persona": persona_name,
                            "creative_id": template_id or "",
                            "file_path": video_path,
                            "status": "failed",
                            "qa_score": qa_score,
                            "duration_seconds": script.get("duration_seconds", 0),
                            "slide_count": len(script.get("slides", [])),
                            "cost_total": per_video_cost,
                            "file_size_bytes": os.path.getsize(video_path) if os.path.exists(video_path) else 0,
                            "created_at": datetime.now().isoformat(),
                            "thumbnail_path": thumb_path or "",
                            "script_json": json.dumps(script),
                            "qa_details": json.dumps(review),
                            "cost_breakdown": json.dumps({"images": img_cost, "voice": vo_cost, "scripts": script_cost_per, "qa": real_qa_cost}),
                            "upload_status": "failed",
                            "post_type": script.get("_post_type", "link_in_bio"),
                            "video_engine": video_engine,
                            "voice_id": _voice_id_used,
                        })
                    except Exception as db_err:
                        logger.warning(f"Failed to insert failed video into DB: {db_err}")
                    videos_failed += 1

                    # Auto-retry logic
                    training_mode = jobs.get(job_id, {}).get("training_mode", False)
                    max_retries = 10 if training_mode else 1
                    retry_count = script.get("_retry_count", 0)

                    if retry_count < max_retries:
                        try:
                            attempt_label = f"Training attempt {retry_count + 2}/10" if training_mode else "Auto-retry"
                            _log_activity(job_id, "Pipeline", f"{attempt_label} — learning from failure...")
                            jobs[job_id]["message"] = f"{attempt_label}: learning and retrying..."

                            # Reload creative to get updated identity (AI already updated it above)
                            _retry_creative = get_creative(template_id) if template_id else None
                            if _retry_creative:
                                _retry_config = dict(enriched_config)
                                _fresh_wc = _retry_creative.get("writing_config", {}) or {}
                                _retry_config["_creative"]["writing"]["identity"] = _fresh_wc.get("identity", "")
                                _retry_config["_template"]["writer_identity"] = _fresh_wc.get("identity", "")
                                _retry_config["_post_type"] = script.get("_post_type", "link_in_bio")
                                _retry_scripts = generate_scripts(_retry_config, count=1, model=script_model, available_screenshots=available_screenshots or None)
                                if _retry_scripts:
                                    _retry_script = _retry_scripts[0]
                                    _retry_script["_retry_count"] = retry_count + 1
                                    _retry_script["_post_type"] = script.get("_post_type", "link_in_bio")
                                    scripts.append(_retry_script)
                                    _log_activity(job_id, "Pipeline", f"{attempt_label}: new script \"{_retry_script.get('title', 'retry')}\"")
                        except Exception as retry_err:
                            logger.error(f"Retry failed: {retry_err}")

                if qa_passed:
                    videos_created += 1
                jobs[job_id]["videos_created"] = videos_created

            except Exception as e:
                logger.error(f"Error on video {i+1}: {e}", exc_info=True)
                # Determine which stage failed
                failed_stage = "unknown"
                agent_name = "Pipeline"
                msg = jobs[job_id].get("message", "")
                if "images" in msg: failed_stage, agent_name = "images", "Image Artist"
                elif "voiceover" in msg: failed_stage, agent_name = "voice", "Voice Actor"
                elif "assembling" in msg: failed_stage, agent_name = "edit", "Video Editor"
                elif "QA" in msg: failed_stage, agent_name = "qa", "QA Reviewer"
                elif "scripts" in msg.lower(): failed_stage, agent_name = "script", "Script Writer"
                _log_error(job_id, title, failed_stage, agent_name, str(e))
                # Save failed video to DB so it shows in vault
                try:
                    _db_insert_video({
                        "id": f"{app_slug}_{i:03d}_{int(time.time())}_fail",
                        "app_slug": app_slug,
                        "title": f"[FAILED] {title}",
                        "persona": persona_name,
                        "creative_id": template_id or "",
                        "file_path": "",
                        "status": "failed",
                        "qa_score": 0,
                        "duration_seconds": 0,
                        "slide_count": len(script.get("slides", [])) if script else 0,
                        "cost_total": 0,
                        "file_size_bytes": 0,
                        "created_at": datetime.now().isoformat(),
                        "script_json": json.dumps(script) if script else "{}",
                        "qa_details": json.dumps({"error": str(e), "stage": failed_stage}),
                        "upload_status": "failed",
                        "post_type": script.get("_post_type", "link_in_bio") if script else "link_in_bio",
                        "video_engine": video_engine,
                    })
                except Exception:
                    pass
                for stage_name in stages:
                    stages[stage_name] = [x for x in stages[stage_name] if x.get("title") != title]
                continue

        prev_errors = jobs[job_id].get("errors", [])
        prev_agents = jobs[job_id].get("agents", {})
        prev_cost = jobs[job_id].get("cost", {})
        fail_msg = f" ({videos_failed} failed QA)" if videos_failed else ""
        _log_activity(job_id, "Pipeline", f"Finished — {videos_created} videos created{fail_msg}")
        prev_log = jobs[job_id].get("activity_log", [])
        for ag in prev_agents.values():
            if ag["status"] == "working":
                ag["status"] = "done"
        jobs[job_id] = {
            "status": "done",
            "message": f"Created {videos_created} videos!{fail_msg}",
            "videos_created": videos_created,
            "completed": videos_created,
            "total": count,
            "total_count": count,
            "pipeline_stages": stages,
            "agents": prev_agents,
            "errors": prev_errors,
            "activity_log": prev_log,
            "cost": prev_cost,
            "_timestamp": time.time(),
        }
        _save_jobs()
        jobs[job_id]["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        save_job_to_db(job_id, jobs[job_id])
        # Update generation_logs
        try:
            conn = get_db()
            conn.execute("UPDATE generation_logs SET status='done', completed_at=?, cost_total=?, cost_breakdown=? WHERE job_id=?",
                (time.strftime("%Y-%m-%dT%H:%M:%S"), prev_cost.get("total", 0), json.dumps(prev_cost), job_id))
            conn.commit(); conn.close()
        except Exception as e:
            logger.debug(f"Could not update generation_logs: {e}")

    except Exception as e:
        jobs[job_id] = {
            "status": "error", "message": str(e), "videos_created": 0,
            "pipeline_stages": {"script":[],"images":[],"voice":[],"edit":[],"qa":[],"ready":[]},
            "_timestamp": time.time(), "error_message": str(e),
        }
        _save_jobs()
        save_job_to_db(job_id, jobs[job_id])
        try:
            conn = get_db()
            conn.execute("UPDATE generation_logs SET status='error', error_message=? WHERE job_id=?", (str(e)[:500], job_id))
            conn.commit(); conn.close()
        except Exception as db_e:
            logger.debug(f"Could not update generation_logs on error: {db_e}")
        logger.error(f"Generation error: {e}", exc_info=True)


# ─── Batch 3: Suggestions, Tutorial, Assets ───────────────────────────────────

@app.route("/api/suggestions/<slug>", methods=["GET"])
def get_suggestions(slug):
    """AI-powered tips based on video performance data."""
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify([])
    with open(config_path) as f:
        config = json.load(f)

    suggestions = []
    personas = config.get("personas", [])
    qa_threshold = config.get("tuning", {}).get("qa_threshold", 7.0)

    # Suggestion: check QA threshold
    if qa_threshold < 5:
        suggestions.append({"type": "warning", "icon": "⚠️", "text": f"QA threshold is very low ({qa_threshold}). Consider raising to 6+ for better content quality."})

    # Suggestion: check persona count
    if len(personas) < 2:
        suggestions.append({"type": "tip", "icon": "💡", "text": "Add more personas for content variety. Multiple faces reduce shadow ban risk."})
    elif len(personas) >= 3:
        suggestions.append({"type": "success", "icon": "✅", "text": f"Great — {len(personas)} personas gives excellent content variety."})

    # Suggestion: check screenshots
    screenshots_dir = OUTPUT_DIR / slug / "app_screenshots"
    ss_count = len(list(screenshots_dir.glob("*.png"))) + len(list(screenshots_dir.glob("*.jpg"))) if screenshots_dir.exists() else 0
    if ss_count == 0:
        suggestions.append({"type": "tip", "icon": "📱", "text": "Upload app screenshots! Demo slides using real screenshots are free (no image generation cost) and look more authentic."})
    elif ss_count < 4:
        suggestions.append({"type": "tip", "icon": "📱", "text": f"You have {ss_count} screenshots. Upload more for variety — each saves $0.045 when used as a demo slide."})

    # Suggestion: music
    music_dir = Path("assets/music")
    music_count = len(list(music_dir.glob("*.mp3"))) + len(list(music_dir.glob("*.wav"))) if music_dir.exists() else 0
    if music_count == 0:
        suggestions.append({"type": "warning", "icon": "🎵", "text": "No background music! Drop MP3 files in assets/music/ for instant quality boost. Organize by mood (energetic/, chill/, etc.)."})

    # Suggestion: voice settings
    tuning = config.get("tuning", {})
    stability = tuning.get("voice_stability", 0.3)
    if stability > 0.6:
        suggestions.append({"type": "tip", "icon": "🎙️", "text": "Voice stability is high — voices may sound robotic. Try lowering to 0.2-0.4 for more natural expression."})

    return jsonify(suggestions)


@app.route("/api/tutorial-status", methods=["GET"])
def get_tutorial_status():
    flag_path = DATA_DIR / ".tutorial_completed"
    return jsonify({"completed": flag_path.exists()})

@app.route("/api/tutorial-status", methods=["POST"])
def set_tutorial_status():
    flag_path = DATA_DIR / ".tutorial_completed"
    flag_path.touch()
    return jsonify({"status": "completed"})


@app.route("/api/assets/music", methods=["GET"])
def list_music():
    music_dir = Path("assets/music")
    tracks = []
    if music_dir.exists():
        for f in sorted(music_dir.rglob("*")):
            if f.suffix.lower() in (".mp3", ".wav") and f.is_file():
                mood = f.parent.name if f.parent != music_dir else "general"
                tracks.append({"name": f.name, "mood": mood, "size_mb": round(f.stat().st_size / (1024*1024), 1), "path": str(f)})
    return jsonify(tracks)

@app.route("/api/assets/screenshots/<slug>", methods=["GET"])
def list_app_screenshots(slug):
    ss_dir = OUTPUT_DIR / slug / "app_screenshots"
    files = []
    if ss_dir.exists():
        for f in sorted(ss_dir.iterdir()):
            if f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                files.append({"name": f.name, "size_mb": round(f.stat().st_size / (1024*1024), 1), "url": f"/api/apps/{slug}/screenshots/{f.name}"})
    return jsonify(files)


# Start background queue processor
threading.Thread(target=_process_upload_queue, daemon=True).start()

# ─── Flask Error Handlers ────────────────────────────────────────────────────────
@app.errorhandler(404)
def not_found(e):
    logger.warning("404: %s", request.url)
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def server_error(e):
    logger.error("500 error on %s: %s", request.url, e, exc_info=True)
    return jsonify({"error": "Internal server error", "detail": str(e)}), 500

# ─── RUN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info("=" * 50)
    logger.info("Content Factory")
    logger.info(f"Open http://localhost:{port}")
    logger.info("=" * 50)
    app.run(debug=(port == 5000), host="0.0.0.0", port=port)
