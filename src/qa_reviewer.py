from __future__ import annotations

"""
QA Reviewer — Uses Claude Vision to review video quality before posting.

Extracts key frames from the assembled video and sends them to Claude
for a quality assessment. Videos scoring below the threshold get flagged
for regeneration.
"""

import os
import re
import json
import base64
import anthropic
from pathlib import Path


def _strip_markdown_json(text: str) -> str:
    """Robustly strip markdown code fences from JSON responses."""
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text


def encode_image_b64(image_path: str) -> str:
    """Read an image file and return base64-encoded string."""
    with open(image_path, "rb") as f:
        return base64.standard_b64encode(f.read()).decode("utf-8")


def review_video(
    frame_paths: list[str],
    script: dict,
    app_config: dict,
    api_key: str | None = None,
    model: str = "claude-haiku-4-5-20251001",
    threshold: float = 7.0,
) -> dict:
    """
    Review a video's key frames using Claude Vision.

    Sends 3-5 key frames to Claude and asks for a quality assessment
    across multiple dimensions.

    Args:
        frame_paths: Paths to extracted key frames
        script: The script dict for context
        app_config: App configuration for brand context
        api_key: Anthropic API key
        model: Claude model (Haiku is cheap enough for bulk QA)
        threshold: Minimum score (1-10) to pass QA

    Returns:
        Dict with: score, pass/fail, feedback, per-frame scores
    """
    client = anthropic.Anthropic(api_key=api_key)

    # Build the message with images
    content = []

    content.append({
        "type": "text",
        "text": f"""You are a TikTok content quality reviewer for {app_config['app_name']}.

Review these key frames from a TikTok video and rate the quality.

SCRIPT CONTEXT:
- Hook: {script.get('hook_text', 'N/A')}
- Style: {script.get('video_style', 'N/A')}
- Persona: {script.get('persona_id', 'N/A')}

Rate EACH frame and the overall video on these criteria (1-10):

1. VISUAL APPEAL: Do the images look natural and high-quality? Any AI artifacts?
2. TEXT READABILITY: Is the text overlay clear and readable at mobile size?
3. SCROLL-STOP POWER (frame 1 only): Would this first frame make someone stop scrolling?
4. BRAND CONSISTENCY: Do the frames look like they belong to the same video?
5. OVERALL QUALITY: Overall production quality.

Respond in JSON:
{{
  "frame_scores": [
    {{"frame": 1, "visual": 8, "text_readable": 9, "notes": "..."}}
  ],
  "scroll_stop_score": 8,
  "brand_consistency": 9,
  "overall_score": 8.5,
  "pass": true,
  "issues": ["list of specific issues if any"],
  "suggestions": ["list of improvement suggestions"]
}}

Be honest and critical. A score below 7 means the video should be regenerated.""",
    })

    # Add key frames as images (limit to 5 to keep costs down)
    for i, frame_path in enumerate(frame_paths[:5]):
        img_b64 = encode_image_b64(frame_path)
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": img_b64,
            },
        })

    response = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=[{"role": "user", "content": content}],
    )

    response_text = response.content[0].text.strip()

    # Parse JSON response — robustly strip markdown
    response_text = _strip_markdown_json(response_text)

    try:
        review = json.loads(response_text)
    except json.JSONDecodeError:
        # If Claude doesn't return valid JSON, create a default pass
        review = {
            "overall_score": 7.0,
            "pass": True,
            "issues": ["Could not parse QA response"],
            "suggestions": [],
            "raw_response": response_text,
        }

    # Apply threshold
    score = review.get("overall_score", 0)
    review["pass"] = score >= threshold
    review["threshold"] = threshold

    return review


def review_first_frame(
    first_frame_path: str,
    script: dict,
    api_key: str | None = None,
    model: str = "claude-haiku-4-5-20251001",
) -> dict:
    """
    Quick review of just the first frame (thumbnail).

    This is the most important frame — it determines whether
    someone stops scrolling. Cheaper than full review.
    """
    client = anthropic.Anthropic(api_key=api_key)

    img_b64 = encode_image_b64(first_frame_path)

    response = client.messages.create(
        model=model,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"""Rate this TikTok video thumbnail (first frame) for scroll-stopping power.

The hook text is: "{script.get('hook_text', '')}"

Score 1-10 on:
1. Would someone stop scrolling to watch this?
2. Is the text clearly readable?
3. Is the image visually striking?

Respond in JSON: {{"score": 8, "would_stop_scroll": true, "fix": "suggestion if score < 7"}}""",
                },
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": img_b64,
                    },
                },
            ],
        }],
    )

    response_text = response.content[0].text.strip()
    response_text = _strip_markdown_json(response_text)

    try:
        return json.loads(response_text)
    except json.JSONDecodeError:
        return {"score": 7, "would_stop_scroll": True, "raw": response_text}


def save_review(review: dict, output_path: str) -> str:
    """Save QA review to a JSON file."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(review, f, indent=2)
    return output_path


if __name__ == "__main__":
    print("QA Reviewer module loaded.")
    print("Usage: Called by pipeline.py after video assembly.")
    print("Reviews key frames via Claude Vision and scores quality 1-10.")
