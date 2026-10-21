"""Archive limits after the 0.4.0 review: disguised streams, listings that understate their
contents, free space counted once, entry limits on every tar path, and packaging only what
git tracks."""

from __future__ import annotations

import bz2
import gzip
import importlib.util
import io
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest

from hala import limits
from hala.convert import convert_file
from hala.engines import archives
from hala.runner import ConversionError

SPEC = importlib.util.spec_from_file_location(
    "build_deb", Path(__file__).parent.parent / "packaging" / "build_deb.py")
assert SPEC and SPEC.loader
build_deb = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_deb)


@pytest.fixture
def small_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "BOMB_FLOOR", 1 << 20)


def _forged_gzip(size: int, claimed: int) -> bytes:
    data = bytearray(gzip.compress(bytes(size)))
    data[-4:] = claimed.to_bytes(4, "little")  # ISIZE: the size gzip says it unpacks to
    return bytes(data)


@pytest.mark.usefixtures("small_limits")
@pytest.mark.parametrize("name,data", [
    ("bomb.zip", bz2.compress(bytes(8 << 20))),  # 7z lists no size at all
    ("bomb.7z", bz2.compress(bytes(8 << 20))),
    ("bomb.rar", bz2.compress(bytes(8 << 20))),
    ("forged.zip", _forged_gzip(8 << 20, 10)),  # 7z lists 10 bytes
])
def test_disguised_streams_are_refused(tmp_path: Path, name: str, data: bytes) -> None:
    src = tmp_path / name
    src.write_bytes(data)
    with pytest.raises(ConversionError, match=r"not a \.(zip|7z|rar) archive"):
        convert_file(src, "folder")
    assert [p.name for p in tmp_path.iterdir()] == [name]  # nothing unpacked, no scratch


def test_genuine_zip_and_7z_still_unpack(tmp_path: Path) -> None:
    src = tmp_path / "ok.zip"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("dir/a.txt", "hello " * 1000)
        z.writestr("dir/empty.txt", "")
    out = convert_file(src, "folder")
    assert (out / "a.txt").read_text().startswith("hello") and (out / "empty.txt").exists()
    seven = tmp_path / "ok.7z"
    subprocess.run(["7z", "a", "-bso0", "-bsp0", seven, out], check=True)
    assert (convert_file(seven, "folder") / "a.txt").exists()


def test_a_zip_with_forged_sizes_unpacks_no_more_than_it_lists(tmp_path: Path) -> None:
    src = tmp_path / "forged.zip"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("zeros.bin", bytes(1 << 20))
    data = bytearray(src.read_bytes())
    real = (1 << 20).to_bytes(4, "little")
    data = data.replace(real, (1000).to_bytes(4, "little"))  # local header + central directory
    src.write_bytes(bytes(data))
    try:
        out = convert_file(src, "folder")
    except ConversionError:
        return  # refusing is fine too
    assert out.stat().st_size <= 1000


@pytest.mark.usefixtures("small_limits")
def test_contents_past_the_listing_are_stopped(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """The RLIMIT_FSIZE backstop: if a listing understates, the unpacker is stopped at the cap."""
    monkeypatch.setattr(archives, "_listed_7z", lambda _ctx, _src, _ext: ("zip", 1, 1))
    src = tmp_path / "big.zip"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("zeros.bin", bytes(8 << 20))
    with pytest.raises(ConversionError, match="more than its listing said"):
        convert_file(src, "folder")
    assert [p.name for p in tmp_path.iterdir()] == ["big.zip"]


def test_an_archive_that_doesnt_fit_is_refused(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "ok.zip"
    with zipfile.ZipFile(src, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.txt", "x" * 100_000)  # a few hundred bytes packed
    monkeypatch.setattr(limits, "MIN_FREE", 0)
    # Enough for the up-front check (twice the archive's size), not for its contents.
    monkeypatch.setattr(limits, "free_space", lambda _folder: 2000)
    with pytest.raises(ConversionError, match=r"but only 2\.0 KB is free"):
        convert_file(src, "folder")


@pytest.mark.parametrize("name", ["big.tar", "big.gz"])
def test_free_space_is_read_once_not_counted_twice(tmp_path: Path, name: str,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """A running total is compared with the space there was at the start: 6 MB in 10 MB of
    free space fits, although 6 MB unpacked plus 6 MB already written wouldn't."""
    size = 6 << 20
    src = tmp_path / name
    payload = bytes(range(256)) * (size // 256)  # barely compresses: no bomb ratio
    if name == "big.gz":
        src.write_bytes(gzip.compress(payload, compresslevel=1))
    else:
        with tarfile.open(src, "w") as tar:
            for i in range(3):
                info = tarfile.TarInfo(f"part{i}.bin")
                info.size = size // 3
                tar.addfile(info, io.BytesIO(payload[:size // 3]))
    monkeypatch.setattr(limits, "MIN_FREE", 1 << 20)
    free = {"left": 20 << 20}

    def shrinking(_folder: Path) -> int:  # the disk fills up as the archive unpacks
        free["left"] -= 3 << 20
        return free["left"]

    monkeypatch.setattr(limits, "free_space", shrinking)
    out = convert_file(src, "folder")
    assert out.exists()


@pytest.mark.parametrize("ext", ["tar", "tar.gz", "tar.zst"])
def test_too_many_entries_on_every_tar_path(tmp_path: Path, ext: str,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_ENTRIES", 5)
    plain = tmp_path / "many.tar"
    with tarfile.open(plain, "w") as tar:
        for i in range(10):
            info = tarfile.TarInfo(f"f{i}.txt")
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    src = tmp_path / f"many.{ext}"
    if ext == "tar.gz":
        src.write_bytes(gzip.compress(plain.read_bytes()))
    elif ext == "tar.zst":
        subprocess.run(["zstd", "-q", plain, "-o", src], check=True)
    with pytest.raises(ConversionError, match="more than 5"):
        convert_file(src, "folder")


def test_sparse_message_says_the_archive_wasnt_made(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "SPARSE_FLOOR", 1 << 20)
    disk = tmp_path / "disk.img"
    with open(disk, "wb") as f:
        f.truncate(8 << 20)
    with pytest.raises(ConversionError, match=r"the archive wasn't made \(move that file out"):
        limits.check_not_sparse(disk)


def test_unpack_cap() -> None:
    assert limits.unpack_cap(1, None) == limits.BOMB_FLOOR
    assert limits.unpack_cap(1 << 30, None) == limits.BOMB_RATIO << 30
    assert limits.unpack_cap(1, limits.MIN_FREE + 5) == 5
    assert limits.unpack_cap(1, 0) == 0


def test_the_deb_refuses_files_git_doesnt_track(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    git("init", "-q")
    (tmp_path / ".gitignore").write_text("*.orig\n__pycache__/\n")
    pkg = tmp_path / "hala"
    (pkg / "__pycache__").mkdir(parents=True)
    (pkg / "cli.py").write_text("")
    git("add", ".")
    (pkg / "__pycache__" / "cli.cpython-314.pyc").write_bytes(b"")
    (pkg / "cli.py").write_text("# changed, uncommitted: fine\n")
    assert build_deb.stray_files(tmp_path) == []
    (pkg / "notes.txt").write_text("scratch")
    (pkg / "cli.py.orig").write_text("old")
    assert sorted(build_deb.stray_files(tmp_path)) == ["hala/cli.py.orig", "hala/notes.txt"]
    git("add", "hala/notes.txt")  # staged new files are packaged
    assert build_deb.stray_files(tmp_path) == ["hala/cli.py.orig"]
