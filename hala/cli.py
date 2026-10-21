"""Command line: `hala convert`, `tool`, `run`, `dropzone`, `targets`, `formats`,
`install-menus`, `doctor`."""

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
from .jobs import DEFAULT_WORKERS, Job, JobQueue, Status
from .registry import ANY, ROUTES, common_targets, targets_for

EXTERNAL_TOOLS = {
    "ffmpeg": "video and audio", "ffprobe": "video and audio", "vips": "images",
    "vipsheader": "images", "soffice": "office documents", "pandoc": "markup and e-books",
    "pdftoppm": "PDF to images", "pdftotext": "PDF to text", "7z": "archives",
    "qpdf": "PDF merge and split", "gs": "PDF compression", "exiftool": "metadata",
    "zbarimg": "QR codes", "nice": "low-priority jobs", "ionice": "low-priority jobs",
}


def cmd_targets(args: argparse.Namespace) -> int:
    exts = [f.ext_of(Path(p)) for p in args.files]
    found = common_targets(exts)
    if args.json:
        print(json.dumps(found))
    elif found:
        print(" ".join(found))
    else:
        print("no common conversions for these files", file=sys.stderr)
        return 1
    return 0


def cmd_formats(args: argparse.Namespace) -> int:
    rows: dict[str, list[str]] = {}
    for route in ROUTES:
        for src in sorted(route.sources):
            rows.setdefault(src, targets_for(src) if src != ANY else list(route.targets))
    width = max(map(len, rows))
    for src, outs in rows.items():
        label = "any file" if src == ANY else src
        print(f"{label:<{max(width, 8)}}  → {' '.join(outs)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    missing = 0
    for tool, purpose in EXTERNAL_TOOLS.items():
        path = shutil.which(tool)
        missing += path is None
        print(f"{'✓' if path else '✗'} {tool:<11} {purpose}{'' if path else '  (MISSING)'}")
    return 1 if missing else 0


def cmd_convert(args: argparse.Namespace) -> int:
    outdir = Path(args.outdir) if args.outdir else None
    return _run_queue(args, lambda queue: [queue.submit(Path(p), args.to, outdir)
                                           for p in args.files])


def cmd_tool(args: argparse.Namespace) -> int:
    from .tools import tool, tools

    if not args.tool:
        for t in tools().values():
            many = " (combines several files)" if t.combine else ""
            print(f"{t.name:<10} {t.help}{many}")
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
                                  "error": job.error}), flush=True)
            elif job.status is Status.DONE:
                print(f"✓ {job.src.name} → {job.output}")
            elif job.status is Status.FAILED:
                print(f"✗ {job.src.name}: {job.error}", file=sys.stderr)
            else:
                print(f"- {job.src.name}: cancelled", file=sys.stderr)
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
                print(f"- {job.src.name}: cancelled", file=sys.stderr)
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
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def cmd_dropzone(args: argparse.Namespace) -> int:
    if args.autostart:
        from .gui.dropzone import set_autostart  # needs Qt; keep the plain CLI usable without it

        exe = shutil.which("hala") or "hala"
        path = set_autostart(args.autostart == "on", exe, args.edge)
        print(f"autostart {'enabled' if args.autostart == 'on' else 'disabled'}: {path}")
        return 0
    from .gui.dropzone import main as dropzone_main

    return dropzone_main(args.edge)


def cmd_app(args: argparse.Namespace) -> int:
    """The application menu's Hala (and Open with → Hala): files are described, as with
    `run`; without files the drop zone starts, or a notification says where it already is."""
    if args.files:
        from .desktop import run

        return run([Path(p) for p in args.files], None)
    from .gui import dropzone  # needs Qt; keep the plain CLI usable without it

    code = dropzone.main()
    if code != dropzone.ALREADY_RUNNING:
        return code
    from .desktop import notify

    notify("Hala is running", "Drag files to the thin strip at the screen edge, or right-click "
           "Hala's tray icon for the shelf.")
    return 0


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
    from . import servicemenus

    if args.remove:
        print(f"removed {servicemenus.uninstall()} Dolphin menu files")
        return 0
    written = servicemenus.install()
    print(f"installed {len(written)} Dolphin menu files in {servicemenus.MENU_DIR}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hala", description="Convert files locally.")
    parser.add_argument("--version", action="version", version=f"hala {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("convert", help="convert files; results go next to the originals")
    p.add_argument("files", nargs="+")
    p.add_argument("-t", "--to", required=True, help="target format, e.g. jpg, mp3, pdf, folder")
    p.add_argument("-o", "--outdir", help="put results here instead of next to the originals")
    p.add_argument("-j", "--jobs", type=positive_int, default=DEFAULT_WORKERS,
                   help=f"conversions at once (default {DEFAULT_WORKERS})")
    p.add_argument("--json", action="store_true", help="one JSON line per finished file")
    p.set_defaults(func=cmd_convert)

    p = sub.add_parser("targets", help="list what these files can be converted to")
    p.add_argument("files", nargs="+")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_targets)

    p = sub.add_parser("run", help="convert with Plasma progress + notifications (for menus)")
    p.add_argument("files", nargs="+")
    what = p.add_mutually_exclusive_group()
    what.add_argument("-t", "--to", help="without it (or --tool), say what the files can become")
    what.add_argument("--tool", help="run a tool instead (asks for its settings first)")
    p.add_argument("--ask", action=argparse.BooleanOptionalAction, default=True,
                   help="show the tool's settings dialog (default: yes)")
    p.add_argument("-j", "--jobs", type=positive_int, default=DEFAULT_WORKERS)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("dropzone", help="drag files to a screen edge to pick a format")
    p.add_argument("--edge", choices=("right", "left"), default="right")
    p.add_argument("--autostart", choices=("on", "off"),
                   help="start the drop zone at login (or stop doing so), then exit")
    p.set_defaults(func=cmd_dropzone)

    p = sub.add_parser("app", help="what the app menu starts: the drop zone (files: what they "
                       "can become)")
    p.add_argument("files", nargs="*")
    p.set_defaults(func=cmd_app)

    p = sub.add_parser("edit", help="crop, annotate or redact pictures (saves a copy)")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_edit)

    p = sub.add_parser("action", help="zip, copy/move to a folder, send to a phone, shelve")
    p.add_argument("action", choices=tuple(ACTIONS))
    p.add_argument("files", nargs="*", help="(only shelf may be given none: it just opens)")
    p.add_argument("--to", help="copy/move: the folder (asks when left out)")
    p.add_argument("--device", help="phone: KDE Connect device id (asks when there are several)")
    p.set_defaults(func=cmd_action)

    p = sub.add_parser("shelf", help="a floating shelf that holds files to drag elsewhere")
    p.add_argument("files", nargs="*")
    p.set_defaults(func=cmd_shelf)

    p = sub.add_parser("install-menus", help="add (or --remove) the Dolphin Convert to menus")
    p.add_argument("--remove", action="store_true")
    p.set_defaults(func=cmd_install_menus)

    _add_tool_parsers(sub)

    sub.add_parser("formats", help="show the whole conversion table").set_defaults(
        func=cmd_formats)
    sub.add_parser("doctor", help="check the converter tools are installed").set_defaults(
        func=cmd_doctor)
    return parser


def _add_tool_parsers(sub: argparse._SubParsersAction) -> None:
    from .tools import tools

    p = sub.add_parser("tool", help="compress, trim, merge… (`hala tool` lists them)")
    p.set_defaults(func=cmd_tool, tool=None)
    each = p.add_subparsers(dest="tool")
    for t in tools().values():
        tp = each.add_parser(t.name, help=t.help, description=t.help)
        tp.add_argument("files", nargs="+",
                        help="in order" if t.combine else None)
        for param in t.params:
            flag = "--" + param.name
            unit = f" ({param.unit})" if param.unit else ""
            if param.kind == "bool":
                tp.add_argument(flag, action=argparse.BooleanOptionalAction, default=None,
                                help=param.help)
            elif param.kind == "choice":
                tp.add_argument(flag, choices=param.choices, help=param.help)
            else:
                tp.add_argument(flag, metavar=(param.unit or param.kind).upper(),
                                help=param.help + unit)
        if t.name == "metadata":
            tp.add_argument("--show", action="store_true", help="print it instead of changing it")
        tp.add_argument("-o", "--outdir", help="put results here instead of next to the originals")
        tp.add_argument("-j", "--jobs", type=positive_int, default=DEFAULT_WORKERS)
        tp.add_argument("--json", action="store_true", help="one JSON line per finished job")


# Started from the drop zone, Dolphin or a tray menu: nobody sees their stderr.
DETACHED = {"app", "run", "action", "shelf", "edit"}
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
               else f"Hala hit a bug: {exc}\n\nDetails: {CRASH_LOG}")
    if kdialog := shutil.which("kdialog"):
        import subprocess

        subprocess.Popen([kdialog, "--title", "Hala", "--sorry", message],
                         stdin=subprocess.DEVNULL, start_new_session=True)


if __name__ == "__main__":
    sys.exit(main())
