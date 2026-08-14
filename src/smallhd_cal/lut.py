from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import numpy as np

RGBFn = Callable[[float, float, float], tuple[float, float, float]]

LEGAL_BLACK = 16.0 / 255.0
LEGAL_SCALE = 219.0 / 255.0


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


def expand_legal(value: float) -> float:
    return clamp01((value - LEGAL_BLACK) / LEGAL_SCALE)


def squeeze_legal(value: float) -> float:
    return LEGAL_BLACK + LEGAL_SCALE * clamp01(value)


def wrap_legal_range(transform: RGBFn) -> RGBFn:
    """Adapt a full-range correction transform for a legal-range LUT pipeline.

    Probe verification on SmallHD PageOS 6 shows the 3D LUT stage indexes
    entries by raw byte position (so with a legal-range video feed, grid point
    p is looked up for signal expand_legal(p)) and expands stored values from
    legal to full range before driving the panel. Compensate by sampling the
    correction at the signal each grid point really represents, and
    double-squeezing outputs: after one baseline squeeze (the drive the
    calibration measurements correspond to) plus the monitor's expansion, the
    panel receives exactly the intended drive.
    """
    def wrapped(r: float, g: float, b: float) -> tuple[float, float, float]:
        out = transform(expand_legal(r), expand_legal(g), expand_legal(b))
        return tuple(squeeze_legal(squeeze_legal(channel)) for channel in out)

    return wrapped


INDEX_ORDERS = ("blue-fastest", "red-fastest")


class CubeLUT:
    """A parsed 3D LUT with trilinear lookup.

    `grid[ri, gi, bi]` holds the stored RGB triplet for grid point
    (ri, gi, bi) / (size - 1) regardless of the file's row order.
    """

    def __init__(self, grid: np.ndarray) -> None:
        if grid.ndim != 4 or grid.shape[3] != 3 or len(set(grid.shape[:3])) != 1:
            raise ValueError("CubeLUT grid must have shape (size, size, size, 3).")
        self.grid = grid
        self.size = grid.shape[0]

    def lookup(self, r: float, g: float, b: float) -> tuple[float, float, float]:
        result = []
        coords = [clamp01(v) * (self.size - 1) for v in (r, g, b)]
        lows = [int(np.floor(c)) for c in coords]
        highs = [min(low + 1, self.size - 1) for low in lows]
        fracs = [c - low for c, low in zip(coords, lows, strict=True)]

        for channel in range(3):
            value = 0.0
            for corner in range(8):
                idx = []
                weight = 1.0
                for axis in range(3):
                    if corner >> axis & 1:
                        idx.append(highs[axis])
                        weight *= fracs[axis]
                    else:
                        idx.append(lows[axis])
                        weight *= 1.0 - fracs[axis]
                value += weight * float(self.grid[idx[0], idx[1], idx[2], channel])
            result.append(value)
        return tuple(result)

    def __call__(self, r: float, g: float, b: float) -> tuple[float, float, float]:
        return self.lookup(r, g, b)


def read_smallhd_cube(path: str | Path, index_order: str = "blue-fastest") -> CubeLUT:
    """Parse a .cube file written by write_smallhd_cube.

    `index_order` must state the order the file was written with; the returned
    grid is always addressed as grid[ri, gi, bi].
    """
    if index_order not in INDEX_ORDERS:
        raise ValueError(f"Unknown index order {index_order!r}; expected one of {INDEX_ORDERS}.")

    size = None
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        upper = line.upper()
        if upper.startswith(("LUT_SIZE ", "LUT_3D_SIZE ")):
            size = int(line.split()[-1])
            continue
        if upper[0].isalpha() or upper.startswith(('"', "'")):
            continue  # other keyword lines (TITLE, LUT_3D_INPUT_RANGE, DOMAIN_*)
        rows.append([float(part) for part in line.split()])

    if size is None:
        raise ValueError(f"No LUT_SIZE header found in {path}.")
    if len(rows) != size**3:
        raise ValueError(f"Expected {size**3} data rows in {path}, found {len(rows)}.")

    data = np.array(rows, dtype=float)
    if index_order == "blue-fastest":
        # rows iterate red slowest, green, blue fastest
        grid = data.reshape(size, size, size, 3)
    else:
        grid = data.reshape(size, size, size, 3).transpose(2, 1, 0, 3)
    return CubeLUT(grid)


def write_bmd_cube(
    path: str | Path,
    size: int,
    transform: RGBFn | None = None,
    title: str | None = None,
) -> None:
    """Write a standard Resolve/BMD-style .cube (the format SmallHD certifies).

    SmallHD's own calibration guidance says wizard imports should use
    BMD-format LUTs (17-point for Cine 7-class monitors), and the tools it
    certifies (ColourSpace BMD export, Resolve, Calman) all write the
    standard header: LUT_3D_SIZE, red-fastest rows, full 0-1 domain. The
    legacy write_smallhd_cube format (LUT_SIZE header cloned from the
    monitor's export) appears to hit a different, range-guessing parser
    branch in the firmware.
    """
    if transform is None:
        def transform(r: float, g: float, b: float) -> tuple[float, float, float]:
            return r, g, b

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(f'TITLE "{title or path.stem}"\n')
        f.write(f"LUT_3D_SIZE {size}\n")
        f.write("LUT_3D_INPUT_RANGE 0.0 1.0\n")
        values = np.linspace(0.0, 1.0, size)
        for b in values:
            for g in values:
                for r in values:
                    rr, gg, bb = transform(float(r), float(g), float(b))
                    f.write(f"{clamp01(rr):.6f} {clamp01(gg):.6f} {clamp01(bb):.6f}\n")


def read_bmd_cube(path: str | Path) -> CubeLUT:
    """Parse a standard red-fastest .cube written by write_bmd_cube."""
    return read_smallhd_cube(path, index_order="red-fastest")


def write_smallhd_cube(
    path: str | Path,
    size: int,
    transform: RGBFn | None = None,
    index_order: str = "blue-fastest",
) -> None:
    """Write a SmallHD-friendly 3D LUT.

    SmallHD firmware strings claim exported LUTs are red-fastest, but probe
    verification on PageOS 6 shows the importer indexes entries blue-fastest
    (loading a red-fastest LUT swaps red and blue on screen). Default to what
    the hardware actually does; pass index_order="red-fastest" for standard
    .cube consumers such as Resolve.
    """
    if index_order not in INDEX_ORDERS:
        raise ValueError(f"Unknown index order {index_order!r}; expected one of {INDEX_ORDERS}.")
    if transform is None:
        def transform(r: float, g: float, b: float) -> tuple[float, float, float]:
            return r, g, b

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    fastest, slowest = ("Blue", "Red") if index_order == "blue-fastest" else ("Red", "Blue")
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write("# SmallHD Exported LUT.\n")
        f.write("#\n")
        f.write("# Triplets are ordered RGB\n")
        f.write(f"# {fastest} changes fastest\n")
        f.write(f"# {slowest} changes slowest\n")
        f.write(f"LUT_SIZE {size}\n")

        values = np.linspace(0.0, 1.0, size)
        for outer in values:
            for g in values:
                for inner in values:
                    r, b = (outer, inner) if index_order == "blue-fastest" else (inner, outer)
                    rr, gg, bb = transform(float(r), float(g), float(b))
                    f.write(f"{clamp01(rr):.8f} {clamp01(gg):.8f} {clamp01(bb):.8f}\n")
