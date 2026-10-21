"""Compress to a target size: images by quality (then size), video and audio by bitrate,
PDFs with ghostscript's presets."""

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
MIN_VIDEO_KBPS = 60
PDF_PRESETS = ("/printer", "/ebook", "/screen")  # 300, 150 and 72 dpi images


def _half_size_mb(files: list[Path]) -> float:
    largest = max(p.stat().st_size for p in files)
    return max(0.1, round(largest / 2 / 1e6, 1))


SIZE = Param("size", "Compress to", "float", 10.0, "target size of each file", unit="MB",
             suggest=_half_size_mb)


def compress(ctx: Context, inputs: list[Path], params: Params) -> Path:
    src = inputs[0]
    target = int(params["size"] * 1e6)
    if target <= 0:
        raise ConversionError("the target size has to be above 0 MB")
    if src.stat().st_size <= target:
        raise ConversionError(f"{src.name} is already under {params['size']:g} MB")
    ext = f.ext_of(src)
    if ext in f.IMAGE_IN:
        return _image(ctx, src, target)
    if ext in f.VIDEO_IN:
        return _video(ctx, src, target)
    if ext in f.AUDIO_IN:
        return _audio(ctx, src, target)
    return _pdf(ctx, src, target)


def _image(ctx: Context, src: Path, target: int) -> Path:
    """Highest quality that fits; if even quality 10 doesn't, scale down and search again."""
    ext = f.ext_of(src)
    out_ext = ext if ext in LOSSY_IMAGES else "jpg"  # PNG/TIFF/BMP can't trade quality
    images.check_input(ctx, src)
    staged = images._normalize(ctx, src, flatten=out_ext == "jpg")
    out = ctx.workdir / f"out.{out_ext}"
    scale = 1.0
    for _attempt in range(6):
        source = staged
        if scale < 1.0:
            source = ctx.workdir / "scaled.v"
            ctx.run(["vips", "resize", staged, source, f"{scale:.4f}"])

        def size_at(quality: int, source: Path = source) -> int:
            ctx.run(["vips", "copy", source, f"{out}[Q={quality},strip=false]"])
            return out.stat().st_size

        lo, hi, best = 10, 95, None
        while lo <= hi:
            mid = (lo + hi) // 2
            if size_at(mid) <= target:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        if best is not None:
            size_at(best)
            return out
        # Too big even at quality 10: shrink by the area ratio, plus a margin.
        scale *= math.sqrt(target / out.stat().st_size) * 0.9
        ctx.progress(min(0.9, 0.2 * (_attempt + 1)))
    raise ConversionError("couldn't get this image that small")


def _video(ctx: Context, src: Path, target: int) -> Path:
    info = media.probe(ctx, src)
    duration = media._duration(info)
    if duration <= 0:
        raise ConversionError("can't tell how long this video is")
    has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
    total_kbps = target * 8 * HEADROOM / duration / 1000
    audio_kbps = (128 if total_kbps > 1000 else 64) if has_audio else 0
    video_kbps = int(total_kbps - audio_kbps)
    if video_kbps < MIN_VIDEO_KBPS:
        smallest = (MIN_VIDEO_KBPS + audio_kbps) * 1000 * duration / 8 / HEADROOM / 1e6
        raise ConversionError(f"too small for a {duration:.0f} s video "
                              f"(the least that still looks like video is ~{smallest:.1f} MB)")
    # Fewer bits need fewer pixels: below ~1.5 Mb/s, 720p looks better than a blocky 1080p.
    height = 480 if video_kbps < 600 else 720 if video_kbps < 1500 else None
    scale = ["-vf", f"scale=-2:'min({height},ih)'"] if height else media.EVEN_SIZE
    out = ctx.workdir / "out.mp4"
    passlog = ctx.workdir / "pass"
    common = ["-i", src, "-map", "0:v:0", *scale, "-c:v", "libx264", "-preset", "medium",
              "-b:v", f"{video_kbps}k", "-pix_fmt", "yuv420p", "-passlogfile", passlog]
    # Each pass reports 0-1; the batch should see 0-0.5 then 0.5-1.
    first = replace(ctx, on_progress=lambda frac: ctx.progress(frac / 2))
    media._ffmpeg(first, [*common, "-pass", "1", "-an", "-f", "null", "-"], duration)
    second = replace(ctx, on_progress=lambda frac: ctx.progress(0.5 + frac / 2))
    audio = ["-map", "0:a:0", "-c:a", "aac", "-b:a", f"{audio_kbps}k"] if has_audio else []
    media._ffmpeg(second, [*common, "-pass", "2", *audio, "-movflags", "+faststart", out],
                  duration)
    return out


def _audio(ctx: Context, src: Path, target: int) -> Path:
    info = media.probe(ctx, src)
    duration = media._duration(info)
    if duration <= 0:
        raise ConversionError("can't tell how long this recording is")
    kbps = int(target * 8 * HEADROOM / duration / 1000)
    if kbps < 24:
        raise ConversionError(f"too small for {duration:.0f} s of audio "
                              f"(24 kb/s, about {24000 * duration / 8 / 1e6:.1f} MB, "
                              "is the least that's still listenable)")
    ext = f.ext_of(src)
    out_ext = ext if ext in LOSSY_AUDIO else "m4a"
    codec = LOSSY_AUDIO[out_ext][0]
    out = ctx.workdir / f"out.{out_ext}"
    media._ffmpeg(ctx, ["-i", src, "-map", "0:a:0", "-map_metadata", "0", "-c:a", codec,
                        "-b:a", f"{min(kbps, 320)}k", out], duration)
    return out


def _pdf(ctx: Context, src: Path, target: int) -> Path:
    """The best-looking preset that fits."""
    smallest = None
    for i, preset in enumerate(PDF_PRESETS):
        out = ctx.workdir / f"out{i}.pdf"
        ctx.run(["gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
                 "-dCompatibilityLevel=1.6", f"-dPDFSETTINGS={preset}",
                 f"-sOutputFile={out}", src])
        ctx.progress((i + 1) / len(PDF_PRESETS))
        size = out.stat().st_size
        if size <= target:
            return out
        smallest = size if smallest is None else min(smallest, size)
    raise ConversionError(f"the smallest this PDF gets is {(smallest or 0) / 1e6:.1f} MB")


TOOL = Tool(
    "compress", "Compress", done="Compressed",
    sources=frozenset({*f.IMAGE_IN - {"svg", "gif"}, *f.VIDEO_IN, *f.AUDIO_IN, "pdf"}),
    engine=compress, suffix=lambda p: "compressed", params=(SIZE,), icon="archive-insert",
    help="make each file fit a size, e.g. for email attachments",
)
