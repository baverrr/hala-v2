"""Phase 6: the wheel's action bubbles, zip/copy/move/phone, and the shelf."""

from __future__ import annotations

import os
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication

from hala import actions
from hala.actions import actions_for, copy_to, move_to
from hala.cli import build_parser, cmd_action, cmd_shelf
from hala.gui import dropzone, shelf
from hala.gui.geometry import ACTION_PREFIX, BUBBLE_RADIUS, place
from hala.gui.overlay import Overlay
from hala.runner import ConversionError
from hala.tools import run_tool, tool, tools_for

_app = QApplication.instance() or QApplication([])
assert isinstance(_app, QApplication)
APP: QApplication = _app


@pytest.fixture(autouse=True)
def private_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the user's real logs and last-used folder out of the tests."""
    from hala import cli, desktop
    from hala.gui import pickers

    monkeypatch.setattr(desktop, "LOG_FILE", tmp_path / "last-run.log")
    monkeypatch.setattr(pickers, "LAST_FOLDER", tmp_path / "last-folder")
    monkeypatch.setattr(shelf, "LOG", tmp_path / "shelf.log")
    monkeypatch.setattr(cli, "CRASH_LOG", tmp_path / "crash.log")


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("alpha")
    (src / "b.jpg").write_bytes(b"\xff\xd8 not really")
    (src / "folder").mkdir()
    (src / "folder" / "inner.txt").write_text("inner")
    dest = tmp_path / "dest"
    dest.mkdir()
    return {"a": src / "a.txt", "b": src / "b.jpg", "folder": src / "folder", "dest": dest}


# --- geometry -------------------------------------------------------------------------------


def test_bubbles_sit_below_the_ring_and_are_hit() -> None:
    wheel = place(["jpg", "png"], 1000, 300, 1400, 900, actions=["shelf", "copy"])
    (t1, x1, y1), (t2, x2, y2) = wheel.bubbles
    assert (t1, t2) == (ACTION_PREFIX + "shelf", ACTION_PREFIX + "copy")
    assert y1 == y2 > wheel.cy + wheel.outer and x1 < wheel.cx < x2
    assert wheel.target_at(x1, y1) == "action:shelf"
    assert wheel.target_at(x2 + BUBBLE_RADIUS - 2, y2) == "action:copy"
    assert wheel.target_at(x1, y1 + 3 * BUBBLE_RADIUS) is None  # below the row


def test_bubbles_go_above_when_the_wheel_is_low() -> None:
    wheel = place(["jpg"], 1000, 890, 1400, 900, actions=["shelf"])
    _t, _x, y = wheel.bubbles[0]
    assert y < wheel.cy - wheel.outer
    assert y - BUBBLE_RADIUS > 0 and wheel.cy + wheel.outer < 900


def test_wheel_with_bubbles_fits_on_screen() -> None:
    for y in (0, 300, 450, 600, 900):
        wheel = place(["jpg"] * 12, 1400, y, 1400, 900, tools=["a", "b"],
                      actions=list(actions.ACTIONS))
        for _t, x, by in wheel.bubbles:
            assert x - BUBBLE_RADIUS >= 0 and x + BUBBLE_RADIUS <= 1400
            assert by - BUBBLE_RADIUS >= 0 and by + BUBBLE_RADIUS <= 900


def test_which_actions_apply(files: dict[str, Path]) -> None:
    assert actions_for([files["a"]]) == ["shelf", "phone", "copy", "move"]
    assert "zip" in actions_for([files["a"], files["b"]])  # one archive for several
    assert "phone" in actions_for([files["folder"]])  # zipped before KDE Connect sends it
    assert actions_for([]) == []


# --- overlay and drop zone ------------------------------------------------------------------


def test_drop_on_a_bubble_launches_the_action(files: dict[str, Path]) -> None:
    launched: list = []
    overlay = Overlay(APP.primaryScreen(), lambda t, f: launched.append((t, f)))
    overlay.resize(1400, 900)
    overlay.begin([files["a"], files["b"]], QPointF(1399, 300))
    assert overlay.wheel is not None
    _target, x, y = next(b for b in overlay.wheel.bubbles if b[0] == "action:zip")
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(files["a"]))])
    overlay.dropEvent(QDropEvent(QPointF(x, y), Qt.DropAction.CopyAction, data,
                                 Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier))
    assert launched == [("action:zip", [files["a"], files["b"]])]


def test_unconvertible_files_still_get_actions_and_paint(files: dict[str, Path]) -> None:
    overlay = Overlay(APP.primaryScreen(), lambda t, f: None)
    overlay.resize(1400, 900)
    overlay.begin([files["folder"], files["a"]], QPointF(1399, 300))
    assert overlay.wheel is not None and overlay.wheel.actions
    overlay.hovered = "action:shelf"
    assert not overlay.grab().isNull()


def test_launch_runs_hala_action(monkeypatch: pytest.MonkeyPatch, files: dict[str, Path]) -> None:
    calls: list = []
    monkeypatch.setattr(dropzone.subprocess, "Popen", lambda cmd, **_k: calls.append(cmd))
    dropzone.launch("action:copy", [files["a"]])
    assert calls[0][1:] == ["-I", "-m", "hala.cli", "action", "copy", str(files["a"])]


# --- zip ------------------------------------------------------------------------------------


def test_zip_packs_everything_into_one_archive(files: dict[str, Path]) -> None:
    zipper = tool("zip")
    assert zipper.hidden and zipper not in tools_for([files["a"], files["b"]])
    out = run_tool(zipper, [files["a"], files["b"], files["folder"]])
    assert out.name == "Archive.zip" and out.parent == files["a"].parent
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == ["a.txt", "b.jpg", "folder/", "folder/inner.txt"]
    again = run_tool(zipper, [files["a"], files["b"]])
    assert again.name == "Archive (2).zip"


def test_zip_across_folders_keeps_relative_paths(files: dict[str, Path], tmp_path: Path) -> None:
    other = tmp_path / "other.txt"
    other.write_text("o")
    twin = files["dest"] / "a.txt"  # same name as src/a.txt: must not collide
    twin.write_text("twin")
    dash = tmp_path / "-rf"  # must not be read as a 7z switch
    dash.write_text("d")
    with zipfile.ZipFile(run_tool(tool("zip"), [files["a"], other, twin, dash])) as z:
        assert sorted(z.namelist()) == ["-rf", "dest/a.txt", "other.txt", "src/a.txt"]


# --- copy / move ----------------------------------------------------------------------------


def test_copy_never_overwrites(files: dict[str, Path]) -> None:
    first = copy_to(files["a"], files["dest"])
    second = copy_to(files["a"], files["dest"])
    assert (first.name, second.name) == ("a.txt", "a (2).txt")
    assert files["a"].read_text() == second.read_text() == "alpha"
    copied = copy_to(files["folder"], files["dest"])
    assert (copied / "inner.txt").read_text() == "inner"
    assert not list(files["dest"].glob(".hala-*"))


def test_move_on_the_same_disk(files: dict[str, Path]) -> None:
    (files["dest"] / "a.txt").write_text("already here")
    moved = move_to(files["a"], files["dest"])
    assert moved.name == "a (2).txt" and moved.read_text() == "alpha"
    assert not files["a"].exists()
    assert (files["dest"] / "a.txt").read_text() == "already here"
    folder = move_to(files["folder"], files["dest"])
    assert (folder / "inner.txt").exists() and not files["folder"].exists()


def test_move_across_disks_copies_then_deletes(files: dict[str, Path],
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    real_stat = Path.stat

    def other_disk(self: Path, **kwargs: object) -> os.stat_result:
        result = real_stat(self, **kwargs)  # type: ignore[arg-type]
        if self == files["dest"]:
            fields = list(result)
            fields[stat.ST_DEV] += 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", other_disk)
    moved = move_to(files["folder"], files["dest"])
    assert (moved / "inner.txt").read_text() == "inner" and not files["folder"].exists()


def test_move_and_copy_refuse_bad_targets(files: dict[str, Path]) -> None:
    assert move_to(files["a"], files["a"].parent) == files["a"]  # already there: nothing to do
    with pytest.raises(ConversionError, match="inside itself"):
        move_to(files["folder"], files["folder"] / "deeper")
    with pytest.raises(ConversionError, match="no longer exists"):
        copy_to(files["dest"] / "ghost.txt", files["dest"])


# --- KDE Connect ----------------------------------------------------------------------------


def fake_kdeconnect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, listing: str,
                    share_exit: int = 0) -> Path:
    log = tmp_path / "kdeconnect.log"
    script = tmp_path / "bin" / "kdeconnect-cli"
    script.parent.mkdir()
    script.write_text(f"""#!/bin/sh
echo "$@" >> {log}
case "$1" in --list-available) printf '{listing}';; esac
[ "$3" = "--share" ] && exit {share_exit}
exit 0
""")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{script.parent}:{os.environ['PATH']}")
    return log


def test_phones_and_sending(files: dict[str, Path], tmp_path: Path,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    log = fake_kdeconnect(tmp_path, monkeypatch, "abc123 My Pixel\\nzz9 Tablet\\n")
    assert actions.phones() == [("abc123", "My Pixel"), ("zz9", "Tablet")]
    assert actions.send_to_phone([files["a"], files["b"]], "abc123") == []
    sent = log.read_text().splitlines()[1:]
    assert sent == [f"--device abc123 --share {files['a']}",
                    f"--device abc123 --share {files['b']}"]
    monkeypatch.setattr(actions, "PHONE_ZIPS", tmp_path / "phone")
    assert actions.send_to_phone([files["folder"]], "abc123") == []  # zipped, then sent
    zipped = tmp_path / "phone" / "folder.zip"
    assert log.read_text().splitlines()[-1] == f"--device abc123 --share {zipped}"
    with zipfile.ZipFile(zipped) as z:
        assert "folder/inner.txt" in z.namelist()


def test_send_failure_is_explained(files: dict[str, Path], tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    fake_kdeconnect(tmp_path, monkeypatch, "", share_exit=1)
    assert actions.phones() == []
    [(path, reason)] = actions.send_to_phone([files["a"]], "abc123")
    assert path == files["a"] and "exit code 1" in reason


def test_run_action_phone_without_devices_notifies(files: dict[str, Path], tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop

    fake_kdeconnect(tmp_path, monkeypatch, "")
    shown: list = []
    monkeypatch.setattr(desktop, "notify", lambda *a, **k: shown.append((a, k)))
    assert desktop.run_action("phone", [files["a"]]) == 1
    assert shown[0][0][0] == "No phone connected"
    assert shown[0][1]["actions"] == ["kdeconnect", "Open KDE Connect"]


def test_run_action_copy_reports(files: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop

    shown: list = []
    monkeypatch.setattr(desktop, "notify", lambda *a, **k: shown.append(a))
    ghost = files["a"].parent / "ghost.txt"
    assert desktop.run_action("copy", [files["a"], ghost], folder=files["dest"]) == 1
    assert shown[0][0] == "Copied 1 of 2 files" and "ghost.txt" in shown[0][1]
    assert (files["dest"] / "a.txt").exists()
    shown.clear()
    assert desktop.run_action("move", [files["b"]], folder=files["dest"]) == 0
    assert shown[0][0] == "Moved to dest"


# --- shelf ----------------------------------------------------------------------------------


def test_shelf_protocol_round_trip(tmp_path: Path) -> None:
    paths = [Path("/a b/ü.txt"), Path("/x/y")]
    assert shelf.decode(shelf.encode(paths)) == paths
    assert shelf.decode(b"relative/path\n/ok\n") == [Path("/ok")]  # absolute paths only
    assert shelf.encode([Path("/bad\nname")]) == b""


def test_shelf_adds_dedupes_and_prunes(files: dict[str, Path]) -> None:
    s = shelf.Shelf()
    assert s.add([files["a"], files["b"], files["a"], files["dest"] / "ghost"]) == 2
    assert s.files() == [files["a"], files["b"]]
    assert "2 items" in s.title.text()
    files["b"].unlink()  # e.g. moved away by a drag target
    s.prune()
    assert s.files() == [files["a"]]
    s.remove([files["a"]])
    assert s.files() == [] and s.hint.isVisibleTo(s)


def test_shelf_drag_out_carries_urls_and_prunes(files: dict[str, Path],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    s = shelf.Shelf()
    s.add([files["a"], files["b"], files["folder"]])
    dragged: list = []

    def exec_(drag: object, *_a: object) -> Qt.DropAction:
        dragged.append([u.toLocalFile() for u in drag.mimeData().urls()])  # type: ignore[attr-defined]
        files["b"].unlink()  # the drop target moved it away
        return Qt.DropAction.MoveAction

    monkeypatch.setattr(shelf.QDrag, "exec", exec_)
    s.drag_out(s.files())
    assert dragged == [[str(files["a"]), str(files["b"]), str(files["folder"])]]
    assert s.files() == [files["a"], files["folder"]]  # kept, except the one that left
    files["a"].unlink()
    s.drag_out([files["a"]])  # only missing files: no drag at all, just tidied away
    assert len(dragged) == 1 and s.files() == [files["folder"]]


def test_shelf_ignores_its_own_drags(files: dict[str, Path],
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    s = shelf.Shelf()
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(files["a"]))])
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, data,
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    monkeypatch.setattr(QDropEvent, "source", lambda _e: s)  # a drag started by the shelf
    s.dropEvent(event)
    assert s.files() == []


def test_shelf_accepts_drops_from_other_apps(files: dict[str, Path]) -> None:
    s = shelf.Shelf()
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(files["a"])), QUrl("https://example.com/x")])
    event = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                       data,
                       Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier)
    s.dropEvent(event)
    assert s.files() == [files["a"]]
    assert event.dropAction() == Qt.DropAction.CopyAction  # never ask the source to delete


def test_shelf_server_receives_files(files: dict[str, Path], tmp_path: Path) -> None:
    sock = tmp_path / "shelf.sock"
    s = shelf.Shelf()
    server = shelf.Server(s, shelf.listen(sock), sock)
    try:
        assert shelf.send([files["a"], files["b"]], sock)
        for _ in range(50):
            APP.processEvents()
            if s.files():
                break
        assert s.files() == [files["a"], files["b"]]
        assert stat.S_IMODE(sock.stat().st_mode) == 0o600
    finally:
        server.close()
    assert not sock.exists()
    assert not shelf.send([files["a"]], sock)  # nobody listening


class FakeProc:
    def __init__(self, code: int | None):
        self.code = code

    def poll(self) -> int | None:
        return self.code


def test_put_starts_a_shelf_when_none_runs(files: dict[str, Path],
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    answers = iter([False, False, True])  # not running; starting; listening
    monkeypatch.setattr(shelf, "send", lambda _f, *a: next(answers))
    monkeypatch.setattr(shelf.subprocess, "Popen",
                        lambda cmd, **_k: calls.append(cmd) or FakeProc(None))
    monkeypatch.setattr(shelf.time, "sleep", lambda _s: None)
    assert shelf.put([files["a"]]) == 0
    assert calls[0][1:] == ["-I", "-m", "hala.cli", "shelf", str(files["a"])]


def test_put_reports_a_shelf_that_never_starts(files: dict[str, Path],
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop

    shown: list = []
    monkeypatch.setattr(desktop, "notify", lambda *a, **k: shown.append((a, k)))
    monkeypatch.setattr(shelf, "send", lambda _f, *a: False)
    monkeypatch.setattr(shelf.subprocess, "Popen", lambda cmd, **_k: FakeProc(1))  # crashed
    assert shelf.put([files["a"]]) == 1
    assert shown[0][0][0] == "Couldn't open the shelf" and shown[0][1]["problem"]


# --- CLI and menus --------------------------------------------------------------------------


def test_cli_routes_action_and_shelf() -> None:
    args = build_parser().parse_args(["action", "copy", "a.txt", "--to", "/tmp"])
    assert args.func is cmd_action and args.to == "/tmp"
    assert build_parser().parse_args(["shelf"]).func is cmd_shelf
    with pytest.raises(SystemExit):
        build_parser().parse_args(["action", "explode", "a.txt"])


def test_shelf_menu_for_every_file() -> None:
    from hala import servicemenus

    text = servicemenus.build_menus("/usr/bin/hala")["hala-shelf.desktop"]
    assert "MimeType=all/all;" in text and 'Exec="/usr/bin/hala" action shelf %F' in text


def test_cli_zip_tool_works_from_the_command_line(files: dict[str, Path]) -> None:
    out = subprocess.run([str(Path(__file__).parents[1] / ".venv" / "bin" / "hala"), "tool",
                          "zip", str(files["a"]), str(files["b"])],
                         capture_output=True, text=True, check=True, timeout=60).stdout
    assert "Archive.zip" in out


def test_tray_has_show_shelf(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list = []
    monkeypatch.setattr(dropzone, "launch", lambda t, f: calls.append((t, f)))
    zone = dropzone.DropZone(APP, "right")
    action = next(a for a in zone._menu.actions() if a.text() == "Show shelf")
    action.trigger()
    assert calls == [("action:shelf", [])]  # detached: the tray never waits on the shelf
    for strip, overlay in zone.pairs:
        strip.close()
        overlay.close()
    zone.tray.hide()




# --- regressions from the phase 6 review ----------------------------------------------------


@pytest.fixture
def link(files: dict[str, Path]) -> Path:
    path = files["a"].parent / "link.txt"
    path.symlink_to(files["a"])
    return path


def test_moving_a_symlink_moves_the_link_not_its_target(
        files: dict[str, Path], link: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop

    monkeypatch.setattr(desktop, "notify", lambda *a, **k: None)
    assert desktop.run_action("move", [link], folder=files["dest"]) == 0
    moved = files["dest"] / "link.txt"
    assert moved.is_symlink() and os.readlink(moved) == str(files["a"])
    assert files["a"].read_text() == "alpha" and not os.path.lexists(link)


def test_symlink_to_a_folder_moves_and_copies_as_a_link(files: dict[str, Path]) -> None:
    dir_link = files["a"].parent / "dirlink"
    dir_link.symlink_to(files["folder"])
    copied = copy_to(dir_link, files["dest"])
    assert copied.is_symlink() and os.readlink(copied) == str(files["folder"])
    moved = move_to(dir_link, files["dest"])
    assert moved.name == "dirlink (2)" and moved.is_symlink()
    assert (files["folder"] / "inner.txt").exists()


def other_disk_for(monkeypatch: pytest.MonkeyPatch, dest: Path) -> None:
    real_stat = Path.stat

    def stat_(self: Path, **kwargs: object) -> os.stat_result:
        result = real_stat(self, **kwargs)  # type: ignore[arg-type]
        if self == dest:
            fields = list(result)
            fields[stat.ST_DEV] += 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(Path, "stat", stat_)


def test_failed_cross_disk_move_keeps_the_original(files: dict[str, Path],
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    import errno
    import shutil

    other_disk_for(monkeypatch, files["dest"])

    def full(*_a: object, **_k: object) -> None:
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(shutil, "copytree", full)
    with pytest.raises(ConversionError, match="No space left"):
        move_to(files["folder"], files["dest"])
    assert (files["folder"] / "inner.txt").exists()
    assert list(files["dest"].iterdir()) == []


def test_move_that_cannot_delete_the_original_is_partial(files: dict[str, Path],
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil

    from hala import desktop
    from hala.actions import PartialMove

    other_disk_for(monkeypatch, files["dest"])

    real_rmtree = shutil.rmtree

    def stuck(path: object, *a: object, **k: object) -> None:
        if Path(str(path)) == files["folder"]:  # only the original; scratch cleanup works
            raise PermissionError(13, "Permission denied")
        real_rmtree(path, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(shutil, "rmtree", stuck)
    with pytest.raises(PartialMove) as caught:
        move_to(files["folder"], files["dest"])
    assert (caught.value.placed / "inner.txt").exists() and files["folder"].exists()
    shown: list = []
    monkeypatch.setattr(desktop, "notify", lambda *a, **k: shown.append(a))
    (files["dest"] / "folder").rename(files["dest"] / "first")
    assert desktop.run_action("move", [files["folder"]], folder=files["dest"]) == 1
    summary, body, _icon, done = shown[0][:4]
    assert summary == "Moved 1 of 1 file" and "couldn't be removed" in body
    assert done == [files["dest"] / "folder"]  # Show in folder still offered


def test_bind_mount_move_falls_back_to_copy(files: dict[str, Path],
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    import errno

    def cross(*_a: object, **_k: object) -> None:
        raise OSError(errno.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", cross)
    monkeypatch.setattr(os, "rename", cross)
    moved = move_to(files["a"], files["dest"])  # same st_dev, but no renames across
    assert moved.read_text() == "alpha" and not files["a"].exists()


def test_folder_name_collision_on_move(files: dict[str, Path]) -> None:
    (files["dest"] / "folder").mkdir()
    (files["dest"] / "folder" / "keep.txt").write_text("keep")
    moved = move_to(files["folder"], files["dest"])
    assert moved.name == "folder (2)" and (moved / "inner.txt").exists()
    assert (files["dest"] / "folder" / "keep.txt").read_text() == "keep"


def test_zip_of_a_folder_and_something_inside_it(files: dict[str, Path]) -> None:
    out = run_tool(tool("zip"), [files["folder"] / "inner.txt", files["folder"], files["a"]])
    assert out.parent == files["a"].parent  # not inside the folder being zipped
    with zipfile.ZipFile(out) as z:
        names = z.namelist()
    assert not any(".hala-" in n for n in names)
    assert sorted(names) == ["a.txt", "folder/", "folder/inner.txt"]


def test_zip_takes_awkward_names(files: dict[str, Path]) -> None:
    odd = files["a"].parent / "@list"
    odd.write_text("not a list file")
    with zipfile.ZipFile(run_tool(tool("zip"), [odd, files["a"]])) as z:
        assert z.read("@list") == b"not a list file"


@pytest.mark.parametrize("size", [(1366, 768), (1280, 720), (1536, 864), (1920, 1080)])
@pytest.mark.parametrize("count, ring_tools", [(12, 2), (24, 0), (24, 2), (16, 3)])
def test_bubbles_stay_on_small_screens(size: tuple[int, int], count: int,
                                       ring_tools: int) -> None:
    from hala.gui.geometry import BUBBLE_LABEL

    width, height = size
    for x in (0, width):
        for y in range(0, height + 1, 48):
            wheel = place(["jpg"] * count, x, y, width, height,
                          tools=[f"t{i}" for i in range(ring_tools)],
                          actions=list(actions.ACTIONS))
            for _t, bx, by in wheel.bubbles:
                top = by - BUBBLE_RADIUS - (BUBBLE_LABEL if wheel.actions_above else 0)
                bottom = by + BUBBLE_RADIUS + (0 if wheel.actions_above else BUBBLE_LABEL)
                assert top >= 0 and bottom <= height and bx - BUBBLE_RADIUS >= 0
                assert bx + BUBBLE_RADIUS <= width


def test_a_drop_on_a_label_picks_the_action_not_the_petal() -> None:
    from hala.gui.geometry import BUBBLE_LABEL

    above = place(["jpg"] * 12, 1000, 890, 1400, 900, actions=["shelf", "copy"])
    assert above.actions_above
    _t, x, y = above.bubbles[0]
    assert above.target_at(x, y - BUBBLE_RADIUS - BUBBLE_LABEL / 2) == "action:shelf"
    below = place(["jpg"] * 12, 1000, 300, 1400, 900, actions=["shelf", "copy"])
    _t, x, y = below.bubbles[1]
    assert below.target_at(x, y + BUBBLE_RADIUS + BUBBLE_LABEL / 2) == "action:copy"


def test_shelf_message_cut_off_mid_path_is_dropped() -> None:
    assert shelf.decode(b"/tmp/a\n/home/jef") == [Path("/tmp/a")]
    assert shelf.decode(b"/home/jef") == []


def test_stale_socket_is_replaced_and_lock_is_exclusive(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    stale = tmp_path / "s.sock"
    stale.write_text("left behind")
    sock = shelf.listen(stale)
    try:
        assert stat.S_ISSOCK(stale.stat().st_mode)
        assert stat.S_IMODE(stale.stat().st_mode) == 0o600
    finally:
        sock.close()
    monkeypatch.setattr(shelf, "LOCK", tmp_path / "shelf.lock")
    first = shelf._lock()
    assert first is not None and shelf._lock() is None
    first.close()


def test_second_shelf_waits_for_the_first(files: dict[str, Path],
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter([False, False, False, True])
    sent: list = []
    monkeypatch.setattr(shelf, "send", lambda f, *a: sent.append(f) or next(answers))
    monkeypatch.setattr(shelf, "_lock", lambda: None)  # the first shelf holds it
    monkeypatch.setattr(shelf.time, "sleep", lambda _s: None)
    assert shelf.main([files["a"]]) == 0
    assert sent[-1] == [files["a"]]
    monkeypatch.setattr(shelf, "send", lambda f, *a: False)
    monkeypatch.setattr(shelf, "START_WAIT_S", 0.0)
    with pytest.raises(ConversionError, match="isn't answering"):
        shelf.main([files["a"]])


def test_prune_keeps_files_on_a_briefly_unreachable_mount(
        files: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    s = shelf.Shelf()
    s.add([files["a"]])
    real = os.lstat
    monkeypatch.setattr(shelf.os, "lstat", lambda p: (_ for _ in ()).throw(
        OSError(116, "Stale file handle")) if Path(p) == files["a"] else real(p))
    s.prune()
    assert s.files() == [files["a"]]


@pytest.mark.parametrize("devices, picked", [("abc One\n", None), ("abc One\nxyz Two\n", "xyz")])
def test_run_action_phone_picks_a_device(files: dict[str, Path], tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch, devices: str,
                                         picked: str | None) -> None:
    from hala import desktop
    from hala.gui import pickers

    log = fake_kdeconnect(tmp_path, monkeypatch, devices)
    shown: list = []
    monkeypatch.setattr(desktop, "notify", lambda *a, **k: shown.append(a))
    monkeypatch.setattr(pickers, "pick_device", lambda found: picked)
    assert desktop.run_action("phone", [files["a"]]) == 0
    assert f"--device {picked or 'abc'} --share" in log.read_text()
    assert shown[0][0] == f"Sending to {'Two' if picked else 'One'}"


def test_run_action_phone_reports_partial_sends(files: dict[str, Path], tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop

    fake_kdeconnect(tmp_path, monkeypatch, "abc One\n")
    shown: list = []
    monkeypatch.setattr(desktop, "notify", lambda *a, **k: shown.append(a))
    gone = files["dest"] / "ghost.txt"  # deleted between choosing and sending
    assert desktop.run_action("phone", [files["a"], gone]) == 1
    assert shown[0][0] == "Sending 1 of 2 files to One" and "ghost.txt" in shown[0][1]


def test_run_action_routes_zip_shelf_and_cancelled_pickers(
        files: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop
    from hala.gui import pickers

    seen: list = []
    monkeypatch.setattr(desktop, "run", lambda f, t, **k: seen.append(("run", f, t, k)) or 0)
    monkeypatch.setattr(shelf, "put", lambda f: seen.append(("shelf", f)) or 0)
    monkeypatch.setattr(pickers, "pick_folder", lambda *a: None)
    assert desktop.run_action("zip", [files["a"], files["b"]]) == 0
    assert seen[0] == ("run", [files["a"], files["b"]], None, {"tool_name": "zip", "ask": False})
    assert desktop.run_action("shelf", []) == 0 and seen[1] == ("shelf", [])
    assert desktop.run_action("copy", [files["a"]]) == 1  # picker cancelled: nothing done
    assert list(files["dest"].iterdir()) == []


def test_notification_text_survives_undecodable_names() -> None:
    from hala.desktop import _esc

    assert _esc("caf\udce9 & co.txt") == "caf\ufffd &amp; co.txt"


def test_detached_command_crash_is_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    from hala import cli, desktop

    def boom(*_a: object, **_k: object) -> int:
        raise RuntimeError("something odd")

    monkeypatch.setattr(desktop, "run_action", boom)
    assert cli.main(["action", "copy", "x.txt", "--to", str(tmp_path)]) == 1
    assert "something odd" in (tmp_path / "crash.log").read_text()
    assert "RuntimeError" in capsys.readouterr().err
