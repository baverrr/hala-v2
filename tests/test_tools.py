"""The phase-4 tools, on small generated files, checked with independent tools."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from hala.cli import main
from hala.runner import ConversionError
from hala.tools import format_time, parse_time, run_tool, tool, tools_for
from hala.tools.metadata import current
from hala.tools.pdftools import parse_ranges


def sh(*cmd: str | Path) -> str:
    return subprocess.run([str(c) for c in cmd], check=True, capture_output=True,
                          text=True).stdout


def duration(path: Path) -> float:
    return float(sh("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                    "csv=p=0", path))


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("media")
    s = {"clip": d / "clip.mp4", "quiet": d / "quiet.mp4", "song": d / "song.mp3",
         "photo": d / "photo.jpg", "qr": d / "qr.png", "doc": d / "doc.pdf"}
    sh("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=6",
       "-f", "lavfi", "-i", "sine=frequency=300:duration=6,volume=0.05", "-c:v", "libx264",
       "-preset", "ultrafast", "-c:a", "aac", "-shortest", "-metadata", "title=Clip",
       "-metadata", "location=+21.3-157.8/", s["clip"])
    sh("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=25:duration=2",
       "-c:v", "libx264", "-preset", "ultrafast", s["quiet"])
    sh("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo:d=1", "-f",
       "lavfi", "-i", "sine=frequency=440:duration=3", "-filter_complex",
       "[0][1][0]concat=n=3:v=0:a=1,volume=0.1", "-c:a", "libmp3lame", "-q:a", "4", s["song"])
    sh("vips", "gaussnoise", d / "n.v", "800", "600")
    sh("vips", "colourspace", d / "n.v", d / "rgb.v", "srgb")
    sh("vips", "copy", d / "rgb.v", f"{s['photo']}[Q=97]")
    sh("exiftool", "-q", "-overwrite_original", "-GPSLatitude=21.3", "-GPSLatitudeRef=N",
       "-GPSLongitude=157.8", "-GPSLongitudeRef=W", "-Title=Beach", "-Orientation#=6",
       s["photo"])
    sh("qrencode", "-o", s["qr"], "-s", "6", "hala test code")
    pages = []
    for i in range(3):  # one-page PDFs from the built-in writer, merged into a 3-page doc
        png = d / f"p{i}.png"
        sh("vips", "black", png, "100", "80")
        assert main(["convert", "-t", "pdf", str(png)]) == 0
        pages.append(d / f"p{i}.pdf")
    sh("qpdf", "--empty", "--pages", *pages, "--", s["doc"])
    return s


@pytest.fixture
def work(tmp_path: Path, media: dict[str, Path]) -> dict[str, Path]:
    copies = {}
    for key, path in media.items():
        copies[key] = tmp_path / path.name
        copies[key].write_bytes(path.read_bytes())
    return copies


# --- framework ------------------------------------------------------------------------------

@pytest.mark.parametrize(("text", "seconds"), [("83.5", 83.5), ("1:23.5", 83.5),
                                               ("1:02:03", 3723.0), ("0:05", 5.0)])
def test_parse_time(text: str, seconds: float) -> None:
    assert parse_time(text) == seconds


def test_parse_time_rejects_junk() -> None:
    with pytest.raises(ValueError):
        parse_time("five")


def test_format_time() -> None:
    assert format_time(83.5) == "1:23.50" and format_time(3723) == "1:02:03.00"


def test_tools_for_selection(tmp_path: Path) -> None:
    names = [t.name for t in tools_for([tmp_path / "a.pdf"])]
    assert "split-pdf" in names and "merge-pdf" not in names  # merge needs two
    assert "merge-pdf" in [t.name for t in tools_for([tmp_path / "a.pdf", tmp_path / "b.png"])]
    assert [t.name for t in tools_for([tmp_path / "a.docx"])] == []


def test_wrong_file_type_is_refused(work: dict[str, Path]) -> None:
    with pytest.raises(ConversionError, match="doesn't work on mp3"):
        run_tool(tool("split-pdf"), [work["song"]])


def test_bad_parameter_is_explained(work: dict[str, Path]) -> None:
    with pytest.raises(ConversionError, match="Start: 'soon'"):
        run_tool(tool("trim"), [work["clip"]], {"start": "soon"})


# --- compress -------------------------------------------------------------------------------

def test_compress_image_fits(work: dict[str, Path]) -> None:
    out = run_tool(tool("compress"), [work["photo"]], {"size": 0.15})
    assert out.name == "photo (compressed).jpg" and out.stat().st_size <= 150_000


def test_compress_video_fits(work: dict[str, Path]) -> None:
    out = run_tool(tool("compress"), [work["clip"]], {"size": 0.2})
    assert out.suffix == ".mp4" and out.stat().st_size <= 200_000


def test_compress_already_small(work: dict[str, Path]) -> None:
    with pytest.raises(ConversionError, match="already under"):
        run_tool(tool("compress"), [work["song"]], {"size": 50})


def test_compress_suggests_half(work: dict[str, Path]) -> None:
    size = tool("compress").resolve([work["photo"]])["size"]
    assert size == pytest.approx(work["photo"].stat().st_size / 2e6, abs=0.06)


# --- metadata -------------------------------------------------------------------------------

def test_strip_photo_keeps_orientation(work: dict[str, Path]) -> None:
    assert current(work["photo"]).get("location")
    out = run_tool(tool("metadata"), [work["photo"]], {"remove": "everything"})
    assert out.name == "photo (no metadata).jpg" and current(out) == {}
    assert sh("exiftool", "-s3", "-n", "-Orientation", out).strip() == "6"


def test_strip_video_location_keeps_title(work: dict[str, Path]) -> None:
    out = run_tool(tool("metadata"), [work["clip"]], {"remove": "location"})
    assert current(out) == {"title": "Clip"}


def test_set_title(work: dict[str, Path]) -> None:
    out = run_tool(tool("metadata"), [work["song"]], {"remove": "nothing", "title": "Aloha"})
    assert current(out)["title"] == "Aloha" and out.name == "song (edited).mp3"


def test_metadata_nothing_to_do(work: dict[str, Path]) -> None:
    with pytest.raises(ConversionError, match="nothing to change"):
        run_tool(tool("metadata"), [work["song"]], {"remove": "nothing"})


# --- PDF and QR -----------------------------------------------------------------------------

def test_merge_pdf_and_image(work: dict[str, Path]) -> None:
    out = run_tool(tool("merge-pdf"), [work["doc"], work["qr"]])
    assert out.name == "doc (merged).pdf" and sh("qpdf", "--show-npages", out).strip() == "4"


@pytest.mark.parametrize(("pages", "names"), [
    ("", ["doc p1.pdf", "doc p2.pdf", "doc p3.pdf"]),
    ("1-2, 3-", ["doc p1-2.pdf", "doc p3.pdf"]),
])
def test_split_pdf(work: dict[str, Path], pages: str, names: list[str]) -> None:
    out = run_tool(tool("split-pdf"), [work["doc"]], {"pages": pages})
    assert sorted(p.name for p in out.iterdir()) == names


def test_page_ranges() -> None:
    assert parse_ranges("1-3, 5, 8-", 9) == [(1, 3), (5, 5), (8, 9)]
    with pytest.raises(ConversionError, match="aren't in"):
        parse_ranges("4-12", 9)
    with pytest.raises(ConversionError, match="can't read"):
        parse_ranges("one", 9)


def test_read_qr(work: dict[str, Path]) -> None:
    out = run_tool(tool("read-qr"), [work["qr"]])
    assert out.read_text() == "hala test code\n"


def test_read_qr_nothing(work: dict[str, Path]) -> None:
    with pytest.raises(ConversionError, match="no QR code"):
        run_tool(tool("read-qr"), [work["photo"]])


# --- audio and video ------------------------------------------------------------------------

def loudness(path: Path) -> float:
    err = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a:0",
                          "-af", "loudnorm=print_format=json", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    block = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", err)[-1]  # more log lines follow it
    return float(json.loads(block)["input_i"])


def test_normalize(work: dict[str, Path]) -> None:
    out = run_tool(tool("normalize"), [work["clip"]], {"loudness": -16})
    assert abs(loudness(out) + 16) < 1.0


def test_normalize_needs_sound(work: dict[str, Path]) -> None:
    with pytest.raises(ConversionError, match="no sound"):
        run_tool(tool("normalize"), [work["quiet"]])


def test_trim_precise(work: dict[str, Path]) -> None:
    out = run_tool(tool("trim"), [work["clip"]], {"start": "1", "end": "3.5"})
    assert duration(out) == pytest.approx(2.5, abs=0.1)


def test_trim_silence(work: dict[str, Path]) -> None:
    out = run_tool(tool("trim"), [work["song"]], {"silence": True})
    assert duration(out) == pytest.approx(3.0, abs=0.4)  # 1 s of silence each side gone


def test_trim_backwards(work: dict[str, Path]) -> None:
    with pytest.raises(ConversionError, match="has to be before"):
        run_tool(tool("trim"), [work["clip"]], {"start": "4", "end": "2"})


def test_split_video_into_exact_parts(work: dict[str, Path]) -> None:
    out = run_tool(tool("split"), [work["clip"]], {"parts": 3})
    parts = sorted(out.iterdir())
    assert [p.name for p in parts] == [f"clip part {i}.mp4" for i in (1, 2, 3)]
    assert all(duration(p) == pytest.approx(2.0, abs=0.15) for p in parts)


def test_join_lossless_and_reencoded(work: dict[str, Path]) -> None:
    same = run_tool(tool("join"), [work["clip"], work["clip"]])
    assert duration(same) == pytest.approx(12, abs=0.2)
    mixed = run_tool(tool("join"), [work["clip"], work["quiet"]])  # second has no sound
    assert duration(mixed) == pytest.approx(8, abs=0.3)
    kinds = sh("ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of",
               "csv=p=0", mixed).split()
    assert kinds == ["video", "audio"]


def test_join_refuses_mixing_audio_and_video(work: dict[str, Path]) -> None:
    with pytest.raises(ConversionError, match="not both"):
        run_tool(tool("join"), [work["clip"], work["song"]])


# --- CLI ------------------------------------------------------------------------------------

def test_cli_tool_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tool"]) == 0
    assert "compress" in capsys.readouterr().out


def test_cli_tool_runs_and_combines(work: dict[str, Path],
                                    capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["tool", "merge-pdf", "--json", str(work["doc"]), str(work["qr"])]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["status"] == "done"
    assert main(["tool", "metadata", "--show", str(work["photo"])]) == 0
    assert "Beach" in capsys.readouterr().out
