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
from log_config import get_logger

logger = get_logger(__name__)


def _strip_markdown_json(text: str) -> str:
    """Robustly extract JSON from Claude responses, handling markdown fences and extra text."""
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()
    for open_char, close_char in [("{", "}"), ("[", "]")]:
        start = text.find(open_char)
        if start == -1:
            continue
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
    post_type: str | None = None,
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
- Correct price: {app_config.get('pricing', {}).get('price', 'N/A')}
- Free trial: {app_config.get('pricing', {}).get('free_trial', 'N/A')}

Rate EACH frame and the overall video on these criteria (1-10):

1. VISUAL APPEAL: Do the images look natural and high-quality? Any AI artifacts?
2. TEXT READABILITY: Is the text overlay clear and readable at mobile size?
3. SCROLL-STOP POWER (frame 1 only): Would this first frame make someone stop scrolling?
4. BRAND CONSISTENCY: Do the frames look like they belong to the same video?
5. PRICING ACCURACY: If any text shows a price, does it match the correct price above? Flag any wrong prices as a critical issue.
6. OVERALL QUALITY: Overall production quality.

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
    cleaned_text = _strip_markdown_json(response_text)

    review = None
    try:
        review = json.loads(cleaned_text)
    except json.JSONDecodeError:
        pass

    # Retry: If first parse failed, try asking Claude for just the score
    if review is None:
        logger.warning("JSON parse failed, retrying with simpler prompt...")
        try:
            retry_response = client.messages.create(
                model=model,
                max_tokens=256,
                messages=[
                    {"role": "user", "content": content},
                    {"role": "assistant", "content": response_text},
                    {"role": "user", "content": 'Your response was not valid JSON. Please respond with ONLY a JSON object like: {"overall_score": 7.5, "pass": true, "issues": [], "suggestions": []}'},
                ],
            )
            retry_text = _strip_markdown_json(retry_response.content[0].text.strip())
            review = json.loads(retry_text)
            logger.info("Retry succeeded — got valid JSON")
        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"Retry also failed: {e}")

    # If ALL parsing failed, default to FAIL (not pass!) — don't let broken reviews through
    if review is None:
        review = {
            "overall_score": 0,
            "pass": False,
            "issues": ["QA review could not be parsed — defaulting to FAIL for safety"],
            "suggestions": ["Re-run QA on this video manually"],
            "raw_response": response_text[:500],
        }
        logger.warning("Could not parse review — defaulting to FAIL")

    # Coerce overall_score to float (Claude sometimes returns strings or ints)
    raw_score = review.get("overall_score", 0)
    try:
        score = float(raw_score)
    except (ValueError, TypeError):
        score = 0.0
        logger.warning(f"overall_score was not a number ({raw_score!r}), treating as 0")

    review["overall_score"] = score
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
    cleaned_text = _strip_markdown_json(response_text)

    try:
        result = json.loads(cleaned_text)
        # Coerce score to float
        raw_score = result.get("score", 0)
        try:
            result["score"] = float(raw_score)
        except (ValueError, TypeError):
            result["score"] = 0.0
        return result
    except json.JSONDecodeError:
        # Default to FAIL — don't auto-pass unparseable reviews
        return {"score": 0, "would_stop_scroll": False, "raw": response_text[:500],
                "issues": ["Could not parse first-frame review — defaulting to fail"]}


def save_review(review: dict, output_path: str) -> str:
    """Save QA review to a JSON file."""
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(review, f, indent=2)
    return output_path


if __name__ == "__main__":
    logger.info("QA Reviewer module loaded.")
    logger.info("Usage: Called by pipeline.py after video assembly.")
    logger.info("Reviews key frames via Claude Vision and scores quality 1-10.")
