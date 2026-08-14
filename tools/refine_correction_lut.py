from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smallhd_cal.lut import write_smallhd_cube
from smallhd_cal.measurement import read_measurements_json
from smallhd_cal.refine import build_refined_correction


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Refine a rec709 correction LUT using a verify capture taken with the "
        "previous LUT active, fitting the monitor's actual LUT application behavior."
    )
    parser.add_argument("--baseline", default="measurements/measurements_v0.json")
    parser.add_argument(
        "--verify",
        required=True,
        nargs="+",
        help="Verify capture JSONs in order; the first taken with the plain generated LUT "
        "active, each subsequent one with the previous refinement active.",
    )
    parser.add_argument("--out", default="luts/SmallHD_correction_rec709_gamma24_33.cube")
    parser.add_argument("--size", type=int, default=33)
    parser.add_argument("--gamma", type=float, default=2.4)
    parser.add_argument(
        "--damping",
        type=float,
        nargs="+",
        default=None,
        help="Per-verify color damping schedule (one value per --verify entry), matching "
        "how each historical LUT was generated. Default: full step except the newest, 0.5.",
    )
    args = parser.parse_args()

    baseline = read_measurements_json(ROOT / args.baseline)
    verifies = [read_measurements_json(ROOT / path) for path in args.verify]
    transform = build_refined_correction(
        baseline, verifies, target_gamma=args.gamma, color_dampings=args.damping
    )
    write_smallhd_cube(ROOT / args.out, args.size, transform)
    print(f"Wrote refined rec709 correction LUT to {ROOT / args.out}")


if __name__ == "__main__":
    main()
