"""Translations: gettext catalogues, kept as plain .po files in hala/locale/.

    from .i18n import _, N_, ngettext

    _("Compress")                                  # translated now
    ngettext("{n} file", "{n} files", n).format(n=n)
    N_("Compress")                                 # only marked: translated where it's shown
    pgettext("format", "Text")                     # one word, two meanings: a context each

Text that's defined once and shown later (tool labels, parameter help, format names) is
marked with N_() and passed through _() where it's shown, so the Dolphin menus can be written
in every language from the English text, and tests can switch languages on the fly.

The language comes from HALA_LANGUAGE, else the usual LANGUAGE, LC_ALL, LC_MESSAGES and LANG
as GNU gettext reads them: LANGUAGE (which can list several, "pt_BR:pt:es") counts unless the
locale itself is C or POSIX, so `LC_ALL=C hala ...` is English. "de_AT.UTF-8" tries de_AT.po,
then de.po. Without a match, the text stays English.

The .po files are read directly (compiled to a .mo in memory, a few milliseconds), so there's
no build step and nothing binary in git. `tools/i18n.py` extracts the strings and updates them.
"""

from __future__ import annotations

import ast
import gettext
import io
import os
import struct
from collections.abc import Iterator
from functools import cache
from pathlib import Path

LOCALE_DIR = Path(__file__).parent / "locale"
ENV_VARS = ("HALA_LANGUAGE", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")
LOCALE_VARS = ("LC_ALL", "LC_MESSAGES", "LANG")

_current: gettext.NullTranslations = gettext.NullTranslations()
_language = "en"


def _(message: str) -> str:
    return _current.gettext(message)


def ngettext(singular: str, plural: str, n: int) -> str:
    return _current.ngettext(singular, plural, n)


def pgettext(context: str, message: str) -> str:
    """For a word that means different things in different places ("Text" the format, "Text"
    the editor's tool)."""
    return _current.pgettext(context, message)


def N_(message: str) -> str:
    """Marks text for extraction without translating it yet."""
    return message


def NC_(context: str, message: str) -> str:
    """N_() with a context: shown later through pgettext(context, message)."""
    return message


def n_files(count: int) -> str:
    """"1 file", "3 files": the count shown on the wheel, in dialogs and notifications."""
    return ngettext("{n} file", "{n} files", count).format(n=count)


def language() -> str:
    """The catalogue in use ("de", "pt_BR"), or "en" when the text is untranslated."""
    return _language


def available() -> list[str]:
    """Languages with a catalogue, e.g. ["de", "es", "fr", "pt_BR"]."""
    return sorted(p.stem for p in LOCALE_DIR.glob("*.po"))


def candidates(spec: str) -> list[str]:
    """Catalogue names to try for a locale or LANGUAGE value: "de_AT.UTF-8" -> de_AT, de;
    "sr_RS@latin" -> sr_RS@latin, sr@latin, sr_RS, sr. C and POSIX mean English."""
    found: list[str] = []
    for part in spec.split(":"):
        name, _at, modifier = part.partition("@")
        name = name.partition(".")[0]
        if name in ("C", "POSIX"):
            break  # LANGUAGE=C, or "de:C" (German, then plain English)
        if not name:
            continue
        lang = name.partition("_")[0]
        options = [name, lang]
        if modifier:
            options = [f"{name}@{modifier}", f"{lang}@{modifier}", *options]
        found += [o for o in dict.fromkeys(options) if o not in found]
    return found


def requested() -> list[str]:
    if value := os.environ.get("HALA_LANGUAGE"):
        return candidates(value)
    locale = next((v for var in LOCALE_VARS if (v := os.environ.get(var))), "")
    if not candidates(locale) and locale:
        return []  # C or POSIX: GNU gettext ignores LANGUAGE then, and so does Hala
    return candidates(os.environ.get("LANGUAGE") or locale)


@cache
def translator(lang: str) -> gettext.NullTranslations:
    """The catalogue for one language (no fallbacks), or English if there's none. Read once:
    the Dolphin menus look up every language for each of their ~90 files."""
    path = LOCALE_DIR / f"{lang}.po"
    if not path.is_file():
        return gettext.NullTranslations()
    return gettext.GNUTranslations(io.BytesIO(compile_po(path.read_text("utf-8"))))


def set_language(spec: str | None = None) -> str:
    """Switch to the best catalogue for spec (a locale or LANGUAGE value; None: from the
    environment). Returns the language chosen, "en" if none matched."""
    global _current, _language
    wanted = candidates(spec) if spec is not None else requested()
    have = set(available())
    chosen: list[str] = []
    for lang in wanted:
        if lang.partition("_")[0].partition("@")[0] == "en":
            break  # the text is English already: "en_GB:de" mustn't end up German
        if lang in have:
            chosen.append(lang)
    current: gettext.NullTranslations = gettext.NullTranslations()
    for lang in reversed(chosen):  # the first choice on top, falling back to the others
        # A fresh copy each time: translator() is cached, and fallbacks are set on the object.
        t = gettext.GNUTranslations(io.BytesIO(compile_po(
            (LOCALE_DIR / f"{lang}.po").read_text("utf-8"))))
        t.add_fallback(current)
        current = t
    _current, _language = current, chosen[0] if chosen else "en"
    return _language


# --- .po -> .mo -------------------------------------------------------------------------------


def parse_po(text: str) -> list[tuple[str | None, str, str | None, list[str], bool]]:
    """(msgctxt, msgid, msgid_plural, msgstrs, fuzzy) for each entry; obsolete (#~) entries
    are left out."""
    entries: list[tuple[str | None, str, str | None, list[str], bool]] = []
    entry: dict[str, list[str]] = {}
    field = ""
    fuzzy = False

    def flush() -> None:
        nonlocal entry, fuzzy
        if "msgid" in entry:
            strs = [s for _i, s in sorted(_strs(entry))]
            ctxt = "".join(entry["msgctxt"]) if "msgctxt" in entry else None
            plural = "".join(entry["msgid_plural"]) if "msgid_plural" in entry else None
            entries.append((ctxt, "".join(entry["msgid"]), plural, strs, fuzzy))
        entry, fuzzy = {}, False

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#~"):
            continue
        if not line or line.startswith("#"):
            if any(k.startswith("msgstr") for k in entry):
                flush()
            if line.startswith("#,") and "fuzzy" in line[2:].replace(",", " ").split():
                fuzzy = True
            continue
        if line.startswith('"'):
            entry[field].append(ast.literal_eval(line))
            continue
        key, _sp, value = line.partition(" ")
        if key in ("msgctxt", "msgid") and any(k.startswith("msgstr") for k in entry):
            flush()
        field = key
        entry[field] = [ast.literal_eval(value)]
    flush()
    return entries


def _strs(entry: dict[str, list[str]]) -> Iterator[tuple[int, str]]:
    for key, parts in entry.items():
        if key == "msgstr":
            yield 0, "".join(parts)
        elif key.startswith("msgstr["):
            yield int(key[7:-1]), "".join(parts)


def compile_po(text: str) -> bytes:
    """A .mo file's bytes from a .po file's text. Untranslated and fuzzy entries are left out,
    so their English shows, as with msgfmt; so is a plural missing one of its forms (it would
    show as nothing for those numbers)."""
    catalog: dict[bytes, bytes] = {}
    for ctxt, msgid, plural, strs, fuzzy in parse_po(text):
        if (fuzzy and msgid) or not all(strs) or not strs:
            continue
        key = msgid if plural is None else f"{msgid}\0{plural}"
        if ctxt is not None:
            key = f"{ctxt}\x04{key}"
        catalog[key.encode()] = "\0".join(strs).encode()
    keys = sorted(catalog)
    count = len(keys)
    # Header (7 words), the original and translation tables (2 words per entry), then text.
    ids_at = 7 * 4
    strs_at = ids_at + count * 8
    data_at = strs_at + count * 8
    tables: list[int] = []
    blob = io.BytesIO()
    for strings in (keys, [catalog[k] for k in keys]):
        for s in strings:
            tables += [len(s), data_at + blob.tell()]
            blob.write(s + b"\0")
    header = struct.pack("<7I", 0x950412DE, 0, count, ids_at, strs_at, 0, 0)
    return header + struct.pack(f"<{len(tables)}I", *tables) + blob.getvalue()


set_language()
