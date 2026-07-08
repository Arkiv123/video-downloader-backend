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
    """Collapse yt-dlp's raw format list into simple, human-friendly options."""
    seen = set()
    options = []

    for f in info.get("formats", []):
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        ext = f.get("ext")
        height = f.get("height")

        if vcodec != "none" and height:
            label = f"{height}p ({ext})"
            key = ("video", height, ext)
            if key not in seen:
                seen.add(key)
                options.append({
                    "format_id": f.get("format_id"),
                    "label": label,
                    "type": "video",
                    "ext": ext,
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                })
        elif vcodec == "none" and acodec != "none":
            abr = f.get("abr")
            label = f"Audio only (~{int(abr)}kbps)" if abr else "Audio only"
            key = ("audio", int(abr) if abr else 0, ext)
            if key not in seen:
                seen.add(key)
                options.append({
                    "format_id": f.get("format_id"),
                    "label": label,
                    "type": "audio",
                    "ext": ext,
                    "filesize": f.get("filesize") or f.get("filesize_approx"),
                })

    videos = sorted(
        [o for o in options if o["type"] == "video"],
        key=lambda o: int(o["label"].split("p")[0]),
        reverse=True,
    )
    audios = [o for o in options if o["type"] == "audio"]
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

    ydl_opts = {
        "outtmpl": outtmpl,
        # chosen video + best audio, with safe fallbacks so it never comes back silent
        "format": "bestaudio/best" if req.audio_only else f"{req.format_id}+bestaudio/best/{req.format_id}/best",
        # prefer mp4, but allow mkv when codecs don't fit mp4 (webm sources etc.)
        "merge_output_format": "mp4/mkv",
        "concurrent_fragment_downloads": 8,
        "noplaylist": True,
        "restrictfilenames": True,          # kills weird-character filename bugs
        # faststart = clean scrubbing/seeking in VLC and every player
        "postprocessor_args": {"merger+ffmpeg": ["-movflags", "+faststart"]},
        **YOUTUBE_BYPASS,
    }

    # aria2c makes it faster, but not every environment has it. use it only if present.
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
