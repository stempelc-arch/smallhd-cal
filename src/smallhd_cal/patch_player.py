from __future__ import annotations

import argparse
import time
from pathlib import Path

import cv2
import numpy as np

from smallhd_cal.measurement import load_patch_sequence
from smallhd_cal.paths import default_app_paths, resolve_existing_path

# src/smallhd_cal/patch_player.py -> project root, so the default --csv (and
# any relative path passed in) resolves the same way regardless of the
# process's current working directory, matching automation.py/gui.py.
ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description="Fullscreen RGB patch player for monitor measurement.")
    parser.add_argument("--csv", default="measurements/patch_sequence_v0.csv")
    parser.add_argument("--seconds", type=float, default=1.0)
    parser.add_argument("--window", default="SmallHD Patch Player")
    args = parser.parse_args()

    paths = default_app_paths(ROOT)
    csv_path = resolve_existing_path(args.csv, paths.resource_root, paths.user_data_root)
    patches = load_patch_sequence(csv_path)
    cv2.namedWindow(args.window, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(args.window, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    try:
        for idx, patch in enumerate(patches, start=1):
            r, g, b = patch.rgb8
            # OpenCV uses BGR memory order.
            img = np.zeros((1080, 1920, 3), dtype=np.uint8)
            img[:, :] = (b, g, r)
            cv2.imshow(args.window, img)
            print(f"Patch {idx}/{len(patches)} {patch.name} RGB=({r}, {g}, {b})")
            cv2.waitKey(1)
            time.sleep(args.seconds)
    finally:
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
