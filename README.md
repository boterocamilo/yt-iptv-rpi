# yt-iptv-rpi

Turn any YouTube channel into a live TV channel — complete with EPG (programme guide), 1080p streaming, and automatic daily scheduling. Built to run on a Raspberry Pi and integrate with [Jellyfin](https://jellyfin.org/).

---

## How it works

- You add a YouTube channel URL via the web UI
- The app fetches up to 200 videos from that channel and builds a **daily schedule** (fresh random order every day, loops when all videos have played)
- The schedule is exposed as an **XMLTV EPG** so Jellyfin shows titles, times, and thumbnails — exactly like real TV
- When you tune in, it streams the video that is scheduled for **right now**, seeking to the correct position (if a video started 10 minutes ago, you join 10 minutes in)
- Stream URLs are pre-resolved in the background so playback starts fast
- When a video ends, the next scheduled one starts automatically — no gaps

---

## Requirements

- Raspberry Pi 4 (or any Linux machine with Docker)
- Docker + Docker Compose
- A local network (or port-forwarded setup)

---

## Quick start

### 1. Install Docker (if not already installed)

```bash
curl -fsSL https://get.docker.com | sh && sudo usermod -aG docker $USER && newgrp docker
sudo systemctl enable docker
```

### 2. Clone the repo

```bash
git clone https://github.com/YOUR_USERNAME/yt-iptv-rpi.git
cd yt-iptv-rpi
```

### 3. Configure your IP

```bash
cp .env.example .env
nano .env   # set RPI_IP to your Pi's local IP, e.g. 192.168.1.100
```

### 4. Launch

```bash
docker compose up -d --build
```

The first build downloads ffmpeg and yt-dlp for ARM — allow ~5 minutes.

---

## Access

| Service | URL |
|---|---|
| Channel Manager | `http://<RPI_IP>:5000` |
| Jellyfin | `http://<RPI_IP>:8096` |

---

## Jellyfin setup

1. Open Jellyfin at `http://<RPI_IP>:8096` and complete the initial setup wizard
2. Go to **Dashboard → Live TV**
3. Add a **Tuner Device** → type **M3U Tuner** → URL: `http://<RPI_IP>:5000/playlist.m3u`
4. Add a **TV Guide Data Provider** → type **XMLTV** → URL: `http://<RPI_IP>:5000/epg.xml`
5. Go to **Dashboard → Live TV → Guide** → click **Refresh Guide Data**
6. Open **Live TV** in Jellyfin — your channels appear with full EPG

---

## Adding channels

1. Open the Channel Manager at `http://<RPI_IP>:5000`
2. Paste a YouTube channel URL, e.g. `https://www.youtube.com/@SomeChannel/videos`
3. Fill in a name and group (optional)
4. Click **Fetch Preview** then **Add to Jellyfin**
5. The app fetches up to 200 videos and builds the schedule — takes 1–3 minutes
6. Refresh the Jellyfin guide to see the new channel

---

## Architecture

```
Jellyfin player
    │
    ├── GET /playlist.m3u      → list of channels
    ├── GET /epg.xml           → XMLTV programme guide (built from daily schedule)
    └── GET /stream/<id>       → continuous mpegts stream
                                    │
                                    ├── looks up current scheduled video
                                    ├── seeks to correct offset (live-TV behaviour)
                                    ├── ffmpeg merges video+audio → mpegts
                                    └── when video ends → next scheduled video starts
```

### Key components

| File | Purpose |
|---|---|
| `yt-iptv/stream_proxy.py` | Flask app — all routing, scheduling, streaming, EPG |
| `yt-iptv/ui/index.html` | Channel Manager web UI |
| `yt-iptv/Dockerfile` | Container definition (Python 3.11 + ffmpeg + yt-dlp) |
| `docker-compose.yml` | Orchestrates yt-iptv + Jellyfin |

---

## Configuration

All config is at the top of `stream_proxy.py`:

| Variable | Default | Description |
|---|---|---|
| `PLAYLIST_MAX_ENTRIES` | `200` | Max videos fetched per channel |
| `EPG_REFRESH_INTERVAL` | `3600` | Seconds between full EPG refreshes |
| `SURL_TTL` | `14400` | Stream URL cache TTL in seconds (4 hours) |

---

## Updating yt-dlp

YouTube changes frequently. If streams stop working, update yt-dlp:

```bash
docker exec yt-iptv yt-dlp -U
```

---

## Troubleshooting

**Channel not playing**
```bash
docker logs yt-iptv --tail 50
```

**EPG not showing in Jellyfin**
- Trigger a manual refresh: `curl http://<RPI_IP>:5000/epg/refresh`
- Then refresh the guide in Jellyfin Dashboard → Live TV → Guide

**Check schedule and cache status**
```bash
curl http://<RPI_IP>:5000/health | python3 -m json.tool
```

---

## Limitations

- YouTube's CDN URLs expire after ~4 hours — the app refreshes them automatically
- 1080p requires H.264 (avc1) to be available for the video; falls back to 720p if not
- Not all YouTube channels allow scraping — some may return no results

---

## License

MIT
