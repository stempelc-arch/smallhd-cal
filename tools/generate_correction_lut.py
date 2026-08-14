from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smallhd_cal.calibration import CORRECTION_MODES, build_correction_transform
from smallhd_cal.lut import wrap_legal_range, write_smallhd_cube
from smallhd_cal.measurement import read_measurements_json


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a SmallHD correction LUT from measured patches."
    )
    parser.add_argument("--measurements", default="measurements/measurements_v0.json")
    parser.add_argument("--out", default="luts/SmallHD_correction_gray_gamma24_33.cube")
    parser.add_argument("--size", type=int, default=33)
    parser.add_argument("--gamma", type=float, default=2.4)
    parser.add_argument(
        "--mode",
        choices=CORRECTION_MODES,
        default="gray",
        help="gray: grayscale/gamma only; rec709, p3-d65, and dci-p3: also correct "
        "primaries and white toward the named target",
    )
    parser.add_argument(
        "--lut-range",
        choices=("legal", "full"),
        default="legal",
        help="legal: compensate for the SmallHD LUT stage treating values as 16-235 video "
        "(verified on PageOS 6); full: write the correction unwrapped",
    )
    args = parser.parse_args()

    measurements = read_measurements_json(ROOT / args.measurements)
    transform = build_correction_transform(measurements, mode=args.mode, target_gamma=args.gamma)
    if args.lut_range == "legal":
        transform = wrap_legal_range(transform)
    write_smallhd_cube(ROOT / args.out, args.size, transform)
    print(f"Wrote {args.mode} correction LUT ({args.lut_range} range) to {ROOT / args.out}")


if __name__ == "__main__":
    main()
