"""Build dist/hala_VERSION_all.deb: Hala as an installed desktop application.

    .venv/bin/python packaging/build_deb.py            # dist/hala_0.1.0+202610211245_all.deb
    .venv/bin/python packaging/build_deb.py --install  # ...and install it (sudo -A)
    .venv/bin/python packaging/build_deb.py --release  # dist/hala_0.1.0_all.deb

Builds other than --release get the build time after the version, so each one is newer than
the last and apt upgrades to it; older .debs in dist/ are removed.

It installs the hala package for the system Python 3 (PySide6 and GLib come from apt, as in
development), /usr/bin/hala, the application menu entry, the icon and every Dolphin menu
(system-wide, so each account on the machine gets them). The converters are apt
dependencies; LibreOffice and pandoc are recommended rather than required, as they're big.
No maintainer scripts: dpkg's triggers refresh the icon and application caches.
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hala import __version__  # noqa: E402
from hala.servicemenus import APP_ENTRY, ICON, build_menus  # noqa: E402

EXE = "/usr/bin/hala"
MAINTAINER = "baverrr <342259467+baverrr@users.noreply.github.com>"
DEPENDS = [
    "python3 (>= 3.12)", "python3-gi", "gir1.2-glib-2.0 (>= 2.80)",
    "python3-pyside6.qtcore", "python3-pyside6.qtgui", "python3-pyside6.qtwidgets",
    "qt6-svg-plugins", "liblayershellqtinterface6",
    "ffmpeg", "libvips-tools", "poppler-utils", "7zip", "qpdf", "ghostscript",
    "libimage-exiftool-perl", "zbar-tools", "zstd",
]
RECOMMENDS = ["pandoc", "libreoffice-writer", "libreoffice-calc", "libreoffice-impress",
              "kdeconnect", "kdialog", "dolphin"]
DESCRIPTION = """\
Local file converter for KDE Plasma
 Convert images, video, audio, documents, e-books and archives on this machine:
 drag files to the screen edge and pick a format on the wheel, or right-click
 them in Dolphin (Convert to, Hala tools). Tools compress to a size, strip
 metadata, merge and split PDFs, read QR codes, and trim, split, join or
 normalize media. A floating shelf holds files, and pictures can be cropped,
 annotated and redacted. Nothing is uploaded anywhere."""
LAUNCHER = """#!/usr/bin/python3 -I
import sys

from hala.cli import main

sys.exit(main())
"""


def stage(root: Path, version: str = __version__) -> None:
    lib = root / "usr/lib/python3/dist-packages/hala"
    shutil.copytree(ROOT / "hala", lib, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    bin_ = root / "usr/bin/hala"
    bin_.parent.mkdir(parents=True)
    bin_.write_text(LAUNCHER)
    bin_.chmod(0o755)

    apps = root / "usr/share/applications"
    apps.mkdir(parents=True)
    (apps / "hala.desktop").write_text(APP_ENTRY.format(exe=EXE))
    icons = root / "usr/share/icons/hicolor/scalable/apps"
    icons.mkdir(parents=True)
    shutil.copyfile(ICON, icons / "hala.svg")
    menus = root / "usr/share/kio/servicemenus"
    menus.mkdir(parents=True)
    for name, text in build_menus(EXE).items():
        (menus / name).write_text(text)
        (menus / name).chmod(0o755)

    doc = root / "usr/share/doc/hala"
    doc.mkdir(parents=True)
    shutil.copyfile(ROOT / "README.md", doc / "README.md")
    (doc / "copyright").write_text(
        "Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/\n"
        "Upstream-Name: hala\n\nFiles: *\nCopyright: 2026 baverrr\n"
        "License: proprietary\n All rights reserved.\n")
    with gzip.open(doc / "changelog.gz", "wt") as log:
        log.write(f"hala ({version}) unstable; urgency=medium\n\n"
                  "  * Packaged as a desktop application.\n\n"
                  f" -- {MAINTAINER}  Wed, 21 Oct 2026 12:00:00 +0000\n")

    root.chmod(0o755)
    for path in root.rglob("*"):  # predictable modes, whatever the checkout's umask was
        if path.is_dir():
            path.chmod(0o755)
        elif not path.stat().st_mode & 0o100:
            path.chmod(0o644)

    size_kb = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) // 1024 + 1
    control = root / "DEBIAN/control"
    control.parent.mkdir(mode=0o755)
    control.write_text(
        f"Package: hala\nVersion: {version}\nArchitecture: all\n"
        f"Maintainer: {MAINTAINER}\nInstalled-Size: {size_kb}\nSection: utils\n"
        f"Priority: optional\nDepends: {', '.join(DEPENDS)}\n"
        f"Recommends: {', '.join(RECOMMENDS)}\nDescription: {DESCRIPTION}\n")


def build(out_dir: Path, version: str = __version__) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("hala_*_all.deb"):
        old.unlink()
    deb = out_dir / f"hala_{version}_all.deb"
    with tempfile.TemporaryDirectory(prefix="hala-deb-") as tmp:
        root = Path(tmp) / "root"
        root.mkdir()
        stage(root, version)
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(root),
                        str(deb)], check=True, capture_output=True)
    return deb


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Hala's .deb.")
    parser.add_argument("--release", action="store_true",
                        help="plain version number, without the build time")
    parser.add_argument("--install", action="store_true",
                        help="then install it with sudo -A apt-get (asks for the password)")
    args = parser.parse_args()
    version = __version__ if args.release else f"{__version__}+{time.strftime('%Y%m%d%H%M')}"
    deb = build(ROOT / "dist", version)
    print(deb)
    if args.install:
        return subprocess.run(["sudo", "-A", "-p", "Password", "apt-get", "install", "-y",
                               str(deb)]).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
