"""Hala off Plasma: session detection, progress and notifications without Plasma's services,
the clipboard on X11, the other file managers' entries and GNOME's shortcut."""

from __future__ import annotations

import configparser
import json
import os
import sys
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from hala import filemanagers, session, shortcut


def _fake(bin_: Path, name: str, script: str) -> Path:
    bin_.mkdir(exist_ok=True)
    path = bin_ / name
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return path


@pytest.fixture
def bare_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """PATH is only a folder for fake tools, so no real clipboard tool or dialog answers.
    (The fakes are sh scripts and use absolute paths.)"""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    monkeypatch.setenv("PATH", str(bin_))
    return bin_


# --- session --------------------------------------------------------------------------------


@pytest.mark.parametrize(("desktop", "wayland", "display", "library", "xwayland"), [
    ("KDE", True, True, True, False),  # Plasma: layer shell
    ("Hyprland", True, True, True, False),  # wlroots: layer shell too
    ("ubuntu:GNOME", True, True, True, True),  # Mutter has none: XWayland
    ("sway", True, True, False, True),  # no LayerShellQt installed: XWayland
    ("GNOME", True, False, True, False),  # no XWayland to fall back on
    ("GNOME", False, True, True, False),  # X11 already
])
def test_when_edge_windows_go_through_xwayland(monkeypatch: pytest.MonkeyPatch, desktop: str,
                                               wayland: bool, display: bool, library: bool,
                                               xwayland: bool) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
    monkeypatch.delenv("HALA_NO_LAYERSHELL", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland" if wayland else "x11")
    if wayland:
        monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    else:
        monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    if display:
        monkeypatch.setenv("DISPLAY", ":0")
    else:
        monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr(session, "layer_shell_library", lambda: library)
    assert session.wants_xwayland() is xwayland
    assert session.qt_args("hala") == (["hala", "-platform", "xcb"] if xwayland else ["hala"])
    assert "QT_QPA_PLATFORM" not in os.environ  # the editor and dialogs stay native


def test_a_chosen_qt_platform_is_respected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setenv("QT_QPA_PLATFORM", "wayland")
    assert not session.wants_xwayland()


def test_file_manager_names(monkeypatch: pytest.MonkeyPatch) -> None:
    for desktop, name in (("KDE", "Dolphin"), ("X-Cinnamon", "Nemo"), ("XFCE", "Thunar"),
                          ("ubuntu:GNOME", "Files"), ("", "your file manager")):
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
        assert session.file_manager() == name


def test_clipboard_tools_by_session(bare_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert session.clipboard_reader() is None
    _fake(bare_path, "xsel", "")
    assert session.clipboard_reader() == ["xsel", "--output", "--clipboard"]
    _fake(bare_path, "xclip", "")
    assert session.clipboard_reader()[0] == "xclip"  # it can ask for the file list's type
    assert session.clipboard_writer()[0] == "xclip"
    _fake(bare_path, "wl-paste", "")
    _fake(bare_path, "wl-copy", "")
    assert session.clipboard_reader()[0] == "xclip"  # not a Wayland session
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    assert session.clipboard_reader()[0] == "wl-paste"
    assert session.clipboard_writer() == ["wl-copy"]


def test_copied_files_come_from_xclip_on_x11(tmp_path: Path, bare_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    from hala.gui import wheelapp

    (tmp_path / "pic one.png").write_bytes(b"x")
    _fake(bare_path, "xclip", f"printf 'file://{tmp_path}/pic%%20one.png\\r\\n'\n")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    assert wheelapp.clipboard_files() == [tmp_path / "pic one.png"]


def test_copied_paths_come_from_xsel(tmp_path: Path, bare_path: Path,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from hala.gui import wheelapp

    (tmp_path / "My Pics").mkdir()
    (tmp_path / "My Pics" / "a b.png").write_bytes(b"x")
    (tmp_path / "c.png").write_bytes(b"x")
    # Thunar/Caja copy bare paths; Nautilus "x-special/nautilus-clipboard", a verb and URLs.
    _fake(bare_path, "xsel", f"""printf '%s\\n' 'x-special/nautilus-clipboard' copy \
'{tmp_path}/My Pics/a b.png' 'file://{tmp_path}/c.png' '/no/such/file'
""")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert wheelapp.clipboard_files() == [tmp_path / "My Pics" / "a b.png", tmp_path / "c.png"]


def test_nothing_copied_and_no_klipper_off_plasma(bare_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from hala.gui import wheelapp

    _fake(bare_path, "xclip", "echo 'Error: target text/uri-list not available' >&2; exit 1\n")
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "XFCE")
    assert wheelapp.clipboard_files() == []


def test_error_dialog_falls_back_to_zenity(bare_path: Path, tmp_path: Path) -> None:
    out = tmp_path / "zenity.txt"
    _fake(bare_path, "zenity", f'printf "%s\\n" "$@" > {out}\n')
    assert session.error_dialog("Couldn't convert\n\nbroken.mp4: bad data")
    for _ in range(50):
        if out.exists() and out.read_text():
            break
        import time

        time.sleep(0.05)
    assert "--no-markup" in out.read_text() and "broken.mp4: bad data" in out.read_text()


def test_no_dialog_tool_at_all(bare_path: Path) -> None:
    assert not session.error_dialog("nobody will see this")


# --- notifications and progress without Plasma ------------------------------------------------


class FakeBus:
    """Just enough of Gio.DBusConnection for the notification code."""

    def __init__(self, server: str = "gnome-shell", caps: tuple[str, ...] = ("body-markup",
                                                                              "actions")):
        from gi.repository import GLib

        self.GLib = GLib
        self.server, self.caps = server, caps
        self.sent: list[tuple[str, tuple]] = []
        self.signals: list = []
        self.next_id = 7

    def call_sync(self, _dest, _path, _iface, method, params, _rtype, *_rest):
        GLib = self.GLib
        if method == "GetCapabilities":
            return GLib.Variant("(as)", (list(self.caps),))
        if method == "GetServerInformation":
            return GLib.Variant("(ssss)", (self.server, "", "1", "1.2"))
        if method == "requestView":
            raise GLib.Error("org.freedesktop.DBus.Error.ServiceUnknown")
        self.sent.append((method, params.unpack() if params is not None else ()))
        if method == "Notify":
            replaces = params.unpack()[1]
            return GLib.Variant("(u)", (replaces or self.next_id,))
        return None

    def call(self, _dest, _path, _iface, method, params, *_rest):
        self.sent.append((method, params.unpack()))

    def signal_subscribe(self, *args):
        self.signals.append(args)
        return len(self.signals)


@pytest.fixture
def fresh_server(monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop

    monkeypatch.setattr(desktop, "_server", {})


def test_body_markup_per_server(fresh_server: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop

    body = "<b>a &amp; b.mp4</b>: bad<br><i>note</i>"
    assert desktop._for_server(FakeBus("Plasma"), body) == body
    monkeypatch.setattr(desktop, "_server", {})
    assert desktop._for_server(FakeBus("gnome-shell"), body) == (
        "<b>a &amp; b.mp4</b>: bad\n<i>note</i>")
    monkeypatch.setattr(desktop, "_server", {})
    assert desktop._for_server(FakeBus("tiny", caps=("actions",)), body) == (
        "a & b.mp4: bad\nnote")


def test_progress_falls_back_to_a_notification(fresh_server: None) -> None:
    from hala import desktop

    bus = FakeBus()
    cancelled: list[bool] = []
    view = desktop.progress_view(bus, lambda: cancelled.append(True))
    assert isinstance(view, desktop.NotificationView)
    view.call("setInfoMessage", "s", "Converting 3 files to JPG")
    view.call("setTotalAmount", "ts", 3, "files")
    view.call("setDescriptionField", "uss", 0, "File", "a.png")
    notifies = [args for method, args in bus.sent if method == "Notify"]
    assert notifies, "the first update shows it at once"
    _app, replaces, _icon, summary, _body, actions, hints, timeout = notifies[0]
    assert summary == "Converting 3 files to JPG" and actions == ["cancel", "Cancel"]
    assert replaces == 0 and timeout == 0 and hints["value"] == 0
    view.call("setPercent", "u", 40)  # within PROGRESS_EVERY_S: not shown yet...
    assert len([m for m, _ in bus.sent if m == "Notify"]) == 1
    assert view.pending  # ...but due as soon as the wait is over
    view.call("setDescriptionField", "uss", 0, "File", "Tom & <Jerry>.mp4")
    view._flush()
    assert not view.pending
    view.shown_at -= desktop.PROGRESS_EVERY_S
    view.call("setProcessedAmount", "ts", 1, "files")
    last = [args for method, args in bus.sent if method == "Notify"][-1]
    assert last[1] == 7  # replaces the same notification
    assert last[4] == "40% · 1 of 3 done\nTom &amp; &lt;Jerry&gt;.mp4"  # escaped: it's markup
    # Its Cancel button cancels; another notification's button doesn't.
    handler = bus.signals[0][-1]
    handler(None, None, None, None, None, bus.GLib.Variant("(us)", (99, "cancel")))
    assert not cancelled
    handler(None, None, None, None, None, bus.GLib.Variant("(us)", (7, "cancel")))
    assert cancelled == [True]
    view.call("terminate", "s", "")
    assert ("CloseNotification", (7,)) in bus.sent
    view.call("setPercent", "u", 100)  # after the end: nothing more is shown
    assert bus.sent[-1][0] == "CloseNotification"


def test_copy_goes_through_wl_copy_off_plasma(bare_path: Path, tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop

    out = tmp_path / "clip.txt"
    _fake(bare_path, "wl-copy", f"/bin/cat > {out}\n")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    desktop.copy_text(FakeBus(), "https://example.com/qr")
    for _ in range(50):
        if out.exists() and out.read_text():
            break
        import time

        time.sleep(0.05)
    assert out.read_text() == "https://example.com/qr"


def test_copy_without_any_clipboard_tool_says_so(bare_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    from hala import desktop

    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    with pytest.raises(OSError, match="xclip"):
        desktop.copy_text(None, "text")


# --- other file managers ----------------------------------------------------------------------


def _load_extension(source: str, module: str, monkeypatch: pytest.MonkeyPatch):
    """Run a generated nautilus/caja-python extension against a stand-in of their API."""
    launched: list[list[str]] = []

    class MenuItem:
        def __init__(self, **kwargs):
            self.props = kwargs
            self.callback = None

        def connect(self, _signal, callback):
            self.callback = callback

    api = types.SimpleNamespace(MenuProvider=type("MenuProvider", (), {}), MenuItem=MenuItem)
    repository = types.ModuleType("gi.repository")
    repository.GObject = types.SimpleNamespace(  # type: ignore[attr-defined]
        GObject=type("GObject", (), {}))
    setattr(repository, module, api)
    monkeypatch.setitem(sys.modules, "gi.repository", repository)
    namespace: dict = {}
    exec(compile(source, "hala.py", "exec"), namespace)
    monkeypatch.setattr(namespace["subprocess"], "Popen",
                        lambda argv, **_kw: launched.append(argv))
    return namespace["HalaMenu"](), launched


class _File:
    def __init__(self, path: str | None):
        self.path = path

    def get_location(self):
        return types.SimpleNamespace(get_path=lambda: self.path)


@pytest.mark.parametrize(("app", "module"), [("nautilus", "Nautilus"), ("caja", "Caja")])
def test_python_extension_menus(app: str, module: str, monkeypatch: pytest.MonkeyPatch) -> None:
    source = filemanagers.python_extension(app, "/opt/hala's/bin/hala")
    menu, launched = _load_extension(source, module, monkeypatch)
    items = menu.get_file_items([_File("/home/me/a.png"), _File("/home/me/b.pdf")])  # 4.0 API
    assert [i.props["label"] for i in items] == ["Hala…", "Crop, annotate, redact…"]
    items[0].callback()
    assert launched == [["/opt/hala's/bin/hala", "wheel", "/home/me/a.png", "/home/me/b.pdf"]]
    window = object()
    items = menu.get_file_items(window, [_File("/home/me/song.mp3")])  # 3.0 API / Caja
    assert [i.props["label"] for i in items] == ["Hala…"]
    assert menu.get_file_items([_File(None)]) == []  # trash, network places


def test_nemo_actions_parse() -> None:
    for name, text in filemanagers.nemo_actions("/usr/bin/hala").items():
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str  # type: ignore[assignment]
        cp.read_string(text)
        action = cp["Nemo Action"]
        assert action["Exec"].startswith("/usr/bin/hala ") and action["Exec"].endswith(" %F")
        assert action["Quote"] == "double" and filemanagers.MARK in action
        if name == "hala-edit.nemo_action":
            assert "pdf" in action["Extensions"].split(";")


UCA = """<?xml version="1.0" encoding="UTF-8"?>
<actions>
<action>
\t<icon>utilities-terminal</icon>
\t<name>Open Terminal Here</name>
\t<unique-id>1600000000000000-1</unique-id>
\t<command>exo-open --working-directory %f --launch TerminalEmulator</command>
\t<description>Example for a custom action</description>
\t<patterns>*</patterns>
\t<startup-notify/>
\t<directories/>
</action>
</actions>
"""


def test_install_and_uninstall_every_file_manager(tmp_path: Path) -> None:
    uca = filemanagers.THUNAR_UCA
    uca.parent.mkdir(parents=True)
    uca.write_text(UCA)
    written = filemanagers.install("/usr/bin/hala")
    data = filemanagers.DATA
    for relative in ("nautilus-python/extensions/hala_menu.py",
                     "caja-python/extensions/hala_menu.py", "nemo/actions/hala-wheel.nemo_action",
                     "nemo/actions/hala-edit.nemo_action"):
        assert data / relative in written
    names = [a.findtext("name") for a in ET.parse(uca).getroot()]
    assert names == ["Open Terminal Here", "Hala…", "Crop, annotate, redact…"]
    filemanagers.install("/usr/bin/hala")  # again: replaced, not doubled
    assert len(ET.parse(uca).getroot()) == 3
    wheel = ET.parse(uca).getroot()[1]
    assert wheel.findtext("command") == "/usr/bin/hala wheel %F"
    assert wheel.find("directories") is not None
    # Somebody else's file in the same folder is left alone.
    theirs = data / "nemo/actions/other.nemo_action"
    theirs.write_text("[Nemo Action]\nName=Other\n")
    assert filemanagers.uninstall() == 6
    assert [a.findtext("name") for a in ET.parse(uca).getroot()] == ["Open Terminal Here"]
    assert not (data / "nautilus-python/extensions/hala_menu.py").exists()
    assert theirs.exists()


def test_no_user_copy_where_the_package_has_one(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    system = tmp_path / "usr-share"
    (system / "nemo/actions").mkdir(parents=True)
    (system / "nemo/actions/hala-wheel.nemo_action").write_text("[Nemo Action]\n")
    monkeypatch.setattr(filemanagers, "SYSTEM_DATA", system)
    stale = filemanagers.DATA / "nemo/actions/hala-wheel.nemo_action"
    stale.parent.mkdir(parents=True)
    stale.write_text(f"[Nemo Action]\n{filemanagers.MARK}=wheel\n")  # from an older install
    written = filemanagers.install("/usr/bin/hala")
    assert not stale.exists()
    assert filemanagers.DATA / "nemo/actions/hala-edit.nemo_action" in written
    assert "Nemo" in filemanagers.installed_for(written)
    assert any(name.startswith("GNOME Files") for name in filemanagers.installed_for(written))


def test_a_linked_uca_file_stays_linked_and_keeps_comments(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "uca.xml"
    real.parent.mkdir()
    real.write_text(UCA.replace("<actions>", "<actions>\n<!-- my actions -->"))
    uca = filemanagers.THUNAR_UCA
    uca.parent.mkdir(parents=True)
    uca.symlink_to(real)
    filemanagers.install("/usr/bin/hala")
    assert uca.is_symlink()
    assert "<!-- my actions -->" in real.read_text() and "Hala…" in real.read_text()


def test_thunar_without_a_uca_file_is_left_alone() -> None:
    assert filemanagers.THUNAR_UCA not in filemanagers.install("/usr/bin/hala")
    assert not filemanagers.THUNAR_UCA.exists()


def test_a_broken_uca_file_isnt_touched() -> None:
    uca = filemanagers.THUNAR_UCA
    uca.parent.mkdir(parents=True)
    uca.write_text("<actions><action>")
    filemanagers.install("/usr/bin/hala")
    assert uca.read_text() == "<actions><action>"


def test_the_package_ships_the_other_file_managers() -> None:
    files = filemanagers.system_files("/usr/bin/hala")
    assert set(files) == {"nautilus-python/extensions/hala_menu.py",
                          "caja-python/extensions/hala_menu.py",
                          "nemo/actions/hala-wheel.nemo_action",
                          "nemo/actions/hala-edit.nemo_action"}


# --- GNOME shortcut -----------------------------------------------------------------------------


@pytest.fixture
def gsettings(bare_path: Path, tmp_path: Path) -> Path:
    """A fake gsettings keeping its keys in a JSON file."""
    store = tmp_path / "dconf.json"
    store.write_text(json.dumps({"custom-keybindings": "@as []"}))
    _fake(bare_path, "gsettings", f"""exec {sys.executable} - "$@" <<'EOF'
import json, sys
store = {str(store)!r}
data = json.load(open(store))
op, schema, key = sys.argv[1:4]
name = key if schema.endswith("media-keys") else schema.split(":", 1)[1] + key
if op == "get":
    print(data.get(name, "''"))
elif op == "set":
    data[name] = sys.argv[4]
elif op == "reset":
    data.pop(name, None)
json.dump(data, open(store, "w"))
EOF
""")
    return store


def test_gnome_shortcut_is_added_once_and_kept(gsettings: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")
    shortcut.offer_once("/usr/bin/hala")
    data = json.loads(gsettings.read_text())
    assert data["custom-keybindings"] == repr([shortcut.PATH])
    assert data[shortcut.PATH + "binding"] == "<Super><Shift>h"
    assert data[shortcut.PATH + "command"] == "/usr/bin/hala wheel --clipboard"
    assert shortcut.DONE.exists()
    # The user rebinds it, then removes it in Settings: Hala doesn't undo either.
    data[shortcut.PATH + "binding"] = "<Super>c"
    gsettings.write_text(json.dumps(data))
    shortcut.add_gnome("/opt/hala/bin/hala")  # (install-menus) points at the new place
    data = json.loads(gsettings.read_text())
    assert data[shortcut.PATH + "binding"] == "<Super>c"
    assert data[shortcut.PATH + "command"] == "/opt/hala/bin/hala wheel --clipboard"
    assert shortcut.remove_gnome()
    assert json.loads(gsettings.read_text())["custom-keybindings"] == "[]"
    shortcut.offer_once("/usr/bin/hala")
    assert json.loads(gsettings.read_text())["custom-keybindings"] == "[]"


def test_other_shortcuts_stay(gsettings: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    mine = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/"
    gsettings.write_text(json.dumps({"custom-keybindings": repr([mine])}))
    assert shortcut.add_gnome("/usr/bin/hala")
    assert json.loads(gsettings.read_text())["custom-keybindings"] == repr([mine, shortcut.PATH])
    assert shortcut.remove_gnome()
    assert json.loads(gsettings.read_text())["custom-keybindings"] == repr([mine])


def test_no_shortcut_outside_gnome_or_without_gsettings(bare_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    assert not shortcut.applies()
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    assert not shortcut.add_gnome("/usr/bin/hala")  # no gsettings at all
    shortcut.offer_once("/usr/bin/hala")
    assert not shortcut.DONE.exists()
