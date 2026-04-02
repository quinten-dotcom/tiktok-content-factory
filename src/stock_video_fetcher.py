from __future__ import annotations

"""
Stock Video Fetcher — Fetches and caches stock video clips from Pixabay.

Searches Pixabay's free video API by keyword, scores results by tag relevance,
downloads the best match, and caches locally with AI-generated descriptions
for future reuse.

Cost: $0 (Pixabay is free) + ~$0.001 per clip for AI description via Haiku.
"""

import json
import os
import random
import hashlib
import time
import requests
from pathlib import Path
from log_config import get_logger

logger = get_logger(__name__)


# ── Config ──────────────────────────────────────────────────────────
PIXABAY_API_KEY = os.environ.get("PIXABAY_API_KEY", "")
CACHE_DIR = Path(__file__).parent.parent / "data" / "stock_cache"
CACHE_INDEX_PATH = CACHE_DIR / "index.json"

# Pixabay API rate limit: 100 requests per minute
_last_request_time = 0


def _rate_limit():
    """Respect Pixabay's rate limit of 100 req/min."""
    global _last_request_time
    elapsed = time.time() - _last_request_time
    if elapsed < 0.65:  # ~92 req/min to stay safe
        time.sleep(0.65 - elapsed)
    _last_request_time = time.time()


def _load_cache_index() -> dict:
    """Load the local cache index of downloaded + tagged clips."""
    if CACHE_INDEX_PATH.exists():
        try:
            return json.loads(CACHE_INDEX_PATH.read_text())
        except (json.JSONDecodeError, IOError):
            return {"clips": {}, "version": 1}
    return {"clips": {}, "version": 1}


def _save_cache_index(index: dict):
    """Save the cache index."""
    CACHE_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_INDEX_PATH.write_text(json.dumps(index, indent=2))


# Subject words that should NOT be interchanged — matching the wrong subject is worse than no match
_SUBJECT_GROUPS = {
    "people": {
        "woman": {"woman", "women", "girl", "female", "lady", "mom", "mother"},
        "man": {"man", "men", "boy", "male", "guy", "dad", "father"},
        "child": {"child", "children", "kid", "kids", "baby", "toddler", "teen"},
        "family": {"family", "families", "couple", "parents"},
        "person": {"person", "people", "someone"},
    },
}

# Related word clusters — partial credit for semantically close matches
_RELATED_WORDS = {
    "cooking": {"kitchen", "chef", "food", "meal", "recipe", "cook", "stove", "oven", "pan", "pot"},
    "kitchen": {"cooking", "cook", "food", "meal", "stove", "oven", "counter", "fridge"},
    "eating": {"food", "meal", "dinner", "lunch", "breakfast", "restaurant", "table"},
    "shopping": {"store", "grocery", "market", "cart", "aisle", "buy", "purchase"},
    "phone": {"mobile", "smartphone", "screen", "app", "scrolling", "device", "cell"},
    "healthy": {"health", "fitness", "wellness", "organic", "fresh", "diet", "nutrition"},
    "happy": {"smile", "smiling", "joy", "cheerful", "laughing", "positive", "excited"},
    "morning": {"sunrise", "dawn", "breakfast", "wake", "routine"},
    "exercise": {"workout", "gym", "fitness", "running", "yoga", "sport", "training"},
    "home": {"house", "apartment", "living", "room", "indoor", "domestic", "cozy"},
}


def _score_tags(video_tags: str, search_query: str) -> float:
    """
    Score how well a video's tags match the search query.
    Higher = better match.

    Scoring logic:
    1. Exact word matches (highest weight)
    2. Semantic/related word matches (partial credit)
    3. Subject mismatch penalty (e.g., query says "woman" but clip shows "man")
    4. Full phrase bonus
    """
    # Split multi-word tags into individual words for matching
    raw_tags = [t.strip().lower() for t in video_tags.split(",")]
    tag_words = set()
    for tag in raw_tags:
        for word in tag.split():
            tag_words.add(word)
    # Also keep full multi-word tags for exact phrase matching
    tag_phrases = set(raw_tags)

    query_words = search_query.lower().split()
    query_word_set = set(query_words)

    if not query_words:
        return 0

    # ── Step 1: Check for subject mismatch (strong penalty) ──
    subject_penalty = 0
    for group_name, group_variants in _SUBJECT_GROUPS.get("people", {}).items():
        # Find if query mentions a specific subject
        query_subjects = query_word_set & group_variants
        if not query_subjects:
            continue
        # Check if tags have a DIFFERENT subject from the same category
        for other_group, other_variants in _SUBJECT_GROUPS["people"].items():
            if other_group == group_name or other_group == "person":
                continue  # Same group or generic "person" — no penalty
            conflicting = tag_words & other_variants
            if conflicting and not (tag_words & group_variants):
                # Tags mention a different subject and NOT the queried one
                subject_penalty = -0.35
                break

    # ── Step 2: Score each query word ──
    word_scores = []
    for i, qw in enumerate(query_words):
        # First word (usually the subject) gets higher weight
        weight = 1.3 if i == 0 else 1.0

        if qw in tag_words:
            # Exact match
            word_scores.append(1.0 * weight)
        else:
            # Check for related word match (partial credit)
            related = _RELATED_WORDS.get(qw, set())
            if related & tag_words:
                word_scores.append(0.4 * weight)
            else:
                # Check reverse: is any related set for a tag word matching our query?
                found_related = False
                for tw in tag_words:
                    if qw in _RELATED_WORDS.get(tw, set()):
                        word_scores.append(0.35 * weight)
                        found_related = True
                        break
                if not found_related:
                    word_scores.append(0)

    # Base score: weighted average of word matches
    total_weight = sum(1.3 if i == 0 else 1.0 for i in range(len(query_words)))
    score = sum(word_scores) / total_weight if total_weight > 0 else 0

    # ── Step 3: Full phrase bonus ──
    query_phrase = search_query.lower().strip()
    if query_phrase in tag_phrases:
        score += 0.25

    # ── Step 4: Apply subject penalty ──
    score += subject_penalty

    return max(min(score, 2.0), 0)


def _generate_ai_description(video_path: str, tags: str) -> str:
    """
    Generate a rich description of a video clip using Claude Haiku vision.
    Extracts a frame and describes what's in it.
    Cost: ~$0.001 per clip.
    """
    try:
        import subprocess
        import base64
        import tempfile

        # Extract a frame from 1 second in (or start if shorter)
        frame_path = tempfile.mktemp(suffix=".jpg")
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", "1", "-i", video_path,
                "-frames:v", "1", "-q:v", "2", frame_path,
            ],
            capture_output=True,
            timeout=15,
        )

        if not os.path.exists(frame_path):
            return f"Stock video clip. Tags: {tags}"

        with open(frame_path, "rb") as f:
            img_data = base64.b64encode(f.read()).decode("utf-8")

        os.unlink(frame_path)

        import anthropic
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": (
                            "Describe this video frame in one detailed sentence for a stock video library. "
                            "Include: who/what is in the shot, what they're doing, the setting, lighting, "
                            "camera angle, and mood. Be specific and factual. Output ONLY the description."
                        ),
                    },
                ],
            }],
        )

        description = response.content[0].text.strip()
        logger.info(f"AI description: {description[:80]}...")
        return description

    except Exception as e:
        logger.warning(f"AI description failed ({e}), using tags only")
        return f"Stock video clip. Tags: {tags}"


def search_pixabay(
    query: str,
    orientation: str = "vertical",
    min_duration: int = 3,
    max_duration: int = 30,
    per_page: int = 20,
) -> list[dict]:
    """
    Search Pixabay for stock videos matching a query.

    Returns list of video results with metadata.
    """
    if not PIXABAY_API_KEY:
        raise ValueError(
            "PIXABAY_API_KEY not set. Get a free key at https://pixabay.com/api/docs/"
        )

    _rate_limit()

    params = {
        "key": PIXABAY_API_KEY,
        "q": query,
        "video_type": "film",
        "orientation": orientation,  # "vertical" for 9:16 TikTok
        "per_page": per_page,
        "safesearch": "true",
        "order": "popular",
    }

    response = requests.get(
        "https://pixabay.com/api/videos/",
        params=params,
        timeout=15,
    )

    if response.status_code != 200:
        raise Exception(f"Pixabay API error (HTTP {response.status_code}): {response.text[:200]}")

    data = response.json()
    hits = data.get("hits", [])

    # Filter by duration
    results = []
    for hit in hits:
        duration = hit.get("duration", 0)
        if min_duration <= duration <= max_duration:
            results.append({
                "id": hit["id"],
                "tags": hit.get("tags", ""),
                "duration": duration,
                "pageURL": hit.get("pageURL", ""),
                "videos": hit.get("videos", {}),
                "views": hit.get("views", 0),
                "downloads": hit.get("downloads", 0),
                "user": hit.get("user", ""),
            })

    return results


def fetch_best_clip(
    query: str,
    output_dir: str | None = None,
    orientation: str = "vertical",
    min_duration: int = 3,
    max_duration: int = 15,
    prefer_quality: str = "medium",  # "large" (1080p), "medium" (720p), "small" (360p)
    use_cache: bool = True,
    ai_tag: bool = True,
) -> dict | None:
    """
    Fetch the best matching stock video clip for a query.

    1. Check local cache first
    2. Search Pixabay if not cached
    3. Score results by tag relevance (quality gate: 0.55 minimum)
    4. Download best match
    5. Generate AI description for future matching
    6. Return clip metadata

    Returns:
        dict with keys: path, duration, tags, description, query, pixabay_id
        or None if no match found
    """
    cache_index = _load_cache_index()

    # ── Step 1: Check cache for a good match ──────────────────────
    if use_cache:
        candidates = []
        for clip_id, clip_data in cache_index.get("clips", {}).items():
            # Use the same smart scoring as fresh searches
            tag_score = _score_tags(clip_data.get("tags", ""), query)

            # Also score against the AI-generated description (more detailed than tags)
            clip_desc = clip_data.get("description", "").lower()
            desc_words = set(clip_desc.split())
            query_word_set = set(query.lower().split())
            desc_score = len(query_word_set & desc_words) / max(len(query_word_set), 1)

            # Weighted: tag scoring (smart) is primary, description is supplementary
            total_score = tag_score * 0.7 + desc_score * 0.3

            if total_score >= 0.60 and os.path.exists(clip_data.get("path", "")):
                candidates.append((total_score, clip_data))

        if candidates:
            # Pick the best match — variety comes from diverse queries
            candidates.sort(key=lambda x: x[0], reverse=True)
            best = candidates[0][1]
            logger.info(f"Cache hit for '{query}' (score={candidates[0][0]:.2f}): {best['path']}")
            return best

    # ── Step 2: Search Pixabay ────────────────────────────────────
    logger.info(f"Searching Pixabay for '{query}'...")
    try:
        results = search_pixabay(
            query=query,
            orientation=orientation,
            min_duration=min_duration,
            max_duration=max_duration,
        )
    except Exception as e:
        logger.error(f"Pixabay search failed: {e}")
        return None

    if not results:
        logger.info(f"No results for '{query}'")
        return None

    # ── Step 3: Score and pick best ───────────────────────────────
    scored = []
    for r in results:
        tag_score = _score_tags(r["tags"], query)
        popularity = min(r.get("views", 0) / 100000, 1.0)  # Normalize
        total = tag_score * 0.95 + popularity * 0.05  # Relevance first, popularity = tiebreaker
        scored.append((total, r))

    scored.sort(key=lambda x: x[0], reverse=True)

    # Quality gate: skip if best match is too weak
    # 0.65 threshold means: for a 2-word query, both words must match (or 1 exact + 1 related)
    # For a 3-word query, at least 2 must match exactly
    if scored[0][0] < 0.65:
        logger.info(f"Best score {scored[0][0]:.2f} < 0.65 for '{query}', skipping (top tags: '{scored[0][1]['tags'][:60]}')")
        return None

    # Pick the best match — variety comes from diverse queries
    _, chosen = scored[0]
    logger.info(f"Best match: pixabay_{chosen['id']} (score={scored[0][0]:.2f}, tags='{chosen['tags'][:50]}')")

    # ── Step 4: Download ──────────────────────────────────────────
    # Pick video quality
    videos = chosen.get("videos", {})
    video_url = None
    for quality in [prefer_quality, "medium", "large", "small"]:
        if quality in videos and videos[quality].get("url"):
            video_url = videos[quality]["url"]
            break

    if not video_url:
        logger.error(f"No downloadable video URL for clip {chosen['id']}")
        return None

    # Download to cache directory
    save_dir = Path(output_dir) if output_dir else CACHE_DIR / "clips"
    save_dir.mkdir(parents=True, exist_ok=True)

    filename = f"pixabay_{chosen['id']}_{prefer_quality}.mp4"
    save_path = str(save_dir / filename)

    # Skip download if already exists
    if not os.path.exists(save_path):
        logger.info(f"Downloading clip {chosen['id']}...")
        _rate_limit()
        vid_response = requests.get(video_url, timeout=60)
        if vid_response.status_code != 200:
            logger.error(f"Download failed (HTTP {vid_response.status_code})")
            return None
        with open(save_path, "wb") as f:
            f.write(vid_response.content)
        logger.info(f"Saved: {save_path}")

        # Auto-crop horizontal clips to vertical (9:16) for TikTok
        try:
            import subprocess as _sp
            probe = _sp.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", save_path],
                capture_output=True, text=True, timeout=10,
            )
            dims = probe.stdout.strip().split(",")
            if len(dims) == 2:
                w, h = int(dims[0]), int(dims[1])
                if w > h:
                    # Horizontal clip — center-crop to 9:16
                    crop_w = int(h * 9 / 16)
                    crop_x = (w - crop_w) // 2
                    cropped_path = save_path.replace(".mp4", "_vert.mp4")
                    _sp.run([
                        "ffmpeg", "-y", "-i", save_path,
                        "-vf", f"crop={crop_w}:{h}:{crop_x}:0,scale=720:1280",
                        "-c:v", "libx264", "-crf", "23", "-preset", "ultrafast",
                        "-an", cropped_path,
                    ], capture_output=True, timeout=30)
                    if os.path.exists(cropped_path) and os.path.getsize(cropped_path) > 1000:
                        os.remove(save_path)
                        os.rename(cropped_path, save_path)
                        logger.info(f"Cropped horizontal ({w}x{h}) to vertical 720x1280")
                    else:
                        logger.warning(f"Crop failed, using original ({w}x{h})")
                else:
                    logger.info(f"Already vertical: {w}x{h}")
        except Exception:
            pass
    else:
        logger.info(f"Already cached: {save_path}")

    # ── Step 5: AI description ────────────────────────────────────
    description = chosen["tags"]
    if ai_tag:
        description = _generate_ai_description(save_path, chosen["tags"])

    # ── Step 6: Cache the clip metadata ───────────────────────────
    clip_entry = {
        "path": save_path,
        "duration": chosen["duration"],
        "tags": chosen["tags"],
        "description": description,
        "query": query,
        "pixabay_id": chosen["id"],
        "pixabay_url": chosen.get("pageURL", ""),
        "user": chosen.get("user", ""),
        "cached_at": time.time(),
    }

    cache_index["clips"][str(chosen["id"])] = clip_entry
    _save_cache_index(cache_index)

    return clip_entry


def fetch_clips_for_script(
    script: dict,
    output_dir: str | None = None,
    orientation: str = "vertical",
    ai_tag: bool = True,
) -> list[dict | None]:
    """
    Fetch stock video clips for all slides in a script.

    Each slide should have a 'stock_query' field (set by script generator)
    describing what stock footage to find.

    Falls back to the slide's 'image_prompt' if no stock_query is set.

    Returns:
        List of clip dicts (or None for slides that should use other sources)
    """
    clips = []
    used_pixabay_ids = set()  # Track used IDs to prevent duplicate clips

    for i, slide in enumerate(script.get("slides", [])):
        source = slide.get("source", "ai_generated")

        # Only fetch stock for stock-type slides
        if source.startswith("stock:"):
            query = source.replace("stock:", "").strip()
        elif source.startswith("stock_video:"):
            query = source.replace("stock_video:", "").strip()
        elif source == "stock":
            # Use image_prompt as fallback search query
            query = slide.get("stock_query", slide.get("image_prompt", ""))
            # Simplify long prompts into search keywords
            if len(query) > 60:
                query = " ".join(query.split()[:6])
        else:
            # This slide uses a different source (screenshot, screen recording, AI image)
            clips.append(None)
            continue

        logger.info(f"Slide {i}: fetching '{query}'")

        # Try up to 3 times to find a clip we haven't used yet
        clip = None
        for attempt in range(3):
            clip = fetch_best_clip(
                query=query,
                output_dir=output_dir,
                orientation=orientation,
                ai_tag=ai_tag,
            )
            if clip and clip.get("pixabay_id") not in used_pixabay_ids:
                break
            elif clip and attempt < 2:
                # Got a duplicate, try a different query variation
                query = query + " " + random.choice(["close up", "wide shot", "lifestyle", "detail", "kitchen", "cooking"])
                logger.warning(f"Duplicate clip, retrying with '{query}'")
                clip = None

        if clip:
            used_pixabay_ids.add(clip.get("pixabay_id"))
        clips.append(clip)

    return clips


# ── CLI test ──────────────────────────────────────────────────────
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()

    logger.info("Testing stock video fetcher...")

    test_queries = [
        "woman cooking kitchen",
        "grocery shopping store",
        "family dinner table",
        "fridge opening food",
        "meal prep vegetables",
    ]

    for q in test_queries:
        logger.info(f"Query: '{q}'")
        result = fetch_best_clip(q, ai_tag=False)
        if result:
            logger.info(f"Found: {result['path']}")
            logger.info(f"Tags: {result['tags']}")
            logger.info(f"Duration: {result['duration']}s")
        else:
            logger.info("No results")
