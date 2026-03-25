from __future__ import annotations
"""
Config Generator — Creates a full app config from just a name + description.

Uses Claude to generate everything:
- Content pillars and strategy
- 3 persona characters with appearances, voices, and writing styles
- Hashtag sets
- CTA variations
- Text styling and color grading
- Video style distribution

You provide: app name + one-line description
Claude provides: everything else
"""

import json
import re
import anthropic
import os
from pathlib import Path


CONFIG_GENERATION_PROMPT = """You are a TikTok marketing strategist. Given an app name and description, generate a COMPLETE content strategy and configuration.

APP NAME: {app_name}
APP DESCRIPTION: {app_description}
{folder_context_prompt}

Generate a full JSON configuration. Be creative and specific. Think about what would actually go viral on TikTok for this type of app.

Return ONLY valid JSON matching this exact structure:

{{
  "app_name": "{app_name}",
  "app_description": "{app_description}",
  "tiktok_handle": "@suggested_handle",
  "niche": "one word niche category",
  "app_store_url": "",
  "play_store_url": "",
  "link_in_bio_url": "",

  "ica": {{
    "target_audience": "Specific demographic: age range, occupation/lifestyle, key characteristics",
    "pain_points": "3-4 specific pain points these users have that the app addresses",
    "desired_outcome": "What they want to achieve — be specific and emotional",
    "tone": "How the brand should speak to them — with example phrases",
    "hook_style": "What type of hooks grab this audience (curiosity gaps, bold claims, etc.)",
    "platforms": "Where this audience hangs out online besides TikTok"
  }},

  "content_pillars": [
    "5 specific content topics that would perform well on TikTok for this app",
    "be specific and creative, not generic",
    "think about what problems the app solves",
    "think about what emotions it triggers",
    "think about trending formats that fit"
  ],

  "video_styles": {{
    "story_narration": 0.30,
    "text_heavy_educational": 0.25,
    "split_screen_comparison": 0.15,
    "trending_sound_text_only": 0.15,
    "app_demo_screenrecord": 0.15
  }},

  "personas": [
    {{
      "id": "lowercase_first_name",
      "name": "First Name",
      "archetype": "The [Descriptive Role]",
      "description": "Detailed physical description: age, hair color/style, eye color, typical clothing, aesthetic vibe. Be specific enough that an AI image generator would produce consistent results.",
      "image_prompt_prefix": "Portrait photo of a [age]-year-old [gender] with [hair], [eyes], [distinguishing features], natural skin texture",
      "voice_config": {{
        "elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM",
        "kokoro_voice": "af_sarah",
        "speaking_speed": 1.1
      }},
      "writing_style": {{
        "tone": "describe the tone",
        "energy": "low/medium/high",
        "vocabulary": "describe vocab style with example phrases they'd use",
        "humor": "describe humor style",
        "avoid": ["list of things this persona would never say"]
      }}
    }},
    {{
      "id": "second_persona",
      "name": "Second Name",
      "archetype": "The [Different Role]",
      "description": "Different from persona 1 — different age, look, vibe",
      "image_prompt_prefix": "Portrait photo of a ...",
      "voice_config": {{
        "elevenlabs_voice_id": "29vD33N1CtxCmqQRPOHJ",
        "kokoro_voice": "am_michael",
        "speaking_speed": 1.15
      }},
      "writing_style": {{
        "tone": "different from persona 1",
        "energy": "different level",
        "vocabulary": "different style with example phrases",
        "humor": "different humor",
        "avoid": ["different avoidances"]
      }}
    }},
    {{
      "id": "third_persona",
      "name": "Third Name",
      "archetype": "The [Third Role]",
      "description": "Different from both others — contrasting look and vibe",
      "image_prompt_prefix": "Portrait photo of a ...",
      "voice_config": {{
        "elevenlabs_voice_id": "EXAVITQu4vr4xnSDxMaL",
        "kokoro_voice": "af_bella",
        "speaking_speed": 1.0
      }},
      "writing_style": {{
        "tone": "different from others",
        "energy": "different level",
        "vocabulary": "different style with example phrases",
        "humor": "different humor",
        "avoid": ["different avoidances"]
      }}
    }}
  ],

  "cta_variations": [
    "4 different natural-sounding calls to action for the link in bio",
    "vary between casual and direct",
    "example: 'Link in bio if you want to try it'",
    "example: 'Go grab it — link in bio'"
  ],

  "hashtag_sets": {{
    "broad": ["#3-4 broad hashtags for the general niche"],
    "medium": ["#4-5 medium-specificity hashtags"],
    "niche": ["#3-4 very specific hashtags for this exact app type"]
  }},

  "color_grade": {{
    "warmth": 1.08,
    "contrast": 1.05,
    "saturation": 0.95,
    "vignette_strength": 0.15
  }},

  "text_style": {{
    "font": "Montserrat-Black",
    "hook_font_size": 72,
    "body_font_size": 56,
    "subtitle_font_size": 44,
    "text_color": "#FFFFFF",
    "text_stroke_color": "#000000",
    "text_stroke_width": 3,
    "highlight_color": "#FFD700",
    "subtitle_bg_color": "rgba(0,0,0,0.6)",
    "subtitle_bg_radius": 12
  }}
}}

IMPORTANT RULES:
- Make content_pillars SPECIFIC to this app, not generic marketing fluff
- Personas should feel like real TikTok creators, not corporate characters
- Hashtags should be ones that actually exist and have volume on TikTok
- CTAs should sound natural, like a real person talking
- The tiktok_handle should be catchy and available-looking (short, memorable)
- Mix persona genders and ages for variety
- Make the writing styles genuinely different from each other

Output ONLY the JSON. No markdown, no explanation, no code blocks."""


def _strip_markdown_json(text: str) -> str:
    """Robustly strip markdown code fences from JSON responses."""
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def generate_app_config(
    app_name: str,
    app_description: str,
    folder_context: dict = None,
    api_key: str = None,
    model: str = "claude-sonnet-4-20250514",
) -> dict:
    """
    Generate a complete app configuration from just a name and description.

    Uses Claude Sonnet (not Haiku) for this since it's a one-time creative task
    and quality matters more than cost here.

    Args:
        app_name: Name of the app
        app_description: One-line description of the app
        folder_context: Optional dict with 'images' and 'documents' lists containing
                       file context to enhance the generation
        api_key: Anthropic API key (uses env var if not provided)
        model: Claude model to use
    """
    client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"))

    # Build folder context prompt from uploaded documents
    folder_context_prompt = ""
    if folder_context:
        documents = folder_context.get("documents", [])
        images = folder_context.get("images", [])
        if documents or images:
            folder_context_prompt = "\nAPP CONTEXT (from uploaded files):\n"
            if documents:
                folder_context_prompt += "Documents:\n"
                for doc in documents:
                    folder_context_prompt += f"- {doc['filename']}:\n{doc['content']}\n\n"
            if images:
                folder_context_prompt += f"({len(images)} screenshots/images included)\n"

    prompt = CONFIG_GENERATION_PROMPT.format(
        app_name=app_name,
        app_description=app_description,
        folder_context_prompt=folder_context_prompt,
    )

    # Build message with images if provided (sent to Claude Vision)
    message_content = [{"type": "text", "text": prompt}]

    if folder_context:
        images = folder_context.get("images", [])
        for img in images:
            message_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": img["base64"],
                },
            })

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": message_content}],
    )

    response_text = response.content[0].text.strip()

    # Clean up potential markdown wrapping (robust approach)
    response_text = _strip_markdown_json(response_text)

    config = json.loads(response_text)
    return config


def save_app_config(config: dict, config_dir: str = "config") -> str:
    """Save the generated config to a JSON file."""
    Path(config_dir).mkdir(parents=True, exist_ok=True)

    slug = config["app_name"].lower().replace(" ", "_").replace("-", "_")
    # Remove any non-alphanumeric characters except underscore
    slug = "".join(c for c in slug if c.isalnum() or c == "_")

    path = f"{config_dir}/{slug}.json"
    with open(path, "w") as f:
        json.dump(config, f, indent=2)

    print(f"Config saved: {path}")
    return path


def onboard_app(app_name: str, app_description: str, folder_path: str = None, api_key: str = None) -> str:
    """
    Full onboard: generate config from name + description, optionally with folder context, and save it.

    Returns the path to the saved config file.
    """
    print(f"\nGenerating full TikTok strategy for: {app_name}")
    print(f"Description: {app_description}")

    folder_context = None
    if folder_path:
        print(f"Reading app assets from: {folder_path}")
        folder_context = read_folder_context(folder_path)
        if folder_context.get("documents") or folder_context.get("images"):
            print(f"Found {len(folder_context.get('documents', []))} documents and {len(folder_context.get('images', []))} images")

    print("Thinking about content pillars, personas, hashtags, tone...")

    config = generate_app_config(app_name, app_description, folder_context=folder_context, api_key=api_key)

    path = save_app_config(config)

    # Print summary
    print(f"\n{'='*50}")
    print(f"  APP: {config['app_name']}")
    print(f"  Handle: {config['tiktok_handle']}")
    print(f"  Niche: {config['niche']}")
    print(f"{'='*50}")
    print(f"\n  Content Pillars:")
    for p in config.get("content_pillars", []):
        print(f"    - {p}")
    print(f"\n  Personas:")
    for p in config.get("personas", []):
        print(f"    - {p['name']} — {p['archetype']}")
    print(f"\n  CTAs:")
    for c in config.get("cta_variations", []):
        print(f"    - {c}")
    print(f"\n  Config saved to: {path}")
    print(f"  Ready for: python3 pipeline.py generate --app {path}")

    return path


def read_folder_context(folder_path: str) -> dict:
    """
    Read files from a folder and return structured context.

    Returns:
        dict with 'documents' list (filename, content) and 'images' list (filename, base64)
    """
    import base64

    folder = Path(folder_path)
    if not folder.exists():
        return {"documents": [], "images": []}

    documents = []
    images = []

    for file_path in folder.rglob("*"):
        if not file_path.is_file():
            continue

        # Read text files
        if file_path.suffix in [".txt", ".md", ".json"]:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                documents.append({
                    "filename": file_path.name,
                    "content": content[:2000],  # Limit to 2000 chars per file
                })
            except Exception as e:
                print(f"Warning: Could not read {file_path}: {e}")

        # Read image files
        elif file_path.suffix.lower() in [".png", ".jpg", ".jpeg", ".gif", ".webp"]:
            try:
                with open(file_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                images.append({
                    "filename": file_path.name,
                    "base64": b64,
                })
            except Exception as e:
                print(f"Warning: Could not read image {file_path}: {e}")

    return {
        "documents": documents[:5],  # Limit to 5 documents
        "images": images[:3],  # Limit to 3 images
    }


if __name__ == "__main__":
    import sys
    from dotenv import load_dotenv
    load_dotenv()

    if len(sys.argv) < 3:
        print("Usage: python3 config_generator.py \"App Name\" \"One-line description\"")
        sys.exit(1)

    name = sys.argv[1]
    desc = sys.argv[2]
    onboard_app(name, desc)
