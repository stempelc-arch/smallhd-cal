from __future__ import annotations

import os
import platform
import re
import select
import shlex
import signal
import subprocess
import time
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

XYZ_RE = re.compile(
    r"\bXYZ\b\s*:?\s+"
    r"(?P<x>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+"
    r"(?P<y>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\s+"
    r"(?P<z>[+-]?(?:\d+(?:\.\d*)?|\.\d+))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ProbeReading:
    xyz: tuple[float, float, float]
    raw_output: str


class ProbeError(RuntimeError):
    pass


def parse_xyz_output(output: str) -> tuple[float, float, float]:
    match = XYZ_RE.search(output)
    if match is None:
        raise ProbeError("Probe output did not include an XYZ reading.")

    return (
        float(match.group("x")),
        float(match.group("y")),
        float(match.group("z")),
    )


ProbeCommand = str | Sequence[str]

# -e: emissive spot reading, -x: XYZ output, -O: take one reading and exit
# instead of waiting for interactive keypresses.
SPOTREAD_ARGS = ("-e", "-x", "-O")
# Same, but interactive (no -O): keep one process open and trigger many reads.
SPOTREAD_INTERACTIVE_ARGS = ("-e", "-x")

# spotread's interactive "ready for a reading" prompt (it waits for a keypress).
_TRIGGER_RE = re.compile(
    r"hit .*?(?:to (?:take|trigger)|any other key)|take a reading|to measure",
    re.IGNORECASE,
)


def find_bundled_spotread(root: str | Path) -> Path | None:
    root = Path(root)
    matches = sorted(root.glob("Argyll*/bin/spotread"))
    if not matches:
        return None

    machine = platform.machine()
    preferred_arch = "arm64" if machine == "arm64" else "x86_64"
    for match in matches:
        if _binary_matches_arch(match, preferred_arch):
            return match

    # No bundled binary matches this machine's arch (e.g. an arm64 Mac with
    # only the x86_64 copy bundled, as this project's packaging intentionally
    # does — it runs under Rosetta). That's a working, expected configuration
    # here, but a stale/incomplete bundle would hit this same fallback with no
    # signal that arch selection didn't actually succeed — warn either way
    # rather than staying silent.
    warnings.warn(
        f"No bundled spotread matches this machine's architecture ({preferred_arch}); "
        f"falling back to {matches[0]}.",
        RuntimeWarning,
        stacklevel=2,
    )
    return matches[0]


def read_spotread(
    command: ProbeCommand = ("spotread", *SPOTREAD_ARGS), timeout: float = 60.0
) -> ProbeReading:
    args = shlex.split(command) if isinstance(command, str) else list(command)
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise ProbeError(f"Probe command not found: {args[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ProbeError(f"Probe command timed out after {timeout:g} seconds.") from exc

    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    if result.returncode != 0:
        raise ProbeError(f"Probe command failed with exit code {result.returncode}.\n{output}")

    return ProbeReading(xyz=parse_xyz_output(output), raw_output=output)


class SpotreadSession:
    """Keep one interactive spotread process open and take many readings.

    Each ``read_spotread`` call re-launches spotread and re-opens the instrument
    (~10 s of overhead per reading); this opens it once and triggers each reading
    with a keypress, so a full calibration sweep is far faster. Accuracy is
    unchanged — same instrument, same integration, calibrated once at the start.

    spotread reads single keypresses from a tty, so it is driven over a
    pseudo-terminal (a plain pipe would not trigger). The output format is the
    same as one-shot mode, so ``parse_xyz_output`` parses each reading.
    """

    def __init__(
        self,
        command: ProbeCommand,
        *,
        ready_timeout: float = 45.0,
        read_timeout: float = 90.0,
        log=None,
    ) -> None:
        self.command = shlex.split(command) if isinstance(command, str) else list(command)
        self.ready_timeout = ready_timeout
        self.read_timeout = read_timeout
        self._log = log or (lambda _message: None)
        self._proc: subprocess.Popen | None = None
        self._master_fd = -1
        self._buffer = ""

    def start(self) -> SpotreadSession:
        import pty

        master_fd, slave_fd = pty.openpty()
        try:
            self._proc = subprocess.Popen(
                self.command, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                close_fds=True, start_new_session=True,
            )
        except FileNotFoundError as exc:
            os.close(master_fd)
            os.close(slave_fd)
            raise ProbeError(f"Probe command not found: {self.command[0]}") from exc
        os.close(slave_fd)
        self._master_fd = master_fd
        # Wait until it has opened the instrument and is prompting for a reading.
        self._read_until(_TRIGGER_RE, self.ready_timeout, "instrument ready prompt")
        self._log("spotread session ready")
        return self

    def read(self) -> tuple[float, float, float]:
        if self._proc is None or self._proc.poll() is not None:
            raise ProbeError("spotread session is not running.")
        try:
            os.write(self._master_fd, b" ")  # trigger one reading
        except OSError as exc:
            raise ProbeError("spotread session closed while triggering a reading.") from exc
        text = self._read_until(XYZ_RE, self.read_timeout, "XYZ reading")
        return parse_xyz_output(text)

    def _read_until(self, pattern: re.Pattern[str], timeout: float, what: str) -> str:
        deadline = time.monotonic() + timeout
        while True:
            match = pattern.search(self._buffer)
            if match:
                consumed, self._buffer = self._buffer[: match.end()], self._buffer[match.end():]
                return consumed
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError(f"spotread timed out waiting for {what} after {timeout:g}s.")
            # select()/read() race against a concurrent close() from another
            # thread (e.g. the GUI's Escape-cancel path): closing the fd mid-call
            # can raise a bare OSError here rather than going through read()'s
            # own try/except below, so wrap both — otherwise it escapes as an
            # uncaught exception in this background thread instead of the
            # ProbeError callers already handle.
            try:
                ready, _, _ = select.select([self._master_fd], [], [], min(remaining, 1.0))
            except OSError as exc:
                raise ProbeError(f"spotread session closed while waiting for {what}.") from exc
            if ready:
                try:
                    chunk = os.read(self._master_fd, 4096)
                except OSError as exc:
                    raise ProbeError(f"spotread pipe closed while waiting for {what}.") from exc
                if chunk:
                    self._buffer += chunk.decode("utf-8", "replace")
            if self._proc is not None and self._proc.poll() is not None:
                match = pattern.search(self._buffer)
                if match:
                    return self._buffer[: match.end()]
                raise ProbeError(f"spotread exited before {what}.")

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            os.write(self._master_fd, b"q")  # ask interactive spotread to quit
        except OSError:
            pass
        try:
            self._proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
            except OSError:
                pass
        try:
            os.close(self._master_fd)
        except OSError:
            pass
        self._proc = None

    def __enter__(self) -> SpotreadSession:
        return self.start()

    def __exit__(self, *_exc) -> None:
        self.close()


def _binary_matches_arch(path: Path, arch: str) -> bool:
    try:
        result = subprocess.run(
            ["file", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

    return result.returncode == 0 and arch in result.stdout
