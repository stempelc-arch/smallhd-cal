"""Pure, UI-agnostic reporting and workflow logic for a calibration session.

The GUI is a thin shell over these helpers so the interesting logic stays
testable without a display: convergence-table rows, the "what next" hint,
readiness warnings, capture output paths, and export naming. The math here
mirrors the session CLI (tools/calibrate_session.py) so both front-ends agree.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from smallhd_cal.analysis import xyz_to_xy
from smallhd_cal.calibration import color_target
from smallhd_cal.measurement import read_measurements_json
from smallhd_cal.presets import CalibrationPreset
from smallhd_cal.session import (
    CHAIN_STATE_REQUIRED_FIELDS,
    CalibrationSession,
    SessionIteration,
)

PRIMARY_NAMES = ("white", "red", "green", "blue")

# Marker the software signal-space refine loop leaves in an iteration's notes.
# Those captures measure our cube applied by US on an identity monitor: real
# probe data, but blind to what the monitor's importer does to the cube.
SOFTWARE_VERIFY_MARKER = "software signal-space verify"


@dataclass(frozen=True)
class IterationRow:
    """One row of the convergence table for the GUI."""

    label: str
    index: int
    is_recheck: bool
    is_selected: bool
    has_verify: bool
    # None when there is no verify capture or it could not be read.
    white_err: float | None = None
    red_err: float | None = None
    green_err: float | None = None
    blue_err: float | None = None
    gray50_dev: float | None = None
    # Mean CIEDE2000 over the whole verify patch set (grays, skin, mixes — not
    # just the primaries). None when the capture is the short 11-patch set or
    # could not be scored. This is the perceptual metric; the primary-only
    # errors above can improve while this gets worse (RX firmware ringing).
    mean_de2000: float | None = None
    # True when the numbers come from the software signal-space loop (the cube
    # applied by us on an identity monitor) rather than the installed LUT.
    is_software: bool = False

    @property
    def errors(self) -> tuple[float | None, float | None, float | None, float | None]:
        return (self.white_err, self.red_err, self.green_err, self.blue_err)


def _resolve(root: Path, stored: str) -> Path:
    path = Path(stored)
    return path if path.is_absolute() else root / path


def target_chromaticities(session: CalibrationSession) -> dict[str, tuple[float, float]]:
    target = color_target(session.target_name)
    chromaticities = dict(zip(("red", "green", "blue"), target.primaries_xy, strict=True))
    chromaticities["white"] = target.white_xy
    return chromaticities


def measurement_row(
    measurements_path: str | Path,
    targets: dict[str, tuple[float, float]],
    target_gamma: float,
) -> tuple[dict[str, float], float | None] | None:
    """Return (primary xy errors, gray50 deviation) for one capture.

    Returns None when the capture is missing or does not contain the primary
    and grayscale patches this report needs. gray50 deviation is None when no
    mid-gray patch is present.
    """
    measurements = read_measurements_json(measurements_path)
    if not measurements:
        return None
    by_name = {m.patch.name: m for m in measurements}
    try:
        errors = {}
        for name in PRIMARY_NAMES:
            x, y = xyz_to_xy(by_name[name].xyz)
            tx, ty = targets[name]
            errors[name] = math.hypot(x - tx, y - ty)
        white_y = by_name["white"].xyz[1]
        black_y = by_name["black"].xyz[1]
    except KeyError:
        return None

    gray = next(
        (
            m
            for m in by_name.values()
            if m.patch.name.startswith("gray_") and 0.49 < m.patch.r < 0.51
        ),
        None,
    )
    if gray is None or white_y <= black_y:
        gray_dev: float | None = None
    else:
        measured = (gray.xyz[1] - black_y) / (white_y - black_y)
        gray_dev = measured - gray.patch.r**target_gamma
    return errors, gray_dev


def iteration_rows(session: CalibrationSession, root: str | Path = ".") -> list[IterationRow]:
    """Build the convergence table, one row per verify (and per recheck)."""
    root = Path(root)
    targets = target_chromaticities(session)
    selected_index = session.selected_iteration_index
    rows: list[IterationRow] = []

    def build(label: str, iteration: SessionIteration, verify_path: str, recheck: bool) -> IterationRow:
        resolved = _resolve(root, verify_path)
        result = measurement_row(resolved, targets, session.target_gamma)
        is_selected = iteration.index == selected_index
        # Rechecks are always hardware captures; an iteration's own verify is a
        # software prediction only while its notes still carry the marker.
        software = not recheck and SOFTWARE_VERIFY_MARKER in (iteration.notes or "")
        de = _mean_de2000(resolved, session)
        if result is None:
            return IterationRow(label, iteration.index, recheck, is_selected, has_verify=True,
                                mean_de2000=de, is_software=software)
        errors, gray_dev = result
        return IterationRow(
            label,
            iteration.index,
            recheck,
            is_selected,
            has_verify=True,
            white_err=errors["white"],
            red_err=errors["red"],
            green_err=errors["green"],
            blue_err=errors["blue"],
            gray50_dev=gray_dev,
            mean_de2000=de,
            is_software=software,
        )

    for iteration in session.iterations:
        if iteration.verify_path is None:
            rows.append(
                IterationRow(
                    str(iteration.index),
                    iteration.index,
                    is_recheck=False,
                    is_selected=iteration.index == selected_index,
                    has_verify=False,
                )
            )
        else:
            rows.append(build(str(iteration.index), iteration, iteration.verify_path, recheck=False))
        for recheck_index, recheck_path in enumerate(iteration.verify_rechecks, start=1):
            label = f"{iteration.index}r{recheck_index}"
            rows.append(build(label, iteration, recheck_path, recheck=True))
    return rows


# Level values the operator types into the monitor's own wizard (min black /
# max white luminance, in cd/m²). Stored in the session's chain_state so they
# persist and are entered identically on every LUT upload.
WHITE_LEVEL_KEY = "white_level_nits"
BLACK_LEVEL_KEY = "black_level_nits"


def levels_from_capture(measurements) -> tuple[float, float] | None:
    """(white_y, black_y) in cd/m² from a capture, if both patches are present."""
    by_name = {m.patch.name: m for m in measurements}
    if "white" in by_name and "black" in by_name:
        return by_name["white"].xyz[1], by_name["black"].xyz[1]
    return None


def saved_levels(session: CalibrationSession) -> tuple[str | None, str | None]:
    """(white/max, black/min) nit strings saved for the monitor's level entry."""
    return session.chain_state.get(WHITE_LEVEL_KEY), session.chain_state.get(BLACK_LEVEL_KEY)


# A matched legal feed reads baseline black around 0.16 nits (~700-1000:1 on the
# Cine 7); a monitor declared FULL against the Mac's legal feed lifts black to
# ~0.75-1 nit (~100-140:1). 300:1 splits those two populations cleanly.
FEED_MISMATCH_MIN_CONTRAST = 300.0


# A raw wide-gamut Cine 7/1703 panel baselines with green ~0.12-0.14 from the
# Rec.709 point; a green already inside this radius means some correction is
# active, so the characterization would ride on top of it (rx home test 3
# stalled at 87% exactly this way — the previous session's LUT was still live).
BASELINE_CORRECTED_GREEN_ERR = 0.06


def baseline_identity_warning(measurements) -> str | None:
    """A warning when the baseline looks already-corrected, not native."""
    green = next(
        (m for m in measurements if getattr(m.patch, "name", None) == "green"), None
    )
    if green is None:
        return None
    x_sum = sum(green.xyz)
    if x_sum <= 0:
        return None
    x, y = green.xyz[0] / x_sum, green.xyz[1] / x_sum
    err = math.hypot(x - 0.300, y - 0.600)
    if err >= BASELINE_CORRECTED_GREEN_ERR:
        return None
    return (
        f"The baseline's green patch is only {err:.3f} from the Rec.709 target — "
        "a raw wide-gamut panel reads ~0.12+. A correction LUT from an earlier "
        "session is probably still active on the monitor. Characterizing through "
        "it produces a LUT that only works stacked on the old one. Activate the "
        "identity calibration, then re-run the baseline."
    )


def feed_range_warning(measurements, *, min_contrast: float = FEED_MISMATCH_MIN_CONTRAST) -> str | None:
    """A warning when a capture's black level looks like an input-range mismatch.

    Meant to run on the baseline BEFORE a long live sweep: a lifted black means
    the monitor's declared input range doesn't match the actual feed, and every
    measurement taken in that state is junk.
    """
    levels = levels_from_capture(measurements)
    if levels is None:
        return None
    white_y, black_y = levels
    if black_y <= 0 or white_y <= black_y:
        return None
    contrast = white_y / black_y
    if contrast >= min_contrast:
        return None
    return (
        f"Black measured {black_y:.2f} nits against {white_y:.0f} nits white "
        f"(contrast ~{contrast:.0f}:1) — something in the chain is lifting black, "
        "and every measurement taken in this state is junk. Usual causes, in "
        "order: the monitor's Color Pipe RANGE is misreading the feed (the Mac "
        "sends video-levels over HDMI — set the pipe's Range to Legal, or Auto "
        "if Legal looks wrong; explicit Full lifts black on this feed), the "
        "identity calibration isn't the active one, or Display Luminance / "
        "backlight state changed. Fix, then re-run the baseline — a healthy "
        "capture reads 700:1 or better."
    )


# Healthy hardware sweeps land max_residual ~0.02 (test 7 v1: 0.0215); the
# pathological top-clip run read 0.118+. Warn only clearly above the healthy band.
LIVE_RESIDUAL_WARN = 0.03


def live_health_warning(notes: str, *, threshold: float = LIVE_RESIDUAL_WARN) -> str | None:
    """A warning when a live sweep's convergence health metric looks poor."""
    match = re.search(r"max_residual=([0-9.]+)", notes or "")
    if not match:
        return None
    residual = float(match.group(1))
    if residual <= threshold:
        return None
    return (
        f"Some colors did not fully converge (worst residual {residual:.3f}). "
        "The LUT is still usable, but check probe placement and monitor state, "
        "and consider re-running the sweep before trusting a verify."
    )


# Accuracy %: white-weighted mean chromaticity error vs the target primaries,
# mapped so 0 error = 100% and a mean xy error of ACCURACY_ANCHOR reads 0%.
# White is weighted double because the white point dominates perceived accuracy.
ACCURACY_ANCHOR = 0.10


def accuracy_percent(row: IterationRow) -> float | None:
    """A 0-100 accuracy score for a verified LUT, from its primary xy errors."""
    if None in (row.white_err, row.red_err, row.green_err, row.blue_err):
        return None
    weighted = (2 * row.white_err + row.red_err + row.green_err + row.blue_err) / 5
    return max(0.0, min(100.0, 100.0 * (1 - weighted / ACCURACY_ANCHOR)))


def accuracy_label(percent: float) -> str:
    if percent >= 93:
        return "excellent"
    if percent >= 85:
        return "very good"
    if percent >= 75:
        return "good"
    if percent >= 60:
        return "fair"
    return "needs work"


def _mean_de2000(verify_path: Path, session: CalibrationSession) -> float | None:
    """Mean CIEDE2000 over a verify capture, or None if it can't be scored.

    Only meaningful for the extended (30-patch) verify; the short 11-patch
    baseline set has no interior colors, so its dE says little about grays/skin.
    """
    from smallhd_cal.analysis import verify_delta_e_report

    try:
        measurements = read_measurements_json(verify_path)
    except (OSError, ValueError, KeyError):
        return None
    if len(measurements) < 20:
        return None
    try:
        return verify_delta_e_report(
            measurements, target_name=session.target_name, target_gamma=session.target_gamma
        ).average
    except (ValueError, KeyError):
        return None


SHIP_DE2000 = 2.0  # a single hardware verify at/under this is broadcast-grade


def is_shippable(verify_path: Path, session: CalibrationSession) -> bool:
    """True when one hardware verify is already good enough to stop refining.

    On a near-verbatim monitor (e.g. the Cine 7 on PageOS 5.5.6) the first
    hardware verify already lands broadcast-grade, so auto-refining builds an
    extra cube that just gets rejected. A monitor that still measures poorly
    (the RX's reshaping firmware) stays above the threshold and keeps refining.
    """
    de = _mean_de2000(Path(verify_path), session)
    return de is not None and de <= SHIP_DE2000


def iteration_score(row: IterationRow) -> float | None:
    """A single scalar for ranking iterations. Lower is better.

    Prefers the perceptual mean CIEDE2000 (grays, skin, and mixes, not just the
    gamut corners) whenever it's available: on firmware that reshapes an
    imported cube, the primary-only error can drop while mid-tones and skin
    blow up (RX v3→v5: primaries 79→87% but dE2000 2.2→6.0). Falls back to the
    white-plus-primary error only when no full capture exists (the short
    baseline set), which still reproduces the Cine 7 / 1703 keeper choices.
    """
    if row.mean_de2000 is not None:
        return row.mean_de2000
    if None in (row.white_err, row.red_err, row.green_err, row.blue_err):
        return None
    # Put the primary metric on roughly the dE2000 scale so mixed histories rank
    # sanely; ~200x maps a 0.01 xy error to ~2 dE, matching observed pairs.
    return 200.0 * (row.white_err + (row.red_err + row.green_err + row.blue_err) / 3.0)


def scored_primary_rows(rows: list[IterationRow]) -> list[tuple[IterationRow, float]]:
    """Verified rows paired with their score, in iteration order.

    Rechecks are included only when they are the sole hardware evidence for
    their iteration, so a hardware recheck of a software-predicted LUT can be
    ranked (and beat) the prediction it replaces.
    """
    software_only = {
        row.index for row in rows if not row.is_recheck and row.has_verify and row.is_software
    }
    scored = []
    for row in rows:
        if row.is_recheck and row.index not in software_only:
            continue
        score = iteration_score(row)
        if score is not None:
            scored.append((row, score))
    return scored


def hardware_rows(rows: list[IterationRow]) -> list[IterationRow]:
    """Rows measured with the LUT actually installed (rechecks + hardware verifies)."""
    return [row for row in rows if row.has_verify and not row.is_software]


def best_verified_iteration(rows: list[IterationRow]) -> IterationRow | None:
    """The verified iteration a human would keep (best score); None if none verified.

    A hardware measurement always outranks a software prediction: on firmware
    that reshapes imported cubes, the prediction can read 95% where the panel
    reads 79%, and picking the "best" from predictions keeps the wrong LUT.
    """
    scored = scored_primary_rows(rows)
    if not scored:
        return None
    hardware = [item for item in scored if not item[0].is_software]
    return min(hardware or scored, key=lambda item: item[1])[0]


# Convergence thresholds. In the default "channel" refine mode the fit is
# stateless and converges monotonically, so "stopped improving while already
# good" reliably means we're at the LUT-import noise floor. _GOOD_* are raw xy
# (compared to row errors directly); the score epsilons are on iteration_score's
# unified ~dE2000 scale (primary error is mapped ×200 to match), so a 1 dE swing
# reads as improvement and a 2 dE jump as a regression.
_GOOD_WHITE = 0.010
_GOOD_PRIMARY = 0.025
_GOOD_DE2000 = 5.0  # mean dE over the full set — broadcast-acceptable overall
_IMPROVE_EPS = 1.0
_REGRESS_EPS = 2.0


@dataclass(frozen=True)
class ConvergenceStatus:
    state: str  # "early" | "converging" | "converged" | "regressed"
    best_index: int | None
    message: str


def convergence_status(rows: list[IterationRow]) -> ConvergenceStatus:
    """Decide whether to keep refining or stop, from the verified iterations.

    Reproduces the human keeper decision on real histories: it only calls
    "converged" once the best result is actually good AND the newest round
    stopped improving it, so early rough rounds never trigger a premature stop.
    """
    scored = [item for item in scored_primary_rows(rows) if not item[0].is_software]
    if not scored:
        predicted = [row for row in rows if row.has_verify and row.is_software]
        if predicted:
            latest = predicted[-1]
            return ConvergenceStatus(
                "early", latest.index,
                f"v{latest.index} is a software prediction. Load it on the monitor and verify "
                "— only that measurement shows what the installed LUT does.",
            )
        return ConvergenceStatus("early", None, "Load the first LUT and verify to begin.")
    best_row, best_score = min(scored, key=lambda item: item[1])
    latest_row, latest_score = scored[-1]
    if len(scored) < 2:
        return ConvergenceStatus(
            "early", best_row.index,
            f"v{best_row.index} verified. Refine and verify again to gauge convergence.",
        )

    prev_best = min(score for _row, score in scored[:-1])
    improvement = prev_best - latest_score
    # "Good" means either the primaries are tight OR the perceptual dE2000 over
    # the whole patch set is low — a slightly wide primary that still looks
    # excellent overall (gamut-limited panels) should still count as converged.
    # best_row can win purely on mean_de2000 (scored_primary_rows only requires
    # a score) while still missing individual primary errors, so guard before
    # comparing them.
    if None in (best_row.white_err, best_row.red_err, best_row.green_err, best_row.blue_err):
        primaries_good = False
    else:
        worst_primary = max(best_row.red_err, best_row.green_err, best_row.blue_err)
        primaries_good = best_row.white_err < _GOOD_WHITE and worst_primary < _GOOD_PRIMARY
    perceptually_good = best_row.mean_de2000 is not None and best_row.mean_de2000 < _GOOD_DE2000
    best_is_good = primaries_good or perceptually_good

    if latest_score > best_score + _REGRESS_EPS:
        return ConvergenceStatus(
            "regressed", best_row.index,
            f"v{latest_row.index} came out worse — usually a LUT-import variation, not a real "
            f"step back. Keep v{best_row.index}: finish, or re-verify v{best_row.index}.",
        )
    if best_is_good and improvement < _IMPROVE_EPS:
        return ConvergenceStatus(
            "converged", best_row.index,
            f"Converged at v{best_row.index}. More rounds only chase measurement noise — "
            "finish and keep it.",
        )
    return ConvergenceStatus(
        "converging", best_row.index, "Still improving — load the next LUT and verify again.",
    )


@dataclass(frozen=True)
class NextAction:
    title: str
    detail: str


def next_action(session: CalibrationSession, root: str | Path = ".") -> NextAction:
    """The single most useful next step, phrased for the operator."""
    root = Path(root)
    if session.baseline_path is None or not _resolve(root, session.baseline_path).exists():
        return NextAction(
            "Capture the no-LUT baseline",
            "Put the monitor in bypass/unity mode, then click Baseline.",
        )
    current = session.current_iteration
    if current is None:
        return NextAction(
            "Generate the first correction LUT",
            "Click Generate to build the initial LUT from the baseline.",
        )
    if current.verify_path is None:
        return NextAction(
            f"Load {Path(current.cube_path).name} on the monitor, then Verify",
            "Copy the LUT via SD card, import it in the calibration wizard, "
            "then click Verify to measure it.",
        )
    if session.selected_iteration is None:
        return NextAction(
            f"Select v{current.index} as the keeper, or Refine again",
            "If the errors look converged, click Select. Otherwise Refine for "
            "another pass and verify again.",
        )
    return NextAction(
        f"Export the selected LUT (v{session.selected_iteration.index})",
        "Click Export to copy the selected LUT into the exports folder.",
    )


def readiness_warnings(session: CalibrationSession) -> list[str]:
    """Setup problems that would invalidate captures or the exported LUT."""
    warnings: list[str] = []
    firmware = session.firmware
    if not firmware.manual_adjustments_zeroed:
        warnings.append("Manual adjustments are not recorded as zeroed.")
    if firmware.measured_feed_range == "unknown":
        warnings.append("Measured feed range is unknown; profile the signal chain.")
    if (
        firmware.declared_input_range != "unknown"
        and firmware.measured_feed_range != "unknown"
        and firmware.declared_input_range != firmware.measured_feed_range
    ):
        warnings.append(
            "Declared input range differs from the measured feed range; "
            "keep this exact chain stable."
        )
    if session.profile_path is None:
        warnings.append("No device profile is linked yet.")
    required = CHAIN_STATE_REQUIRED_FIELDS.get(session.device_mode, ())
    missing = [
        field
        for field in required
        if not (session.chain_state.get(field) or "").strip()
        or (session.chain_state.get(field) or "").strip().upper() == "TBD"
    ]
    if missing:
        warnings.append(
            f"Missing chain state for {session.device_mode}: {', '.join(missing)}."
        )
    return warnings


def baseline_capture_path(session_dir: str | Path) -> Path:
    return Path(session_dir) / "baseline.json"


@dataclass(frozen=True)
class VerifyTarget:
    iteration: SessionIteration
    output_path: Path
    is_recheck: bool


def verify_capture_target(
    session: CalibrationSession,
    session_dir: str | Path,
    index: int | None = None,
) -> VerifyTarget:
    """Where the next verify capture should be written.

    A fresh verify of the current LUT writes verify_v{index}.json; an explicit
    --index recheck writes verify_v{index}_recheck_{n}.json without replacing
    the original. Mirrors the CLI's verify command.
    """
    session_dir = Path(session_dir)
    iteration = (
        session.iteration_by_index(index) if index is not None else session.current_iteration
    )
    if iteration is None:
        if index is None:
            raise ValueError("Generate a LUT before verifying.")
        raise ValueError(f"No iteration {index} in this session.")

    if index is None:
        return VerifyTarget(iteration, session_dir / f"verify_v{iteration.index}.json", False)
    recheck_index = len(iteration.verify_rechecks) + 1
    output = session_dir / f"verify_v{iteration.index}_recheck_{recheck_index}.json"
    return VerifyTarget(iteration, output, True)


def slugify(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "unknown"


def export_filename(session: CalibrationSession, iteration_index: int) -> str:
    monitor = slugify(session.monitor_id)
    model = slugify(session.model) or "unknown-model"
    gamma = str(session.target_gamma).replace(".", "p")
    target = slugify(session.target_name)
    return f"{monitor}_{model}_{target}_gamma{gamma}_v{iteration_index}.cube"


def apply_preset(session: CalibrationSession, preset: CalibrationPreset) -> None:
    """Copy a preset's device/target/firmware/chain fields onto a session.

    Mirrors the CLI apply-preset so the GUI can create sessions from presets.
    """
    session.model = preset.model
    session.device_mode = preset.device_mode
    session.target_name = preset.target_name
    session.target_gamma = preset.target_gamma
    session.profile_path = preset.profile_path
    session.firmware.calibration_target = preset.calibration_target
    session.firmware.declared_input_range = preset.declared_input_range
    session.firmware.measured_feed_range = preset.measured_feed_range
    session.firmware.dynamic_range_step = preset.dynamic_range_step
    session.firmware.manual_adjustments_zeroed = preset.manual_adjustments_zeroed
    session.update_chain_state(dict(preset.chain_state))
    if preset.notes and preset.notes not in session.firmware.notes:
        if session.firmware.notes:
            session.firmware.notes += "\n"
        session.firmware.notes += preset.notes


def delta_e_line(
    measurements,
    *,
    target_name: str = "rec709",
    target_gamma: float = 2.4,
) -> str | None:
    """One-line CIEDE2000 summary of a verify capture, or None if unscorable.

    Reference points: dE2000 < 1 is invisible, < 2-3 is broadcast-grade
    (EBU/ITU tolerance), > 5 is plainly visible.
    """
    from smallhd_cal.analysis import verify_delta_e_report

    try:
        rep = verify_delta_e_report(
            measurements, target_name=target_name, target_gamma=target_gamma
        )
    except (ValueError, KeyError):
        return None
    return (
        f"dE2000 avg {rep.average:.2f}, max {rep.maximum:.2f} ({rep.worst_name}), "
        f"{len(rep.rows)} patches"
    )


def _is_neutral_patch(name: str) -> bool:
    return name in ("black", "white") or name.startswith("gray")


def neutral_axis_warning(
    measurements,
    *,
    target_name: str = "rec709",
    target_gamma: float = 2.4,
) -> str | None:
    """Flag a verify whose ceiling is the white point / gray axis, not colors.

    A calibration can pass on primaries while the white and grays are tinted —
    the most visible error, and the classic signature of a feed whose LUT stage
    is applied *after* a colour-space conversion (SDI or wireless): the software
    refine builds the cube against the signal it measures before the chain, so
    it predicts a clean neutral axis that the installed LUT does not deliver.
    Returns a warning string when the neutral axis dominates, else None.
    """
    from smallhd_cal.analysis import verify_delta_e_report

    try:
        rep = verify_delta_e_report(
            measurements, target_name=target_name, target_gamma=target_gamma
        )
    except (ValueError, KeyError):
        return None

    neutral = [r for r in rep.rows if _is_neutral_patch(r.name)]
    colors = [r for r in rep.rows if not _is_neutral_patch(r.name)]
    if not neutral or not colors:
        return None
    white = next((r.de2000 for r in rep.rows if r.name == "white"), None)
    neutral_avg = sum(r.de2000 for r in neutral) / len(neutral)
    color_avg = sum(r.de2000 for r in colors) / len(colors)

    # Only warn when the neutral axis is both objectionable in absolute terms
    # and clearly worse than the colours (so a uniformly-good or uniformly-poor
    # result doesn't trip it).
    worst_neutral = white if white is not None else neutral_avg
    if worst_neutral < 3.0 or worst_neutral < color_avg + 1.5:
        return None
    white_txt = f"white dE {white:.1f}, " if white is not None else ""
    return (
        f"Neutral axis is the limiter: {white_txt}gray-axis avg {neutral_avg:.1f} "
        f"vs colours {color_avg:.1f}. The white point is tinted even though the "
        f"primaries score well. On a converted feed (SDI/wireless) this is the "
        f"LUT stage running after a colour-space conversion the software refine "
        f"can't see — refine from this hardware capture (try matrix mode) rather "
        f"than another software round."
    )


def brightness_hint(current_nits: float, target_nits: float, *, tolerance: float = 0.05):
    """Steer a fine-tune brightness slider onto the target with a live reading.

    Returns (on_target, message). Within +/- tolerance of the target counts as
    on target; otherwise the message says which way to move the slider.
    """
    if target_nits <= 0:
        return False, ""
    off = (current_nits - target_nits) / target_nits
    if abs(off) <= tolerance:
        return True, f"✓ on target — {current_nits:.0f} of ~{target_nits:.0f} nits"
    direction = "LOWER" if off > 0 else "RAISE"
    side = "high" if off > 0 else "low"
    return False, (
        f"{direction} the fine-tune brightness — {current_nits:.0f} nits, "
        f"{abs(off) * 100:.0f}% {side} (target ~{target_nits:.0f})"
    )


def luminance_target_warning(baseline, target_nits: float, *, tolerance: float = 0.25) -> str | None:
    """Warn when the baseline white sits far from the shared Studio target.

    Matched monitors must share one brightness; a baseline captured well off
    the target (the RX at ~198 nits against a 100-nit target) means the fine-
    tune brightness isn't set, which breaks TX<->RX matching and characterizes
    the panel at the wrong backlight. Returns a string or None.
    """
    by = {m.patch.name: m for m in baseline}
    white = by.get("white")
    if white is None or target_nits <= 0:
        return None
    y = white.xyz[1]
    off = (y - target_nits) / target_nits
    if abs(off) <= tolerance:
        return None
    return (
        f"Baseline white is {y:.0f} nits, but the shared Studio target is ~{target_nits:.0f}. "
        "The fine-tune brightness isn't on the matched level — use “Read peak nits” to dial "
        f"the slider to ~{target_nits:.0f}, then re-run the baseline (matched monitors must "
        "share one brightness)."
    )


def is_software_verified(iteration) -> bool:
    """True when the iteration's only verify came from the software refine loop.

    Those captures measure patches pushed through the cube on a still-identity
    monitor: real probe data, but not proof the installed LUT behaves. The
    iteration still needs a hardware verify (or a recheck) once loaded.
    """
    if not getattr(iteration, "verify_path", None):
        return False
    if getattr(iteration, "verify_rechecks", None):
        return False
    return SOFTWARE_VERIFY_MARKER in (getattr(iteration, "notes", "") or "")
