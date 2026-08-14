"""Generate BMD-format diagnostic LUTs for the SmallHD import wizard.

Why: every tool SmallHD certifies (ColourSpace BMD17 export, Resolve, Calman)
writes standard .cube files — LUT_3D_SIZE header, red-fastest rows, full 0-1
domain — and SmallHD reports "no known issues" importing those. This project's
legacy cubes (LUT_SIZE header cloned from the monitor's own export) appear to
hit a different firmware parser branch with content-dependent range guessing.
These diagnostics establish, in one SD trip, how the standard-format path
behaves on a given monitor:

  diag_bmd17_identity.cube   Import TWICE (as two separate custom calibrations)
                             and capture a verify sweep under each. Identical
                             readings = deterministic import.
  diag_bmd17_swapmark.cube   Rotates channels (input r,g,b -> output g,b,r).
                             Show a pure RED patch: BLUE screen means the
                             importer parsed red-fastest correctly; GREEN
                             screen means it read the file blue-fastest.
  diag_bmd17_gammamark.cube  Applies x^0.5 in the LUT's own input domain.
                             Capture a verify sweep. The black patch tells you
                             what the LUT input is: if black stays at the
                             baseline floor the monitor normalizes video range
                             BEFORE the LUT; if black jumps to roughly
                             0.25-drive gray (obvious, several nits) the LUT is
                             indexed by raw code values.

Wizard settings for every import: same calibration target, declared input
range FULL (SmallHD's own recommendation for the wizard), dynamic-range step
skipped, manual adjustments off. Repeat the whole set with declared LEGAL only
if the FULL results are surprising.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smallhd_cal.lut import write_bmd_cube

DEFAULT_OUT = Path.home() / "Documents" / "SmallHD Calibration" / "bmd_diagnostics"


def generate(out_dir: Path, size: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    write_bmd_cube(out_dir / f"diag_bmd{size}_identity.cube", size)
    write_bmd_cube(
        out_dir / f"diag_bmd{size}_swapmark.cube",
        size,
        lambda r, g, b: (g, b, r),
    )
    write_bmd_cube(
        out_dir / f"diag_bmd{size}_gammamark.cube",
        size,
        lambda r, g, b: (math.sqrt(r), math.sqrt(g), math.sqrt(b)),
    )
    print(f"Wrote 3 diagnostic cubes ({size}-point, standard BMD format) to:\n  {out_dir}\n")
    print(__doc__.split("behaves on a given monitor:", 1)[1])


def _patches(path: Path) -> dict[str, list[float]]:
    data = json.loads(path.read_text())
    return {m["patch"]["name"]: m["xyz"] for m in data["measurements"]}


def analyze(identity_a: Path, identity_b: Path | None, gammamark: Path | None, baseline: Path | None) -> None:
    a = _patches(identity_a)
    if identity_b is not None:
        b = _patches(identity_b)
        worst = 0.0
        for name in sorted(set(a) & set(b)):
            ya, yb = a[name][1], b[name][1]
            rel = abs(ya - yb) / max(ya, yb, 1e-6)
            worst = max(worst, rel)
            print(f"  {name:10s} Y {ya:8.2f} vs {yb:8.2f}  ({100 * rel:.1f}% delta)")
        verdict = "DETERMINISTIC" if worst < 0.05 else "INCONSISTENT (same old story)"
        print(f"Identity import consistency: worst luminance delta {100 * worst:.1f}% -> {verdict}")
    if gammamark is not None and baseline is not None:
        g = _patches(gammamark)
        base = _patches(baseline)
        if "black" in g and "black" in base:
            lift = g["black"][1] / max(base["black"][1], 1e-6)
            domain = "NORMALIZED before LUT (author full-domain LUTs)" if lift < 3 else \
                "RAW CODE indexing (legal wrap still required)"
            print(f"Gammamark black: {g['black'][1]:.2f} nits vs baseline {base['black'][1]:.2f} "
                  f"({lift:.1f}x) -> LUT input domain: {domain}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    gen = sub.add_parser("generate", help="Write the diagnostic cubes (default).")
    gen.add_argument("--out", type=Path, default=DEFAULT_OUT)
    gen.add_argument("--size", type=int, default=17)
    ana = sub.add_parser("analyze", help="Interpret captured verify sweeps.")
    ana.add_argument("--identity-a", type=Path, required=True)
    ana.add_argument("--identity-b", type=Path)
    ana.add_argument("--gammamark", type=Path)
    ana.add_argument("--baseline", type=Path, help="No-correction (identity) capture for the black floor.")
    args = parser.parse_args()

    if args.cmd == "analyze":
        analyze(args.identity_a, args.identity_b, args.gammamark, args.baseline)
    else:
        out = getattr(args, "out", DEFAULT_OUT)
        size = getattr(args, "size", 17)
        generate(out, size)


if __name__ == "__main__":
    main()
