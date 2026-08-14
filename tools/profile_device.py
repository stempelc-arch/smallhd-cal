from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smallhd_cal.analysis import xyz_to_xy
from smallhd_cal.automation import AutoCalibrationSettings, run_patch_capture
from smallhd_cal.lut import squeeze_legal, write_smallhd_cube
from smallhd_cal.measurement import read_measurements_json
from smallhd_cal.paths import default_app_paths

STEPS = ("nolut", "identity-full", "swap-marker", "identity-legal")

PROFILE_PATCHES = """patch_name,r,g,b
black,0,0,0
gray_064,0.25098039215686274,0.25098039215686274,0.25098039215686274
gray_128,0.5019607843137255,0.5019607843137255,0.5019607843137255
gray_192,0.7529411764705882,0.7529411764705882,0.7529411764705882
white,1,1,1
red,1,0,0
green,0,1,0
blue,0,0,1
"""


def cmd_generate(args: argparse.Namespace) -> None:
    out = Path(args.dir)
    out.mkdir(parents=True, exist_ok=True)

    (out / "patch_sequence_profile.csv").write_text(PROFILE_PATCHES, encoding="utf-8")

    write_smallhd_cube(out / "diag_identity_full.cube", args.size)

    # Written red-fastest on purpose: if the importer indexes blue-fastest
    # (as PageOS 6 on the 1703 PX does), the red boost lands on blue instead.
    def red_boost(r: float, g: float, b: float) -> tuple[float, float, float]:
        return min(r + 0.35, 1.0), g, b

    write_smallhd_cube(out / "diag_swap_marker.cube", args.size, red_boost, index_order="red-fastest")

    def legal_identity(r: float, g: float, b: float) -> tuple[float, float, float]:
        return squeeze_legal(r), squeeze_legal(g), squeeze_legal(b)

    write_smallhd_cube(out / "diag_identity_legal.cube", args.size, legal_identity)

    print(f"Wrote diagnostic LUTs and profile patch set to {out}")
    print("Copy the three .cube files to the SD card and import them on the monitor.")
    print("Then for each step below, activate the named LUT (or none) and run capture:")
    for step in STEPS:
        print(f"  profile_device.py capture --dir {args.dir} --step {step}")


def cmd_capture(args: argparse.Namespace) -> None:
    out = Path(args.dir)
    settings = AutoCalibrationSettings(
        patch_csv=str(out / "patch_sequence_profile.csv"),
        measurements_json=str(out / f"capture_{args.step}.json"),
        resume=False,
    )
    run_patch_capture(default_app_paths(ROOT), settings)
    print(f"Captured {args.step}.")


def cmd_analyze(args: argparse.Namespace) -> None:
    out = Path(args.dir)
    captures = {}
    for step in STEPS:
        path = out / f"capture_{step}.json"
        if path.exists():
            captures[step] = {m.patch.name: m for m in read_measurements_json(path)}
    if "nolut" not in captures:
        raise SystemExit("At least the 'nolut' capture is required.")

    profile: dict[str, object] = {}
    ref = captures["nolut"]
    print("== Reference (no LUT, bypass) ==")
    _print_levels(ref)

    # Some models change backlight state when any LUT is active (the Cine 7
    # drops from ~1420 to ~104 nits), so all comparisons are white-relative.
    ref_black_rel = ref["black"].xyz[1] / ref["white"].xyz[1]
    profile["bypass_white_y"] = round(ref["white"].xyz[1], 2)

    if "identity-full" in captures:
        cur = captures["identity-full"]
        print("\n== Identity LUT, full-range content ==")
        _print_levels(cur)
        profile["lut_mode_white_y"] = round(cur["white"].xyz[1], 2)
        backlight_ratio = cur["white"].xyz[1] / ref["white"].xyz[1]
        if not 0.9 < backlight_ratio < 1.1:
            print(f"-> Backlight state changes with a LUT active: white {backlight_ratio:.2f}x "
                  "of bypass. Capture the session baseline with an identity LUT active.")
            profile["lut_mode_changes_backlight"] = True
        black_rel = cur["black"].xyz[1] / cur["white"].xyz[1]
        if black_rel < ref_black_rel * 0.5:
            verdict = "expands-values"
            print("-> Black got relatively darker: LUT stage expands values legal->full")
            print("   (and the feed is legal-range: bypass never reached true black).")
        else:
            verdict = "applies-as-is"
            print("-> Identity LUT reproduces bypass relative levels: values applied as-is.")
        profile["identity_full_behavior"] = verdict
        profile["identity_full_black_rel"] = round(black_rel, 5)
        profile["bypass_black_rel"] = round(ref_black_rel, 5)

    if "swap-marker" in captures:
        cur = captures["swap-marker"]
        print("\n== Swap-marker LUT (red boost, written red-fastest) ==")
        # Diagonal entries (black/gray/white) look the same under either index
        # order; the off-diagonal red/blue corners are the discriminator. With
        # a blue-fastest importer the file's red and blue axes swap, so the
        # red input lands on the boosted-blue corner entry and vice versa.
        red_in = cur["red"].xyz
        blue_in = cur["blue"].xyz
        rx, ry = xyz_to_xy(red_in)
        bx, by_ = xyz_to_xy(blue_in)
        print(f"   red input  -> xy ({rx:.4f}, {ry:.4f})")
        print(f"   blue input -> xy ({bx:.4f}, {by_:.4f})")
        if red_in[0] > red_in[2] and blue_in[2] > blue_in[0]:
            profile["index_order"] = "red-fastest"
            print("-> Channels intact: importer indexes red-fastest (standard .cube).")
        elif red_in[2] > red_in[0] and blue_in[0] > blue_in[2]:
            profile["index_order"] = "blue-fastest"
            print("-> Red and blue swapped: importer indexes blue-fastest (as on the 1703 PX).")
        else:
            profile["index_order"] = "inconclusive"
            print("-> Inconclusive channel behavior; inspect the capture manually.")

    if "identity-legal" in captures:
        cur = captures["identity-legal"]
        print("\n== Identity LUT, legal-range content ==")
        _print_levels(cur)
        black_rel = cur["black"].xyz[1] / cur["white"].xyz[1]
        profile["identity_legal_black_rel"] = round(black_rel, 5)
        if 0.7 < black_rel / ref_black_rel < 1.4:
            print("-> Legal-content identity reproduces bypass relative levels: with the")
            print("   device's range handling, legal content round-trips cleanly. Use this")
            print("   LUT as the active LUT for baseline captures.")
            profile["identity_legal_reproduces_bypass"] = True
        else:
            print("-> Legal-content identity does NOT reproduce bypass relative levels;")
            print("   inspect before calibrating.")
            profile["identity_legal_reproduces_bypass"] = False

    (out / "profile.json").write_text(json.dumps(profile, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {out / 'profile.json'}")


def _print_levels(ms: dict) -> None:
    for name in ("black", "gray_128", "white"):
        m = ms[name]
        x, y = xyz_to_xy(m.xyz)
        print(f"   {name:9s} Y {m.xyz[1]:8.3f}  xy ({x:.4f}, {y:.4f})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Profile a SmallHD monitor's LUT pipeline behavior in one SD-card trip: "
        "generate diagnostic LUTs, capture short patch runs per LUT, then analyze."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("generate", help="Write diagnostic LUTs and the short patch set")
    p.add_argument("--dir", required=True)
    p.add_argument("--size", type=int, default=17)
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("capture", help="Capture the profile patch set for one step")
    p.add_argument("--dir", required=True)
    p.add_argument("--step", required=True, choices=STEPS)
    p.set_defaults(func=cmd_capture)

    p = sub.add_parser("analyze", help="Analyze captured steps into a device profile")
    p.add_argument("--dir", required=True)
    p.set_defaults(func=cmd_analyze)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
