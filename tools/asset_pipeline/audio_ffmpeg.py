"""FFmpeg for setup-time audio transcoding (conversion only, never shipped).

Resolution order: SKATE3_FFMPEG override, system PATH, then a hash-pinned
LGPL Windows build downloaded like the other setup tools. The pinned build
is only a fallback; prefer a local FFmpeg. Only the decoder (native XMA)
and Vorbis encoder are used.
"""
import os
import shutil
from pathlib import Path

# BtbN n9.0 LGPL static Windows build. The `latest` release rolls, so a
# checksum mismatch fails loudly instead of converting with unknown bits.
FFMPEG_URL = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-n9.0-latest-win64-lgpl-9.0.zip")
FFMPEG_SHA = "ef4ad9973c87043387a0c79158df7adbe32ed516fc5f591e6dce25b639a4b14b"

def executable(cache, report):
    override = os.environ.get("SKATE3_FFMPEG")
    if override:
        candidate = Path(override)
        if candidate.is_file():
            return candidate
        raise RuntimeError(f"SKATE3_FFMPEG does not exist: {override}")
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    if os.name != "nt":
        # The pinned fallback below is a Windows build; macOS/Linux setups
        # install FFmpeg from their package manager instead.
        raise RuntimeError("FFmpeg is required for music conversion "
                           "(brew install ffmpeg, apt install ffmpeg, or set SKATE3_FFMPEG)")
    from . import install as engine
    folder = Path(cache) / "ffmpeg"
    marker = folder / ".complete"
    if not marker.is_file():
        archive = engine.download(FFMPEG_URL, FFMPEG_SHA, Path(cache), report)
        report("Extracting FFmpeg")
        engine.unpack_zip(archive, folder)
        marker.write_text(FFMPEG_SHA, encoding="utf-8")
    exe = next(folder.rglob("ffmpeg.exe"), None)
    if exe is None:
        raise RuntimeError("Missing ffmpeg.exe in downloaded FFmpeg build")
    return exe

def probe_for(ffmpeg):
    """Matching ffprobe for duration spot-checks, if one is available."""
    sibling = Path(ffmpeg).parent / ("ffprobe" + Path(ffmpeg).suffix)
    if sibling.is_file():
        return sibling
    found = shutil.which("ffprobe")
    return Path(found) if found else None
