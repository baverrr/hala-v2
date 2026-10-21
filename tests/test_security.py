"""Security review (2026-10-21): hostile inputs mustn't read other files, run scripts or reach
the network."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from hala.convert import convert_file
from hala.engines import documents
from hala.engines.images import check_picture
from hala.runner import LOCAL_ONLY, ConversionError, local_inputs
from hala.servicemenus import exec_arg

MARKER = "hala-test-marker-4f1c"


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    """A file outside the document's folder that a hostile document tries to pull in."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    path = elsewhere / "secret.txt"
    path.write_text(MARKER + "\n")
    return path


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    return folder


def _text_of(path: Path) -> str:
    if path.suffix in (".docx", ".epub", ".odt"):
        with zipfile.ZipFile(path) as z:
            return "\n".join(z.read(n).decode(errors="replace") for n in z.namelist())
    return path.read_text(errors="replace")


@pytest.mark.parametrize(("name", "body"), [
    ("a.rst", "Hi\n\n.. include:: {secret}\n"),
    ("a.org", "Hi\n\n#+INCLUDE: \"{secret}\"\n"),
    ("a.tex", "Hi \\input{{{secret}}}\n"),
])
def test_include_directives_cant_read_files(docs: Path, secret: Path, name: str,
                                            body: str) -> None:
    src = docs / name
    src.write_text(body.format(secret=secret))
    assert MARKER not in convert_file(src, "txt").read_text()


@pytest.mark.parametrize("dst", ["docx", "epub"])
def test_only_pictures_beside_the_document_are_embedded(docs: Path, secret: Path,
                                                        samples: dict[str, Path],
                                                        dst: str) -> None:
    (docs / "img").mkdir()
    (docs / "img" / "ok.png").write_bytes(samples["png"].read_bytes())
    outside = secret.with_name("photo.png")
    outside.write_bytes(samples["png"].read_bytes())
    marker = outside.read_bytes()
    src = docs / "a.md"
    src.write_text(f"![ok](img/ok.png)\n\n![abs]({outside})\n\n![up](../elsewhere/photo.png)\n\n"
                   f"![url](http://127.0.0.1:9/x.png)\n\n"
                   f'<video src="{secret}"></video>\n')
    result = convert_file(src, dst)
    with zipfile.ZipFile(result) as z:
        media = [n for n in z.namelist() if n.lower().endswith(".png")]
        assert len(media) == 1  # just img/ok.png (the same bytes as photo.png)
        assert z.read(media[0]) == marker
        assert all(MARKER.encode() not in z.read(n) for n in z.namelist())


def test_docx_pictures_survive_the_sandbox(docs: Path, samples: dict[str, Path]) -> None:
    (docs / "p.png").write_bytes(samples["png"].read_bytes())
    src = docs / "a.md"
    src.write_text("# T\n\n![p](p.png)\n")
    docx = convert_file(src, "docx")
    epub = convert_file(docx, "epub")
    with zipfile.ZipFile(epub) as z:
        assert any(n.lower().endswith(".png") for n in z.namelist())


def test_epub_metadata_cant_name_files(docs: Path, secret: Path) -> None:
    src = docs / "a.md"
    src.write_text(f"---\ntitle: T\ncover-image: {secret}\ncss: {secret}\n---\n\nHi\n")
    assert MARKER not in _text_of(convert_file(src, "epub"))


def test_lock_down_profile_keeps_other_settings(tmp_path: Path) -> None:
    xcu = tmp_path / "user" / "registrymodifications.xcu"
    documents.lock_down_profile(tmp_path)
    xcu.write_text(xcu.read_text().replace(
        "</oor:items>", '<item oor:path="/x/y"><prop oor:name="Z" oor:op="fuse">'
        "<value>1</value></prop></item>\n</oor:items>"))
    documents.lock_down_profile(tmp_path)
    text = xcu.read_text()
    assert text.count('oor:name="DisableMacrosExecution"') == 1
    assert '<prop oor:name="Z"' in text
    assert text.rstrip().endswith("</oor:items>")


def test_disguised_scripts_never_reach_imagemagick(tmp_path: Path) -> None:
    ps = tmp_path / "photo.png"
    ps.write_text("%!PS\n/Helvetica findfont 20 scalefont setfont 10 10 moveto (x) show\n")
    with pytest.raises(ConversionError, match="isn't a picture"):
        convert_file(ps, "jpg")
    mvg = tmp_path / "photo.jpg"
    mvg.write_text('push graphic-context\nimage over 0,0 0,0 "text:/etc/hostname"\n')
    with pytest.raises(ConversionError, match="isn't a picture"):
        check_picture(mvg)


def test_mislabelled_pictures_still_convert(tmp_path: Path, samples: dict[str, Path]) -> None:
    png_called_jpg = tmp_path / "really-png.jpg"
    png_called_jpg.write_bytes(samples["png"].read_bytes())
    assert convert_file(png_called_jpg, "webp").stat().st_size > 0
    for key in ("svg", "gif", "webp", "heic", "avif", "tiff", "bmp"):
        if key in samples:
            check_picture(samples[key])


def test_ffmpeg_inputs_are_local_only() -> None:
    cmd = local_inputs(["ffmpeg", "-y", "-i", "a.mp4", "-f", "lavfi", "-i", "sine", "o.mp4"])
    assert cmd == ["ffmpeg", "-y", *LOCAL_ONLY, "-i", "a.mp4", "-f", "lavfi",
                   *LOCAL_ONLY, "-i", "sine", "o.mp4"]
    assert local_inputs(["ffprobe", "x.mp4"]) == ["ffprobe", *LOCAL_ONLY, "x.mp4"]
    assert local_inputs(["vips", "copy", "-i"]) == ["vips", "copy", "-i"]


def test_ffmpeg_refuses_urls_inside_concat_lists(tmp_path: Path) -> None:
    # ffmpeg only applies the whitelist to what it opens, so prove it's in effect end to end.
    from hala.runner import Context

    listing = tmp_path / "list.txt"
    listing.write_text("ffconcat version 1.0\nfile 'http://127.0.0.1:9/a.ts'\n")
    with pytest.raises(ConversionError, match="not on whitelist"):
        Context(tmp_path).run(["ffmpeg", "-v", "error", "-f", "concat", "-safe", "0", "-i",
                               listing, "-f", "null", "-"])


def test_exec_lines_are_quoted_per_the_spec() -> None:
    assert exec_arg("/usr/bin/hala") == "/usr/bin/hala"
    assert exec_arg('/a b/$x"`\\%') == '/a b/\\\\$x\\\\"\\\\`\\\\\\\\%%'
