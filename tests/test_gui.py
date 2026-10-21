"""The drop zone and format wheel, on Qt's offscreen platform (no compositor needed)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication

from hala.cli import main
from hala.gui import dropzone, layershell
from hala.gui.geometry import INNER_RADIUS, TOOL_RING, outer_radius, place
from hala.gui.overlay import Overlay

# --- geometry -------------------------------------------------------------------------------


def test_segments_cover_the_circle_clockwise_from_top() -> None:
    wheel = place(["a", "b", "c", "d"], 500, 500, 1000, 1000)
    assert [s.start for s in wheel.segments] == [0, 90, 180, 270]
    mid = (wheel.inner + wheel.outer) / 2
    assert wheel.target_at(500, 500 - mid) == "a"  # 12 o'clock
    assert wheel.target_at(500 + mid, 500) == "b"  # 3 o'clock
    assert wheel.target_at(500, 500 + mid) == "c"
    assert wheel.target_at(500 - mid, 500) == "d"


def test_middle_and_far_away_cancel() -> None:
    wheel = place(["a", "b"], 500, 500, 1000, 1000)
    assert wheel.target_at(500, 500) is None
    assert wheel.target_at(500, 500 - INNER_RADIUS + 1) is None
    assert wheel.target_at(500, 500 - wheel.outer - 30) == "a"  # a little past the ring
    assert wheel.target_at(500, 500 - wheel.outer - 200) is None


def test_wheel_opens_inside_the_screen_edge() -> None:
    targets = ["jpg"] * 11
    wheel = place(targets, 1919, 5, 1920, 1080)
    reach = outer_radius(11) + 24
    assert wheel.cx == pytest.approx(1920 - reach) and wheel.cy == pytest.approx(reach)
    assert wheel.cx + wheel.outer < 1920 and wheel.cy - wheel.outer > 0


def test_radius_grows_with_targets() -> None:
    assert outer_radius(3) < outer_radius(20)


def test_tiny_screen_centres_the_wheel() -> None:
    wheel = place(["a"] * 30, 10, 10, 300, 200)
    assert (wheel.cx, wheel.cy) == (150, 100)


# --- overlay --------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app() -> QApplication:
    instance = QApplication.instance()
    return instance if isinstance(instance, QApplication) else QApplication([])


def mime(*paths: Path) -> QMimeData:
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return data


def drag_and_drop(overlay: Overlay, data: QMimeData, at: QPointF) -> None:
    buttons, mods = Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
    action = Qt.DropAction.CopyAction
    QApplication.sendEvent(overlay, QDragEnterEvent(at.toPoint(), action, data, buttons, mods))
    QApplication.sendEvent(overlay, QDragMoveEvent(at.toPoint(), action, data, buttons, mods))
    QApplication.sendEvent(overlay, QDropEvent(at, action, data, buttons, mods))


def test_no_layer_shell_offscreen(app: QApplication) -> None:
    assert not layershell.available()


def test_drop_on_segment_launches(app: QApplication, tmp_path: Path) -> None:
    launched: list[tuple[str, list[Path]]] = []
    overlay = Overlay(app.primaryScreen(), lambda t, files: launched.append((t, files)))
    pic = tmp_path / "pic.png"
    pic.write_bytes(b"")
    overlay.begin([pic], QPointF(overlay.width() - 1, overlay.height() / 2))
    wheel = overlay.wheel
    assert wheel is not None and wheel.targets[0] == "jpg"
    x, y = wheel.point_at(wheel.segments[0].middle, (wheel.inner + wheel.outer) / 2)
    drag_and_drop(overlay, mime(pic), QPointF(x, y))
    assert launched == [("jpg", [pic])]
    assert not overlay.isVisible() and overlay.files == []


def test_drop_in_the_middle_cancels(app: QApplication, tmp_path: Path) -> None:
    launched: list[tuple[str, list[Path]]] = []
    overlay = Overlay(app.primaryScreen(), lambda t, files: launched.append((t, files)))
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"")
    overlay.begin([clip], QPointF(400, 300))
    assert overlay.wheel is not None
    drag_and_drop(overlay, mime(clip), QPointF(overlay.wheel.cx, overlay.wheel.cy))
    assert launched == [] and not overlay.isVisible()


def test_mixed_files_offer_common_targets(app: QApplication, tmp_path: Path) -> None:
    overlay = Overlay(app.primaryScreen(), lambda *_: None)
    files = [tmp_path / "a.png", tmp_path / "b.mp3"]
    overlay.begin(files, QPointF(10, 10))
    assert overlay.wheel is not None and overlay.wheel.targets == ("zip", "tar.gz", "7z")
    overlay.finish(None)


def test_click_mode(app: QApplication, tmp_path: Path) -> None:
    from PySide6.QtTest import QTest

    launched: list[tuple[str, list[Path]]] = []
    overlay = Overlay(app.primaryScreen(), lambda t, files: launched.append((t, files)))
    song = tmp_path / "song.wav"
    overlay.begin([song], QPointF(500, 400), click_mode=True)
    wheel = overlay.wheel
    assert wheel is not None
    x, y = wheel.point_at(wheel.segments[1].middle, (wheel.inner + wheel.outer) / 2)
    QTest.mouseClick(overlay, Qt.MouseButton.LeftButton, pos=QPoint(int(x), int(y)))
    assert launched == [(wheel.targets[1], [song])]


def test_painting_does_not_crash(app: QApplication, tmp_path: Path) -> None:
    overlay = Overlay(app.primaryScreen(), lambda *_: None)
    overlay.begin([tmp_path / "a.png"], QPointF(300, 300))
    overlay.hovered = overlay.wheel.targets[2] if overlay.wheel else None
    assert not overlay.grab().isNull()
    overlay.begin([], QPointF(300, 300))  # only non-local files were dragged
    assert not overlay.grab().isNull()
    overlay.finish(None)


# --- drop zone ------------------------------------------------------------------------------

def test_strip_hands_the_drag_to_the_overlay(app: QApplication, tmp_path: Path) -> None:
    overlay = Overlay(app.primaryScreen(), lambda *_: None)
    strip = dropzone.Strip(app.primaryScreen(), overlay, "right")
    pic = tmp_path / "pic.png"
    data = mime(pic)  # the event doesn't own it; keep it alive
    QApplication.sendEvent(strip, QDragEnterEvent(
        QPoint(2, 2), Qt.DropAction.CopyAction, data, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    assert overlay.isVisible() and overlay.files == [pic]
    assert overlay.wheel is not None and overlay.wheel.cx > overlay.width() / 2
    overlay.finish(None)


def test_single_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dropzone, "LOCK_FILE", tmp_path / "dz.lock")
    first = dropzone._single_instance()
    assert first is not None
    assert dropzone._single_instance() is None
    first.close()
    second = dropzone._single_instance()
    assert second is not None
    second.close()


def test_autostart_on_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                          capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(dropzone, "AUTOSTART", tmp_path / "autostart" / "hala-dropzone.desktop")
    assert main(["dropzone", "--autostart", "on", "--edge", "left"]) == 0
    text = dropzone.AUTOSTART.read_text()
    assert "dropzone --edge left" in text
    assert main(["dropzone", "--autostart", "off"]) == 0
    assert not dropzone.AUTOSTART.exists()
    assert "disabled" in capsys.readouterr().out


def test_launch_runs_hala_run(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(dropzone.subprocess, "Popen", lambda cmd, **_: calls.append(cmd))
    dropzone.launch("webp", [Path("/x/a.png"), Path("/x/-b.png")])
    assert calls[0][1:] == ["-I", "-m", "hala.cli", "run", "-t", "webp", "/x/a.png", "/x/-b.png"]


def test_size_text(tmp_path: Path) -> None:
    from hala.gui.preview import human_size, total_size

    assert human_size(369) == "369 bytes"
    assert human_size(2_200_000) == "2.2 MB"
    (tmp_path / "a").write_bytes(b"x" * 1500)
    assert total_size([tmp_path / "a"]) == 1500
    assert total_size([tmp_path]) is None  # folders aren't walked


def test_tool_ring_geometry() -> None:
    wheel = place(["jpg", "png"], 500, 500, 1000, 1000, tools=["compress", "metadata"])
    assert wheel.split == pytest.approx(INNER_RADIUS + TOOL_RING)
    tool_mid = (wheel.inner + wheel.split) / 2
    format_mid = (wheel.split + wheel.outer) / 2
    assert wheel.target_at(500, 500 - tool_mid) == "tool:compress"
    assert wheel.target_at(500, 500 + tool_mid) == "tool:metadata"
    assert wheel.target_at(500, 500 - format_mid) == "jpg"
    assert wheel.target_at(500, 500) is None


def test_drop_on_tool_petal_launches_tool(app: QApplication, tmp_path: Path) -> None:
    launched: list[tuple[str, list[Path]]] = []
    overlay = Overlay(app.primaryScreen(), lambda t, files: launched.append((t, files)))
    clip = tmp_path / "clip.mp4"
    overlay.begin([clip], QPointF(500, 450))
    wheel = overlay.wheel
    assert wheel is not None and wheel.tools[0] == "compress"
    seg = wheel.tool_segments[0]
    x, y = wheel.point_at(seg.middle, (wheel.inner + wheel.split) / 2)
    drag_and_drop(overlay, mime(clip), QPointF(x, y))
    assert launched == [("tool:compress", [clip])]


def test_launch_runs_a_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(dropzone.subprocess, "Popen", lambda cmd, **_: calls.append(cmd))
    dropzone.launch("tool:trim", [Path("/x/a.mp4")])
    assert calls[0][4:] == ["run", "--tool", "trim", "/x/a.mp4"]



def test_mp4_replaces_bmp_as_an_image_target() -> None:
    from hala.registry import targets_for

    assert "bmp" not in targets_for("png") and "mp4" in targets_for("bmp")
    assert targets_for("png").index("mp4") == 4  # BMP's old slot, after TIFF
