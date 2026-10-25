"""Tools: things to do to a file that aren't a format change (compress, trim, merge…).

Each Tool declares what it accepts and what settings it takes; the CLI (`hala tool`), the
settings dialogs, the Dolphin "Hala tools" menus and the wheel's inner ring are all generated
from these declarations. Like conversions, tools write a new file next to the original.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import formats as f
from ..convert import produce
from ..i18n import N_, _, ngettext
from ..registry import ANY  # in Tool.sources: any file or folder
from ..runner import Context, ConversionError

Params = dict[str, Any]
Engine = Callable[[Context, list[Path], Params], Path]


# How a wrong value's kind is named: "Loudness: 'loud' isn't a valid number".
KIND_NAMES = {"float": N_("number"), "int": N_("whole number"), "time": N_("time")}


@dataclass(frozen=True)
class Param:
    """label, help and unit are English, marked with N_(): shown through _()."""

    name: str
    label: str
    # "float", "int", "str", "password" (a str, masked and never logged), "bool", "choice"
    # or "time" (seconds, typed as 1:23.5)
    kind: str
    default: Any = None
    help: str = ""
    choices: tuple[str, ...] = ()
    unit: str = ""
    # Only shown and used for these input extensions (e.g. "precise" only for video).
    only_for: frozenset[str] | None = None
    # Default worked out from the files, e.g. half their size for "compress to".
    suggest: Callable[[list[Path]], Any] | None = None
    # Grey hint text in the dialog, e.g. the file's current title.
    placeholder: Callable[[list[Path]], str] | None = None

    def default_for(self, files: Sequence[Path]) -> Any:
        if self.suggest is not None and files:
            try:
                return self.suggest(list(files))
            except (OSError, ValueError):
                pass
        return self.default

    def applies_to(self, files: Sequence[Path]) -> bool:
        return self.only_for is None or any(f.ext_of(p) in self.only_for for p in files)

    def parse(self, raw: Any) -> Any:
        """A typed value from a string (CLI) or an already-typed value (dialog)."""
        if raw is None or raw == "":
            return self.default
        try:
            if self.kind == "float":
                return float(raw)
            if self.kind == "int":
                return int(raw)
            if self.kind == "bool":
                if isinstance(raw, bool):
                    return raw
                return str(raw).lower() in ("1", "yes", "true", "on")
            if self.kind == "time":
                return raw if isinstance(raw, int | float) else parse_time(str(raw))
        except ValueError:
            raise ConversionError(_("{setting}: {value!r} isn't a valid {kind}").format(
                setting=_(self.label), value=raw, kind=_(KIND_NAMES.get(self.kind, self.kind))
            )) from None
        if self.kind == "choice" and raw not in self.choices:
            raise ConversionError(_("{setting}: pick one of {choices}").format(
                setting=_(self.label), choices=", ".join(self.choices)))
        return str(raw)


@dataclass(frozen=True)
class Tool:
    """label, help, done, button and short are English, marked with N_(): shown through _().
    suffix() and output_name() are called at run time, so they translate themselves."""

    name: str  # CLI name: "compress"
    label: str  # menu and wheel: "Compress"
    sources: frozenset[str]
    engine: Engine
    suffix: Callable[[Params], str]  # output naming: "pic (compressed).jpg"
    params: tuple[Param, ...] = ()
    combine: bool = False  # all files -> one output (merge, join) instead of one each
    min_files: int = 1
    icon: str = "tools"
    help: str = ""
    # The output is short text worth showing in the notification (QR codes).
    shows_text: bool = False
    done: str = ""  # notification title when it worked: "Compressed"
    button: str = ""  # the dialog's OK button, if the label isn't a verb
    short: str = ""  # label on the wheel's tool ring, where space is tight
    # Not on the wheel's tool ring or in the Dolphin menus (zip: an action bubble instead).
    hidden: bool = False
    # The output's name (without extension) instead of "first (suffix)": "Archive".
    output_name: Callable[[list[Path]], str] | None = None
    # Cleans up the selection before running (zip: drop files inside selected folders).
    tidy: Callable[[list[Path]], list[Path]] | None = None

    @property
    def asks(self) -> bool:
        """Whether it can open a settings dialog (menus then show "Compress…")."""
        return bool(self.params) or self.combine

    def accepts(self, files: Sequence[Path]) -> bool:
        return (len(files) >= self.min_files
                and (ANY in self.sources or all(f.ext_of(p) in self.sources for p in files)))

    def visible_params(self, files: Sequence[Path]) -> list[Param]:
        return [p for p in self.params if p.applies_to(files)]

    def resolve(self, files: Sequence[Path], given: Params | None = None) -> Params:
        """Every parameter's value: what was given, else the suggestion/default."""
        given = given or {}
        return {p.name: p.parse(given[p.name]) if p.name in given else p.default_for(files)
                for p in self.params}


def parse_time(text: str) -> float:
    """Seconds from "83.5", "1:23.5" or "1:02:03"."""
    text = text.strip()
    if not re.fullmatch(r"\d+(:\d{1,2}){0,2}(\.\d+)?", text):
        raise ValueError(text)
    seconds = 0.0
    for part in text.split(":"):
        seconds = seconds * 60 + float(part)
    return seconds


def format_time(seconds: float) -> str:
    whole = int(seconds)
    h, m, s = whole // 3600, whole // 60 % 60, seconds - whole // 60 * 60
    return f"{h}:{m:02d}:{s:05.2f}" if h else f"{m}:{s:05.2f}"


def needs_files(tool: Tool) -> str:
    return ngettext("{tool} needs at least {n} file", "{tool} needs at least {n} files",
                    tool.min_files).format(tool=_(tool.label), n=tool.min_files)


def run_tool(tool: Tool, inputs: list[Path], params: Params | None = None, *,
             outdir: Path | None = None, on_progress: Callable[[float], None] | None = None,
             cancel_event: threading.Event | None = None,
             notes: list[str] | None = None) -> Path:
    inputs = [p.expanduser().resolve() for p in inputs]
    for p in inputs:
        if not p.exists():
            raise ConversionError(_("{path} does not exist").format(path=p))
    if not tool.accepts(inputs):
        if len(inputs) < tool.min_files:
            raise ConversionError(needs_files(tool))
        odd = next(p for p in inputs if f.ext_of(p) not in tool.sources)
        raise ConversionError(_("{tool} doesn't work on {format} files").format(
            tool=_(tool.label), format=f.ext_of(odd) or odd.name))
    if tool.tidy is not None:
        inputs = tool.tidy(inputs)
    values = tool.resolve(inputs, params)
    first = inputs[0]

    def name(produced: Path) -> str:
        label = (tool.output_name(inputs) if tool.output_name
                 else f"{f.stem_of(first)} ({tool.suffix(values)})")
        return label if produced.is_dir() else label + f.split_name(produced.name)[1]

    return produce(first, lambda ctx: tool.engine(ctx, inputs, values), name, outdir=outdir,
                   on_progress=on_progress, cancel_event=cancel_event, notes=notes)


def _registry() -> dict[str, Tool]:
    from . import compress, edit_media, metadata, pdftools, qr, zipping

    tools = [compress.TOOL, metadata.TOOL, pdftools.MERGE, pdftools.SPLIT, pdftools.UNLOCK,
             qr.TOOL,
             edit_media.NORMALIZE, edit_media.TRIM, edit_media.SPLIT, edit_media.JOIN,
             zipping.TOOL]
    return {t.name: t for t in tools}


TOOLS: dict[str, Tool] = {}


def tools() -> dict[str, Tool]:
    if not TOOLS:
        TOOLS.update(_registry())
    return TOOLS


def tool(name: str) -> Tool:
    try:
        return tools()[name]
    except KeyError:
        raise ConversionError(_("no tool called {name!r} (there's {tools})").format(
            name=name, tools=", ".join(tools()))) from None


def tools_for(files: Sequence[Path]) -> list[Tool]:
    """Tools that can take this selection (each file, or all of them together), in order.
    Hidden tools (zip) are left out: they're reached another way."""
    return [t for t in tools().values() if not t.hidden and t.accepts(files)]
