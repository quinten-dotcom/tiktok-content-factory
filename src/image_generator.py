from __future__ import annotations

"""
Image Generator — Uses Flux (via fal.ai) to generate selfie-style images.

Supports two modes:
- flux_schnell: Fast and cheap ($0.003/image), slightly less consistent characters
- flux_kontext: Character-consistent ($0.04/image), uses a reference image to keep
  the same face across all slides
"""

import os
import time
import json
import requests
from pathlib import Path
from PIL import Image, ImageEnhance, ImageFilter
from io import BytesIO
from log_config import get_logger

try:
    import fal_client
except ImportError:
    fal_client = None

logger = get_logger(__name__)


def generate_image_schnell(
    prompt: str,
    output_path: str,
    width: int = 1080,
    height: int = 1920,
    api_key: str | None = None,
) -> str:
    """
    Generate an image using Flux Schnell via fal.ai.

    Fast, cheap ($0.003/image), good quality for TikTok.
    """
    key = api_key or os.environ.get("FAL_KEY")

    # Use synchronous endpoint (blocks until result is ready, ~3-5s for Schnell)
    response = requests.post(
        "https://fal.run/fal-ai/flux/schnell",
        headers={
            "Authorization": f"Key {key}",
            "Content-Type": "application/json",
        },
        json={
            "prompt": prompt,
            "image_size": {"width": width, "height": height},
            "num_images": 1,
            "enable_safety_checker": False,
        },
        timeout=120,
    )

    if response.status_code != 200:
        try:
            err = response.json()
        except Exception:
            err = response.text[:300]
        raise Exception(f"fal.ai Schnell error (HTTP {response.status_code}): {err}")

    result = response.json()

    # Check for error responses (e.g., locked account, exhausted balance)
    if "detail" in result:
        detail = result["detail"]
        if isinstance(detail, str) and "locked" in detail.lower():
            raise Exception("fal.ai account is locked — add credits at fal.ai/dashboard/billing")
        raise Exception(f"fal.ai error: {detail}")

    if "images" not in result:
        raise Exception(f"fal.ai returned no images: {json.dumps(result)[:200]}")
    image_url = result["images"][0]["url"]

    # Download and save
    img_response = requests.get(image_url)
    img = Image.open(BytesIO(img_response.content))
    img.save(output_path)

    return output_path


def generate_image_kontext(
    prompt: str,
    reference_image_path: str,
    output_path: str,
    api_key: str | None = None,
) -> str:
    """
    Generate a character-consistent image using Flux Kontext via fal.ai.

    Takes a reference image and places the same person in a new scene.
    More expensive ($0.04/image) but maintains facial consistency.
    """
    key = api_key or os.environ.get("FAL_KEY")

    # Upload reference image to fal.ai
    with open(reference_image_path, "rb") as f:
        ref_data = f.read()

    # Encode reference as base64 data URL
    import base64

    ref_b64 = base64.b64encode(ref_data).decode("utf-8")
    ref_url = f"data:image/png;base64,{ref_b64}"

    # Use synchronous endpoint (blocks until result is ready, ~10-20s for Kontext)
    response = requests.post(
        "https://fal.run/fal-ai/flux-pro/kontext",
        headers={
            "Authorization": f"Key {key}",
            "Content-Type": "application/json",
        },
        json={
            "prompt": prompt,
            "image_url": ref_url,
            "image_size": {"width": 1080, "height": 1920},
            "num_images": 1,
        },
        timeout=180,
    )

    if response.status_code != 200:
        try:
            err = response.json()
        except Exception:
            err = response.text[:300]
        raise Exception(f"fal.ai Kontext error (HTTP {response.status_code}): {err}")

    result = response.json()

    if "images" not in result:
        raise Exception(f"Kontext returned no images: {json.dumps(result)[:200]}")
    image_url = result["images"][0]["url"]

    img_response = requests.get(image_url)
    img = Image.open(BytesIO(img_response.content))
    img.save(output_path)

    return output_path


def _poll_fal_result(request_id: str, api_key: str, max_wait: int = 120, model_path: str = "fal-ai/flux/schnell") -> str:
    """Poll fal.ai queue until result is ready.

    Args:
        request_id: The fal.ai queue request ID
        api_key: fal.ai API key
        max_wait: Maximum seconds to wait before timing out
        model_path: The fal.ai model path (e.g. 'fal-ai/flux/schnell' or 'fal-ai/flux-pro/kontext')
    """
    start = time.time()
    while time.time() - start < max_wait:
        resp = requests.get(
            f"https://queue.fal.run/{model_path}/requests/{request_id}/status",
            headers={"Authorization": f"Key {api_key}"},
        )
        # Handle non-JSON responses from fal.ai (rate limits, server errors, etc.)
        try:
            status = resp.json()
        except Exception:
            logger.warning(f"fal.ai returned non-JSON (HTTP {resp.status_code}): {resp.text[:200]}")
            if resp.status_code == 401:
                raise Exception("fal.ai API key is invalid or expired. Check FAL_KEY in .env")
            if resp.status_code == 429:
                logger.info("fal.ai rate limited, waiting 10s...")
                time.sleep(10)
                continue
            time.sleep(3)
            continue

        if status.get("status") == "COMPLETED":
            # Fetch result
            result_resp = requests.get(
                f"https://queue.fal.run/{model_path}/requests/{request_id}",
                headers={"Authorization": f"Key {api_key}"},
            )
            try:
                result_data = result_resp.json()
            except Exception:
                raise Exception(f"fal.ai result response not JSON (HTTP {result_resp.status_code}): {result_resp.text[:200]}")
            if "images" not in result_data:
                raise Exception(f"fal.ai completed but no images returned: {json.dumps(result_data)[:200]}")
            return result_data["images"][0]["url"]
        elif status.get("status") == "FAILED":
            raise Exception(f"fal.ai image generation failed: {status.get('error', 'Unknown error')}")
        time.sleep(2)
    raise TimeoutError(f"fal.ai request {request_id} timed out after {max_wait}s")


def apply_color_grade(image_path: str, color_config: dict) -> str:
    """
    Apply consistent color grading to an image.
    Makes all slides in a video feel visually cohesive.
    """
    img = Image.open(image_path)

    # Warmth (shift toward orange/yellow)
    warmth = color_config.get("warmth", 1.0)
    if warmth != 1.0:
        enhancer = ImageEnhance.Color(img)
        img = enhancer.enhance(warmth)

    # Contrast
    contrast = color_config.get("contrast", 1.0)
    if contrast != 1.0:
        enhancer = ImageEnhance.Contrast(img)
        img = enhancer.enhance(contrast)

    # Saturation
    saturation = color_config.get("saturation", 1.0)
    if saturation != 1.0:
        enhancer = ImageEnhance.Color(img)
        img = enhancer.enhance(saturation)

    # Vignette
    vignette = color_config.get("vignette_strength", 0)
    if vignette > 0:
        img = _apply_vignette(img, vignette)

    img.save(image_path)
    return image_path


def _apply_vignette(img: Image.Image, strength: float) -> Image.Image:
    """Apply a subtle vignette effect."""
    import numpy as np

    width, height = img.size
    arr = np.array(img, dtype=np.float32)

    # Create radial gradient
    y, x = np.ogrid[:height, :width]
    cx, cy = width / 2, height / 2
    r = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
    r_max = np.sqrt(cx**2 + cy**2)
    vignette = 1 - strength * (r / r_max) ** 2

    # Apply to all channels
    for c in range(3):
        arr[:, :, c] *= vignette

    arr = np.clip(arr, 0, 255).astype(np.uint8)
    return Image.fromarray(arr)


def generate_reference_image(persona: dict, output_path: str, api_key: str | None = None) -> str:
    """
    Generate the initial reference image for a persona.
    This becomes the 'character sheet' that Kontext uses for consistency.
    """
    prompt = (
        f"{persona['image_prompt_prefix']}. "
        f"Looking directly at camera, warm natural smile, "
        f"simple clean background, soft natural lighting, "
        f"high quality portrait, shot on iPhone, 9:16 portrait orientation. "
        f"Photorealistic, no AI artifacts."
    )
    return generate_image_schnell(prompt, output_path, api_key=api_key)


def generate_images_for_script(
    script: dict,
    output_dir: str,
    app_config: dict,
    reference_image_path: str | None = None,
    engine: str = "flux_schnell",
    api_key: str | None = None,
) -> list[str]:
    """
    Generate all images for a single video script.

    Args:
        script: The script dict with slides containing image_prompts
        output_dir: Where to save images
        app_config: App config (for color grading)
        reference_image_path: Path to persona reference image (for Kontext mode)
        engine: "flux_schnell" or "flux_kontext"
        api_key: fal.ai API key

    Returns:
        List of image file paths in slide order
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    image_paths = []

    for i, slide in enumerate(script["slides"]):
        img_path = str(out / f"slide_{i:02d}.png")
        prompt = slide["image_prompt"]

        logger.info(f"    Generating image {i + 1}/{len(script['slides'])}...")

        if engine == "flux_kontext" and reference_image_path:
            generate_image_kontext(prompt, reference_image_path, img_path, api_key=api_key)
        else:
            generate_image_schnell(prompt, img_path, api_key=api_key)

        # Apply consistent color grading
        color_config = app_config.get("color_grade", {})
        if color_config:
            apply_color_grade(img_path, color_config)

        image_paths.append(img_path)

    return image_paths


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    # Test: generate a single reference image
    test_persona = {
        "image_prompt_prefix": "Portrait photo of a 24-year-old woman with shoulder-length brown hair, warm brown eyes, friendly smile",
    }
    logger.info("Generating test reference image...")
    generate_reference_image(test_persona, "output/test_reference.png")
    logger.info("Done!")
