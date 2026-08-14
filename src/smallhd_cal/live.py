"""Live per-point closed-loop calibration.

Instead of building a LUT blind, loading it via SD card, measuring, and refining
(which fails on monitors that mangle each LUT differently on import), this drives
the calibration by adjusting the *signal the Mac displays* while a fixed identity
LUT stays loaded. For each target color it shows a patch, measures it, nudges the
signal, and re-measures until the output lands on target — then records the signal
that achieved it. Those converged signals ARE the correction LUT: load it once.

Everything here takes an injected ``measure(r, g, b) -> XYZ`` callback, so the
convergence and LUT construction are unit-tested against a synthetic panel with
no hardware. The GUI supplies a real callback that shows a patch and reads the
probe.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from smallhd_cal.calibration import (
    _grayscale_response,
    _rgb_to_xyz_matrix,
    color_target,
    fit_native_matrix,
)
from smallhd_cal.lut import RGBFn, squeeze_legal
from smallhd_cal.measurement import Measurement

MeasureFn = Callable[[float, float, float], tuple[float, float, float]]


@dataclass(frozen=True)
class PanelModel:
    """Local model of the panel used to turn a measured error into a signal step.

    Built from the identity-LUT baseline: the native RGB->XYZ matrix, the black
    XYZ, and the grayscale signal<->linear tone curve. It only needs to be
    approximately right — the closed loop corrects any model error via feedback.
    """

    native: np.ndarray
    black: np.ndarray
    white_net_y: float
    signal_codes: np.ndarray  # gray input codes, ascending
    linear_levels: np.ndarray  # normalized linear luminance at each code

    @classmethod
    def from_baseline(cls, baseline: list[Measurement]) -> PanelModel:
        native, black = fit_native_matrix(baseline)
        measured_norm, input_codes = _grayscale_response(baseline)
        white = next(m for m in baseline if m.patch.r == m.patch.g == m.patch.b == 1.0)
        black_m = next(m for m in baseline if m.patch.r == m.patch.g == m.patch.b == 0.0)
        white_net_y = white.xyz[1] - black_m.xyz[1]
        return cls(
            native=np.asarray(native, dtype=float),
            black=np.asarray(black, dtype=float),
            white_net_y=float(white_net_y),
            signal_codes=np.asarray(input_codes, dtype=float),
            linear_levels=np.asarray(measured_norm, dtype=float),
        )

    def xyz_to_native_linear(self, xyz) -> np.ndarray:
        """Solve for native-basis linear RGB (white-normalized) from XYZ."""
        rel = (np.asarray(xyz, dtype=float) - self.black) / self.white_net_y
        return np.linalg.solve(self.native, rel)

    def signal_for_linear(self, linear) -> np.ndarray:
        """Per-channel: the input signal that produces a normalized linear level."""
        lin = np.atleast_1d(np.asarray(linear, dtype=float))
        return np.array([
            float(np.interp(np.clip(v, 0.0, 1.0), self.linear_levels, self.signal_codes))
            for v in lin
        ])


@dataclass
class ConvergeResult:
    signal: tuple[float, float, float]
    achieved_xyz: tuple[float, float, float]
    iterations: int
    residual: float  # max abs native-linear error at the end


def converge_color(
    measure: MeasureFn,
    target_xyz,
    model: PanelModel,
    *,
    gain: float = 0.9,
    max_iters: int = 8,
    tol: float = 0.0015,
    start: tuple[float, float, float] | None = None,
) -> ConvergeResult:
    """Find the signal whose measured output lands on ``target_xyz``.

    Newton-ish fixed point in signal space: convert the measured vs target error
    to a per-channel signal step through the tone curve, damped by ``gain``.
    """
    target_lin = model.xyz_to_native_linear(target_xyz)
    want_signal = model.signal_for_linear(target_lin)
    signal = np.array(start if start is not None else want_signal, dtype=float)
    signal = np.clip(signal, 0.0, 1.0)

    achieved = np.zeros(3)
    residual = float("inf")
    used = 0
    for used in range(1, max_iters + 1):
        achieved = np.asarray(measure(*signal), dtype=float)
        meas_lin = model.xyz_to_native_linear(achieved)
        residual = float(np.max(np.abs(target_lin - meas_lin)))
        if residual <= tol:
            break
        got_signal = model.signal_for_linear(meas_lin)
        signal = np.clip(signal + gain * (want_signal - got_signal), 0.0, 1.0)

    return ConvergeResult(
        signal=tuple(float(v) for v in signal),
        achieved_xyz=tuple(float(v) for v in achieved),
        iterations=used,
        residual=residual,
    )


@dataclass
class LiveCharacterization:
    """Converged signals for the gray ramp and color patches, plus the model."""

    model: PanelModel
    gray_codes: np.ndarray  # target input levels
    gray_signals: np.ndarray  # (N,3) signal that produced neutral gray at each level
    target_name: str
    target_gamma: float
    results: list[ConvergeResult] = field(default_factory=list)
    patch_results: list[LivePatchResult] = field(default_factory=list)


@dataclass(frozen=True)
class LivePatchResult:
    """A target RGB patch and the measured signal that reproduced it."""

    target_rgb: tuple[float, float, float]
    target_xyz: tuple[float, float, float]
    result: ConvergeResult


def _target_white_xy(target_name: str) -> tuple[float, float]:
    return color_target(target_name).white_xy


def target_xyz_for_rgb(
    rgb: tuple[float, float, float],
    model: PanelModel,
    *,
    target_name: str = "rec709",
    target_gamma: float = 2.4,
) -> tuple[float, float, float]:
    """Absolute XYZ target for a normalized target-space RGB signal.

    The requested color is first mapped into the panel's native linear basis and
    peak-scaled if needed, matching the eventual LUT transform. That keeps live
    convergence from chasing impossible out-of-gamut XYZ targets.
    """
    target = color_target(target_name)
    target_matrix = _rgb_to_xyz_matrix(target.primaries_xy, target.white_xy)
    correction = np.linalg.solve(model.native, target_matrix)
    white_peak = float(correction.sum(axis=1).max())
    if white_peak > 1.0:
        correction /= white_peak
    native_linear = np.clip(correction @ np.clip(rgb, 0.0, 1.0) ** target_gamma, 0.0, 1.0)
    xyz = model.black + (model.native @ native_linear) * model.white_net_y
    return tuple(float(v) for v in xyz)


def characterize_patch_set(
    measure: MeasureFn,
    model: PanelModel,
    patches: list[tuple[float, float, float]],
    *,
    target_name: str = "rec709",
    target_gamma: float = 2.4,
    on_point: Callable[[int, int], None] | None = None,
    **converge_kwargs,
) -> list[LivePatchResult]:
    """Converge arbitrary target-space RGB patches.

    Used for primaries/secondaries while the monitor stays in one fixed
    identity-LUT state; ``build_live_correction`` fits the color matrix from
    these converged signals so the color mixing is measured, not modeled.
    Each patch starts from the model's own prediction (distinct colors are far
    apart, so chaining from the previous patch would start the loop far off).
    """
    results = []
    total = len(patches)
    for index, patch in enumerate(patches, start=1):
        target_xyz = target_xyz_for_rgb(
            patch,
            model,
            target_name=target_name,
            target_gamma=target_gamma,
        )
        res = converge_color(measure, target_xyz, model, **converge_kwargs)
        results.append(LivePatchResult(tuple(float(v) for v in patch), target_xyz, res))
        if on_point is not None:
            on_point(index, total)
    return results


def characterize_gray_ramp(
    measure: MeasureFn,
    model: PanelModel,
    *,
    target_name: str = "rec709",
    target_gamma: float = 2.4,
    levels: np.ndarray | None = None,
    on_point: Callable[[int, int], None] | None = None,
    **converge_kwargs,
) -> LiveCharacterization:
    """Converge each gray level to the target white at gamma-correct luminance.

    ``on_point(done, total)`` is called after each converged level so a UI can
    show progress across the (unattended) sweep.
    """
    if levels is None:
        levels = np.linspace(0.0, 1.0, 17)

    # Match converge_color's own default so "converged" here means the same
    # thing it does there.
    tol = converge_kwargs.get("tol", 0.0015)

    signals = []
    results = []
    prev = None
    total = len(levels)
    for index, level in enumerate(levels, start=1):
        if float(level) <= 0.0:
            # Pure black: the signal can't go below 0 and a colorimeter can't
            # integrate ~0 nits (spotread hangs / times out), so don't measure
            # it — map black input straight to black output.
            res = ConvergeResult(
                signal=(0.0, 0.0, 0.0),
                achieved_xyz=tuple(float(v) for v in model.black),
                iterations=0,
                residual=0.0,
            )
        else:
            # Target through the same peak-scaled correction the LUT builder uses,
            # so the neutral white at the top of the ramp is REACHABLE: on a
            # greenish panel, hitting D65 means pulling green down, so full-
            # luminance neutral white is off-gamut and signals would clip at 1.0.
            target_xyz = target_xyz_for_rgb(
                (level, level, level), model, target_name=target_name, target_gamma=target_gamma
            )
            res = converge_color(measure, target_xyz, model, start=prev, **converge_kwargs)
            # Only chain from a level that actually converged — seeding the next
            # level's start point from a noisy/occluded read that never settled
            # would compound the drift up the whole rest of the ramp instead of
            # letting each level re-derive independently from the model's guess.
            if res.residual <= tol:
                prev = res.signal
        signals.append(res.signal)
        results.append(res)
        if on_point is not None:
            on_point(index, total)
    return LiveCharacterization(
        model=model,
        gray_codes=np.asarray(levels, dtype=float),
        gray_signals=np.asarray(signals, dtype=float),
        target_name=target_name,
        target_gamma=target_gamma,
        results=results,
    )


def _signal_to_native_linear(
    signal: np.ndarray,
    gray_native_linear: np.ndarray,
    channel_signal: np.ndarray,
) -> np.ndarray:
    """Invert the live per-channel gray maps: converged signal -> native-linear drive."""
    out = np.empty(3)
    for c in range(3):
        order = np.argsort(channel_signal[:, c])
        out[c] = float(
            np.interp(
                np.clip(signal[c], 0.0, 1.0),
                channel_signal[order, c],
                gray_native_linear[order, c],
            )
        )
    return out


# How much harder the fit tries to satisfy measured primaries vs secondaries.
PRIMARY_FIT_WEIGHT = 6.0


def _fit_measured_correction(
    patch_results: list[LivePatchResult],
    correction: np.ndarray,
    gray_native_linear: np.ndarray,
    channel_signal: np.ndarray,
    gamma: float,
) -> np.ndarray:
    """Refit the color matrix from live-converged color patches.

    Each converged patch is a measured fact: to land target-linear input ``v``
    (= rgb**gamma) on its true target chromaticity, the panel needed native
    drive ``n(signal)`` — so the matrix must satisfy ``M @ v = n``. Solve per
    row by least squares over all measured patches, then renormalize each row
    so ``M @ (1,1,1)`` still equals the gray ramp's white drive: the primaries
    become measured while the measured neutral axis stays untouched (an
    unconstrained fit that fights the gray map diverges — refine.py lesson).
    Primaries are weighted well above secondaries: one matrix can't satisfy
    both on a panel with channel interaction, and the primaries are what a
    verify (and the accuracy score) measures — secondaries only tug the fit
    where it costs the primaries almost nothing.
    Falls back to the model matrix if the fit is degenerate or wildly off
    (a botched measurement must not poison the whole LUT).
    """
    inputs = np.array([np.clip(item.target_rgb, 0.0, 1.0) ** gamma for item in patch_results])
    drives = np.array([
        _signal_to_native_linear(
            np.asarray(item.result.signal, dtype=float), gray_native_linear, channel_signal
        )
        for item in patch_results
    ])
    if len(patch_results) < 3 or np.linalg.matrix_rank(inputs) < 3:
        return correction
    weights = np.sqrt([
        PRIMARY_FIT_WEIGHT if np.count_nonzero(np.asarray(item.target_rgb) > 0.0) == 1 else 1.0
        for item in patch_results
    ])[:, None]
    fitted = np.linalg.lstsq(inputs * weights, drives * weights, rcond=None)[0].T
    row_sums = fitted @ np.ones(3)
    if np.any(row_sums <= 1e-6) or float(np.max(np.abs(fitted - correction))) > 0.25:
        return correction
    white_drive = correction @ np.ones(3)
    return fitted * (white_drive / row_sums)[:, None]


def build_live_correction(
    characterization: LiveCharacterization,
    *,
    feed: str = "legal",
) -> RGBFn:
    """Build the correction LUT transform from a live characterization.

    The per-channel tone + white balance comes from the LIVE-converged gray
    ramp (its per-channel signal at each luminance). The color mixing (target
    primaries -> native basis) comes from the LIVE-converged color patches
    when present (measured primaries/secondaries, gray-neutral row-normalized),
    else from the model matrix — so with a full sweep, nothing in the LUT is
    an inverted guess.
    """
    model = characterization.model
    target = color_target(characterization.target_name)
    gamma = characterization.target_gamma

    target_matrix = _rgb_to_xyz_matrix(target.primaries_xy, target.white_xy)
    correction = np.linalg.solve(model.native, target_matrix)
    white_peak = float(correction.sum(axis=1).max())
    if white_peak > 1.0:
        correction /= white_peak

    # Per-channel LIVE maps: desired native-linear at each gray level -> signal.
    # The x-axis is channel-specific: a neutral D65 gray on a greenish/wide-gamut
    # native panel may need different native-linear red, green, and blue amounts.
    gray_linear = characterization.gray_codes.astype(float) ** gamma
    gray_native_linear = np.array(
        [np.clip(correction @ np.array([v, v, v], dtype=float), 0.0, 1.0) for v in gray_linear]
    )
    channel_signal = characterization.gray_signals  # (N,3)
    order = np.argsort(gray_linear)
    gray_native_linear = gray_native_linear[order]
    channel_signal = channel_signal[order]

    if characterization.patch_results:
        # The row normalization inside preserves correction @ (1,1,1), which is
        # exactly the gray maps' x-axis at the top — neutrals keep reproducing
        # the measured gray signals bit-for-bit.
        correction = _fit_measured_correction(
            characterization.patch_results, correction, gray_native_linear, channel_signal, gamma
        )

    def per_channel_signal(linear: np.ndarray) -> np.ndarray:
        return np.array([
            float(
                np.interp(
                    np.clip(linear[c], 0.0, 1.0),
                    gray_native_linear[:, c],
                    channel_signal[:, c],
                )
            )
            for c in range(3)
        ])

    def transform(r: float, g: float, b: float) -> tuple[float, float, float]:
        signal = np.clip([r, g, b], 0.0, 1.0)
        desired_native = np.clip(correction @ signal**gamma, 0.0, 1.0)
        out = per_channel_signal(desired_native)
        if feed == "legal":
            out = np.array([squeeze_legal(float(v)) for v in out])
        return tuple(float(v) for v in np.clip(out, 0.0, 1.0))

    return transform
