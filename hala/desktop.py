"""`hala run`: convert from a menu or drop, reporting through Plasma's own job progress.

Progress shows in Plasma's job tracker (like a file copy), with a working Cancel button; on
other desktops, in a notification that's updated as files finish, with a Cancel button. When
the batch ends a notification offers Open / Show in folder, or lists what failed with a
Details button for the full log. A cancelled batch only gets a notification if some files
were already converted or skipped. Without a target (the app entry's "Open with"), it just
says what the files can become.
"""

from __future__ import annotations

import html
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import (  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]
    Gio,
    GLib,
    GLibUnix,
)

from . import formats as f  # noqa: E402
from . import session  # noqa: E402
from .i18n import _, n_files, ngettext  # noqa: E402
from .jobs import DEFAULT_WORKERS, Job, JobQueue, Status, clean_note  # noqa: E402
from .registry import common_targets, route_for  # noqa: E402
from .runner import ConversionError  # noqa: E402
from .tools import Tool, needs_files, tool  # noqa: E402

APP_ID = "hala"
JOB_SERVER = ("org.kde.JobViewServer", "/JobViewServer", "org.kde.JobViewServerV2")
JOB_VIEW = "org.kde.JobViewV2"
NOTIFY = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
          "org.freedesktop.Notifications")
FILE_MANAGER = ("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1")
# JobViewV2 capability bits: 1 = Killable, which gives the entry a Cancel button.
KILLABLE = 1
# Outside Plasma, the progress notification is replaced at most this often, so it doesn't
# keep popping up.
PROGRESS_EVERY_S = 2.0
# Characters of a text result (QR codes) shown in the notification.
TEXT_PREVIEW = 300
# How long the result notification keeps its buttons working before the process exits.
ACTION_WINDOW_S = 15 * 60
LOG_FILE = Path.home() / ".cache" / "hala" / "last-run.log"
# Closing lines tools print after the real reason ("Conversion failed!"); skipped in summaries.
GENERIC_ERRORS = ("Conversion failed!", "Error opening output file", "Exiting normally",
                  "[out#", "Error while ")

log = logging.getLogger("hala")


class JobView:
    """One Plasma progress entry for the whole batch. Silently does nothing outside Plasma."""

    def __init__(self, bus: Gio.DBusConnection, on_cancel: Callable[[], None]):
        self.bus, self.path = bus, None
        try:
            reply = bus.call_sync(*JOB_SERVER, "requestView",
                                  GLib.Variant("(sia{sv})", (APP_ID, KILLABLE, {})),
                                  GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, 2000, None)
            self.path = reply.unpack()[0]
        except GLib.Error as exc:
            log.info("no Plasma job view: %s", exc.message)
            return
        bus.signal_subscribe(JOB_SERVER[0], JOB_VIEW, "cancelRequested", self.path, None,
                             Gio.DBusSignalFlags.NONE, lambda *_: on_cancel())

    def call(self, method: str, signature: str, *args: Any) -> None:
        if self.path is None:
            return
        self.bus.call(JOB_SERVER[0], self.path, JOB_VIEW, method,
                      GLib.Variant(f"({signature})", args), None, Gio.DBusCallFlags.NONE, -1,
                      None, None)


class NotificationView:
    """JobView's stand-in outside Plasma: one notification for the batch, replaced as it goes
    (with a "value" hint, which servers that draw progress bars use), with a Cancel button."""

    def __init__(self, bus: Gio.DBusConnection, on_cancel: Callable[[], None]):
        self.bus, self.on_cancel = bus, on_cancel
        self.id = 0
        self.info = ""
        self.file = ""
        self.total = 0
        self.processed = 0
        self.percent = 0
        self.shown_at = 0.0
        self.pending = 0  # a GLib timeout that shows an update held back by the throttle
        self.closed = False
        bus.signal_subscribe(NOTIFY[0], NOTIFY[2], "ActionInvoked", NOTIFY[1], None,
                             Gio.DBusSignalFlags.NONE, self._on_action)

    def call(self, method: str, _signature: str, *args: Any) -> None:
        if method == "setInfoMessage":
            self.info = args[0]
        elif method == "setDescriptionField":
            self.file = args[2]
        elif method == "setTotalAmount":
            self.total = args[0]
        elif method == "setProcessedAmount":
            self.processed = args[0]
        elif method == "setPercent":
            self.percent = args[0]
        elif method == "terminate":
            self._close()
            return
        else:
            return
        wait = PROGRESS_EVERY_S - (time.monotonic() - self.shown_at)
        if wait <= 0 or not self.id:
            self._show()
        elif not self.pending:  # held back: shown when the wait is over, even if nothing else
            self.pending = GLib.timeout_add(int(wait * 1000) + 1, self._flush)

    def _flush(self) -> bool:
        self.pending = 0
        self._show()
        return False

    def _show(self) -> None:
        if self.closed:
            return
        body = f"{self.percent}%"
        if self.total > 1:
            body += " · " + _("{done} of {total} done").format(done=self.processed,
                                                              total=self.total)
        if self.file:
            body += f"<br>{_esc(self.file)}"
        hints = {"desktop-entry": GLib.Variant("s", APP_ID),
                 "value": GLib.Variant("i", max(0, min(100, self.percent))),
                 "urgency": GLib.Variant("y", 0), "suppress-sound": GLib.Variant("b", True)}
        try:
            reply = self.bus.call_sync(
                *NOTIFY, "Notify",
                GLib.Variant("(susssasa{sv}i)", ("Hala", self.id, "hala",
                                                 _printable(self.info or "Hala"),
                                                 _printable(_for_server(self.bus, body)),
                                                 ["cancel", _("Cancel")], hints, 0)),
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 2000, None)
        except GLib.Error as exc:
            log.info("no progress notification: %s", exc.message)
            self.closed = True  # don't retry every few seconds
            return
        self.id = reply.unpack()[0]
        self.shown_at = time.monotonic()

    def _close(self) -> None:
        self.closed = True
        if self.pending:
            GLib.source_remove(self.pending)
            self.pending = 0
        if self.id:
            self.bus.call(*NOTIFY, "CloseNotification", GLib.Variant("(u)", (self.id,)), None,
                          Gio.DBusCallFlags.NONE, -1, None, None)

    def _on_action(self, _bus, _sender, _path, _iface, _signal, params: GLib.Variant) -> None:
        nid, action = params.unpack()
        if nid == self.id and self.id and action == "cancel" and not self.closed:
            self.on_cancel()


def progress_view(bus: Gio.DBusConnection,
                  on_cancel: Callable[[], None]) -> JobView | NotificationView:
    """Plasma's job tracker if it's there, else a progress notification."""
    view = JobView(bus, on_cancel)
    return view if view.path is not None else NotificationView(bus, on_cancel)


class Batch:
    """A conversion to target, or (with tool) a tool run, over files."""

    def __init__(self, files: list[Path], target: str, workers: int, tool: Tool | None = None,
                 params: dict[str, Any] | None = None):
        self.files, self.target, self.tool, self.params = files, target, tool, params or {}
        self.loop = GLib.MainLoop()
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        self.queue = JobQueue(workers=workers, listener=self._from_worker)
        self.jobs: list[Job] = []
        self.skipped: list[str] = []
        self.last_update = 0.0
        self.notification_id = 0
        self.view: JobView | NotificationView | None = None
        self.outputs: list[Path] = []
        self.copy_text = ""
        self.finished = False

    # Worker threads call this; hop onto the main loop before touching D-Bus state.
    def _from_worker(self, job: Job) -> None:
        GLib.idle_add(self._on_job, job)

    def start(self) -> int:
        if self.tool is not None:
            self._submit_tool(self.tool)
        else:
            for path in self.files:
                ext = f.ext_of(path)
                if ext == self.target:
                    self.skipped.append(_("{name} is already {format}").format(
                        name=path.name, format=self.target.upper()))
                elif route_for(ext, self.target) is None:
                    self.skipped.append(_("{name} can't become {format}").format(
                        name=path.name, format=self.target.upper()))
                else:
                    self.jobs.append(self.queue.submit(path, self.target)[0])
        if not self.jobs:
            log.warning("nothing to convert: %s", "; ".join(self.skipped))
            self._notify(_("Nothing to convert"), "\n".join(self.skipped), "dialog-information",
                         problem=True)
            self._wait_for_notification()
            return 1
        self.view = progress_view(self.bus, self._cancel_all)
        # Logout or `kill` should cancel cleanly (tools stopped, scratch removed), not orphan.
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            GLibUnix.signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal)
        count = len(self.jobs)
        doing = (f"{_(self.tool.label)}: {n_files(len(self.files))}" if self.tool else
                 ngettext("Converting {n} file to {format}", "Converting {n} files to {format}",
                          count).format(n=count, format=self.target.upper()))
        self.view.call("setInfoMessage", "s", doing)
        # "files" is the unit's id for Plasma (it names it itself), not text.
        self.view.call("setTotalAmount", "ts", count, "files")
        self.view.call("setDescriptionField", "uss", 0, _("File"), self.jobs[0].src.name)
        self.loop.run()
        return 0 if all(j.status is Status.DONE for j in self.jobs) and not self.skipped else 1

    def _submit_tool(self, tool: Tool) -> None:
        if tool.combine:
            usable = [p for p in self.files if tool.accepts([p, p])]
            self.skipped += [_doesnt_take(p, tool) for p in self.files if p not in usable]
            if len(usable) >= tool.min_files:
                self.jobs.append(self.queue.submit_tool(tool.name, usable, self.params)[0])
            elif usable:
                self.skipped.append(needs_files(tool))
            return
        for path in self.files:
            if tool.accepts([path]):
                self.jobs.append(self.queue.submit_tool(tool.name, [path], self.params)[0])
            else:
                self.skipped.append(_doesnt_take(path, tool))

    def _title(self) -> str:
        return (_(self.tool.done) if self.tool
                else _("Converted to {format}").format(format=self.target.upper()))

    def _on_signal(self) -> bool:
        self._cancel_all()
        if self.finished:  # only the notification wait is left; nothing to clean up
            self.loop.quit()
        return True

    def _cancel_all(self) -> None:
        for job in self.jobs:
            job.cancel()

    def _on_job(self, job: Job) -> bool:
        assert self.view is not None
        finished = [j for j in self.jobs if j.status.is_terminal]
        now = time.monotonic()
        if job.status is Status.RUNNING:
            if now - self.last_update < 0.25:
                return False  # throttle: ffmpeg reports progress many times a second
            self.view.call("setDescriptionField", "uss", 0, _("File"), job.src.name)
        self.last_update = now
        overall = sum(1.0 if j in finished else j.progress for j in self.jobs) / len(self.jobs)
        self.view.call("setPercent", "u", int(overall * 100))
        self.view.call("setProcessedAmount", "ts", len(finished), "files")
        if len(finished) == len(self.jobs) and not self.finished:
            self.finished = True  # several queued events can all see the batch complete
            self._finish()
        return False

    def _finish(self) -> None:
        assert self.view is not None
        self.queue.shutdown()
        done = [j for j in self.jobs if j.status is Status.DONE]
        failed = [j for j in self.jobs if j.status is Status.FAILED]
        cancelled = any(j.status is Status.CANCELLED for j in self.jobs)
        if len(done) == len(self.jobs) and not self.skipped:
            self.view.call("terminate", "s", "")
            self._notify_success(done)
        elif cancelled and not failed and not done and not self.skipped:
            self.view.call("terminate", "s", _("Cancelled"))
            self.loop.quit()
            return
        else:
            # The notification below carries the details.
            self.view.call("terminate", "s", _("Cancelled") if cancelled else "")
            self._notify_problems(done, failed, cancelled)
        self._wait_for_notification()

    def _notify_success(self, done: list[Job]) -> None:
        outputs = [j.output for j in done if j.output]
        self.copy_text = ""
        if len(outputs) == 1:
            body = _esc(outputs[0].name)
            actions = ["open", _("Open"), "show", _("Show in folder")]
            if self.tool is not None and self.tool.shows_text and outputs[0].is_file():
                # Short results (QR codes) are the point: show them, and offer to copy them.
                self.copy_text = outputs[0].read_text(errors="replace").strip()
                body = _esc(self.copy_text[:TEXT_PREVIEW]).replace("\n", "<br>")
                actions = ["copy", _("Copy"), *actions]
        else:
            folder = _common_folder(outputs).name
            body = _("{files} in {folder}").format(files=n_files(len(outputs)),
                                                    folder=_esc(folder))
            actions = ["show", _("Show in folder")]
        body += "".join(f"<br>{line}" for line in _note_lines(done))
        self.outputs = outputs
        self._notify(self._title(), body, "hala", actions)

    def _notify_problems(self, done: list[Job], failed: list[Job], cancelled: bool) -> None:
        self.outputs = [j.output for j in done if j.output]
        # Tools quote the whole input path ("/long/path/x.mp4: Invalid data"); the name will do.
        lines = [f"<b>{_esc(j.src.name)}</b>: "
                 f"{_esc(_error_detail(j.error).replace(str(j.src) + ': ', ''))}"
                 for j in failed[:5]]
        lines += [_esc(s) for s in self.skipped[:5]]
        # Notes about results that did work (pictures left out...) still matter.
        lines += _note_lines(done)
        if cancelled:
            lines.append(_("The rest were cancelled."))
        total = len(self.jobs) + len(self.skipped)
        if self.tool is not None:
            summary = (_("{done}: {n} of {total}").format(done=_(self.tool.done), n=len(done),
                                                         total=total) if done
                       else _("{tool} didn't work").format(tool=_(self.tool.label)))
        else:
            summary = (_("{n} of {total} converted to {format}").format(
                n=len(done), total=total, format=self.target.upper()) if done
                else _("Couldn't convert to {format}").format(format=self.target.upper()))
        actions = ["show", _("Show in folder")] if self.outputs else []
        if failed:
            actions += ["details", _("Details")]
        self._notify(summary, "<br>".join(lines), "dialog-warning", actions, problem=True)

    def _notify(self, summary: str, body: str, icon: str,
                actions: list[str] | None = None, problem: bool = False) -> None:
        hints = {"desktop-entry": GLib.Variant("s", APP_ID)}
        summary, body = _printable(summary), _printable(_for_server(self.bus, body))
        try:
            reply = self.bus.call_sync(
                *NOTIFY, "Notify",
                GLib.Variant("(susssasa{sv}i)", ("Hala", 0, icon, summary, body,
                                                 actions or [], hints, -1)),
                GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, 2000, None)
        except (GLib.Error, TypeError, UnicodeError) as exc:
            log.error("notification failed (%s): %s", getattr(exc, "message", exc), summary)
            if problem:
                _fallback_dialog(summary, body)
            return
        self.notification_id = reply.unpack()[0]
        self.bus.signal_subscribe(NOTIFY[0], NOTIFY[2], "ActionInvoked", NOTIFY[1], None,
                                  Gio.DBusSignalFlags.NONE, self._on_action)
        self.bus.signal_subscribe(NOTIFY[0], NOTIFY[2], "NotificationClosed", NOTIFY[1], None,
                                  Gio.DBusSignalFlags.NONE, self._on_closed)

    def _wait_for_notification(self) -> None:
        # The buttons only work while this process is on the bus to hear ActionInvoked. Called
        # both from inside the running loop (_finish) and before it ever ran (nothing to do).
        if not self.notification_id:
            self.loop.quit()
            return
        GLib.timeout_add_seconds(ACTION_WINDOW_S, self.loop.quit)
        if not self.loop.is_running():
            self.loop.run()

    def _on_action(self, _bus, _sender, _path, _iface, _signal, params: GLib.Variant) -> None:
        nid, action = params.unpack()
        if nid != self.notification_id:
            return
        outputs = self.outputs
        try:
            if action == "open" and outputs:
                Gio.AppInfo.launch_default_for_uri(outputs[0].as_uri(), None)
            elif action == "show" and outputs:
                self.bus.call_sync(*FILE_MANAGER, "ShowItems",
                                   GLib.Variant("(ass)", ([p.as_uri() for p in outputs], "")),
                                   None, Gio.DBusCallFlags.NONE, 5000, None)
            elif action == "copy" and self.copy_text:
                copy_text(self.bus, self.copy_text)
            elif action == "details":
                Gio.AppInfo.launch_default_for_uri(LOG_FILE.as_uri(), None)
            elif action == "kdeconnect":
                subprocess.Popen(["kdeconnect-app"], stdin=subprocess.DEVNULL,
                                 start_new_session=True)
        except OSError as exc:  # kdeconnect-app missing: nothing better to open
            log.warning("%s action failed: %s", action, exc)
        except GLib.Error as exc:
            # No app for this type, or no file manager on the bus: open the folder instead.
            log.warning("%s action failed: %s", action, exc.message)
            if outputs:
                try:
                    Gio.AppInfo.launch_default_for_uri(_common_folder(outputs).as_uri(), None)
                except GLib.Error as exc2:
                    log.error("couldn't open the folder either: %s", exc2.message)
        # One press is the answer: Plasma doesn't always close the notification itself (e.g.
        # from history under Do Not Disturb), so close it and exit once the launch is under way.
        self.bus.call(*NOTIFY, "CloseNotification", GLib.Variant("(u)", (nid,)), None,
                      Gio.DBusCallFlags.NONE, -1, None, None)
        GLib.timeout_add(500, self.loop.quit)

    def _on_closed(self, _bus, _sender, _path, _iface, _signal, params: GLib.Variant) -> None:
        if params.unpack()[0] == self.notification_id:
            # Plasma closes the notification right after ActionInvoked; give the app or file
            # manager we just launched a moment before the process exits.
            GLib.timeout_add(500, self.loop.quit)


def _fallback_dialog(summary: str, body: str) -> None:
    """Problems must reach the user somehow when notifications are down."""
    text = summary + "\n\n" + _plain(body)
    if not session.error_dialog(text):
        log.error("no way to show this problem: %s", text)


def _plain(body: str) -> str:
    """A notification body's markup as plain text: lines for <br>, no tags, no entities."""
    text = re.sub(r"<br\s*/?>", "\n", body)
    return html.unescape(re.sub(r"</?(b|i|u)>", "", text))


# The server's name and whether it takes markup, asked once per process.
_server: dict[str, Any] = {}


def _for_server(bus: Gio.DBusConnection | None, body: str) -> str:
    """The body as this notification server will show it properly. Plasma takes Hala's
    markup as is; GNOME and most others take <b>/<i> but print <br> literally, so lines are
    newlines there; a server without body markup gets plain text."""
    if "markup" not in _server:
        name, markup = "", True
        if bus is not None:
            try:
                caps = bus.call_sync(*NOTIFY, "GetCapabilities", None,
                                     GLib.VariantType("(as)"), Gio.DBusCallFlags.NONE, 2000,
                                     None).unpack()[0]
                info = bus.call_sync(*NOTIFY, "GetServerInformation", None,
                                     GLib.VariantType("(ssss)"), Gio.DBusCallFlags.NONE, 2000,
                                     None).unpack()
                name, markup = info[0], "body-markup" in caps
            except GLib.Error as exc:
                log.info("notification server unknown: %s", exc.message)
        _server.update(name=name, markup=markup)
    if not _server["markup"]:
        return _plain(body)
    if _server["name"] in ("Plasma", "KDE"):
        return body
    return re.sub(r"<br\s*/?>", "\n", body)


def copy_text(bus: Gio.DBusConnection | None, text: str) -> None:
    """Put text on the clipboard: Klipper on Plasma, else wl-copy, xclip or xsel."""
    if session.is_kde() and bus is not None:
        try:
            bus.call_sync("org.kde.klipper", "/klipper", "org.kde.klipper.klipper",
                          "setClipboardContents", GLib.Variant("(s)", (text,)), None,
                          Gio.DBusCallFlags.NONE, 2000, None)
            return
        except GLib.Error as exc:
            log.info("Klipper isn't there (%s); trying the command line", exc.message)
    writer = session.clipboard_writer()
    if writer is None:
        raise OSError(_("no clipboard tool: install wl-clipboard (Wayland) or xclip (X11)"))
    # wl-copy and xclip stay behind to serve the clipboard: don't wait for them.
    proc = subprocess.Popen(writer, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    assert proc.stdin is not None
    proc.stdin.write(text.encode())
    proc.stdin.close()


def _note_lines(done: list[Job], shown: int = 3) -> list[str]:
    """The jobs' notes as notification lines (all of them go to the log)."""
    notes = [f"{j.src.name}: {n}" if len(done) > 1 else n for j in done for n in j.notes]
    for note in notes:
        log.info("note: %s", note)
    lines = [f"<i>{_esc(clean_note(n))}</i>" for n in notes[:shown]]
    if len(notes) > shown:
        lines.append("<i>" + _("…and {n} more (see {log})").format(
            n=len(notes) - shown, log=LOG_FILE.name) + "</i>")
    return lines


def _esc(text: str) -> str:
    # Plasma renders a markup subset; it shows "&#x27;" literally, so leave quotes alone.
    return html.escape(_printable(text), quote=False)


def _printable(text: str) -> str:
    """File names that aren't valid UTF-8 (old zips, Windows shares) come through as lone
    surrogates, which D-Bus strings can't carry; show them as U+FFFD instead."""
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _common_folder(paths: list[Path]) -> Path:
    return Path(os.path.commonpath([p.parent for p in paths]))


def _error_detail(text: str | None) -> str:
    """The most telling line of an error: the last one that isn't a tool's generic sign-off.

    ConversionError texts look like "ffmpeg failed:\\n<stderr tail>"; the heading is skipped.
    """
    lines = [ln.strip() for ln in (text or _("failed")).splitlines() if ln.strip()] or [_("failed")]
    body = lines[1:] if len(lines) > 1 else lines
    specific = [ln for ln in body if not ln.startswith(GENERIC_ERRORS)]
    return (specific or body)[-1]


def _setup_log() -> None:
    # Once per process: a second "w" handler would truncate the first's file. hala/__init__.py
    # adds a NullHandler (library style), which doesn't count as set up.
    if any(not isinstance(h, logging.NullHandler) for h in log.handlers):
        return
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(LOG_FILE, mode="w")
    except OSError:
        handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)


def notify(summary: str, body: str, icon: str = "hala", outputs: list[Path] | None = None,
           actions: list[str] | None = None, problem: bool = False) -> None:
    """A one-off notification; its buttons (Open, Show in folder...) work until it's closed.
    Blocks while it can still be clicked, like the end of a batch."""
    try:
        batch = Batch([], "", 1)
    except GLib.Error as exc:  # no session bus
        log.error("can't notify (%s): %s", exc.message, summary)
        if problem:
            _fallback_dialog(summary, body)
        return
    batch.outputs = list(outputs or [])
    batch._notify(summary, body, icon, actions, problem=problem)
    batch._wait_for_notification()


def _describe(files: list[Path]) -> int:
    """No target given: say what the files can become (the app entry's "Open with")."""
    targets = [t.upper() for t in common_targets([f.ext_of(p) for p in files])]
    where = (_("Right-click → Convert to in Dolphin.") if session.is_kde()
             else _("Right-click → Hala… in your file manager, or drag them to the screen "
                    "edge."))
    body = (_("Can become: {formats}.").format(formats=", ".join(targets)) + "<br>" + where
            if targets else _("Hala can't convert these files."))
    notify("Hala", body)
    return 0 if targets else 1


def _doesnt_take(path: Path, tool: Tool) -> str:
    return _("{name}: {tool} doesn't take {format} files").format(
        name=path.name, tool=_(tool.label), format=f.ext_of(path) or _("these"))


def run_action(name: str, files: list[Path], folder: Path | None = None,
               device: str | None = None) -> int:
    """`hala action NAME`: the wheel's bubbles. Asks for what's missing (folder, phone)."""
    from . import actions

    _setup_log()
    # Absolute, but not resolved: a symlink must be copied or moved as the link itself.
    files = [Path(os.path.abspath(p.expanduser())) for p in files]
    log.info("action %s on %d file(s)", name, len(files))
    if name != "shelf" and not files:
        raise ConversionError(_("{action} needs at least one file").format(action=name))
    if name == "zip":
        return run(files, None, tool_name="zip", ask=False)
    if name == "shelf":
        from .gui import shelf

        return shelf.put(files)
    if name in ("copy", "move"):
        if folder is None:
            from .gui.pickers import pick_folder

            folder = pick_folder(_(actions.ACTIONS[name].label), files)
            if folder is None:
                return 1
        return _copy_or_move(name, files, folder)
    if name == "phone":
        return _send_to_phone(files, device)
    raise ConversionError(_("no action called {name!r} (there's {actions})").format(
        name=name, actions=", ".join(actions.ACTIONS)))


def _copy_or_move(name: str, files: list[Path], folder: Path) -> int:
    from .actions import PartialMove, copy_to, move_to

    work = copy_to if name == "copy" else move_to
    done: list[Path] = []
    problems: list[str] = []
    for path in files:
        try:
            done.append(work(path, folder))
        except PartialMove as exc:  # it did arrive: show it, but say what's left behind
            done.append(exc.placed)
            problems.append(f"<b>{_esc(path.name)}</b>: {_esc(str(exc))}")
            log.error("%s %s: %s", name, path, exc)
        except ConversionError as exc:
            problems.append(f"<b>{_esc(path.name)}</b>: {_esc(str(exc))}")
            log.error("%s %s: %s", name, path, exc)
    copying = name == "copy"
    if problems:
        if done:
            summary = (ngettext("Copied {n} of {total} file", "Copied {n} of {total} files",
                                len(files)) if copying else
                       ngettext("Moved {n} of {total} file", "Moved {n} of {total} files",
                                len(files))).format(n=len(done), total=len(files))
        else:
            summary = (ngettext("Couldn't copy it", "Couldn't copy them", len(files)) if copying
                       else ngettext("Couldn't move it", "Couldn't move them", len(files)))
        notify(summary, "<br>".join(problems[:6]), "dialog-warning", done,
               ["show", _("Show in folder")] if done else [], problem=True)
        return 1
    body = (_esc(done[0].name) if len(done) == 1
            else _("{files} in {folder}").format(files=n_files(len(done)),
                                                 folder=_esc(folder.name)))
    summary = (_("Copied to {folder}") if copying else _("Moved to {folder}")).format(
        folder=folder.name)
    notify(summary, body, "hala", done, ["show", _("Show in folder")])
    return 0


def _send_to_phone(files: list[Path], device: str | None) -> int:
    from .actions import phones, send_to_phone

    try:
        found = phones()
    except ConversionError as exc:
        notify(_("Can't send to a phone"), _esc(str(exc)), "dialog-warning", problem=True)
        return 1
    if device is None:
        if not found:
            app = (["kdeconnect", _("Open KDE Connect")] if shutil.which("kdeconnect-app")
                   else [])
            notify(_("No phone connected"), _("Pair a phone in KDE Connect and make sure it's "
                                              "on the same network."),
                   "dialog-warning", actions=app, problem=True)
            return 1
        if len(found) == 1:
            device = found[0][0]
        else:
            from .gui.pickers import pick_device

            device = pick_device(found)
            if device is None:
                return 1
    name = dict(found).get(device, device)
    refused = send_to_phone(files, device)
    for path, reason in refused:
        log.error("phone %s: %s", path, reason)
    sending = len(files) - len(refused)
    if refused:
        lines = [f"<b>{_esc(p.name)}</b>: {_esc(r)}" for p, r in refused[:6]]
        summary = (ngettext("Sending {n} of {total} file to {device}",
                            "Sending {n} of {total} files to {device}", len(files)).format(
                       n=sending, total=len(files), device=name) if sending
                   else _("Couldn't send to {device}").format(device=name))
        notify(summary, "<br>".join(lines), "dialog-warning", problem=True)
        return 1
    what = files[0].name if len(files) == 1 else n_files(len(files))
    # KDE Connect has only queued them; it shows the transfer itself.
    notify(_("Sending to {device}").format(device=name), _esc(what), "kdeconnect")
    return 0


def run(files: list[Path], target: str | None, workers: int = DEFAULT_WORKERS,
        tool_name: str | None = None, ask: bool = True,
        params: dict[str, Any] | None = None) -> int:
    _setup_log()
    files = [p.expanduser().resolve() for p in files]
    if tool_name:
        chosen = tool(tool_name)
        if ask:
            from .gui.dialog import ask as ask_dialog
            from .gui.dialog import needs_dialog

            if needs_dialog(chosen, files):
                answer = ask_dialog(chosen, files)
                if answer is None:
                    log.info("%s cancelled in its dialog", chosen.name)
                    return 1
                files, params = answer
        secret = {p.name for p in chosen.params if p.kind == "password"}
        shown = {k: ("(hidden)" if k in secret and v else v) for k, v in (params or {}).items()}
        log.info("%s on %d file(s) with %s", chosen.name, len(files), shown)
        return Batch(files, chosen.name, workers, tool=chosen, params=params).start()
    if not target:
        return _describe(files)
    log.info("converting %d file(s) to %s", len(files), target)
    return Batch(files, f.normalize_ext(target), workers).start()
