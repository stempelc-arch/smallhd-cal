"""In-process calibration workflow steps, callable from the GUI.

These mirror the session CLI (tools/calibrate_session.py) command-by-command,
but as importable functions with no argparse and no I/O to stdout. The GUI runs
them on a worker thread; keeping them here (rather than shelling out to the CLI)
means the packaged, frozen app has no dependency on a Python interpreter or the
tools/ directory. Each function returns a short human-readable status line.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from smallhd_cal.analysis import summarize_measurements
from smallhd_cal.calibration import build_target_matrix_correction
from smallhd_cal.lut import read_smallhd_cube, wrap_legal_range, write_bmd_cube
from smallhd_cal.measurement import (
    Measurement,
    Patch,
    read_measurements_json,
    write_measurements_json,
)
from smallhd_cal.presets import get_preset
from smallhd_cal.refine import refine_step
from smallhd_cal.report import (
    BLACK_LEVEL_KEY,
    WHITE_LEVEL_KEY,
    apply_preset,
    export_filename,
    levels_from_capture,
)
from smallhd_cal.session import (
    SessionIteration,
    load_session,
    new_session,
    save_session,
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


IDENTITY_CUBE_NAME = "SmallHD_identity_BMD17.cube"


def ensure_identity_lut(luts_dir: str | Path) -> Path:
    """Write (once) the identity cube the operator imports before a baseline.

    BMD format, 17-point — the same recipe as the correction cubes, because the
    legacy LUT_SIZE-header identity files in luts/ hit the firmware's
    range-guessing parser branch and don't pass video through untouched.
    """
    path = Path(luts_dir) / IDENTITY_CUBE_NAME
    if not path.exists():
        write_bmd_cube(path, 17, title="SmallHD identity (BMD 17pt)")
    return path


def create_session_from_preset(
    sessions_root: str | Path,
    monitor_id: str,
    preset_name: str,
    *,
    plan_key: str | None = None,
    force: bool = False,
) -> Path:
    """Create sessions_root/<monitor_id> from a preset. Returns the session dir.

    `plan_key` records which DevicePlan the session was started from, so the
    wizard can show that device's exact on-monitor procedures on every visit.
    """
    session_dir = Path(sessions_root) / monitor_id
    if (session_dir / "session.json").exists() and not force:
        raise ValueError(f"Session already exists: {session_dir}")
    preset = get_preset(preset_name)
    session = new_session(
        monitor_id,
        model=preset.model,
        target_gamma=preset.target_gamma,
        target_name=preset.target_name,
        device_mode=preset.device_mode,
    )
    apply_preset(session, preset)
    if plan_key:
        session.chain_state["device_plan"] = plan_key
    save_session(session_dir, session)
    return session_dir


def record_baseline(session_dir: str | Path, measurements_path: str | Path) -> str:
    """Point the session at a freshly captured baseline."""
    session = load_session(session_dir)
    session.baseline_path = str(measurements_path)
    save_session(session_dir, session)
    return f"Baseline recorded: {measurements_path}"


def run_live_calibration(
    session_dir: str | Path,
    measure,
    *,
    size: int = 17,
    lut_range: str = "full",
    levels=None,
    on_progress=None,
) -> str:
    """Build a correction LUT by live per-point convergence, in one automated pass.

    ``measure(r, g, b) -> XYZ`` shows a patch (through the fixed identity LUT) and
    returns the probe reading; the convergence loop calls it many times, adjusting
    the signal until each target color lands, then the achieved signals become the
    LUT. Needs the identity-LUT baseline already captured (used as the start model).
    """
    from smallhd_cal.live import (
        PanelModel,
        build_live_correction,
        characterize_gray_ramp,
        characterize_patch_set,
    )

    session_dir = Path(session_dir)
    session = load_session(session_dir)
    if session.baseline_path is None:
        raise ValueError("Capture the identity-LUT baseline before live calibration.")
    baseline = read_measurements_json(session.baseline_path)
    if not baseline:
        raise ValueError(f"Baseline capture is empty or missing: {session.baseline_path}")

    model = PanelModel.from_baseline(baseline)

    # Match the LUT's black handling to the ACTUAL feed, measured from the
    # baseline: if black is near true-black (high contrast) the feed reaches 0,
    # so a full-range LUT keeps it; a lifted black (~1 nit) means a legal feed,
    # where a full-range LUT would push black below the floor and clip. "auto"
    # (default) infers it; an explicit "legal"/"full" overrides.
    by_name = {m.patch.name: m for m in baseline}
    contrast = 0.0
    if "white" in by_name and "black" in by_name:
        black_y, white_y = by_name["black"].xyz[1], by_name["white"].xyz[1]
        contrast = white_y / black_y if black_y > 0 else 0.0
    # BMD-diagnostic run 2026-07-09: the Mac HDMI feed reaches code 0 (full
    # range) and the standard-format import path indexes the LUT by raw code —
    # so the LUT is authored in the full 0-1 domain and black maps to true
    # black. Only an explicit "legal" builds a legal-wrapped LUT (for a chain
    # that genuinely clips to legal).
    feed = "legal" if lut_range == "legal" else "full"

    if levels is None:
        # 11 gray points (the LUT interpolates) with a capped iteration count
        # keeps the sweep short; the persistent probe session makes each read fast.
        levels = np.linspace(0.0, 1.0, 11)

    # Primaries + secondaries converge after the grays: the fitted color matrix
    # then comes from measurement instead of the baseline model, which is what
    # bounds primary accuracy (model-only runs left red ~0.03 off in xy).
    color_patches = [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
        (1.0, 1.0, 0.0),
        (0.0, 1.0, 1.0),
        (1.0, 0.0, 1.0),
    ]
    total_points = len(levels) + len(color_patches)

    def gray_progress(done: int, _total: int) -> None:
        if on_progress is not None:
            on_progress(done, total_points)

    def patch_progress(done: int, _total: int) -> None:
        if on_progress is not None:
            on_progress(len(levels) + done, total_points)

    characterization = characterize_gray_ramp(
        measure,
        model,
        target_name=session.target_name,
        target_gamma=session.target_gamma,
        levels=levels,
        on_point=gray_progress,
        max_iters=5,
    )
    characterization.patch_results = characterize_patch_set(
        measure,
        model,
        color_patches,
        target_name=session.target_name,
        target_gamma=session.target_gamma,
        on_point=patch_progress,
        max_iters=5,
    )
    transform = build_live_correction(characterization, feed=feed)

    index = session.next_index()
    cube_path = str(session_dir / f"lut_v{index}.cube")
    write_bmd_cube(cube_path, size, transform)

    # Persist the characterization + a convergence health metric so a bad run
    # (e.g. points that clipped / never converged) is diagnosable after the fact.
    results = characterization.results + [p.result for p in characterization.patch_results]
    max_residual = max((r.residual for r in results), default=0.0)
    measurements = sum(r.iterations for r in results)
    char_path = session_dir / f"live_v{index}.json"
    char_path.write_text(_characterization_json(characterization), encoding="utf-8")

    session.add_iteration(
        SessionIteration(
            index=index,
            cube_path=cube_path,
            cube_index_order="red-fastest",
            created_at=_now(),
            notes=(
                f"live closed-loop gray ramp + {len(color_patches)} color patches, "
                f"feed={feed} (requested={lut_range}, "
                f"baseline contrast={contrast:.0f}:1), characterization={char_path}, "
                f"measurements={measurements}, max_residual={max_residual:.4f}"
            ),
        )
    )
    save_session(session_dir, session)
    return cube_path


def run_signal_refine(
    session_dir: str | Path,
    measure,
    patches: list[Patch],
    *,
    rounds: int = 2,
    size: int = 17,
    converged_score: float = 0.0045,
    on_progress=None,
) -> str:
    """Converge the correction in software before any SD trip.

    The BMD diagnostics (2026-07-09) proved the import path is deterministic
    and the feed/pipeline raw full-domain, so displaying lut.lookup(patch) on
    the still-identity monitor reproduces what the loaded cube would show.
    Each round measures a verify sweep through the current cube in signal
    space, records it as that iteration's verify, and refines — the SD-trip
    refine loop with zero SD trips. Stops early once the score is at the
    converged threshold (further rounds just chase probe noise; see the
    hardware v3→v4 ring). Returns the cube to load.
    """
    session_dir = Path(session_dir)
    # rounds refines plus a closing verify: every cube (including the last
    # refined one) ends up software-verified, so the scoreboard, the
    # convergence banner, and the "load this" instruction all name the SAME
    # iteration — a final unverified cube caused conflicting advice (test 69).
    total = (rounds + 1) * len(patches)
    previous_score = None

    for round_index in range(rounds + 1):
        session = load_session(session_dir)
        iteration = session.current_iteration
        if iteration is None:
            raise ValueError("Run live calibration before signal-space refinement.")
        lut = read_smallhd_cube(iteration.cube_path, iteration.cube_index_order)

        measurements = []
        for patch_index, patch in enumerate(patches):
            shown = lut.lookup(patch.r, patch.g, patch.b)
            xyz = measure(*shown)
            measurements.append(Measurement(patch=patch, xyz=tuple(xyz), timestamp=_now()))
            if on_progress is not None:
                on_progress(round_index * len(patches) + patch_index + 1, total)

        verify_path = session_dir / f"verify_v{iteration.index}.json"
        write_measurements_json(verify_path, measurements)
        record_verify(session_dir, iteration.index, verify_path, is_recheck=False)
        iteration_note = f"software signal-space verify ({len(measurements)} patches)"
        session = load_session(session_dir)
        current = session.iteration_by_index(iteration.index)
        if current is not None and iteration_note not in (current.notes or ""):
            current.notes = f"{current.notes} | {iteration_note}".strip(" |")
            save_session(session_dir, session)

        score = _verify_score(measurements, session.target_name)
        if round_index >= rounds:
            break  # closing verify done; no further refine
        if score is not None and score <= converged_score:
            break
        # Gamut-limited panels can never hit the absolute threshold (e.g. the
        # RX's blue floor alone exceeds it) — stop on a plateau instead of
        # refining probe noise into the cube.
        if score is not None and previous_score is not None and score >= previous_score - 0.0005:
            break
        previous_score = score
        refine_lut(session_dir, size=size, feed="full")

    session = load_session(session_dir)
    return session.current_iteration.cube_path


def _verify_score(measurements: list[Measurement], target_name: str) -> float | None:
    """White-weighted mean chromaticity error, the metric behind accuracy%."""
    from smallhd_cal.calibration import color_target

    target = color_target(target_name)
    wanted = {
        "white": target.white_xy,
        "red": target.primaries_xy[0],
        "green": target.primaries_xy[1],
        "blue": target.primaries_xy[2],
    }
    by_name = {m.patch.name: m for m in measurements}
    errors = {}
    for name, (tx, ty) in wanted.items():
        m = by_name.get(name)
        if m is None:
            return None
        x_sum = sum(m.xyz)
        if x_sum <= 0:
            return None
        x, y = m.xyz[0] / x_sum, m.xyz[1] / x_sum
        errors[name] = float(np.hypot(x - tx, y - ty))
    return (2 * errors["white"] + errors["red"] + errors["green"] + errors["blue"]) / 5


def _characterization_json(characterization) -> str:
    import json

    model = characterization.model
    payload = {
        "target_name": characterization.target_name,
        "target_gamma": characterization.target_gamma,
        "gray_codes": characterization.gray_codes.tolist(),
        "gray_signals": characterization.gray_signals.tolist(),
        "results": [
            {
                "signal": list(r.signal),
                "achieved_xyz": list(r.achieved_xyz),
                "iterations": r.iterations,
                "residual": r.residual,
            }
            for r in characterization.results
        ],
        "patches": [
            {
                "target_rgb": list(p.target_rgb),
                "target_xyz": list(p.target_xyz),
                "signal": list(p.result.signal),
                "achieved_xyz": list(p.result.achieved_xyz),
                "iterations": p.result.iterations,
                "residual": p.result.residual,
            }
            for p in characterization.patch_results
        ],
        "model": {
            "native": model.native.tolist(),
            "black": model.black.tolist(),
            "white_net_y": model.white_net_y,
        },
    }
    return json.dumps(payload, indent=1) + "\n"


def record_dynamic_range(session_dir: str | Path, measurements_path: str | Path) -> str:
    """Record a dynamic-range capture (matches the firmware's Measure step).

    Reports the panel's black/white luminance and native contrast, and marks
    the session's dynamic-range wizard step as measured.
    """
    session = load_session(session_dir)
    session.dynamic_range_path = str(measurements_path)
    session.firmware.dynamic_range_step = "measured"
    save_session(session_dir, session)
    summary = summarize_measurements(read_measurements_json(measurements_path))
    return (
        f"Contrast {summary.contrast_ratio:.0f}:1 "
        f"(black Y {summary.black_y:.4f}, white Y {summary.white_y:.1f} cd/m²)"
    )


def generate_lut(session_dir: str | Path, *, size: int = 17, lut_range: str = "full") -> str:
    """Build the first correction LUT from the baseline (mirrors CLI generate)."""
    session_dir = Path(session_dir)
    session = load_session(session_dir)
    if session.baseline_path is None:
        raise ValueError("Capture the baseline before generating a LUT.")
    baseline = read_measurements_json(session.baseline_path)
    if not baseline:
        raise ValueError(f"Baseline capture is empty or missing: {session.baseline_path}")

    transform = build_target_matrix_correction(
        baseline,
        target_gamma=session.target_gamma,
        target_name=session.target_name,
    )
    if lut_range == "legal":
        transform = wrap_legal_range(transform)

    index = session.next_index()
    cube_path = str(session_dir / f"lut_v{index}.cube")
    write_bmd_cube(cube_path, size, transform)
    session.add_iteration(
        SessionIteration(
            index=index,
            cube_path=cube_path,
            cube_index_order="red-fastest",
            created_at=_now(),
            notes=f"initial {session.target_name} correction, lut-range={lut_range}",
        )
    )
    save_session(session_dir, session)
    return f"Wrote {cube_path}"


def record_verify(
    session_dir: str | Path,
    iteration_index: int,
    output_path: str | Path,
    *,
    is_recheck: bool,
) -> str:
    """Attach a verify capture to an iteration (fresh verify or a recheck)."""
    session = load_session(session_dir)
    iteration = session.iteration_by_index(iteration_index)
    if iteration is None:
        raise ValueError(f"No iteration {iteration_index} in this session.")
    if is_recheck:
        iteration.verify_rechecks.append(str(output_path))
    else:
        iteration.verify_path = str(output_path)
        # This capture replaces any software prediction for this iteration, so
        # the iteration must stop advertising itself as software-verified.
        from smallhd_cal.report import SOFTWARE_VERIFY_MARKER

        if SOFTWARE_VERIFY_MARKER in (iteration.notes or ""):
            iteration.notes = " | ".join(
                part for part in (iteration.notes or "").split(" | ")
                if SOFTWARE_VERIFY_MARKER not in part
            ).strip(" |")

    # First verify with a LUT active establishes the black/white nit levels to
    # type into the monitor's wizard; keep them stable afterwards so the same
    # numbers are entered on every LUT upload.
    levels = levels_from_capture(read_measurements_json(output_path))
    if levels:
        white_y, black_y = levels
        session.chain_state.setdefault(WHITE_LEVEL_KEY, f"{white_y:.2f}")
        session.chain_state.setdefault(BLACK_LEVEL_KEY, f"{black_y:.2f}")

    save_session(session_dir, session)
    return f"Verify recorded for v{iteration_index}: {output_path}"


def save_probe_level(session_dir: str | Path, which: str, nits: float) -> str:
    """Save a black (min) or white (max) nit level for the monitor's wizard."""
    key = WHITE_LEVEL_KEY if which == "white" else BLACK_LEVEL_KEY
    session = load_session(session_dir)
    session.update_chain_state({key: f"{nits:.2f}"})
    save_session(session_dir, session)
    label = "white/max" if which == "white" else "black/min"
    return f"Saved {label} level: {nits:.2f} nits"


def refine_lut(
    session_dir: str | Path,
    *,
    size: int = 17,
    damping: float = 0.5,
    mode: str = "channel",
    feed: str = "full",
    verify_path: str | Path | None = None,
) -> str:
    """Generate the next LUT from a verify capture (mirrors CLI refine).

    `verify_path` overrides the iteration's own verify — pass a hardware
    recheck to refine against what the *installed* LUT really does. On monitors
    whose firmware reshapes an imported cube (the Cine 7 500 RX smooths
    saturated corners and shadows), that measured composite is the only thing
    worth refining against; the software signal-space verify cannot see it.
    """
    session_dir = Path(session_dir)
    session = load_session(session_dir)
    iteration = session.current_iteration
    if iteration is None:
        raise ValueError("Run a verify capture for the current LUT before refining.")
    verify_source = str(verify_path) if verify_path is not None else iteration.verify_path
    if verify_source is None:
        raise ValueError("Run a verify capture for the current LUT before refining.")
    if session.baseline_path is None:
        raise ValueError("Session has no baseline.")

    baseline = read_measurements_json(session.baseline_path)
    verify = read_measurements_json(verify_source)
    active_lut = read_smallhd_cube(iteration.cube_path, iteration.cube_index_order)
    transform, compensation = refine_step(
        baseline,
        verify,
        active_lut,
        np.array(iteration.compensation),
        target_gamma=session.target_gamma,
        target_name=session.target_name,
        color_damping=damping,
        feed=feed,
        mode=mode,
    )

    index = session.next_index()
    cube_path = str(session_dir / f"lut_v{index}.cube")
    write_bmd_cube(cube_path, size, transform)
    session.add_iteration(
        SessionIteration(
            index=index,
            cube_path=cube_path,
            cube_index_order="red-fastest",
            compensation=compensation.tolist(),
            damping=damping,
            created_at=_now(),
            notes=(
                f"refined from hardware capture {Path(verify_source).name}"
                if verify_path is not None
                else ""
            ),
        )
    )
    save_session(session_dir, session)
    return f"Wrote {cube_path}"


def select_iteration(session_dir: str | Path, index: int) -> str:
    """Mark a verified iteration as the keeper (mirrors CLI select)."""
    session = load_session(session_dir)
    iteration = session.select_iteration(index)
    save_session(session_dir, session)
    return f"Selected v{iteration.index}: {iteration.cube_path}"


def export_selected(session_dir: str | Path, out_dir: str | Path) -> str:
    """Copy the selected LUT into out_dir with a descriptive name."""
    session = load_session(session_dir)
    selected = session.selected_iteration
    if selected is None:
        raise ValueError("No selected LUT. Select a verified iteration first.")
    source = Path(selected.cube_path)
    if not source.exists():
        raise ValueError(f"Selected LUT does not exist: {source}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / export_filename(session, selected.index)
    shutil.copy2(source, destination)
    return f"Exported {destination}"


def clear_selection(session_dir: str | Path) -> str:
    """Un-finish a session so a new refine round can be verified and re-selected."""
    session = load_session(session_dir)
    session.selected_iteration_index = None
    session.selected_at = None
    save_session(session_dir, session)
    return "Selection cleared"


def suggested_session_name(
    sessions_root: str | Path, preset_name: str, stem: str | None = None
) -> str:
    """A tidy default name: <stem-or-model-slug>-<target>-<YYYY-MM-DD>[-N if taken].

    Consistent, sortable names keep the session list organized without the
    operator inventing one each time (which produced 'test 69' and folders with
    stray slashes). Device plans pass an explicit stem (e.g. "cine7-tx"); the
    date groups a day's work; the -N suffix disambiguates repeat runs the same
    day.
    """
    from smallhd_cal.report import slugify

    preset = get_preset(preset_name)
    model = stem or slugify(preset.model) or "monitor"
    base = f"{model}-{preset.target_name}-{datetime.now(UTC):%Y-%m-%d}"
    root = Path(sessions_root)
    if not (root / base / "session.json").exists():
        return base
    n = 2
    while (root / f"{base}-{n}" / "session.json").exists():
        n += 1
    return f"{base}-{n}"


def delete_session(sessions_root: str | Path, monitor_id: str) -> str:
    """Remove a session directory (used to prune scratch/test runs)."""
    session_dir = Path(sessions_root) / monitor_id
    if not (session_dir / "session.json").exists():
        raise ValueError(f"No session named {monitor_id!r}.")
    shutil.rmtree(session_dir)
    return f"Deleted session {monitor_id}"


def session_is_finished(session_dir: str | Path) -> bool:
    """True once an iteration has been selected/exported — i.e. worth keeping.

    Everything else is a scratch run the operator can safely prune.
    """
    session = load_session(session_dir)
    return session.selected_iteration_index is not None
