"""
FastAPI backend for the video downloader.
Exposes:
  POST /formats   -> returns clean quality options for a given URL
  POST /download  -> downloads at chosen quality, merges audio, streams file back
                     (but redirects to the direct source link when no merge is
                      needed -> lightning fast, bypasses this server entirely)
"""

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
import yt_dlp
import os
import uuid
import glob
import shutil

app = FastAPI(title="Video Downloader API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# --- The bot-check bypass. Makes yt-dlp pose as the YouTube phone app,
#     which skips most "confirm you're not a bot" blocks. No cookies needed.
YOUTUBE_BYPASS = {
    "extractor_args": {"youtube": {"player_client": ["android", "ios", "web"]}}
}


class URLRequest(BaseModel):
    url: str


class DownloadRequest(BaseModel):
    url: str
    format_id: str = "best"
    audio_only: bool = False


def _clean_formats(info):
    """Collapse yt-dlp's raw format list into simple, human-friendly options.

    Keeps the FULL resolution ladder. When two entries share the same
    resolution, we keep the better one (mp4 preferred, bigger filesize wins)
    instead of dropping resolutions like the old version did.
    """
    best_video = {}   # height -> option
    best_audio = {}    # abr    -> option

    def _score(f, prefer_ext):
        # higher is better: prefer the chosen container, then bigger file
        ext_bonus = 1 if f.get("ext") == prefer_ext else 0
        size = f.get("filesize") or f.get("filesize_approx") or 0
        return (ext_bonus, size)

    for f in info.get("formats", []):
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        ext = f.get("ext")
        height = f.get("height")

        # video streams (with or without built-in audio)
        if vcodec and vcodec != "none" and height:
            has_audio = acodec and acodec != "none"
            opt = {
                "format_id": f.get("format_id"),
                "label": f"{height}p ({ext})",
                "type": "video",
                "ext": ext,
                "height": height,
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "progressive": bool(has_audio),  # already has audio? no merge needed
                "url": f.get("url"),
            }
            cur = best_video.get(height)
            if cur is None:
                best_video[height] = opt
            else:
                # prefer a progressive (audio+video) stream, then mp4, then bigger
                cur_prog = cur.get("progressive")
                if opt["progressive"] and not cur_prog:
                    best_video[height] = opt
                elif opt["progressive"] == cur_prog and _score(f, "mp4") > _score(
                    {"ext": cur["ext"], "filesize": cur["filesize"]}, "mp4"
                ):
                    best_video[height] = opt

        # audio-only streams
        elif (not vcodec or vcodec == "none") and acodec and acodec != "none":
            abr = f.get("abr") or 0
            label = f"Audio only (~{int(abr)}kbps)" if abr else "Audio only"
            opt = {
                "format_id": f.get("format_id"),
                "label": label,
                "type": "audio",
                "ext": ext,
                "abr": int(abr),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "progressive": True,   # audio-only never needs a merge
                "url": f.get("url"),
            }
            key = int(abr)
            if key not in best_audio or _score(f, "m4a") > _score(
                {"ext": best_audio[key]["ext"], "filesize": best_audio[key]["filesize"]},
                "m4a",
            ):
                best_audio[key] = opt

    videos = sorted(best_video.values(), key=lambda o: o["height"], reverse=True)
    audios = sorted(best_audio.values(), key=lambda o: o.get("abr", 0), reverse=True)
    return videos + audios


@app.post("/formats")
def get_formats(req: URLRequest):
    try:
        ydl_opts = {"quiet": True, "skip_download": True, "noplaylist": True, **YOUTUBE_BYPASS}
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


def _direct_url_for(url, format_id):
    """Ask yt-dlp for the direct CDN link of a single (progressive) format.
    Returns the URL if that format already has audio+video, else None."""
    try:
        opts = {"quiet": True, "skip_download": True, "noplaylist": True,
                "format": format_id, **YOUTUBE_BYPASS}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        # when a single format is selected, yt-dlp puts its details at top level
        req_formats = info.get("requested_formats")
        if req_formats:
            return None  # multiple streams -> needs merging, can't direct-link
        vcodec = info.get("vcodec")
        acodec = info.get("acodec")
        if vcodec and vcodec != "none" and acodec and acodec != "none":
            return info.get("url")
    except Exception:
        return None
    return None


@app.post("/download")
def download(req: DownloadRequest):
    # LIGHTNING PATH: if the chosen format already has audio+video, send the
    # browser straight to the source CDN. No server download, near-instant.
    if not req.audio_only:
        direct = _direct_url_for(req.url, req.format_id)
        if direct:
            return RedirectResponse(url=direct, status_code=302)

    # MERGE PATH: format needs video+audio combined (or audio extraction).
    job_id = str(uuid.uuid4())[:8]
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title)s.%(ext)s")

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": "bestaudio/best" if req.audio_only else f"{req.format_id}+bestaudio/best/{req.format_id}/best",
        "merge_output_format": "mp4/mkv",
        "concurrent_fragment_downloads": 16,   # more parallel = faster
        "noplaylist": True,
        "restrictfilenames": True,
        "postprocessor_args": {"merger+ffmpeg": ["-movflags", "+faststart"]},
        **YOUTUBE_BYPASS,
    }

    if shutil.which("aria2c"):
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = ["-x", "16", "-s", "16", "-k", "1M"]

    if req.audio_only:
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

    return FileResponse(
        path=filename,
        filename=os.path.basename(filename).split("_", 1)[-1],
        media_type="application/octet-stream",
    )


@app.get("/")
def health_check():
    return {"status": "ok", "message": "Video downloader API is running."}
