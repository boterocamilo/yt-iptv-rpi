#!/usr/bin/env python3
"""
yt-iptv: YouTube → Live TV proxy for Jellyfin.
Raspberry Pi 4 optimised (arm64/armhf).

Routes (Jellyfin):
  GET  /playlist.m3u          - M3U playlist
  GET  /stream/<id>           - Continuous mpegts stream (schedule-aware)
  GET  /epg.xml               - XMLTV EPG (built from daily schedule)
  GET  /epg/refresh           - Trigger refresh

Routes (Management UI):
  GET  /                      - Channel manager UI
  GET  /api/channels          - List channels (JSON)
  POST /api/channels          - Add channel
  DELETE /api/channels/<id>   - Remove channel
  POST /api/preview           - Preview URL metadata

Routes (Misc):
  GET  /health                - Health + schedule status
"""
import subprocess, json, datetime, threading, html, time, logging, uuid, os, random
from pathlib import Path
from flask import Flask, Response, abort, jsonify, request, send_from_directory
from flask_cors import CORS

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("yt-iptv")
app = Flask(__name__, static_folder="ui", static_url_path="/ui")
CORS(app)

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────
SERVER_HOST          = "0.0.0.0"
SERVER_PORT          = 5000
PROXY_BASE_URL       = os.environ.get("PROXY_BASE_URL", "http://YOUR_RPI_IP:5000")
EPG_REFRESH_INTERVAL = 3600          # seconds between full refreshes
PLAYLIST_MAX_ENTRIES = 200           # videos fetched per channel
SURL_TTL             = 4 * 3600     # stream URL cache TTL (4 h)
CHANNELS_FILE        = Path("/data/channels.json")

# ─────────────────────────────────────────────────────────────
# Channel persistence
# ─────────────────────────────────────────────────────────────
_channels_lock = threading.Lock()
DEFAULT_CHANNELS = [
    {"id": "ch1", "name": "Lofi Hip Hop Radio",
     "logo": "https://i.imgur.com/4M7IWwP.png",
     "url": "https://www.youtube.com/watch?v=jfKfPfyJRdk",
     "group": "Music", "playlist": False},
    {"id": "ch2", "name": "NASA Live",
     "logo": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/e5/NASA_logo.svg/200px-NASA_logo.svg.png",
     "url": "https://www.youtube.com/watch?v=21X5lGlDOfg",
     "group": "Science", "playlist": False},
]

def _load_channels() -> list:
    CHANNELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    if CHANNELS_FILE.exists():
        try:
            return json.loads(CHANNELS_FILE.read_text())
        except Exception as e:
            log.error(f"[channels] Read error: {e}")
    _save_channels(DEFAULT_CHANNELS)
    return list(DEFAULT_CHANNELS)

def _save_channels(channels: list):
    CHANNELS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHANNELS_FILE.write_text(json.dumps(channels, indent=2))

def get_channels() -> list:
    with _channels_lock:
        return _load_channels()

def save_channels(channels: list):
    with _channels_lock:
        _save_channels(channels)

# ─────────────────────────────────────────────────────────────
# yt-dlp helpers
# ─────────────────────────────────────────────────────────────
def _run_ytdlp(args: list, timeout: int = 60):
    try:
        return subprocess.run(["yt-dlp"] + args,
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("yt-dlp timed out")
    except FileNotFoundError:
        log.error("yt-dlp not found")
    except Exception as e:
        log.error(f"yt-dlp error: {e}")
    return None

def _best_thumb(thumbs) -> str:
    if not thumbs:
        return ""
    best = sorted([t for t in thumbs if t.get("url")],
                  key=lambda t: t.get("width", 0) * t.get("height", 0),
                  reverse=True)
    return best[0]["url"] if best else ""

# ─────────────────────────────────────────────────────────────
# Video metadata cache  (title, duration, thumbnail per video)
# ─────────────────────────────────────────────────────────────
_video_cache: dict = {}   # {channel_id: {"entries": [...], "fetched_at": float}}
_vcache_lock = threading.Lock()

def _refresh_video_cache(channel: dict):
    url = channel["url"]
    is_channel = any(x in url for x in
                     ["/@", "/channel/", "/user/", "/c/", "/videos", "playlist?list="])
    if not is_channel:
        return

    log.info(f"[vcache] Fetching up to {PLAYLIST_MAX_ENTRIES} videos for '{channel['name']}'...")
    result = _run_ytdlp([
        "--flat-playlist", "--playlist-end", str(PLAYLIST_MAX_ENTRIES),
        "--dump-json", "--no-warnings", url
    ], timeout=300)

    if not result or not result.stdout.strip():
        log.warning(f"[vcache] No results for '{channel['name']}'")
        return

    entries = []
    for line in result.stdout.strip().splitlines():
        try:
            data = json.loads(line)
            video_url = data.get("url") or data.get("webpage_url")
            if not video_url:
                continue
            entries.append({
                "url":       video_url,
                "title":     data.get("title") or "Unknown",
                "duration":  max(int(data.get("duration") or 600), 1),
                "thumbnail": (data.get("thumbnail")
                              or _best_thumb(data.get("thumbnails")) or ""),
            })
        except Exception:
            pass

    if entries:
        with _vcache_lock:
            _video_cache[channel["id"]] = {
                "entries":    entries,
                "fetched_at": time.time(),
            }
        log.info(f"[vcache] Cached {len(entries)} videos for '{channel['name']}'")
        _rebuild_schedule(channel["id"], channel["name"])

# ─────────────────────────────────────────────────────────────
# Daily schedule  (deterministic per day, fresh random order)
# ─────────────────────────────────────────────────────────────
_schedule_cache: dict = {}   # {channel_id: {"date": str, "entries": [...]}}
_sched_lock = threading.Lock()

def _rebuild_schedule(channel_id: str, ch_name: str):
    today    = datetime.date.today()
    date_str = today.isoformat()
    seed     = today.toordinal() * 31337 + sum(ord(c) for c in channel_id)

    with _vcache_lock:
        cached = _video_cache.get(channel_id, {})
    entries = cached.get("entries", [])
    if not entries:
        log.warning(f"[schedule] No videos cached for '{ch_name}', skipping.")
        return

    rng      = random.Random(seed)
    shuffled = list(entries)
    rng.shuffle(shuffled)

    # Build from 00:00 UTC today, covering 25 hours (overlap into tomorrow)
    midnight   = datetime.datetime.combine(
        today, datetime.time.min, tzinfo=datetime.timezone.utc)
    day_end_ts = midnight.timestamp() + 25 * 3600

    sched_entries = []
    current_ts    = midnight.timestamp()
    idx           = 0
    while current_ts < day_end_ts:
        v        = shuffled[idx % len(shuffled)]
        dur      = v["duration"]
        end_ts   = current_ts + dur
        sched_entries.append({
            "url":        v["url"],
            "title":      v["title"],
            "duration":   dur,
            "thumbnail":  v.get("thumbnail", ""),
            "start_time": current_ts,
            "end_time":   end_ts,
        })
        current_ts = end_ts
        idx       += 1

    with _sched_lock:
        _schedule_cache[channel_id] = {"date": date_str, "entries": sched_entries}
    log.info(f"[schedule] {len(sched_entries)} entries for '{ch_name}' ({date_str})")

    # Pre-resolve stream URLs for current + next 3 videos
    threading.Thread(target=_prefetch_next_entries,
                     args=(channel_id,), daemon=True).start()

def _get_current_entry(channel_id: str):
    """Return (entry, offset_seconds) for what should be playing right now."""
    with _sched_lock:
        schedule = _schedule_cache.get(channel_id)

    if not schedule:
        return None, 0

    # Rebuild if the calendar date has rolled over
    if schedule.get("date") != datetime.date.today().isoformat():
        ch = next((c for c in get_channels() if c["id"] == channel_id), None)
        if ch:
            _rebuild_schedule(channel_id, ch["name"])
        with _sched_lock:
            schedule = _schedule_cache.get(channel_id)
        if not schedule:
            return None, 0

    now = time.time()
    for entry in schedule["entries"]:
        if entry["start_time"] <= now < entry["end_time"]:
            return entry, now - entry["start_time"]
    return None, 0

# ─────────────────────────────────────────────────────────────
# Stream URL cache  (pre-resolved video+audio CDN URLs)
# ─────────────────────────────────────────────────────────────
_surl_cache: dict = {}   # {yt_url: {"video_url","audio_url","fetched_at"}}
_surl_lock = threading.Lock()

def _resolve_stream_urls(youtube_url: str) -> tuple:
    result = _run_ytdlp([
        "--no-playlist", "-f",
        ("bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]"
         "/bestvideo[height<=720][vcodec^=avc1]+bestaudio[ext=m4a]/best"),
        "--get-url", youtube_url,
    ], timeout=60)
    if not result or not result.stdout.strip():
        return None, None
    urls = result.stdout.strip().splitlines()
    return urls[0], (urls[1] if len(urls) > 1 else None)

def _get_cached_stream_urls(youtube_url: str) -> tuple:
    now = time.time()
    with _surl_lock:
        cached = _surl_cache.get(youtube_url)
        if cached and now - cached["fetched_at"] < SURL_TTL:
            log.info(f"[surl] Cache hit: ...{youtube_url[-11:]}")
            return cached["video_url"], cached.get("audio_url")

    log.info(f"[surl] Resolving: ...{youtube_url[-11:]}")
    video_url, audio_url = _resolve_stream_urls(youtube_url)
    if video_url:
        with _surl_lock:
            _surl_cache[youtube_url] = {
                "video_url":  video_url,
                "audio_url":  audio_url,
                "fetched_at": now,
            }
    return video_url, audio_url

def _prefetch_next_entries(channel_id: str, count: int = 3):
    """Pre-resolve stream URLs for the next N scheduled videos."""
    with _sched_lock:
        schedule = _schedule_cache.get(channel_id)
    if not schedule:
        return
    now      = time.time()
    upcoming = [e for e in schedule["entries"] if e["end_time"] > now][:count + 1]
    for entry in upcoming:
        url = entry["url"]
        with _surl_lock:
            cached = _surl_cache.get(url)
            if cached and time.time() - cached["fetched_at"] < SURL_TTL:
                continue
        log.info(f"[surl] Prefetching '{entry['title']}'")
        _get_cached_stream_urls(url)

# ─────────────────────────────────────────────────────────────
# Channel preview (metadata for the Add-Channel UI)
# ─────────────────────────────────────────────────────────────
def fetch_channel_preview(url: str) -> dict:
    is_channel = any(x in url for x in
                     ["/@", "/channel/", "/user/", "/c/", "/videos", "playlist?list="])
    args = (["--flat-playlist", "--playlist-end", "1",
             "--dump-json", "--no-warnings", url]
            if is_channel else
            ["--no-playlist", "--dump-json", "--no-warnings", url])
    result = _run_ytdlp(args, timeout=90)
    if not result or not result.stdout.strip():
        return {}
    try:
        data = json.loads(result.stdout.strip().splitlines()[0])
        return {
            "title":     data.get("title", ""),
            "thumbnail": (data.get("thumbnail")
                          or _best_thumb(data.get("thumbnails")) or ""),
            "channel":   (data.get("uploader") or data.get("channel")
                          or data.get("playlist_uploader") or ""),
            "duration":  int(data.get("duration") or 0),
            "is_live":   bool(data.get("is_live")
                              or data.get("live_status") == "is_live"),
        }
    except Exception:
        return {}

# ─────────────────────────────────────────────────────────────
# XMLTV EPG  (built directly from the daily schedule)
# ─────────────────────────────────────────────────────────────
def build_xmltv(hours_ahead: int = 24) -> str:
    channels     = get_channels()
    now_ts       = time.time()
    window_start = now_ts - 3600
    window_end   = now_ts + hours_ahead * 3600
    fmt          = "%Y%m%d%H%M%S +0000"

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<tv generator-info-name="yt-iptv">']

    for ch in channels:
        lines.append(f'  <channel id="{ch["id"]}">')
        lines.append(f'    <display-name>{html.escape(ch["name"])}</display-name>')
        if ch.get("logo"):
            lines.append(f'    <icon src="{html.escape(ch["logo"])}" />')
        lines.append('  </channel>')

    for ch in channels:
        with _sched_lock:
            schedule = _schedule_cache.get(ch["id"])

        if not schedule or not schedule.get("entries"):
            _prog(lines, ch["id"],
                  datetime.datetime.utcfromtimestamp(window_start),
                  datetime.datetime.utcfromtimestamp(window_end),
                  ch["name"], "EPG loading…", "", fmt)
            continue

        for entry in schedule["entries"]:
            if entry["end_time"]   < window_start:
                continue
            if entry["start_time"] > window_end:
                break
            _prog(lines, ch["id"],
                  datetime.datetime.utcfromtimestamp(entry["start_time"]),
                  datetime.datetime.utcfromtimestamp(entry["end_time"]),
                  entry["title"], "", entry.get("thumbnail", ""), fmt)

    lines.append('</tv>')
    return "\n".join(lines)

def _prog(lines, ch_id, start, stop, title, desc, icon, fmt):
    lines.append(
        f'  <programme start="{start.strftime(fmt)}" '
        f'stop="{stop.strftime(fmt)}" channel="{ch_id}">')
    lines.append(f'    <title>{html.escape(title)}</title>')
    if desc: lines.append(f'    <desc>{html.escape(desc)}</desc>')
    if icon: lines.append(f'    <icon src="{html.escape(icon)}" />')
    lines.append('  </programme>')

# ─────────────────────────────────────────────────────────────
# Background refresh
# ─────────────────────────────────────────────────────────────
def refresh_all(channels=None):
    targets = channels or get_channels()
    for ch in targets:
        try:
            _refresh_video_cache(ch)   # fetches metadata + rebuilds schedule
        except Exception as e:
            log.error(f"[refresh] Error for '{ch['name']}': {e}")

def _background_refresh():
    log.info("[EPG] Background refresh started")
    time.sleep(2)
    while True:
        try:
            refresh_all()
        except Exception as e:
            log.error(f"[EPG] Refresh error: {e}")
        time.sleep(EPG_REFRESH_INTERVAL)

# ─────────────────────────────────────────────────────────────
# Flask — Jellyfin endpoints
# ─────────────────────────────────────────────────────────────
@app.route("/playlist.m3u")
def playlist():
    channels = get_channels()
    lines    = ["#EXTM3U"]
    for ch in channels:
        lines.append(
            f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-name="{ch["name"]}" '
            f'tvg-logo="{ch.get("logo","")}" '
            f'group-title="{ch.get("group","YouTube")}",{ch["name"]}')
        lines.append(f"{PROXY_BASE_URL}/stream/{ch['id']}")
    return Response("\n".join(lines), mimetype="application/x-mpegurl")

@app.route("/stream/<channel_id>")
def stream(channel_id):
    ch = next((c for c in get_channels() if c["id"] == channel_id), None)
    if not ch:
        abort(404)
    log.info(f"[stream] Request for '{ch['name']}'")

    def generate():
        while True:
            entry, offset = _get_current_entry(channel_id)
            if not entry:
                log.warning(f"[stream] No schedule for '{ch['name']}', retrying in 5s…")
                time.sleep(5)
                continue

            log.info(f"[stream] '{entry['title']}' offset={offset:.0f}s")
            video_url, audio_url = _get_cached_stream_urls(entry["url"])

            if not video_url:
                log.warning(f"[stream] Could not resolve URL for '{entry['title']}'")
                remaining = entry["end_time"] - time.time()
                time.sleep(min(max(remaining, 0) + 1, 10))
                continue

            seek = int(offset)
            args = ["ffmpeg", "-loglevel", "error", "-hide_banner"]

            # Apply input-side seek (fast keyframe seek) when offset > 2s
            if seek > 2:
                args += ["-ss", str(seek)]
            args += ["-i", video_url]

            if audio_url:
                if seek > 2:
                    args += ["-ss", str(seek)]
                args += ["-i", audio_url,
                         "-c:v", "copy", "-c:a", "aac",
                         "-map", "0:v:0", "-map", "1:a:0"]
            else:
                args += ["-c", "copy"]

            args += ["-f", "mpegts", "-"]

            proc = subprocess.Popen(
                args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break        # video ended → loop will pick next entry
                    yield chunk
            finally:
                proc.kill()

            # Pre-fetch next entry's stream URLs in the background
            threading.Thread(target=_prefetch_next_entries,
                             args=(channel_id,), daemon=True).start()

    return Response(generate(), mimetype="video/mp2t",
                    headers={"X-Accel-Buffering": "no",
                             "Cache-Control":      "no-cache"})

@app.route("/epg.xml")
def epg():
    return Response(build_xmltv(hours_ahead=24), mimetype="application/xml")

@app.route("/epg/refresh")
def epg_refresh():
    threading.Thread(target=refresh_all, daemon=True).start()
    return jsonify({"status": "refresh started"})

# ─────────────────────────────────────────────────────────────
# Flask — Management API
# ─────────────────────────────────────────────────────────────
@app.route("/")
def ui():
    return send_from_directory("ui", "index.html")

@app.route("/api/channels", methods=["GET"])
def api_list_channels():
    channels = get_channels()
    result   = []
    for ch in channels:
        entry, offset = _get_current_entry(ch["id"])
        with _sched_lock:
            sched = _schedule_cache.get(ch["id"])
        result.append({
            **ch,
            "epg": {
                "loaded":    bool(sched),
                "entries":   len(sched["entries"]) if sched else 0,
                "title":     entry["title"]           if entry else None,
                "thumbnail": entry.get("thumbnail")   if entry else None,
            },
        })
    return jsonify(result)

@app.route("/api/channels", methods=["POST"])
def api_add_channel():
    body = request.get_json(force=True)
    url  = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    log.info(f"[API] Previewing URL: {url}")
    preview     = fetch_channel_preview(url)
    is_playlist = any(x in url for x in
                      ["/@", "/channel/", "/user/", "/c/",
                       "/videos", "playlist?list="]) or body.get("playlist", False)
    new_ch = {
        "id":       "ch_" + uuid.uuid4().hex[:8],
        "name":     (body.get("name")
                     or preview.get("channel")
                     or preview.get("title") or url),
        "logo":     body.get("logo") or preview.get("thumbnail") or "",
        "url":      url,
        "group":    body.get("group") or "YouTube",
        "playlist": is_playlist,
    }
    channels = get_channels()
    channels.append(new_ch)
    save_channels(channels)
    threading.Thread(target=refresh_all, args=([new_ch],), daemon=True).start()
    log.info(f"[API] Added channel: {new_ch['name']}")
    return jsonify(new_ch), 201

@app.route("/api/channels/<channel_id>", methods=["DELETE"])
def api_delete_channel(channel_id):
    channels  = get_channels()
    remaining = [c for c in channels if c["id"] != channel_id]
    if len(remaining) == len(channels):
        return jsonify({"error": "channel not found"}), 404
    save_channels(remaining)
    with _sched_lock:
        _schedule_cache.pop(channel_id, None)
    with _vcache_lock:
        _video_cache.pop(channel_id, None)
    log.info(f"[API] Deleted channel: {channel_id}")
    return jsonify({"status": "deleted", "id": channel_id})

@app.route("/api/preview", methods=["POST"])
def api_preview():
    body = request.get_json(force=True)
    url  = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "url is required"}), 400
    preview = fetch_channel_preview(url)
    if not preview:
        return jsonify({"error": "Could not fetch metadata. Check the URL."}), 422
    preview["is_playlist"] = any(x in url for x in
                                 ["/@", "/channel/", "/user/", "/c/",
                                  "/videos", "playlist?list="])
    return jsonify(preview)

@app.route("/health")
def health():
    channels = get_channels()
    detail   = {}
    for ch in channels:
        with _sched_lock:
            sched = _schedule_cache.get(ch["id"])
        with _vcache_lock:
            vcache = _video_cache.get(ch["id"], {})
        entry, offset = _get_current_entry(ch["id"])
        detail[ch["id"]] = {
            "name":             ch["name"],
            "videos_cached":    len(vcache.get("entries", [])),
            "schedule_entries": len(sched["entries"]) if sched else 0,
            "schedule_date":    sched["date"]          if sched else None,
            "now_playing":      entry["title"]          if entry else None,
            "offset_seconds":   int(offset)             if entry else None,
        }
    return jsonify({"status": "ok", "channels": len(channels), "detail": detail})

# ─────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Starting yt-iptv proxy ...")
    log.info(f"Channels: {[c['name'] for c in get_channels()]}")
    threading.Thread(target=_background_refresh, daemon=True).start()
    app.run(host=SERVER_HOST, port=SERVER_PORT, debug=False)
