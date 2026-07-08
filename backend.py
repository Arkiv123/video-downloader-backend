"""
FastAPI backend for the video downloader.
Exposes:
  POST /formats   -> returns clean quality options for a given URL
  POST /download  -> downloads at chosen quality, merges audio, streams file back
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

    Keeps the FULL resolution ladder, and ALWAYS offers an "Audio only (MP3)"
    choice even when the site (e.g. Pinterest) only serves combined streams,
    because we can extract audio from the best stream on the server.
    """
    best_video = {}   # height -> option
    best_audio = {}   # abr    -> option

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

    # GUARANTEE an audio choice. If the site gave no separate audio track
    # (Pinterest, some IG/TikTok), offer an MP3 we extract from the best stream.
    if not audios:
        audios = [{
            "format_id": "audio-mp3",   # special marker handled in /download
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


@app.post("/download")
def download(req: DownloadRequest):
    job_id = str(uuid.uuid4())[:8]
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{job_id}_%(title)s.%(ext)s")

    # treat our special MP3 marker as an audio-only request
    audio_only = req.audio_only or req.format_id == "audio-mp3"

    if audio_only:
        chosen_format = "bestaudio/best"
    else:
        # chosen video + best audio, with safe fallbacks so it never comes back silent
        chosen_format = f"{req.format_id}+bestaudio/best/{req.format_id}/best"

    ydl_opts = {
        "outtmpl": outtmpl,
        "format": chosen_format,
        "merge_output_format": "mp4/mkv",
        "concurrent_fragment_downloads": 16,   # more parallel chunks = faster
        "noplaylist": True,
        "restrictfilenames": True,
        "postprocessor_args": {"merger+ffmpeg": ["-movflags", "+faststart"]},
        **YOUTUBE_BYPASS,
    }

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

    # don't guess the extension — find whatever file actually got created for this job
    matches = glob.glob(os.path.join(DOWNLOAD_DIR, f"{job_id}_*"))
    matches = [m for m in matches if not m.endswith((".part", ".ytdl", ".temp"))]
    if not matches:
        raise HTTPException(status_code=500, detail="File not found after download.")
    filename = max(matches, key=os.path.getsize)  # the finished file is the biggest

    return FileResponse(
        path=filename,
        filename=os.path.basename(filename).split("_", 1)[-1],
        media_type="application/octet-stream",
    )


@app.get("/")
def health_check():
    return {"status": "ok", "message": "Video downloader API is running."}
