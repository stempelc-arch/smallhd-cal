from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    patches: list[tuple[str, int, int, int]] = []

    # grayscale ramp
    for value in [0, 16, 32, 48, 64, 80, 96, 112, 128, 144, 160, 176, 192, 208, 224, 235, 255]:
        patches.append((f"gray_{value}", value, value, value))

    # primaries / secondaries
    patches.extend(
        [
            ("red", 255, 0, 0),
            ("green", 0, 255, 0),
            ("blue", 0, 0, 255),
            ("cyan", 0, 255, 255),
            ("magenta", 255, 0, 255),
            ("yellow", 255, 255, 0),
            ("white", 255, 255, 255),
        ]
    )

    out = ROOT / "measurements" / "patch_sequence_v0.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "r", "g", "b"])
        writer.writerows(patches)
    print(f"Wrote {len(patches)} patches to {out}")


if __name__ == "__main__":
    main()
