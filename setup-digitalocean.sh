#!/bin/bash
# ═══════════════════════════════════════════════════════════════
#  TikTok Content Factory — DigitalOcean One-Click Setup
#  Paste this entire script into the Droplet console.
#  It installs everything and starts your app permanently.
# ═══════════════════════════════════════════════════════════════

set -e
echo "══════════════════════════════════════════"
echo "  Setting up TikTok Content Factory..."
echo "══════════════════════════════════════════"

# 1. System updates + dependencies
echo "[1/6] Installing system packages..."
apt-get update -qq
apt-get install -y -qq python3 python3-pip python3-venv git ffmpeg nginx > /dev/null 2>&1

# 2. Clone the repo
echo "[2/6] Downloading app from GitHub..."
cd /opt
if [ -d "tiktok-factory" ]; then
    cd tiktok-factory && git pull origin master
else
    git clone https://github.com/quinten-dotcom/tiktok-content-factory.git tiktok-factory
    cd tiktok-factory
fi

# 3. Create data directory (persistent app data lives here)
echo "[3/6] Setting up persistent data directory..."
mkdir -p /opt/tiktok-factory-data/config
mkdir -p /opt/tiktok-factory-data/output

# 4. Install Python packages
echo "[4/6] Installing Python packages..."
pip3 install --break-system-packages -q -r requirements.txt 2>/dev/null || pip3 install -q -r requirements.txt

# 5. Create systemd service (auto-start on boot, restart on crash)
echo "[5/6] Setting up auto-start service..."
cat > /etc/systemd/system/tiktok-factory.service << 'SERVICEEOF'
[Unit]
Description=TikTok Content Factory
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tiktok-factory
Environment=PORT=8080
Environment=DATA_DIR=/opt/tiktok-factory-data
ExecStart=/usr/bin/python3 app.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable tiktok-factory
systemctl restart tiktok-factory

# 6. Set up nginx as reverse proxy (port 80 → 8080)
echo "[6/6] Setting up web server..."
cat > /etc/nginx/sites-available/tiktok-factory << 'NGINXEOF'
server {
    listen 80;
    server_name _;
    client_max_body_size 500M;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 300s;
        proxy_connect_timeout 300s;
    }
}
NGINXEOF

rm -f /etc/nginx/sites-enabled/default
ln -sf /etc/nginx/sites-available/tiktok-factory /etc/nginx/sites-enabled/
nginx -t -q && systemctl restart nginx

# Open firewall
ufw allow 80/tcp > /dev/null 2>&1 || true
ufw allow 443/tcp > /dev/null 2>&1 || true

echo ""
echo "══════════════════════════════════════════"
echo "  DONE! Your app is live at:"
echo "  http://$(curl -s ifconfig.me)"
echo ""
echo "  Data stored in: /opt/tiktok-factory-data/"
echo "  (survives updates forever)"
echo "══════════════════════════════════════════"
