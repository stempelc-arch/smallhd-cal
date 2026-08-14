from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from smallhd_cal.measurement import Measurement

D65_XY = (0.3127, 0.3290)


@dataclass(frozen=True)
class MeasurementSummary:
    total_patches: int
    grayscale_patches: int
    black_y: float
    white_y: float
    contrast_ratio: float
    white_xy: tuple[float, float]
    white_xy_error: float
    estimated_gamma: float


def summarize_measurements(measurements: list[Measurement]) -> MeasurementSummary:
    if not measurements:
        raise ValueError("At least one measurement is required.")

    grayscale = sorted(
        (measurement for measurement in measurements if _is_grayscale(measurement)),
        key=lambda measurement: measurement.patch.r,
    )
    if len(grayscale) < 3:
        raise ValueError("At least three grayscale measurements are required.")

    black = grayscale[0]
    white = grayscale[-1]
    black_y = black.xyz[1]
    white_y = white.xyz[1]
    if white_y <= black_y:
        raise ValueError("White luminance must be greater than black luminance.")
    if black_y < 0:
        # Luminance can't be physically negative; a slightly negative reading
        # is probe noise near true black. max(black_y, 1e-6) below would
        # silently turn that into a wildly inflated contrast ratio instead of
        # flagging the invalid measurement.
        raise ValueError(f"Black measurement luminance is negative ({black_y:.6f}); reading is invalid.")

    contrast_ratio = white_y / max(black_y, 1e-6)
    white_xy = xyz_to_xy(white.xyz)
    white_xy_error = float(np.hypot(white_xy[0] - D65_XY[0], white_xy[1] - D65_XY[1]))

    return MeasurementSummary(
        total_patches=len(measurements),
        grayscale_patches=len(grayscale),
        black_y=black_y,
        white_y=white_y,
        contrast_ratio=contrast_ratio,
        white_xy=white_xy,
        white_xy_error=white_xy_error,
        estimated_gamma=estimate_gamma(grayscale),
    )


def xyz_to_xy(xyz: tuple[float, float, float]) -> tuple[float, float]:
    x, y, z = xyz
    total = x + y + z
    if total <= 0.0:
        return 0.0, 0.0
    return x / total, y / total


def estimate_gamma(grayscale: list[Measurement]) -> float:
    black_y = grayscale[0].xyz[1]
    white_y = grayscale[-1].xyz[1]
    if white_y <= black_y:
        raise ValueError("White luminance must be greater than black luminance.")

    codes: list[float] = []
    luminance: list[float] = []
    for measurement in grayscale:
        code = measurement.patch.r
        normalized_y = (measurement.xyz[1] - black_y) / (white_y - black_y)
        if 0.0 < code < 1.0 and normalized_y > 0.0:
            codes.append(code)
            luminance.append(normalized_y)

    if len(codes) < 2:
        raise ValueError("At least two nonzero grayscale points are required to estimate gamma.")

    slope, _intercept = np.polyfit(np.log(codes), np.log(luminance), deg=1)
    return float(slope)


def _is_grayscale(measurement: Measurement) -> bool:
    patch = measurement.patch
    return np.isclose(patch.r, patch.g) and np.isclose(patch.g, patch.b)


@dataclass(frozen=True)
class DeltaERow:
    name: str
    de2000: float


@dataclass(frozen=True)
class DeltaEReport:
    rows: list[DeltaERow]
    average: float
    maximum: float
    worst_name: str


def xyz_to_lab(xyz, white_xyz) -> tuple[float, float, float]:
    """CIE 1976 L*a*b* from XYZ, relative to the given reference white."""
    def f(t: float) -> float:
        return t ** (1.0 / 3.0) if t > (6.0 / 29.0) ** 3 else t / (3 * (6.0 / 29.0) ** 2) + 4.0 / 29.0

    fx, fy, fz = (f(max(c, 0.0) / max(w, 1e-12)) for c, w in zip(xyz, white_xyz, strict=True))
    return 116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)


def delta_e_2000(lab1, lab2) -> float:
    """CIEDE2000 color difference (Sharma et al. reference implementation)."""
    L1, a1, b1 = lab1
    L2, a2, b2 = lab2
    C1 = math.hypot(a1, b1)
    C2 = math.hypot(a2, b2)
    C_bar = (C1 + C2) / 2.0
    G = 0.5 * (1.0 - math.sqrt(C_bar**7 / (C_bar**7 + 25.0**7)))
    a1p, a2p = (1 + G) * a1, (1 + G) * a2
    C1p, C2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360 if (a1p or b1) else 0.0
    h2p = math.degrees(math.atan2(b2, a2p)) % 360 if (a2p or b2) else 0.0

    dLp = L2 - L1
    dCp = C2p - C1p
    if C1p * C2p == 0:
        dhp = 0.0
    else:
        dh = h2p - h1p
        if dh > 180:
            dh -= 360
        elif dh < -180:
            dh += 360
        dhp = dh
    dHp = 2 * math.sqrt(C1p * C2p) * math.sin(math.radians(dhp) / 2)

    Lp_bar = (L1 + L2) / 2
    Cp_bar = (C1p + C2p) / 2
    if C1p * C2p == 0:
        hp_bar = h1p + h2p
    else:
        hp_bar = (h1p + h2p) / 2
        if abs(h1p - h2p) > 180:
            hp_bar += 180 if hp_bar < 180 else -180

    T = (
        1
        - 0.17 * math.cos(math.radians(hp_bar - 30))
        + 0.24 * math.cos(math.radians(2 * hp_bar))
        + 0.32 * math.cos(math.radians(3 * hp_bar + 6))
        - 0.20 * math.cos(math.radians(4 * hp_bar - 63))
    )
    d_theta = 30 * math.exp(-(((hp_bar - 275) / 25) ** 2))
    RC = 2 * math.sqrt(Cp_bar**7 / (Cp_bar**7 + 25.0**7))
    SL = 1 + 0.015 * (Lp_bar - 50) ** 2 / math.sqrt(20 + (Lp_bar - 50) ** 2)
    SC = 1 + 0.045 * Cp_bar
    SH = 1 + 0.015 * Cp_bar * T
    RT = -math.sin(math.radians(2 * d_theta)) * RC
    return math.sqrt(
        (dLp / SL) ** 2
        + (dCp / SC) ** 2
        + (dHp / SH) ** 2
        + RT * (dCp / SC) * (dHp / SH)
    )


def verify_delta_e_report(
    measurements: list[Measurement],
    *,
    target_name: str = "rec709",
    target_gamma: float = 2.4,
) -> DeltaEReport:
    """Score a verify capture against the ideal target in CIEDE2000.

    Targets are pure colorimetry: target-linear RGB (signal^gamma) through the
    target's RGB->XYZ matrix, scaled so target white carries the measured white
    patch's luminance. Both sides convert to Lab against that ideal white.
    """
    from smallhd_cal.calibration import _rgb_to_xyz_matrix, color_target

    if not measurements:
        raise ValueError("At least one measurement is required.")
    target = color_target(target_name)
    matrix = _rgb_to_xyz_matrix(target.primaries_xy, target.white_xy)
    white_target = matrix @ np.ones(3)
    # A name-keyed dict would silently keep only the last "white" reading if a
    # capture ever contains two (a re-measure appended under the same patch
    # name); since every row's Lab reference hangs off this single value,
    # require it to be unambiguous instead of picking one without warning.
    whites = [m for m in measurements if m.patch.name == "white"]
    if len(whites) > 1:
        raise ValueError(
            f"Verify capture has {len(whites)} patches named 'white'; expected at most one."
        )
    white_y = whites[0].xyz[1] if whites else max(m.xyz[1] for m in measurements)
    scale = white_y / white_target[1]
    white_ref = tuple(white_target * scale)

    rows = []
    for m in measurements:
        rgb = np.clip([m.patch.r, m.patch.g, m.patch.b], 0.0, 1.0)
        target_xyz = tuple(matrix @ (rgb**target_gamma) * scale)
        de = delta_e_2000(xyz_to_lab(target_xyz, white_ref), xyz_to_lab(m.xyz, white_ref))
        rows.append(DeltaERow(name=m.patch.name, de2000=de))
    worst = max(rows, key=lambda r: r.de2000)
    return DeltaEReport(
        rows=rows,
        average=sum(r.de2000 for r in rows) / len(rows),
        maximum=worst.de2000,
        worst_name=worst.name,
    )
