from __future__ import annotations

"""
Script Generator — Uses Claude API to generate TikTok video scripts.

Each script includes: hook, slides with image prompts + text overlays + voiceover,
hashtags, and a TikTok caption.
"""

import json
import random
import anthropic
from pathlib import Path


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
{json.dumps(app_config['cta_variations'])}

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
- Each script must be meaningfully different — different angle, different hook, different topic

Output ONLY the JSON array. No markdown, no explanation."""

    return prompt


def generate_scripts(
    app_config: dict,
    count: int = 7,
    api_key: str | None = None,
    model: str = "claude-haiku-4-5-20251001",
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

    all_scripts = []

    for style, style_count in style_groups.items():
        persona = pick_persona(app_config)
        prompt = build_script_prompt(app_config, persona, style, count=style_count)

        response = client.messages.create(
            model=model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )

        response_text = response.content[0].text.strip()

        # Parse JSON — handle potential markdown wrapping
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]

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
        print(f"  Saved: {file_path.name}")

    return paths


if __name__ == "__main__":
    # Quick test
    import os
    from dotenv import load_dotenv

    load_dotenv()

    config = load_app_config("config/example_app.json")
    print(f"Generating scripts for: {config['app_name']}")
    print(f"Personas: {[p['name'] for p in config['personas']]}")

    scripts = generate_scripts(config, count=7)
    save_scripts(scripts, "output/scripts")
    print(f"\nGenerated {len(scripts)} scripts!")
