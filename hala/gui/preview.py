"""What the middle of the wheel shows besides the name: the files' total size."""

from __future__ import annotations

from pathlib import Path


def total_size(paths: list[Path]) -> int | None:
    """Bytes in all the files, or None when a folder is involved (not worth walking)."""
    total = 0
    for p in paths:
        if not p.is_file():
            return None
        try:
            total += p.stat().st_size
        except OSError:
            return None
    return total


def human_size(n: int) -> str:
    """Decimal units, one decimal place, as file managers show them: "2.2 MB"."""
    value = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            return f"{n} bytes" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1000
    raise AssertionError("unreachable")
