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
from fastapi.responses import FileResponse
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

app = FastAPI(title="Video Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = "downloads"
CACHE_DIR = "cache"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

CACHE_INDEX_PATH = os.path.join(CACHE_DIR, "_index.json")
MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024   # keep the cache under ~2 GB

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

# --- YouTube player-client fallbacks. If the default extraction fails
#     (bot check, empty formats, SABR-only response), retry with other
#     clients. Harmless for non-YouTube URLs (extractor_args are ignored).
#
#     Order matters: the default (yt-dlp's own rotation) goes first because
#     paired with a PO-token provider it now returns full format tables.
#     web_safari + mweb are the clients that a PO token unlocks; tv/android
#     are last-resort because they often serve only storyboards or DRM.
CLIENT_FALLBACKS = [
    None,
    {"extractor_args": {"youtube": {"player_client": ["web_safari", "mweb"]}}},
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
        "socket_timeout": 30,
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
    is_youtube = "youtube.com" in url or "youtu.be" in url
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
    try:
        with open(CACHE_INDEX_PATH, "r") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _save_index(idx):
    try:
        with open(CACHE_INDEX_PATH, "w") as fh:
            json.dump(idx, fh)
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
            opt = {
                "format_id": f.get("format_id"),
                "label": f"{height}p ({ext})",
                "type": "video",
                "ext": ext,
                "height": height,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "progressive": has_audio,
            }
            cur = best_video.get(height)
            if cur is None or _better(opt, cur, "mp4"):
                best_video[height] = opt

        elif _is_audio(f):
            abr = int(f.get("abr") or f.get("tbr") or 0)
            label = f"Audio only (~{abr}kbps)" if abr else "Audio only"
            opt = {
                "format_id": f.get("format_id"),
                "label": label,
                "type": "audio",
                "ext": ext,
                "abr": abr,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "progressive": True,
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

    return {
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
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

    base_extra = {
        "outtmpl": outtmpl,
        "format": _build_format_chain(req, audio_only),
        "concurrent_fragment_downloads": 16,
        "restrictfilenames": True,
    }

    if not audio_only and FFMPEG_AVAILABLE:
        base_extra["merge_output_format"] = "mp4"
        base_extra["postprocessor_args"] = {"merger": ["-movflags", "+faststart"]}

    if shutil.which("aria2c"):
        base_extra["external_downloader"] = "aria2c"
        base_extra["external_downloader_args"] = ["-x", "16", "-s", "16", "-k", "1M"]

    if audio_only and FFMPEG_AVAILABLE:
        base_extra["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    # Try default extraction first; on 'Requested format is not available' or
    # bot-check errors, retry. YouTube URLs rotate player clients; every
    # platform gets a final attempt with the loosest possible 'best' selector.
    # Cookies stay OFF for the first full sweep (a stale cookie forces a
    # degraded YouTube player that returns no media) and only turn ON for a
    # final escalation sweep, for genuinely gated content.
    is_youtube = "youtube.com" in req.url or "youtu.be" in req.url
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
    downloaded = False
    for client_cfg, fmt_override, use_cookies in attempts:
        extra = dict(base_extra)
        if client_cfg:
            extra.update(client_cfg)
        if fmt_override:
            extra["format"] = fmt_override
        try:
            with yt_dlp.YoutubeDL(_base_opts(extra, use_cookies=use_cookies)) as ydl:
                ydl.extract_info(req.url, download=True)
            downloaded = True
            break
        except Exception as e:
            last_err = e

    if not downloaded:
        raise HTTPException(status_code=400, detail=f"Download failed: {last_err}")

    matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}_*"))
    matches = [m for m in matches if not m.endswith((".part", ".ytdl", ".temp", ".aria2"))]
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


@app.get("/")
def health_check():
    return {"status": "ok", "message": "Video downloader API is running.", "cookies": bool(COOKIE_FILE)}
