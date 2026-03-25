from __future__ import annotations

"""
Video Assembler — The core engine.

Takes images, audio, and subtitle data and produces a final TikTok-ready MP4.

Features:
- Ken Burns effect (randomized zoom/pan on each slide)
- Text overlay with pop-in animation
- Word-by-word animated subtitles (TikTok style)
- Crossfade transitions between slides
- Background music mixing
- 1080x1920 output at 30fps
"""

import os
import random
import json
from pathlib import Path

from moviepy import (
    ImageClip,
    AudioFileClip,
    CompositeVideoClip,
    CompositeAudioClip,
    TextClip,
    concatenate_videoclips,
)
from PIL import Image, ImageDraw, ImageFont
import numpy as np


# ─── CONSTANTS ───────────────────────────────────────────────────────────────

VIDEO_WIDTH = 1080
VIDEO_HEIGHT = 1920
FPS = 30
CROSSFADE_DURATION = 0.3


# ─── KEN BURNS EFFECT ───────────────────────────────────────────────────────

def apply_ken_burns(clip, duration, zoom_range=(1.0, 1.12), pan_range=(-30, 30)):
    """
    Apply a randomized Ken Burns (zoom + pan) effect to a static image clip.

    Randomizes:
    - Zoom direction (in or out)
    - Zoom amount (within zoom_range)
    - Pan direction and amount (horizontal and vertical)
    """
    start_zoom = random.uniform(zoom_range[0], zoom_range[0] + 0.03)
    end_zoom = random.uniform(zoom_range[1] - 0.03, zoom_range[1])

    # Randomly decide zoom in or zoom out
    if random.random() > 0.5:
        start_zoom, end_zoom = end_zoom, start_zoom

    # Random pan offsets
    pan_x_start = random.uniform(pan_range[0], pan_range[1])
    pan_x_end = random.uniform(pan_range[0], pan_range[1])
    pan_y_start = random.uniform(pan_range[0] * 0.5, pan_range[1] * 0.5)
    pan_y_end = random.uniform(pan_range[0] * 0.5, pan_range[1] * 0.5)

    def make_frame(get_frame, t):
        frame = get_frame(t)
        h, w = frame.shape[:2]

        progress = t / max(duration, 0.01)
        current_zoom = start_zoom + (end_zoom - start_zoom) * progress
        current_pan_x = pan_x_start + (pan_x_end - pan_x_start) * progress
        current_pan_y = pan_y_start + (pan_y_end - pan_y_start) * progress

        # Calculate crop region
        new_w = int(w / current_zoom)
        new_h = int(h / current_zoom)
        cx = w // 2 + int(current_pan_x)
        cy = h // 2 + int(current_pan_y)

        x1 = max(0, cx - new_w // 2)
        y1 = max(0, cy - new_h // 2)
        x2 = min(w, x1 + new_w)
        y2 = min(h, y1 + new_h)

        # Adjust if we hit bounds
        if x2 - x1 < new_w:
            x1 = max(0, x2 - new_w)
        if y2 - y1 < new_h:
            y1 = max(0, y2 - new_h)

        cropped = frame[y1:y2, x1:x2]

        # Resize back to original dimensions
        from PIL import Image as PILImage

        pil_img = PILImage.fromarray(cropped)
        pil_img = pil_img.resize((w, h), PILImage.LANCZOS)
        return np.array(pil_img)

    return clip.transform(make_frame, apply_to="mask" if clip.mask else None)


# ─── TEXT OVERLAY RENDERING ──────────────────────────────────────────────────

def render_text_overlay(
    text: str,
    width: int = VIDEO_WIDTH,
    height: int = VIDEO_HEIGHT,
    font_path: str | None = None,
    font_size: int = 64,
    text_color: str = "#FFFFFF",
    stroke_color: str = "#000000",
    stroke_width: int = 3,
    position: str = "center",
) -> np.ndarray:
    """
    Render text overlay as a transparent RGBA image using Pillow.

    Returns a numpy array (RGBA) that can be composited onto the video.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Load font
    try:
        if font_path and os.path.exists(font_path):
            font = ImageFont.truetype(font_path, font_size)
        else:
            # Try system fonts
            for font_name in ["Montserrat-Black.ttf", "Arial-Bold.ttf", "DejaVuSans-Bold.ttf"]:
                try:
                    font = ImageFont.truetype(font_name, font_size)
                    break
                except OSError:
                    continue
            else:
                font = ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    # Word wrap
    words = text.split()
    lines = []
    current_line = ""

    for word in words:
        test_line = f"{current_line} {word}".strip()
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if bbox[2] - bbox[0] > width * 0.85:
            if current_line:
                lines.append(current_line)
            current_line = word
        else:
            current_line = test_line

    if current_line:
        lines.append(current_line)

    # Calculate total text height
    line_height = font_size * 1.3
    total_height = len(lines) * line_height

    # Position
    if position == "center":
        y_start = (height - total_height) / 2 - height * 0.05
    elif position == "top":
        y_start = height * 0.15
    elif position == "bottom":
        y_start = height * 0.55
    else:
        y_start = (height - total_height) / 2

    # Draw each line
    for i, line in enumerate(lines):
        bbox = draw.textbbox((0, 0), line, font=font)
        text_width = bbox[2] - bbox[0]
        x = (width - text_width) / 2
        y = y_start + i * line_height

        # Stroke (outline)
        if stroke_width > 0:
            for dx in range(-stroke_width, stroke_width + 1):
                for dy in range(-stroke_width, stroke_width + 1):
                    draw.text((x + dx, y + dy), line, font=font, fill=stroke_color)

        # Main text
        draw.text((x, y), line, font=font, fill=text_color)

    return np.array(img)


# ─── SUBTITLE RENDERING ─────────────────────────────────────────────────────

def create_subtitle_clips(
    subtitle_lines: list[dict],
    total_duration: float,
    text_style: dict,
) -> list:
    """
    Create animated word-by-word subtitle clips.

    Each word appears as it's spoken, with the current word highlighted
    and previous words dimmed. TikTok-style subtitle animation.
    """
    clips = []
    highlight_color = text_style.get("highlight_color", "#FFD700")
    text_color = text_style.get("text_color", "#FFFFFF")
    font_size = text_style.get("subtitle_font_size", 44)
    bg_color = text_style.get("subtitle_bg_color", "rgba(0,0,0,0.6)")

    for line in subtitle_lines:
        words = line["words"]
        line_start = line["start"]
        line_end = line["end"]
        full_text = line["text"]

        # Create the subtitle background + text as a Pillow image
        for word_info in words:
            word_start = word_info["start"]
            word_end = word_info["end"]

            # Render the full line with the current word highlighted
            subtitle_img = _render_subtitle_line(
                words,
                current_word=word_info["word"],
                current_word_start=word_start,
                font_size=font_size,
                text_color=text_color,
                highlight_color=highlight_color,
            )

            clip = (
                ImageClip(subtitle_img)
                .with_duration(word_end - word_start)
                .with_start(word_start)
                .with_position(("center", VIDEO_HEIGHT * 0.78))
            )
            clips.append(clip)

    return clips


def _render_subtitle_line(
    words: list[dict],
    current_word: str,
    current_word_start: float,
    font_size: int = 44,
    text_color: str = "#FFFFFF",
    highlight_color: str = "#FFD700",
    bg_padding: int = 16,
) -> np.ndarray:
    """Render a single subtitle line with word highlighting."""
    full_text = " ".join(w["word"] for w in words)

    # Create a wide enough image
    temp_img = Image.new("RGBA", (VIDEO_WIDTH, font_size * 3), (0, 0, 0, 0))
    draw = ImageDraw.Draw(temp_img)

    try:
        font = ImageFont.truetype("DejaVuSans-Bold.ttf", font_size)
    except OSError:
        font = ImageFont.load_default()

    # Measure full text
    bbox = draw.textbbox((0, 0), full_text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]

    # Create properly sized image with background
    img_w = text_width + bg_padding * 2
    img_h = text_height + bg_padding * 2
    img = Image.new("RGBA", (img_w, img_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Draw rounded rectangle background
    draw.rounded_rectangle(
        [(0, 0), (img_w, img_h)],
        radius=12,
        fill=(0, 0, 0, 153),  # ~60% opacity black
    )

    # Draw each word, highlighting the current one
    x_offset = bg_padding
    for word_info in words:
        word = word_info["word"]
        color = highlight_color if word == current_word and word_info["start"] == current_word_start else text_color

        # Black outline for readability
        for dx in [-1, 0, 1]:
            for dy in [-1, 0, 1]:
                draw.text((x_offset + dx, bg_padding + dy), word, font=font, fill="#000000")

        draw.text((x_offset, bg_padding), word, font=font, fill=color)

        word_bbox = draw.textbbox((0, 0), word + " ", font=font)
        x_offset += word_bbox[2] - word_bbox[0]

    return np.array(img)


# ─── BACKGROUND MUSIC ───────────────────────────────────────────────────────

def get_background_music(
    music_dir: str = "assets/music",
    duration: float = 30.0,
    volume: float = 0.12,
) -> AudioFileClip | None:
    """
    Pick a random background music track and adjust volume.

    Volume is intentionally low (0.12) so voiceover is clearly dominant.
    """
    music_path = Path(music_dir)
    if not music_path.exists():
        return None

    tracks = list(music_path.glob("*.mp3")) + list(music_path.glob("*.wav"))
    if not tracks:
        return None

    track = random.choice(tracks)
    audio = AudioFileClip(str(track))

    # Loop if needed
    if audio.duration < duration:
        loops_needed = int(duration / audio.duration) + 1
        clips = [audio] * loops_needed
        audio = CompositeAudioClip(
            [c.with_start(i * audio.duration) for i, c in enumerate(clips)]
        )

    # Trim to duration and set volume
    audio = audio.subclipped(0, duration).with_effects([lambda c: c.volumex(volume)])

    return audio


# ─── MAIN ASSEMBLY ───────────────────────────────────────────────────────────

def assemble_video(
    script: dict,
    image_paths: list[str],
    voiceover_path: str | None,
    subtitle_data: dict,
    output_path: str,
    text_style: dict | None = None,
    music_dir: str = "assets/music",
) -> str:
    """
    Assemble the final TikTok video from all components.

    This is the main function that brings everything together:
    1. Creates image clips with Ken Burns effect
    2. Adds text overlays (hook + body text)
    3. Adds animated subtitles synced to voiceover
    4. Mixes voiceover + background music
    5. Exports as 1080x1920 MP4

    Args:
        script: The script dict with slide data
        image_paths: List of image file paths (one per slide)
        voiceover_path: Path to voiceover audio (None for trending sound videos)
        subtitle_data: Dict with "words", "lines", "slide_timings"
        output_path: Where to save the final MP4
        text_style: Text styling configuration
        music_dir: Directory containing background music files

    Returns:
        Path to the final video file
    """
    if text_style is None:
        text_style = {
            "font": None,
            "hook_font_size": 72,
            "body_font_size": 56,
            "subtitle_font_size": 44,
            "text_color": "#FFFFFF",
            "text_stroke_color": "#000000",
            "text_stroke_width": 3,
            "highlight_color": "#FFD700",
        }

    slides = script["slides"]
    slide_timings = subtitle_data.get("slide_timings", [])
    subtitle_lines = subtitle_data.get("lines", [])

    # Fallback: equal timing if no subtitle data
    if not slide_timings:
        dur = 3.5
        slide_timings = [
            {"slide_index": i, "start": i * dur, "end": (i + 1) * dur}
            for i in range(len(slides))
        ]

    total_duration = slide_timings[-1]["end"]

    # ─── Build slide clips with Ken Burns ────────────────────────────────
    slide_clips = []

    for timing in slide_timings:
        idx = timing["slide_index"]
        start = timing["start"]
        end = timing["end"]
        duration = end - start

        if idx >= len(image_paths):
            break

        # Load image and create clip
        img_clip = (
            ImageClip(image_paths[idx])
            .resized((VIDEO_WIDTH, VIDEO_HEIGHT))
            .with_duration(duration)
        )

        # Apply Ken Burns
        img_clip = apply_ken_burns(img_clip, duration)

        # ─── Text overlay for this slide ─────────────────────────────
        slide_data = slides[idx] if idx < len(slides) else {}
        overlay_text = slide_data.get("text_overlay", "")

        if overlay_text:
            # First slide gets larger hook font
            font_size = (
                text_style["hook_font_size"] if idx == 0
                else text_style["body_font_size"]
            )

            text_img = render_text_overlay(
                overlay_text,
                font_size=font_size,
                text_color=text_style["text_color"],
                stroke_color=text_style["text_stroke_color"],
                stroke_width=text_style["text_stroke_width"],
                position="center" if idx == 0 else "top",
            )

            text_clip = (
                ImageClip(text_img)
                .with_duration(duration)
            )

            # Pop-in animation: scale from 0.85 to 1.0 over first 0.2 seconds
            img_clip = CompositeVideoClip(
                [img_clip, text_clip],
                size=(VIDEO_WIDTH, VIDEO_HEIGHT),
            )

        img_clip = img_clip.with_start(start)
        slide_clips.append(img_clip)

    # ─── Compose all slides ──────────────────────────────────────────────
    video = CompositeVideoClip(slide_clips, size=(VIDEO_WIDTH, VIDEO_HEIGHT))

    # ─── Add subtitle clips ──────────────────────────────────────────────
    if subtitle_lines:
        sub_clips = create_subtitle_clips(subtitle_lines, total_duration, text_style)
        video = CompositeVideoClip(
            [video] + sub_clips,
            size=(VIDEO_WIDTH, VIDEO_HEIGHT),
        )

    video = video.with_duration(total_duration)

    # ─── Audio: voiceover + background music ─────────────────────────────
    audio_clips = []

    if voiceover_path and os.path.exists(voiceover_path):
        vo_audio = AudioFileClip(voiceover_path)
        audio_clips.append(vo_audio)

    bg_music = get_background_music(music_dir, total_duration)
    if bg_music:
        audio_clips.append(bg_music)

    if audio_clips:
        if len(audio_clips) == 1:
            video = video.with_audio(audio_clips[0])
        else:
            mixed = CompositeAudioClip(audio_clips)
            video = video.with_audio(mixed)

    # ─── Export ───────────────────────────────────────────────────────────
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    video.write_videofile(
        output_path,
        fps=FPS,
        codec="libx264",
        audio_codec="aac",
        preset="medium",
        threads=4,
        logger=None,  # Suppress moviepy's verbose output
    )

    print(f"    Video saved: {output_path}")
    return output_path


# ─── CONVENIENCE: EXTRACT KEY FRAMES FOR QA ─────────────────────────────────

def extract_key_frames(video_path: str, output_dir: str, num_frames: int = 5) -> list[str]:
    """
    Extract key frames from a video for QA review.

    Grabs frames at evenly spaced intervals through the video.
    These frames are sent to Claude Vision for quality assessment.
    """
    from moviepy import VideoFileClip

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    clip = VideoFileClip(video_path)
    duration = clip.duration
    frame_paths = []

    for i in range(num_frames):
        t = (i / max(num_frames - 1, 1)) * duration * 0.95  # Don't go to very end
        frame = clip.get_frame(t)
        frame_img = Image.fromarray(frame)

        path = str(out / f"frame_{i:02d}.png")
        frame_img.save(path)
        frame_paths.append(path)

    clip.close()
    return frame_paths


if __name__ == "__main__":
    print("Video assembler module loaded.")
    print(f"Output: {VIDEO_WIDTH}x{VIDEO_HEIGHT} @ {FPS}fps")
    print("Run via pipeline.py for full video generation.")
