"""The image editor: the step document, redaction, saving, and the window on Qt's offscreen."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import subprocess

from PySide6.QtCore import QPointF, QRect, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from hala.editing import Document, Step, load_image, output_ext, render, save_copy
from hala.gui import editor as editor_mod
from hala.gui.editor import ASPECTS, Editor
from hala.runner import ConversionError

APP = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def no_modal_dialogs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Warning boxes would block the test run; record them instead."""
    shown: list[str] = []
    for name in ("warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, name,
                            staticmethod(lambda *a, **_k: shown.append(str(a[2]))))
    return shown


def noisy(width: int = 60, height: int = 40) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    for y in range(height):
        for x in range(width):
            image.setPixelColor(x, y, QColor((x * 37) % 256, (y * 91) % 256, (x * y) % 256))
    return image


def test_crop_rotate_flip_change_the_size() -> None:
    doc = Document(noisy())
    doc.add(Step("crop", rect=QRect(10, 5, 30, 20)))
    assert doc.render().size().toTuple() == (30, 20)
    doc.add(Step("rotate", amount=90))
    assert doc.render().size().toTuple() == (20, 30)
    doc.add(Step("flip", amount=1))
    assert doc.render().size().toTuple() == (20, 30)


def test_undo_redo_and_new_edit_clears_redo() -> None:
    doc = Document(noisy())
    doc.add(Step("crop", rect=QRect(0, 0, 10, 10)))
    doc.add(Step("rotate", amount=90))
    assert doc.undo() and doc.render().size().toTuple() == (10, 10)
    assert doc.can_redo
    doc.add(Step("flip", amount=2))
    assert not doc.can_redo and not doc.redo()
    assert doc.undo() and doc.undo() and not doc.undo()
    assert doc.render().size().toTuple() == (60, 40) and not doc.modified


def test_black_redaction_is_opaque_even_on_transparent_pixels() -> None:
    image = QImage(20, 20, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    doc = Document(image)
    doc.add(Step("black", rect=QRect(5, 5, 10, 10), color=QColor(255, 0, 0, 40)))
    out = doc.render()
    assert out.pixelColor(10, 10) == QColor(0, 0, 0, 255)
    assert out.pixelColor(2, 2).alpha() == 0


def test_pixelate_destroys_detail_and_stays_inside() -> None:
    base = noisy()
    doc = Document(base)
    area = QRect(10, 10, 30, 20)
    doc.add(Step("pixelate", rect=area))
    out = doc.render()
    # Outside the area nothing changed; inside it is blocky (many neighbours now identical).
    assert out.pixelColor(2, 2) == base.pixelColor(2, 2)
    assert out.pixelColor(45, 35) == base.pixelColor(45, 35)
    same = sum(out.pixelColor(x, 20) == out.pixelColor(x + 1, 20) for x in range(10, 39))
    was = sum(base.pixelColor(x, 20) == base.pixelColor(x + 1, 20) for x in range(10, 39))
    assert same > was + 15
    assert all(out.pixelColor(x, y).alpha() == 255 for x in range(10, 40) for y in range(10, 30))


def test_drawing_leaves_a_mark_only_where_drawn() -> None:
    image = QImage(40, 40, QImage.Format.Format_ARGB32)
    image.fill(QColor("white"))
    doc = Document(image)
    red = QColor("#ff0000")
    doc.add(Step("rect", rect=QRect(5, 5, 20, 20), color=red, width=3))
    doc.add(Step("arrow", points=(QPointF(2, 35), QPointF(35, 35)), color=red, width=3))
    doc.add(Step("text", points=(QPointF(2, 2),), text="Hi", color=red, size=12))
    out = doc.render()
    assert out.pixelColor(5, 15) != QColor("white")  # the rectangle's edge
    assert out.pixelColor(30, 35) != QColor("white")  # along the arrow
    assert out.pixelColor(30, 10) == QColor("white")
    assert any(out.pixelColor(x, y) != QColor("white") for x in range(2, 14) for y in range(2, 18))


def test_load_falls_back_to_vips(workdir: dict[str, Path]) -> None:
    assert load_image(workdir["png"]).size().toTuple() == (64, 48)
    for key in ("heic", "svg"):  # Qt may lack HEIC; vips opens both
        assert not load_image(workdir[key]).isNull()
    with pytest.raises(ConversionError):
        load_image(workdir["txt"])


def test_save_copy_never_overwrites_and_keeps_the_format(workdir: dict[str, Path]) -> None:
    src = workdir["png"]
    before = src.read_bytes()
    first = save_copy(load_image(src), src)
    second = save_copy(load_image(src), src)
    assert (first.name, second.name) == ("pic (edited).png", "pic (edited) (2).png")
    assert src.read_bytes() == before
    assert load_image(first).size() == load_image(src).size()
    assert not list(src.parent.glob(".hala-*"))
    assert output_ext(workdir["gif"]) == "png" and output_ext(workdir["jpg"]) == "jpg"


def test_save_as_jpg_flattens_transparency_onto_white(tmp_path: Path) -> None:
    image = QImage(8, 8, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    out = save_copy(image, tmp_path / "x.png", dest=tmp_path / "flat.jpg")
    assert load_image(out).pixelColor(4, 4).red() > 240
    with pytest.raises(ConversionError):
        save_copy(image, tmp_path / "x.png", dest=tmp_path / "nope.xyz")


def drag(editor: Editor, a: tuple[int, int], b: tuple[int, int]) -> None:
    """A mouse drag between two widget positions."""
    canvas = editor.canvas
    for kind, pos in ((QMouseEvent.Type.MouseButtonPress, a), (QMouseEvent.Type.MouseMove, b),
                      (QMouseEvent.Type.MouseButtonRelease, b)):
        event = QMouseEvent(kind, QPointF(*pos), QPointF(*pos), Qt.MouseButton.LeftButton,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        handler = {QMouseEvent.Type.MouseButtonPress: canvas.mousePressEvent,
                   QMouseEvent.Type.MouseMove: canvas.mouseMoveEvent,
                   QMouseEvent.Type.MouseButtonRelease: canvas.mouseReleaseEvent}[kind]
        handler(event)


def test_window_crop_by_dragging_then_save(workdir: dict[str, Path]) -> None:
    editor = Editor(workdir["png"])
    editor.resize(800, 600)
    editor.show()
    APP.processEvents()
    editor._pick_tool("crop")
    scale, origin = editor.canvas._scale_and_origin()
    a = (int(origin.x() + 8 * scale), int(origin.y() + 4 * scale))
    b = (int(origin.x() + 40 * scale), int(origin.y() + 28 * scale))
    drag(editor, a, b)
    assert editor.canvas.crop_rect is not None and not editor.doc.modified
    editor.canvas.apply_crop()
    size = editor.doc.render().size()
    assert abs(size.width() - 32) <= 2 and abs(size.height() - 24) <= 2
    assert editor.windowTitle().startswith("* ")
    assert editor.save()
    assert editor.saved_as is not None and editor.saved_as.name == "pic (edited).png"
    editor.close()


def test_window_redaction_and_undo(workdir: dict[str, Path]) -> None:
    editor = Editor(workdir["png"])
    editor.resize(800, 600)
    editor.show()
    APP.processEvents()
    editor._pick_tool("black")
    scale, origin = editor.canvas._scale_and_origin()
    drag(editor, (int(origin.x() + 10 * scale), int(origin.y() + 10 * scale)),
         (int(origin.x() + 30 * scale), int(origin.y() + 30 * scale)))
    assert editor.doc.steps and editor.doc.steps[-1].kind == "black"
    assert editor.doc.render().pixelColor(20, 20) == QColor(0, 0, 0)
    editor._undo()
    assert not editor.doc.modified
    editor.close()


def test_edit_menu_is_generated_for_pictures() -> None:
    from hala import servicemenus

    menus = servicemenus.build_menus("/usr/bin/hala")
    text = menus["hala-edit.desktop"]
    assert "image/png" in text and "Exec=\"/usr/bin/hala\" edit %F" in text
    assert "text/plain" not in text.split("MimeType=")[1].splitlines()[0]


# --- regressions from the phase 5 review ----------------------------------------------------


def test_render_cache_follows_undo_then_a_different_edit() -> None:
    base = noisy()
    doc = Document(base)
    a, b = Step("rotate", amount=90), Step("flip", amount=1)
    doc.add(a)
    doc.render()
    doc.undo()
    doc.add(b)  # same number of steps as before, different document
    assert doc.render() == render(base, [b])


def test_modified_tracks_the_saved_state_not_the_step_count() -> None:
    doc = Document(noisy())
    doc.add(Step("rotate", amount=90))
    doc.mark_clean()
    assert not doc.modified
    doc.undo()
    assert doc.modified
    doc.add(Step("flip", amount=1))  # back to one step, but not the saved one
    assert doc.modified
    doc.undo()
    doc.redo()
    assert doc.modified


def distinct(width: int = 4, height: int = 3) -> QImage:
    image = QImage(width, height, QImage.Format.Format_ARGB32)
    for y in range(height):
        for x in range(width):
            image.setPixelColor(x, y, QColor(x * 60, y * 80, 100 + x + 10 * y))
    return image


def test_rotate_and_flip_move_pixels_exactly() -> None:
    src = distinct()
    w, h = src.width(), src.height()
    right = render(src, [Step("rotate", amount=90)])
    assert all(right.pixelColor(h - 1 - y, x) == src.pixelColor(x, y)
               for x in range(w) for y in range(h))
    assert render(src, [Step("rotate", amount=90), Step("rotate", amount=270)]) == src
    assert render(src, [Step("rotate", amount=90)] * 4) == src
    flipped = render(src, [Step("flip", amount=1)])
    assert all(flipped.pixelColor(w - 1 - x, y) == src.pixelColor(x, y)
               for x in range(w) for y in range(h))
    upside = render(src, [Step("flip", amount=2)])
    assert all(upside.pixelColor(x, h - 1 - y) == src.pixelColor(x, y)
               for x in range(w) for y in range(h))


def test_unknown_step_kind_is_an_error_not_a_no_op() -> None:
    with pytest.raises(ValueError):
        render(noisy(), [Step("smudge", rect=QRect(0, 0, 5, 5))])  # type: ignore[arg-type]


def test_saved_png_keeps_no_text_metadata(tmp_path: Path) -> None:
    src = tmp_path / "secret.png"
    image = noisy()
    image.setText("Comment", "SECRET-GPS 51.5,-0.1")
    image.setText("XML:com.adobe.xmp", "<x:xmpmeta>SECRET</x:xmpmeta>")
    assert image.save(str(src))
    loaded = load_image(src)
    assert loaded.textKeys()  # Qt does carry it through loading...
    out = save_copy(loaded, src)
    assert QImage(str(out)).textKeys() == []  # ...but it must not reach the copy
    assert b"SECRET" not in out.read_bytes()


def test_exif_orientation_applied_and_metadata_dropped(tmp_path: Path,
                                                       workdir: dict[str, Path]) -> None:
    src = tmp_path / "turned.jpg"
    src.write_bytes(workdir["jpg"].read_bytes())  # 64 x 48
    subprocess.run(["exiftool", "-q", "-overwrite_original", "-Orientation#=6",
                    "-GPSLatitude=51.5", "-GPSLatitudeRef=N", str(src)], check=True)
    assert load_image(src).size().toTuple() == (48, 64)
    out = save_copy(load_image(src), src)
    tags = subprocess.run(["exiftool", "-s", "-Orientation", "-GPSLatitude", str(out)],
                          capture_output=True, text=True, check=True).stdout
    assert tags.strip() == ""


@pytest.mark.parametrize("make_dest", [
    lambda d: d / "missing" / "x.png",  # folder doesn't exist
    lambda d: d / "x.gif",  # can't write that format
    lambda d: d / "noext",
])
def test_save_errors_are_conversion_errors(tmp_path: Path, make_dest) -> None:
    with pytest.raises(ConversionError):
        save_copy(noisy(), tmp_path / "x.png", dest=make_dest(tmp_path))


def test_save_into_read_only_folder_is_a_conversion_error(tmp_path: Path) -> None:
    locked = tmp_path / "locked"
    locked.mkdir()
    src = locked / "x.png"
    assert noisy().save(str(src))
    locked.chmod(0o555)
    try:
        if os.access(locked, os.W_OK):
            pytest.skip("running as root")
        with pytest.raises(ConversionError, match="can't save in"):
            save_copy(load_image(src), src)
    finally:
        locked.chmod(0o755)


def open_editor(path: Path) -> Editor:
    editor = Editor(path)
    editor.resize(800, 600)
    editor.show()
    APP.processEvents()
    return editor


def answer_close(monkeypatch: pytest.MonkeyPatch, answer: QMessageBox.StandardButton) -> list:
    asked: list = []

    def question(*args: object, **kwargs: object) -> QMessageBox.StandardButton:
        asked.append(args)
        return answer

    monkeypatch.setattr(QMessageBox, "question", staticmethod(question))
    return asked


def close(editor: Editor) -> bool:
    from PySide6.QtGui import QCloseEvent

    event = QCloseEvent()
    editor.closeEvent(event)
    return event.isAccepted()


def test_close_prompts_after_save_undo_and_a_different_edit(
        workdir: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    editor = open_editor(workdir["png"])
    editor.canvas.transform("rotate", 90)
    assert editor.save()
    assert not editor.windowTitle().startswith("* ")
    editor._pick_tool("crop")  # used to put the "*" back
    assert not editor.windowTitle().startswith("* ")
    editor._undo()
    editor.canvas.transform("flip", 1)
    asked = answer_close(monkeypatch, QMessageBox.StandardButton.Cancel)
    assert not close(editor) and asked


def test_close_prompt_answers(workdir: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    editor = open_editor(workdir["png"])
    asked = answer_close(monkeypatch, QMessageBox.StandardButton.Cancel)
    assert close(editor) and not asked  # nothing edited: no question
    editor.canvas.transform("rotate", 90)
    assert not close(editor)  # Cancel
    answer_close(monkeypatch, QMessageBox.StandardButton.Discard)
    assert close(editor)
    answer_close(monkeypatch, QMessageBox.StandardButton.Save)
    assert close(editor)
    assert (workdir["png"].parent / "pic (edited).png").exists()


def test_close_with_save_after_undoing_to_the_original_closes(
        workdir: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    editor = open_editor(workdir["png"])
    editor.canvas.transform("rotate", 90)
    assert editor.save()
    editor._undo()
    answer_close(monkeypatch, QMessageBox.StandardButton.Save)
    assert close(editor)
    assert len(list(workdir["png"].parent.glob("pic (edited)*"))) == 1


def test_failed_save_keeps_the_window_open(
        workdir: dict[str, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    editor = open_editor(workdir["png"])
    editor.canvas.transform("rotate", 90)

    def boom(*_a: object, **_k: object) -> Path:
        raise OSError("disk on fire")

    monkeypatch.setattr(editor_mod, "save_copy", boom)
    answer_close(monkeypatch, QMessageBox.StandardButton.Save)
    assert not close(editor)


def test_second_save_without_changes_writes_nothing(workdir: dict[str, Path]) -> None:
    editor = open_editor(workdir["png"])
    editor.canvas.transform("rotate", 90)
    assert editor.save() and editor.save()
    assert len(list(workdir["png"].parent.glob("pic (edited)*"))) == 1


def test_redo_drops_a_pending_crop(workdir: dict[str, Path]) -> None:
    editor = open_editor(workdir["png"])
    editor.canvas.transform("rotate", 90)
    editor._undo()
    editor._pick_tool("crop")
    editor.canvas.crop_rect = QRect(0, 0, 10, 10)
    editor._redo()
    assert editor.canvas.crop_rect is None


@pytest.mark.parametrize("aspect", [a for a in ASPECTS.values() if a])
@pytest.mark.parametrize("start, end", [((10, 10), (500, 500)), ((60, 40), (-50, -50)),
                                        ((5, 5), (60, 5)), ((30, 20), (30, 47))])
def test_aspect_crop_stays_inside_and_keeps_the_ratio(
        workdir: dict[str, Path], aspect: float, start: tuple, end: tuple) -> None:
    editor = open_editor(workdir["png"])  # 64 x 48
    canvas = editor.canvas
    editor._pick_tool("crop")
    canvas.aspect = aspect
    canvas._start, canvas._now = QPointF(*start), QPointF(*end)
    rect = canvas._pending_rect()
    assert rect is not None
    assert QRect(0, 0, 64, 48).contains(rect)
    if rect.height() > 3:
        assert abs(rect.width() - rect.height() * aspect) <= 1 + aspect


def test_main_rejects_non_pictures_and_broken_files(workdir: dict[str, Path], tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not really a png")
    assert editor_mod.main([workdir["txt"], broken]) == 1
    err = capsys.readouterr().err
    assert "plain.txt: not a picture" in err and "broken.png" in err


def test_cli_routes_edit() -> None:
    from hala.cli import build_parser, cmd_edit

    assert build_parser().parse_args(["edit", "a.png"]).func is cmd_edit
    with pytest.raises(SystemExit):
        build_parser().parse_args(["edit"])
