# TikTok Content Factory — Setup Guide

## Quick Start (5 minutes)

```bash
# 1. Clone/copy this folder to your machine
cd tiktok-factory

# 2. Install dependencies
pip install -r requirements.txt

# 3. Install FFmpeg (needed for video assembly)
# Mac: brew install ffmpeg
# Ubuntu: sudo apt install ffmpeg
# Windows: choco install ffmpeg

# 4. Copy .env.example to .env and add your API keys
cp .env.example .env
# Then edit .env with your keys

# 5. Onboard your first app
python pipeline.py onboard

# 6. Generate videos
python pipeline.py generate --app config/your_app.json

# 7. Upload queued videos
python pipeline.py upload
```

## API Keys You Need

| Service | What For | Get It | Cost |
|---------|----------|--------|------|
| **Anthropic (Claude)** | Script generation + QA | [console.anthropic.com](https://console.anthropic.com) | ~$5/mo |
| **fal.ai** | Image generation (Flux) | [fal.ai/dashboard](https://fal.ai/dashboard) | ~$15-50/mo |
| **ElevenLabs** | Voiceover (or use Kokoro free) | [elevenlabs.io](https://elevenlabs.io) | $5/mo+ |
| **TikTok Developer** | Video uploading | [developers.tiktok.com](https://developers.tiktok.com) | Free |

### Free Alternative: Use Kokoro for Voice
Set `VOICE_ENGINE=kokoro` in your `.env` to use the free open-source Kokoro TTS.
Install: `pip install kokoro-onnx`

## Commands

```bash
# Generate 7 videos for one app
python pipeline.py generate --app config/my_app.json

# Generate 3 test videos (for review)
python pipeline.py generate --app config/my_app.json --count 3

# Generate for ALL apps in config/
python pipeline.py generate --all

# Upload all scheduled videos
python pipeline.py upload

# Full daily run (generate all + upload)
python pipeline.py daily

# Set up a new app interactively
python pipeline.py onboard
```

## Automating Daily Runs

### Option A: Cron job (Linux/Mac)
```bash
# Run daily at 6 AM
crontab -e
0 6 * * * cd /path/to/tiktok-factory && python pipeline.py daily >> logs/daily.log 2>&1
```

### Option B: Claude Scheduled Task
Use Claude's scheduled task feature to run `python pipeline.py daily` on a cron.

## Adding a New App

1. Copy `config/example_app.json` to `config/your_app.json`
2. Edit the JSON — update app name, niche, personas, content pillars
3. Run `python pipeline.py generate --app config/your_app.json --count 3`
4. Review the 3 test videos in `output/your_app/`
5. If they look good, add it to the daily rotation

## Project Structure

```
tiktok-factory/
├── pipeline.py              ← Main orchestrator (run this)
├── config/
│   └── example_app.json     ← App configuration template
├── src/
│   ├── script_generator.py  ← Claude API script generation
│   ├── image_generator.py   ← Flux image generation via fal.ai
│   ├── voice_generator.py   ← ElevenLabs / Kokoro voiceover
│   ├── subtitle_generator.py← Whisper word-level subtitles
│   ├── video_assembler.py   ← MoviePy video assembly (core engine)
│   ├── qa_reviewer.py       ← Claude Vision quality review
│   └── uploader.py          ← TikTok upload + scheduling
├── output/                  ← Generated videos go here
├── assets/
│   ├── music/               ← Drop royalty-free MP3s here
│   └── fonts/               ← Custom fonts (optional)
├── requirements.txt
├── .env.example
└── SETUP.md                 ← You're reading this
```

## Troubleshooting

**"No module named moviepy"** — Run `pip install moviepy`
**"ffmpeg not found"** — Install FFmpeg for your OS
**"Whisper out of memory"** — Use `model_size="tiny"` in subtitle_generator.py
**"fal.ai timeout"** — Images take 2-10 seconds; the script auto-polls
**"TikTok upload fails"** — Check cookies file is fresh; re-export from browser
