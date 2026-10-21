"""Compress to a target size, trying the best-looking way first and falling back step by step.

Pictures: quality, then without metadata, then (if allowed) WebP, then smaller. Video: a
two-pass encode at the bitrate that fits, with resolution, frame rate and sound lowered as the
budget shrinks, re-encoded smaller if the result overshoots. Audio: bitrate (mono when low,
Opus if allowed). PDFs: a lossless repack, Ghostscript's presets, then lower image resolutions.
"""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

from .. import formats as f
from ..engines import images, media
from ..runner import Context, ConversionError
from . import Param, Params, Tool

LOSSY_IMAGES = ("jpg", "webp", "heic", "avif")
LOSSY_AUDIO = {"mp3": ("libmp3lame",), "m4a": ("aac",), "ogg": ("libvorbis",),
               "opus": ("libopus",)}
# Containers keep a little overhead beyond the streams; aim this far under the target.
HEADROOM = 0.96
MIN_VIDEO_KBPS = 40
# Below this many kb/s for the picture, fewer pixels look better than blocky ones.
VIDEO_HEIGHTS = ((300, 360), (600, 480), (1500, 720))
LOW_FPS_KBPS = 400  # and fewer frames: 24 a second instead of 30-60
MIN_AUDIO_KBPS = 24
MIN_OPUS_KBPS = 12  # Opus stays clear for speech down here, where MP3 and AAC fall apart
MONO_BELOW_KBPS = 64
ENCODE_TRIES = 3  # a re-encode at a lower bitrate when the result came out too big
PDF_PRESETS = ("/printer", "/ebook", "/screen")  # 300, 150 and 72 dpi images
PDF_LOW_DPI = (50, 36)  # after /screen: pictures at these resolutions


def _half_size_mb(files: list[Path]) -> float:
    largest = max(p.stat().st_size for p in files)
    return max(0.1, round(largest / 2 / 1e6, 1))


SIZE = Param("size", "Compress to", "float", 10.0, "target size of each file", unit="MB",
             suggest=_half_size_mb)
OTHER_FORMAT = Param("reformat", "Allow a smaller format", "bool", False,
                     "pictures may become WebP and sound Opus when that's what fits",
                     only_for=frozenset({*f.IMAGE_IN, *f.AUDIO_IN}))


def compress(ctx: Context, inputs: list[Path], params: Params) -> Path:
    src = inputs[0]
    target = int(params["size"] * 1e6)
    if target <= 0:
        raise ConversionError("the target size has to be above 0 MB")
    if src.stat().st_size <= target:
        raise ConversionError(f"{src.name} is already under {params['size']:g} MB")
    other = bool(params.get("reformat"))
    ext = f.ext_of(src)
    if ext in f.IMAGE_IN:
        return _image(ctx, src, target, other)
    if ext in f.VIDEO_IN:
        return _video(ctx, src, target)
    if ext in f.AUDIO_IN:
        return _audio(ctx, src, target, other)
    return _pdf(ctx, src, target)


def _image(ctx: Context, src: Path, target: int, other: bool) -> Path:
    """Highest quality that fits: with metadata, then without, then (if allowed) as WebP;
    if nothing fits even at quality 10, scale down and search again."""
    ext = f.ext_of(src)
    out_ext = ext if ext in LOSSY_IMAGES else "jpg"  # PNG/TIFF/BMP can't trade quality
    images.check_input(ctx, src)
    staged = images._normalize(ctx, src, flatten=out_ext == "jpg")
    tries = [(out_ext, "false"), (out_ext, "true")]
    if other and out_ext != "webp":
        tries.append(("webp", "true"))
    for i, (fmt, strip) in enumerate(tries):
        if (out := _best_quality(ctx, staged, fmt, strip, target)) is not None:
            return out
        ctx.progress(0.1 * (i + 1))
    fmt = tries[-1][0]  # the smallest kind tried: shrink that one
    out = ctx.workdir / f"out.{fmt}"
    scale = 1.0
    for attempt in range(6):
        # Too big even at quality 10: shrink by the area ratio, plus a margin.
        scale *= math.sqrt(target / out.stat().st_size) * 0.9
        scaled = ctx.workdir / "scaled.v"
        ctx.run(["vips", "resize", staged, scaled, f"{scale:.4f}"])
        if (found := _best_quality(ctx, scaled, fmt, "true", target)) is not None:
            return found
        ctx.progress(min(0.9, 0.4 + 0.1 * attempt))
    raise ConversionError("couldn't get this image that small")


def _best_quality(ctx: Context, source: Path, fmt: str, strip: str,
                  target: int) -> Path | None:
    """The file at the highest quality (10-95) that fits, or None; out.FMT holds the last try."""
    out = ctx.workdir / f"out.{fmt}"

    def size_at(quality: int) -> int:
        ctx.run(["vips", "copy", source, f"{out}[Q={quality},strip={strip}]"])
        return out.stat().st_size

    lo, hi, best = 10, 95, None
    while lo <= hi:
        mid = (lo + hi) // 2
        if size_at(mid) <= target:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best is None:
        size_at(10)  # leave the smallest attempt, for the next step's estimate
        return None
    size_at(best)
    return out


def _video(ctx: Context, src: Path, target: int) -> Path:
    info = media.probe(ctx, src)
    duration = media._duration(info)
    if duration <= 0:
        raise ConversionError("can't tell how long this video is")
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    total_kbps = target * 8 * HEADROOM / duration / 1000
    audio = ((128, 2) if total_kbps > 1000 else (64, 2) if total_kbps > 300 else (32, 1)
             ) if has_audio else (0, 0)
    video_kbps = int(total_kbps - audio[0])
    out = ctx.workdir / "out.mp4"
    for attempt in range(ENCODE_TRIES):
        if video_kbps < MIN_VIDEO_KBPS:
            smallest = (MIN_VIDEO_KBPS + audio[0]) * 1000 * duration / 8 / HEADROOM / 1e6
            raise ConversionError(f"too small for a {duration:.0f} s video (the least that "
                                  f"still looks like video is ~{smallest:.1f} MB)")
        _encode_video(ctx, src, out, video_kbps, audio, duration, attempt)
        size = out.stat().st_size
        if size <= target:
            return out
        video_kbps = int(video_kbps * target / size * 0.95)  # overshot: aim lower and redo
    raise ConversionError(f"couldn't get this video under {target / 1e6:g} MB")


def _encode_video(ctx: Context, src: Path, out: Path, video_kbps: int,
                  audio: tuple[int, int], duration: float, attempt: int) -> None:
    height = next((h for kbps, h in VIDEO_HEIGHTS if video_kbps < kbps), None)
    scale = ["-vf", f"scale=-2:'min({height},ih)'"] if height else media.EVEN_SIZE
    fps = ["-fpsmax", "24"] if video_kbps < LOW_FPS_KBPS else []
    passlog = ctx.workdir / "pass"
    common = ["-i", src, "-map", "0:v:0", *scale, *fps, "-c:v", "libx264", "-preset", "medium",
              "-b:v", f"{video_kbps}k", "-pix_fmt", "yuv420p", "-passlogfile", passlog]
    # Each pass of each try reports 0-1; the batch should see one steady climb.
    span = 1 / ENCODE_TRIES

    def part(start: float) -> Context:
        return replace(ctx, on_progress=lambda frac: ctx.progress(
            span * attempt + span * (start + frac / 2)))

    media._ffmpeg(part(0), [*common, "-pass", "1", "-an", "-f", "null", "-"], duration)
    kbps, channels = audio
    sound = (["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{kbps}k", "-ac", str(channels)]
             if kbps else [])
    media._ffmpeg(part(0.5), [*common, "-pass", "2", *sound, "-movflags", "+faststart", out],
                  duration)


def _audio(ctx: Context, src: Path, target: int, other: bool) -> Path:
    info = media.probe(ctx, src)
    duration = media._duration(info)
    if duration <= 0:
        raise ConversionError("can't tell how long this recording is")
    kbps = int(target * 8 * HEADROOM / duration / 1000)
    ext = f.ext_of(src)
    out_ext = ext if ext in LOSSY_AUDIO else "m4a"
    floor = MIN_AUDIO_KBPS
    if other and kbps < MIN_AUDIO_KBPS:
        out_ext, floor = "opus", MIN_OPUS_KBPS
    if kbps < floor:
        hint = "" if other else "; Allow a smaller format reaches 12 kb/s with Opus"
        raise ConversionError(f"too small for {duration:.0f} s of audio ({floor} kb/s, about "
                              f"{floor * 1000 * duration / 8 / 1e6:.1f} MB, is the least that's "
                              f"still listenable{hint})")
    codec = LOSSY_AUDIO[out_ext][0]
    out = ctx.workdir / f"out.{out_ext}"
    for _attempt in range(ENCODE_TRIES):
        mono = ["-ac", "1"] if kbps < MONO_BELOW_KBPS else []
        media._ffmpeg(ctx, ["-i", src, "-map", "0:a:0", "-map_metadata", "0", "-c:a", codec,
                            "-b:a", f"{min(kbps, 320)}k", *mono, out], duration)
        size = out.stat().st_size
        if size <= target:
            return out
        kbps = int(kbps * target / size * 0.95)  # a variable-rate codec overshot
        if kbps < floor:
            break
    raise ConversionError(f"couldn't get this recording under {target / 1e6:g} MB")


def _pdf(ctx: Context, src: Path, target: int) -> Path:
    """A lossless repack if that's enough, else the best-looking Ghostscript setting that fits:
    the presets, then lower picture resolutions still."""
    repacked = ctx.workdir / "repacked.pdf"
    ctx.run(["qpdf", "--recompress-flate", "--compression-level=9",
             "--object-streams=generate", src, repacked])
    smallest = repacked.stat().st_size
    if smallest <= target:
        return repacked
    steps = [[f"-dPDFSETTINGS={preset}"] for preset in PDF_PRESETS]
    steps += [["-dPDFSETTINGS=/screen", "-dDownsampleColorImages=true",
               "-dDownsampleGrayImages=true", "-dDownsampleMonoImages=true",
               f"-dColorImageResolution={dpi}", f"-dGrayImageResolution={dpi}",
               f"-dMonoImageResolution={dpi * 4}", "-dColorImageDownsampleThreshold=1.0",
               "-dGrayImageDownsampleThreshold=1.0"] for dpi in PDF_LOW_DPI]
    for i, settings in enumerate(steps):
        out = ctx.workdir / f"out{i}.pdf"
        ctx.run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                 "-dCompatibilityLevel=1.6", *settings, f"-sOutputFile={out}", repacked])
        ctx.progress((i + 1) / len(steps))
        size = out.stat().st_size
        if size <= target:
            return out
        smallest = min(smallest, size)
    raise ConversionError(f"the smallest this PDF gets is {smallest / 1e6:.1f} MB")


TOOL = Tool(
    "compress", "Compress", done="Compressed",
    sources=frozenset({*f.IMAGE_IN - {"svg", "gif"}, *f.VIDEO_IN, *f.AUDIO_IN, "pdf"}),
    engine=compress, suffix=lambda p: "compressed", params=(SIZE, OTHER_FORMAT),
    icon="archive-insert",
    help="make each file fit a size, e.g. for email attachments",
)
