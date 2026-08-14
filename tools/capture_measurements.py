from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smallhd_cal.measurement import (
    Measurement,
    latest_measurements_by_patch,
    load_patch_sequence,
    read_measurements_json,
    write_measurements_json,
)
from smallhd_cal.probe import (
    SPOTREAD_ARGS,
    ProbeCommand,
    ProbeError,
    find_bundled_spotread,
    read_spotread,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Capture XYZ probe measurements for a SmallHD patch sequence."
    )
    parser.add_argument("--csv", default="measurements/patch_sequence_v0.csv")
    parser.add_argument("--out", default="measurements/measurements_v0.json")
    parser.add_argument(
        "--command",
        default=None,
        help="Probe command to run. Defaults to bundled Argyll spotread if present, else spotread on PATH.",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--no-prompt", action="store_true")
    parser.add_argument("--resume", action="store_true", help="Skip patches already present in --out.")
    args = parser.parse_args()

    patches = load_patch_sequence(ROOT / args.csv)
    out = ROOT / args.out
    measurements = read_measurements_json(out) if args.resume else []
    measured_by_patch = latest_measurements_by_patch(measurements)
    command = _probe_command(args.command)
    print(f"Using probe command: {_format_command(command)}")
    if args.resume and measurements:
        print(f"Resuming with {len(measurements)} existing measurements from {out}")

    for index, patch in enumerate(patches, start=1):
        if patch.name in measured_by_patch:
            print(f"[{index}/{len(patches)}] Skip {patch.name}; already measured")
            continue

        r, g, b = patch.rgb8
        print(f"[{index}/{len(patches)}] Measure {patch.name} RGB=({r}, {g}, {b})")
        if not args.no_prompt:
            input("Show this patch full-screen, place the probe, then press Enter...")

        try:
            reading = read_spotread(command, timeout=args.timeout)
        except ProbeError as exc:
            raise SystemExit(f"Measurement failed for {patch.name}: {exc}") from exc

        measurement = Measurement(
            patch=patch,
            xyz=reading.xyz,
            timestamp=datetime.now(UTC).isoformat(),
        )
        measurements.append(measurement)
        measured_by_patch[patch.name] = measurement
        write_measurements_json(out, measurements)
        print(f"  XYZ={reading.xyz[0]:.4f} {reading.xyz[1]:.4f} {reading.xyz[2]:.4f}")

    write_measurements_json(out, measurements)
    print(f"Wrote {len(measurements)} measurements to {out}")

def _probe_command(command: str | None) -> ProbeCommand:
    if command is not None:
        return command

    bundled = find_bundled_spotread(ROOT)
    if bundled is not None:
        return [str(bundled), *SPOTREAD_ARGS]

    return ["spotread", *SPOTREAD_ARGS]


def _format_command(command: ProbeCommand) -> str:
    if isinstance(command, str):
        return command
    return " ".join(command)


if __name__ == "__main__":
    main()
