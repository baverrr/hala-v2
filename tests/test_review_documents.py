"""Review of 0.2-0.4 (docs/review-2026-10-21-0.4.md): documents, notes, clipboard, phone."""

from __future__ import annotations

import importlib.util
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hala import actions, desktop
from hala.cli import main
from hala.convert import convert_file
from hala.engines import documents
from hala.engines.weasy_confined import confined, with_print_style
from hala.gui import wheelapp
from hala.jobs import Job, Status, clean_note

NEEDS_WEASYPRINT = pytest.mark.skipif(importlib.util.find_spec("weasyprint") is None,
                                      reason="WeasyPrint isn't installed")


def _attachments(pdf: Path) -> str:
    return subprocess.run(["qpdf", "--list-attachments", pdf], capture_output=True,
                          text=True).stdout


def _images(pdf: Path) -> int:
    listing = subprocess.run(["pdfimages", "-list", pdf], capture_output=True, text=True,
                             check=True).stdout.splitlines()[2:]
    return sum(1 for row in listing if row.split()[2] == "image")


# --- markup PDFs: no attachments, no hidden folders --------------------------------------------


@NEEDS_WEASYPRINT
@pytest.mark.parametrize("name, text", [
    ("doc.md", "# T\n\n[notes](tax-return.txt){rel=attachment}\n"),
    ("doc.html", '<html><body><p>T</p><a rel="attachment" href="tax-return.txt">x</a>'
                 "</body></html>"),
])
def test_links_cant_attach_files(tmp_path: Path, name: str, text: str) -> None:
    (tmp_path / "tax-return.txt").write_text("TOPSECRET-TAXDATA\n")
    src = tmp_path / name
    src.write_text(text)
    pdf = convert_file(src, "pdf")
    assert "has no embedded files" in _attachments(pdf)
    qdf = subprocess.run(["qpdf", "--qdf", "--object-streams=disable", pdf, "-"],
                         capture_output=True, check=True).stdout
    assert b"TOPSECRET" not in qdf


@NEEDS_WEASYPRINT
def test_hidden_folders_stay_out(tmp_path: Path, samples: dict[str, Path]) -> None:
    # A document saved straight into the home folder must not reach ~/.ssh.
    (tmp_path / "pics").mkdir()
    (tmp_path / ".ssh").mkdir()
    (tmp_path / "pics" / "a.png").write_bytes(samples["png"].read_bytes())
    (tmp_path / ".ssh" / "key.png").write_bytes(samples["png"].read_bytes())
    src = tmp_path / "doc.md"
    src.write_text("# T\n\n![a](pics/a.png)\n\n![k](.ssh/key.png)\n")
    notes: list[str] = []
    pdf = convert_file(src, "pdf", notes=notes)
    assert _images(pdf) == 1
    assert any("key.png" in n for n in notes)


def test_hidden_folders_stay_out_of_other_formats(tmp_path: Path,
                                                  samples: dict[str, Path]) -> None:
    (tmp_path / ".ssh").mkdir()
    (tmp_path / ".ssh" / "key.png").write_bytes(samples["png"].read_bytes())
    src = tmp_path / "doc.md"
    src.write_text("![k](.ssh/key.png)\n")
    notes: list[str] = []
    convert_file(src, "html", notes=notes)
    assert notes and "key.png" in notes[0]


def test_confined_fetcher(tmp_path: Path, samples: dict[str, Path]) -> None:
    root = tmp_path / "root"
    (root / "pics").mkdir(parents=True)
    (root / ".ssh").mkdir()
    (tmp_path / "rootx").mkdir()
    picture = samples["png"].read_bytes()
    (root / "pics" / "a.png").write_bytes(picture)
    (root / ".ssh" / "id.png").write_bytes(picture)
    (root / "notes.txt").write_text("not a picture")
    (tmp_path / "outside.png").write_bytes(picture)
    (tmp_path / "rootx" / "b.png").write_bytes(picture)
    (root / "link.png").symlink_to(tmp_path / "outside.png")
    fetch = confined([root.resolve()], lambda url, *_a, **_k: "ok")
    assert fetch((root / "pics" / "a.png").as_uri()) == "ok"
    assert fetch("data:image/png;base64,AAAA") == "ok"
    refused = [
        (root / "link.png").as_uri(),  # symlink out of the root
        f"file://{root}/../outside.png",
        f"file://{root}/pics%2F..%2F..%2Foutside.png",
        f"file://otherhost{root}/pics/a.png",
        (tmp_path / "rootx" / "b.png").as_uri(),  # a sibling whose name starts the same
        (root / ".ssh" / "id.png").as_uri(),  # hidden folder
        (root / "notes.txt").as_uri(),  # not a picture or font
        "http://127.0.0.1:9/x.png",
    ]
    for url in refused:
        with pytest.raises(ValueError):
            fetch(url)


def test_print_style_is_the_last_style() -> None:
    page = with_print_style("<html><head><style>body{padding:50px}</style></head><body/>")
    assert page.index("padding:50px") < page.index("@page") < page.index("</head>")


@NEEDS_WEASYPRINT
def test_print_style_sets_the_page(tmp_path: Path) -> None:
    src = tmp_path / "doc.md"
    src.write_text("# Title\n\nHello body text here.\n")
    pdf = convert_file(src, "pdf")
    bbox = subprocess.run(["pdftotext", "-bbox", pdf, "-"], capture_output=True, text=True,
                          check=True).stdout
    hello = next(line for line in bbox.splitlines() if ">Hello<" in line)
    attrs = {k: float(v) for k, v in re.findall(r'(\w+)="([\d.]+)"', hello)}
    x_min, y_min, y_max = attrs["xMin"], attrs["yMin"], attrs["yMax"]
    # 20 mm left margin (56.7 pt) without pandoc's 50px padding; 10.5 pt text, not 12 pt.
    assert 55 < x_min < 59
    assert y_max - y_min < 15.5


@NEEDS_WEASYPRINT
def test_markup_pdf_reports_pictures_weasyprint_couldnt_load(tmp_path: Path) -> None:
    (tmp_path / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)  # header only
    src = tmp_path / "doc.md"
    src.write_text("# T\n\n![b](broken.png)\n")
    notes: list[str] = []
    convert_file(src, "pdf", notes=notes)
    assert any("WeasyPrint couldn't load: broken.png" in n for n in notes)


@NEEDS_WEASYPRINT
def test_broken_weasyprint_falls_back_to_libreoffice(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(documents, "sys", SimpleNamespace(executable="false"))
    went: list[str] = []

    def office(ctx: Any, src: Path, dst_ext: str) -> Path:
        went.append(src.suffix)
        out = ctx.workdir / "lo.pdf"
        out.write_bytes(b"%PDF-1.4\n")
        return out

    monkeypatch.setattr(documents, "office", office)
    src = tmp_path / "doc.md"
    src.write_text("# T\n")
    notes: list[str] = []
    convert_file(src, "pdf", notes=notes)
    assert went == [".docx"]
    assert any(n.startswith("WeasyPrint failed") and "LibreOffice" in n for n in notes)


# --- notes ---------------------------------------------------------------------------------


def test_notes_lose_control_characters() -> None:
    assert clean_note("left out \x1b]8;;evil\x07x\n.png\x7f") == "left out ?]8;;evil?x?.png?"


def test_cli_prints_clean_notes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    src = tmp_path / "docs" / "a.md"
    src.parent.mkdir()
    src.write_text("![x](http://127.0.0.1:9/a%1B]0;pwned%07.png)\n")
    assert main(["convert", "-t", "html", str(src)]) == 0
    out = capsys.readouterr().out
    assert "note:" in out and "\x1b" not in out and "\x07" not in out


def _job(n: int, notes: list[str], status: Status = Status.DONE) -> Job:
    return Job(id=n, src=Path(f"/x/f{n}.md"), target="pdf", status=status,
               output=Path(f"/x/f{n}.pdf") if status is Status.DONE else None,
               error="boom" if status is Status.FAILED else None, notes=notes)


def test_notes_are_capped_with_a_count() -> None:
    lines = desktop._note_lines([_job(1, [f"note {i}" for i in range(5)])])
    assert len(lines) == 4 and "and 2 more" in lines[-1]


def test_a_partly_failed_batch_still_shows_notes() -> None:
    told: list[str] = []
    done, failed = _job(1, ["left out 1 picture: a.png"]), _job(2, [], Status.FAILED)
    fake = SimpleNamespace(jobs=[done, failed], skipped=[], tool=None, target="pdf",
                           _notify=lambda _s, body, *_a, **_k: told.append(body))
    desktop.Batch._notify_problems(fake, [done], [failed], False)  # type: ignore[arg-type]
    assert told and "left out 1 picture: a.png" in told[0]


def test_job_repr_hides_its_parameters() -> None:
    job = Job(id=1, src=Path("/x/a.pdf"), target="unlock", tool=True,
              params={"password": "sesame"})
    assert "sesame" not in repr(job)


# --- phone, clipboard, hala:// --------------------------------------------------------------


def test_phone_zip_folder_that_cant_be_made_is_refused(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    blocker = tmp_path / "cache"
    blocker.write_text("a file, not a folder")
    monkeypatch.setattr(actions, "PHONE_ZIPS", blocker / "phone")
    folder = tmp_path / "album"
    folder.mkdir()
    (folder / "a.txt").write_text("x")
    refused = actions.send_to_phone([folder], "device")
    assert len(refused) == 1 and refused[0][0] == folder and "can't make" in refused[0][1]


def test_uri_list_edge_cases(tmp_path: Path) -> None:
    (tmp_path / "a?b#c.png").write_bytes(b"x")
    (tmp_path / "plain.png").write_bytes(b"x")
    skipped: list[str] = []
    text = (f"file://{tmp_path}/a%3Fb%23c.png file://{tmp_path}/plain.png "  # Klipper style
            f"file://{tmp_path}/nul%00.png file:relative.png "
            "smb://nas/share/x.png sftp://host/y.png just some words")
    assert wheelapp.parse_uri_list(text, skipped) == [tmp_path / "a?b#c.png",
                                                      tmp_path / "plain.png"]
    assert skipped == ["smb://nas/share/x.png", "sftp://host/y.png"]


def test_only_network_files_copied_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    told: list[str] = []

    def clipboard(skipped: list[str]) -> list[Path]:
        skipped += ["smb://nas/a.png", "smb://nas/b.png"]
        return []

    monkeypatch.setattr(wheelapp, "clipboard_files", clipboard)
    monkeypatch.setattr(desktop, "notify", lambda summary, body, *_a, **_k: told.append(body))
    assert wheelapp.main([], from_clipboard=True) == 1
    assert told and "2 network files skipped" in told[0]


def test_hala_link_with_a_file_runs_just_the_file(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    pic = tmp_path / "a.png"
    pic.write_bytes(b"x")
    ran: list[list[Path]] = []
    monkeypatch.setattr(desktop, "run", lambda files, target: ran.append(files) or 0)
    assert main(["app", "hala://open", str(pic)]) == 0
    assert ran == [[pic]]

