"""Running external tools: low priority, cancellable, with readable errors."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import _

# Keep conversions from making the desktop laggy: nice 10 for CPU, and the lowest best-effort
# I/O priority (ionice class 2, level 7).
NICE = ["nice", "-n", "10", "ionice", "-c", "2", "-n", "7"]
# ffmpeg follows references inside media files (playlists, concat lists); let it open only
# local files, never URLs a downloaded file might point it at.
LOCAL_ONLY = ["-protocol_whitelist", "file,pipe"]


def local_inputs(args: list[str]) -> list[str]:
    """args with every ffmpeg/ffprobe input limited to LOCAL_ONLY; other commands as given."""
    name = Path(args[0]).name if args else ""
    if name == "ffprobe":
        return [args[0], *LOCAL_ONLY, *args[1:]]
    if name != "ffmpeg":
        return args
    limited: list[str] = []
    for arg in args:
        if arg == "-i":
            limited += LOCAL_ONLY
        limited.append(arg)
    return limited


class ConversionError(RuntimeError):
    pass


class Cancelled(ConversionError):
    pass


def require(tool: str) -> str:
    path = shutil.which(tool)
    if path is None:
        raise ConversionError(_("'{tool}' is not installed (run `hala doctor`)").format(tool=tool))
    return path


@dataclass(frozen=True)
class Context:
    """What an engine gets: a private scratch dir next to the output, plus run/progress hooks."""

    workdir: Path
    on_progress: Callable[[float], None] = lambda _f: None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # Things the user should know about a result that still worked (e.g. pictures a document
    # pointed at but that were left out); shown with the result.
    notes: list[str] = field(default_factory=list)

    def progress(self, fraction: float) -> None:
        self.on_progress(max(0.0, min(1.0, fraction)))

    def note(self, text: str) -> None:
        self.notes.append(text)

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled(_("cancelled"))

    def run(self, cmd: Sequence[str | os.PathLike[str]], *, cwd: Path | None = None,
            stdout_line: Callable[[str], None] | None = None,
            env: dict[str, str] | None = None, timeout: float | None = None,
            stdin_data: bytes | None = None) -> str:
        """Run cmd at low priority and return its stdout.

        Raises Cancelled if the cancel event fires, and ConversionError (with the last 8 lines
        of stderr) if the tool fails or runs longer than timeout seconds.
        """
        return self.run_full(cmd, cwd=cwd, stdout_line=stdout_line, env=env,
                             timeout=timeout, stdin_data=stdin_data)[0]

    def run_full(self, cmd: Sequence[str | os.PathLike[str]], *, cwd: Path | None = None,
                 stdout_line: Callable[[str], None] | None = None,
                 env: dict[str, str] | None = None, timeout: float | None = None,
                 ok_codes: Collection[int] = (0,),
                 stdin_data: bytes | None = None) -> tuple[str, str, int]:
        """Like run(), but returns (stdout, stderr, exit code) and accepts any exit code in
        ok_codes (zbarimg exits 4 for "nothing found"; ffmpeg's loudnorm reports on stderr).
        stdin_data is written to the tool's input (a password stays off the command line,
        where any user could read it with ps)."""
        self.check_cancelled()
        args = local_inputs([str(a) for a in cmd])
        if not args:
            raise ConversionError(_("no command to run"))
        for tool in (args[0], NICE[0], NICE[3]):
            require(tool)
        proc = subprocess.Popen(
            # Own process group, so killpg() reaches the tool and anything it spawns.
            [*NICE, *args], cwd=cwd,
            stdin=subprocess.DEVNULL if stdin_data is None else subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True,
            env={**os.environ, **env} if env else None,
        )
        if stdin_data is not None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(stdin_data)
            except BrokenPipeError:
                pass  # it exited without reading; its exit code says why
            finally:
                proc.stdin.close()
        # Drain stderr alongside stdout, so a chatty tool can't block on a full pipe.
        stderr_chunks: list[bytes] = []
        reader = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()),  # type: ignore[union-attr]
                                  daemon=True)
        reader.start()
        timed_out = threading.Event()
        watcher = threading.Thread(target=self._watch, args=(proc, timeout, timed_out),
                                   daemon=True)
        watcher.start()
        out_lines: list[str] = []
        assert proc.stdout is not None
        try:
            for raw in proc.stdout:
                line = raw.decode(errors="replace")
                out_lines.append(line)
                if stdout_line:
                    stdout_line(line.rstrip("\n"))
            code = proc.wait()
        except BaseException:
            _stop(proc)  # a failing callback mustn't leave the tool running unattended
            raise
        reader.join()
        self.check_cancelled()
        name = Path(args[0]).name
        if timed_out.is_set():
            raise ConversionError(_("{name} stopped responding (no result after {seconds:.0f} s)")
                                  .format(name=name, seconds=timeout))
        stderr = b"".join(stderr_chunks).decode(errors="replace")
        if code not in ok_codes:
            tail = ("\n".join(stderr.strip().splitlines()[-8:])
                    or _("exit code {code}").format(code=code))
            raise ConversionError(_("{name} failed:\n{details}").format(name=name, details=tail))
        return "".join(out_lines), stderr, code

    def _watch(self, proc: subprocess.Popen[bytes], timeout: float | None,
               timed_out: threading.Event) -> None:
        waited = 0.0
        while proc.poll() is None:
            if self.cancel_event.wait(0.2):
                _stop(proc)
                return
            waited += 0.2
            if timeout is not None and waited >= timeout:
                timed_out.set()
                _stop(proc)
                return


def _stop(proc: subprocess.Popen[bytes]) -> None:
    # Ask nicely, then insist: encoders like libvpx can take a while to wind down.
    for sig, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, None)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(grace)
            return
        except subprocess.TimeoutExpired:
            continue
