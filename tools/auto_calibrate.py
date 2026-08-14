from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smallhd_cal.automation import (
    AutoCalibrationSettings,
    AutomationError,
    run_auto_calibration,
)
from smallhd_cal.calibration import CORRECTION_MODES
from smallhd_cal.paths import default_app_paths


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fully automate SmallHD patch display, probe capture, analysis, and LUT generation."
    )
    parser.add_argument("--csv", default="measurements/patch_sequence_v0.csv")
    parser.add_argument("--out", default="measurements/measurements_v0.json")
    parser.add_argument("--lut", default="luts/SmallHD_correction_gray_gamma24_33.cube")
    parser.add_argument("--settle", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--gamma", type=float, default=2.4)
    parser.add_argument("--size", type=int, default=33)
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument(
        "--mode",
        choices=CORRECTION_MODES,
        default="gray",
        help="gray: grayscale/gamma only; rec709, p3-d65, and dci-p3: also correct "
        "primaries and white toward the named target",
    )
    args = parser.parse_args()

    settings = AutoCalibrationSettings(
        patch_csv=args.csv,
        measurements_json=args.out,
        output_lut=args.lut,
        settle_seconds=args.settle,
        timeout=args.timeout,
        gamma=args.gamma,
        lut_size=args.size,
        resume=not args.no_resume,
        correction_mode=args.mode,
    )

    try:
        result = run_auto_calibration(default_app_paths(ROOT), settings)
    except AutomationError as exc:
        raise SystemExit(str(exc)) from exc

    print("Automation complete")
    print(f"Measurements: {result.measurement_count}")
    print(f"White Y: {result.summary.white_y:.4f}")
    print(f"Estimated gamma: {result.summary.estimated_gamma:.3f}")
    print(f"LUT: {result.lut_path}")


if __name__ == "__main__":
    main()
