"""Dolphin menu generation and the desktop-run helpers (no D-Bus needed)."""

from __future__ import annotations

import configparser
import shlex
import subprocess
from pathlib import Path

import pytest

from hala import servicemenus
from hala.cli import build_parser
from hala.desktop import _common_folder, _error_detail
from hala.registry import route_for

EXE = "/opt/hala/bin/hala"


@pytest.fixture(scope="module")
def menus() -> dict[str, configparser.ConfigParser]:
    parsed = {}
    for name, text in servicemenus.build_menus(EXE).items():
        cp = configparser.ConfigParser(interpolation=None, strict=False)
        cp.optionxform = str  # type: ignore[assignment]  # keep key case
        cp.read_string(text)
        parsed[name] = cp
    return parsed


def actions(menu: configparser.ConfigParser) -> list[str]:
    return [menu[s]["Name"] for s in menu.sections() if s.startswith("Desktop Action")]


def entry(menu: configparser.ConfigParser, key: str) -> list[str]:
    return [x for x in menu["Desktop Entry"].get(key, "").split(";") if x]


def test_single_file_menu_lists_exact_targets(menus: dict) -> None:
    png = menus["hala-png.desktop"]
    assert "image/png" in entry(png, "MimeType")
    assert png["Desktop Entry"]["X-KDE-RequiredNumberOfUrls"] == "1"
    names = actions(png)
    assert "JPG" in names and "PDF" in names
    assert "PNG" not in names
    assert "ZIP" not in names  # Ark already has Compress


def test_archives_offer_extract_and_repack(menus: dict) -> None:
    names = actions(menus["hala-zip.desktop"])
    assert names[0] == "Folder (extract)" and "7Z" in names


def test_inherited_types_are_excluded(menus: dict) -> None:
    txt = menus["hala-txt.desktop"]
    assert "text/plain" in entry(txt, "MimeType")
    excluded = entry(txt, "ExcludeServiceTypes")
    assert {"text/csv", "text/markdown"} <= set(excluded)
    assert "text/plain" not in excluded


def test_family_menu_for_multiple_files(menus: dict) -> None:
    family = menus["hala-family-image.desktop"]
    assert family["Desktop Entry"]["X-KDE-MinNumberOfUrls"] == "2"
    assert {"image/png", "image/jpeg", "image/heif"} <= set(entry(family, "MimeType"))
    assert {"PNG", "JPG"} <= set(actions(family))


def test_exec_lines_quote_the_binary(menus: dict) -> None:
    section = menus["hala-wav.desktop"]["Desktop Action to_mp3"]
    assert section["Exec"] == f'"{EXE}" run --to mp3 %F'


def test_jpeg_alias_extensions_are_included() -> None:
    db = servicemenus.MimeDb()
    assert db.mimes_for("jpg")[0] == "image/jpeg"
    assert db.inherits("text/csv", "text/plain")


def test_install_and_remove(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for attr, sub in (("MENU_DIR", "menus"), ("APP_DIR", "apps"), ("ICON_DIR", "icons")):
        monkeypatch.setattr(servicemenus, attr, tmp_path / sub)
    other = tmp_path / "menus" / "hala-mine.desktop"
    other.parent.mkdir()
    other.write_text("[Desktop Entry]\n")  # same prefix, not ours: must survive
    written = servicemenus.install(EXE)
    assert written and all(p.stat().st_mode & 0o111 for p in written)
    assert (tmp_path / "icons" / "hala.svg").exists()
    assert servicemenus.uninstall() == len(written)
    assert other.exists()


def test_helpers() -> None:
    assert _error_detail("ffmpeg failed:\nInvalid data") == "Invalid data"
    assert _error_detail("this video has no audio track") == "this video has no audio track"
    assert _error_detail(None) == "failed"
    # ffmpeg's real reason sits above its generic sign-off lines.
    assert _error_detail("ffmpeg failed:\nheight not divisible by 2 (161x121)\n"
                         "Error while opening encoder\nConversion failed!") \
        == "height not divisible by 2 (161x121)"
    assert _common_folder([Path("/a/b/x.jpg"), Path("/a/b/y.jpg")]) == Path("/a/b")
    assert _common_folder([Path("/a/b/x.jpg"), Path("/a/c/y.jpg")]) == Path("/a")


def test_text_menus_skip_unconvertible_text_types(menus: dict) -> None:
    # .py, .json and .log all inherit text/plain; the txt menu would only fail on them.
    for name in ("hala-txt.desktop", "hala-family-document.desktop"):
        excluded = set(entry(menus[name], "ExcludeServiceTypes"))
        assert {"text/x-python", "text/x-python3", "application/json", "text/x-log"} <= excluded


def test_low_weight_globs_ignored() -> None:
    db = servicemenus.MimeDb()
    assert db.mimes_for("ass") == ["text/x-ssa"]  # not audio/aac (a weight-10 "*.ass" glob)
    assert db.mimes_for("ts") == ["video/mp2t"]  # not the Qt Linguist type


def test_every_menu_action_is_a_real_route(menus: dict) -> None:
    exts_by_menu = {f"hala-{e.replace('.', '_')}.desktop": e
                    for e in {m["Desktop Entry"]["X-Hala-Menu"] for m in menus.values()}
                    if not e.startswith("tool-") and e not in ("edit", "shelf")}
    for name, ext in exts_by_menu.items():
        if name not in menus:
            continue
        menu = menus[name]
        actions = [s for s in menu.sections() if s.startswith("Desktop Action")]
        assert len(actions) == len(set(actions))
        for section in actions:
            target = shlex.split(menu[section]["Exec"])[3]
            assert route_for(ext, target) is not None, (name, target)


def test_app_entry_exec_parses() -> None:
    exec_line = next(line for line in servicemenus.APP_ENTRY.format(exe=EXE).splitlines()
                     if line.startswith("Exec="))
    argv = [a.replace("%F", "/tmp/x.png") for a in shlex.split(exec_line[5:])]
    args = build_parser().parse_args(argv[1:])
    assert args.command == "app" and args.files == ["/tmp/x.png"]


class FakeView:
    def __init__(self, *_args: object) -> None:
        self.calls: list[tuple] = []

    def call(self, method: str, _sig: str, *args: object) -> None:
        self.calls.append((method, *args))


@pytest.fixture
def batch_env(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, list[str]]]:
    """Batch with D-Bus stubbed out; returns the notifications it would have shown."""
    from hala import desktop

    shown: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(desktop.Gio, "bus_get_sync", lambda *_: None)
    monkeypatch.setattr(desktop, "JobView", FakeView)
    monkeypatch.setattr(desktop.GLibUnix, "signal_add", lambda *_: 0)
    monkeypatch.setattr(desktop.Batch, "_notify",
                        lambda self, summary, body, icon, actions=None, problem=False:
                        shown.append((summary, body, actions or [])))
    return shown


def test_batch_nothing_to_convert(workdir: dict[str, Path],
                                  batch_env: list[tuple[str, str, list[str]]]) -> None:
    from hala.desktop import Batch

    assert Batch([workdir["png"], workdir["wav"]], "png", 1).start() == 1
    summary, body, _ = batch_env[0]
    assert summary == "Nothing to convert"
    assert "already PNG" in body and "can't become PNG" in body


def test_batch_partial_success_reports_both(workdir: dict[str, Path],
                                            batch_env: list[tuple[str, str, list[str]]]) -> None:
    from hala.desktop import Batch

    assert Batch([workdir["png"], workdir["jpg"]], "jpg", 1).start() == 1
    summary, body, actions = batch_env[0]
    assert summary == "1 of 2 converted to JPG"
    assert "already JPG" in body and "show" in actions


def test_batch_success(workdir: dict[str, Path],
                       batch_env: list[tuple[str, str, list[str]]]) -> None:
    from hala.desktop import Batch

    assert Batch([workdir["png"]], "gif", 1).start() == 0
    summary, body, actions = batch_env[0]
    assert summary == "Converted to GIF" and body == "pic.gif" and "open" in actions


def test_batch_failure_names_file_not_path(tmp_path: Path,
                                           batch_env: list[tuple[str, str, list[str]]]) -> None:
    from hala.desktop import Batch

    broken = tmp_path / "broken.mp4"
    broken.write_text("not a video")
    assert Batch([broken], "mkv", 1).start() == 1
    summary, body, actions = batch_env[0]
    assert summary == "Couldn't convert to MKV" and "details" in actions
    assert "<b>broken.mp4</b>: Invalid data" in body and str(tmp_path) not in body


def test_batch_runs_a_tool_per_file(tmp_path: Path,
                                    batch_env: list[tuple[str, str, list[str]]]) -> None:
    from hala.desktop import Batch
    from hala.tools import tool

    qr = tmp_path / "qr.png"
    subprocess.run(["qrencode", "-o", str(qr), "hello hala"], check=True)
    notes = tmp_path / "notes.txt"
    notes.write_text("x")
    assert Batch([qr, notes], "read-qr", 1, tool=tool("read-qr")).start() == 1
    summary, body, _actions = batch_env[0]
    assert summary == "QR code read: 1 of 2"
    assert "notes.txt: Read QR doesn't take txt files" in body


def test_batch_shows_qr_text_with_copy(tmp_path: Path,
                                       batch_env: list[tuple[str, str, list[str]]]) -> None:
    from hala.desktop import Batch
    from hala.tools import tool

    qr = tmp_path / "qr.png"
    subprocess.run(["qrencode", "-o", str(qr), "hello hala"], check=True)
    assert Batch([qr], "read-qr", 1, tool=tool("read-qr")).start() == 0
    summary, body, actions = batch_env[0]
    assert (summary, body) == ("QR code read", "hello hala") and actions[:2] == ["copy", "Copy"]


def test_batch_combines_for_merge(tmp_path: Path,
                                  batch_env: list[tuple[str, str, list[str]]]) -> None:
    from hala.desktop import Batch
    from hala.tools import tool

    pngs = []
    for i in range(2):
        pngs.append(tmp_path / f"p{i}.png")
        subprocess.run(["vips", "black", str(pngs[-1]), "20", "20"], check=True)
    batch = Batch(pngs, "merge-pdf", 2, tool=tool("merge-pdf"))
    assert batch.start() == 0 and len(batch.jobs) == 1
    assert batch_env[0][1] == "p0 (merged).pdf"


def test_tool_menus(menus: dict) -> None:
    join = menus["hala-tool-join.desktop"]
    assert join["Desktop Entry"]["X-KDE-Submenu"] == "Hala tools"
    assert join["Desktop Entry"]["X-KDE-MinNumberOfUrls"] == "2"
    assert "video/mp4" in entry(join, "MimeType")
    qr = menus["hala-tool-read-qr.desktop"]["Desktop Action run"]
    assert qr["Name"] == "Read QR"  # no settings, so no "…"
    compress = menus["hala-tool-compress.desktop"]["Desktop Action run"]
    assert compress["Name"] == "Compress…"
    assert shlex.split(compress["Exec"])[1:4] == ["run", "--tool", "compress"]
    from hala.tools import tools

    tool_menus = [n for n in menus if n.startswith("hala-tool-")]
    assert sorted(tool_menus) == sorted(f"hala-tool-{name}.desktop"
                                        for name, t in tools().items() if not t.hidden)
    assert "hala-tool-zip.desktop" not in menus  # an action bubble, and Ark has Compress
