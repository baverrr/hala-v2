"""Office documents via headless LibreOffice; markup and e-books via pandoc."""

from __future__ import annotations

import fcntl
import importlib.util
import json
import re
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any

from ..formats import ext_of
from ..runner import Context, ConversionError

# LibreOffice gets its own profiles so it never attaches to (or is blocked by) a LibreOffice
# window the user has open. A profile serves one conversion at a time, so there is one per
# worker (lo-profile, lo-profile-2, ...), each claimed with a lock file; conversions beyond
# that wait for a free one, in this process or any other hala.
PROFILE = Path.home() / ".cache" / "hala" / "lo-profile"
PROFILES = 2  # jobs.DEFAULT_WORKERS: as many LibreOffice runs as conversions at once
# A conversion that takes longer has almost certainly hung (e.g. on a dialog nobody can see).
SOFFICE_TIMEOUT_S = 10 * 60

EXPORT_FILTERS = {
    "docx": "docx:MS Word 2007 XML",
    "xlsx": "xlsx:Calc MS Excel 2007 XML",
    "pptx": "pptx:Impress MS PowerPoint 2007 XML",
    "csv": "csv:Text - txt - csv (StarCalc):44,34,76",  # comma, double quote, UTF-8
    "txt": "txt:Text (encoded):UTF8",
}
IMPORT_FILTERS = {"txt": "Text (encoded):UTF8", "csv": "Text - txt - csv (StarCalc)"}
# Documents come from anywhere: no macros, no external links or data refreshed on load (a
# spreadsheet could otherwise pull in another local file or call a web server).
SCRIPTING = "/org.openoffice.Office.Common/Security/Scripting"
LOCKDOWN = (
    (SCRIPTING, "DisableMacrosExecution", "xs:boolean", "true"),
    (SCRIPTING, "MacroSecurityLevel", "xs:int", "3"),
    (SCRIPTING, "BlockUntrustedRefererLinks", "xs:boolean", "true"),
    (SCRIPTING, "DisableActiveContent", "xs:boolean", "true"),
    (SCRIPTING, "DisableOLEAutomation", "xs:boolean", "true"),
    ("/org.openoffice.Office.Calc/Content/Update", "Link", "xs:int", "1"),  # never
    ("/org.openoffice.Office.Writer/Content/Update", "Link", "xs:int", "0"),  # never
)
XCU_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n<oor:items '
            'xmlns:oor="http://openoffice.org/2001/registry" '
            'xmlns:xs="http://www.w3.org/2001/XMLSchema" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n')
XCU_TAIL = "</oor:items>\n"


def office(ctx: Context, src: Path, dst_ext: str) -> Path:
    outdir = ctx.workdir / "lo"
    outdir.mkdir()
    with _profile_lock(ctx) as profile:
        cmd: list[str | Path] = ["soffice", f"-env:UserInstallation={profile.as_uri()}",
                                 "--headless", "--norestore", "--nolockcheck"]
        if (infilter := IMPORT_FILTERS.get(ext_of(src))) is not None:
            cmd.append(f"--infilter={infilter}")
        cmd += ["--convert-to", EXPORT_FILTERS.get(dst_ext, dst_ext), "--outdir", outdir, src]
        # svp: LibreOffice's headless display backend; it never touches Wayland/X.
        ctx.run(cmd, env={"SAL_USE_VCLPLUGIN": "svp"}, timeout=SOFFICE_TIMEOUT_S)
    produced = list(outdir.iterdir())
    if not produced:
        # soffice exits 0 even when it can't load or export the file.
        raise ConversionError(f"LibreOffice could not convert this file to {dst_ext.upper()}")
    return produced[0]


def profile_path(slot: int) -> Path:
    return PROFILE if slot == 0 else PROFILE.with_name(f"{PROFILE.name}-{slot + 1}")


@contextmanager
def _profile_lock(ctx: Context) -> Iterator[Path]:
    """A free profile, held (flock) until the block ends; waits, cancellably, while all are
    busy. flock locks belong to the open file, so threads of one process exclude each other
    as well as other processes."""
    PROFILE.parent.mkdir(parents=True, exist_ok=True)
    while True:
        for slot in range(PROFILES):
            profile = profile_path(slot)
            lockfile = open(PROFILE.parent / f"lo{'' if slot == 0 else slot + 1}.lock", "w")  # noqa: SIM115
            try:
                fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                lockfile.close()
                continue
            try:
                profile.mkdir(exist_ok=True)
                lock_down_profile(profile)
                yield profile
            finally:
                lockfile.close()  # releases the flock
            return
        ctx.check_cancelled()
        ctx.cancel_event.wait(0.2)


def lock_down_profile(profile: Path = PROFILE) -> None:
    """Write LOCKDOWN into the profile's settings (LibreOffice keeps whatever else it saved)."""
    xcu = profile / "user" / "registrymodifications.xcu"
    try:
        text = xcu.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = XCU_HEAD + XCU_TAIL
    if XCU_TAIL.strip() not in text:
        text = XCU_HEAD + XCU_TAIL  # unreadable leftovers: LibreOffice rebuilds the rest
    for path, prop, _kind, _value in LOCKDOWN:
        text = re.sub(rf'<item oor:path="{re.escape(path)}"><prop oor:name="{prop}"[^\n]*\n?',
                      "", text)
    ours = "".join(f'<item oor:path="{path}"><prop oor:name="{prop}" oor:op="fuse">'
                   f'<value>{value}</value></prop></item>\n'
                   for path, prop, _kind, value in LOCKDOWN)
    end = text.rindex(XCU_TAIL.strip())
    xcu.parent.mkdir(parents=True, exist_ok=True)
    xcu.write_text(text[:end] + ours + text[end:], encoding="utf-8")


PANDOC_WRITERS = {"md": "gfm", "txt": "plain", "html": "html5"}
# Metadata fields that name a file for a writer to read in (EPUB cover, stylesheet).
FILE_METADATA = ("cover-image", "epub-cover-image", "css", "stylesheet")
# Attributes in raw HTML that the EPUB writer fetches and embeds.
RAW_SOURCE = re.compile(r"""\b(?:src|poster|data)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
                        re.IGNORECASE)
URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def pandoc(ctx: Context, src: Path, dst_ext: str) -> Path:
    if dst_ext == "pdf" and importlib.util.find_spec("weasyprint") is None:
        # No WeasyPrint (and pandoc's default PDF route needs LaTeX): DOCX, then LibreOffice.
        docx = pandoc(ctx, src, "docx")
        return office(ctx, docx, "pdf")
    # Pass 1 parses in pandoc's sandbox, so include directives (".. include::", \input,
    # #+INCLUDE) can't read other files. Pass 2 writes from the checked syntax tree; only
    # pictures inside the document's own folder may be embedded, never other local files
    # (~/.ssh/...) or URLs.
    media = ctx.workdir / "media"
    tree = json.loads(ctx.run(["pandoc", "--sandbox", src, "-t", "json",
                               f"--extract-media={media}"], cwd=ctx.workdir))
    roots = (src.parent.resolve(), media.resolve())
    meta = tree.get("meta", {})
    for key in FILE_METADATA:
        meta.pop(key, None)
    left_out: list[str] = []

    def allowed(target: str) -> bool:
        if _is_local(target, src.parent, roots):
            return True
        left_out.append(target)
        return False

    # WeasyPrint fetches whatever HTML points at (stylesheets, url() in styles, <link>), so
    # for a PDF no raw HTML survives at all, not even from header-includes metadata.
    _confine(tree, allowed, drop_raw=dst_ext == "pdf")
    if left_out:
        shown = ", ".join(PurePosixPath(t).name or t for t in left_out[:3])
        more = f" and {len(left_out) - 3} more" if len(left_out) > 3 else ""
        ctx.note(f"left out {len(left_out)} picture{'s' * (len(left_out) != 1)} from outside "
                 f"the document's folder: {shown}{more}")
    # Named after the source: a standalone HTML page without a title is titled by this name.
    parsed = ctx.workdir / "parsed" / f"{src.stem}.json"
    parsed.parent.mkdir()
    parsed.write_text(json.dumps(tree), encoding="utf-8")
    # A PDF is made from standalone HTML by WeasyPrint, confined to the same folders.
    write_ext = "html" if dst_ext == "pdf" else dst_ext
    out = ctx.workdir / f"out.{write_ext}"
    cmd: list[str | Path] = ["pandoc", "-f", "json", parsed, "-o", out]
    if (writer := PANDOC_WRITERS.get(write_ext)) is not None:
        cmd += ["-t", writer]
    if write_ext in ("html", "epub"):
        cmd.append("--standalone")
    # Relative image links ("![](img/x.png)") are resolved against the source's folder.
    cmd += ["--resource-path", src.parent]
    ctx.run(cmd, cwd=ctx.workdir)
    if dst_ext != "pdf":
        return out
    pdf = ctx.workdir / "out.pdf"
    ctx.run([sys.executable, "-I", "-m", "hala.engines.weasy_confined", out, pdf, src.parent,
             *roots, ctx.workdir], cwd=ctx.workdir, timeout=SOFFICE_TIMEOUT_S)
    return pdf


def _is_local(target: str, base: Path, roots: tuple[Path, ...]) -> bool:
    """Whether a picture reference stays inside one of roots (data: URIs carry their own)."""
    if target.startswith(("data:", "#")) or not target:
        return True
    if URI_SCHEME.match(target) or target.startswith("~"):
        return False
    try:
        where = (base / target.split("#")[0].split("?")[0]).resolve()
    except (OSError, ValueError):
        return False
    return any(where.is_relative_to(root) for root in roots)


def _confine(node: Any, allowed: Callable[[str], bool], drop_raw: bool = False) -> Any:
    """Replace pictures and raw HTML that point outside allowed with their alt text/nothing;
    with drop_raw, all raw HTML goes."""
    if isinstance(node, list):
        for i, item in enumerate(node):
            node[i] = _confine(item, allowed, drop_raw)
    elif isinstance(node, dict):
        kind = node.get("t")
        if kind == "Image":
            attr, alt, (target, _title) = node["c"]
            if not allowed(target):
                return {"t": "Span", "c": [attr, _confine(alt, allowed)]}
        elif kind in ("RawInline", "RawBlock"):
            sources = ["".join(m) for m in RAW_SOURCE.findall(node["c"][1])]
            if drop_raw or not all(allowed(s) for s in sources):
                return {"t": "Str", "c": ""} if kind == "RawInline" else {"t": "Plain", "c": []}
        for key, value in node.items():
            if key != "t":
                node[key] = _confine(value, allowed, drop_raw)
    return node
