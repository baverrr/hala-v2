"""End-to-end conversions on real sample files, checked with independent tools."""

from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path

import pytest

from hala import convert
from hala import formats as f
from hala.convert import convert_file
from hala.runner import Cancelled, ConversionError


def out(*cmd: str | Path) -> str:
    return subprocess.run([str(c) for c in cmd], check=True, capture_output=True,
                          text=True).stdout


def streams(path: Path) -> list[dict]:
    info = json.loads(out("ffprobe", "-v", "error", "-print_format", "json", "-show_streams", path))
    return info["streams"]


def vips_field(path: Path, field: str) -> str:
    return out("vipsheader", "-f", field, path).strip()


def pdf_pages(path: Path) -> int:
    for line in out("pdfinfo", path).splitlines():
        if line.startswith("Pages:"):
            return int(line.split()[1])
    raise AssertionError("no page count")


IMAGE_SOURCES = ["png", "jpg", "webp", "heic", "tiff", "bmp", "avif", "svg", "gif"]


@pytest.mark.parametrize("dst", [*f.IMAGE_OUT, "pdf"])
@pytest.mark.parametrize("src", IMAGE_SOURCES)
def test_image_matrix(workdir: dict[str, Path], src: str, dst: str) -> None:
    if src == dst:
        pytest.skip("same format")
    result = convert_file(workdir[src], dst)
    assert result.parent == workdir[src].parent
    assert result.suffix == f".{dst}"
    if dst == "pdf":
        assert pdf_pages(result) == 1
        return
    if dst == "mp4":
        assert streams(result)[0]["codec_name"] == "h264"
        return
    assert int(vips_field(result, "width")) > 0
    if dst == "jpg":
        assert vips_field(result, "bands") == "3"  # alpha flattened


def test_animation_keeps_frames(workdir: dict[str, Path]) -> None:
    webp = convert_file(workdir["gif"], "webp")
    assert int(vips_field(webp, "n-pages")) == int(vips_field(workdir["gif"], "n-pages")) > 1


def test_image_to_pdf_is_valid_and_sized(workdir: dict[str, Path]) -> None:
    result = convert_file(workdir["jpg"], "pdf")
    assert re.search(r"Page size:\s+30.72 x 23.04 pts", out("pdfinfo", result))  # 64x48 @150dpi
    png = convert_file(result, "png")
    assert vips_field(png, "width") == "64"


@pytest.mark.parametrize("dst", f.VIDEO_OUT)
def test_video_formats(workdir: dict[str, Path], dst: str) -> None:
    if dst == "mp4":
        pytest.skip("same format")
    result = convert_file(workdir["mp4"], dst)
    kinds = {s["codec_type"] for s in streams(result)}
    assert "video" in kinds
    if dst != "gif":
        assert "audio" in kinds


def test_video_remux_keeps_codec(workdir: dict[str, Path]) -> None:
    result = convert_file(workdir["mp4"], "mkv")
    assert {s["codec_name"] for s in streams(result)} == {"h264", "aac"}


@pytest.mark.parametrize("dst", ["mp3", "flac", "opus"])
def test_video_to_audio(workdir: dict[str, Path], dst: str) -> None:
    result = convert_file(workdir["mp4"], dst)
    assert [s["codec_type"] for s in streams(result)] == ["audio"]


def test_silent_video_to_audio_explains(workdir: dict[str, Path]) -> None:
    with pytest.raises(ConversionError, match="no audio track"):
        convert_file(workdir["silent"], "mp3")
    assert not list(workdir["silent"].parent.glob(".hala-*"))


@pytest.mark.parametrize("dst", [x for x in f.AUDIO_OUT if x != "wav"])
def test_audio_formats(workdir: dict[str, Path], dst: str) -> None:
    result = convert_file(workdir["wav"], dst)
    (stream,) = streams(result)
    assert stream["codec_type"] == "audio"
    assert 0.9 < float(stream.get("duration", "1")) < 1.2


@pytest.mark.parametrize("dst", ["vtt", "ass"])
def test_subtitles(workdir: dict[str, Path], dst: str) -> None:
    text = convert_file(workdir["srt"], dst).read_text()
    assert "Hello" in text and "World" in text


def test_names_never_clobber(workdir: dict[str, Path]) -> None:
    sample_jpg = workdir["jpg"].read_bytes()
    first = convert_file(workdir["png"], "jpg")
    second = convert_file(workdir["png"], "jpg")
    assert first.name == "pic (2).jpg" and second.name == "pic (3).jpg"
    assert workdir["jpg"].read_bytes() == sample_jpg  # the existing pic.jpg was not touched
    assert not list(first.parent.glob(".hala-*"))


def test_outdir(workdir: dict[str, Path], tmp_path: Path) -> None:
    result = convert_file(workdir["png"], "webp", outdir=tmp_path / "exports")
    assert result.parent == tmp_path / "exports"


def test_unsupported_pair_lists_options(workdir: dict[str, Path]) -> None:
    with pytest.raises(ConversionError, match=r"it can become: .*mp3"):
        convert_file(workdir["wav"], "docx")


def test_cancel_stops_ffmpeg(tmp_path: Path) -> None:
    long = tmp_path / "long.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=1280x720:rate=30:duration=120", "-c:v", "libx264",
                    "-preset", "ultrafast", str(long)], check=True)
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        convert_file(long, "webm", cancel_event=cancel)
    assert time.monotonic() - started < 10
    assert sorted(p.name for p in tmp_path.iterdir()) == ["long.mp4"]


# --- archives -------------------------------------------------------------------------------

def test_extract_single_top_level_folder(workdir: dict[str, Path]) -> None:
    result = convert_file(workdir["zip"], "folder")
    assert result.name == "bundle" and result.is_dir()
    assert (result / "sub" / "b.txt").read_text() == "b"


@pytest.mark.parametrize("dst", ["tar.gz", "7z"])
def test_repack(workdir: dict[str, Path], dst: str) -> None:
    result = convert_file(workdir["zip"], dst)
    assert result.name == f"bundle.{dst}"
    back = convert_file(result, "folder", outdir=result.parent / "back")
    assert (back / "a.txt").read_text() == "a"


def test_pack_file_and_folder(workdir: dict[str, Path]) -> None:
    zipped = convert_file(workdir["txt"], "zip")
    assert "plain.txt" in out("7z", "l", zipped)
    folder = convert_file(workdir["zip"], "folder")
    seven = convert_file(folder, "7z")
    assert seven.name == "bundle.7z"


def test_unsafe_tar_refused(tmp_path: Path) -> None:
    evil = tmp_path / "evil.tar"
    with tarfile.open(evil, "w") as tar:
        info = tarfile.TarInfo("../escaped.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ConversionError, match="unsafe"):
        convert_file(evil, "folder")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["evil.tar"]  # no output, no scratch


# --- documents (LibreOffice / pandoc) -------------------------------------------------------

@pytest.mark.office
def test_docx_to_pdf_to_text(workdir: dict[str, Path]) -> None:
    pdf = convert_file(workdir["docx"], "pdf")
    assert pdf_pages(pdf) == 1
    assert "Title" in convert_file(pdf, "txt").read_text()


@pytest.mark.office
def test_txt_to_docx_keeps_unicode(workdir: dict[str, Path]) -> None:
    docx = convert_file(workdir["txt"], "docx")
    assert "ünïcödé" in convert_file(docx, "md").read_text()


@pytest.mark.office
def test_csv_roundtrip(workdir: dict[str, Path]) -> None:
    xlsx = convert_file(workdir["csv"], "xlsx")
    back = convert_file(xlsx, "csv", outdir=xlsx.parent / "back")
    assert back.read_text().splitlines() == ["name,count", "apples,3", "pears,5"]


@pytest.mark.office
def test_markdown_to_pdf_via_docx(workdir: dict[str, Path]) -> None:
    assert pdf_pages(convert_file(workdir["md"], "pdf")) >= 1


@pytest.mark.parametrize("dst", ["html", "epub", "docx", "txt"])
def test_markdown_pandoc(workdir: dict[str, Path], dst: str) -> None:
    result = convert_file(workdir["md"], dst)
    assert result.stat().st_size > 0
    if dst == "epub":
        back = convert_file(result, "md", outdir=result.parent / "back")
        assert "Title" in back.read_text()


# --- review fixes (2026-10-20) --------------------------------------------------------------

def test_dotted_folder_keeps_its_name(tmp_path: Path) -> None:
    folder = tmp_path / "project-v1.2"
    folder.mkdir()
    (folder / "a.txt").write_text("a")
    assert convert_file(folder, "zip").name == "project-v1.2.zip"


def test_odd_names_are_packed_literally(tmp_path: Path) -> None:
    dash = tmp_path / "-rf"
    dash.mkdir()
    (dash / "a.txt").write_text("a")
    assert "-rf/a.txt" in out("7z", "l", "-ba", convert_file(dash, "7z"))
    (tmp_path / "what?.txt").write_text("q")
    (tmp_path / "whatX.txt").write_text("x")  # would match "what?" as a wildcard
    listing = out("7z", "l", "-ba", convert_file(tmp_path / "what?.txt", "zip"))
    assert "what?.txt" in listing and "whatX.txt" not in listing


def _evil_link_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    (tree / "evil").mkdir(parents=True)
    (tree / "evil" / "etc").symlink_to("/etc")
    return tree


def test_unsafe_link_in_tar_zst_refused(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.zst"
    subprocess.run(["tar", "--zstd", "-cf", archive, "-C", _evil_link_tree(tmp_path), "evil"],
                   check=True)
    with pytest.raises(ConversionError, match="unsafe link"):
        convert_file(archive, "folder")


def test_unsafe_link_in_zip_refused(tmp_path: Path) -> None:
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as z:
        info = zipfile.ZipInfo("evil/up")
        info.create_system = 3  # Unix, so the mode bits below count
        info.external_attr = 0o120777 << 16
        z.writestr(info, "../../../../etc")
    # 7-Zip 26 refuses this itself; _check_links backs it up for older 7z builds.
    with pytest.raises(ConversionError, match=r"unsafe link|Dangerous link"):
        convert_file(archive, "folder")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["evil.zip"]


def test_single_gz(tmp_path: Path) -> None:
    import gzip

    src = tmp_path / "notes.txt.gz"
    src.write_bytes(gzip.compress(b"hello"))
    assert convert_file(src, "folder").read_bytes() == b"hello"


def test_extracting_twice_does_not_merge(workdir: dict[str, Path]) -> None:
    first = convert_file(workdir["zip"], "folder")
    (first / "mine.txt").write_text("user edit")
    second = convert_file(workdir["zip"], "folder")
    assert (first.name, second.name) == ("bundle", "bundle (2)")
    assert not (second / "mine.txt").exists()


def test_no_hardlink_filesystem(workdir: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    def no_links(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(convert.os, "link", no_links)
    before = workdir["jpg"].read_bytes()
    assert convert_file(workdir["png"], "jpg").name == "pic (2).jpg"
    assert workdir["jpg"].read_bytes() == before


def test_cancel_after_engine_places_nothing(workdir: dict[str, Path]) -> None:
    cancel = threading.Event()
    with pytest.raises(Cancelled):
        convert_file(workdir["png"], "gif", cancel_event=cancel,
                     on_progress=lambda frac: cancel.set() if frac >= 0.5 else None)
    assert not (workdir["png"].parent / "pic.gif").exists()


def test_read_only_scratch_is_cleaned(tmp_path: Path) -> None:
    locked = tmp_path / ".hala-x" / "ro"
    locked.mkdir(parents=True)
    (locked / "f").write_text("x")
    locked.chmod(0o555)
    convert._remove_tree(tmp_path / ".hala-x")
    assert not (tmp_path / ".hala-x").exists()


def test_stale_scratch_is_swept(workdir: dict[str, Path]) -> None:
    import os

    stale = workdir["png"].parent / ".hala-stale"
    stale.mkdir()
    old = time.time() - 2 * convert.STALE_AFTER_S
    os.utime(stale, (old, old))
    convert_file(workdir["png"], "gif")
    assert not stale.exists()


def test_odd_sized_video_transcodes(tmp_path: Path) -> None:
    src = tmp_path / "odd.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=161x121:rate=10:duration=1", "-c:v", "ffv1", str(src)],
                   check=True)
    video = next(s for s in streams(convert_file(src, "mp4")) if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (160, 120)


def test_cmyk_becomes_srgb(tmp_path: Path) -> None:
    rgb = tmp_path / "rgb.v"
    out("vips", "black", tmp_path / "b.v", "8", "8", "--bands", "3")
    out("vips", "linear", tmp_path / "b.v", rgb, "1 1 1", "200 40 40", "--uchar")
    cmyk = tmp_path / "cmyk.jpg"
    out("vips", "colourspace", rgb, cmyk, "cmyk")
    png = convert_file(cmyk, "png")
    assert vips_field(png, "bands") == "3"  # not RGBA with K as alpha
    red = [float(v) for v in out("vips", "getpoint", png, "0", "0").split()]
    assert red[0] > 150 and red[1] < 100


def test_16bit_alpha_flattens_to_white(tmp_path: Path) -> None:
    out("vips", "black", tmp_path / "b.v", "8", "8", "--bands", "3")
    out("vips", "colourspace", tmp_path / "b.v", tmp_path / "b16.v", "rgb16")
    src = tmp_path / "clear16.png"
    out("vips", "bandjoin_const", tmp_path / "b16.v", src, "0")  # fully transparent
    jpg = convert_file(src, "jpg")
    assert [float(v) for v in out("vips", "getpoint", jpg, "0", "0").split()] == [255.0] * 3


@pytest.mark.office
def test_markdown_relative_images(tmp_path: Path) -> None:
    (tmp_path / "img").mkdir()
    out("vips", "black", tmp_path / "img" / "x.png", "4", "4")
    md = tmp_path / "doc.md"
    md.write_text("# Pic\n\n![](img/x.png)\n")
    with zipfile.ZipFile(convert_file(md, "docx")) as z:
        assert any(n.startswith("word/media/") for n in z.namelist())


def test_still_image_to_mp4(workdir: dict[str, Path]) -> None:
    from hala.engines.images import STILL_SECONDS

    result = convert_file(workdir["png"], "mp4")
    video = next(s for s in streams(result) if s["codec_type"] == "video")
    assert video["codec_name"] == "h264" and (video["width"], video["height"]) == (64, 48)
    duration = float(out("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                         "csv=p=0", result))
    assert duration == pytest.approx(STILL_SECONDS, abs=0.1)


@pytest.mark.parametrize("src", ["gif", "webp"])
def test_animation_to_mp4_keeps_frames(workdir: dict[str, Path], src: str) -> None:
    anim = workdir["gif"]
    if src == "webp":
        anim = workdir["gif"].with_name("anim.webp")
        out("vips", "copy", f"{workdir['gif']}[n=-1]", anim)
    video = next(s for s in streams(convert_file(anim, "mp4")) if s["codec_type"] == "video")
    assert int(video["nb_frames"]) == 5
