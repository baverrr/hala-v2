"""Video, audio and subtitles via ffmpeg."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ..formats import AUDIO_OUT
from ..i18n import _
from ..runner import Cancelled, Context, ConversionError

log = logging.getLogger("hala")
# 4:2:0 encoders need even dimensions; phone and screen recordings are often odd.
EVEN_SIZE = ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]

VIDEO_CODECS: dict[str, list[str]] = {
    "mp4": ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
    "mov": ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"],
    "mkv": ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-c:a", "aac", "-b:a", "192k"],
    # -cpu-used 4: libvpx defaults to its slowest mode, ~5x slower for little gain.
    "webm": ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1", "-deadline", "good",
             "-cpu-used", "4",
             "-c:a", "libopus", "-b:a", "128k"],
    "avi": ["-c:v", "mpeg4", "-q:v", "3", "-c:a", "libmp3lame", "-q:a", "2"],
    "wmv": ["-c:v", "wmv2", "-b:v", "4M", "-c:a", "wmav2", "-b:a", "192k"],
}
AUDIO_CODECS: dict[str, list[str]] = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "2"],
    "m4a": ["-c:a", "aac", "-b:a", "256k"],
    "wav": ["-c:a", "pcm_s16le"],
    "flac": ["-c:a", "flac"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "6"],
    "opus": ["-c:a", "libopus", "-b:a", "160k"],
    "aiff": ["-c:a", "pcm_s16be"],
}
# Codecs these containers take as-is, so a rewrap is enough (much faster, no quality loss).
REMUX_OK: dict[str, tuple[set[str], set[str]]] = {
    "mp4": ({"h264", "hevc", "av1"}, {"aac", "mp3", "alac", "opus"}),
    "mov": ({"h264", "hevc", "prores"}, {"aac", "mp3", "alac", "pcm_s16le"}),
    "mkv": ({"h264", "hevc", "av1", "vp8", "vp9", "mpeg4"},
            {"aac", "mp3", "opus", "vorbis", "flac", "ac3", "eac3", "dts"}),
    "webm": ({"vp8", "vp9", "av1"}, {"opus", "vorbis"}),
}


def probe(ctx: Context, src: Path) -> dict:
    out = ctx.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                   "-show_streams", src])
    return json.loads(out)


def _duration(info: dict) -> float:
    try:
        return float(info["format"]["duration"])
    except (KeyError, ValueError):
        return 0.0


def _ffmpeg(ctx: Context, args: list[str | Path], duration: float) -> None:
    """Run ffmpeg, reporting progress from its -progress stream."""

    def on_line(line: str) -> None:
        key, _sep, value = line.partition("=")
        if key == "out_time_us" and duration > 0 and value.isdigit():
            ctx.progress(int(value) / 1e6 / duration)

    ctx.run(["ffmpeg", "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args],
            stdout_line=on_line)


def _can_remux(info: dict, dst_ext: str) -> bool:
    if dst_ext not in REMUX_OK:
        return False
    video_ok, audio_ok = REMUX_OK[dst_ext]
    streams = info.get("streams", [])
    videos = [s for s in streams if s.get("codec_type") == "video"
              and not s.get("disposition", {}).get("attached_pic")]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    if not videos:
        return False
    return (all(s.get("codec_name") in video_ok for s in videos)
            and all(s.get("codec_name") in audio_ok for s in audios))


def convert_video(ctx: Context, src: Path, dst_ext: str) -> Path:
    info = probe(ctx, src)
    duration = _duration(info)
    out = ctx.workdir / f"out.{dst_ext}"
    if dst_ext in AUDIO_OUT:
        if not any(s.get("codec_type") == "audio" for s in info.get("streams", [])):
            raise ConversionError(_("this video has no audio track"))
        _ffmpeg(ctx, ["-i", src, "-vn", "-map", "0:a:0", "-map_metadata", "0",
                      *AUDIO_CODECS[dst_ext], out], duration)
        return out
    if dst_ext == "gif":
        # Per-video palette for decent GIF quality; capped size and frame rate.
        graph = ("fps=12,scale='min(iw,720)':-2:flags=lanczos,split[a][b];"
                 "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4")
        _ffmpeg(ctx, ["-i", src, "-filter_complex", graph, "-loop", "0", out], duration)
        return out
    if _can_remux(info, dst_ext):
        try:
            _ffmpeg(ctx, ["-i", src, "-map", "0:v", "-map", "0:a?", "-c", "copy",
                          *(["-movflags", "+faststart"] if dst_ext in ("mp4", "mov") else []),
                          out], duration)
            return out
        except Cancelled:
            raise
        except ConversionError as exc:
            log.info("remux of %s to %s failed, transcoding instead: %s", src, dst_ext, exc)
            out.unlink(missing_ok=True)
    _ffmpeg(ctx, ["-i", src, "-map", "0:v:0", "-map", "0:a?", "-map_metadata", "0",
                  *EVEN_SIZE, *VIDEO_CODECS[dst_ext], out], duration)
    return out


def convert_audio(ctx: Context, src: Path, dst_ext: str) -> Path:
    info = probe(ctx, src)
    out = ctx.workdir / f"out.{dst_ext}"
    # Keep embedded cover art where the target container can carry it.
    keep_cover = dst_ext in ("mp3", "m4a", "flac") and any(
        s.get("disposition", {}).get("attached_pic") for s in info.get("streams", []))
    maps = ["-map", "0:a:0", *(["-map", "0:v:0", "-c:v", "copy", "-disposition:v",
                                "attached_pic"] if keep_cover else [])]
    _ffmpeg(ctx, ["-i", src, *maps, "-map_metadata", "0", *AUDIO_CODECS[dst_ext], out],
            _duration(info))
    return out


def convert_subtitle(ctx: Context, src: Path, dst_ext: str) -> Path:
    out = ctx.workdir / f"out.{dst_ext}"
    _ffmpeg(ctx, ["-i", src, out], 0)
    return out
