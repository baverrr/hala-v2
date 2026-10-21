"""`hala run`: convert from a menu or drop, reporting through Plasma's own job progress.

Progress shows in Plasma's job tracker (like a file copy), with a working Cancel button. When
the batch ends a notification offers Open / Show in folder, or lists what failed with a
Details button for the full log. A cancelled batch only gets a notification if some files
were already converted or skipped. Without a target (the app entry's "Open with"), it just
says what the files can become.
"""

from __future__ import annotations

import html
import logging
import os
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
from gi.repository import Gio, GLib, GLibUnix  # noqa: E402  # pyright: ignore[reportAttributeAccessIssue]

from . import formats as f  # noqa: E402
from .jobs import DEFAULT_WORKERS, Job, JobQueue, Status, clean_note  # noqa: E402
from .registry import common_targets, route_for  # noqa: E402
from .runner import ConversionError  # noqa: E402
from .tools import Tool, tool  # noqa: E402

APP_ID = "hala"
JOB_SERVER = ("org.kde.JobViewServer", "/JobViewServer", "org.kde.JobViewServerV2")
JOB_VIEW = "org.kde.JobViewV2"
NOTIFY = ("org.freedesktop.Notifications", "/org/freedesktop/Notifications",
          "org.freedesktop.Notifications")
FILE_MANAGER = ("org.freedesktop.FileManager1", "/org/freedesktop/FileManager1",
                "org.freedesktop.FileManager1")
# JobViewV2 capability bits: 1 = Killable, which gives the entry a Cancel button.
KILLABLE = 1
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
        self.view: JobView | None = None
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
                    self.skipped.append(f"{path.name} is already {self.target.upper()}")
                elif route_for(ext, self.target) is None:
                    self.skipped.append(f"{path.name} can't become {self.target.upper()}")
                else:
                    self.jobs.append(self.queue.submit(path, self.target)[0])
        if not self.jobs:
            log.warning("nothing to convert: %s", "; ".join(self.skipped))
            self._notify("Nothing to convert", "\n".join(self.skipped), "dialog-information",
                         problem=True)
            self._wait_for_notification()
            return 1
        self.view = JobView(self.bus, self._cancel_all)
        # Logout or `kill` should cancel cleanly (tools stopped, scratch removed), not orphan.
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            GLibUnix.signal_add(GLib.PRIORITY_HIGH, sig, self._on_signal)
        count = len(self.jobs)
        doing = (f"{self.tool.label}: {len(self.files)} file{'s' * (len(self.files) != 1)}"
                 if self.tool else
                 f"Converting {count} file{'s' * (count != 1)} to {self.target.upper()}")
        self.view.call("setInfoMessage", "s", doing)
        self.view.call("setTotalAmount", "ts", count, "files")
        self.view.call("setDescriptionField", "uss", 0, "File", self.jobs[0].src.name)
        self.loop.run()
        return 0 if all(j.status is Status.DONE for j in self.jobs) and not self.skipped else 1

    def _submit_tool(self, tool: Tool) -> None:
        if tool.combine:
            usable = [p for p in self.files if tool.accepts([p, p])]
            self.skipped += [f"{p.name}: {tool.label} doesn't take {f.ext_of(p) or 'these'} files"
                             for p in self.files if p not in usable]
            if len(usable) >= tool.min_files:
                self.jobs.append(self.queue.submit_tool(tool.name, usable, self.params)[0])
            elif usable:
                self.skipped.append(f"{tool.label} needs at least {tool.min_files} files")
            return
        for path in self.files:
            if tool.accepts([path]):
                self.jobs.append(self.queue.submit_tool(tool.name, [path], self.params)[0])
            else:
                self.skipped.append(f"{path.name}: {tool.label} doesn't take "
                                    f"{f.ext_of(path) or 'these'} files")

    def _title(self) -> str:
        return self.tool.done if self.tool else f"Converted to {self.target.upper()}"

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
            self.view.call("setDescriptionField", "uss", 0, "File", job.src.name)
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
            self.view.call("terminate", "s", "Cancelled")
            self.loop.quit()
            return
        else:
            # The notification below carries the details.
            self.view.call("terminate", "s", "Cancelled" if cancelled else "")
            self._notify_problems(done, failed, cancelled)
        self._wait_for_notification()

    def _notify_success(self, done: list[Job]) -> None:
        outputs = [j.output for j in done if j.output]
        self.copy_text = ""
        if len(outputs) == 1:
            body = _esc(outputs[0].name)
            actions = ["open", "Open", "show", "Show in folder"]
            if self.tool is not None and self.tool.shows_text and outputs[0].is_file():
                # Short results (QR codes) are the point: show them, and offer to copy them.
                self.copy_text = outputs[0].read_text(errors="replace").strip()
                body = _esc(self.copy_text[:TEXT_PREVIEW]).replace("\n", "<br>")
                actions = ["copy", "Copy", *actions]
        else:
            folder = _common_folder(outputs).name
            body = f"{len(outputs)} files in {_esc(folder)}"
            actions = ["show", "Show in folder"]
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
            lines.append("The rest were cancelled.")
        total = len(self.jobs) + len(self.skipped)
        if self.tool is not None:
            summary = (f"{self.tool.done}: {len(done)} of {total}" if done
                       else f"{self.tool.label} didn't work")
        else:
            summary = (f"{len(done)} of {total} converted to {self.target.upper()}" if done
                       else f"Couldn't convert to {self.target.upper()}")
        actions = ["show", "Show in folder"] if self.outputs else []
        if failed:
            actions += ["details", "Details"]
        self._notify(summary, "<br>".join(lines), "dialog-warning", actions, problem=True)

    def _notify(self, summary: str, body: str, icon: str,
                actions: list[str] | None = None, problem: bool = False) -> None:
        hints = {"desktop-entry": GLib.Variant("s", APP_ID)}
        summary, body = _printable(summary), _printable(body)
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
                self.bus.call_sync("org.kde.klipper", "/klipper", "org.kde.klipper.klipper",
                                   "setClipboardContents", GLib.Variant("(s)", (self.copy_text,)),
                                   None, Gio.DBusCallFlags.NONE, 2000, None)
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
    if kdialog := shutil.which("kdialog"):
        text = summary + "\n\n" + html.unescape(body.replace("<br>", "\n").replace("<b>", "")
                                                .replace("</b>", ""))
        subprocess.Popen([kdialog, "--title", "Hala", "--sorry", text],
                         stdin=subprocess.DEVNULL, start_new_session=True)


def _note_lines(done: list[Job], shown: int = 3) -> list[str]:
    """The jobs' notes as notification lines (all of them go to the log)."""
    notes = [f"{j.src.name}: {n}" if len(done) > 1 else n for j in done for n in j.notes]
    for note in notes:
        log.info("note: %s", note)
    lines = [f"<i>{_esc(clean_note(n))}</i>" for n in notes[:shown]]
    if len(notes) > shown:
        lines.append(f"<i>…and {len(notes) - shown} more (see {LOG_FILE.name})</i>")
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
    lines = [ln.strip() for ln in (text or "failed").splitlines() if ln.strip()] or ["failed"]
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
    body = (f"Can become: {', '.join(targets)}.<br>Right-click → Convert to in Dolphin."
            if targets else "Hala can't convert these files.")
    notify("Hala", body)
    return 0 if targets else 1


def _plural(count: int, word: str = "file") -> str:
    return f"{count} {word}{'s' * (count != 1)}"


def run_action(name: str, files: list[Path], folder: Path | None = None,
               device: str | None = None) -> int:
    """`hala action NAME`: the wheel's bubbles. Asks for what's missing (folder, phone)."""
    from . import actions

    _setup_log()
    # Absolute, but not resolved: a symlink must be copied or moved as the link itself.
    files = [Path(os.path.abspath(p.expanduser())) for p in files]
    log.info("action %s on %d file(s)", name, len(files))
    if name != "shelf" and not files:
        raise ConversionError(f"{name} needs at least one file")
    if name == "zip":
        return run(files, None, tool_name="zip", ask=False)
    if name == "shelf":
        from .gui import shelf

        return shelf.put(files)
    if name in ("copy", "move"):
        if folder is None:
            from .gui.pickers import pick_folder

            folder = pick_folder("Copy to" if name == "copy" else "Move to", files)
            if folder is None:
                return 1
        return _copy_or_move(name, files, folder)
    if name == "phone":
        return _send_to_phone(files, device)
    raise ConversionError(f"no action called {name!r} (there's {', '.join(actions.ACTIONS)})")


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
    verb = "Copied" if name == "copy" else "Moved"
    if problems:
        summary = (f"{verb} {len(done)} of {_plural(len(files))}" if done
                   else f"Couldn't {name} {'it' if len(files) == 1 else 'them'}")
        notify(summary, "<br>".join(problems[:6]), "dialog-warning", done,
               ["show", "Show in folder"] if done else [], problem=True)
        return 1
    body = _esc(done[0].name) if len(done) == 1 else f"{_plural(len(done))} in {_esc(folder.name)}"
    notify(f"{verb} to {folder.name}", body, "hala", done, ["show", "Show in folder"])
    return 0


def _send_to_phone(files: list[Path], device: str | None) -> int:
    from .actions import phones, send_to_phone

    try:
        found = phones()
    except ConversionError as exc:
        notify("Can't send to a phone", _esc(str(exc)), "dialog-warning", problem=True)
        return 1
    if device is None:
        if not found:
            app = ["kdeconnect", "Open KDE Connect"] if shutil.which("kdeconnect-app") else []
            notify("No phone connected", "Pair a phone in KDE Connect and make sure it's on "
                   "the same network.", "dialog-warning", actions=app, problem=True)
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
        summary = (f"Sending {sending} of {_plural(len(files))} to {name}" if sending
                   else f"Couldn't send to {name}")
        notify(summary, "<br>".join(lines), "dialog-warning", problem=True)
        return 1
    what = files[0].name if len(files) == 1 else _plural(len(files))
    # KDE Connect has only queued them; it shows the transfer itself.
    notify(f"Sending to {name}", _esc(what), "kdeconnect")
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
