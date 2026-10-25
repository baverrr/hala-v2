"""Size limits people can change (limits.conf, `hala limits`), and crop, rotate and flip
for PDF pages in the editor."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
from test_workarounds import _page_sizes, _pdf_text, _text_pdf

from hala import limits
from hala.cli import main
from hala.runner import ConversionError


@pytest.fixture
def conf(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private config folder, and the limits back at their defaults afterwards."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    for s in limits.SETTINGS:
        monkeypatch.setattr(limits, s.attr, limits.DEFAULTS[s.key] * s.scale)
    monkeypatch.setattr(limits, "_loaded", {})
    return tmp_path / "config" / "hala" / "limits.conf"


# --- limits ---------------------------------------------------------------------------------


def test_defaults_without_a_file(conf: Path) -> None:
    assert limits.current() == {"max_megapixels": 500, "max_archive_entries": 100_000,
                                "max_unpack_ratio": 100, "unpack_floor_gb": 1}
    assert not conf.exists()


def test_set_and_reset_from_the_command_line(conf: Path, capsys: pytest.CaptureFixture) -> None:
    assert main(["limits", "set", "max_megapixels", "1,500"]) == 0
    assert "max_megapixels = 1500" in conf.read_text()
    assert limits.MAX_PIXELS == 1_500_000_000
    assert "(default 500)" in capsys.readouterr().out
    assert main(["limits", "set", "max_archive_entries", "250000"]) == 0
    assert main(["limits", "reset", "max_megapixels"]) == 0
    assert limits.MAX_PIXELS == 500_000_000
    assert limits.MAX_ENTRIES == 250_000
    assert main(["limits", "reset"]) == 0
    assert not conf.exists()  # nothing differs from the defaults
    assert limits.MAX_ENTRIES == 100_000


@pytest.mark.parametrize("value", ["0", "-5", "lots", "999999999"])
def test_bad_values_are_refused(conf: Path, value: str, capsys: pytest.CaptureFixture) -> None:
    assert main(["limits", "set", "max_megapixels", value]) == 2
    assert "max_megapixels must be" in capsys.readouterr().err
    assert not conf.exists()


def test_a_bad_file_falls_back_to_the_defaults(conf: Path,
                                               capsys: pytest.CaptureFixture) -> None:
    conf.parent.mkdir(parents=True)
    conf.write_text("[limits]\nmax_megapixels = huge\nmystery = 3\nmax_unpack_ratio = 400\n")
    assert limits.current()["max_megapixels"] == 500
    assert limits.BOMB_RATIO == 400  # the good line still counts
    assert main(["limits"]) == 1
    err = capsys.readouterr().err
    assert "max_megapixels must be a whole number" in err
    assert "no limit called 'mystery'" in err
    conf.write_text("not an ini file at all")
    assert limits.current()["max_unpack_ratio"] == 100


def test_a_changed_file_is_picked_up_by_a_running_process(conf: Path) -> None:
    limits.check_pixels(20_000, 20_000, "big.png")  # 400 MP: allowed
    conf.parent.mkdir(parents=True)
    conf.write_text("[limits]\nmax_megapixels = 300\n")
    with pytest.raises(ConversionError, match="hala limits set max_megapixels"):
        limits.check_pixels(20_000, 20_000, "big.png")
    conf.write_text("[limits]\nmax_megapixels = 450\n")
    os.utime(conf, ns=(1, 1))  # (a new stamp even within the same clock tick)
    limits.check_pixels(20_000, 20_000, "big.png")
    conf.unlink()
    assert limits.current()["max_megapixels"] == 500


def test_a_raised_limit_lets_a_big_archive_through(conf: Path) -> None:
    with pytest.raises(ConversionError, match="max_archive_entries"):
        limits.check_unpacked(200_000, 200_000, 10 << 20, None)
    assert main(["limits", "set", "max_archive_entries", "300000"]) == 0
    limits.check_unpacked(200_000, 200_000, 10 << 20, None)


def test_tests_monkeypatching_a_constant_keep_it(conf: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_PIXELS", 100)
    with pytest.raises(ConversionError):
        limits.check_pixels(20, 20, "tiny.png")  # no file: nothing to re-apply


# --- PDF crop, rotate, flip -----------------------------------------------------------------


def _turns(path: Path) -> list[int]:
    info = subprocess.run(["pdfinfo", "-f", "1", "-l", "99", path], capture_output=True,
                          text=True, check=True).stdout
    return [int(m) for m in re.findall(r"^Page +\d+ rot: +(\d+)", info, re.MULTILINE)]


def test_a_page_only_rotated_keeps_its_text(tmp_path: Path) -> None:
    from hala.editing import PdfPages, Step, save_pdf_copy

    src = _text_pdf(tmp_path / "doc.pdf", ["ALPHA", "BRAVO", "CHARLIE"])
    pages = PdfPages(src)
    pages.page(1).add(Step("rotate", amount=90))
    pages.page(2).add(Step("rotate", amount=90))
    pages.page(2).add(Step("rotate", amount=270))  # back where it was
    assert pages.flattened_pages() == []
    assert pages.rotated_pages() == {1: 90}
    out = save_pdf_copy(pages)
    pages.close()
    assert "BRAVO" in _pdf_text(out, 2)  # not redrawn: the text is still there
    assert _turns(out) == [0, 90, 0]
    assert _page_sizes(out) == _page_sizes(src)


def test_rotating_an_already_turned_page_adds_to_its_turn(tmp_path: Path) -> None:
    from hala.editing import PdfPages, Step, save_pdf_copy

    flat = _text_pdf(tmp_path / "flat.pdf", ["ONE", "TWO"])
    src = tmp_path / "turned.pdf"
    subprocess.run(["qpdf", "--rotate=+90:2", flat, src], check=True)
    pages = PdfPages(src)
    pages.page(1).add(Step("rotate", amount=180))
    out = save_pdf_copy(pages)
    pages.close()
    assert _turns(out) == [0, 270]


def test_a_cropped_page_is_flattened_at_its_new_size(tmp_path: Path) -> None:
    from PySide6.QtCore import QRect

    from hala.editing import PdfPages, Step, save_pdf_copy

    src = _text_pdf(tmp_path / "doc.pdf", ["KEEP top", "OTHER"])
    pages = PdfPages(src)
    page = pages.page(0)
    width = page.base.width()
    page.add(Step("crop", rect=QRect(0, 0, width // 2, width // 2)))
    assert pages.flattened_pages() == [0]
    out = save_pdf_copy(pages)
    pages.close()
    sizes = _page_sizes(out)
    w, h = (float(v) for v in sizes[0].split(" x "))
    assert abs(w - 595 / 2) < 2 and abs(h - 595 / 2) < 2  # half the A4 width, square
    assert sizes[1] == _page_sizes(src)[1]  # the other page is untouched
    assert "OTHER" in _pdf_text(out, 2)
    assert "KEEP" not in _pdf_text(out, 1)  # flattened: a picture, no text


def test_a_page_rotated_and_marked_is_flattened_turned(tmp_path: Path) -> None:
    from PySide6.QtCore import QRect

    from hala.editing import PdfPages, Step, save_pdf_copy

    src = _text_pdf(tmp_path / "doc.pdf", ["SECRET"])
    pages = PdfPages(src)
    pages.page(0).add(Step("rotate", amount=90))
    pages.page(0).add(Step("black", rect=QRect(0, 0, 40, 40)))
    out = save_pdf_copy(pages)
    pages.close()
    assert _page_sizes(out) == ["842 x 595"]  # landscape now, the same size
    assert "SECRET" not in _pdf_text(out, 1)


def test_the_editor_offers_crop_and_rotate_for_pdfs(tmp_path: Path) -> None:
    from hala.gui.editor import Editor

    window = Editor(_text_pdf(tmp_path / "doc.pdf", ["ONE"]))
    assert "crop" in window.tool_actions
    names = {a.text() for bar in window.findChildren(type(window.addToolBar("x")))
             for a in bar.actions()}
    assert {"Rotate left", "Rotate right", "Flip horizontally"} <= names
    window.canvas.transform("rotate", 90)
    assert window.doc.render().width() > window.doc.render().height()
    window.pdf.mark_clean()  # (so closing doesn't ask)
    window.close()


def test_set_wont_rewrite_a_file_it_cant_fully_read(conf: Path,
                                                    capsys: pytest.CaptureFixture) -> None:
    conf.parent.mkdir(parents=True)
    conf.write_text("[limits]\nmax_megapixels = 900\nmax_megapixels = 901\n")  # a duplicate
    assert main(["limits", "set", "max_archive_entries", "500000"]) == 1
    assert "fix" in capsys.readouterr().err
    assert "max_megapixels = 900" in conf.read_text()  # left as it was
    assert main(["limits", "reset"]) == 0
    assert not conf.exists()


def test_a_raised_limit_raises_qts_cap_too(conf: Path) -> None:
    from PySide6.QtGui import QImageReader

    from hala.editing import load_image

    assert main(["limits", "set", "max_megapixels", "1000"]) == 0
    conf.parent.mkdir(parents=True, exist_ok=True)
    load_image(_png(conf.parent))
    assert QImageReader.allocationLimit() >= 1000 * 4_000_000 >> 20  # 1000 MP, 4 bytes each


def _png(folder: Path) -> Path:
    from PySide6.QtGui import QImage

    path = folder / "small.png"
    image = QImage(8, 8, QImage.Format.Format_RGB32)
    image.fill(0)
    image.save(str(path))
    return path


def test_turning_a_page_right_round_is_no_edit(tmp_path: Path) -> None:
    from hala.editing import PdfPages, Step

    pages = PdfPages(_text_pdf(tmp_path / "doc.pdf", ["ONE"]))
    pages.page(0).add(Step("rotate", amount=90))
    assert pages.edited
    pages.page(0).add(Step("rotate", amount=270))
    assert not pages.edited and pages.edited_pages() == []
    pages.close()
