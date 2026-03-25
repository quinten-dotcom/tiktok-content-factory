from __future__ import annotations

"""
Voice Generator — Generates voiceover audio from script text.

Supports two engines:
- ElevenLabs: Best quality, paid ($5/mo starter)
- Kokoro: Free, open-source, runs locally, surprisingly good
"""

import os
import subprocess
from pathlib import Path


def generate_voiceover_elevenlabs(
    text: str,
    output_path: str,
    voice_id: str = "21m00Tcm4TlvDq8ikWAM",
    speaking_speed: float = 1.0,
    api_key: str | None = None,
) -> str:
    """
    Generate voiceover using ElevenLabs API.

    Args:
        text: The text to speak
        output_path: Where to save the MP3/WAV file
        voice_id: ElevenLabs voice ID
        speaking_speed: Playback speed multiplier (1.0 = normal)
        api_key: ElevenLabs API key
    """
    import requests

    key = api_key or os.environ.get("ELEVENLABS_API_KEY")

    response = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
        headers={
            "xi-api-key": key,
            "Content-Type": "application/json",
        },
        json={
            "text": text,
            "model_id": "eleven_turbo_v2_5",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
                "speed": speaking_speed,
            },
        },
    )

    if response.status_code != 200:
        raise Exception(f"ElevenLabs error {response.status_code}: {response.text}")

    with open(output_path, "wb") as f:
        f.write(response.content)

    return output_path


def generate_voiceover_kokoro(
    text: str,
    output_path: str,
    voice: str = "af_sarah",
    speaking_speed: float = 1.0,
) -> str:
    """
    Generate voiceover using Kokoro TTS (free, local).

    Requires: pip install kokoro-onnx
    Or: pip install kokoro (for GPU-accelerated version)

    Args:
        text: The text to speak
        output_path: Where to save the WAV file
        voice: Kokoro voice name (af_sarah, am_michael, af_bella, etc.)
        speaking_speed: Speed multiplier
    """
    try:
        from kokoro_onnx import Kokoro

        kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
        audio, sample_rate = kokoro.create(text, voice=voice, speed=speaking_speed)

        import soundfile as sf
        sf.write(output_path, audio, sample_rate)

    except ImportError:
        # Fallback: try command-line kokoro
        cmd = [
            "kokoro",
            "--text", text,
            "--voice", voice,
            "--speed", str(speaking_speed),
            "--output", output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise Exception(f"Kokoro TTS failed: {result.stderr}")

    return output_path


def generate_voiceover(
    text: str,
    output_path: str,
    voice_config: dict,
    engine: str = "elevenlabs",
    api_key: str | None = None,
) -> str:
    """
    Generate voiceover using the configured engine.

    Args:
        text: Script text to speak
        output_path: Where to save audio file
        voice_config: Persona's voice configuration dict
        engine: "elevenlabs" or "kokoro"
        api_key: API key (for ElevenLabs)

    Returns:
        Path to the generated audio file
    """
    speed = voice_config.get("speaking_speed", 1.0)

    if engine == "elevenlabs":
        voice_id = voice_config.get("elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM")
        return generate_voiceover_elevenlabs(
            text, output_path, voice_id=voice_id, speaking_speed=speed, api_key=api_key
        )
    elif engine == "kokoro":
        voice = voice_config.get("kokoro_voice", "af_sarah")
        return generate_voiceover_kokoro(text, output_path, voice=voice, speaking_speed=speed)
    else:
        raise ValueError(f"Unknown voice engine: {engine}")


def generate_voiceover_for_script(
    script: dict,
    output_dir: str,
    engine: str = "elevenlabs",
    api_key: str | None = None,
) -> str:
    """
    Generate the full voiceover for an entire script.

    Concatenates all slide voiceover text into one continuous audio file.
    Also generates per-slide audio for timing purposes.

    Returns:
        Path to the full voiceover audio file
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    voice_config = script.get("persona", {}).get("voice_config", {})

    # Check if this is a no-voiceover video (trending sound style)
    if not script.get("has_voiceover", True):
        print("    Skipping voiceover (trending sound format)")
        return None

    # Generate per-slide audio clips for precise timing
    slide_audio_paths = []
    for i, slide in enumerate(script["slides"]):
        vo_text = slide.get("voiceover", "")
        if not vo_text.strip():
            continue

        slide_path = str(out / f"slide_voice_{i:02d}.wav")
        print(f"    Generating voiceover for slide {i + 1}...")

        generate_voiceover(
            vo_text,
            slide_path,
            voice_config,
            engine=engine,
            api_key=api_key,
        )
        slide_audio_paths.append(slide_path)

    # Also generate one continuous voiceover for the full script
    full_text = " ".join(
        slide.get("voiceover", "") for slide in script["slides"] if slide.get("voiceover")
    )
    full_path = str(out / "full_voiceover.wav")
    generate_voiceover(full_text, full_path, voice_config, engine=engine, api_key=api_key)

    return full_path


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    test_text = "Hey guys, I found this app that completely changed how I study. Trust me on this — link in bio."
    print("Generating test voiceover...")
    generate_voiceover(
        test_text,
        "output/test_voice.wav",
        voice_config={"elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM", "speaking_speed": 1.1},
        engine=os.environ.get("VOICE_ENGINE", "elevenlabs"),
    )
    print("Done!")
