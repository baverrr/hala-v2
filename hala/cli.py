"""Command line: `hala convert`, `tool`, `run`, `dropzone`, `targets`, `formats`,
`install-menus`, `limits`, `doctor`."""

from __future__ import annotations

import argparse
import json
import shutil
import signal
import sys
import threading
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path

from . import __version__
from . import formats as f
from .actions import ACTIONS
from .i18n import N_, _, ngettext
from .jobs import DEFAULT_WORKERS, Job, JobQueue, Status, clean_note
from .registry import ANY, ROUTES, common_targets, targets_for

EXTERNAL_TOOLS = {
    "ffmpeg": N_("video and audio"), "ffprobe": N_("video and audio"), "vips": N_("images"),
    "vipsheader": N_("images"), "soffice": N_("office documents"),
    "pandoc": N_("markup and e-books"), "pdftoppm": N_("PDF to images"),
    "pdftotext": N_("PDF to text"), "7z": N_("archives"), "qpdf": N_("PDF merge and split"),
    "gs": N_("PDF compression"), "exiftool": N_("metadata"), "zbarimg": N_("QR codes"),
    "nice": N_("low-priority jobs"), "ionice": N_("low-priority jobs"),
}


def cmd_targets(args: argparse.Namespace) -> int:
    exts = [f.ext_of(Path(p)) for p in args.files]
    found = common_targets(exts)
    if args.json:
        print(json.dumps(found))
    elif found:
        print(" ".join(found))
    else:
        print(_("no common conversions for these files"), file=sys.stderr)
        return 1
    return 0


def cmd_formats(args: argparse.Namespace) -> int:
    rows: dict[str, list[str]] = {}
    for route in ROUTES:
        for src in sorted(route.sources):
            rows.setdefault(src, targets_for(src) if src != ANY else list(route.targets))
    width = max(map(len, rows))
    for src, outs in rows.items():
        label = _("any file") if src == ANY else src
        print(f"{label:<{max(width, 8)}}  → {' '.join(outs)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import session

    for line in session.describe():
        print(line)
    print()
    missing = 0
    for tool, purpose in EXTERNAL_TOOLS.items():
        path = shutil.which(tool)
        missing += path is None
        missing_note = "" if path else "  " + _("(MISSING)")
        print(f"{'✓' if path else '✗'} {tool:<11} {_(purpose)}{missing_note}")
    return 1 if missing else 0


def cmd_limits(args: argparse.Namespace) -> int:
    from . import limits

    values, problems = limits.read_config()
    if args.change == "set" and problems:  # rewriting it would drop what couldn't be read
        for problem in problems:
            print(f"hala: {problem}", file=sys.stderr)
        print("hala: " + _("fix {path} first, or start again with `hala limits reset`")
              .format(path=limits.config_file()), file=sys.stderr)
        return 1
    if args.change == "set":
        try:
            values[args.key] = limits.parse_value(args.key, args.value)
        except (KeyError, ValueError) as exc:
            print(f"hala: {exc.args[0]}", file=sys.stderr)
            return 2
    elif args.change == "reset":
        if args.key and args.key not in limits.DEFAULTS:
            print(f"hala: {limits.unknown_limit(args.key)}", file=sys.stderr)
            return 2
        values = {k: v for k, v in values.items() if args.key and k != args.key}
    if args.change:
        try:
            path = limits.write_config(values)
        except OSError as exc:
            print("hala: " + _("can't save {path}: {error}").format(
                path=limits.config_file(), error=exc.strerror or exc), file=sys.stderr)
            return 1
        problems = []
    else:
        path = limits.config_file()
    now = limits.current()
    width = max(map(len, now))
    for s in limits.SETTINGS:
        default = limits.DEFAULTS[s.key]
        mark = "" if now[s.key] == default else "  " + _("(default {n:,})").format(n=default)
        print(f"{s.key:<{width}}  {now[s.key]:>10,}  {_(s.help)}{mark}")
    print("\n" + (str(path) if path.exists() else
                  _("no {path}: all defaults").format(path=path)))
    for problem in problems:
        print(_("warning: {problem}").format(problem=problem), file=sys.stderr)
    return 1 if problems else 0


def cmd_convert(args: argparse.Namespace) -> int:
    outdir = Path(args.outdir) if args.outdir else None
    return _run_queue(args, lambda queue: [queue.submit(Path(p), args.to, outdir)
                                           for p in args.files])


def cmd_tool(args: argparse.Namespace) -> int:
    from .tools import tool, tools

    if not args.tool:
        for t in tools().values():
            many = " " + _("(combines several files)") if t.combine else ""
            print(f"{t.name:<10} {_(t.help)}{many}")
        return 0
    chosen = tool(args.tool)
    files = [Path(p) for p in args.files]
    if args.tool == "metadata" and args.show:
        from .tools.metadata import current

        for p in files:
            print(f"{p.name}: " + (json.dumps(current(p), ensure_ascii=False) or "{}"))
        return 0
    given = {p.name: getattr(args, p.name) for p in chosen.params
             if getattr(args, p.name, None) is not None}
    for param in chosen.params:
        # A password on the command line shows up in `ps` for every user: ask for it instead.
        if param.kind == "password" and param.name not in given and sys.stdin.isatty():
            from .engines.pdf import NEEDS_PASSWORD, encryption

            if any(encryption(p) == NEEDS_PASSWORD for p in files):
                import getpass

                given[param.name] = getpass.getpass(f"{_(param.label)}: ")
    outdir = Path(args.outdir) if args.outdir else None
    if chosen.combine:
        return _run_queue(args, lambda queue: [queue.submit_tool(chosen.name, files, given,
                                                                 outdir)])
    return _run_queue(args, lambda queue: [queue.submit_tool(chosen.name, [p], given, outdir)
                                           for p in files])


Submit = Callable[[JobQueue], list[tuple[Job, Future[Job]]]]


def _run_queue(args: argparse.Namespace, submit: Submit) -> int:
    """Run the jobs submit() queues, reporting each as it finishes; Ctrl+C cancels."""
    lock = threading.Lock()
    running: dict[int, Job] = {}
    tty = sys.stderr.isatty() and not args.json

    def status_line() -> None:
        if tty and running:
            parts = [f"{j.src.name[:24]} {j.progress:4.0%}" for j in running.values()]
            sys.stderr.write("\r\033[K" + "  ".join(parts))
            sys.stderr.flush()

    def listener(job: Job) -> None:
        with lock:
            if job.status is Status.RUNNING:
                running[job.id] = job
                status_line()
                return
            running.pop(job.id, None)
            if tty:
                sys.stderr.write("\r\033[K")
            if args.json:
                print(json.dumps({"src": str(job.src), "target": job.target,
                                  "status": job.status.value,
                                  "output": str(job.output) if job.output else None,
                                  "error": job.error, "notes": job.notes}), flush=True)
            elif job.status is Status.DONE:
                print(f"✓ {job.src.name} → {job.output}")
                for note in job.notes:
                    print("  " + _("note: {note}").format(note=clean_note(note)))
            elif job.status is Status.FAILED:
                print(f"✗ {job.src.name}: {job.error}", file=sys.stderr)
            else:
                print(f"- {job.src.name}: {_('cancelled')}", file=sys.stderr)
            status_line()

    queue = JobQueue(workers=args.jobs, listener=listener)
    submitted = submit(queue)
    # Treat `kill` like Ctrl+C, so running tools are stopped and scratch dirs removed.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        results = [future.result() for _job, future in submitted]
    except KeyboardInterrupt:
        for job, _future in submitted:
            job.cancel()
        queue.shutdown(cancel=True)
        for job, future in submitted:
            if future.cancelled():  # never started, so the listener never heard of it
                print(f"- {job.src.name}: {_('cancelled')}", file=sys.stderr)
        return 130
    queue.shutdown()
    return 0 if all(r.status is Status.DONE for r in results) else 1


def cmd_run(args: argparse.Namespace) -> int:
    from .desktop import run  # needs GLib; keep the plain CLI usable without it

    return run([Path(p) for p in args.files], args.to, workers=args.jobs, tool_name=args.tool,
               ask=args.ask)


def positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(_("must be at least 1"))
    return value


def cmd_dropzone(args: argparse.Namespace) -> int:
    if args.autostart:
        from .gui.dropzone import set_autostart  # needs Qt; keep the plain CLI usable without it

        exe = shutil.which("hala") or "hala"
        path = set_autostart(args.autostart == "on", exe, args.edge)
        done = _("autostart enabled: {path}") if args.autostart == "on" else _(
            "autostart disabled: {path}")
        print(done.format(path=path))
        return 0
    from .gui.dropzone import main as dropzone_main

    return dropzone_main(args.edge)


def cmd_app(args: argparse.Namespace) -> int:
    """The application menu's Hala (and Open with → Hala): files are described, as with
    `run`; without files the drop zone starts, or a notification says where it already is."""
    # hala://open (a link from a web page) just starts Hala; it names no files.
    files = [p for p in args.files if not p.startswith("hala:")]
    if files:
        from .desktop import run

        return run([Path(p) for p in files], None)
    from .gui import dropzone  # needs Qt; keep the plain CLI usable without it

    code = dropzone.main()
    if code != dropzone.ALREADY_RUNNING:
        return code
    from .desktop import notify

    notify(_("Hala is running"), _("Drag files to the thin strip at the screen edge, or "
                                   "right-click Hala's tray icon for the shelf."))
    return 0


def cmd_wheel(args: argparse.Namespace) -> int:
    from .gui.wheelapp import main as wheel_main  # needs Qt; keep the plain CLI usable without it

    return wheel_main([Path(p) for p in args.files], from_clipboard=args.clipboard)


def cmd_edit(args: argparse.Namespace) -> int:
    from .gui.editor import main as editor_main  # needs Qt; keep the plain CLI usable without it

    return editor_main([Path(p).expanduser().resolve() for p in args.files])


def cmd_action(args: argparse.Namespace) -> int:
    from .desktop import run_action  # needs GLib; keep the plain CLI usable without it

    return run_action(args.action, [Path(p) for p in args.files],
                      folder=Path(args.to).expanduser() if args.to else None,
                      device=args.device)


def cmd_shelf(args: argparse.Namespace) -> int:
    from .gui.shelf import main as shelf_main  # needs Qt; keep the plain CLI usable without it

    return shelf_main([Path(p) for p in args.files])


def cmd_install_menus(args: argparse.Namespace) -> int:
    from . import filemanagers, servicemenus, shortcut

    if args.remove:
        removed = servicemenus.uninstall()
        print(ngettext("removed {n} Dolphin menu file", "removed {n} Dolphin menu files",
                       removed).format(n=removed))
        if shortcut.remove_gnome():
            print(_("removed the GNOME shortcut"))
        return 0
    exe = shutil.which("hala") or "hala"
    written = servicemenus.install(exe)
    print(ngettext("installed {n} Dolphin menu file in {folder}",
                   "installed {n} Dolphin menu files in {folder}", len(written)).format(
        n=len(written), folder=servicemenus.MENU_DIR))
    others = filemanagers.installed_for(filemanagers.install(exe))
    if others:
        print(_("Hala… is in the right-click menu of: {apps}").format(apps=", ".join(others)))
    if "Thunar" not in others and shutil.which("thunar"):
        print(_("for Thunar: start it once, then run this again"))
    if shortcut.applies() and shortcut.add_gnome(exe):
        print(_("added the GNOME shortcut Super+Shift+H (Settings > Keyboard to change it)"))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hala", description=_("Convert files locally."))
    parser.add_argument("--version", action="version", version=f"hala {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("convert", help=_("convert files; results go next to the originals"))
    p.add_argument("files", nargs="+")
    p.add_argument("-t", "--to", required=True,
                   help=_("target format, e.g. jpg, mp3, pdf, folder"))
    p.add_argument("-o", "--outdir", help=_("put results here instead of next to the originals"))
    p.add_argument("-j", "--jobs", type=positive_int, default=DEFAULT_WORKERS,
                   help=_("conversions at once (default {n})").format(n=DEFAULT_WORKERS))
    p.add_argument("--json", action="store_true", help=_("one JSON line per finished file"))
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("targets", help=_("list what these files can be converted to"))
    p.add_argument("files", nargs="+")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_targets)

    p = sub.add_parser("run",
                       help=_("convert with desktop progress + notifications (for menus)"))
    p.add_argument("files", nargs="+")
    what = p.add_mutually_exclusive_group()
    what.add_argument("-t", "--to",
                      help=_("without it (or --tool), say what the files can become"))
    what.add_argument("--tool", help=_("run a tool instead (asks for its settings first)"))
    p.add_argument("--ask", action=argparse.BooleanOptionalAction, default=True,
                   help=_("show the tool's settings dialog (default: yes)"))
    p.add_argument("-j", "--jobs", type=positive_int, default=DEFAULT_WORKERS)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("dropzone", help=_("drag files to a screen edge to pick a format"))
    p.add_argument("--edge", choices=("right", "left"), default="right")
    p.add_argument("--autostart", choices=("on", "off"),
                   help=_("start the drop zone at login (or stop doing so), then exit"))
    p.set_defaults(func=cmd_dropzone)

    p = sub.add_parser("app", help=_("what the app menu starts: the drop zone (files: what "
                                     "they can become)"))
    p.add_argument("files", nargs="*")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("wheel", help=_("the format wheel for FILES (or --clipboard), without "
                                       "dragging to the screen edge"))
    p.add_argument("files", nargs="*")
    p.add_argument("--clipboard", action="store_true",
                   help=_("use the files last copied (Ctrl+C) instead; the global shortcut "
                          "does this"))
    p.set_defaults(func=cmd_wheel)

    p = sub.add_parser("edit",
                       help=_("crop, annotate or redact pictures or PDFs (saves a copy)"))
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("action", help=_("zip, copy/move to a folder, send to a phone, shelve"))
    p.add_argument("action", choices=tuple(ACTIONS))
    p.add_argument("files", nargs="*", help=_("(only shelf may be given none: it just opens)"))
    p.add_argument("--to", help=_("copy/move: the folder (asks when left out)"))
    p.add_argument("--device",
                   help=_("phone: KDE Connect device id (asks when there are several)"))
    p.set_defaults(func=cmd_action)

    p = sub.add_parser("shelf", help=_("a floating shelf that holds files to drag elsewhere"))
    p.add_argument("files", nargs="*")
    p.set_defaults(func=cmd_shelf)

    p = sub.add_parser("install-menus",
                       help=_("add (or --remove) the file manager menus, app entry and "
                              "shortcut"))
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_install_menus)

    from .limits import DEFAULTS

    p = sub.add_parser("limits", help=_("show or change the size limits (huge pictures, "
                                             "archives)"))
    change = p.add_subparsers(dest="change")
    cp = change.add_parser("set", help=_("change one, e.g. `hala limits set "
                                                "max_megapixels 1000`"))
    cp.add_argument("key", choices=tuple(DEFAULTS))
    cp.add_argument("value")
    cp = change.add_parser("reset", help=_("go back to the default for one (or, without "
                                                  "a name, for all)"))
    cp.add_argument("key", nargs="?")
    p.set_defaults(func=cmd_limits)

    _add_tool_parsers(sub)

    sub.add_parser("formats", help=_("show the whole conversion table")).set_defaults(
        func=cmd_formats)
    sub.add_parser("doctor", help=_("check the converter tools are installed")).set_defaults(
        func=cmd_doctor)
    return parser


def _add_tool_parsers(sub: argparse._SubParsersAction) -> None:
    from .tools import tools

    p = sub.add_parser("tool", help=_("compress, trim, merge… (`hala tool` lists them)"))
    p.set_defaults(func=cmd_tool, tool=None)
    each = p.add_subparsers(dest="tool")
    for t in tools().values():
        tp = each.add_parser(t.name, help=_(t.help), description=_(t.help))
        tp.add_argument("files", nargs="+",
                        help=_("in order") if t.combine else None)
        for param in t.params:
            flag = "--" + param.name
            unit = f" ({_(param.unit)})" if param.unit else ""
            if param.kind == "bool":
                tp.add_argument(flag, action=argparse.BooleanOptionalAction, default=None,
                                help=_(param.help))
            elif param.kind == "choice":
                tp.add_argument(flag, choices=param.choices, help=_(param.help))
            elif param.kind == "password":
                tp.add_argument(flag, metavar="PASSWORD",
                                help=_(param.help) + " " + _("(leave it out to be asked; on the "
                                                             "command line, other users can see "
                                                             "it)"))
            else:
                tp.add_argument(flag, metavar=(param.unit or param.kind).upper(),
                                help=_(param.help) + unit)
        if t.name == "metadata":
            tp.add_argument("--show", action="store_true",
                            help=_("print it instead of changing it"))
        tp.add_argument("-o", "--outdir",
                        help=_("put results here instead of next to the originals"))
        tp.add_argument("-j", "--jobs", type=positive_int, default=DEFAULT_WORKERS)
        tp.add_argument("--json", action="store_true", help=_("one JSON line per finished job"))


# Started from the drop zone, Dolphin or a tray menu: nobody sees their stderr.
DETACHED = {"app", "run", "action", "shelf", "edit", "wheel"}
CRASH_LOG = Path.home() / ".cache" / "hala" / "crash.log"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command not in DETACHED:
        return args.func(args)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        raise
    except Exception as exc:  # last resort: make it visible, then exit
        _report_crash(args.command, exc)
        return 1


def _report_crash(command: str, exc: BaseException) -> None:
    import time
    import traceback

    from .runner import ConversionError

    text = "".join(traceback.format_exception(exc))
    sys.stderr.write(text)
    try:
        CRASH_LOG.parent.mkdir(parents=True, exist_ok=True)
        with CRASH_LOG.open("a") as out:
            out.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} hala {command}\n{text}")
    except OSError:
        pass
    if sys.stderr.isatty():
        return
    message = (str(exc) if isinstance(exc, ConversionError)
               else _("Hala hit a bug: {error}\n\nDetails: {log}").format(error=exc,
                                                                          log=CRASH_LOG))
    from . import session

    session.error_dialog(message)


if __name__ == "__main__":
    sys.exit(main())
