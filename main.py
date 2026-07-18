"""
Core video downloader engine.
Supports quality selection (video/audio) and uses aria2c for max download speed
when it is installed (falls back to yt-dlp's own downloader otherwise).
"""

import yt_dlp
import os
import shutil

DOWNLOAD_DIR = "downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def get_available_formats(url: str):
    """
    Fetches available video/audio quality options for a given URL
    WITHOUT downloading anything. Used to populate a quality dropdown.
    """
    ydl_opts = {"quiet": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = []
        for f in info.get("formats", []):
            formats.append({
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution") or "audio only",
                "filesize": f.get("filesize"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
            })
        return {
            "title": info.get("title"),
            "thumbnail": info.get("thumbnail"),
            "duration": info.get("duration"),
            "formats": formats,
        }


def download_video(url: str, format_id: str = "best", audio_only: bool = False):
    """
    Downloads a video (or audio) at the requested quality/format.
    Uses aria2c as external downloader + multiple connections when available.
    """
    if audio_only:
        chosen_format = "bestaudio/best"
    elif format_id == "best":
        chosen_format = "bestvideo+bestaudio/best"
    else:
        # video-only formats (common on YouTube >720p) need audio merged in
        chosen_format = f"{format_id}+bestaudio/{format_id}/best"

    ydl_opts = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
        "format": chosen_format,
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,
        "noplaylist": True,
    }

    if shutil.which("aria2c"):
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = [
            "-x", "16",   # 16 connections per download
            "-s", "16",   # split file into 16 pieces
            "-k", "1M"    # 1MB min split size
        ]

    if audio_only:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        # post-processing changes the extension (merge -> mp4, audio -> mp3);
        # prepare_filename doesn't know that, so fix it up here
        base = os.path.splitext(filename)[0]
        if audio_only:
            candidate = base + ".mp3"
            if os.path.exists(candidate):
                return candidate
        for ext in (".mp4", ".mkv", ".webm"):
            candidate = base + ext
            if os.path.exists(candidate):
                return candidate
        return filename


# Quick local test (run this file directly to test in terminal)
if __name__ == "__main__":
    test_url = input("Paste a video URL to test: ").strip()
    print("\nFetching available qualities...\n")
    data = get_available_formats(test_url)
    print(f"Title: {data['title']}\n")
    for f in data["formats"]:
        print(f"  [{f['format_id']}] {f['resolution']} - {f['ext']} - vcodec={f['vcodec']} acodec={f['acodec']}")

    choice = input("\nEnter format_id to download (or press Enter for best quality): ").strip()
    audio_flag = input("Audio only? (y/n): ").strip().lower() == "y"

    print("\nDownloading...\n")
    path = download_video(test_url, format_id=choice or "best", audio_only=audio_flag)
    print(f"\nDone! Saved to: {path}")