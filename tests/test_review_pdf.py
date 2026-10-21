"""Review of 0.2.0-0.4.0, PDF side: passwords end to end, what a redacted copy keeps, qpdf's
"worked, with warnings", unlock without qpdf, render limits, compress notes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from test_workarounds import _pdf_text, _sh, _text_pdf

from hala import limits
from hala.convert import convert_file
from hala.engines import pdf
from hala.runner import ConversionError
from hala.tools import run_tool, tool


@pytest.fixture
def locked(tmp_path: Path) -> dict[str, Path]:
    plain = _text_pdf(tmp_path / "plain.pdf", ["FIRST page", "SECOND page"])
    pw = tmp_path / "pw.pdf"
    _sh("qpdf", "--encrypt", "--user-password=sesame pw", "--owner-password=owner", "--bits=256",
        "--", plain, pw)
    restricted = tmp_path / "restricted.pdf"
    _sh("qpdf", "--encrypt", "--owner-password=owner", "--bits=256", "--print=none", "--",
        plain, restricted)
    return {"plain": plain, "pw": pw, "restricted": restricted}


class Recorder:
    """Records the argv of every process started, and lets it run."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.argvs: list[list[str]] = []
        real = subprocess.Popen
        argvs = self.argvs

        class Popen(real):  # type: ignore[misc,valid-type]
            def __init__(self, args, *a, **k):  # type: ignore[no-untyped-def]
                argvs.append([str(x) for x in (args if isinstance(args, list) else [args])])
                super().__init__(args, *a, **k)

        monkeypatch.setattr(subprocess, "Popen", Popen)


def _no_password_in(argvs: list[list[str]], secret: str) -> None:
    for argv in argvs:
        for part in argv:
            assert secret not in part, argv
            assert not part.startswith(("--password=", "--user-password")), argv


def test_the_password_stays_off_every_command_line(locked: dict[str, Path],
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from hala.editing import PdfPages

    rec = Recorder(monkeypatch)
    out = run_tool(tool("unlock"), [locked["pw"]], {"password": "sesame pw"})
    pages = PdfPages(locked["pw"], "sesame pw")
    assert pages.count == 2
    pages.close()
    assert "SECOND" in _pdf_text(out, 2)
    assert any("qpdf" in " ".join(a) and "--decrypt" in a for a in rec.argvs)
    _no_password_in(rec.argvs, "sesame")


@pytest.mark.parametrize("password", [" sesame pw ", "pässwörd"])
def test_odd_passwords_really_unlock(tmp_path: Path, password: str) -> None:
    from hala.editing import PdfPages

    plain = _text_pdf(tmp_path / "plain.pdf", ["ONLY page"])
    locked = tmp_path / "odd.pdf"
    _sh("qpdf", "--encrypt", f"--user-password={password}", "--owner-password=owner",
        "--bits=256", "--", plain, locked)
    out = run_tool(tool("unlock"), [locked], {"password": password})
    assert pdf.encryption(out) == pdf.NOT_ENCRYPTED and "ONLY" in _pdf_text(out, 1)
    with pytest.raises(pdf.WrongPassword):
        PdfPages(locked, password.strip() + "x")
    pages = PdfPages(locked, password)
    assert pages.count == 1
    pages.close()


def test_a_redacted_copy_keeps_nothing_document_wide(tmp_path: Path) -> None:
    from PySide6.QtCore import QRect

    from hala.editing import PdfPages, Step, save_pdf_copy

    base = _text_pdf(tmp_path / "base.pdf", ["ALPHA public", "BRAVO secret", "CHARLIE public"])
    marks = tmp_path / "marks.ps"
    marks.write_text("[/Title (BRAVO outline) /Page 2 /OUT pdfmark\n"
                     "[/Title (BRAVO info) /Author (BRAVO author) /DOCINFO pdfmark\n")
    marked = tmp_path / "marked.pdf"
    _sh("gs", "-q", "-dSAFER", "-dBATCH", "-dNOPAUSE", "-sDEVICE=pdfwrite",
        f"-sOutputFile={marked}", base, marks)
    attachment = tmp_path / "notes.pdf"  # qpdf's AppArmor profile: PDF-like names only
    attachment.write_bytes(b"BRAVO attached")
    src = tmp_path / "plans.pdf"
    _sh("qpdf", marked, "--add-attachment", attachment, "--", src)
    assert "BRAVO" in subprocess.run(["pdfinfo", src], capture_output=True, text=True).stdout

    pages = PdfPages(src)
    page = pages.page(1)
    page.add(Step("black", rect=QRect(0, 0, 10, 10)))  # any edit flattens the page
    out = save_pdf_copy(pages)
    pages.close()

    def run(*cmd: str | Path) -> str:
        return subprocess.run([str(c) for c in cmd], capture_output=True, text=True).stdout

    assert "notes.pdf" not in run("qpdf", "--list-attachments", out)
    assert '"outlines": []' in run("qpdf", "--json", "--json-key=outlines", out)
    assert "BRAVO" not in run("pdfinfo", "-meta", out)
    fonts = run("pdffonts", "-f", "2", "-l", "2", out).splitlines()
    assert len(fonts) <= 2  # just the header: the flattened page has no text
    assert len(run("pdfimages", "-list", "-f", "2", "-l", "2", out).splitlines()) == 3  # 1 image
    qdf = tmp_path / "out.qdf"
    subprocess.run(["qpdf", "--qdf", "--object-streams=disable", out, qdf], capture_output=True)
    assert b"BRAVO" not in qdf.read_bytes()
    assert "ALPHA" in _pdf_text(out, 1)


def test_a_typed_password_doesnt_stop_a_restricted_pdf(locked: dict[str, Path]) -> None:
    unlock = tool("unlock")
    for typed in ("sesame pw", "owner"):  # another PDF's password, then its own
        out = run_tool(unlock, [locked["restricted"]], {"password": typed})
        assert pdf.encryption(out) == pdf.NOT_ENCRYPTED
        out.unlink()


def _damaged(tmp_path: Path) -> Path:
    """A PDF with its streams uncompressed and its cross-reference offset wrong: qpdf repairs
    it, with a warning (exit code 3)."""
    good = _text_pdf(tmp_path / "good.pdf", ["DAMAGED but readable"] * 3)
    loose = tmp_path / "loose.pdf"
    _sh("qpdf", "--stream-data=uncompress", "--object-streams=disable", good, loose)
    data = loose.read_bytes()
    head, _sep, tail = data.rpartition(b"startxref")
    damaged = tmp_path / "damaged.pdf"
    damaged.write_bytes(head + b"startxref\n12345\n%%EOF\n")
    code = subprocess.run(["qpdf", "--check", damaged], capture_output=True).returncode
    assert code == 3 and tail
    return damaged


def test_compress_repairs_a_damaged_pdf(tmp_path: Path) -> None:
    damaged = _damaged(tmp_path)
    notes: list[str] = []
    target = damaged.stat().st_size * 0.7 / 1e6
    out = run_tool(tool("compress"), [damaged], {"size": target}, notes=notes)
    assert out.stat().st_size <= target * 1e6 and "DAMAGED" in _pdf_text(out, 1)
    assert any("repaired" in n for n in notes)


def test_merge_split_and_metadata_accept_a_repaired_pdf(tmp_path: Path) -> None:
    damaged = _damaged(tmp_path)
    plain = _text_pdf(tmp_path / "plain.pdf", ["PLAIN"])
    merged = run_tool(tool("merge-pdf"), [damaged, plain])
    assert "PLAIN" in _pdf_text(merged, 4)
    split = run_tool(tool("split-pdf"), [damaged])
    assert len(list(split.iterdir())) == 3
    assert run_tool(tool("metadata"), [damaged], {"remove": "everything"}).exists()


def test_without_qpdf_the_password_check_says_so(locked: dict[str, Path], tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from hala.editing import PdfPages

    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert pdf.encryption(locked["pw"]) == pdf.UNKNOWN
    for job in (lambda: run_tool(tool("unlock"), [locked["pw"]], {"password": "sesame pw"}),
                lambda: PdfPages(locked["pw"], "sesame pw")):
        with pytest.raises(ConversionError, match="qpdf isn't installed"):
            job()


def test_merge_and_metadata_say_to_unlock_first(locked: dict[str, Path]) -> None:
    for job in (lambda: run_tool(tool("merge-pdf"), [locked["pw"], locked["plain"]]),
                lambda: run_tool(tool("metadata"), [locked["pw"]], {"remove": "everything"})):
        with pytest.raises(ConversionError, match="password-protected: unlock it first"):
            job()


def test_a_wrong_password_leaves_no_scratch(locked: dict[str, Path], tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from hala.editing import PdfPages

    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    with pytest.raises(pdf.WrongPassword):
        PdfPages(locked["pw"], "nope")
    assert not list(scratch.glob("hala-pdf-*"))


class Tty:
    def __init__(self, tty: bool) -> None:
        self.tty = tty

    def isatty(self) -> bool:
        return self.tty


def test_the_cli_asks_for_the_password(locked: dict[str, Path],
                                       monkeypatch: pytest.MonkeyPatch) -> None:
    import getpass

    from hala.cli import main

    asked: list[str] = []

    def ask(prompt: str = "") -> str:
        asked.append(prompt)
        return "sesame pw"

    monkeypatch.setattr(getpass, "getpass", ask)
    monkeypatch.setattr(sys, "stdin", Tty(True))
    assert main(["tool", "unlock", str(locked["pw"])]) == 0
    assert asked and (locked["pw"].parent / "pw (unlocked).pdf").exists()


def test_the_cli_doesnt_ask_without_a_terminal(locked: dict[str, Path],
                                               monkeypatch: pytest.MonkeyPatch,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    import getpass

    from hala.cli import main

    def ask(prompt: str = "") -> str:
        raise AssertionError("asked without a terminal")

    monkeypatch.setattr(getpass, "getpass", ask)
    monkeypatch.setattr(sys, "stdin", Tty(False))
    assert main(["tool", "unlock", str(locked["pw"])]) != 0
    assert "needs its password" in capsys.readouterr().err


def _blank_pdf(path: Path, width: float, height: float, pages: int = 1) -> Path:
    """Blank pages of the given size in points, written by hand (no tool makes giant pages)."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [" + b" ".join(f"{3 + i} 0 R".encode()
                                                      for i in range(pages))
               + f"] /Count {pages} >>".encode()]
    objects += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] >>".encode()
                ] * pages
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
            ).encode()
    path.write_bytes(bytes(out))
    return path


def test_huge_pages_are_refused_before_rendering(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from hala.editing import PdfPages

    big = _blank_pdf(tmp_path / "big.pdf", 14400, 14400)  # 900 megapixels at 150 dpi
    rec = Recorder(monkeypatch)
    for job in (lambda: convert_file(big, "png"), lambda: run_tool(tool("read-qr"), [big])):
        with pytest.raises(ConversionError, match="megapixels"):
            job()
    pages = PdfPages(big)
    with pytest.raises(ConversionError, match="megapixels"):
        pages.page(0)
    pages.close()
    assert not any("pdftoppm" in a[0] or "pdftoppm" in a for a in rec.argvs)


def test_too_many_pages_or_too_little_space_is_refused(tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    many = _blank_pdf(tmp_path / "many.pdf", 595, 842, pages=3)
    monkeypatch.setattr(pdf, "MAX_RENDER_PAGES", 2)
    with pytest.raises(ConversionError, match="limited to 2"):
        convert_file(many, "png")
    monkeypatch.setattr(pdf, "MAX_RENDER_PAGES", 5000)
    # Enough to start (the up-front check), not enough for the pictures.
    monkeypatch.setattr(limits, "free_space", lambda _p: limits.MIN_FREE + 500_000)
    with pytest.raises(ConversionError, match=r"only .* is free"):
        convert_file(many, "jpg")


def test_compress_says_when_the_format_changes(tmp_path: Path, samples: dict[str, Path]) -> None:
    notes: list[str] = []
    png = samples["png"]  # RGBA
    run_tool(tool("compress"), [png], {"size": png.stat().st_size * 0.8 / 1e6},
             outdir=tmp_path, notes=notes)
    assert any("JPG instead of PNG" in n and "transparent" in n for n in notes)

    notes.clear()
    wav = samples["wav"]
    run_tool(tool("compress"), [wav], {"size": wav.stat().st_size * 0.5 / 1e6},
             outdir=tmp_path, notes=notes)
    assert any("M4A instead of WAV" in n for n in notes)

    notes.clear()
    srt = tmp_path / "subs.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nhello\n")
    mkv = tmp_path / "film.mkv"
    _sh("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=25:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=660:duration=2", "-i", srt,
        "-map", "0", "-map", "1", "-map", "2", "-map", "3",
        "-c:v", "libx264", "-b:v", "2M", "-c:a", "aac", "-c:s", "srt", mkv)
    run_tool(tool("compress"), [mkv], {"size": mkv.stat().st_size * 0.5 / 1e6},
             outdir=tmp_path, notes=notes)
    assert any("MP4 instead of MKV" in n and "1 more sound track" in n
               and "1 subtitle track" in n for n in notes), notes


def test_audio_that_overshoots_below_the_floor_gets_the_same_advice(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hala.tools import compress

    song = tmp_path / "talk.mp3"
    _sh("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anoisesrc=duration=20",
        "-c:a", "libmp3lame", "-b:a", "128k", song)
    monkeypatch.setattr(compress, "HEADROOM", 1.6)  # aims too high: every try overshoots
    target = 25_000 * 20 / 8 / 1e6  # just above MP3's floor
    with pytest.raises(ConversionError, match="Allow a smaller format"):
        run_tool(tool("compress"), [song], {"size": target})
