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
import yt_dlp
import os
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

# --- The bot-check bypass. Makes yt-dlp pose as the YouTube phone app,
#     which skips most "confirm you're not a bot" blocks.
YOUTUBE_BYPASS = {
    "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}}
}

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


def _base_opts(extra=None):
    """Common yt-dlp options. Use cookies when available; otherwise fall back
    to the phone-app bypass. The two don't play nicely together, so pick one."""
    opts = {"noplaylist": True}
    if COOKIE_FILE:
        opts["cookiefile"] = COOKIE_FILE   # cookies alone; no client override
    else:
        opts.update(YOUTUBE_BYPASS)        # no cookies -> phone-app trick
    if extra:
        opts.update(extra)
    return opts


class URLRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str = "best"
    audio_only: bool = False


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
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        ext = f.get("ext")
        height = f.get("height")

        if vcodec and vcodec != "none" and height:
            has_audio = acodec and acodec != "none"
            opt = {
                "format_id": f.get("format_id"),
                "label": f"{height}p ({ext})",
                "type": "video",
                "ext": ext,
                "height": height,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "progressive": bool(has_audio),
            }
            cur = best_video.get(height)
            if cur is None or _better(opt, cur, "mp4"):
                best_video[height] = opt

        elif (not vcodec or vcodec == "none") and acodec and acodec != "none":
            abr = int(f.get("abr") or 0)
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
        ydl_opts = _base_opts({"quiet": True, "skip_download": True})
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not fetch formats: {e}")

    return {
        "title": info.get("title"),
        "thumbnail": info.get("thumbnail"),
        "duration": info.get("duration"),
        "formats": _clean_formats(info),
    }


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

    chosen_format = "bestaudio/best" if audio_only else f"{req.format_id}+bestaudio/best/{req.format_id}/best"

    ydl_opts = _base_opts({
        "outtmpl": outtmpl,
        "format": chosen_format,
        "merge_output_format": "mp4/mkv",
        "concurrent_fragment_downloads": 16,
        "restrictfilenames": True,
        "postprocessor_args": {"merger+ffmpeg": ["-movflags", "+faststart"]},
    })

    if shutil.which("aria2c"):
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = ["-x", "16", "-s", "16", "-k", "1M"]

    if audio_only:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(req.url, download=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Download failed: {e}")

    matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}_*"))
    matches = [m for m in matches if not m.endswith((".part", ".ytdl", ".temp"))]
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
