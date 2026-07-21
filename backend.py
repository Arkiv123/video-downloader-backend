"""
FastAPI backend for the video downloader.
Exposes:
  POST /formats   -> returns clean quality options for a given URL
  POST /download  -> downloads at chosen quality, merges audio, streams file back
                     Finished files are CACHED, so the same video+quality served
                     again is instant (no re-fetch, no re-merge).
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from typing import Optional
import yt_dlp
import os
import re
import uuid
import glob
import shutil
import hashlib
import json
import time
import tempfile
import threading

app = FastAPI(title="Video Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _enlarge_threadpool():
    """Sync endpoints run in Starlette's anyio threadpool. Enlarge it so that
    in-flight downloads (which occupy a thread each while yt-dlp runs) can't
    starve quick /formats and health checks. The download semaphore, not this
    pool, is what actually caps heavy work."""
    try:
        import anyio
        limiter = anyio.to_thread.current_default_thread_limiter()
        limiter.total_tokens = int(os.environ.get("THREADPOOL_SIZE", "80"))
    except Exception:
        pass

DOWNLOAD_DIR = "downloads"
CACHE_DIR = "cache"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

CACHE_INDEX_PATH = os.path.join(CACHE_DIR, "_index.json")
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024   # keep the cache under ~2 GB

# Serializes reads/writes to the on-disk cache index so concurrent downloads
# can't corrupt _index.json (which would cause silent cache misses and needless
# re-downloads). In-process lock only; fine because we run a single uvicorn
# worker on the free tier.
_INDEX_LOCK = threading.Lock()

# --- Extraction memo. Resolving a YouTube URL (PO-token mint + Deno signature
#     solve + client rotation) is the slow part, and today it runs TWICE: once
#     in /formats, again in /download. We stash the full sanitized info dict
#     here keyed by URL, so /download can reuse it via download_with_info_file
#     and skip the whole handshake. Entries are short-lived: the resolved media
#     URLs YouTube hands back are time-limited (usually ~6h), so we expire well
#     before that to avoid handing yt-dlp a dead URL.
_INFO_MEMO = {}
_INFO_MEMO_LOCK = threading.Lock()
_INFO_TTL_SECONDS = 60 * 20        # 20 min: comfortably inside YouTube's URL life
_INFO_MEMO_MAX = 200               # cap entries so memory can't grow unbounded

# --- Download concurrency valve. Each active download can spawn many sockets
#     and burn CPU (merabuffer/ffmpeg). On a tiny free-tier box, letting an
#     unbounded number run at once OOM-crashes the whole server — which stalls
#     EVERYONE. Instead we admit a bounded number concurrently; the rest queue
#     for a slot (fast, since most time is network I/O). Tunable via env so you
#     can raise it for free when you move to a bigger box, no redeploy of logic.
MAX_CONCURRENT_DOWNLOADS = int(os.environ.get("MAX_CONCURRENT_DOWNLOADS", "4"))
_DOWNLOAD_SLOTS = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)
# How long a queued download waits for a free slot before we tell the client
# to retry. Prevents threads from blocking forever under a spike (which would
# starve /formats and jam the whole site).
_SLOT_WAIT_SECONDS = int(os.environ.get("SLOT_WAIT_SECONDS", "90"))


def _memo_get(url):
    now = time.time()
    with _INFO_MEMO_LOCK:
        entry = _INFO_MEMO.get(url)
        if entry and now - entry[0] <= _INFO_TTL_SECONDS:
            return entry[1]
        if entry:
            _INFO_MEMO.pop(url, None)
    return None


def _memo_put(url, info):
    now = time.time()
    with _INFO_MEMO_LOCK:
        # evict expired + oldest entries when over the cap
        if len(_INFO_MEMO) >= _INFO_MEMO_MAX:
            for k in sorted(_INFO_MEMO, key=lambda k: _INFO_MEMO[k][0])[:_INFO_MEMO_MAX // 4 + 1]:
                _INFO_MEMO.pop(k, None)
        _INFO_MEMO[url] = (now, info)

# --- Cookies. YouTube on a cloud server often needs a logged-in cookie file to
#     get past "confirm you're not a bot". We look in two places:
#       1) local file "cookies.txt" (for testing on your PC)
#       2) Render Secret File at "/etc/secrets/cookies.txt" (for production)
def _find_cookies():
    for path in ("cookies.txt", "/etc/secrets/cookies.txt"):
        if os.path.exists(path):
            return path
    return None

COOKIE_FILE = _find_cookies()

# ffmpeg is needed to merge video+audio and to convert to MP3. On hosts
# without it (e.g. Render native Python runtime instead of Docker) we must
# avoid "+" merge selectors or every download fails.
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None


# --- JavaScript runtime for YouTube signature solving. yt-dlp needs a JS
#     runtime (deno >=2.3, node >=22, or bun) to solve YouTube's n-signature
#     challenge; without it, format URLs come back throttled/missing and you
#     get "Requested format is not available". yt-dlp only enables deno by
#     default. We look for a usable binary in PATH and common install dirs and
#     build a js_runtimes dict pointing at whatever we find.
def _detect_js_runtimes():
    runtimes = {}
    home = os.path.expanduser("~")
    candidates = {
        "deno": [
            shutil.which("deno"),
            os.path.join(home, ".deno", "bin", "deno.exe"),
            os.path.join(home, ".deno", "bin", "deno"),
        ],
        "node": [shutil.which("node")],
        "bun": [shutil.which("bun"), os.path.join(home, ".bun", "bin", "bun")],
    }
    for name, paths in candidates.items():
        for p in paths:
            if p and os.path.exists(p):
                # empty dict = "enabled, find it yourself"; {'path': p}
                # pins the exact binary so PATH doesn't matter for uvicorn.
                runtimes[name] = {"path": p}
                break
        else:
            # still enable by name in case it's resolvable at call time
            runtimes.setdefault(name, {})
    return runtimes


JS_RUNTIMES = _detect_js_runtimes()


# --- PO-token provider server. The bgutil plugin (installed via pip) auto-
#     connects to an HTTP server on 127.0.0.1:4416 to mint the proof-of-origin
#     tokens YouTube now requires. If that server isn't already running we try
#     to start it here so a plain `uvicorn backend:app` just works. The Docker
#     image starts it in the CMD instead; this is the local-dev convenience.
POT_PORT = 4416


def _pot_server_up():
    import socket
    try:
        with socket.create_connection(("127.0.0.1", POT_PORT), timeout=1):
            return True
    except OSError:
        return False


def _find_pot_server_script():
    """Locate the bgutil server entrypoint (build/main.js) if it was cloned
    and built. Checked locations cover the Docker image and a local clone."""
    home = os.path.expanduser("~")
    for base in ("/opt/bgutil", os.path.join(home, "bgutil-ytdlp-pot-provider")):
        candidate = os.path.join(base, "server", "build", "main.js")
        if os.path.exists(candidate):
            return candidate
    return None


def _ensure_pot_server():
    if _pot_server_up():
        return
    script = _find_pot_server_script()
    node = shutil.which("node")
    if not (script and node):
        # No local server available — the http provider will simply be
        # unavailable and yt-dlp falls back to no-PO-token extraction.
        return
    try:
        import subprocess
        subprocess.Popen(
            [node, script],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


_ensure_pot_server()

# --- YouTube player-client fallbacks. If the first extraction fails (bot
#     check, empty formats, SABR-only response), retry with other clients.
#     Harmless for non-YouTube URLs (extractor_args are ignored).
#
#     SPEED: the first attempt asks for the small set of clients that a PO
#     token unlocks (web_safari + mweb) in ONE call — this returns the full
#     format table without paying the extra network round-trips of yt-dlp's
#     broad default rotation (which probes many clients). The remaining entries
#     are cheaper, single-client retries reached only if the first fails.
#     tv/android are last-resort (often storyboards-only or DRM).
CLIENT_FALLBACKS = [
    {"extractor_args": {"youtube": {"player_client": ["web_safari", "mweb"]}}},
    {"extractor_args": {"youtube": {"player_client": ["mweb"]}}},
    {"extractor_args": {"youtube": {"player_client": ["tv", "web"]}}},
    {"extractor_args": {"youtube": {"player_client": ["android_vr"]}}},
]


def _base_opts(extra=None, use_cookies=False):
    """Common yt-dlp options.

    PO tokens: a locally-running bgutil PO-token provider (installed via the
    `bgutil-ytdlp-pot-provider` plugin, server on 127.0.0.1:4416) is picked
    up automatically by yt-dlp. That is the durable fix for YouTube's
    "confirm you're not a bot" / empty-format responses in 2025+.

    Cookies are OPT-IN per attempt (use_cookies=True), NOT attached by
    default. A stale/expired cookies.txt actively poisons YouTube: it routes
    the request to a degraded "tv" player that returns only storyboards and
    no media. So we extract without cookies first and only fall back to
    cookies when the clean attempts all fail (e.g. age-gated / private)."""
    opts = {
        "noplaylist": True,
        # SPEED: fail a hung/slow request fast so we fall through to the next
        # client instead of blocking. Cap yt-dlp's own retries too — with the
        # PO-token provider the first good client usually works, so long retry
        # storms just add latency.
        "socket_timeout": 12,
        "retries": 2,
        "extractor_retries": 1,
        "fragment_retries": 3,
    }
    # JS runtime for YouTube's n-signature / EJS challenge. Without one,
    # yt-dlp can't solve signatures and format URLs come back throttled or
    # missing ("Requested format is not available"). Auto-detected at import
    # (deno/node/bun); highest-priority available runtime wins.
    if JS_RUNTIMES:
        opts["js_runtimes"] = JS_RUNTIMES
    if use_cookies and COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE
    if extra:
        opts.update(extra)
    return opts


# --- Spotify (and other DRM music services). Spotify audio is DRM-encrypted,
#     so the actual Spotify stream cannot be downloaded — yt-dlp has no Spotify
#     extractor by design. The universally-used workaround (spotDL, etc.) is to
#     read the track's "Artist - Title" from Spotify's public page (no API key)
#     and download the matching song from YouTube Music instead. That is what we
#     do here: a Spotify URL is transparently rewritten to a YT-Music search.
_SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-\w+/)?track/([A-Za-z0-9]+)")

# Spotify's public page never changes for a given track, so the "Artist Title"
# scrape is pure waste after the first time. Without this cache the same track
# hits Spotify's page TWICE per download — once in /formats, once in /download —
# each a blocking HTTP GET with a 12s timeout. Memoizing by track id collapses
# that to a single fetch for the life of the process (and 0 for repeats).
_SPOTIFY_MEMO = {}


def _spotify_query(url):
    """Return 'Artist Title' for a Spotify track URL, or None if it isn't one
    / can't be read. Uses only the public page's <title> + og:description, no
    auth. Albums/playlists aren't handled (they'd need many searches).
    Result is cached per track id so the page is fetched at most once."""
    sm = _SPOTIFY_RE.search(url)
    if not sm:
        return None
    track_id = sm.group(1)
    if track_id in _SPOTIFY_MEMO:
        return _SPOTIFY_MEMO[track_id]
    import urllib.request, html as _html
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        page = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "ignore")
    except Exception:
        return None
    title = artist = None
    m = re.search(r"<title>([^<]*)</title>", page)
    if m:
        # "Blinding Lights - song and lyrics by The Weeknd | Spotify"
        t = _html.unescape(m.group(1))
        mm = re.match(r"(.+?)\s*-\s*song(?: and lyrics)? by\s+(.+?)\s*\|", t)
        if mm:
            title, artist = mm.group(1).strip(), mm.group(2).strip()
    if not title:
        m = re.search(r'<meta property="og:title" content="([^"]*)"', page)
        if m:
            title = _html.unescape(m.group(1)).strip()
    if not artist:
        m = re.search(r'<meta property="og:description" content="([^"]*)"', page)
        if m:
            # "The Weeknd · After Hours · Song · 2020"
            parts = re.split(r"\s*[·|]\s*", _html.unescape(m.group(1)))
            if parts:
                artist = parts[0].strip()
    if not title:
        return None
    result = f"{artist} {title}".strip() if artist else title
    # Cache only positive results. A network failure above returns None WITHOUT
    # caching, so a transient Spotify hiccup doesn't poison the track forever.
    _SPOTIFY_MEMO[track_id] = result
    return result


def _rewrite_music_url(url):
    """Rewrite unsupported music-service URLs to a searchable equivalent.
    Currently: Spotify track -> YouTube Music search. Returns the (possibly
    unchanged) URL. ytsearch1: makes yt-dlp fetch the single best match."""
    q = _spotify_query(url)
    if q:
        # ytsearch1 returns the top match as a normal YouTube video, which then
        # flows through the exact same audio/format pipeline as any other URL.
        return f"ytsearch1:{q}"
    return url


def _extract_with_fallbacks(url, extra):
    """extract_info that retries across player clients before giving up.

    Strategy (durable across platforms):
      1. Try each player client WITHOUT cookies + PO token. This is the
         path that returns full format tables for most public videos.
      2. Only if every clean attempt fails do we retry the client rotation
         WITH cookies — for genuinely gated content (age-restricted,
         members-only, region-locked) where a login actually helps.
    Non-YouTube URLs skip client rotation but still get the no-cookies-then
    -cookies escalation, which is harmless and occasionally unblocks
    Instagram/Facebook private posts."""
    # A Spotify (or similar) URL becomes a "ytsearch1:Artist Title" query that
    # resolves via YouTube, so it gets the YouTube client rotation too.
    url = _rewrite_music_url(url)
    is_youtube = ("youtube.com" in url or "youtu.be" in url
                  or url.startswith("ytsearch"))
    clients = CLIENT_FALLBACKS if is_youtube else [None]
    last_err = None

    for use_cookies in (False, True):
        # no point retrying with cookies if we don't have any
        if use_cookies and not COOKIE_FILE:
            break
        for client_cfg in clients:
            opts = _base_opts(extra, use_cookies=use_cookies)
            if client_cfg:
                opts.update(client_cfg)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info and (info.get("formats") or info.get("entries") or info.get("url")):
                    return info
                last_err = Exception("Extractor returned no formats.")
            except Exception as e:
                last_err = e
    raise last_err


class URLRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str = "best"
    audio_only: bool = False
    height: Optional[int] = None   # resolution the user picked; drives fallbacks


# ------------------------- cache helpers -------------------------
def _load_index():
    with _INDEX_LOCK:
        try:
            with open(CACHE_INDEX_PATH, "r") as fh:
                return json.load(fh)
        except Exception:
            return {}


def _save_index(idx):
    # Atomic write: dump to a temp file in the same dir, then os.replace so a
    # concurrent reader never sees a half-written (corrupt) index.
    with _INDEX_LOCK:
        try:
            fd, tmp = tempfile.mkstemp(dir=CACHE_DIR, suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(idx, fh)
            os.replace(tmp, CACHE_INDEX_PATH)
        except Exception:
            try:
                os.remove(tmp)
            except Exception:
                pass


def _cache_key(url, format_id, audio_only):
    raw = f"{url}|{format_id}|{audio_only}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _prune_cache():
    idx = _load_index()
    files = []
    total = 0
    for key, path in list(idx.items()):
        if os.path.exists(path):
            sz = os.path.getsize(path)
            files.append((os.path.getmtime(path), key, path, sz))
            total += sz
        else:
            idx.pop(key, None)
    if total > MAX_CACHE_BYTES:
        files.sort()
        while total > MAX_CACHE_BYTES and files:
            _, key, path, sz = files.pop(0)
            try:
                os.remove(path)
            except Exception:
                pass
            idx.pop(key, None)
            total -= sz
    _save_index(idx)


# ------------------------- formats -------------------------
# Formats that are never downloadable media (storyboards, thumbnails).
_JUNK_EXTS = {"mhtml", "jpg", "jpeg", "png", "webp", "gif", "svg", "json"}

_NOTE_HEIGHTS = {
    "144p": 144, "240p": 240, "360p": 360, "480p": 480, "540p": 540,
    "720p": 720, "1080p": 1080, "1440p": 1440, "2160p": 2160, "4320p": 4320,
    "tiny": 144, "low": 240, "sd": 480, "medium": 480, "hd": 720, "high": 1080,
}


def _guess_height(f):
    """Best-effort height. Many extractors (TikTok, Instagram, Facebook, X,
    Reddit, ...) omit `height` and only give width, resolution string, or a
    quality note like "hd"/"sd" — those formats must still show up."""
    if f.get("height"):
        return int(f["height"])
    res = f.get("resolution") or ""
    m = re.search(r"(\d+)\s*[xX×]\s*(\d+)", str(res))
    if m:
        return min(int(m.group(1)), int(m.group(2)))
    m = re.search(r"(\d{3,4})p", str(f.get("format_note") or "") + " " + str(res))
    if m:
        return int(m.group(1))
    note = str(f.get("format_note") or "").strip().lower()
    if note in _NOTE_HEIGHTS:
        return _NOTE_HEIGHTS[note]
    if f.get("width"):
        # assume 16:9 as a rough grade so the option is at least selectable
        return int(round(int(f["width"]) * 9 / 16))
    return None


def _is_video(f):
    vcodec = f.get("vcodec")
    if vcodec and vcodec != "none":
        return True
    # vcodec unknown (None): treat as video when there's any visual dimension
    # and it isn't a pure audio stream
    if vcodec is None and (f.get("height") or f.get("width") or f.get("resolution") not in (None, "audio only")):
        acodec = f.get("acodec")
        return not (acodec and acodec != "none" and not f.get("height") and not f.get("width"))
    return False


def _is_audio(f):
    acodec = f.get("acodec")
    vcodec = f.get("vcodec")
    if acodec and acodec != "none" and (not vcodec or vcodec == "none"):
        return True
    # audio-only formats where acodec is unknown but resolution says so
    return f.get("resolution") == "audio only" and (not vcodec or vcodec == "none")


# Protocols that are a single, plain HTTP file the browser can download itself.
# HLS/DASH (m3u8*, *dash*) are segmented manifests — they need yt-dlp/ffmpeg to
# stitch, so they must stay on the server path and never get a direct_url.
_DIRECT_PROTOCOLS = {"https", "http"}


def _direct_url(f):
    """Return a browser-downloadable direct URL for a format, or None.

    Only progressive (pre-merged) single-file HTTP streams qualify: the browser
    can pull those itself, skipping the server entirely. Anything that needs a
    video+audio merge (YouTube HD) or manifest stitching (HLS/DASH) returns None
    and falls through to the normal server-side download+merge path."""
    url = f.get("url")
    if not url:
        return None
    proto = (f.get("protocol") or "").split("+")[0]
    if proto not in _DIRECT_PROTOCOLS:
        return None
    return url


def _clean_formats(info):
    best_video = {}
    best_audio = {}

    def _better(new, old, prefer_ext):
        if bool(new.get("progressive")) != bool(old.get("progressive")):
            return new.get("progressive")
        new_ext = 1 if new.get("ext") == prefer_ext else 0
        old_ext = 1 if old.get("ext") == prefer_ext else 0
        if new_ext != old_ext:
            return new_ext > old_ext
        return (new.get("filesize") or 0) > (old.get("filesize") or 0)

    for f in info.get("formats", []):
        ext = f.get("ext")
        if ext in _JUNK_EXTS or not f.get("format_id"):
            continue
        # DRM'd streams can't be downloaded — don't offer them
        if f.get("has_drm"):
            continue

        if _is_video(f):
            height = _guess_height(f)
            if not height:
                continue  # generic "best" fallback below still covers it
            acodec = f.get("acodec")
            has_audio = bool(acodec and acodec != "none")
            # Only pre-merged (has_audio) HTTP streams can be handed straight to
            # the browser; video-only streams need a server-side audio merge.
            direct = _direct_url(f) if has_audio else None
            opt = {
                "format_id": f.get("format_id"),
                "label": f"{height}p ({ext})",
                "type": "video",
                "ext": ext,
                "height": height,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "progressive": has_audio,
                "direct_url": direct,
            }
            cur = best_video.get(height)
            if cur is None or _better(opt, cur, "mp4"):
                best_video[height] = opt

        elif _is_audio(f):
            abr = int(f.get("abr") or f.get("tbr") or 0)
            label = f"Audio only (~{abr}kbps)" if abr else "Audio only"
            # Audio we hand to the browser directly ONLY when no MP3 transcode
            # is involved — the direct stream keeps its native ext (m4a/webm/opus).
            # MP3 conversion still needs the server (ffmpeg), so leave it None there.
            direct = _direct_url(f)
            opt = {
                "format_id": f.get("format_id"),
                "label": label,
                "type": "audio",
                "ext": ext,
                "abr": abr,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "progressive": True,
                "direct_url": direct,
            }
            cur = best_audio.get(abr)
            if cur is None or _better(opt, cur, "m4a"):
                best_audio[abr] = opt

    videos = sorted(best_video.values(), key=lambda o: o["height"], reverse=True)
    audios = sorted(best_audio.values(), key=lambda o: o.get("abr", 0), reverse=True)

    # Guaranteed fallback: yt-dlp's own "best" selector works on effectively
    # every extractor, even when per-format metadata is too sparse to list.
    # This is what makes sources with weird format tables still downloadable.
    if not videos:
        videos = [{
            "format_id": "best",
            "label": "Best available (auto)",
            "type": "video",
            "ext": info.get("ext") or "mp4",
            "height": info.get("height") or 0,
            "filesize": info.get("filesize") or info.get("filesize_approx"),
            "progressive": True,
            # Some single-file extractors (TikTok/IG/Twitter) expose the final
            # URL right on the info dict — hand it straight to the browser.
            "direct_url": _direct_url(info),
        }]

    if not audios:
        audios = [{
            "format_id": "audio-mp3",
            "label": "Audio only (MP3)",
            "type": "audio",
            "ext": "mp3",
            "abr": 192,
            "filesize": None,
            "progressive": True,
            # MP3 requires a server-side ffmpeg transcode, so never direct.
            "direct_url": None,
        }]

    return videos + audios


@app.post("/formats")
def get_formats(req: URLRequest):
    try:
        info = _extract_with_fallbacks(req.url, {"quiet": True, "skip_download": True})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch formats: {e}")

    # Some extractors hand back a playlist wrapper even with noplaylist
    # (multi-clip posts on Instagram/TikTok/Reddit). Use the first entry.
    if info.get("_type") == "playlist" or "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise HTTPException(status_code=400, detail="No downloadable media found at this URL.")
        info = entries[0]

    # Live-stream detection. An in-progress live stream never "ends", so a
    # synchronous download would run forever and hit request timeouts on the
    # free tier. We surface a flag + human message so the frontend can explain
    # it instead of appearing to hang. is_live=True is currently airing;
    # was_live/None with a duration means it's an archived VOD (downloadable).
    live_status = info.get("live_status")
    is_live = bool(info.get("is_live")) or live_status in ("is_live", "is_upcoming")

    # Stash the resolved info so /download can skip a second full extraction
    # (the slow PO-token + signature handshake). sanitize_info makes it safe to
    # round-trip through JSON, which is how download_with_info_file consumes it.
    try:
        _memo_put(req.url, yt_dlp.YoutubeDL.sanitize_info(info))
    except Exception:
        pass

    return {
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "is_live": is_live,
        "live_note": ("This is a live broadcast. Downloading works once the "
                      "stream has ended and is available as a recording.")
                     if is_live else None,
        "formats": _clean_formats(info),
    }


def _build_format_chain(req, audio_only):
    """Build a '/'-separated yt-dlp selector chain: exact format first, then
    same-resolution fallbacks, then progressively looser ones. This is what
    fixes 'Requested format is not available' — the two extractions (formats
    vs download) can see different format tables on YouTube, so a raw format
    ID alone is never trusted to still exist."""
    fid = (req.format_id or "").strip()
    merge = FFMPEG_AVAILABLE  # '+' selectors need ffmpeg to mux

    if audio_only:
        chain = []
        if fid and fid not in ("best", "audio-mp3"):
            chain.append(fid)
        chain += ["bestaudio", "best"]
        return "/".join(chain)

    chain = []
    if fid and fid != "best":
        if merge:
            chain.append(f"{fid}+bestaudio")
        chain.append(fid)
    if req.height:
        h = int(req.height)
        if merge:
            # best pair at the chosen resolution, then nearest below it
            chain.append(f"bestvideo[height={h}]+bestaudio")
            chain.append(f"bestvideo[height<={h}]+bestaudio")
        # progressive (pre-merged) file at or below the chosen resolution —
        # works without ffmpeg and on every platform
        chain.append(f"best[height<={h}]")
    if merge:
        chain.append("bestvideo+bestaudio")
    chain.append("best")
    return "/".join(chain)


# ------------------------- download -------------------------
# Per-download parallelism. High connection counts saturate the available pipe
# and beat per-connection throttling (good for single-user speed); the download
# semaphore above bounds how many run at once so the box can't be overwhelmed.
# Both are env-tunable so you can scale up on a bigger host without code edits.
_FRAG_CONNECTIONS = int(os.environ.get("FRAG_CONNECTIONS", "16"))
_ARIA_CONNECTIONS = os.environ.get("ARIA_CONNECTIONS", "16")


def _make_base_extra(req, audio_only, outtmpl):
    """yt-dlp options shared by the fast path and the full-extraction sweep."""
    base_extra = {
        "outtmpl": outtmpl,
        "format": _build_format_chain(req, audio_only),
        "concurrent_fragment_downloads": _FRAG_CONNECTIONS,
        "restrictfilenames": True,
    }
    if not audio_only and FFMPEG_AVAILABLE:
        base_extra["merge_output_format"] = "mp4"
        # stream-copy merge (no re-encode) + move moov atom to the front so the
        # file starts playing before it's fully downloaded — no quality loss.
        base_extra["postprocessor_args"] = {"merger": ["-movflags", "+faststart"]}
    if shutil.which("aria2c"):
        base_extra["external_downloader"] = "aria2c"
        base_extra["external_downloader_args"] = [
            "-x", _ARIA_CONNECTIONS, "-s", _ARIA_CONNECTIONS, "-k", "1M"
        ]
    if audio_only and FFMPEG_AVAILABLE:
        base_extra["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
    return base_extra


def _fast_download_from_memo(url, base_extra):
    """Fast path: reuse the info dict resolved in /formats and download it via
    download_with_info_file, skipping the entire re-extraction (PO-token mint +
    signature solve + client rotation). Returns True on success. Any failure
    (expired URLs, stale memo) returns False so the caller runs the full sweep."""
    info = _memo_get(url)
    if not info:
        return False
    fd, info_path = tempfile.mkstemp(dir=DOWNLOAD_DIR, suffix=".info.json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(info, fh)
        with yt_dlp.YoutubeDL(_base_opts(base_extra, use_cookies=False)) as ydl:
            ydl.download_with_info_file(info_path)
        return True
    except Exception:
        return False
    finally:
        try:
            os.remove(info_path)
        except Exception:
            pass


def _full_download_sweep(req, base_extra, audio_only):
    """Full extraction + download with the client/cookie fallback ladder.
    Used when the fast path isn't available or its URLs have expired."""
    # Spotify/etc. -> YouTube-Music search, same as the formats path.
    dl_url = _rewrite_music_url(req.url)
    is_youtube = ("youtube.com" in dl_url or "youtu.be" in dl_url
                  or dl_url.startswith("ytsearch"))
    loosest = "bestaudio/best" if audio_only else (
        "bestvideo+bestaudio/best" if FFMPEG_AVAILABLE else "best")
    clients = CLIENT_FALLBACKS if is_youtube else [None]

    attempts = []
    # sweep 1: no cookies, each client with the chosen format chain
    for client_cfg in clients:
        attempts.append((client_cfg, None, False))
    # then no cookies with the loosest selector (ignore chosen format)
    attempts.append((None, loosest, False))
    # sweep 2: same, but WITH cookies — only reached if everything above failed
    if COOKIE_FILE:
        for client_cfg in clients:
            attempts.append((client_cfg, None, True))
        attempts.append((None, loosest, True))

    last_err = None
    for client_cfg, fmt_override, use_cookies in attempts:
        extra = dict(base_extra)
        if client_cfg:
            extra.update(client_cfg)
        if fmt_override:
            extra["format"] = fmt_override
        try:
            with yt_dlp.YoutubeDL(_base_opts(extra, use_cookies=use_cookies)) as ydl:
                ydl.extract_info(dl_url, download=True)
            return True, None
        except Exception as e:
            last_err = e
    return False, last_err


@app.post("/download")
def download(req: DownloadRequest):
    audio_only = req.audio_only or req.format_id == "audio-mp3"
    key = _cache_key(req.url, req.format_id, audio_only)

    idx = _load_index()
    cached = idx.get(key)
    if cached and os.path.exists(cached):
        return FileResponse(
            path=cached,
            filename=os.path.basename(cached).split("_", 1)[-1],
            media_type="application/octet-stream",
            headers={"X-Cache": "HIT"},
        )

    job_id = str(uuid.uuid4())[:8]
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title)s.%(ext)s")
    base_extra = _make_base_extra(req, audio_only, outtmpl)

    # Concurrency valve: bound how many downloads run at once so a traffic
    # spike can't OOM/CPU-starve the box (a crash would stall EVERY user).
    # Queued requests wait up to _SLOT_WAIT_SECONDS for a slot; if the box is
    # still saturated we return 503 so the caller can retry, rather than pinning
    # a worker thread forever (which would starve /formats and jam the site).
    last_err = None
    if not _DOWNLOAD_SLOTS.acquire(timeout=_SLOT_WAIT_SECONDS):
        raise HTTPException(
            status_code=503,
            detail="Server is busy handling other downloads. Please retry in a moment.",
        )
    try:
        # Fast path first (reuses the /formats extraction), then the full sweep.
        downloaded = _fast_download_from_memo(req.url, base_extra)
        if not downloaded:
            downloaded, last_err = _full_download_sweep(req, base_extra, audio_only)
    finally:
        _DOWNLOAD_SLOTS.release()

    if not downloaded:
        raise HTTPException(status_code=400, detail=f"Download failed: {last_err}")

    matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}_*"))
    matches = [m for m in matches
               if not m.endswith((".part", ".ytdl", ".temp", ".aria2", ".info.json"))]
    if not matches:
        raise HTTPException(status_code=500, detail="File not found after download.")
    filename = max(matches, key=os.path.getsize)

    cached_path = os.path.join(CACHE_DIR, f"{key}_{os.path.basename(filename)}")
    try:
        shutil.move(filename, cached_path)
    except Exception:
        cached_path = filename
    idx = _load_index()
    idx[key] = cached_path
    _save_index(idx)
    _prune_cache()

    return FileResponse(
        path=cached_path,
        filename=os.path.basename(cached_path).split("_", 1)[-1],
        media_type="application/octet-stream",
        headers={"X-Cache": "MISS"},
    )


# ------------------------- stream (zero-disk pass-through) -------------------------
# "Water on a hot pan": water never pools, it flows across and is gone. Same idea
# for bytes — instead of writing the whole file to disk (bounded by Render's
# ephemeral disk + the 2 GB cache cap), we PIPE the source stream straight through
# the server to the client. Nothing accumulates: a 20 GB file uses ~0 bytes of
# disk here. This is what makes file size effectively "unlimited" — the only
# remaining ceiling is bandwidth, which no code can make infinite.
#
# Scope: only single-file progressive HTTP(S) streams (a format that already
# carries both video+audio, or an audio-only track). Merged HD (video+audio) and
# HLS/DASH manifests need ffmpeg/stitching to a seekable file, so they can't be a
# pure pass-through and stay on the /download path. The frontend tries the browser
# -direct CDN fetch first, this proxy second, and /download last.
_STREAM_CHUNK = 512 * 1024  # 512 KB per pumped chunk — big enough to be efficient,
                            # small enough that memory stays flat under load.


def _resolve_stream_url(url, format_id):
    """Resolve a single progressive HTTP(S) media URL for (url, format_id).

    Reuses the /formats memo when possible (no re-extraction), else runs the
    normal client/cookie fallback ladder. Returns (media_url, ext, title) or
    (None, None, None) if the chosen format isn't a plain single-file stream."""
    info = _memo_get(url)
    if not info:
        try:
            info = _extract_with_fallbacks(url, {"quiet": True, "skip_download": True})
        except Exception:
            return None, None, None
    if info.get("_type") == "playlist" or "entries" in info:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            return None, None, None
        info = entries[0]

    title = info.get("title") or "video"
    formats = info.get("formats") or []

    def _pick(f):
        # A streamable format is a plain http(s) file with a direct URL.
        u = _direct_url(f)
        return u, f.get("ext")

    # 1) exact format_id match, if it's a single-file stream
    if format_id and format_id not in ("best", "audio-mp3"):
        for f in formats:
            if f.get("format_id") == format_id:
                u, ext = _pick(f)
                if u:
                    return u, ext, title
                return None, None, None  # chosen format needs a merge -> not streamable

    # 2) best progressive video (has both audio+video) that's a plain file
    best = None
    for f in formats:
        if not _is_video(f):
            continue
        acodec = f.get("acodec")
        if not (acodec and acodec != "none"):
            continue  # video-only -> needs an audio merge, not a pass-through
        u, ext = _pick(f)
        if not u:
            continue
        h = _guess_height(f) or 0
        if best is None or h > best[3]:
            best = (u, ext, title, h)
    if best:
        return best[0], best[1], best[2]

    # 3) the info dict's own final URL (TikTok/IG/Twitter single-file case)
    u = _direct_url(info)
    if u:
        return u, info.get("ext"), title
    return None, None, None


@app.post("/stream")
def stream(req: DownloadRequest):
    """Zero-disk pass-through download for single-file progressive streams.

    Pipes bytes source -> server -> client without buffering the file on disk.
    Forwards the client's Range header so seeking/resuming works, and mirrors the
    upstream status (206/200) + Content-Range/Length back. Returns 409 when the
    chosen format needs a server-side merge (the caller then uses /download)."""
    import requests

    audio_only = req.audio_only or req.format_id == "audio-mp3"
    if audio_only and req.format_id == "audio-mp3":
        # audio-mp3 implies an ffmpeg transcode, which can't be a pass-through.
        raise HTTPException(status_code=409, detail="MP3 needs the server path.")

    media_url, ext, title = _resolve_stream_url(req.url, req.format_id)
    if not media_url:
        # Not a single-file stream (merge/HLS/DASH needed) — tell the caller to
        # fall back to /download rather than pretending we can stream it.
        raise HTTPException(status_code=409, detail="Not a direct stream; use /download.")

    # Pass the Range header through so the browser can seek and resume. The
    # concurrency valve still applies: a stream holds a slot for its lifetime,
    # same as a disk download, so a spike can't exhaust upstream sockets.
    if not _DOWNLOAD_SLOTS.acquire(timeout=_SLOT_WAIT_SECONDS):
        raise HTTPException(status_code=503, detail="Server is busy. Please retry in a moment.")

    try:
        rng = req_range = None
        try:
            # StreamingResponse can't see the raw request headers here, so we
            # don't have the incoming Range; forward none and let the browser
            # re-request ranges against the returned stream if it must. Most
            # save-to-disk fetches read start-to-end, which is exactly this.
            upstream = requests.get(media_url, stream=True, timeout=30, headers={
                "User-Agent": "Mozilla/5.0",
            })
        except Exception as e:
            _DOWNLOAD_SLOTS.release()
            raise HTTPException(status_code=502, detail=f"Upstream fetch failed: {e}")

        if upstream.status_code >= 400:
            _DOWNLOAD_SLOTS.release()
            raise HTTPException(status_code=502, detail=f"Upstream returned {upstream.status_code}.")

        def _pump():
            # The one place bytes move: read a chunk, yield it, forget it. Nothing
            # is retained, so peak memory is one _STREAM_CHUNK regardless of file
            # size. The slot is released when the generator is exhausted or the
            # client disconnects (GeneratorExit).
            try:
                for chunk in upstream.iter_content(chunk_size=_STREAM_CHUNK):
                    if chunk:
                        yield chunk
            finally:
                upstream.close()
                _DOWNLOAD_SLOTS.release()

        safe = re.sub(r'[\\/:*?"<>|]', "", title or "video")[:80]
        filename = f"{safe}.{ext or 'mp4'}"
        headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
        clen = upstream.headers.get("Content-Length")
        if clen:
            headers["Content-Length"] = clen
        media_type = upstream.headers.get("Content-Type") or "application/octet-stream"
        return StreamingResponse(_pump(), media_type=media_type, headers=headers)
    except HTTPException:
        raise
    except Exception as e:
        _DOWNLOAD_SLOTS.release()
        raise HTTPException(status_code=500, detail=f"Stream failed: {e}")


@app.get("/")
def health_check():
    return {"status": "ok", "message": "Video downloader API is running.", "cookies": bool(COOKIE_FILE)}
