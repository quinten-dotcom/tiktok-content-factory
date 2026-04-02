from __future__ import annotations

"""
Script Generator — Uses Claude API to generate TikTok video scripts.

Each script includes: hook, slides with image prompts + text overlays + voiceover,
hashtags, and a TikTok caption.
"""

import json
import re
import random
import anthropic
from pathlib import Path
from log_config import get_logger

logger = get_logger(__name__)


def _strip_markdown_json(text: str) -> str:
    """Robustly extract JSON from Claude responses, handling markdown fences and extra text."""
    # Try markdown code fence first
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    # Find the outermost JSON array [...] or object {...}
    # This handles cases where Claude adds text before/after the JSON
    for open_char, close_char in [("[", "]"), ("{", "}")]:
        start = text.find(open_char)
        if start == -1:
            continue
        # Find the matching closing bracket
        depth = 0
        in_string = False
        escape_next = False
        for i in range(start, len(text)):
            c = text[i]
            if escape_next:
                escape_next = False
                continue
            if c == "\\":
                escape_next = True
                continue
            if c == '"' and not escape_next:
                in_string = not in_string
                continue
            if in_string:
                continue
            if c == open_char:
                depth += 1
            elif c == close_char:
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]

    return text


def load_app_config(config_path: str) -> dict:
    """Load an app's configuration from JSON."""
    with open(config_path, "r") as f:
        return json.load(f)


def pick_persona(app_config: dict) -> dict:
    """Randomly select a persona from the app's roster."""
    return random.choice(app_config["personas"])


def pick_video_style(app_config: dict) -> str:
    """Weighted random selection of video style."""
    styles = app_config["video_styles"]
    names = list(styles.keys())
    weights = list(styles.values())
    return random.choices(names, weights=weights, k=1)[0]


def pick_hashtags(app_config: dict, count: int = 4) -> list[str]:
    """Pick a mix of broad, medium, and niche hashtags."""
    h = app_config["hashtag_sets"]
    selected = []
    selected.append(random.choice(h["broad"]))
    selected.extend(random.sample(h["medium"], min(2, len(h["medium"]))))
    selected.append(random.choice(h["niche"]))
    return selected[:count]


def _format_pricing_block(app_config: dict) -> str:
    """Format pricing info for inclusion in script prompts."""
    pricing = app_config.get("pricing", {})
    if not pricing:
        return ""

    lines = ["\nPRICING (MUST be mentioned accurately in CTA slides):"]
    if pricing.get("price"):
        lines.append(f"- Price: {pricing['price']}")
    if pricing.get("annual_price"):
        lines.append(f"- Annual: {pricing['annual_price']}")
    if pricing.get("free_trial"):
        lines.append(f"- Free trial: {pricing['free_trial']}")
    if pricing.get("free_tier"):
        lines.append(f"- Free tier: {pricing['free_tier']}")
    if pricing.get("paid_tier"):
        lines.append(f"- Paid tier: {pricing['paid_tier']}")
    if pricing.get("price_note"):
        lines.append(f"- Note: {pricing['price_note']}")
    lines.append("- CRITICAL: When mentioning price or free trial, use the EXACT values above. Never guess or round.")
    return "\n".join(lines)


def _build_hybrid_prompt(app_config: dict, persona: dict, video_style: str, count: int, available_footage: list) -> str:
    """Build prompt for hybrid mode — stock footage + app recordings."""
    # Format footage with descriptions so Claude knows what each recording shows
    footage_lines = []
    for f in available_footage:
        if isinstance(f, dict):
            footage_lines.append(f"  - {f['filename']} ({f.get('duration', '?')}s) — {f.get('description', 'App recording')}")
        else:
            footage_lines.append(f"  - {f}")
    footage_list = "\n".join(footage_lines)

    return f"""You are a viral TikTok scriptwriter for {app_config['app_name']}: {app_config['app_description']}.

PERSONA: {persona['name']} — {persona['archetype']}

HYBRID VIDEO MODE — You have TWO types of visuals:

TYPE 1: REAL APP RECORDINGS (your uploaded screen recordings):
{footage_list}
- Use for slides showing the app in action: demos, features, results
- Set source to "video_footage:<EXACT filename from list above>" — COPY the filename EXACTLY as shown, including extension
- CRITICAL: Do NOT rename, rephrase, or modify the filename. Use it VERBATIM from the list above.
- Set clip_start and clip_end (seconds) to pick the best section of the recording
- Read the descriptions carefully — pick the recording whose description best matches what you're talking about
- These are authentic footage and build viewer trust

TYPE 2: STOCK FOOTAGE from Pixabay:
- Use for lifestyle/mood/reaction slides
- Set source to "stock:<2-3 simple words>"
- Pixabay search is LITERAL — use common nouns only, no adjectives or emotions
- GOOD: "stock:woman kitchen cooking", "stock:family dinner table", "stock:phone scrolling"
- BAD: "stock:overwhelmed stressed mom kitchen", "stock:happy family eating joyfully together"
- 2-3 words max. Simpler = better results.

MIX STRATEGY:
- Hook slide -> stock footage (eye-catching lifestyle scene)
- Demo slides -> real app recordings (show the actual app)
- Result/reaction slides -> stock footage (positive outcome)
- Typical: 2-3 stock clips + 1-2 app recordings per video

CONTENT PILLARS: {json.dumps(app_config.get('content_pillars', []))}

CTA OPTIONS (use one per video, vary them):
{json.dumps(app_config.get('cta_variations', ['Link in bio']))}
{_format_pricing_block(app_config)}

Generate exactly {count} scripts as a JSON array. Each script:
{{
  "title": "internal title",
  "video_style": "{video_style}",
  "persona_id": "{persona['id']}",
  "slides": [
    {{
      "slide_type": "hook|value|demo|cta|result",
      "source": "stock:keywords OR video_footage:filename",
      "clip_start": 0,
      "clip_end": 5,
      "voiceover": "what narrator says — MUST be a complete sentence",
      "image_prompt": "",
      "duration_seconds": 4
    }}
  ],
  "description": "TikTok caption",
  "has_voiceover": true
}}

RULES:
- Every voiceover MUST be a COMPLETE sentence. Never end mid-thought.
- Every slide MUST show something VISUALLY DIFFERENT.
- 4-6 slides per video, duration_seconds 3-6 each.
- Stock keywords: 2-3 simple common words ONLY.
- App recordings: match the description to what you're talking about.
- image_prompt = "" for all slides (no AI images).
- Total voiceover 15-30 seconds when spoken aloud.
- The CTA slide MUST mention the actual price or free trial accurately. Do NOT make up prices.

Output ONLY the JSON array."""


def _build_stock_prompt(app_config: dict, persona: dict, video_style: str, count: int) -> str:
    """Build prompt for stock-only mode — all Pixabay footage."""
    return f"""You are a viral TikTok scriptwriter for {app_config['app_name']}: {app_config['app_description']}.

PERSONA: {persona['name']} — {persona['archetype']}

STOCK VIDEO MODE — All visuals come from Pixabay stock footage.
Set source to "stock:<2-4 word search keywords>" for every slide.
Examples: "stock:woman cooking kitchen", "stock:grocery shopping aisle", "stock:family dinner table"

CONTENT PILLARS: {json.dumps(app_config.get('content_pillars', []))}

CTA OPTIONS (use one per video, vary them):
{json.dumps(app_config.get('cta_variations', ['Link in bio']))}
{_format_pricing_block(app_config)}

Generate exactly {count} scripts as a JSON array. Each script:
{{
  "title": "internal title",
  "video_style": "{video_style}",
  "persona_id": "{persona['id']}",
  "slides": [
    {{
      "slide_type": "hook|value|demo|cta|result",
      "source": "stock:2-4 word keywords",
      "voiceover": "what narrator says — MUST be a complete sentence",
      "image_prompt": "",
      "duration_seconds": 4
    }}
  ],
  "description": "TikTok caption",
  "has_voiceover": true
}}

RULES:
- Every voiceover MUST be a COMPLETE sentence. Never end mid-thought.
- Every slide MUST show something VISUALLY DIFFERENT.
- 4-6 slides per video, 3-6 seconds each.
- Stock keywords must be specific: "woman chopping vegetables" not just "cooking".
- image_prompt = "" for all slides.
- Total voiceover 15-30 seconds.
- The CTA slide MUST mention the actual price or free trial accurately. Do NOT make up prices.

Output ONLY the JSON array."""


def build_script_prompt(app_config: dict, persona: dict, video_style: str, count: int = 7) -> str:
    """Build the prompt that generates TikTok scripts."""

    style_instructions = {
        "story_narration": (
            "Story narration format: The persona shares a personal story or experience. "
            "Each slide shows them in a different scene as the story progresses. "
            "Should feel like a friend telling you something over coffee."
        ),
        "text_heavy_educational": (
            "Text-heavy educational format: Big bold text dominates each slide. "
            "Images are lifestyle/aesthetic backgrounds — slightly blurred or moody. "
            "The text carries all the information. Think 'fact cards' with voiceover."
        ),
        "split_screen_comparison": (
            "Before/after comparison format: Alternate between 'problem' slides and 'solution' slides. "
            "Problem slides show frustration/chaos. Solution slides show calm/organized (with the app). "
            "Strong visual contrast between the two states."
        ),
        "trending_sound_text_only": (
            "Trending sound format: NO voiceover for this one. The video uses a trending sound. "
            "All information is conveyed through text overlays on lifestyle images. "
            "Keep text punchy — max 5 words per slide. Fast-paced, 3 seconds per slide max."
        ),
        "app_demo_screenrecord": (
            "App demo format: Mix selfie-style images with app screenshot descriptions. "
            "The persona talks about a specific feature while showing what it looks like. "
            "Practical, straightforward, 'let me show you this' energy."
        ),
    }

    prompt = f"""You are a viral TikTok scriptwriter. You are writing for {app_config['app_name']}: {app_config['app_description']}.

PERSONA: {persona['name']} — {persona['archetype']}
Writing style: {json.dumps(persona['writing_style'])}

VIDEO STYLE: {video_style}
{style_instructions.get(video_style, '')}

CONTENT PILLARS (pick from these topics):
{json.dumps(app_config['content_pillars'])}

CTA OPTIONS (use one per video, vary them):
{json.dumps(app_config.get('cta_variations', ['Link in bio']))}
{_format_pricing_block(app_config)}

Generate exactly {count} unique TikTok video scripts as a JSON array.

For EACH script, output this exact structure:
{{
  "title": "internal title for tracking (not shown to viewers)",
  "video_style": "{video_style}",
  "persona_id": "{persona['id']}",
  "hook_text": "the text shown on screen in the first 2 seconds — MUST stop the scroll, max 8 words",
  "slides": [
    {{
      "image_prompt": "detailed description of the selfie-style photo for this slide. ALWAYS include: '{persona['image_prompt_prefix']}'. Add the scene, clothing, setting, lighting. Specify 'selfie angle, shot on iPhone, candid, natural lighting, 9:16 portrait orientation'. Make it slightly imperfect — not studio-perfect.",
      "text_overlay": "bold text shown on this slide — max 8 words, punchy",
      "voiceover": "what the narrator says during this slide — conversational, 5-15 seconds when spoken"
    }}
  ],
  "description": "TikTok caption — casual, 1-2 sentences, with emoji",
  "has_voiceover": true
}}

RULES:
- Each script has 4-7 slides
- Total voiceover per video should be 20-45 seconds when spoken aloud
- The hook slide is the MOST important — it must create curiosity, urgency, or shock in under 2 seconds of reading
- Image prompts must describe the SAME person (the persona) in every slide for consistency
- Vary the settings/scenes across slides (desk, kitchen, outside, gym, cozy room, etc.)
- For "{video_style}" specifically: {"set has_voiceover to false and make text_overlay carry ALL information" if video_style == "trending_sound_text_only" else "include natural, conversational voiceover"}
- CTA should appear on the last slide
- The CTA slide MUST mention the actual price or free trial accurately. Do NOT make up prices.
- Each script must be meaningfully different — different angle, different hook, different topic

Output ONLY the JSON array. No markdown, no explanation."""

    return prompt


def generate_scripts(
    app_config: dict,
    count: int = 7,
    api_key: str | None = None,
    model: str = "claude-haiku-4-5-20251001",
    available_screenshots: list | None = None,
) -> list[dict]:
    """
    Generate TikTok video scripts using Claude API.

    Args:
        app_config: The app's configuration dict
        count: Number of scripts to generate
        api_key: Anthropic API key (or set ANTHROPIC_API_KEY env var)
        model: Claude model to use (Haiku is cheapest for bulk)

    Returns:
        List of script dicts ready for the pipeline
    """
    client = anthropic.Anthropic(api_key=api_key)

    # We generate scripts in style-grouped batches for variety
    # Pick styles weighted by config
    styles_for_batch = []
    for _ in range(count):
        styles_for_batch.append(pick_video_style(app_config))

    # Group by style to batch prompts efficiently
    style_groups = {}
    for style in styles_for_batch:
        style_groups[style] = style_groups.get(style, 0) + 1

    # Check for hybrid/stock mode
    hybrid_mode = app_config.get("_hybrid_mode", False)
    stock_mode = app_config.get("_stock_mode", False)

    # Build available footage list with descriptions for hybrid mode
    available_footage = []
    if hybrid_mode:
        footage_descs = app_config.get("_footage_descriptions", {})
        footage_durs = app_config.get("_footage_durations", {})
        footage_names = app_config.get("_available_footage", [])

        if footage_names:
            for name in footage_names:
                available_footage.append({
                    "filename": name,
                    "description": footage_descs.get(name, "App screen recording"),
                    "duration": round(footage_durs.get(name, 10), 1),
                })
        else:
            # Fallback: scan filesystem
            from pathlib import Path as _Path
            app_slug = app_config["app_name"].lower().replace(" ", "_")
            app_slug = "".join(c for c in app_slug if c.isalnum() or c == "_")
            media_dir = _Path(__file__).parent.parent / "data" / "output" / app_slug / "media"
            if media_dir.exists():
                for f in media_dir.iterdir():
                    if f.suffix.lower() in (".mov", ".mp4"):
                        available_footage.append({"filename": f.name, "description": "App screen recording", "duration": 10})

    all_scripts = []

    for style, style_count in style_groups.items():
        persona = pick_persona(app_config)

        if hybrid_mode and available_footage:
            prompt = _build_hybrid_prompt(app_config, persona, style, style_count, available_footage)
        elif stock_mode:
            prompt = _build_stock_prompt(app_config, persona, style, style_count)
        else:
            prompt = build_script_prompt(app_config, persona, style, count=style_count)

        response = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = response.content[0].text.strip()

        # Parse JSON — handle potential markdown wrapping
        response_text = _strip_markdown_json(response_text)

        scripts = json.loads(response_text)

        # Enrich each script with metadata
        for script in scripts:
            script["app_name"] = app_config["app_name"]
            script["hashtags"] = pick_hashtags(app_config)
            script["persona"] = persona

        all_scripts.extend(scripts)

    return all_scripts[:count]


def save_scripts(scripts: list[dict], output_dir: str) -> list[str]:
    """Save generated scripts to JSON files. Returns list of file paths."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    paths = []
    for i, script in enumerate(scripts):
        file_path = output_path / f"script_{i:03d}_{script.get('persona_id', 'unknown')}.json"
        with open(file_path, "w") as f:
            json.dump(script, f, indent=2)
        paths.append(str(file_path))
        logger.info(f"Saved: {file_path.name}")

    return paths


if __name__ == "__main__":
    # Quick test
    import os
    from dotenv import load_dotenv

    load_dotenv()

    config = load_app_config("config/example_app.json")
    logger.info(f"Generating scripts for: {config['app_name']}")
    logger.info(f"Personas: {[p['name'] for p in config['personas']]}")

    scripts = generate_scripts(config, count=7)
    save_scripts(scripts, "output/scripts")
    logger.info(f"Generated {len(scripts)} scripts!")
