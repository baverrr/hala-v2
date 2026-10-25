"""Translation upkeep: pull the strings out of the code, and keep the .po files in step.

    python3 tools/i18n.py extract      # hala/locale/hala.pot from the code (needs xgettext)
    python3 tools/i18n.py update       # extract, then merge the template into every .po
    python3 tools/i18n.py new pt_BR    # start hala/locale/pt_BR.po from the template
    python3 tools/i18n.py check        # every .po: valid, and its {placeholders} intact
    python3 tools/i18n.py stats        # how much of each language is translated

xgettext, msgmerge, msginit and msgfmt come from the gettext package (apt install gettext).
Hala itself doesn't need them: it reads the .po files directly (hala/i18n.py).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hala import __version__, i18n  # noqa: E402

LOCALE = ROOT / "hala" / "locale"
TEMPLATE = LOCALE / "hala.pot"
KEYWORDS = ["_", "N_", "ngettext:1,2", "pgettext:1c,2", "NC_:1c,2"]
HEADER = ("# Hala's text, to be translated: made by tools/i18n.py extract, not by hand.\n"
          "# Start a language with: python3 tools/i18n.py new <lang>\n#\n")
FIELD = re.compile(r"(?<!\{)\{([^{}!:]*)(?:![rsa])?(?::[^{}]*)?\}")


def _tool(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        sys.exit(f"{name} isn't installed (apt install gettext)")
    return path


def sources() -> list[Path]:
    """The Python files git knows about under hala/, in a stable order."""
    out = subprocess.run(["git", "ls-files", "hala/*.py", "hala/**/*.py"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return sorted({ROOT / line for line in out.splitlines() if line})


def extract(template: Path = TEMPLATE) -> Path:
    files = [str(p.relative_to(ROOT)) for p in sources()]
    template.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([_tool("xgettext"), "--language=Python", "--from-code=UTF-8",
                    *(f"--keyword={k}" for k in KEYWORDS), "--add-comments=Translators:",
                    "--sort-by-file", "--package-name=Hala", f"--package-version={__version__}",
                    "--msgid-bugs-address=https://github.com/baverrr/hala-v2/issues",
                    "-o", str(template), *files], cwd=ROOT, check=True)
    # xgettext dates the template; keep it out so an unchanged extract is an unchanged file.
    # Its placeholder header and team address go too (the public export rejects addresses).
    text = re.sub(r'"POT-Creation-Date: [^"]*\\n"\n', "", template.read_text())
    text = re.sub(r"\A(#.*\n)*?(?=#, fuzzy)", HEADER, text)
    text = re.sub(r"(Language-Team: LANGUAGE) <[^>]*>", r"\1", text)
    template.write_text(text)
    return template


def update() -> None:
    extract()
    for po in sorted(LOCALE.glob("*.po")):
        subprocess.run([_tool("msgmerge"), "--quiet", "--update", "--backup=none",
                        "--sort-by-file", str(po), str(TEMPLATE)], check=True)
        print(f"updated {po.relative_to(ROOT)}")


def new(lang: str) -> None:
    po = LOCALE / f"{lang}.po"
    if po.exists():
        sys.exit(f"{po.relative_to(ROOT)} already exists; use update")
    if not TEMPLATE.exists():
        extract()
    subprocess.run([_tool("msginit"), "--no-translator", f"--locale={lang}.UTF-8",
                    f"--input={TEMPLATE}", f"--output-file={po}"], check=True)
    # msginit fills in a mailing-list address for the team; the public export rejects those.
    po.write_text(re.sub(r'("Language-Team: [^"<]*?) *<[^>]*>', r"\1", po.read_text()))


def fields(text: str) -> set[str]:
    """The {placeholders} a translation has to keep ("{{" is a literal brace)."""
    return set(FIELD.findall(text.replace("{{", "").replace("}}", "")))


def problems(po: Path) -> list[str]:
    """A translation that drops or invents a {placeholder} would fail (or mislead) at run
    time; msgfmt --check doesn't know Python's brace syntax. A "%" the English doesn't have
    breaks argparse, which %-formats the CLI's help text."""
    found = []
    for _ctxt, msgid, plural, strs, fuzzy in i18n.parse_po(po.read_text("utf-8")):
        if not msgid or fuzzy:
            continue
        wanted = fields(msgid) | (fields(plural) if plural else set())
        for s in strs:
            if s and fields(s) != wanted:
                found.append(f"{po.name}: {msgid!r}: {s!r} has {sorted(fields(s))}, "
                             f"not {sorted(wanted)}")
            if "%" in s and "%" not in msgid:
                found.append(f"{po.name}: {msgid!r}: {s!r} has a % (write 'percent')")
    return found


def check() -> int:
    bad = 0
    for po in sorted(LOCALE.glob("*.po")):
        if shutil.which("msgfmt"):
            result = subprocess.run(["msgfmt", "--check", "-o", "/dev/null", str(po)])
            bad += result.returncode != 0
        for line in problems(po):
            print(line)
            bad += 1
    return 1 if bad else 0


def stats() -> None:
    for po in sorted(LOCALE.glob("*.po")):
        entries = [e for e in i18n.parse_po(po.read_text("utf-8")) if e[1]]
        done = sum(1 for e in entries if any(e[3]) and not e[4])
        fuzzy = sum(1 for e in entries if e[4])
        print(f"{po.stem:<8} {done:>4}/{len(entries)} translated"
              + (f", {fuzzy} fuzzy" if fuzzy else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("extract")
    sub.add_parser("update")
    sub.add_parser("new").add_argument("lang")
    sub.add_parser("check")
    sub.add_parser("stats")
    args = parser.parse_args()
    if args.command == "extract":
        print(extract().relative_to(ROOT))
    elif args.command == "update":
        update()
    elif args.command == "new":
        new(args.lang)
    elif args.command == "check":
        return check()
    else:
        stats()
    return 0


if __name__ == "__main__":
    sys.exit(main())
