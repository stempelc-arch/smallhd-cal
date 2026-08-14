from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from smallhd_cal.calibration import (
    build_target_linear_map,
    build_target_matrix_correction,
    fit_native_matrix,
)
from smallhd_cal.lut import RGBFn, expand_legal, squeeze_legal, wrap_legal_range
from smallhd_cal.measurement import Measurement

FEED_RANGES = ("legal", "full")


def _drive_for_code(code: float, feed: str) -> float:
    """Panel drive produced by a source code in a no-LUT capture."""
    return squeeze_legal(code) if feed == "legal" else code


def _index_for_signal(signal: float, feed: str) -> float:
    """LUT grid position the monitor looks up for a source signal."""
    return squeeze_legal(signal) if feed == "legal" else signal


def _signal_for_grid(grid: float, feed: str) -> float:
    """Source signal a LUT grid position represents (inverse of the above)."""
    return expand_legal(grid) if feed == "legal" else grid


@dataclass(frozen=True)
class _DriveCurve:
    """Panel drive -> normalized luminance, plus the absolute anchors."""

    drives: np.ndarray
    luminance: np.ndarray
    black_y: float
    white_y: float

    def drive_for_y(self, absolute_y: float) -> float:
        norm = (absolute_y - self.black_y) / (self.white_y - self.black_y)
        return float(np.interp(norm, self.luminance, self.drives))

    def linear_at_drive(self, drive: float) -> float:
        return float(np.interp(drive, self.drives, self.luminance))

    def drive_for_linear(self, linear: float) -> float:
        return float(np.interp(max(0.0, linear), self.luminance, self.drives))


def build_refined_correction(
    baseline: list[Measurement],
    verifies: list[list[Measurement]],
    target_gamma: float = 2.4,
    target_name: str = "rec709",
    color_damping: float = 0.5,
    color_dampings: list[float] | None = None,
) -> RGBFn:
    """Iteratively refine a rec709 correction using verify captures.

    The SmallHD LUT import applies stored values through range and color
    processing that is not reliably predictable across imports. Instead of
    modeling it, fit it from data: each verify capture must have been taken
    with the previous iteration's LUT active (the first verify with the plain
    legal-range-wrapped correction). Per iteration, two empirical maps are
    fitted and inverted:

    - gray ramp: stored LUT value -> achieved panel drive (tone response of
      the whole applied chain);
    - primaries: desired vs achieved native-basis linear RGB, a 3x3 residual
      capturing channel mixing in the monitor's LUT application.

    Baseline and verify captures must share probe placement and monitor state,
    and the import procedure must be identical each time.
    """
    curve = _grayscale_drive_curve(baseline)
    linear_map = build_target_linear_map(
        baseline,
        target_gamma=target_gamma,
        target_name=target_name,
    )
    native, black = fit_native_matrix(baseline)
    white_net_y = _white_net_y(baseline)

    applied: RGBFn = wrap_legal_range(
        build_target_matrix_correction(
            baseline,
            target_gamma=target_gamma,
            target_name=target_name,
        )
    )
    compensation = np.eye(3)

    if color_dampings is not None and len(color_dampings) != len(verifies):
        raise ValueError("color_dampings must have one entry per verify capture.")

    for index, verify in enumerate(verifies):
        stored_pts, drive_pts = _fit_stored_to_drive(verify, applied, curve)
        # The fitted residual is achieved-vs-target for the chain *including*
        # the compensation already applied, so compose rather than replace.
        # Color updates are damped: the monitor's import behavior varies
        # slightly between loads and full-strength corrections ring around
        # the target. Earlier iterations must replay with whatever damping
        # was used when those LUTs were actually generated and loaded, so a
        # per-verify schedule can be given; by default only the newest update
        # is damped (matching a history of full-step builds).
        if color_dampings is not None:
            damping = color_dampings[index]
        else:
            damping = color_damping if index == len(verifies) - 1 else 1.0
        residual = _fit_drive_residual(verify, native, black, white_net_y, linear_map, curve)
        damped = np.eye(3) + damping * (residual - np.eye(3))
        compensation = compensation @ np.linalg.inv(damped)
        applied = _build_transform(linear_map, compensation, curve, stored_pts, drive_pts)

    return applied


REFINE_MODES = ("channel", "matrix")


def refine_step(
    baseline: list[Measurement],
    verify: list[Measurement],
    active_lut: RGBFn,
    active_compensation: np.ndarray,
    target_gamma: float = 2.4,
    target_name: str = "rec709",
    color_damping: float = 0.5,
    feed: str = "legal",
    mode: str = "channel",
) -> tuple[RGBFn, np.ndarray]:
    """One refinement iteration from recorded session state.

    `active_lut` is the LUT that was on the monitor during `verify` — read the
    actual .cube file (see lut.read_smallhd_cube) rather than reconstructing
    it. `active_compensation` is the drive-domain compensation matrix recorded
    when that LUT was generated (identity for the first generated LUT).

    Modes:
    - "channel" (default): fit an independent stored->drive curve per channel
      from every verify patch (each patch's achieved XYZ decomposes into three
      per-channel samples). Stateless and exact for monitors whose LUT
      application is per-channel pointwise — verified on the Cine 7, where the
      per-channel maps are stable across imports while a shared-gray-map +
      global-matrix model diverged.
    - "matrix": the gray-diagonal map plus damped drive-domain 3x3 residual
      used to converge the 1703 PX.

    Returns (new transform, new compensation matrix to record with it —
    identity in channel mode, which carries no cross-iteration state).
    """
    if feed not in FEED_RANGES:
        raise ValueError(f"Unknown feed range {feed!r}; expected one of {FEED_RANGES}.")
    if mode not in REFINE_MODES:
        raise ValueError(f"Unknown refine mode {mode!r}; expected one of {REFINE_MODES}.")
    curve = _grayscale_drive_curve(baseline, feed)
    linear_map = build_target_linear_map(
        baseline,
        target_gamma=target_gamma,
        target_name=target_name,
    )
    native, black = fit_native_matrix(baseline)
    white_net_y = _white_net_y(baseline)

    if mode == "channel":
        channel_maps = _fit_channel_maps(
            verify, active_lut, native, black, white_net_y, curve, feed
        )
        transform = _build_channel_transform(linear_map, curve, channel_maps, feed)
        return transform, np.eye(3)

    stored_pts, drive_pts = _fit_stored_to_drive(verify, active_lut, curve, feed)
    residual = _fit_drive_residual(verify, native, black, white_net_y, linear_map, curve)
    damped = np.eye(3) + color_damping * (residual - np.eye(3))
    compensation = active_compensation @ np.linalg.inv(damped)
    transform = _build_transform(linear_map, compensation, curve, stored_pts, drive_pts, feed)
    return transform, compensation


def _fit_channel_maps(
    verify: list[Measurement],
    applied: RGBFn,
    native: np.ndarray,
    black: np.ndarray,
    white_net_y: float,
    curve: _DriveCurve,
    feed: str,
) -> list[tuple[np.ndarray, np.ndarray]]:
    """Per-channel stored->achieved-drive maps from every verify patch."""
    samples: list[list[tuple[float, float]]] = [[], [], []]
    for m in verify:
        index = tuple(_index_for_signal(v, feed) for v in (m.patch.r, m.patch.g, m.patch.b))
        stored = applied(*index)
        linear = np.linalg.solve(native, (np.array(m.xyz) - black) / white_net_y)
        for channel in range(3):
            drive = curve.drive_for_linear(float(linear[channel]))
            samples[channel].append((float(stored[channel]), drive))

    maps = []
    for channel, pairs in enumerate(samples):
        if len(pairs) < 4:
            raise ValueError("Not enough verify patches to fit per-channel drive maps.")
        pairs.sort()
        stored_pts: list[float] = []
        drive_pts: list[float] = []
        for stored, achieved in pairs:
            if stored_pts and (
                stored <= stored_pts[-1] + 1e-4 or achieved <= drive_pts[-1] + 1e-4
            ):
                continue
            stored_pts.append(stored)
            drive_pts.append(achieved)
        if len(stored_pts) < 3:
            raise ValueError(
                f"Channel {channel} verify samples are not monotonic enough to fit a map."
            )
        maps.append((np.array(stored_pts), np.array(drive_pts)))
    return maps


def _build_channel_transform(
    linear_map,
    curve: _DriveCurve,
    channel_maps: list[tuple[np.ndarray, np.ndarray]],
    feed: str,
) -> RGBFn:
    if feed == "legal":
        stored_min, stored_max = squeeze_legal(0.0), squeeze_legal(1.0)
    else:
        stored_min, stored_max = 0.0, 1.0

    # Per-channel achievable drive ceilings; scale the target in linear light
    # so the hottest white channel just fits (protects the white point).
    ceilings = [
        min(_interp_extrap(stored_max, s_pts, d_pts), float(curve.drives[-1]))
        for s_pts, d_pts in channel_maps
    ]
    white_linear = linear_map(1.0, 1.0, 1.0)
    headroom = 1.0
    for channel in range(3):
        limit = curve.linear_at_drive(ceilings[channel])
        needed = float(white_linear[channel])
        if needed > 0 and limit / needed < headroom:
            headroom = limit / needed

    def transform(r: float, g: float, b: float) -> tuple[float, float, float]:
        signal = tuple(_signal_for_grid(v, feed) for v in (r, g, b))
        desired = linear_map(*signal) * headroom
        stored = []
        for channel in range(3):
            drive = curve.drive_for_linear(float(desired[channel]))
            s_pts, d_pts = channel_maps[channel]
            stored.append(
                float(np.clip(_interp_extrap(drive, d_pts, s_pts), stored_min, stored_max))
            )
        return tuple(stored)

    return transform


def _build_transform(
    linear_map,
    compensation: np.ndarray,
    curve: _DriveCurve,
    stored_pts: np.ndarray,
    drive_pts: np.ndarray,
    feed: str = "legal",
) -> RGBFn:
    """Compose: target linear -> drive -> drive-domain compensation -> stored."""
    if feed == "legal":
        stored_min, stored_max = squeeze_legal(0.0), squeeze_legal(1.0)
    else:
        stored_min, stored_max = 0.0, 1.0
    ceiling_drive = min(_interp_extrap(stored_max, stored_pts, drive_pts), float(curve.drives[-1]))

    def drives_for(linear: np.ndarray, scale: float) -> np.ndarray:
        return compensation @ np.array(
            [curve.drive_for_linear(float(v) * scale) for v in linear]
        )

    # A hard clamp at the legal cap would clip channels unevenly and tint
    # white; instead scale the target in linear light so the hottest white
    # channel just fits under the cap.
    white_linear = linear_map(1.0, 1.0, 1.0)
    headroom = 1.0
    if float(np.max(drives_for(white_linear, 1.0))) > ceiling_drive:
        # low=0.0 (not 0.5): on a panel gamut-limited enough to need headroom
        # below 0.5 to hit ceiling_drive, a bracket starting at 0.5 never gets
        # re-validated and the loop just drifts high toward 0.5 without ever
        # confirming it satisfies the constraint — silently under-protecting
        # white. scale=0 always satisfies it (drive_for_linear(0) is the
        # curve's minimum), so 0.0 is always a valid lower bound to start from.
        low, high = 0.0, 1.0
        for _ in range(30):
            mid = (low + high) / 2.0
            if float(np.max(drives_for(white_linear, mid))) > ceiling_drive:
                high = mid
            else:
                low = mid
        headroom = low

    def transform(r: float, g: float, b: float) -> tuple[float, float, float]:
        signal = tuple(_signal_for_grid(v, feed) for v in (r, g, b))
        drives = drives_for(linear_map(*signal), headroom)
        return tuple(
            float(np.clip(_interp_extrap(float(d), drive_pts, stored_pts), stored_min, stored_max))
            for d in drives
        )

    return transform


def _grayscale_drive_curve(baseline: list[Measurement], feed: str = "legal") -> _DriveCurve:
    grays = sorted((m for m in baseline if _is_gray(m)), key=lambda m: m.patch.r)
    if len(grays) < 3:
        raise ValueError("At least three grayscale baseline measurements are required.")
    black_y = grays[0].xyz[1]
    white_y = grays[-1].xyz[1]
    if white_y <= black_y:
        raise ValueError("White luminance must be greater than black luminance.")
    drives = np.array([_drive_for_code(m.patch.r, feed) for m in grays])
    luminance = np.maximum.accumulate(
        np.array([(m.xyz[1] - black_y) / (white_y - black_y) for m in grays])
    )
    return _DriveCurve(drives=drives, luminance=luminance, black_y=black_y, white_y=white_y)


def _fit_stored_to_drive(
    verify: list[Measurement],
    applied: RGBFn,
    curve: _DriveCurve,
    feed: str = "legal",
) -> tuple[np.ndarray, np.ndarray]:
    pairs: list[tuple[float, float]] = []
    for m in sorted((m for m in verify if _is_gray(m)), key=lambda m: m.patch.r):
        index = _index_for_signal(m.patch.r, feed)
        stored = float(np.mean(applied(index, index, index)))
        achieved = curve.drive_for_y(m.xyz[1])
        pairs.append((stored, achieved))
    if len(pairs) < 3:
        raise ValueError("At least three grayscale verify measurements are required.")

    pairs.sort()
    stored_pts: list[float] = []
    drive_pts: list[float] = []
    for stored, achieved in pairs:
        # Same 1e-4 minimum-gap guard as _fit_channel_maps: near-duplicate
        # points make _interp_extrap's extrapolation slope (last two points)
        # blow up on a near-zero denominator, which then poisons ceiling_drive
        # and the headroom/white-protection logic built on top of it.
        if stored_pts and (
            stored <= stored_pts[-1] + 1e-4 or achieved <= drive_pts[-1] + 1e-4
        ):
            continue
        stored_pts.append(stored)
        drive_pts.append(achieved)
    if len(stored_pts) < 3:
        raise ValueError("Verify measurements are not monotonic enough to fit a drive map.")
    return np.array(stored_pts), np.array(drive_pts)


def _fit_drive_residual(
    verify: list[Measurement],
    native: np.ndarray,
    black: np.ndarray,
    white_net_y: float,
    linear_map,
    curve: _DriveCurve,
) -> np.ndarray:
    """Fit achieved ~= residual @ desired in the panel-drive domain.

    LUT application artifacts (interpolation, precision, channel smear) act on
    code values, so the residual matrix is fitted on drives rather than linear
    light. White is weighted heavily so the fit never trades white accuracy
    for primary accuracy; secondaries stabilize the fit across the gamut.
    """
    by_rgb = {}
    for m in verify:
        by_rgb[(round(m.patch.r, 4), round(m.patch.g, 4), round(m.patch.b, 4))] = m

    required = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))
    weighted = [(rgb, 1.0) for rgb in required]
    weighted += [((1.0, 1.0, 0.0), 1.0), ((0.0, 1.0, 1.0), 1.0), ((1.0, 0.0, 1.0), 1.0)]
    weighted += [((1.0, 1.0, 1.0), 4.0)]

    achieved_cols = []
    desired_cols = []
    for rgb, weight in weighted:
        m = by_rgb.get(rgb)
        if m is None:
            if rgb in required:
                raise ValueError(
                    "Verify capture must include full red/green/blue patches for color refinement."
                )
            continue
        xyz = (np.array(m.xyz) - black) / white_net_y
        achieved_linear = np.linalg.solve(native, xyz)
        achieved_cols.append(
            np.array([curve.drive_for_linear(float(v)) for v in achieved_linear]) * weight
        )
        desired_cols.append(
            np.array([curve.drive_for_linear(float(v)) for v in linear_map(*rgb)]) * weight
        )

    achieved = np.column_stack(achieved_cols)
    desired = np.column_stack(desired_cols)
    residual_t, _res, rank, _sv = np.linalg.lstsq(desired.T, achieved.T, rcond=None)
    if rank < 3:
        raise ValueError("Desired color targets are degenerate; cannot fit residual.")
    residual = residual_t.T
    # Grays are owned by the pointwise stored->drive map; normalize rows so the
    # matrix is gray-neutral and cannot fight it (row-sum-1 matrices are closed
    # under multiplication and inversion, so the composed compensation stays
    # gray-neutral too).
    row_sums = residual.sum(axis=1, keepdims=True)
    if np.any(row_sums <= 0.0):
        raise ValueError("Fitted color residual is not physical (non-positive row sums).")
    residual = residual / row_sums
    if np.linalg.det(residual) <= 0.0:
        raise ValueError("Fitted color residual is not invertible in a physical way.")
    return residual


def _white_net_y(baseline: list[Measurement]) -> float:
    by_rgb = {}
    for m in baseline:
        by_rgb[(round(m.patch.r, 4), round(m.patch.g, 4), round(m.patch.b, 4))] = m
    white = by_rgb[(1.0, 1.0, 1.0)]
    black = by_rgb[(0.0, 0.0, 0.0)]
    return white.xyz[1] - black.xyz[1]


def _interp_extrap(x: float, xs: np.ndarray, ys: np.ndarray) -> float:
    """np.interp with linear extrapolation past both ends."""
    if x < xs[0]:
        slope = (ys[1] - ys[0]) / (xs[1] - xs[0])
        return float(ys[0] + (x - xs[0]) * slope)
    if x > xs[-1]:
        slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
        return float(ys[-1] + (x - xs[-1]) * slope)
    return float(np.interp(x, xs, ys))


def _is_gray(measurement: Measurement) -> bool:
    patch = measurement.patch
    return np.isclose(patch.r, patch.g) and np.isclose(patch.g, patch.b)
