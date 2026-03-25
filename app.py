#!/usr/bin/env python3
from __future__ import annotations
"""
TikTok Content Factory — Web Dashboard

Run this file and open http://localhost:5000 in your browser.
Everything is controlled from the web UI — no terminal needed.

Usage:
    python3 app.py
"""

import os
import sys
import json
import time
import threading
from pathlib import Path
from datetime import datetime

from flask import Flask, render_template_string, request, jsonify, send_file
from dotenv import load_dotenv

load_dotenv()

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

app = Flask(__name__)

# ─── DIRECTORIES ─────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
OUTPUT_DIR = BASE_DIR / "output"
CONFIG_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# ─── STATE ───────────────────────────────────────────────────────────────────
# Track background jobs
jobs = {}


# ─── HTML TEMPLATE ───────────────────────────────────────────────────────────
DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TikTok Content Factory</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0a0a0a;
            color: #e0e0e0;
            min-height: 100vh;
        }

        .header {
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 50%, #0f3460 100%);
            padding: 24px 40px;
            border-bottom: 1px solid #ffffff10;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }

        .header h1 {
            font-size: 24px;
            font-weight: 700;
            background: linear-gradient(135deg, #00d2ff, #7b2ff7);
            -webkit-background-clip: text;
            -webkit-text-fill-color: transparent;
        }

        .header .stats {
            display: flex;
            gap: 24px;
            font-size: 14px;
            color: #888;
        }

        .header .stats span { color: #00d2ff; font-weight: 600; }

        .container {
            max-width: 1200px;
            margin: 0 auto;
            padding: 32px 24px;
        }

        /* ─── Add App Section ─── */
        .add-app {
            background: #141420;
            border: 1px solid #ffffff10;
            border-radius: 16px;
            padding: 32px;
            margin-bottom: 32px;
        }

        .add-app h2 {
            font-size: 18px;
            margin-bottom: 20px;
            color: #fff;
        }

        .add-app .form-row {
            display: flex;
            gap: 12px;
            margin-bottom: 12px;
        }

        .add-app input {
            flex: 1;
            padding: 14px 16px;
            background: #1a1a2e;
            border: 1px solid #ffffff15;
            border-radius: 10px;
            color: #fff;
            font-size: 15px;
            outline: none;
            transition: border-color 0.2s;
        }

        .add-app input:focus {
            border-color: #7b2ff7;
        }

        .add-app input::placeholder { color: #555; }

        .btn {
            padding: 14px 28px;
            border: none;
            border-radius: 10px;
            font-size: 15px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.2s;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }

        .btn-primary {
            background: linear-gradient(135deg, #7b2ff7, #00d2ff);
            color: white;
        }

        .btn-primary:hover { opacity: 0.9; transform: translateY(-1px); }
        .btn-primary:disabled { opacity: 0.5; cursor: not-allowed; transform: none; }

        .btn-success {
            background: #10b981;
            color: white;
        }

        .btn-danger {
            background: #ef4444;
            color: white;
            padding: 8px 16px;
            font-size: 13px;
        }

        .btn-sm {
            padding: 10px 20px;
            font-size: 13px;
        }

        /* ─── App Cards ─── */
        .apps-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
            gap: 20px;
        }

        .app-card {
            background: #141420;
            border: 1px solid #ffffff10;
            border-radius: 16px;
            padding: 24px;
            transition: border-color 0.2s;
        }

        .app-card:hover { border-color: #7b2ff730; }

        .app-card .app-header {
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 16px;
        }

        .app-card h3 {
            font-size: 18px;
            color: #fff;
            margin-bottom: 4px;
        }

        .app-card .handle {
            color: #7b2ff7;
            font-size: 14px;
        }

        .app-card .niche-tag {
            background: #7b2ff720;
            color: #7b2ff7;
            padding: 4px 12px;
            border-radius: 20px;
            font-size: 12px;
            font-weight: 600;
        }

        .app-card .pillars {
            margin: 12px 0;
            font-size: 13px;
            color: #888;
            line-height: 1.6;
        }

        .app-card .pillars span {
            display: inline-block;
            background: #1a1a2e;
            padding: 3px 10px;
            border-radius: 6px;
            margin: 2px 4px 2px 0;
            font-size: 12px;
        }

        .app-card .personas {
            display: flex;
            gap: 8px;
            margin: 12px 0;
        }

        .persona-chip {
            background: #1a1a2e;
            padding: 6px 12px;
            border-radius: 8px;
            font-size: 12px;
        }

        .persona-chip .name { color: #00d2ff; font-weight: 600; }

        .app-card .actions {
            display: flex;
            gap: 8px;
            margin-top: 16px;
            padding-top: 16px;
            border-top: 1px solid #ffffff08;
        }

        /* ─── Status/Log ─── */
        .status-bar {
            position: fixed;
            bottom: 0;
            left: 0;
            right: 0;
            background: #141420;
            border-top: 1px solid #ffffff10;
            padding: 12px 40px;
            font-size: 13px;
            color: #888;
            display: flex;
            justify-content: space-between;
            z-index: 100;
        }

        .status-bar .log { color: #00d2ff; }

        /* ─── Modal ─── */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.7);
            z-index: 200;
            align-items: center;
            justify-content: center;
        }

        .modal-overlay.active { display: flex; }

        .modal {
            background: #1a1a2e;
            border: 1px solid #ffffff15;
            border-radius: 16px;
            padding: 32px;
            max-width: 600px;
            width: 90%;
            max-height: 80vh;
            overflow-y: auto;
        }

        .modal h2 { margin-bottom: 16px; }

        .modal pre {
            background: #0a0a0a;
            padding: 16px;
            border-radius: 8px;
            font-size: 12px;
            overflow-x: auto;
            max-height: 400px;
            overflow-y: auto;
        }

        /* ─── Spinner ─── */
        .spinner {
            display: inline-block;
            width: 16px;
            height: 16px;
            border: 2px solid #ffffff30;
            border-top-color: #00d2ff;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }

        @keyframes spin { to { transform: rotate(360deg); } }

        .generating-msg {
            color: #00d2ff;
            font-size: 14px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        /* ─── Section Label ─── */
        .section-label {
            font-size: 14px;
            font-weight: 600;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-bottom: 16px;
        }

        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #444;
        }

        .empty-state h3 { color: #666; margin-bottom: 8px; }
    </style>
</head>
<body>

<div class="header">
    <h1>TikTok Content Factory</h1>
    <div class="stats">
        Apps: <span id="app-count">0</span> &nbsp;|&nbsp;
        Videos Today: <span id="video-count">0</span> &nbsp;|&nbsp;
        Queued: <span id="queue-count">0</span>
    </div>
</div>

<div class="container">

    <!-- Add New App -->
    <div class="add-app">
        <h2>Add New App</h2>
        <p style="color: #666; font-size: 14px; margin-bottom: 16px;">
            Just type the name and a one-liner. AI handles everything else — personas, content strategy, hashtags, all of it.
        </p>
        <div class="form-row">
            <input type="text" id="app-name" placeholder="App Name (e.g. FocusTimer Pro)">
            <input type="text" id="app-desc" placeholder="One-line description (e.g. A Pomodoro timer with streak tracking)" style="flex: 2;">
            <button class="btn btn-primary" id="add-btn" onclick="addApp()">
                Generate Strategy
            </button>
        </div>
        <div id="add-status" style="margin-top: 12px;"></div>
    </div>

    <!-- Apps List -->
    <div class="section-label">Your Apps</div>
    <div class="apps-grid" id="apps-grid">
        <div class="empty-state" id="empty-state">
            <h3>No apps yet</h3>
            <p>Add your first app above to get started</p>
        </div>
    </div>

</div>

<!-- Status Bar -->
<div class="status-bar">
    <div id="status-text">Ready</div>
    <div class="log" id="status-log"></div>
</div>

<!-- Config Modal -->
<div class="modal-overlay" id="config-modal">
    <div class="modal">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h2 id="modal-title">App Config</h2>
            <button class="btn btn-sm" onclick="closeModal()" style="background: #333;">Close</button>
        </div>
        <pre id="modal-content"></pre>
    </div>
</div>

<script>
    // ─── State ──────────────────────────────────────────────────────
    let apps = [];
    let pollIntervals = {};

    // ─── Load apps on page load ─────────────────────────────────────
    async function loadApps() {
        const res = await fetch('/api/apps');
        apps = await res.json();
        renderApps();
        updateStats();
    }

    // ─── Add new app ────────────────────────────────────────────────
    async function addApp() {
        const name = document.getElementById('app-name').value.trim();
        const desc = document.getElementById('app-desc').value.trim();

        if (!name || !desc) {
            document.getElementById('add-status').innerHTML =
                '<span style="color: #ef4444;">Please fill in both fields</span>';
            return;
        }

        const btn = document.getElementById('add-btn');
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner"></span> Generating...';
        document.getElementById('add-status').innerHTML =
            '<span class="generating-msg"><span class="spinner"></span> AI is creating your content strategy, personas, hashtags... (15-30 seconds)</span>';

        setStatus('Generating strategy for: ' + name);

        try {
            const res = await fetch('/api/apps', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name, description: desc }),
            });

            const data = await res.json();

            if (data.error) {
                document.getElementById('add-status').innerHTML =
                    `<span style="color: #ef4444;">Error: ${data.error}</span>`;
            } else {
                document.getElementById('app-name').value = '';
                document.getElementById('app-desc').value = '';
                document.getElementById('add-status').innerHTML =
                    '<span style="color: #10b981;">App created! Strategy generated.</span>';
                setTimeout(() => { document.getElementById('add-status').innerHTML = ''; }, 3000);
                loadApps();
            }
        } catch (e) {
            document.getElementById('add-status').innerHTML =
                `<span style="color: #ef4444;">Error: ${e.message}</span>`;
        }

        btn.disabled = false;
        btn.innerHTML = 'Generate Strategy';
        setStatus('Ready');
    }

    // ─── Generate videos ────────────────────────────────────────────
    async function generateVideos(slug, count) {
        count = count || 7;
        setStatus(`Generating ${count} videos for ${slug}...`);

        const cardActions = document.getElementById(`actions-${slug}`);
        cardActions.innerHTML = '<span class="generating-msg"><span class="spinner"></span> Generating videos...</span>';

        try {
            const res = await fetch(`/api/generate/${slug}`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ count }),
            });
            const data = await res.json();

            if (data.job_id) {
                // Poll for progress
                pollJob(slug, data.job_id);
            }
        } catch (e) {
            cardActions.innerHTML = `<span style="color: #ef4444;">Error: ${e.message}</span>`;
            setStatus('Error generating videos');
        }
    }

    function pollJob(slug, jobId) {
        const interval = setInterval(async () => {
            const res = await fetch(`/api/job/${jobId}`);
            const data = await res.json();

            const cardActions = document.getElementById(`actions-${slug}`);

            if (data.status === 'running') {
                cardActions.innerHTML =
                    `<span class="generating-msg"><span class="spinner"></span> ${data.message || 'Working...'}</span>`;
                setStatus(data.message || 'Generating...');
            } else if (data.status === 'done') {
                clearInterval(interval);
                cardActions.innerHTML = renderActions(slug);
                setStatus(`Done! ${data.videos_created || 0} videos created for ${slug}`);
                loadApps();
            } else if (data.status === 'error') {
                clearInterval(interval);
                cardActions.innerHTML =
                    `<span style="color: #ef4444;">Error: ${data.message}</span><br><br>` + renderActions(slug);
                setStatus('Error');
            }
        }, 3000);
    }

    // ─── Delete app ─────────────────────────────────────────────────
    async function deleteApp(slug) {
        if (!confirm(`Delete ${slug} and all its content?`)) return;

        await fetch(`/api/apps/${slug}`, { method: 'DELETE' });
        loadApps();
        setStatus(`Deleted ${slug}`);
    }

    // ─── View config ────────────────────────────────────────────────
    async function viewConfig(slug) {
        const res = await fetch(`/api/apps/${slug}/config`);
        const config = await res.json();

        document.getElementById('modal-title').textContent = config.app_name + ' — Config';
        document.getElementById('modal-content').textContent = JSON.stringify(config, null, 2);
        document.getElementById('config-modal').classList.add('active');
    }

    function closeModal() {
        document.getElementById('config-modal').classList.remove('active');
    }

    // ─── Render ─────────────────────────────────────────────────────
    function renderApps() {
        const grid = document.getElementById('apps-grid');
        const empty = document.getElementById('empty-state');

        if (apps.length === 0) {
            empty.style.display = 'block';
            grid.innerHTML = '';
            grid.appendChild(empty);
            return;
        }

        empty.style.display = 'none';
        grid.innerHTML = apps.map(a => `
            <div class="app-card">
                <div class="app-header">
                    <div>
                        <h3>${a.app_name}</h3>
                        <div class="handle">${a.tiktok_handle || ''}</div>
                    </div>
                    <span class="niche-tag">${a.niche || ''}</span>
                </div>

                <p style="font-size: 13px; color: #999; margin-bottom: 8px;">${a.app_description || ''}</p>

                <div class="pillars">
                    ${(a.content_pillars || []).map(p => `<span>${p}</span>`).join('')}
                </div>

                <div class="personas">
                    ${(a.personas || []).map(p =>
                        `<div class="persona-chip"><span class="name">${p.name}</span> — ${p.archetype}</div>`
                    ).join('')}
                </div>

                <div style="font-size: 12px; color: #555; margin-top: 8px;">
                    Videos: ${a.video_count || 0} &nbsp;|&nbsp;
                    Queued: ${a.queued_count || 0}
                </div>

                <div class="actions" id="actions-${a.slug}">
                    ${renderActions(a.slug)}
                </div>
            </div>
        `).join('');

        document.getElementById('app-count').textContent = apps.length;
    }

    function renderActions(slug) {
        return `
            <button class="btn btn-primary btn-sm" onclick="generateVideos('${slug}', 7)">
                Generate 7 Videos
            </button>
            <button class="btn btn-sm" onclick="generateVideos('${slug}', 3)" style="background: #333;">
                Test 3
            </button>
            <button class="btn btn-sm" onclick="viewConfig('${slug}')" style="background: #333;">
                Config
            </button>
            <button class="btn btn-danger btn-sm" onclick="deleteApp('${slug}')">
                Delete
            </button>
        `;
    }

    function updateStats() {
        document.getElementById('app-count').textContent = apps.length;
        let totalVideos = apps.reduce((sum, a) => sum + (a.video_count || 0), 0);
        let totalQueued = apps.reduce((sum, a) => sum + (a.queued_count || 0), 0);
        document.getElementById('video-count').textContent = totalVideos;
        document.getElementById('queue-count').textContent = totalQueued;
    }

    function setStatus(msg) {
        document.getElementById('status-text').textContent = msg;
        document.getElementById('status-log').textContent = new Date().toLocaleTimeString();
    }

    // Enter key support
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && document.activeElement.id === 'app-desc') {
            addApp();
        }
        if (e.key === 'Escape') closeModal();
    });

    // Load on start
    loadApps();
</script>

</body>
</html>
"""


# ─── API ROUTES ──────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(DASHBOARD_HTML)


@app.route("/api/apps", methods=["GET"])
def list_apps():
    """List all configured apps with their stats."""
    apps_list = []
    for config_file in sorted(CONFIG_DIR.glob("*.json")):
        if config_file.name == "example_app.json":
            continue  # Skip the template
        try:
            with open(config_file) as f:
                config = json.load(f)

            slug = config_file.stem

            # Count videos and queued items
            video_count = 0
            queued_count = 0
            app_output = OUTPUT_DIR / slug
            if app_output.exists():
                video_count = len(list(app_output.rglob("*.mp4")))

            queue_dir = OUTPUT_DIR / "upload_queue"
            if queue_dir.exists():
                handle = config.get("tiktok_handle", "").lstrip("@")
                for qf in queue_dir.glob(f"{handle}_*.json"):
                    with open(qf) as f:
                        entry = json.load(f)
                    if entry.get("status") == "queued":
                        queued_count += 1

            config["slug"] = slug
            config["video_count"] = video_count
            config["queued_count"] = queued_count
            apps_list.append(config)
        except Exception as e:
            print(f"Error loading {config_file}: {e}")

    return jsonify(apps_list)


@app.route("/api/apps", methods=["POST"])
def create_app():
    """Create a new app — AI generates the full strategy."""
    data = request.json
    name = data.get("name", "").strip()
    description = data.get("description", "").strip()

    if not name or not description:
        return jsonify({"error": "Name and description are required"}), 400

    try:
        from config_generator import generate_app_config, save_app_config

        config = generate_app_config(name, description)
        path = save_app_config(config, str(CONFIG_DIR))

        return jsonify({"status": "created", "config_path": path, "config": config})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/apps/<slug>", methods=["DELETE"])
def delete_app(slug):
    """Delete an app config."""
    config_path = CONFIG_DIR / f"{slug}.json"
    if config_path.exists():
        os.remove(config_path)
    return jsonify({"status": "deleted"})


@app.route("/api/apps/<slug>/config", methods=["GET"])
def get_app_config(slug):
    """Get full config for an app."""
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404

    with open(config_path) as f:
        return jsonify(json.load(f))


@app.route("/api/generate/<slug>", methods=["POST"])
def start_generation(slug):
    """Start video generation in the background."""
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404

    data = request.json or {}
    count = data.get("count", 7)

    job_id = f"{slug}_{int(time.time())}"
    jobs[job_id] = {"status": "running", "message": "Starting generation...", "videos_created": 0}

    # Run generation in background thread
    thread = threading.Thread(
        target=_run_generation,
        args=(str(config_path), count, job_id),
        daemon=True,
    )
    thread.start()

    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/api/job/<job_id>", methods=["GET"])
def get_job_status(job_id):
    """Check the status of a background job."""
    if job_id not in jobs:
        return jsonify({"status": "not_found"}), 404
    return jsonify(jobs[job_id])


def _run_generation(config_path: str, count: int, job_id: str):
    """Background thread: run the full generation pipeline."""
    try:
        from script_generator import load_app_config, generate_scripts, save_scripts
        from image_generator import generate_images_for_script, generate_reference_image
        from voice_generator import generate_voiceover_for_script
        from subtitle_generator import (
            generate_word_timestamps,
            group_words_into_lines,
            calculate_slide_timings,
            save_subtitle_data,
        )
        from video_assembler import assemble_video, extract_key_frames

        app_config = load_app_config(config_path)
        app_name = app_config["app_name"]
        app_slug = app_name.lower().replace(" ", "_")
        app_slug = "".join(c for c in app_slug if c.isalnum() or c == "_")

        today = datetime.now().strftime("%Y-%m-%d")
        base_dir = OUTPUT_DIR / app_slug / today
        videos_dir = base_dir / "videos"

        voice_engine = os.environ.get("VOICE_ENGINE", "elevenlabs")
        image_engine = os.environ.get("IMAGE_ENGINE", "flux_schnell")

        # Step 1: Reference images
        jobs[job_id]["message"] = "Checking persona reference images..."
        ref_dir = OUTPUT_DIR / "reference_images"
        ref_dir.mkdir(parents=True, exist_ok=True)

        for persona in app_config.get("personas", []):
            ref_path = ref_dir / f"{app_slug}_{persona['id']}.png"
            if not ref_path.exists():
                jobs[job_id]["message"] = f"Generating reference image for {persona['name']}..."
                generate_reference_image(persona, str(ref_path))

        # Step 2: Generate scripts
        jobs[job_id]["message"] = f"Writing {count} video scripts with AI..."
        scripts = generate_scripts(app_config, count=count)
        scripts_dir = base_dir / "scripts"
        save_scripts(scripts, str(scripts_dir))

        # Step 3: Process each video
        videos_created = 0
        for i, script in enumerate(scripts):
            title = script.get("title", f"Video {i+1}")
            jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — generating images..."

            video_dir = base_dir / f"video_{i:03d}"
            video_dir.mkdir(parents=True, exist_ok=True)

            try:
                # Images
                persona = script.get("persona", app_config.get("personas", [{}])[0])
                ref_path = ref_dir / f"{app_slug}_{persona.get('id', 'default')}.png"
                ref_image = str(ref_path) if ref_path.exists() else None

                image_paths = generate_images_for_script(
                    script=script,
                    output_dir=str(video_dir / "images"),
                    app_config=app_config,
                    reference_image_path=ref_image,
                    engine=image_engine,
                )

                # Voiceover
                jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — generating voiceover..."
                voiceover_path = generate_voiceover_for_script(
                    script=script,
                    output_dir=str(video_dir / "audio"),
                    engine=voice_engine,
                )

                # Subtitles
                jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — generating subtitles..."
                if voiceover_path:
                    words = generate_word_timestamps(voiceover_path)
                    lines = group_words_into_lines(words)
                    slide_timings = calculate_slide_timings(words, script)
                else:
                    words, lines = [], []
                    slide_timings = [
                        {"slide_index": j, "start": j * 3.0, "end": (j + 1) * 3.0}
                        for j in range(len(script["slides"]))
                    ]

                subtitle_data = {"words": words, "lines": lines, "slide_timings": slide_timings}

                # Assemble
                jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — assembling video..."
                videos_dir.mkdir(parents=True, exist_ok=True)

                ts = int(time.time())
                video_filename = f"{app_slug}_{i:03d}_{ts}.mp4"
                video_path = str(videos_dir / video_filename)

                assemble_video(
                    script=script,
                    image_paths=image_paths,
                    voiceover_path=voiceover_path,
                    subtitle_data=subtitle_data,
                    output_path=video_path,
                    text_style=app_config.get("text_style"),
                )

                videos_created += 1
                jobs[job_id]["videos_created"] = videos_created

            except Exception as e:
                print(f"Error processing video {i+1}: {e}")
                continue

        jobs[job_id] = {
            "status": "done",
            "message": f"Created {videos_created} videos!",
            "videos_created": videos_created,
        }

    except Exception as e:
        jobs[job_id] = {
            "status": "error",
            "message": str(e),
            "videos_created": 0,
        }
        print(f"Generation error: {e}")


# ─── RUN ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  TikTok Content Factory")
    print("  Open http://localhost:5000 in your browser")
    print("=" * 50 + "\n")
    app.run(debug=True, port=5000)
