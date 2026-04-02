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
from log_config import get_logger

logger = get_logger(__name__)


def generate_voiceover_elevenlabs(
    text: str,
    output_path: str,
    voice_id: str = "21m00Tcm4TlvDq8ikWAM",
    speaking_speed: float = 1.0,
    api_key: str | None = None,
    stability: float = 0.5,
    similarity: float = 0.75,
    style: float = 0.0,
) -> str:
    """
    Generate voiceover using ElevenLabs API.
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
                "stability": stability,
                "similarity_boost": similarity,
                "style": style,
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
        voice_id_raw = voice_config.get("elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM")
        # Support multi-voice rotation: IDs separated by newlines or commas
        import random as _rng
        voice_ids = [v.strip() for v in voice_id_raw.replace(",", "\n").split("\n") if v.strip()]
        voice_id = _rng.choice(voice_ids) if voice_ids else "21m00Tcm4TlvDq8ikWAM"
        return generate_voiceover_elevenlabs(
            text, output_path, voice_id=voice_id, speaking_speed=speed, api_key=api_key,
            stability=float(voice_config.get("stability", 0.5)),
            similarity=float(voice_config.get("similarity", 0.75)),
            style=float(voice_config.get("style", 0)),
        )
    elif engine == "kokoro":
        voice = voice_config.get("kokoro_voice", "af_sarah")
        try:
            return generate_voiceover_kokoro(text, output_path, voice=voice, speaking_speed=speed)
        except Exception as kokoro_err:
            # Fall back to ElevenLabs if kokoro isn't installed
            elevenlabs_key = api_key or os.environ.get("ELEVENLABS_API_KEY")
            if elevenlabs_key:
                logger.warning(f"Kokoro failed ({kokoro_err}), falling back to ElevenLabs")
                voice_id = voice_config.get("elevenlabs_voice_id", "21m00Tcm4TlvDq8ikWAM")
                return generate_voiceover_elevenlabs(
                    text, output_path, voice_id=voice_id, speaking_speed=speed, api_key=elevenlabs_key
                )
            raise
    else:
        raise ValueError(f"Unknown voice engine: {engine}")


def generate_voiceover_for_script(
    script: dict,
    output_dir: str,
    engine: str = "elevenlabs",
    api_key: str | None = None,
    voice_tuning: dict | None = None,
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

    # Agent voice config (from creative) takes priority over script persona config
    voice_config = voice_tuning if voice_tuning else script.get("persona", {}).get("voice_config", {})
    if voice_config.get("elevenlabs_voice_id"):
        logger.info(f"Using ElevenLabs voice: {voice_config['elevenlabs_voice_id']}")

    # Check if this is a no-voiceover video (trending sound style)
    if not script.get("has_voiceover", True):
        logger.info("Skipping voiceover (trending sound format)")
        return None

    # Generate per-slide audio clips for precise timing
    slide_audio = []
    for i, slide in enumerate(script["slides"]):
        vo_text = slide.get("voiceover", "")
        if not vo_text.strip():
            slide_audio.append({"slide_index": i, "path": None, "duration": 0})
            continue

        slide_path = str(out / f"slide_voice_{i:02d}.wav")
        logger.info(f"Generating voiceover for slide {i + 1}...")

        generate_voiceover(
            vo_text,
            slide_path,
            voice_config,
            engine=engine,
            api_key=api_key,
        )

        # Get actual audio duration
        dur = 0
        try:
            import subprocess as _sp
            probe = _sp.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                 "-of", "default=noprint_wrappers=1:nokey=1", slide_path],
                capture_output=True, text=True, timeout=10,
            )
            dur = float(probe.stdout.strip())
        except Exception:
            dur = 3.0  # fallback
        slide_audio.append({"slide_index": i, "path": slide_path, "duration": round(dur, 3)})

    # Also generate one continuous voiceover for the full script
    full_text = " ".join(
        slide.get("voiceover", "") for slide in script["slides"] if slide.get("voiceover")
    )
    full_path = str(out / "full_voiceover.wav")
    generate_voiceover(full_text, full_path, voice_config, engine=engine, api_key=api_key)

    return {"full_path": full_path, "slide_audio": slide_audio}


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()

    test_text = "Hey guys, I found this app that completely changed how I study. Trust me on this — link in bio."
    logger.info("Generating test voiceover...")
    generate_voiceover(
        test_text,
        "output/test_voice.wav",
        voice_config={"elevenlabs_voice_id": "21m00Tcm4TlvDq8ikWAM", "speaking_speed": 1.1},
        engine=os.environ.get("VOICE_ENGINE", "elevenlabs"),
    )
    logger.info("Done!")
