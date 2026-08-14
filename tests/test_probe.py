import subprocess
from pathlib import Path

import pytest

from smallhd_cal.probe import (
    _TRIGGER_RE,
    SPOTREAD_ARGS,
    XYZ_RE,
    ProbeError,
    SpotreadSession,
    find_bundled_spotread,
    parse_xyz_output,
    read_spotread,
)


def test_parse_xyz_output_from_spotread_text() -> None:
    output = """
    Place instrument on spot to be measured
    Result is XYZ: 12.345 67.89 0.123
    """

    assert parse_xyz_output(output) == (12.345, 67.89, 0.123)


def test_parse_xyz_output_raises_for_missing_xyz() -> None:
    with pytest.raises(ProbeError, match="XYZ"):
        parse_xyz_output("No measurement available")


def test_read_spotread_parses_command_output(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args == (["spotread", *SPOTREAD_ARGS],)
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        return subprocess.CompletedProcess(args[0], 0, stdout="XYZ: 1 2 3\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    reading = read_spotread()

    assert reading.xyz == (1.0, 2.0, 3.0)


def test_read_spotread_reports_missing_command(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ProbeError, match="not found"):
        read_spotread("missing-spotread")


def test_find_bundled_spotread(tmp_path: Path) -> None:
    spotread = tmp_path / "Argyll_V3.5.0" / "bin" / "spotread"
    spotread.parent.mkdir(parents=True)
    spotread.write_text("", encoding="utf-8")

    assert find_bundled_spotread(tmp_path) == spotread


def test_find_bundled_spotread_prefers_current_arch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    arm_spotread = tmp_path / "Argyll_V3.5.0" / "bin" / "spotread"
    x86_spotread = tmp_path / "Argyll_V3.5.0 3" / "bin" / "spotread"
    arm_spotread.parent.mkdir(parents=True)
    x86_spotread.parent.mkdir(parents=True)
    arm_spotread.write_text("", encoding="utf-8")
    x86_spotread.write_text("", encoding="utf-8")

    monkeypatch.setattr("smallhd_cal.probe.platform.machine", lambda: "x86_64")

    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        path = args[0][1]
        arch = "x86_64" if str(path).endswith("Argyll_V3.5.0 3/bin/spotread") else "arm64"
        return subprocess.CompletedProcess(args[0], 0, stdout=f"Mach-O 64-bit executable {arch}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert find_bundled_spotread(tmp_path) == x86_spotread


def test_spotread_session_parses_buffered_readings() -> None:
    # The prompt/XYZ parsing works on buffered interactive output without a
    # real process (the fd read path is exercised on hardware).
    session = SpotreadSession(["spotread", "-e", "-x"])
    session._buffer = (
        "Place instrument and hit [space] to trigger a reading:\n"
        " Result is XYZ: 12.0 34.0 56.0, D50 Lab: ...\n"
        "hit [space] to trigger a reading:\n"
        " Result is XYZ: 20.5 41.0 60.0, D50 Lab: ...\n"
    )
    first = session._read_until(XYZ_RE, 1.0, "x")
    assert parse_xyz_output(first) == (12.0, 34.0, 56.0)
    second = session._read_until(XYZ_RE, 1.0, "x")
    assert parse_xyz_output(second) == (20.5, 41.0, 60.0)


def test_trigger_prompt_regex_matches_spotread_prompts() -> None:
    for prompt in (
        "Place instrument on spot to be measured, and hit [space] to trigger a reading",
        "hit [space] to take a reading and [Esc] to exit:",
        "Hit any other key to take a reading:",
    ):
        assert _TRIGGER_RE.search(prompt)


def test_spotread_session_read_without_process_raises() -> None:
    with pytest.raises(ProbeError):
        SpotreadSession(["spotread"]).read()
