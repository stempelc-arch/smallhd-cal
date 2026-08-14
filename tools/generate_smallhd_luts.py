from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smallhd_cal.lut import write_smallhd_cube


def main() -> None:
    out_dir = ROOT / "luts"
    write_smallhd_cube(out_dir / "SmallHD_identity_17.cube", 17)
    write_smallhd_cube(out_dir / "SmallHD_identity_33.cube", 33)
    write_smallhd_cube(out_dir / "SmallHD_VISIBLE_TEST_redbias_17.cube", 17, lambda r, g, b: (r * 1.15, g * 0.92, b * 0.92))
    write_smallhd_cube(out_dir / "SmallHD_VISIBLE_TEST_gamma_17.cube", 17, lambda r, g, b: (r ** 0.8, g ** 0.8, b ** 0.8))
    print(f"Wrote test LUTs to {out_dir}")


if __name__ == "__main__":
    main()
