from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from smallhd_cal.measurement import Measurement

CorrectionFn = Callable[[float], float]
RGBTransform = Callable[[float, float, float], tuple[float, float, float]]

D65_XY = (0.3127, 0.3290)
DCI_WHITE_XY = (0.3140, 0.3510)
REC709_PRIMARIES_XY = (
    (0.64, 0.33),
    (0.30, 0.60),
    (0.15, 0.06),
)
P3_PRIMARIES_XY = (
    (0.680, 0.320),
    (0.265, 0.690),
    (0.150, 0.060),
)


@dataclass(frozen=True)
class ColorTarget:
    name: str
    primaries_xy: tuple[tuple[float, float], ...]
    white_xy: tuple[float, float]
    default_gamma: float


COLOR_TARGETS = {
    "rec709": ColorTarget("rec709", REC709_PRIMARIES_XY, D65_XY, 2.4),
    "p3-d65": ColorTarget("p3-d65", P3_PRIMARIES_XY, D65_XY, 2.4),
    "dci-p3": ColorTarget("dci-p3", P3_PRIMARIES_XY, DCI_WHITE_XY, 2.6),
}

CORRECTION_MODES = ("gray", *COLOR_TARGETS.keys())


def build_grayscale_eotf_correction(
    measurements: list[Measurement],
    target_gamma: float = 2.4,
) -> CorrectionFn:
    measured_norm, input_codes = _grayscale_response(measurements)

    def correct(value: float) -> float:
        value = float(np.clip(value, 0.0, 1.0))
        target_y = value**target_gamma
        return float(np.interp(target_y, measured_norm, input_codes))

    return correct


def fit_native_matrix(measurements: list[Measurement]) -> tuple[np.ndarray, np.ndarray]:
    """Fit the display's native RGB-to-XYZ matrix from measured primaries.

    Returns (native, black_xyz). The matrix is black-subtracted, column-scaled
    to reproduce the measured white, and normalized so native @ (1,1,1) has
    Y = 1 relative to the measured white luminance.
    """
    lookup = _patch_xyz_lookup(measurements)
    black = np.array(_lookup_patch_xyz(lookup, (0.0, 0.0, 0.0)), dtype=float)
    white = np.array(_lookup_patch_xyz(lookup, (1.0, 1.0, 1.0)), dtype=float)
    primaries = np.column_stack(
        [
            np.array(_lookup_patch_xyz(lookup, rgb), dtype=float) - black
            for rgb in ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
        ]
    )
    white_net = white - black
    if white_net[1] <= 0.0:
        raise ValueError("White luminance must be greater than black luminance.")

    try:
        scale = np.linalg.solve(primaries, white_net)
    except np.linalg.LinAlgError as exc:
        raise ValueError("Measured primaries are degenerate; cannot fit a color matrix.") from exc
    if np.any(scale <= 0.0):
        raise ValueError("Measured primaries cannot reproduce the measured white point.")

    return (primaries * scale) / white_net[1], black


LinearMap = Callable[[float, float, float], np.ndarray]


def color_target(name: str) -> ColorTarget:
    try:
        return COLOR_TARGETS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown color target {name!r}; expected one of {tuple(COLOR_TARGETS)}.") from exc


def build_target_linear_map(
    measurements: list[Measurement],
    target_gamma: float = 2.4,
    target_name: str = "rec709",
) -> LinearMap:
    """Map input signal RGB to the desired native-basis linear RGB.

    This is the correction before tone-response encoding: linearize with the
    target EOTF, apply the target-to-native matrix, clip to gamut.
    """
    native, _black = fit_native_matrix(measurements)
    target = color_target(target_name)
    target_matrix = _rgb_to_xyz_matrix(target.primaries_xy, target.white_xy)
    correction = np.linalg.solve(native, target_matrix)

    # If reaching D65 at full luminance needs more than the panel can give on
    # some channel, trade peak luminance for an accurate white point.
    white_peak = float(correction.sum(axis=1).max())
    if white_peak > 1.0:
        correction /= white_peak

    def linear_map(r: float, g: float, b: float) -> np.ndarray:
        signal = np.clip([r, g, b], 0.0, 1.0)
        return np.clip(correction @ signal**target_gamma, 0.0, 1.0)

    return linear_map


def build_target_matrix_correction(
    measurements: list[Measurement],
    target_gamma: float = 2.4,
    target_name: str = "rec709",
) -> RGBTransform:
    """Build an RGB transform that corrects both tone response and chromaticity.

    Uses the measured red, green, and blue full-intensity patches plus white and
    black to fit the display's native RGB-to-XYZ matrix, then maps the target
    linear RGB into native linear RGB and encodes through the measured grayscale
    response. Assumes all three channels share the grayscale tone curve, since
    only a gray ramp is measured. Out-of-gamut targets are clipped per channel.
    """
    measured_norm, input_codes = _grayscale_response(measurements)
    linear_map = build_target_linear_map(
        measurements,
        target_gamma=target_gamma,
        target_name=target_name,
    )

    def transform(r: float, g: float, b: float) -> tuple[float, float, float]:
        return tuple(
            float(np.interp(channel, measured_norm, input_codes))
            for channel in linear_map(r, g, b)
        )

    return transform


def build_rec709_linear_map(
    measurements: list[Measurement],
    target_gamma: float = 2.4,
) -> LinearMap:
    return build_target_linear_map(measurements, target_gamma=target_gamma, target_name="rec709")


def build_rec709_matrix_correction(
    measurements: list[Measurement],
    target_gamma: float = 2.4,
) -> RGBTransform:
    return build_target_matrix_correction(
        measurements,
        target_gamma=target_gamma,
        target_name="rec709",
    )


def build_correction_transform(
    measurements: list[Measurement],
    mode: str = "gray",
    target_gamma: float = 2.4,
) -> RGBTransform:
    if mode == "gray":
        return make_neutral_transform(
            build_grayscale_eotf_correction(measurements, target_gamma=target_gamma)
        )
    if mode in COLOR_TARGETS:
        return build_target_matrix_correction(
            measurements,
            target_gamma=target_gamma,
            target_name=mode,
        )
    raise ValueError(f"Unknown correction mode {mode!r}; expected one of {CORRECTION_MODES}.")


def make_neutral_transform(curve: CorrectionFn) -> Callable[[float, float, float], tuple[float, float, float]]:
    def transform(r: float, g: float, b: float) -> tuple[float, float, float]:
        return curve(r), curve(g), curve(b)

    return transform


def _grayscale_response(measurements: list[Measurement]) -> tuple[np.ndarray, np.ndarray]:
    grayscale = sorted(
        (item for item in measurements if _is_grayscale(item)),
        key=lambda item: item.patch.r,
    )
    if len(grayscale) < 3:
        raise ValueError("At least three grayscale measurements are required.")

    input_codes = np.array([item.patch.r for item in grayscale], dtype=float)
    measured_y = np.array([item.xyz[1] for item in grayscale], dtype=float)

    white_y = measured_y[-1]
    black_y = measured_y[0]
    if white_y <= black_y:
        raise ValueError("White luminance must be greater than black luminance.")

    measured_norm = np.maximum.accumulate((measured_y - black_y) / (white_y - black_y))
    measured_norm = np.clip(measured_norm, 0.0, 1.0)
    return _dedupe_response(measured_norm, input_codes)


def _patch_xyz_lookup(
    measurements: list[Measurement],
) -> dict[tuple[float, float, float], tuple[float, float, float]]:
    """Rounded (r, g, b) -> xyz, built once so callers don't rescan per patch.

    Later measurements win on a repeated patch, matching the previous
    reversed-scan lookup (the most recent capture of a given patch name).
    """
    lookup: dict[tuple[float, float, float], tuple[float, float, float]] = {}
    for measurement in measurements:
        patch = measurement.patch
        lookup[_rgb_key((patch.r, patch.g, patch.b))] = measurement.xyz
    return lookup


def _rgb_key(rgb: tuple[float, float, float]) -> tuple[float, float, float]:
    return tuple(round(v, 6) for v in rgb)


def _lookup_patch_xyz(
    lookup: dict[tuple[float, float, float], tuple[float, float, float]],
    rgb: tuple[float, float, float],
) -> tuple[float, float, float]:
    try:
        return lookup[_rgb_key(rgb)]
    except KeyError as exc:
        raise ValueError(
            f"Missing measurement for RGB patch {rgb}; "
            "rec709 correction needs black, white, and full red/green/blue patches."
        ) from exc


def _rgb_to_xyz_matrix(
    primaries_xy: tuple[tuple[float, float], ...],
    white_xy: tuple[float, float],
) -> np.ndarray:
    columns = np.column_stack(
        [np.array([x / y, 1.0, (1.0 - x - y) / y]) for x, y in primaries_xy]
    )
    wx, wy = white_xy
    white = np.array([wx / wy, 1.0, (1.0 - wx - wy) / wy])
    scale = np.linalg.solve(columns, white)
    return columns * scale


def _is_grayscale(measurement: Measurement) -> bool:
    patch = measurement.patch
    return np.isclose(patch.r, patch.g) and np.isclose(patch.g, patch.b)


def _dedupe_response(
    measured_norm: np.ndarray,
    input_codes: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    unique_y: list[float] = []
    unique_codes: list[float] = []

    for measured, code in zip(measured_norm, input_codes, strict=True):
        if unique_y and np.isclose(measured, unique_y[-1]):
            unique_codes[-1] = max(unique_codes[-1], float(code))
            continue
        unique_y.append(float(measured))
        unique_codes.append(float(code))

    return np.array(unique_y, dtype=float), np.array(unique_codes, dtype=float)
