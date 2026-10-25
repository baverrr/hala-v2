"""Workarounds for the README's known limitations (2026-10-21): the wheel without the screen
edge, parallel LibreOffice, direct PDFs, size and zip-bomb guards, folders to a phone, and
notes about pictures a document had to leave out."""

from __future__ import annotations

import gzip
import importlib.util
import io
import json
import os
import re
import subprocess
import tarfile
import threading
import zipfile
from pathlib import Path
from typing import Literal

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QEnterEvent
from PySide6.QtWidgets import QApplication

from hala import limits, servicemenus
from hala.cli import build_parser, main
from hala.convert import convert_file
from hala.engines import documents
from hala.gui import wheelapp
from hala.gui.overlay import Overlay
from hala.runner import Context, ConversionError
from hala.tools import run_tool, tool

_app = QApplication.instance() or QApplication([])
assert isinstance(_app, QApplication)
APP: QApplication = _app
# What documents.pandoc checks (the weasyprint command can be missing while the module is there).
NEEDS_WEASYPRINT = pytest.mark.skipif(importlib.util.find_spec("weasyprint") is None,
                                      reason="WeasyPrint isn't installed")


# --- the wheel without dragging -------------------------------------------------------------


def test_uri_lists_give_existing_local_files(tmp_path: Path) -> None:
    (tmp_path / "a b.png").write_bytes(b"x")
    (tmp_path / "dir").mkdir()
    text = (f"# comment\r\nfile://{tmp_path}/a%20b.png\r\nfile://localhost{tmp_path}/dir\r\n"
            f"https://example.com/x.png\r\nfile://otherhost/etc/passwd\r\n"
            f"file://{tmp_path}/missing.png\r\nfile://{tmp_path}/a%20b.png\r\n")
    assert wheelapp.parse_uri_list(text) == [tmp_path / "a b.png", tmp_path / "dir"]


def test_copied_files_come_from_wl_paste(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "pic.png").write_bytes(b"x")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    fake = bin_ / "wl-paste"
    fake.write_text(f"""#!/bin/sh
case "$1" in --list-types) printf 'text/uri-list\\ntext/plain\\n';;
*) printf 'file://{tmp_path}/pic.png\\r\\n';; esac
""")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_}:{os.environ['PATH']}")
    assert wheelapp.clipboard_files() == [tmp_path / "pic.png"]


def test_click_mode_wheel_opens_where_the_pointer_enters(tmp_path: Path) -> None:
    overlay = Overlay(APP.primaryScreen(), lambda *_: None)
    events: list[str] = []
    overlay.on_place = lambda _o: events.append("placed")
    overlay.on_close = lambda _o: events.append("closed")
    overlay.resize(1600, 1200)  # room for the whole wheel, so it isn't nudged inward
    overlay.begin([tmp_path / "a.png"], click_mode=True)
    assert overlay.wheel is None  # not until the pointer's position is known
    at = QPointF(800, 600)  # mid-screen: far enough from every edge to need no nudge
    overlay.enterEvent(QEnterEvent(at, at, at))
    assert overlay.wheel is not None and (overlay.wheel.cx, overlay.wheel.cy) == (800, 600)
    overlay.finish(None)
    assert events == ["placed", "closed"]


def test_wheel_command_and_menus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    args = build_parser().parse_args(["wheel", "--clipboard"])
    assert args.command == "wheel" and args.clipboard and args.files == []
    menus = servicemenus.build_menus("/usr/bin/hala")
    wheel = menus["hala-wheel.desktop"]
    assert "MimeType=all/all;" in wheel and 'Exec="/usr/bin/hala" wheel %F' in wheel
    assert "X-KDE-Submenu" not in wheel and "X-KDE-Priority=TopLevel" in wheel  # not under Actions
    assert 'wheel --clipboard' in servicemenus.APP_ENTRY

    for name in ("MENU_DIR", "APP_DIR", "ICON_DIR", "SHORTCUT_DIR"):
        monkeypatch.setattr(servicemenus, name, tmp_path / name.lower())
    servicemenus.install("/usr/bin/hala")
    shortcut = tmp_path / "shortcut_dir" / "hala-wheel.desktop"
    text = shortcut.read_text()
    assert f"X-KDE-Shortcuts={servicemenus.DEFAULT_SHORTCUT}" in text
    assert 'Exec="/usr/bin/hala" wheel --clipboard' in text
    servicemenus.uninstall()
    assert not shortcut.exists()


def test_nothing_copied_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop

    told: list[str] = []
    monkeypatch.setattr(wheelapp, "clipboard_files", list)
    monkeypatch.setattr(desktop, "notify", lambda summary, body, *_a, **_k: told.append(body))
    assert wheelapp.main([], from_clipboard=True) == 1
    assert told and "Copy files first" in told[0]


# --- LibreOffice: one profile per worker ----------------------------------------------------


def test_two_conversions_get_two_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documents, "PROFILE", tmp_path / "lo-profile")
    got: list[str] = []
    both_in = threading.Barrier(2, timeout=5)

    def hold() -> None:
        with documents._profile_lock(Context(tmp_path)) as profile:
            got.append(profile.name)
            both_in.wait()  # both hold a profile at the same time

    threads = [threading.Thread(target=hold) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == ["lo-profile", "lo-profile-2"]
    for name in got:  # each profile is locked down, not only the first
        xcu = tmp_path / name / "user" / "registrymodifications.xcu"
        assert "DisableMacrosExecution" in xcu.read_text()


# --- markup straight to PDF, and pictures left out ------------------------------------------


@NEEDS_WEASYPRINT
def test_markdown_to_pdf_with_weasyprint(tmp_path: Path) -> None:
    src = tmp_path / "notes.md"
    src.write_text("---\nheader-includes: '<link rel=\"stylesheet\" href=\"http://127.0.0.1:9/x.css\">'\n"
                   "---\n\n# Title\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
                   '<div style="background: url(file:///etc/hostname)">raw</div>\n')
    pdf = convert_file(src, "pdf")
    assert pdf.read_bytes().startswith(b"%PDF")
    text = subprocess.run(["pdftotext", pdf, "-"], capture_output=True, text=True).stdout
    assert "Title" in text
    hostname = Path("/etc/hostname").read_text().strip()
    assert not hostname or hostname not in text  # the raw HTML's file:// URL wasn't read


@NEEDS_WEASYPRINT
def test_weasyprint_reads_only_the_documents_folder(tmp_path: Path,
                                                    samples: dict[str, Path]) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "own.png").write_bytes(samples["png"].read_bytes())
    private = tmp_path / "private.png"  # e.g. something in ~/Pictures
    private.write_bytes(samples["png"].read_bytes())
    (docs / "evil.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        'width="64" height="48">'
        f'<image xlink:href="{private.as_uri()}" width="64" height="48"/>'
        '<image xlink:href="http://127.0.0.1:9/track.png" width="64" height="48"/></svg>')
    src = docs / "a.md"
    src.write_text("# T\n\n![own](own.png)\n\n![svg](evil.svg)\n")
    pdf = convert_file(src, "pdf")
    listing = subprocess.run(["pdfimages", "-list", pdf], capture_output=True, text=True,
                             check=True).stdout.splitlines()[2:]
    images = [row for row in listing if row.split()[2] == "image"]  # not smask (alpha) rows
    assert len(images) == 1  # own.png only; the SVG's references were refused


def test_pictures_left_out_are_reported(tmp_path: Path, samples: dict[str, Path]) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    outside = tmp_path / "elsewhere.png"
    outside.write_bytes(samples["png"].read_bytes())
    src = docs / "a.md"
    src.write_text(f"![one]({outside})\n\n![two](http://127.0.0.1:9/b.png)\n")
    notes: list[str] = []
    convert_file(src, "html", notes=notes)
    assert notes == ["left out 2 pictures from outside the document's folder: "
                     "elsewhere.png, b.png"]


def test_cli_prints_notes(tmp_path: Path, samples: dict[str, Path],
                          capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "docs" / "a.md"
    src.parent.mkdir()
    src.write_text(f"![one]({samples['png']})\n")
    assert main(["convert", "--json", "-t", "html", str(src)]) == 0
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["notes"] and "left out 1 picture" in line["notes"][0]


# --- zip bombs, both ways -------------------------------------------------------------------


@pytest.fixture
def small_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bomb-sized limits a test can reach with a few MB."""
    monkeypatch.setattr(limits, "BOMB_FLOOR", 1 << 20)
    monkeypatch.setattr(limits, "SPARSE_FLOOR", 1 << 20)


def _zeros_tar(path: Path, size: int, mode: Literal["w", "w:gz"]) -> Path:
    with tarfile.open(path, mode) as tar:
        info = tarfile.TarInfo("zeros.bin")
        info.size = size
        tar.addfile(info, io.BytesIO(bytes(size)))
    return path


@pytest.mark.usefixtures("small_limits")
@pytest.mark.parametrize("name", ["bomb.tar.gz", "bomb.zip", "bomb.7z", "bomb.gz",
                                  "bomb.tar.zst"])
def test_zip_bombs_are_refused_before_filling_the_disk(tmp_path: Path, name: str) -> None:
    size = 8 << 20  # 8 MB of zeros: a few KB packed, far past 100x
    src = tmp_path / name
    if name == "bomb.tar.gz":
        _zeros_tar(src, size, "w:gz")
    elif name == "bomb.zip":
        with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("zeros.bin", bytes(size))
    elif name == "bomb.gz":
        src.write_bytes(gzip.compress(bytes(size)))
    else:
        raw = tmp_path / "zeros.bin"
        raw.write_bytes(bytes(size))
        if name == "bomb.7z":
            subprocess.run(["7z", "a", "-bso0", "-bsp0", src, raw], check=True)
        else:
            plain = _zeros_tar(tmp_path / "bomb.tar", size, "w")
            subprocess.run(["zstd", "-q", "-19", plain, "-o", src], check=True)
        raw.unlink()
    with pytest.raises(ConversionError, match="zip bomb"):
        convert_file(src, "folder")
    assert not list(tmp_path.glob("zeros*")) and not list(tmp_path.glob(".hala-*"))


def test_too_many_entries_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_ENTRIES", 5)
    src = tmp_path / "many.zip"
    with zipfile.ZipFile(src, "w") as z:
        for i in range(10):
            z.writestr(f"f{i}.txt", "x")
    with pytest.raises(ConversionError, match="10 entries"):
        convert_file(src, "folder")


def test_ordinary_archives_still_unpack(tmp_path: Path) -> None:
    src = tmp_path / "ok.zip"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("notes.txt", "hello " * 1000)  # 6 KB, compresses ~100x but tiny
    assert (convert_file(src, "folder")).read_text().startswith("hello")


@pytest.mark.usefixtures("small_limits")
def test_sparse_files_are_not_packed(tmp_path: Path) -> None:
    folder = tmp_path / "vm"
    folder.mkdir()
    disk = folder / "disk.img"
    with open(disk, "wb") as f:
        f.truncate(64 << 20)  # 64 MB in size, nothing on disk
    for target in ("zip", "7z", "tar.gz"):
        with pytest.raises(ConversionError, match="sparse file"):
            convert_file(folder, target)
    with pytest.raises(ConversionError, match="sparse file"):
        run_tool(tool("zip"), [disk, tmp_path / "vm"])


@pytest.mark.usefixtures("small_limits")
def test_bomb_shaped_archives_are_not_made(tmp_path: Path) -> None:
    zeros = tmp_path / "zeros.bin"
    zeros.write_bytes(bytes(8 << 20))  # a real (not sparse) file that packs ~1000x
    with pytest.raises(ConversionError, match="shape of a zip bomb"):
        convert_file(zeros, "zip")
    assert not (tmp_path / "zeros.zip").exists()


# --- disk space and picture size ------------------------------------------------------------


def test_a_full_disk_is_refused_up_front(tmp_path: Path, samples: dict[str, Path],
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / samples["png"].name
    src.write_bytes(samples["png"].read_bytes())
    monkeypatch.setattr(limits, "free_space", lambda _folder: 10 << 20)
    with pytest.raises(ConversionError, match="not enough free space"):
        convert_file(src, "jpg")
    assert not list(tmp_path.glob(".hala-*"))  # no scratch folder left behind


def test_huge_pictures_are_refused(tmp_path: Path, samples: dict[str, Path],
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / samples["png"].name
    src.write_bytes(samples["png"].read_bytes())
    monkeypatch.setattr(limits, "MAX_PIXELS", 100)  # the sample is 64 x 48
    with pytest.raises(ConversionError, match="megapixels"):
        convert_file(src, "jpg")
    from hala.editing import load_image

    with pytest.raises(ConversionError, match="megapixels"):
        load_image(src)


def test_hala_links_start_the_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hala.gui import dropzone

    assert "MimeType=x-scheme-handler/hala;" in servicemenus.LINK_ENTRY
    assert 'app %u' in servicemenus.LINK_ENTRY  # %u: with %F, KDE would download the link
    started: list[str] = []
    monkeypatch.setattr(dropzone, "main", lambda edge="right": started.append(edge) or 0)
    assert main(["app", "hala://open"]) == 0 and started == ["right"]


# --- compress: more ways to fit (limitation 7) ----------------------------------------------


def _sh(*cmd: str | Path) -> None:
    subprocess.run([str(c) for c in cmd], check=True, capture_output=True)


def test_pdf_lossless_repack_comes_first(tmp_path: Path, samples: dict[str, Path]) -> None:
    packed = convert_file(samples["png"], "pdf", outdir=tmp_path)
    loose = tmp_path / "loose.pdf"  # the same PDF with every stream left uncompressed
    _sh("qpdf", "--stream-data=uncompress", "--object-streams=disable", packed, loose)
    target_mb = (loose.stat().st_size * 0.6) / 1e6
    out = run_tool(tool("compress"), [loose], {"size": target_mb})
    assert out.stat().st_size <= target_mb * 1e6
    info = subprocess.run(["pdfinfo", out], capture_output=True, text=True).stdout
    assert "Ghostscript" not in info  # qpdf's repack was enough: no picture was resampled


def test_compress_drops_metadata_before_quality(tmp_path: Path) -> None:
    photo = tmp_path / "photo.jpg"
    _sh("vips", "gaussnoise", tmp_path / "n.v", "200", "150")
    _sh("vips", "colourspace", tmp_path / "n.v", tmp_path / "rgb.v", "srgb")
    _sh("vips", "copy", tmp_path / "rgb.v", f"{photo}[Q=95]")
    big = tmp_path / "description.txt"
    big.write_text("x" * 400_000)  # too long to pass as an argument
    _sh("exiftool", "-q", "-overwrite_original", f"-XMP-dc:Description<={big}", photo)
    out = run_tool(tool("compress"), [photo], {"size": 0.2})  # the XMP alone is 0.4 MB
    assert out.stat().st_size <= 200_000
    width = subprocess.run(["vipsheader", "-f", "width", out], capture_output=True,
                           text=True).stdout.strip()
    assert width == "200"  # fitted by dropping metadata, not by shrinking the picture


def test_tiny_audio_targets_need_reformat_then_use_opus(tmp_path: Path) -> None:
    song = tmp_path / "talk.mp3"
    _sh("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=60",
        "-c:a", "libmp3lame", "-b:a", "64k", song)
    target = 18_000 * 60 / 8 / 1e6  # 18 kb/s: under MP3's floor, fine for Opus
    with pytest.raises(ConversionError, match="Allow a smaller format"):
        run_tool(tool("compress"), [song], {"size": target})
    out = run_tool(tool("compress"), [song], {"size": target, "reformat": True})
    assert out.suffix == ".opus" and out.stat().st_size <= target * 1e6


def test_reformat_is_only_offered_for_pictures_and_sound(tmp_path: Path) -> None:
    reformat = next(p for p in tool("compress").params if p.name == "reformat")
    assert reformat.applies_to([tmp_path / "a.jpg"]) and reformat.applies_to([tmp_path / "a.mp3"])
    assert not reformat.applies_to([tmp_path / "a.mp4"])
    assert not reformat.applies_to([tmp_path / "a.pdf"])


# --- PDF redaction in the editor (limitation 8) ---------------------------------------------


def _text_pdf(path: Path, pages: list[str], title: str = "Secret plans") -> Path:
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QFont, QPageLayout, QPageSize, QPainter, QPdfWriter

    writer = QPdfWriter(str(path))
    writer.setTitle(title)
    writer.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.A4),
                                     QPageLayout.Orientation.Portrait, QMarginsF(20, 20, 20, 20)))
    painter = QPainter(writer)
    painter.setFont(QFont("Noto Sans", 24))
    for i, text in enumerate(pages):
        if i:
            writer.newPage()
        painter.drawText(200, 600, text)
    painter.end()
    return path


def _pdf_text(path: Path, page: int) -> str:
    return subprocess.run(["pdftotext", "-f", str(page), "-l", str(page), path, "-"],
                          capture_output=True, text=True, check=True).stdout


def _page_sizes(path: Path) -> list[str]:
    info = subprocess.run(["pdfinfo", "-f", "1", "-l", "99", path], capture_output=True,
                          text=True, check=True).stdout
    return [line.split(":", 1)[1].split("pts")[0].strip() for line in info.splitlines()
            if line.startswith("Page") and "size" in line]


def test_redacted_pdf_pages_lose_their_text_the_rest_stay(tmp_path: Path) -> None:
    from PySide6.QtCore import QRect

    from hala.editing import PdfPages, Step, save_pdf_copy

    src = _text_pdf(tmp_path / "plans.pdf", ["ALPHA public", "BRAVO secret", "CHARLIE public"])
    pages = PdfPages(src)
    assert pages.count == 3
    page2 = pages.page(1)
    page2.add(Step("black", rect=QRect(0, 0, page2.base.width(), page2.base.height() // 2)))
    out = save_pdf_copy(pages)
    pages.close()
    assert out.name == "plans (edited).pdf"
    assert "ALPHA" in _pdf_text(out, 1) and "CHARLIE" in _pdf_text(out, 3)
    assert "BRAVO" not in _pdf_text(out, 2)  # flattened: the text under the box is gone
    assert _page_sizes(out) == _page_sizes(src)
    info = subprocess.run(["pdfinfo", out], capture_output=True, text=True).stdout
    assert "Secret plans" not in info  # the original's metadata isn't carried over
    assert "BRAVO" in _pdf_text(src, 2)  # the original is untouched


def test_editor_window_pages_through_a_pdf(tmp_path: Path) -> None:
    from PySide6.QtCore import QRect

    from hala.editing import Step
    from hala.gui.editor import Editor

    src = _text_pdf(tmp_path / "two.pdf", ["ONE", "TWO"])
    window = Editor(src)
    assert "crop" in window.tool_actions  # a cropped page is saved at its new size
    assert window.page_label.text().strip() == "Page 1 of 2"
    window.go_to(1)
    assert window.page_label.text().strip() == "Page 2 of 2"
    assert not window.next_page.isEnabled()
    window.doc.add(Step("black", rect=QRect(10, 10, 100, 100)))
    window.canvas.changed.emit()
    assert window.windowTitle().startswith("* ")
    window.go_to(0)  # the edit on page 2 is kept while looking at page 1
    assert window.windowTitle().startswith("* ")
    assert window.save()
    assert (tmp_path / "two (edited).pdf").exists()
    assert not window.windowTitle().startswith("*")
    window.close()


def test_edit_menu_covers_pdfs() -> None:
    menu = servicemenus.build_menus("/usr/bin/hala")["hala-edit.desktop"]
    assert "application/pdf" in menu


def test_a_rotated_page_keeps_its_size_when_flattened(tmp_path: Path) -> None:
    from PySide6.QtCore import QRect

    from hala.editing import PdfPages, Step, save_pdf_copy

    flat = _text_pdf(tmp_path / "flat.pdf", ["UPRIGHT", "TURNED"])
    src = tmp_path / "turned.pdf"
    _sh("qpdf", "--rotate=+90:2", flat, src)
    pages = PdfPages(src)
    assert pages.sizes[1] == (pages.sizes[0][1], pages.sizes[0][0])  # landscape as shown
    page = pages.page(1)
    assert page.base.width() > page.base.height()
    page.add(Step("black", rect=QRect(0, 0, 50, 50)))
    out = save_pdf_copy(pages)
    pages.close()
    shown = subprocess.run(["pdfinfo", "-f", "2", "-l", "2", out], capture_output=True,
                           text=True).stdout
    assert "842 x 595 pts" in shown  # landscape as it was shown, the same size


def test_unedited_pages_are_not_kept_in_memory(tmp_path: Path) -> None:
    from PySide6.QtCore import QRect

    from hala.editing import Step
    from hala.gui.editor import Editor

    window = Editor(_text_pdf(tmp_path / "three.pdf", ["1", "2", "3"]))
    assert window.pdf is not None
    window.doc.add(Step("black", rect=QRect(0, 0, 20, 20)))  # page 1 edited: kept
    window.go_to(1)
    window.go_to(2)  # page 2 was only looked at: dropped
    assert sorted(window.pdf._docs) == [0, 2]
    window.go_to(0)
    assert window.doc.edited  # page 1's edit is still there
    window.pdf.mark_clean()  # (so closing doesn't ask)
    window.close()


# --- password-protected PDFs ----------------------------------------------------------------


@pytest.fixture
def locked(tmp_path: Path) -> dict[str, Path]:
    """The same two-page PDF plain, with a password ("sesame pw", spaces and all), and with
    only restrictions (it opens without a password but forbids printing)."""
    plain = _text_pdf(tmp_path / "plain.pdf", ["FIRST page", "SECOND page"])
    pw = tmp_path / "pw.pdf"
    _sh("qpdf", "--encrypt", "--user-password=sesame pw", "--owner-password=owner", "--bits=256",
        "--", plain, pw)
    restricted = tmp_path / "restricted.pdf"
    _sh("qpdf", "--encrypt", "--owner-password=owner", "--bits=256", "--print=none", "--",
        plain, restricted)
    return {"plain": plain, "pw": pw, "restricted": restricted}


def test_unlock_tool(locked: dict[str, Path]) -> None:
    from hala.engines import pdf

    unlock = tool("unlock")
    with pytest.raises(pdf.PasswordNeeded, match="needs its password"):
        run_tool(unlock, [locked["pw"]])
    with pytest.raises(pdf.WrongPassword, match="wrong password"):
        run_tool(unlock, [locked["pw"]], {"password": "sesame"})
    out = run_tool(unlock, [locked["pw"]], {"password": "sesame pw"})
    assert out.name == "pw (unlocked).pdf"
    assert pdf.encryption(out) == pdf.NOT_ENCRYPTED and "SECOND" in _pdf_text(out, 2)
    freed = run_tool(unlock, [locked["restricted"]])  # no password needed, limits removed
    assert pdf.encryption(freed) == pdf.NOT_ENCRYPTED
    with pytest.raises(ConversionError, match="isn't password-protected"):
        run_tool(unlock, [locked["plain"]])


def test_the_password_never_goes_on_a_command_line(tmp_path: Path) -> None:
    from hala.engines import pdf

    cmd = pdf.unlock_command(tmp_path / "a.pdf", tmp_path / "b.pdf")
    assert "--password-file=-" in cmd and not any("sesame" in part for part in cmd)


def test_other_tools_say_to_unlock_first(locked: dict[str, Path]) -> None:
    for job in (lambda: convert_file(locked["pw"], "png"),
                lambda: convert_file(locked["pw"], "txt"),
                lambda: run_tool(tool("compress"), [locked["pw"]], {"size": 0.001}),
                lambda: run_tool(tool("split-pdf"), [locked["pw"]]),
                lambda: run_tool(tool("read-qr"), [locked["pw"]])):
        with pytest.raises(ConversionError, match="password-protected: unlock it first"):
            job()
    assert convert_file(locked["restricted"], "txt").read_text().count("page") == 2


def test_editor_opens_a_password_protected_pdf(locked: dict[str, Path]) -> None:
    from PySide6.QtCore import QRect

    from hala.editing import PdfPages, Step, save_pdf_copy
    from hala.engines import pdf

    with pytest.raises(pdf.PasswordNeeded):
        PdfPages(locked["pw"])
    with pytest.raises(pdf.WrongPassword):
        PdfPages(locked["pw"], "nope")
    pages = PdfPages(locked["pw"], "sesame pw")
    assert pages.unlocked and pages.count == 2 and pages.source != locked["pw"]
    unlocked_copy = pages.source
    assert unlocked_copy.parent.stat().st_mode & 0o077 == 0  # private scratch folder
    pages.page(0).add(Step("black", rect=QRect(0, 0, 40, 40)))
    out = save_pdf_copy(pages)
    pages.close()
    assert out.name == "pw (edited).pdf" and pdf.encryption(out) == pdf.NOT_ENCRYPTED
    assert "SECOND" in _pdf_text(out, 2)
    assert not unlocked_copy.exists()  # the unlocked copy is gone with the editor


def test_editor_asks_for_the_password_and_retries(locked: dict[str, Path],
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from PySide6.QtWidgets import QInputDialog

    from hala.gui import editor

    answers = iter([("wrong", True), ("sesame pw", True)])
    prompts: list[str] = []

    def ask(_parent: object, _title: str, label: str, *_a: object) -> tuple[str, bool]:
        prompts.append(label)
        return next(answers)

    monkeypatch.setattr(QInputDialog, "getText", ask)
    window = editor._open(locked["pw"])
    assert window is not None and window.pdf is not None and window.pdf.count == 2
    assert "password-protected" in prompts[0] and "doesn't open" in prompts[1]
    window.close()
    monkeypatch.setattr(QInputDialog, "getText", lambda *_a: ("", False))
    assert editor._open(locked["pw"]) is None  # cancelled


def test_password_fields_are_masked_and_kept_exactly(locked: dict[str, Path]) -> None:
    from PySide6.QtWidgets import QLineEdit

    from hala.gui.dialog import ToolDialog

    dialog = ToolDialog(tool("unlock"), [locked["pw"]])
    field = dialog.fields["password"]
    assert isinstance(field, QLineEdit) and field.echoMode() == QLineEdit.EchoMode.Password
    field.setText(" sesame pw ")
    assert dialog.values()["password"] == " sesame pw "  # not trimmed
    dialog.close()


def test_passwords_stay_out_of_the_log(locked: dict[str, Path], tmp_path: Path,
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop

    monkeypatch.setattr(desktop, "LOG_FILE", tmp_path / "last-run.log")
    monkeypatch.setattr(desktop.log, "handlers", [])

    class NoBatch:
        def __init__(self, *_a: object, **_k: object) -> None:
            pass

        def start(self) -> int:
            return 0

    monkeypatch.setattr(desktop, "Batch", NoBatch)
    desktop.run([locked["pw"]], None, tool_name="unlock", ask=False,
                params={"password": "sesame pw"})
    for handler in desktop.log.handlers:
        handler.flush()
    text = (tmp_path / "last-run.log").read_text()
    assert "unlock on 1 file" in text and "sesame" not in text and "(hidden)" in text


# --- Markup to PDF: Hala's print stylesheet -------------------------------------------------


@NEEDS_WEASYPRINT
def test_markup_pdfs_get_page_numbers_and_the_print_style(tmp_path: Path) -> None:
    src = tmp_path / "plan.md"
    src.write_text("# Plan\n\n" + "A paragraph of text. " * 400 + "\n\n| a | b |\n|---|---|\n"
                   "| 1 | 2 |\n")
    pdf = convert_file(src, "pdf")
    pages = int(next(line.split()[1] for line in subprocess.run(
        ["pdfinfo", pdf], capture_output=True, text=True).stdout.splitlines()
        if line.startswith("Pages:")))
    assert pages >= 2
    squash = str.maketrans("", "", " ")  # pdftotext may drop the spaces around the slash
    assert f"1/{pages}" in _pdf_text(pdf, 1).translate(squash)
    assert f"{pages}/{pages}" in _pdf_text(pdf, pages).translate(squash)
    fonts = subprocess.run(["pdffonts", pdf], capture_output=True, text=True).stdout
    assert re.search(r"(Noto|DejaVu)-?Serif", fonts)  # the body type, not a default


def test_the_run_log_is_written_despite_the_librarys_null_handler(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Found in the live check (2026-10-21): hala/__init__.py's NullHandler made _setup_log
    # think logging was already set up, so last-run.log was never written.
    import logging

    from hala import desktop

    monkeypatch.setattr(desktop, "LOG_FILE", tmp_path / "last-run.log")
    monkeypatch.setattr(desktop.log, "handlers", [logging.NullHandler()])  # as after import
    desktop._setup_log()
    desktop.log.info("action copy on 1 file(s)")
    for handler in desktop.log.handlers:
        handler.flush()
    assert "action copy on 1 file(s)" in (tmp_path / "last-run.log").read_text()
