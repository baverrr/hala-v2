"""Sample files, generated with the same tools the engines use (nothing binary is checked in)."""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest

# The tests check Hala's English text, whatever language this machine is in; `hala` processes
# they start inherit it. test_i18n switches languages itself.
os.environ["HALA_LANGUAGE"] = "C"

from hala import i18n

i18n.set_language("C")


def sh(*cmd: str | Path) -> None:
    subprocess.run([str(c) for c in cmd], check=True, capture_output=True)


SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="80" height="60">
<rect width="80" height="60" fill="#f80"/><circle cx="40" cy="30" r="20" fill="#fff"/></svg>"""
SRT = "1\n00:00:00,000 --> 00:00:01,000\nHello\n\n2\n00:00:01,000 --> 00:00:02,000\nWorld\n"
MARKDOWN = "# Title\n\nSome *text* and a list:\n\n- one\n- two\n"


@pytest.fixture(autouse=True)
def _own_config(tmp_path_factory: pytest.TempPathFactory,
                monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's own ~/.config/hala/limits.conf out of the tests."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path_factory.mktemp("config")))


@pytest.fixture(scope="session")
def samples(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    d = tmp_path_factory.mktemp("samples")
    s: dict[str, Path] = {}

    def ffmpeg(name: str, *args: str) -> None:
        s[name.rsplit(".", 1)[1]] = d / name
        sh("ffmpeg", "-v", "error", "-y", *args, d / name)

    # Video with sound: 1 s colour bars + tone.
    ffmpeg("clip.mp4", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15:duration=1",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest")
    ffmpeg("silent.mkv", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=15:duration=1",
           "-c:v", "libx264")
    s["silent"] = s.pop("mkv")
    ffmpeg("tone.wav", "-f", "lavfi", "-i", "sine=frequency=440:duration=1")
    ffmpeg("anim.gif", "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=5:duration=1")

    # Still images: one RGBA PNG, then every other format derived from it with vips.
    s["png"] = d / "pic.png"
    sh("vips", "gaussnoise", d / "noise.v", "64", "48")
    sh("vips", "colourspace", d / "noise.v", d / "rgb.v", "srgb")
    sh("vips", "bandjoin_const", d / "rgb.v", s["png"], "255")  # adds an alpha band
    for ext in ("jpg", "webp", "heic", "tiff", "bmp", "avif"):
        s[ext] = d / f"pic.{ext}"
        sh("vips", "copy", s["png"], s[ext])
    s["svg"] = d / "vector.svg"
    s["svg"].write_text(SVG)

    s["srt"] = d / "subs.srt"
    s["srt"].write_text(SRT)
    s["md"] = d / "notes.md"
    s["md"].write_text(MARKDOWN)
    s["txt"] = d / "plain.txt"
    s["txt"].write_text("Plain text, ünïcödé.\nSecond line.\n")
    s["csv"] = d / "table.csv"
    s["csv"].write_text("name,count\napples,3\npears,5\n")
    s["docx"] = d / "letter.docx"
    sh("pandoc", s["md"], "-o", s["docx"])

    s["zip"] = d / "bundle.zip"
    with zipfile.ZipFile(s["zip"], "w") as z:
        z.writestr("bundle/a.txt", "a")
        z.writestr("bundle/sub/b.txt", "b")
    return s


@pytest.fixture
def workdir(tmp_path: Path, samples: dict[str, Path]) -> dict[str, Path]:
    """Per-test copies, so outputs land in a fresh folder next to the input."""
    copies = {}
    for key, path in samples.items():
        copies[key] = tmp_path / path.name
        copies[key].write_bytes(path.read_bytes())
    return copies
