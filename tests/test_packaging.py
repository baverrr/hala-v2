"""The .deb's layout (packaging/build_deb.py), checked without dpkg."""

from __future__ import annotations

import configparser
import importlib.util
from pathlib import Path

from hala import __version__

SPEC = importlib.util.spec_from_file_location(
    "build_deb", Path(__file__).parent.parent / "packaging" / "build_deb.py")
assert SPEC and SPEC.loader
build_deb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_deb)


def test_stage_lays_out_an_installable_app(tmp_path: Path) -> None:
    build_deb.stage(tmp_path)
    launcher = tmp_path / "usr/bin/hala"
    assert launcher.stat().st_mode & 0o777 == 0o755
    assert launcher.read_text().startswith("#!/usr/bin/python3 -I\n")
    assert (tmp_path / "usr/lib/python3/dist-packages/hala/cli.py").exists()
    assert not list(tmp_path.rglob("__pycache__"))

    entry = configparser.ConfigParser(interpolation=None)
    entry.read(tmp_path / "usr/share/applications/hala.desktop")
    assert entry["Desktop Entry"]["Exec"] == '"/usr/bin/hala" app %F'
    assert "NoDisplay" not in entry["Desktop Entry"]

    menus = list((tmp_path / "usr/share/kio/servicemenus").glob("hala-*.desktop"))
    assert len(menus) > 50
    assert all('Exec="/usr/bin/hala" ' in m.read_text() for m in menus)

    control = (tmp_path / "DEBIAN/control").read_text()
    assert f"Version: {__version__}\n" in control
    assert "@gmail" not in control
