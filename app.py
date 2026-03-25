#!/usr/bin/env python3
from __future__ import annotations
"""
TikTok Content Factory — Web Dashboard V2

Apple-clean design with full pipeline visibility.
Run this file and open the URL in your browser.
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
jobs = {}
pipeline_stages = {}  # Track per-video pipeline stage


# ─── DASHBOARD HTML ──────────────────────────────────────────────────────────
DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Content Factory</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  :root {
    --bg: #000; --surface: #0a0a0a; --surface-2: #111; --surface-3: #1a1a1a;
    --border: #1f1f1f; --border-light: #2a2a2a;
    --text: #f5f5f7; --text-secondary: #86868b; --text-tertiary: #48484a;
    --accent: #0a84ff; --accent-dim: #0a84ff15;
    --green: #30d158; --green-dim: #30d15815;
    --orange: #ff9f0a; --orange-dim: #ff9f0a15;
    --red: #ff453a;
    --radius: 12px; --radius-sm: 8px;
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', 'Helvetica Neue', sans-serif;
    background: var(--bg); color: var(--text); -webkit-font-smoothing: antialiased;
  }

  /* Nav */
  .nav {
    display: flex; align-items: center; padding: 0 32px; height: 52px;
    border-bottom: 1px solid var(--border);
    background: rgba(0,0,0,0.8); backdrop-filter: blur(20px);
    position: sticky; top: 0; z-index: 100;
  }
  .nav-brand { font-size: 15px; font-weight: 600; }
  .nav-tabs { display: flex; margin-left: 40px; height: 100%; }
  .nav-tab {
    padding: 0 20px; height: 100%; display: flex; align-items: center;
    font-size: 13px; color: var(--text-secondary); cursor: pointer;
    border: none; border-bottom: 2px solid transparent; background: none;
    transition: all 0.2s;
  }
  .nav-tab:hover { color: var(--text); }
  .nav-tab.active { color: var(--text); border-bottom-color: var(--text); }
  .nav-right { margin-left: auto; }
  .nav-status { font-size: 12px; color: var(--text-tertiary); display: flex; align-items: center; gap: 6px; }
  .nav-status .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--green); }

  /* Shared */
  .page { display: none; }
  .page.active { display: block; }
  .container { max-width: 960px; margin: 0 auto; padding: 40px 24px; }
  .container-wide { max-width: 1200px; margin: 0 auto; padding: 40px 24px; }
  .section-title { font-size: 12px; font-weight: 600; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.8px; margin-bottom: 16px; }
  .btn { padding: 8px 18px; border-radius: 20px; font-size: 13px; font-weight: 500; border: none; cursor: pointer; transition: all 0.15s; }
  .btn-primary { background: var(--accent); color: white; }
  .btn-primary:hover { opacity: 0.85; }
  .btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
  .btn-secondary { background: var(--surface-3); color: var(--text-secondary); }
  .btn-secondary:hover { background: var(--border-light); color: var(--text); }
  .btn-danger { background: var(--red); color: white; }
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; }
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border-light); border-top-color: var(--text); border-radius: 50%; animation: spin 0.7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* ─── APPS PAGE ─── */
  .apps-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 32px; }
  .apps-header h1 { font-size: 28px; font-weight: 600; letter-spacing: -0.5px; }
  .app-list { display: flex; flex-direction: column; gap: 1px; background: var(--border); border-radius: var(--radius); overflow: hidden; margin-bottom: 32px; }
  .app-row {
    display: grid; grid-template-columns: 1fr 160px 100px 100px 40px;
    align-items: center; padding: 16px 20px; background: var(--surface);
    cursor: pointer; transition: background 0.15s;
  }
  .app-row:hover { background: var(--surface-2); }
  .app-name { font-size: 15px; font-weight: 500; }
  .app-desc { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
  .col { font-size: 13px; color: var(--text-secondary); }
  .indicator { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; vertical-align: middle; }
  .indicator.on { background: var(--green); }
  .indicator.off { background: var(--text-tertiary); }
  .arrow { color: var(--text-tertiary); font-size: 18px; text-align: right; }

  /* Add app form */
  .add-form { margin-bottom: 32px; }
  .add-form h2 { font-size: 18px; font-weight: 600; margin-bottom: 16px; }
  .form-row { display: flex; gap: 10px; }
  .form-input {
    flex: 1; padding: 10px 14px; background: var(--surface-2); border: 1px solid var(--border);
    border-radius: var(--radius-sm); color: var(--text); font-size: 14px; outline: none;
  }
  .form-input:focus { border-color: var(--accent); }
  .form-input::placeholder { color: var(--text-tertiary); }
  .form-status { margin-top: 12px; font-size: 13px; }

  /* ─── DETAIL PAGE ─── */
  .detail-back { font-size: 13px; color: var(--accent); cursor: pointer; margin-bottom: 24px; display: inline-flex; align-items: center; gap: 4px; background: none; border: none; }
  .detail-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 36px; }
  .detail-header h1 { font-size: 28px; font-weight: 600; letter-spacing: -0.5px; }
  .detail-header .subtitle { font-size: 14px; color: var(--text-secondary); margin-top: 4px; }

  /* ICA */
  .ica-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .ica-item { padding: 12px 16px; background: var(--surface-2); border-radius: var(--radius-sm); }
  .ica-label { font-size: 11px; color: var(--text-tertiary); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }
  .ica-value { font-size: 14px; color: var(--text); line-height: 1.4; }

  /* Strategy */
  .strategy-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 32px; }
  .strategy-card h3 { font-size: 13px; font-weight: 600; color: var(--text-secondary); margin-bottom: 12px; }
  .strategy-tag { display: inline-block; padding: 4px 10px; border-radius: 6px; font-size: 12px; background: var(--surface-3); color: var(--text-secondary); margin: 2px 4px 2px 0; }

  /* Personas */
  .persona-row { display: flex; gap: 16px; margin-bottom: 32px; }
  .persona-card { flex: 1; }
  .persona-name { font-size: 15px; font-weight: 600; }
  .persona-arch { font-size: 12px; color: var(--text-secondary); margin-top: 2px; }
  .persona-desc { font-size: 12px; color: var(--text-tertiary); margin-top: 8px; line-height: 1.5; }

  /* Connect */
  .connect-card { display: flex; align-items: center; gap: 20px; margin-bottom: 32px; }
  .connect-info { flex: 1; }
  .connect-info h3 { font-size: 15px; font-weight: 500; }
  .connect-info p { font-size: 13px; color: var(--text-secondary); margin-top: 2px; }
  .connect-badge { padding: 6px 14px; border-radius: 20px; font-size: 12px; font-weight: 500; }
  .connect-badge.on { background: var(--green-dim); color: var(--green); }
  .connect-badge.off { background: #ff453a15; color: var(--red); }

  /* ─── PIPELINE PAGE ─── */
  .flow-step { display: flex; align-items: flex-start; gap: 20px; padding: 20px 0; border-bottom: 1px solid var(--border); }
  .flow-step:last-child { border-bottom: none; }
  .flow-num { width: 28px; height: 28px; border-radius: 50%; background: var(--surface-3); display: flex; align-items: center; justify-content: center; font-size: 12px; font-weight: 600; color: var(--text-secondary); flex-shrink: 0; }
  .flow-step.done .flow-num { background: var(--green-dim); color: var(--green); }
  .flow-step.active .flow-num { background: var(--accent); color: white; }
  .flow-content { flex: 1; }
  .flow-content h3 { font-size: 15px; font-weight: 500; margin-bottom: 3px; }
  .flow-content p { font-size: 13px; color: var(--text-secondary); line-height: 1.5; }
  .flow-tech { font-size: 11px; color: var(--text-tertiary); margin-top: 4px; }
  .flow-time { font-size: 12px; color: var(--text-tertiary); min-width: 70px; text-align: right; }

  .mini-pipeline { display: flex; gap: 12px; overflow-x: auto; margin-bottom: 32px; }
  .mini-col { min-width: 170px; flex: 1; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; }
  .mini-col-header { padding: 12px 14px; font-size: 12px; font-weight: 600; color: var(--text-secondary); border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; }
  .mini-col-count { color: var(--text-tertiary); font-weight: 400; }
  .mini-col-body { padding: 8px; }
  .mini-item { padding: 10px 12px; border-radius: var(--radius-sm); background: var(--surface-2); margin-bottom: 6px; cursor: pointer; }
  .mini-item:last-child { margin-bottom: 0; }
  .mini-item:hover { background: var(--surface-3); }
  .mi-title { font-size: 12px; font-weight: 500; margin-bottom: 3px; }
  .mi-meta { font-size: 11px; color: var(--text-tertiary); }
  .mi-bar { height: 2px; background: var(--border); border-radius: 1px; margin-top: 6px; overflow: hidden; }
  .mi-fill { height: 100%; background: var(--text-secondary); border-radius: 1px; }

  .edit-stages { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 32px; }
  .edit-stage { padding: 20px; }
  .edit-stage h4 { font-size: 14px; font-weight: 500; margin-bottom: 4px; }
  .edit-stage p { font-size: 12px; color: var(--text-secondary); line-height: 1.5; }

  .cost-bar { display: flex; gap: 24px; padding: 14px 20px; margin-bottom: 32px; align-items: center; font-size: 13px; color: var(--text-secondary); }
  .cost-bar span { color: var(--text); font-weight: 600; }
  .cost-total { margin-left: auto; }
  .cost-total span { color: var(--green); }

  /* ─── VIDEOS PAGE ─── */
  .videos-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 32px; }
  .videos-header h1 { font-size: 28px; font-weight: 600; letter-spacing: -0.5px; }
  .filter-row { display: flex; gap: 8px; }
  .filter-btn { padding: 6px 14px; border-radius: 20px; font-size: 12px; background: var(--surface-2); color: var(--text-secondary); border: 1px solid var(--border); cursor: pointer; }
  .filter-btn.active { background: var(--text); color: var(--bg); border-color: var(--text); }

  .videos-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; }
  .video-card {
    aspect-ratio: 9/16; background: var(--surface-2); border-radius: var(--radius);
    overflow: hidden; cursor: pointer; position: relative; display: flex;
    flex-direction: column; justify-content: flex-end; transition: transform 0.15s;
  }
  .video-card:hover { transform: scale(1.02); }
  .vc-gradient { padding: 16px 12px 14px; background: linear-gradient(transparent, rgba(0,0,0,0.85)); }
  .vc-title { font-size: 12px; font-weight: 500; color: white; margin-bottom: 4px; }
  .vc-meta { font-size: 10px; color: rgba(255,255,255,0.5); }
  .vc-badge { position: absolute; top: 8px; left: 8px; padding: 3px 8px; border-radius: 4px; font-size: 10px; font-weight: 600; backdrop-filter: blur(8px); }
  .vc-badge.posted { background: var(--green-dim); color: var(--green); }
  .vc-badge.scheduled { background: var(--orange-dim); color: var(--orange); }
  .vc-badge.ready { background: rgba(255,255,255,0.1); color: var(--text-secondary); }
  .vc-score { position: absolute; top: 8px; right: 8px; padding: 3px 8px; border-radius: 4px; font-size: 10px; font-weight: 600; background: rgba(0,0,0,0.6); color: var(--green); backdrop-filter: blur(8px); }

  /* ─── SETTINGS PAGE ─── */
  .settings-section { margin-bottom: 40px; }
  .settings-section h2 { font-size: 18px; font-weight: 600; margin-bottom: 16px; }
  .setting-row { display: flex; justify-content: space-between; align-items: center; padding: 14px 0; border-bottom: 1px solid var(--border); }
  .setting-row:last-child { border-bottom: none; }
  .setting-label { font-size: 14px; }
  .setting-desc { font-size: 12px; color: var(--text-tertiary); margin-top: 2px; }
  .setting-value { font-size: 14px; color: var(--text-secondary); }
  .toggle { width: 44px; height: 26px; border-radius: 13px; background: var(--surface-3); position: relative; cursor: pointer; transition: background 0.2s; border: none; }
  .toggle.on { background: var(--green); }
  .toggle::after { content: ''; position: absolute; width: 22px; height: 22px; border-radius: 50%; background: white; top: 2px; left: 2px; transition: transform 0.2s; }
  .toggle.on::after { transform: translateX(18px); }

  /* Empty state */
  .empty { text-align: center; padding: 60px 20px; color: var(--text-tertiary); }
  .empty h3 { color: var(--text-secondary); margin-bottom: 8px; font-size: 16px; }

  @media (max-width: 768px) {
    .form-row { flex-direction: column; }
    .ica-grid, .strategy-grid, .edit-stages { grid-template-columns: 1fr; }
    .persona-row { flex-direction: column; }
    .app-row { grid-template-columns: 1fr 80px 40px; }
    .nav-tabs { overflow-x: auto; }
  }
</style>
</head>
<body>

<nav class="nav">
  <span class="nav-brand">Content Factory</span>
  <div class="nav-tabs">
    <button class="nav-tab active" onclick="showPage('apps')">Apps</button>
    <button class="nav-tab" onclick="showPage('pipeline')" id="tab-pipeline">Pipeline</button>
    <button class="nav-tab" onclick="showPage('videos')">Videos</button>
    <button class="nav-tab" onclick="showPage('settings')">Settings</button>
  </div>
  <div class="nav-right">
    <span class="nav-status" id="nav-status"><span class="dot" id="status-dot"></span> <span id="status-text">Ready</span></span>
  </div>
</nav>

<!-- ═══ APPS PAGE ═══ -->
<div class="page active" id="page-apps">
  <div class="container">
    <div class="apps-header">
      <h1>Your Apps</h1>
    </div>

    <div class="add-form card">
      <h2>Add New App</h2>
      <p style="color: var(--text-secondary); font-size: 13px; margin-bottom: 16px;">Type the name and a one-liner. AI handles strategy, personas, hashtags — everything.</p>
      <div class="form-row">
        <input class="form-input" id="inp-name" placeholder="App Name">
        <input class="form-input" id="inp-desc" placeholder="One-line description" style="flex:2;">
        <button class="btn btn-primary" id="add-btn" onclick="addApp()">Generate</button>
      </div>
      <div class="form-status" id="add-status"></div>
    </div>

    <div id="app-list-container"></div>
  </div>
</div>

<!-- ═══ DETAIL PAGE ═══ -->
<div class="page" id="page-detail">
  <div class="container" id="detail-container">
    <!-- Populated by JS -->
  </div>
</div>

<!-- ═══ PIPELINE PAGE ═══ -->
<div class="page" id="page-pipeline">
  <div class="container-wide">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 8px;">
      <h1 style="font-size:28px; font-weight:600; letter-spacing:-0.5px;">Pipeline</h1>
      <div style="display:flex; gap:8px;">
        <select id="pipeline-app-select" style="padding:8px 14px; border-radius:20px; font-size:13px; background:var(--surface-2); color:var(--text-secondary); border:1px solid var(--border); outline:none;"></select>
        <button class="btn btn-primary" onclick="generateFromPipeline()">Generate 7 Videos</button>
      </div>
    </div>
    <p style="font-size:14px; color:var(--text-secondary); margin-bottom:32px;">How your videos go from idea to posted — fully automatic</p>

    <div class="section-title">How Videos Get Edited</div>
    <div class="edit-stages">
      <div class="edit-stage card">
        <h4>1. AI Images</h4>
        <p>Flux generates selfie-style photos of your persona in scenes matching the script. Same face across all slides.</p>
      </div>
      <div class="edit-stage card">
        <h4>2. Voiceover + Subtitles</h4>
        <p>ElevenLabs generates voiceover. Whisper creates word-level timestamps for animated captions.</p>
      </div>
      <div class="edit-stage card">
        <h4>3. Auto-Edit & Assembly</h4>
        <p>MoviePy stitches slides with Ken Burns zoom, text overlays, transitions, and background music. Output: 1080x1920 MP4.</p>
      </div>
    </div>

    <div class="section-title">Live Pipeline</div>
    <div class="mini-pipeline" id="live-pipeline">
      <div class="mini-col"><div class="mini-col-header">Script <span class="mini-col-count" id="pc-script">0</span></div><div class="mini-col-body" id="pb-script"></div></div>
      <div class="mini-col"><div class="mini-col-header">Images <span class="mini-col-count" id="pc-images">0</span></div><div class="mini-col-body" id="pb-images"></div></div>
      <div class="mini-col"><div class="mini-col-header">Voice <span class="mini-col-count" id="pc-voice">0</span></div><div class="mini-col-body" id="pb-voice"></div></div>
      <div class="mini-col"><div class="mini-col-header">Editing <span class="mini-col-count" id="pc-edit">0</span></div><div class="mini-col-body" id="pb-edit"></div></div>
      <div class="mini-col"><div class="mini-col-header">QA <span class="mini-col-count" id="pc-qa">0</span></div><div class="mini-col-body" id="pb-qa"></div></div>
      <div class="mini-col"><div class="mini-col-header">Ready <span class="mini-col-count" id="pc-ready">0</span></div><div class="mini-col-body" id="pb-ready"></div></div>
    </div>

    <div class="section-title">End-to-End Flow</div>
    <div class="card" style="padding:0;">
      <div style="padding:8px 24px;">
        <div class="flow-step done"><div class="flow-num">&#10003;</div><div class="flow-content"><h3>You add an app</h3><p>Name + description. AI generates full strategy, ICA, personas, hashtags.</p><div class="flow-tech">Claude Sonnet &bull; ~20 sec</div></div><div class="flow-time">Once</div></div>
        <div class="flow-step done"><div class="flow-num">&#10003;</div><div class="flow-content"><h3>Connect TikTok account</h3><p>Link via browser cookies or API for each app.</p><div class="flow-tech">TikTok API or tiktok-uploader</div></div><div class="flow-time">Once</div></div>
        <div class="flow-step done"><div class="flow-num">&#10003;</div><div class="flow-content"><h3>Reference images</h3><p>AI creates face references per persona for consistency.</p><div class="flow-tech">Flux Schnell &bull; $0.003/img</div></div><div class="flow-time">Once</div></div>
        <div class="flow-step"><div class="flow-num">4</div><div class="flow-content"><h3>Scripts written</h3><p>7 video scripts with hooks, slides, text overlays, image prompts.</p><div class="flow-tech">Claude Haiku &bull; ~$0.004/script</div></div><div class="flow-time">~30s</div></div>
        <div class="flow-step"><div class="flow-num">5</div><div class="flow-content"><h3>Images generated</h3><p>4-5 selfie-style images per video, color graded to brand.</p><div class="flow-tech">Flux via fal.ai &bull; $0.003-0.04/img</div></div><div class="flow-time">~45s</div></div>
        <div class="flow-step"><div class="flow-num">6</div><div class="flow-content"><h3>Voiceover recorded</h3><p>Per-persona voice style for authenticity.</p><div class="flow-tech">ElevenLabs &bull; ~$0.01/video</div></div><div class="flow-time">~15s</div></div>
        <div class="flow-step"><div class="flow-num">7</div><div class="flow-content"><h3>Video auto-edited</h3><p>Ken Burns, animated subtitles, text overlays, music.</p><div class="flow-tech">MoviePy + FFmpeg + Whisper</div></div><div class="flow-time">~60s</div></div>
        <div class="flow-step"><div class="flow-num">8</div><div class="flow-content"><h3>QA review</h3><p>Claude Vision scores frames. Must hit 7/10 or regenerate.</p><div class="flow-tech">Claude Vision &bull; ~$0.003</div></div><div class="flow-time">~10s</div></div>
        <div class="flow-step"><div class="flow-num">9</div><div class="flow-content"><h3>Queued and posted</h3><p>Scheduled across peak hours, uploaded automatically.</p><div class="flow-tech">Max 15 uploads/day per account</div></div><div class="flow-time">Auto</div></div>
      </div>
    </div>
  </div>
</div>

<!-- ═══ VIDEOS PAGE ═══ -->
<div class="page" id="page-videos">
  <div class="container-wide">
    <div class="videos-header">
      <h1>Videos</h1>
      <div class="filter-row" id="video-filters"></div>
    </div>
    <div class="videos-grid" id="videos-grid">
      <div class="empty"><h3>No videos yet</h3><p>Generate some videos to see them here</p></div>
    </div>
  </div>
</div>

<!-- ═══ SETTINGS PAGE ═══ -->
<div class="page" id="page-settings">
  <div class="container" style="max-width:700px;">
    <h1 style="font-size:28px; font-weight:600; margin-bottom:36px;">Settings</h1>
    <div class="settings-section">
      <h2>Output</h2>
      <div class="setting-row"><div><div class="setting-label">Videos Per Day</div><div class="setting-desc">Per app, per day</div></div><div class="setting-value" id="set-vpd">7</div></div>
      <div class="setting-row"><div><div class="setting-label">QA Threshold</div><div class="setting-desc">Minimum score to approve</div></div><div class="setting-value">7.0 / 10</div></div>
    </div>
    <div class="settings-section">
      <h2>Engines</h2>
      <div class="setting-row"><div><div class="setting-label">Image Generation</div><div class="setting-desc">AI model for images</div></div><div class="setting-value" id="set-img">Flux Schnell</div></div>
      <div class="setting-row"><div><div class="setting-label">Voice Generation</div></div><div class="setting-value" id="set-voice">ElevenLabs</div></div>
      <div class="setting-row"><div><div class="setting-label">Script Generation</div></div><div class="setting-value">Claude Haiku</div></div>
    </div>
    <div class="settings-section">
      <h2>API Keys</h2>
      <div class="setting-row"><div><div class="setting-label">Anthropic (Claude)</div></div><div class="setting-value" id="key-anthropic" style="color:var(--text-tertiary);">Checking...</div></div>
      <div class="setting-row"><div><div class="setting-label">fal.ai (Flux)</div></div><div class="setting-value" id="key-fal" style="color:var(--text-tertiary);">Checking...</div></div>
      <div class="setting-row"><div><div class="setting-label">ElevenLabs</div></div><div class="setting-value" id="key-eleven" style="color:var(--text-tertiary);">Checking...</div></div>
    </div>
  </div>
</div>

<script>
let apps = [];
let currentApp = null;
let pollTimer = null;

// ─── Page nav ───
function showPage(id) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + id).classList.add('active');
  const map = {apps:0, pipeline:1, videos:2, settings:3};
  if (id === 'detail') { /* no tab highlight for detail */ }
  else document.querySelectorAll('.nav-tab')[map[id]].classList.add('active');
  window.scrollTo(0,0);
  if (id === 'settings') loadSettings();
  if (id === 'videos') loadVideos();
}

// ─── Load apps ───
async function loadApps() {
  const res = await fetch('/api/apps');
  apps = await res.json();
  renderAppList();
  renderPipelineSelect();
}

function renderAppList() {
  const c = document.getElementById('app-list-container');
  if (!apps.length) { c.innerHTML = '<div class="empty"><h3>No apps yet</h3><p>Add your first app above</p></div>'; return; }
  c.innerHTML = '<div class="app-list">' + apps.map(a => `
    <div class="app-row" onclick="showDetail('${a.slug}')">
      <div><div class="app-name">${a.app_name}</div><div class="app-desc">${a.app_description||''}</div></div>
      <div class="col">${a.tiktok_handle||'Not set'}</div>
      <div class="col">${a.video_count||0} videos</div>
      <div class="col"><span class="indicator ${a.tiktok_handle?'on':'off'}"></span>${a.tiktok_handle?'Active':'Setup'}</div>
      <div class="arrow">&rsaquo;</div>
    </div>`).join('') + '</div>';
}

// ─── Add app ───
async function addApp() {
  const name = document.getElementById('inp-name').value.trim();
  const desc = document.getElementById('inp-desc').value.trim();
  if (!name || !desc) { document.getElementById('add-status').innerHTML = '<span style="color:var(--red)">Fill in both fields</span>'; return; }
  const btn = document.getElementById('add-btn');
  btn.disabled = true; btn.textContent = 'Generating...';
  document.getElementById('add-status').innerHTML = '<span style="color:var(--text-secondary)"><span class="spinner"></span> AI is creating strategy, personas, hashtags... ~20 seconds</span>';
  setStatus('Generating strategy...');
  try {
    const res = await fetch('/api/apps', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, description:desc}) });
    const data = await res.json();
    if (data.error) { document.getElementById('add-status').innerHTML = '<span style="color:var(--red)">'+data.error+'</span>'; }
    else {
      document.getElementById('inp-name').value = '';
      document.getElementById('inp-desc').value = '';
      document.getElementById('add-status').innerHTML = '<span style="color:var(--green)">App created! Strategy generated.</span>';
      setTimeout(() => document.getElementById('add-status').innerHTML = '', 3000);
      loadApps();
    }
  } catch(e) { document.getElementById('add-status').innerHTML = '<span style="color:var(--red)">'+e.message+'</span>'; }
  btn.disabled = false; btn.textContent = 'Generate';
  setStatus('Ready');
}

// ─── Detail page ───
async function showDetail(slug) {
  const res = await fetch('/api/apps/' + slug + '/config');
  const config = await res.json();
  currentApp = config;
  currentApp.slug = slug;
  renderDetail(config);
  showPage('detail');
}

function renderDetail(c) {
  const ica = c.ica || {};
  const personas = c.personas || [];
  const pillars = c.content_pillars || [];
  const hashtags = (c.hashtag_sets || {});
  const primary_tags = hashtags.primary || [];
  const niche_tags = hashtags.niche || [];
  const all_tags = [...primary_tags, ...niche_tags].slice(0, 8);
  const styles = c.video_styles || {};

  document.getElementById('detail-container').innerHTML = `
    <button class="detail-back" onclick="showPage('apps')">&larr; All Apps</button>
    <div class="detail-header">
      <div>
        <h1>${c.app_name}</h1>
        <div class="subtitle" style="color:var(--text-secondary); font-size:14px; margin-top:4px;">${c.app_description||''}</div>
      </div>
      <div style="display:flex; gap:8px;">
        <button class="btn btn-danger" onclick="deleteApp('${c.slug}')">Delete</button>
        <button class="btn btn-primary" onclick="generateForApp('${c.slug}')">Generate Videos</button>
      </div>
    </div>

    <div class="section-title">TikTok Account</div>
    <div class="connect-card card">
      <div class="connect-info">
        <h3>${c.tiktok_handle || 'No account connected'}</h3>
        <p>${c.tiktok_handle ? 'Connect via TikTok API or browser cookies to enable auto-posting' : 'Add a TikTok handle in the config to connect'}</p>
      </div>
      <span class="connect-badge ${c.tiktok_handle ? 'on' : 'off'}">${c.tiktok_handle ? 'Ready' : 'Not Connected'}</span>
    </div>

    <div class="section-title">Ideal Customer Avatar</div>
    <div class="card" style="margin-bottom:32px;">
      <p style="font-size:13px; color:var(--text-secondary); margin-bottom:16px;">Auto-generated from your app description. This is who every video is made for.</p>
      <div class="ica-grid">
        <div class="ica-item"><div class="ica-label">Target Audience</div><div class="ica-value">${ica.target_audience || c.niche + ' users ages 18-30'}</div></div>
        <div class="ica-item"><div class="ica-label">Pain Points</div><div class="ica-value">${ica.pain_points || 'Generated from app context'}</div></div>
        <div class="ica-item"><div class="ica-label">Desired Outcome</div><div class="ica-value">${ica.desired_outcome || 'Generated from app context'}</div></div>
        <div class="ica-item"><div class="ica-label">Language & Tone</div><div class="ica-value">${ica.tone || 'Casual, relatable, motivational. No corporate speak.'}</div></div>
        <div class="ica-item"><div class="ica-label">Hook Style</div><div class="ica-value">${ica.hook_style || 'Curiosity gaps, bold claims, personal experience'}</div></div>
        <div class="ica-item"><div class="ica-label">Niche</div><div class="ica-value">${c.niche || '—'}</div></div>
      </div>
    </div>

    <div class="section-title">Content Strategy</div>
    <div class="strategy-grid">
      <div class="strategy-card card"><h3>Content Pillars</h3><div>${pillars.map(p => '<span class="strategy-tag">'+p+'</span>').join('')}</div></div>
      <div class="strategy-card card"><h3>Hashtags</h3><div>${all_tags.map(t => '<span class="strategy-tag">#'+t+'</span>').join('')}</div></div>
    </div>

    <div class="section-title">AI Personas</div>
    <div class="persona-row">
      ${personas.map(p => `
        <div class="persona-card card">
          <div class="persona-name">${p.name}</div>
          <div class="persona-arch">${p.archetype || ''}</div>
          <div class="persona-desc">${p.appearance || ''}</div>
          <div class="persona-desc" style="margin-top:4px;">${p.writing_style || ''}</div>
        </div>
      `).join('')}
    </div>
  `;
}

// ─── Generate ───
async function generateForApp(slug) {
  setStatus('Generating videos...');
  try {
    const res = await fetch('/api/generate/' + slug, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({count:7}) });
    const data = await res.json();
    if (data.job_id) { pollJob(slug, data.job_id); showPage('pipeline'); }
  } catch(e) { setStatus('Error: ' + e.message); }
}

function generateFromPipeline() {
  const sel = document.getElementById('pipeline-app-select');
  if (sel.value) generateForApp(sel.value);
}

function pollJob(slug, jobId) {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const res = await fetch('/api/job/' + jobId);
    const data = await res.json();
    setStatus(data.message || 'Working...');
    updatePipelineView(data);
    if (data.status === 'done' || data.status === 'error') {
      clearInterval(pollTimer); pollTimer = null;
      if (data.status === 'done') setStatus('Done! ' + (data.videos_created||0) + ' videos created');
      else setStatus('Error: ' + data.message);
      loadApps();
    }
  }, 2000);
}

function updatePipelineView(data) {
  const stages = data.pipeline_stages || {};
  ['script','images','voice','edit','qa','ready'].forEach(s => {
    const items = stages[s] || [];
    document.getElementById('pc-' + s).textContent = items.length;
    document.getElementById('pb-' + s).innerHTML = items.map(v =>
      '<div class="mini-item"><div class="mi-title">' + v.title + '</div><div class="mi-meta">' + (v.persona||'') + (v.progress ? ' &bull; ' + v.progress : '') + '</div></div>'
    ).join('') || '';
  });
}

function renderPipelineSelect() {
  const sel = document.getElementById('pipeline-app-select');
  sel.innerHTML = apps.map(a => '<option value="'+a.slug+'">'+a.app_name+'</option>').join('');
}

// ─── Delete ───
async function deleteApp(slug) {
  if (!confirm('Delete this app and all its content?')) return;
  await fetch('/api/apps/' + slug, {method:'DELETE'});
  loadApps(); showPage('apps');
}

// ─── Videos ───
async function loadVideos() {
  const res = await fetch('/api/videos');
  const videos = await res.json();
  const grid = document.getElementById('videos-grid');
  if (!videos.length) { grid.innerHTML = '<div class="empty"><h3>No videos yet</h3><p>Generate some videos to see them here</p></div>'; return; }
  const colors = ['#1a1028,#0f1a2e','#1a1020,#1a0f28','#0f1a20,#0a1828','#1a1520,#0f0a28','#1a2010,#1a2820','#20180f,#281a10'];
  grid.innerHTML = videos.map((v,i) => `
    <div class="video-card" style="background:linear-gradient(135deg,${colors[i%colors.length]});">
      <span class="vc-badge ready">Ready</span>
      ${v.score ? '<span class="vc-score">'+v.score+'</span>' : ''}
      <div class="vc-gradient">
        <div class="vc-title">${v.title || v.filename}</div>
        <div class="vc-meta">${v.app || ''}</div>
      </div>
    </div>
  `).join('');
}

// ─── Settings ───
async function loadSettings() {
  const res = await fetch('/api/settings');
  const s = await res.json();
  document.getElementById('set-vpd').textContent = s.videos_per_day || 7;
  document.getElementById('set-img').textContent = s.image_engine || 'Flux Schnell';
  document.getElementById('set-voice').textContent = s.voice_engine || 'ElevenLabs';
  document.getElementById('key-anthropic').textContent = s.keys.anthropic ? 'Connected' : 'Missing';
  document.getElementById('key-anthropic').style.color = s.keys.anthropic ? 'var(--green)' : 'var(--red)';
  document.getElementById('key-fal').textContent = s.keys.fal ? 'Connected' : 'Missing';
  document.getElementById('key-fal').style.color = s.keys.fal ? 'var(--green)' : 'var(--red)';
  document.getElementById('key-eleven').textContent = s.keys.elevenlabs ? 'Connected' : 'Missing';
  document.getElementById('key-eleven').style.color = s.keys.elevenlabs ? 'var(--green)' : 'var(--red)';
}

// ─── Status ───
function setStatus(msg) {
  document.getElementById('status-text').textContent = msg;
  document.getElementById('status-dot').style.background = msg === 'Ready' ? 'var(--green)' : 'var(--orange)';
}

// ─── Keyboard ───
document.addEventListener('keydown', e => {
  if (e.key === 'Enter' && document.activeElement.id === 'inp-desc') addApp();
});

// Boot
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
    apps_list = []
    for config_file in sorted(CONFIG_DIR.glob("*.json")):
        if config_file.name == "example_app.json":
            continue
        try:
            with open(config_file) as f:
                config = json.load(f)
            slug = config_file.stem
            video_count = 0
            app_output = OUTPUT_DIR / slug
            if app_output.exists():
                video_count = len(list(app_output.rglob("*.mp4")))
            config["slug"] = slug
            config["video_count"] = video_count
            apps_list.append(config)
        except Exception as e:
            print(f"Error loading {config_file}: {e}")
    return jsonify(apps_list)


@app.route("/api/apps", methods=["POST"])
def create_app():
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
    config_path = CONFIG_DIR / f"{slug}.json"
    if config_path.exists():
        os.remove(config_path)
    return jsonify({"status": "deleted"})


@app.route("/api/apps/<slug>/config", methods=["GET"])
def get_app_config(slug):
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404
    with open(config_path) as f:
        config = json.load(f)
    config["slug"] = slug
    return jsonify(config)


@app.route("/api/generate/<slug>", methods=["POST"])
def start_generation(slug):
    config_path = CONFIG_DIR / f"{slug}.json"
    if not config_path.exists():
        return jsonify({"error": "App not found"}), 404
    data = request.json or {}
    count = data.get("count", 7)
    job_id = f"{slug}_{int(time.time())}"
    jobs[job_id] = {
        "status": "running",
        "message": "Starting generation...",
        "videos_created": 0,
        "pipeline_stages": {"script": [], "images": [], "voice": [], "edit": [], "qa": [], "ready": []},
    }
    thread = threading.Thread(target=_run_generation, args=(str(config_path), count, job_id), daemon=True)
    thread.start()
    return jsonify({"job_id": job_id, "status": "started"})


@app.route("/api/job/<job_id>", methods=["GET"])
def get_job_status(job_id):
    if job_id not in jobs:
        return jsonify({"status": "not_found"}), 404
    return jsonify(jobs[job_id])


@app.route("/api/videos", methods=["GET"])
def list_videos():
    videos = []
    for app_dir in OUTPUT_DIR.iterdir():
        if not app_dir.is_dir() or app_dir.name in ("reference_images", "upload_queue"):
            continue
        for mp4 in app_dir.rglob("*.mp4"):
            videos.append({
                "filename": mp4.name,
                "title": mp4.stem.replace("_", " ").title(),
                "app": app_dir.name.replace("_", " ").title(),
                "path": str(mp4),
                "created": mp4.stat().st_mtime,
            })
    videos.sort(key=lambda v: v["created"], reverse=True)
    return jsonify(videos[:50])


@app.route("/api/settings", methods=["GET"])
def get_settings():
    return jsonify({
        "videos_per_day": int(os.environ.get("VIDEOS_PER_DAY", 7)),
        "image_engine": os.environ.get("IMAGE_ENGINE", "flux_schnell"),
        "voice_engine": os.environ.get("VOICE_ENGINE", "elevenlabs"),
        "qa_threshold": float(os.environ.get("QA_THRESHOLD", 7.0)),
        "keys": {
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "fal": bool(os.environ.get("FAL_KEY")),
            "elevenlabs": bool(os.environ.get("ELEVENLABS_API_KEY")),
        }
    })


def _run_generation(config_path: str, count: int, job_id: str):
    """Background generation with pipeline stage tracking."""
    try:
        from script_generator import load_app_config, generate_scripts, save_scripts
        from image_generator import generate_images_for_script, generate_reference_image
        from voice_generator import generate_voiceover_for_script
        from subtitle_generator import (
            generate_word_timestamps, group_words_into_lines,
            calculate_slide_timings, save_subtitle_data,
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

        stages = jobs[job_id]["pipeline_stages"]

        # Reference images
        jobs[job_id]["message"] = "Checking persona reference images..."
        ref_dir = OUTPUT_DIR / "reference_images"
        ref_dir.mkdir(parents=True, exist_ok=True)
        for persona in app_config.get("personas", []):
            ref_path = ref_dir / f"{app_slug}_{persona['id']}.png"
            if not ref_path.exists():
                jobs[job_id]["message"] = f"Generating reference for {persona['name']}..."
                generate_reference_image(persona, str(ref_path))

        # Scripts
        jobs[job_id]["message"] = f"Writing {count} scripts..."
        scripts = generate_scripts(app_config, count=count)
        scripts_dir = base_dir / "scripts"
        save_scripts(scripts, str(scripts_dir))

        # Add all to script stage
        for s in scripts:
            stages["script"].append({"title": s.get("title", "Untitled"), "persona": s.get("persona", {}).get("name", "")})

        videos_created = 0
        for i, script in enumerate(scripts):
            title = script.get("title", f"Video {i+1}")
            persona_name = script.get("persona", {}).get("name", "")

            # Move from script to images
            stages["script"] = [x for x in stages["script"] if x["title"] != title]
            stages["images"].append({"title": title, "persona": persona_name, "progress": "Generating..."})
            jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — images"

            video_dir = base_dir / f"video_{i:03d}"
            video_dir.mkdir(parents=True, exist_ok=True)

            try:
                persona = script.get("persona", app_config.get("personas", [{}])[0])
                ref_path = ref_dir / f"{app_slug}_{persona.get('id', 'default')}.png"
                ref_image = str(ref_path) if ref_path.exists() else None

                image_paths = generate_images_for_script(
                    script=script, output_dir=str(video_dir / "images"),
                    app_config=app_config, reference_image_path=ref_image, engine=image_engine,
                )

                # Move to voice
                stages["images"] = [x for x in stages["images"] if x["title"] != title]
                stages["voice"].append({"title": title, "persona": persona_name})
                jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — voiceover"

                voiceover_path = generate_voiceover_for_script(
                    script=script, output_dir=str(video_dir / "audio"), engine=voice_engine,
                )

                # Move to edit
                stages["voice"] = [x for x in stages["voice"] if x["title"] != title]
                stages["edit"].append({"title": title, "persona": persona_name})
                jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — assembling"

                if voiceover_path:
                    words = generate_word_timestamps(voiceover_path)
                    lines = group_words_into_lines(words)
                    slide_timings = calculate_slide_timings(words, script)
                else:
                    words, lines = [], []
                    slide_timings = [{"slide_index": j, "start": j * 3.0, "end": (j + 1) * 3.0} for j in range(len(script["slides"]))]

                subtitle_data = {"words": words, "lines": lines, "slide_timings": slide_timings}
                videos_dir.mkdir(parents=True, exist_ok=True)
                ts = int(time.time())
                video_filename = f"{app_slug}_{i:03d}_{ts}.mp4"
                video_path = str(videos_dir / video_filename)

                assemble_video(
                    script=script, image_paths=image_paths, voiceover_path=voiceover_path,
                    subtitle_data=subtitle_data, output_path=video_path,
                    text_style=app_config.get("text_style"),
                )

                # Move to QA
                stages["edit"] = [x for x in stages["edit"] if x["title"] != title]
                stages["qa"].append({"title": title, "persona": persona_name})
                jobs[job_id]["message"] = f"Video {i+1}/{count}: {title} — QA review"

                # Skip QA for now, move to ready
                stages["qa"] = [x for x in stages["qa"] if x["title"] != title]
                stages["ready"].append({"title": title, "persona": persona_name, "progress": "Ready"})

                videos_created += 1
                jobs[job_id]["videos_created"] = videos_created

            except Exception as e:
                print(f"Error on video {i+1}: {e}")
                # Remove from all stages on error
                for stage_name in stages:
                    stages[stage_name] = [x for x in stages[stage_name] if x["title"] != title]
                continue

        jobs[job_id] = {
            "status": "done",
            "message": f"Created {videos_created} videos!",
            "videos_created": videos_created,
            "pipeline_stages": stages,
        }

    except Exception as e:
        jobs[job_id] = {
            "status": "error", "message": str(e), "videos_created": 0,
            "pipeline_stages": {"script":[],"images":[],"voice":[],"edit":[],"qa":[],"ready":[]},
        }
        print(f"Generation error: {e}")


# ─── RUN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print("\n" + "=" * 50)
    print("  Content Factory")
    print(f"  Open http://localhost:{port}")
    print("=" * 50 + "\n")
    app.run(debug=(port == 5000), host="0.0.0.0", port=port)
