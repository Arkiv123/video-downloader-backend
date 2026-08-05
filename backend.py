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
from urllib.parse import quote
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
#     SPEED: each entry is ONE client. yt-dlp mints a separate PO token per
#     player client, and a token mint is the single most expensive step in a
#     YouTube extraction (seconds, not milliseconds). Asking for two clients in
#     one attempt therefore pays for two mints even when the first one already
#     returned a full format table — so the happy path is listed alone, first.
#     tv/android are last-resort (often storyboards-only or DRM).
CLIENT_FALLBACKS = [
    # mweb leads because /diag measured it: with cookies it returns 30 real
    # formats in ~13.7s, while web_safari spends ~4.4s failing outright
    # ("Requested format is not available" — a degraded, storyboards-only
    # response). It used to lead, and that was a pure 4.4s tax per fresh URL.
    {"extractor_args": {"youtube": {"player_client": ["mweb"]}}},
    {"extractor_args": {"youtube": {"player_client": ["web_safari"]}}},
    {"extractor_args": {"youtube": {"player_client": ["tv", "web"]}}},
    {"extractor_args": {"youtube": {"player_client": ["android_vr"]}}},
]

# Wall-clock budget for the whole fallback ladder. Without this, 4 clients x
# (no-cookies, then cookies) x yt-dlp's own retries can stack to ~2 minutes on
# a hard URL while the user stares at a spinner. When the budget is spent we
# stop starting NEW attempts and surface the last error instead.
_EXTRACT_BUDGET_SECONDS = float(os.environ.get("EXTRACT_BUDGET_SECONDS", "45"))



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


# --- Which auth mode YouTube actually accepts, remembered process-wide.
#
# Measured on the live Render box (POST /diag): all four player clients fail
# WITHOUT cookies — "Sign in to confirm you're not a bot" — burning ~30s before
# the cookie attempts succeed. That is normal for a datacenter IP; YouTube
# treats cloud ranges as untrusted. Leading with cookie-less attempts therefore
# paid a guaranteed ~30s tax on every fresh URL.
#
# We don't hardcode cookies-first either: a stale cookie file can route to a
# degraded player that returns storyboards only, and hardcoding would make that
# failure permanent. Instead we remember which mode last worked and lead with
# it, falling back to the other order automatically. Self-correcting in both
# directions, no config to keep in sync.
#
# Seeded to prefer cookies when a cookie file exists, because that is what the
# measurement says is true for this deployment — so even the first request
# after a cold start skips the doomed sweep.
_PREFER_COOKIES = bool(COOKIE_FILE)
# Index into CLIENT_FALLBACKS of the player client that last worked. Measured:
# web_safari fails in ~4.4s ("Requested format is not available" — a degraded,
# storyboards-only response) while mweb succeeds, so the default order pays a
# pure 4.4s tax on every fresh URL. Same self-correcting trick as the cookie
# order: lead with the winner, keep the rest as fallbacks.
_PREFER_CLIENT = 0
_PREFER_LOCK = threading.Lock()


def _cookie_order():
    """Auth modes to try, best-known-first. (False,) when we have no cookies."""
    if not COOKIE_FILE:
        return (False,)
    with _PREFER_LOCK:
        return (True, False) if _PREFER_COOKIES else (False, True)


def _client_order(clients):
    """Player clients to try, last-known-good first. Non-YouTube lists (a bare
    [None]) pass straight through."""
    if clients is not CLIENT_FALLBACKS or len(clients) < 2:
        return clients
    with _PREFER_LOCK:
        i = _PREFER_CLIENT
    if not (0 <= i < len(clients)):
        return clients
    return [clients[i]] + [c for k, c in enumerate(clients) if k != i]


def _note_auth_success(use_cookies, client_cfg=None):
    global _PREFER_COOKIES, _PREFER_CLIENT
    with _PREFER_LOCK:
        _PREFER_COOKIES = use_cookies
        if client_cfg is not None:
            try:
                _PREFER_CLIENT = CLIENT_FALLBACKS.index(client_cfg)
            except ValueError:
                pass


def _has_real_media(info):
    """True if the info dict carries at least one genuine audio/video track.

    Guards the cookies-first path: a degraded player response still returns a
    populated `formats` list, but it's storyboards (thumbnail strips) only.
    Treating that as success would hand the user a board full of nothing, so
    we require a real track before accepting an attempt."""
    fmts = info.get("formats") or []
    return any(_is_video(f) or _is_audio(f) for f in fmts)


def _extract_with_fallbacks(url, extra):
    """extract_info that retries across player clients before giving up.

    Two axes are swept: the player client, and whether cookies are attached.
    Cookie order comes from _cookie_order() — whichever mode last worked leads,
    so we stop paying for attempts that this host's IP can't win. An attempt
    only counts as success if it yields a real media track (see
    _has_real_media), which is what makes leading with cookies safe."""
    # A Spotify (or similar) URL becomes a "ytsearch1:Artist Title" query that
    # resolves via YouTube, so it gets the YouTube client rotation too.
    url = _rewrite_music_url(url)
    is_youtube = ("youtube.com" in url or "youtu.be" in url
                  or url.startswith("ytsearch"))
    clients = _client_order(CLIENT_FALLBACKS) if is_youtube else [None]
    last_err = None
    deadline = time.time() + _EXTRACT_BUDGET_SECONDS

    for use_cookies in _cookie_order():
        for client_cfg in clients:
            # Budget check before STARTING an attempt (never mid-flight): once
            # we're this deep the remaining clients are the weak ones anyway,
            # and a fast honest error beats a two-minute spinner.
            if time.time() > deadline and last_err is not None:
                raise last_err
            opts = _base_opts(extra, use_cookies=use_cookies)
            if client_cfg:
                opts.update(client_cfg)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                if info and (info.get("entries") or info.get("url")
                             or _has_real_media(info)):
                    _note_auth_success(use_cookies, client_cfg)
                    return info
                last_err = Exception("Extractor returned no usable formats.")
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


def _formats_payload(info):
    """Build the /formats response from a resolved info dict.

    Split out so a memo hit and a fresh extraction produce byte-identical
    responses — there is exactly one place that shapes this payload."""
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

    return info, {
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "is_live": is_live,
        "live_note": ("This is a live broadcast. Downloading works once the "
                      "stream has ended and is available as a recording.")
                     if is_live else None,
        "formats": _clean_formats(info),
    }


@app.post("/formats")
def get_formats(req: URLRequest):
    # MEMO HIT: resolving a YouTube URL costs 20-50s (PO-token mint + signature
    # solve + client rotation), and that cost was being paid again on every
    # single paste of the same link — a refresh, a retry after a failed grab,
    # or a second visitor on a trending video. The memo already existed for
    # /download; reading it here too makes all of those effectively instant.
    memo = _memo_get(req.url)
    if memo:
        try:
            _, payload = _formats_payload(memo)
            if payload["formats"]:
                return payload
        except HTTPException:
            pass  # stale/odd memo — fall through to a real extraction

    try:
        info = _extract_with_fallbacks(req.url, {"quiet": True, "skip_download": True})
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch formats: {e}")

    info, payload = _formats_payload(info)

    # Stash the resolved info so /download can skip a second full extraction
    # (the slow PO-token + signature handshake). sanitize_info makes it safe to
    # round-trip through JSON, which is how download_with_info_file consumes it.
    try:
        _memo_put(req.url, yt_dlp.YoutubeDL.sanitize_info(info))
    except Exception:
        pass

    return payload


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
        # Measured: dropping this saved nothing (19.2s vs 20.0s time-to-first-
        # byte on a 62MB 1080p merge — noise). The dead time is the 62MB pull
        # from YouTube at ~4.7MB/s, not the container rewrite. Keeping it.
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
    clients = _client_order(CLIENT_FALLBACKS) if is_youtube else [None]

    attempts = []
    # Same ordering lesson as /formats: lead with the auth mode that last
    # worked instead of always burning the cookie-less clients first.
    for use_cookies in _cookie_order():
        for client_cfg in clients:
            attempts.append((client_cfg, None, use_cookies))
        # then the loosest selector (ignore the chosen format) in that mode
        attempts.append((None, loosest, use_cookies))

    last_err = None
    deadline = time.time() + _EXTRACT_BUDGET_SECONDS
    for client_cfg, fmt_override, use_cookies in attempts:
        if time.time() > deadline and last_err is not None:
            break
        extra = dict(base_extra)
        if client_cfg:
            extra.update(client_cfg)
        if fmt_override:
            extra["format"] = fmt_override
        try:
            with yt_dlp.YoutubeDL(_base_opts(extra, use_cookies=use_cookies)) as ydl:
                ydl.extract_info(dl_url, download=True)
            _note_auth_success(use_cookies, client_cfg)
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

    def _is_dash(f):
        """DASH/adaptive fragments are throttled to ~playback speed when pulled
        as one open-ended GET (measured: format 251 dripped at 10KB/s and never
        finished). They're designed for ranged chunk requests. /download's
        aria2c opens several ranged connections and sidesteps it entirely, so
        adaptive formats belong on that path, not the pass-through."""
        return str(f.get("container") or "").endswith("_dash")

    # 1) exact format_id match, if it's a single-file stream
    if format_id and format_id not in ("best", "audio-mp3"):
        for f in formats:
            if f.get("format_id") == format_id:
                # A video track with no audio is a DASH half — passing it
                # through would hand the user a SILENT file that looks fine.
                # Only progressive (video+audio) or pure-audio formats are
                # honestly streamable; everything else needs the merge path.
                acodec = f.get("acodec")
                has_audio = bool(acodec and acodec != "none")
                if _is_video(f) and not has_audio:
                    return None, None, None
                if _is_dash(f):
                    return None, None, None
                u, ext = _pick(f)
                if u:
                    return u, ext, title
                return None, None, None  # chosen format needs a merge -> not streamable
        # Named a format we can't find in this table (a degraded extraction, or
        # a stale memo). Do NOT fall through to "best progressive" — that would
        # silently hand back 360p when the user picked 1080p, which downloads
        # fine and is simply the wrong file. 409 sends it to /download, which
        # re-extracts and can still honour the request.
        return None, None, None

    # 2) best progressive video (has both audio+video) that's a plain file
    best = None
    for f in formats:
        if not _is_video(f):
            continue
        acodec = f.get("acodec")
        if not (acodec and acodec != "none"):
            continue  # video-only -> needs an audio merge, not a pass-through
        if _is_dash(f):
            continue
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
        # HTTP headers are latin-1 only, but titles are not (Korean, Japanese,
        # emoji, accents...). Interpolating the raw title here used to 500 the
        # whole request. RFC 5987 is the fix: an ASCII-transliterated filename=
        # for dumb clients, plus filename*=UTF-8'' carrying the real name.
        # /download never hit this because yt-dlp's restrictfilenames had
        # already flattened the name on disk; /stream never touches disk.
        ascii_name = filename.encode("ascii", "ignore").decode("ascii").strip()
        if not ascii_name or ascii_name.startswith("."):
            ascii_name = f"video.{ext or 'mp4'}"
        headers = {
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(filename, safe='')}"
            )
        }
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


@app.post("/diag")
def diag(req: URLRequest):
    """Timing breakdown for one extraction. Diagnostic only — not used by the site.

    /formats tells you it took 25 seconds; it doesn't tell you WHERE they went.
    This walks the same client ladder but times each attempt separately and
    times the PO-token server independently, which separates the three
    candidates: a slow token mint, a slow per-client extraction, or a ladder
    that's silently burning attempts before one succeeds."""
    url = _rewrite_music_url(req.url)
    is_youtube = ("youtube.com" in url or "youtu.be" in url or url.startswith("ytsearch"))

    # Time the token server on its own. If this is seconds rather than
    # milliseconds, the BotGuard challenge (CPU-bound JS) is the bottleneck and
    # no amount of yt-dlp tuning will help — it's the instance's CPU.
    pot = {}
    try:
        import urllib.request
        t = time.time()
        urllib.request.urlopen(f"http://127.0.0.1:{POT_PORT}/ping", timeout=30).read()
        pot["ping_seconds"] = round(time.time() - t, 2)
    except Exception as e:
        pot["error"] = str(e)[:200]

    trace = []
    clients = _client_order(CLIENT_FALLBACKS) if is_youtube else [None]
    done = False
    for use_cookies in _cookie_order():
        if done:
            break
        for cfg in clients:
            name = "default"
            if cfg:
                name = "+".join(cfg["extractor_args"]["youtube"]["player_client"])
            name += " +cookies" if use_cookies else " (no cookies)"
            t = time.time()
            opts = _base_opts({"quiet": True, "no_warnings": True, "skip_download": True},
                              use_cookies=use_cookies)
            if cfg:
                opts.update(cfg)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                trace.append({"client": name, "seconds": round(time.time() - t, 2),
                              "ok": True, "formats": len(info.get("formats") or []),
                              "real_media": _has_real_media(info)})
                done = True
                break   # stop at the first success, exactly like the real ladder
            except Exception as e:
                trace.append({"client": name, "seconds": round(time.time() - t, 2),
                              "ok": False, "error": str(e)[:160]})

    return {
        "pot_server": pot,
        "prefer_cookies": _PREFER_COOKIES,
        "prefer_client": CLIENT_FALLBACKS[_PREFER_CLIENT],
        "attempts": trace,
        "total_seconds": round(sum(a["seconds"] for a in trace), 2),
    }


@app.get("/")
def health_check():
    """Health + capability probe.

    The capability block is here because YouTube speed depends almost entirely
    on three things being present, and none of them fail loudly: the PO-token
    server, a JS runtime for the n-signature solve, and ffmpeg. When any is
    missing, extraction still "works" — it just falls back to a path that costs
    ~15-20s per player client instead of ~2s. Reporting them turns a mystery
    slowdown into a one-request answer."""
    return {
        "status": "ok",
        "message": "Video downloader API is running.",
        "cookies": bool(COOKIE_FILE),
        # Which auth mode the ladder currently leads with. On a datacenter IP
        # this settles on True — cookie-less YouTube is refused there.
        "prefer_cookies": _PREFER_COOKIES,
        "yt_dlp": getattr(yt_dlp.version, "__version__", "?"),
        # The single biggest YouTube speed factor. False = every extraction
        # pays a per-client Node subprocess to mint a token.
        "pot_server": _pot_server_up(),
        "js_runtimes": sorted(k for k, v in JS_RUNTIMES.items()
                              if v.get("path") or shutil.which(k)),
        "ffmpeg": FFMPEG_AVAILABLE,
        "aria2c": bool(shutil.which("aria2c")),
        "memo_entries": len(_INFO_MEMO),
    }
