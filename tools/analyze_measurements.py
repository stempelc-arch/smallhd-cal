from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smallhd_cal.analysis import summarize_measurements
from smallhd_cal.measurement import read_measurements_json


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize captured SmallHD measurements.")
    parser.add_argument("--measurements", default="measurements/measurements_v0.json")
    args = parser.parse_args()

    measurements = read_measurements_json(ROOT / args.measurements)
    summary = summarize_measurements(measurements)

    print(f"Measurements: {summary.total_patches}")
    print(f"Grayscale patches: {summary.grayscale_patches}")
    print(f"Black Y: {summary.black_y:.4f}")
    print(f"White Y: {summary.white_y:.4f}")
    print(f"Contrast ratio: {summary.contrast_ratio:.1f}:1")
    print(f"White xy: {summary.white_xy[0]:.4f}, {summary.white_xy[1]:.4f}")
    print(f"D65 xy error: {summary.white_xy_error:.5f}")
    print(f"Estimated gamma: {summary.estimated_gamma:.3f}")


if __name__ == "__main__":
    main()
