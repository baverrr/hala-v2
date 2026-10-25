"""Translations: the catalogues load, keep their placeholders, cover the code's strings, and
reach the tools, the CLI and the Dolphin menus."""

from __future__ import annotations

import gettext
import importlib.util
import io
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hala import formats as f
from hala import i18n, servicemenus
from hala.i18n import _
from hala.runner import ConversionError
from hala.tools import tool

ROOT = Path(__file__).resolve().parent.parent
LANGUAGES = i18n.available()


def _upkeep():
    spec = importlib.util.spec_from_file_location("i18n_upkeep", ROOT / "tools" / "i18n.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def german():
    assert i18n.set_language("de") == "de"
    yield
    i18n.set_language("C")


PO = r'''msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\n"
"Plural-Forms: nplurals=3; "
"plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);\n"

#: x.py:1
msgid "Compress"
msgstr "Komprimuj"

#, fuzzy, python-brace-format
msgid "Fuzzy one"
msgstr "Rozmyty"

msgctxt "button"
msgid "Copy"
msgstr "Kopiuj"

msgid "{n} file"
msgid_plural "{n} files"
msgstr[0] "{n} plik"
msgstr[1] "{n} pliki"
msgstr[2] "{n} plików"

msgid "multi"
"line \"quoted\"\n"
msgstr "wiele "
"linii \"cytat\"\n"

msgid "untranslated"
msgstr ""

msgid "{n} half"
msgid_plural "{n} halves"
msgstr[0] "{n} pół"
msgstr[1] ""
msgstr[2] ""

#~ msgid "old"
#~ msgstr "stare"
'''


def _check(t: gettext.NullTranslations) -> None:
    assert t.gettext("Compress") == "Komprimuj"
    assert t.gettext("Fuzzy one") == "Fuzzy one"  # fuzzy entries aren't used, as with msgfmt
    assert t.pgettext("button", "Copy") == "Kopiuj"
    assert t.gettext("Copy") == "Copy"
    assert [t.ngettext("{n} file", "{n} files", n) for n in (1, 3, 5, 22)] == [
        "{n} plik", "{n} pliki", "{n} plików", "{n} pliki"]
    assert t.gettext('multiline "quoted"\n') == 'wiele linii "cytat"\n'
    assert t.gettext("untranslated") == "untranslated"
    assert t.gettext("old") == "old"


def test_po_compiles_like_msgfmt(tmp_path: Path) -> None:
    ours = gettext.GNUTranslations(io.BytesIO(i18n.compile_po(PO)))
    _check(ours)
    # A plural with forms missing stays English rather than showing nothing (msgfmt keeps it).
    assert ours.ngettext("{n} half", "{n} halves", 5) == "{n} halves"
    if shutil.which("msgfmt"):
        (tmp_path / "t.po").write_text(PO)
        subprocess.run(["msgfmt", "-o", tmp_path / "t.mo", tmp_path / "t.po"], check=True)
        with open(tmp_path / "t.mo", "rb") as mo:
            _check(gettext.GNUTranslations(mo))


@pytest.mark.parametrize(("spec", "wanted"), [
    ("de_AT.UTF-8", ["de_AT", "de"]),
    ("sr_RS@latin", ["sr_RS@latin", "sr@latin", "sr_RS", "sr"]),
    ("pt_BR:pt:es", ["pt_BR", "pt", "es"]),
    ("de:C:fr", ["de"]),
    ("de::fr", ["de", "fr"]),
    ("C", []), ("POSIX", []), ("C.UTF-8", []), ("", []),
])
def test_language_candidates(spec: str, wanted: list[str]) -> None:
    assert i18n.candidates(spec) == wanted


def test_language_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        assert i18n.set_language("de_AT.UTF-8") == "de"
        assert i18n.set_language("xx:fr") == "fr"
        assert i18n.set_language("en_GB:de") == "en"  # English first: stays English
        assert _("Compress") == "Compress"
        for var in i18n.ENV_VARS:
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("LANG", "es_ES.UTF-8")
        assert i18n.set_language() == "es"
        monkeypatch.setenv("LANGUAGE", "fr:de")
        assert i18n.set_language() == "fr"
        monkeypatch.setenv("LC_ALL", "C")  # as GNU gettext: LANGUAGE doesn't count under C
        assert i18n.set_language() == "en"
        monkeypatch.setenv("HALA_LANGUAGE", "pt_BR")  # Hala's own setting wins
        assert i18n.set_language() == "pt_BR"
    finally:
        i18n.set_language("C")
    assert i18n.language() == "en"


@pytest.mark.parametrize("lang", LANGUAGES)
def test_catalogue_is_complete_and_keeps_placeholders(lang: str) -> None:
    po = i18n.LOCALE_DIR / f"{lang}.po"
    assert _upkeep().problems(po) == []
    entries = [e for e in i18n.parse_po(po.read_text("utf-8")) if e[1]]
    assert all(any(strs) and not fuzzy for _c, _i, _p, strs, fuzzy in entries)


@pytest.mark.parametrize("lang", LANGUAGES)
def test_catalogue_passes_msgfmt(lang: str) -> None:
    if not shutil.which("msgfmt"):
        pytest.skip("msgfmt isn't installed")
    subprocess.run(["msgfmt", "--check", "-o", "/dev/null", i18n.LOCALE_DIR / f"{lang}.po"],
                   check=True)


def test_template_matches_the_code(tmp_path: Path) -> None:
    """Every marked string is in hala.pot, so translators see it (tools/i18n.py update)."""
    if not shutil.which("xgettext") or not (ROOT / ".git").exists():
        pytest.skip("needs xgettext and a git checkout")
    fresh = _upkeep().extract(tmp_path / "hala.pot")

    def ids(path: Path) -> set[tuple[str | None, str, str | None]]:
        return {(c, m, p) for c, m, p, _s, _f in i18n.parse_po(path.read_text()) if m}

    assert ids(fresh) == ids(i18n.LOCALE_DIR / "hala.pot")


def test_catalogues_match_the_template() -> None:
    template = {m for _c, m, _p, _s, _f in
                i18n.parse_po((i18n.LOCALE_DIR / "hala.pot").read_text()) if m}
    for lang in LANGUAGES:
        have = {m for _c, m, _p, _s, _f in
                i18n.parse_po((i18n.LOCALE_DIR / f"{lang}.po").read_text("utf-8")) if m}
        assert have == template, f"{lang}.po is out of date (python3 tools/i18n.py update)"


def test_translations_reach_tools_and_errors(german: None, tmp_path: Path) -> None:
    compress = tool("compress")
    assert _(compress.label) == "Komprimieren"
    assert compress.suffix({}) == "komprimiert"
    assert f.label("folder") == "Ordner (entpacken)"
    assert f.label("jpg") == "JPG"
    with pytest.raises(ConversionError, match="existiert nicht"):
        from hala.convert import convert_file

        convert_file(tmp_path / "missing.png", "jpg")
    # The CLI's names stay English: they're commands, not text.
    assert compress.name == "compress" and tool("metadata").params[0].choices[0] == "everything"


def test_cli_speaks_the_language() -> None:
    env = {"HALA_LANGUAGE": "fr", "PATH": "/usr/bin:/bin"}
    hala = [sys.executable, "-m", "hala.cli"]  # from this checkout, installed or not
    listing = subprocess.run([*hala, "tool"], capture_output=True, text=True, env=env,
                             cwd=ROOT, check=True).stdout
    assert "compress   faire tenir chaque fichier dans une taille" in listing
    help_text = subprocess.run([*hala, "--help"], capture_output=True, text=True, env=env,
                               cwd=ROOT, check=True).stdout
    assert "Convertir des fichiers sur votre ordinateur." in help_text


def test_desktop_files_carry_every_language() -> None:
    menus = servicemenus.build_menus("/usr/bin/hala")
    text = menus["hala-tool-compress.desktop"]
    assert "Name=Compress…\n" in text
    assert "Name[de]=Komprimieren…\n" in text
    assert "X-KDE-Submenu[fr]=Outils Hala\n" in text
    jpg = menus["hala-jpg.desktop"]
    assert "X-KDE-Submenu[es]=Convertir a\n" in jpg
    # Format names that read the same in every language get no translated copies.
    assert "Name[de]=PNG" not in jpg
    app = servicemenus.localize(servicemenus.APP_ENTRY.format(exe="/usr/bin/hala"))
    assert "GenericName=File Converter\n" in app  # uninstall() recognises old entries by it
    assert "GenericName[pt_BR]=Conversor de arquivos\n" in app
    assert "Name[de]=Hala" not in app
    keywords = next(line for line in app.splitlines() if line.startswith("Keywords[de]="))
    assert keywords.endswith(";")


def test_menus_are_the_same_whoever_builds_them(german: None) -> None:
    """Built on a German machine (or a .deb built there), the menus are still English with
    every language added, not German for everyone."""
    menus = servicemenus.build_menus("/usr/bin/hala")
    assert "Name=Folder (extract)\n" in menus["hala-7z.desktop"]
    assert "Name[fr]=Dossier (extraire)\n" in menus["hala-7z.desktop"]
    assert "Name=Compress…\n" in menus["hala-tool-compress.desktop"]
    i18n.set_language("C")
    assert servicemenus.build_menus("/usr/bin/hala") == menus


def test_one_word_two_meanings(german: None) -> None:
    from hala.tools.edit_media import SPLIT

    assert f.label("txt") == "Text"
    assert _(SPLIT.label) == "Teilen" and _(SPLIT.done) == "In Teile geteilt"
    i18n.set_language("fr")
    assert f.label("txt") == "Texte" and i18n.pgettext("line width", "Line") == "Trait"
    assert _("Line") == "Ligne"


def test_localize_leaves_other_lines_alone() -> None:
    text = "[Desktop Entry]\nName=Hala…\nExec=hala wheel %F\nIcon=hala\n"
    assert servicemenus.localize(text, []) == text
    out = servicemenus.localize(text, ["de"])
    assert out.splitlines() == ["[Desktop Entry]", "Name=Hala…", "Name[de]=Hala …",
                                "Exec=hala wheel %F", "Icon=hala"]
