# yt-iptv on Raspberry Pi 4 — Setup Guide

Configured for Pi at **192.168.0.161**

---

## Prerequisites (on the Pi)

### 1. Install Docker & Docker Compose

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
```

Verify:
```bash
docker --version
docker compose version
```

> If `docker compose` isn't found (older installs), use `docker-compose` instead.

---

## Deploy

### 2. Copy this folder to your Pi

Transfer the entire `yt-iptv-rpi/` folder to your Pi. You can use SCP from your Mac/PC:

```bash
scp -r yt-iptv-rpi/ pi@192.168.0.161:~/yt-iptv-rpi
```

Or copy it via USB / any method you prefer.

### 3. Start the stack

SSH into your Pi, then:

```bash
cd ~/yt-iptv-rpi
docker compose up -d --build
```

This will:
- Build the `yt-iptv` image (downloads ffmpeg + yt-dlp, ~5 min first time)
- Pull the latest Jellyfin image for ARM
- Start both containers in the background

Check they're running:
```bash
docker compose ps
docker compose logs -f yt-iptv
```

---

## Access the services

| Service | URL |
|---|---|
| yt-iptv Channel Manager | http://192.168.0.161:5000 |
| M3U Playlist | http://192.168.0.161:5000/playlist.m3u |
| XMLTV EPG | http://192.168.0.161:5000/epg.xml |
| Jellyfin | http://192.168.0.161:8096 |

---

## Connect yt-iptv to Jellyfin

1. Open Jellyfin at **http://192.168.0.161:8096** and finish the first-run wizard.
2. Go to **Dashboard → Live TV → + (Add Tuner Device)**
   - Type: **M3U Tuner**
   - URL: `http://192.168.0.161:5000/playlist.m3u`
3. Go to **Dashboard → Live TV → + (Add TV Guide)** (EPG)
   - Type: **XMLTV**
   - URL: `http://192.168.0.161:5000/epg.xml`
4. Save, then go to **Live TV** in the sidebar — your YouTube channels will appear.

---

## Add / manage channels

Open **http://192.168.0.161:5000** in your browser. Paste any YouTube URL (live stream, video, or playlist) and click the **⟳** button to preview before adding.

---

## Useful commands

```bash
# View logs
docker compose logs -f

# Restart everything
docker compose restart

# Stop everything
docker compose down

# Update yt-dlp inside the container (fixes broken streams)
docker compose exec yt-iptv yt-dlp -U

# Rebuild after code changes
docker compose up -d --build yt-iptv
```

---

## Troubleshooting

**Streams not loading?** YouTube stream URLs expire. The proxy fetches a fresh URL on each play — if it fails, try refreshing. You can also update yt-dlp: `docker compose exec yt-iptv yt-dlp -U`

**Jellyfin not seeing channels?** After adding or removing channels, trigger a guide refresh in Jellyfin: Dashboard → Live TV → Refresh Guide.

**First build is slow** — normal. The Pi is downloading ~150 MB of packages (ffmpeg, yt-dlp). Subsequent starts are instant.
