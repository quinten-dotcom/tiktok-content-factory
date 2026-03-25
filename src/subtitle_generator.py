from __future__ import annotations

"""
Subtitle Generator — Uses Whisper to generate word-level timestamps.

These timestamps are used to create animated word-by-word subtitles
in the TikTok style (highlighted word, big font, bottom third).
"""

import json
from pathlib import Path


def generate_word_timestamps(audio_path: str, model_size: str = "base") -> list[dict]:
    """
    Use Whisper to get word-level timestamps from audio.

    Args:
        audio_path: Path to the voiceover audio file
        model_size: Whisper model size ("tiny", "base", "small", "medium")
                    "base" is fast and accurate enough for clean AI voiceover

    Returns:
        List of dicts: [{"word": "Hello", "start": 0.0, "end": 0.24}, ...]
    """
    import whisper

    model = whisper.load_model(model_size)

    result = model.transcribe(
        audio_path,
        word_timestamps=True,
        language="en",
    )

    words = []
    for segment in result["segments"]:
        for word_info in segment.get("words", []):
            words.append({
                "word": word_info["word"].strip(),
                "start": round(word_info["start"], 3),
                "end": round(word_info["end"], 3),
            })

    return words


def group_words_into_lines(words: list[dict], max_words_per_line: int = 5) -> list[dict]:
    """
    Group words into subtitle lines for display.

    Each line contains up to max_words_per_line words and has
    a start/end time spanning the full line.

    Returns:
        List of dicts: [{
            "text": "Hello this is my",
            "words": [...],
            "start": 0.0,
            "end": 1.2
        }, ...]
    """
    lines = []
    current_line_words = []

    for word in words:
        current_line_words.append(word)

        if len(current_line_words) >= max_words_per_line:
            lines.append({
                "text": " ".join(w["word"] for w in current_line_words),
                "words": current_line_words,
                "start": current_line_words[0]["start"],
                "end": current_line_words[-1]["end"],
            })
            current_line_words = []

    # Don't forget remaining words
    if current_line_words:
        lines.append({
            "text": " ".join(w["word"] for w in current_line_words),
            "words": current_line_words,
            "start": current_line_words[0]["start"],
            "end": current_line_words[-1]["end"],
        })

    return lines


def calculate_slide_timings(
    words: list[dict],
    script: dict,
    min_slide_duration: float = 2.5,
    max_slide_duration: float = 6.0,
) -> list[dict]:
    """
    Calculate when each slide should appear based on voiceover timing.

    Maps the word timestamps to slide boundaries so that slide transitions
    align with the natural pauses and topic shifts in the voiceover.

    Args:
        words: Word-level timestamps from Whisper
        script: The script dict with slides
        min_slide_duration: Minimum seconds per slide
        max_slide_duration: Maximum seconds per slide

    Returns:
        List of dicts: [{"slide_index": 0, "start": 0.0, "end": 4.2}, ...]
    """
    slides = script["slides"]
    num_slides = len(slides)

    if not words:
        # No voiceover — equal duration slides
        duration_per_slide = 3.5
        return [
            {
                "slide_index": i,
                "start": i * duration_per_slide,
                "end": (i + 1) * duration_per_slide,
            }
            for i in range(num_slides)
        ]

    total_duration = words[-1]["end"] if words else 0

    # Count words per slide's voiceover to estimate proportional timing
    slide_word_counts = []
    for slide in slides:
        vo_text = slide.get("voiceover", "")
        word_count = len(vo_text.split()) if vo_text else 0
        slide_word_counts.append(max(word_count, 1))

    total_words = sum(slide_word_counts)

    # Distribute time proportionally to word count
    timings = []
    current_time = 0.0

    for i, wc in enumerate(slide_word_counts):
        proportion = wc / total_words
        raw_duration = proportion * total_duration
        duration = max(min_slide_duration, min(raw_duration, max_slide_duration))

        timings.append({
            "slide_index": i,
            "start": round(current_time, 3),
            "end": round(current_time + duration, 3),
        })
        current_time += duration

    # Adjust last slide to match total audio duration
    if timings and total_duration > 0:
        timings[-1]["end"] = round(max(total_duration, timings[-1]["start"] + min_slide_duration), 3)

    return timings


def save_subtitle_data(
    words: list[dict],
    lines: list[dict],
    slide_timings: list[dict],
    output_dir: str,
) -> str:
    """Save all subtitle/timing data to a JSON file."""
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    data = {
        "words": words,
        "lines": lines,
        "slide_timings": slide_timings,
    }

    path = str(out / "subtitle_data.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    return path


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        audio_path = sys.argv[1]
        print(f"Generating subtitles for: {audio_path}")
        words = generate_word_timestamps(audio_path)
        lines = group_words_into_lines(words)
        print(f"Found {len(words)} words in {len(lines)} lines")
        for line in lines:
            print(f"  [{line['start']:.1f}s - {line['end']:.1f}s] {line['text']}")
    else:
        print("Usage: python subtitle_generator.py <audio_file>")
