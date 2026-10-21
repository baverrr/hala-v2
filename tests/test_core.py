"""Format detection, the routing table, the job queue and the CLI."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path

import pytest

from hala import formats as f
from hala.cli import main
from hala.jobs import JobQueue, Status
from hala.registry import common_targets, route_for, targets_for
from hala.runner import Cancelled, Context, ConversionError


@pytest.mark.parametrize(("name", "ext", "stem"), [
    ("Photo.JPEG", "jpg", "Photo"),
    ("backup.tar.gz", "tar.gz", "backup"),
    ("old.TGZ", "tar.gz", "old"),
    ("song.aif", "aiff", "song"),
    ("README", "", "README"),
    ("my.holiday.mov", "mov", "my.holiday"),
])
def test_ext_and_stem(name: str, ext: str, stem: str) -> None:
    assert f.ext_of(Path(name)) == ext
    assert f.stem_of(Path(name)) == stem


def test_targets_exclude_self_and_dedupe() -> None:
    outs = targets_for("png")
    assert "png" not in outs
    assert len(outs) == len(set(outs))
    assert outs[:2] == ["jpg", "heic"]  # image formats first, archives last
    assert outs[-3:] == ["zip", "tar.gz", "7z"]


def test_docx_prefers_libreoffice_but_offers_pandoc_extras() -> None:
    route = route_for("docx", "pdf")
    assert route is not None and route.label == "LibreOffice"
    assert {"md", "epub"} <= set(targets_for("docx"))


def test_folder_only_for_archives() -> None:
    assert "folder" in targets_for("zip")
    assert "folder" not in targets_for("png")
    assert targets_for(f.FOLDER) == ["zip", "tar.gz", "7z"]


def test_common_targets_for_mixed_batch() -> None:
    assert common_targets(["png", "jpg"])[0] == "heic"
    assert common_targets(["png", "mp3"]) == ["zip", "tar.gz", "7z"]
    assert common_targets([]) == []


def test_job_queue_batch(workdir: dict[str, Path]) -> None:
    events: list[tuple[int, Status]] = []
    queue = JobQueue(workers=2, listener=lambda j: events.append((j.id, j.status)))
    good, good_future = queue.submit(workdir["png"], "webp")
    bad, bad_future = queue.submit(workdir["wav"], "docx")
    assert good_future.result().status is Status.DONE
    assert bad_future.result().status is Status.FAILED and bad.error
    queue.shutdown()
    assert good.output is not None and good.output.exists()
    assert (good.id, Status.RUNNING) in events and (good.id, Status.DONE) in events


def test_cli_targets_and_convert(workdir: dict[str, Path],
                                 capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["targets", "--json", str(workdir["wav"])]) == 0
    assert "mp3" in json.loads(capsys.readouterr().out)
    assert main(["convert", "--json", "-t", "mp3", str(workdir["wav"]), str(workdir["png"])]) == 1
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    by_src = {Path(x["src"]).name: x for x in lines}
    assert by_src["tone.wav"]["status"] == "done"
    assert by_src["pic.png"]["status"] == "failed"


@pytest.mark.parametrize(("typed", "ext"), [(".JPEG", "jpg"), ("TGZ", "tar.gz"), ("mp3", "mp3")])
def test_normalize_ext(typed: str, ext: str) -> None:
    assert f.normalize_ext(typed) == ext


def test_split_name_is_case_insensitive() -> None:
    assert f.split_name("Old.TAR.GZ") == ("Old", ".TAR.GZ")
    assert f.split_name(".bashrc") == (".bashrc", "")


def test_folder_stem_keeps_dots(tmp_path: Path) -> None:
    folder = tmp_path / "project-v1.2"
    folder.mkdir()
    assert f.stem_of(folder) == "project-v1.2"


def test_status_is_terminal() -> None:
    assert not Status.QUEUED.is_terminal and not Status.RUNNING.is_terminal
    assert all(s.is_terminal for s in (Status.DONE, Status.FAILED, Status.CANCELLED))


# --- runner ---------------------------------------------------------------------------------

def test_run_reports_stderr_tail(tmp_path: Path) -> None:
    with pytest.raises(ConversionError, match=r"sh failed:\nboom"):
        Context(tmp_path).run(["sh", "-c", "echo boom >&2; exit 3"])


def test_run_missing_tool(tmp_path: Path) -> None:
    with pytest.raises(ConversionError, match="not installed"):
        Context(tmp_path).run(["no-such-tool-hala"])


def _group_alive(pgid: int) -> bool:
    """Whether anything in the group is left, allowing a moment for init to reap zombies."""
    for _ in range(20):
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return False
        time.sleep(0.1)
    return True


def test_cancel_escalates_to_sigkill(tmp_path: Path) -> None:
    cancel = threading.Event()
    pids: list[int] = []
    threading.Timer(0.5, cancel.set).start()
    started = time.monotonic()
    with pytest.raises(Cancelled):
        Context(tmp_path, cancel_event=cancel).run(
            ["bash", "-c", "trap '' TERM; echo $$; sleep 60 & wait; sleep 60"],
            stdout_line=lambda line: pids.append(int(line)))
    assert 3 <= time.monotonic() - started < 6  # SIGTERM ignored, SIGKILL after 3 s
    assert pids and not _group_alive(pids[0])


def test_timeout(tmp_path: Path) -> None:
    with pytest.raises(ConversionError, match="stopped responding"):
        Context(tmp_path).run(["sleep", "30"], timeout=0.5)


def test_failing_callback_stops_the_tool(tmp_path: Path) -> None:
    pids: list[int] = []

    def boom(line: str) -> None:
        pids.append(int(line))
        raise ValueError("listener bug")

    with pytest.raises(ValueError):
        Context(tmp_path).run(["sh", "-c", "echo $$; sleep 30"], stdout_line=boom)
    assert not _group_alive(pids[0])


# --- queue and CLI --------------------------------------------------------------------------

def test_cancelled_queued_job_never_runs(workdir: dict[str, Path]) -> None:
    events: list[tuple[int, Status]] = []
    queue = JobQueue(workers=1, listener=lambda j: events.append((j.id, j.status)))
    _first, first_future = queue.submit(workdir["png"], "avif")
    second, second_future = queue.submit(workdir["png"], "webp")
    second.cancel()
    first_future.result()
    assert second_future.result().status is Status.CANCELLED
    assert (second.id, Status.RUNNING) not in events
    queue.shutdown()


def test_doctor_reports_missing_tool(monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda t: None if t == "7z" else real(t))
    assert main(["doctor"]) == 1
    assert "7z" in capsys.readouterr().out


def test_cli_rejects_zero_jobs() -> None:
    with pytest.raises(SystemExit):
        main(["convert", "-j", "0", "-t", "mp3", "x.wav"])


def test_cli_missing_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["convert", "-t", "mp3", str(tmp_path / "nope.wav")]) == 1
    assert "does not exist" in capsys.readouterr().err

