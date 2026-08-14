from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smallhd_cal.analysis import summarize_measurements
from smallhd_cal.automation import (
    AutoCalibrationSettings,
    LiveCalibrationSettings,
    run_live_gray_characterization,
    run_patch_capture,
)
from smallhd_cal.calibration import (
    COLOR_TARGETS,
    build_target_matrix_correction,
    color_target,
)
from smallhd_cal.live import build_live_correction
from smallhd_cal.lut import read_smallhd_cube, wrap_legal_range, write_smallhd_cube
from smallhd_cal.measurement import read_measurements_json
from smallhd_cal.paths import default_app_paths
from smallhd_cal.presets import CalibrationPreset, get_preset, preset_names
from smallhd_cal.refine import refine_step
from smallhd_cal.session import (
    CHAIN_STATE_RECOMMENDED_FIELDS,
    CHAIN_STATE_REQUIRED_FIELDS,
    DEVICE_MODES,
    CalibrationSession,
    SessionIteration,
    discover_session_summaries,
    load_session,
    new_session,
    save_session,
)


def cmd_init(args: argparse.Namespace) -> None:
    preset = get_preset(args.preset) if args.preset else None
    target_name = preset.target_name if preset else args.target_space
    target = color_target(target_name)
    gamma = args.gamma if args.gamma is not None else (preset.target_gamma if preset else target.default_gamma)
    session = new_session(
        args.monitor,
        model=args.model or (preset.model if preset else ""),
        target_gamma=gamma,
        target_name=target_name,
        device_mode=preset.device_mode if preset else args.device_mode,
    )
    session.firmware.calibration_target = preset.calibration_target if preset else args.target
    session.firmware.declared_input_range = preset.declared_input_range if preset else args.input_range
    session.firmware.measured_feed_range = preset.measured_feed_range if preset else "unknown"
    session.firmware.dynamic_range_step = preset.dynamic_range_step if preset else args.dynamic_range
    session.firmware.manual_adjustments_zeroed = (
        preset.manual_adjustments_zeroed if preset else args.adjustments_zeroed
    )
    if preset:
        _apply_preset(session, preset)
    path = save_session(args.dir, session)
    print(f"Initialized session for {args.monitor} at {path}")


def cmd_quickstart(args: argparse.Namespace) -> None:
    preset = get_preset(args.preset)
    session_dir = Path(args.root) / args.monitor
    if session_dir.exists() and not args.force:
        raise SystemExit(f"Session already exists: {session_dir}. Pass --force to overwrite.")
    session = new_session(
        args.monitor,
        model=preset.model,
        target_gamma=preset.target_gamma,
        target_name=preset.target_name,
        device_mode=preset.device_mode,
    )
    _apply_preset(session, preset)
    save_session(session_dir, session)
    print(f"Created {session_dir} from preset {preset.name}")
    print(f"Next: .venv/bin/python tools/calibrate_session.py doctor --monitor {args.monitor}")
    print(f"Then: .venv/bin/python tools/calibrate_session.py baseline --monitor {args.monitor}")


def cmd_baseline(args: argparse.Namespace) -> None:
    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    out = str(session_dir / "baseline.json")
    settings = AutoCalibrationSettings(
        patch_csv="measurements/patch_sequence_v1.csv", measurements_json=out, resume=args.resume
    )
    run_patch_capture(default_app_paths(ROOT), settings)
    session.baseline_path = out
    save_session(session_dir, session)
    summary = summarize_measurements(read_measurements_json(out))
    print(
        f"Baseline saved. White Y {summary.white_y:.2f}, white xy "
        f"({summary.white_xy[0]:.4f}, {summary.white_xy[1]:.4f}), "
        f"gamma {summary.estimated_gamma:.2f}"
    )


def cmd_dynamic_range(args: argparse.Namespace) -> None:
    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    out = str(Path(args.out) if args.out is not None else session_dir / "dynamic_range.json")
    settings = AutoCalibrationSettings(
        patch_csv=args.csv,
        measurements_json=out,
        resume=args.resume,
    )
    run_patch_capture(default_app_paths(ROOT), settings)
    session.dynamic_range_path = out
    session.firmware.dynamic_range_step = "measured"
    save_session(session_dir, session)
    summary = summarize_measurements(read_measurements_json(out))
    print(
        f"Dynamic range saved. Black Y {summary.black_y:.4f}, "
        f"white Y {summary.white_y:.2f}, contrast {summary.contrast_ratio:.0f}:1"
    )


def _session_feed(session: CalibrationSession) -> str:
    """The range math to use for this chain: what the feed *actually* is.

    Prefers the profiled `measured_feed_range`, falls back to the declared
    input range, and only then to legal. This is the single source of truth
    for both generate (how to wrap the LUT) and refine (how to read signals),
    so the two can never silently disagree the way a per-command default can.
    A declared/measured mismatch is surfaced separately by `doctor`.
    """
    fw = session.firmware
    for value in (fw.measured_feed_range, fw.declared_input_range):
        if value in ("legal", "full"):
            return value
    return "legal"


def cmd_generate(args: argparse.Namespace) -> None:
    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    if session.baseline_path is None:
        raise SystemExit("Run the baseline capture first.")
    baseline = read_measurements_json(session.baseline_path)
    lut_range = args.lut_range or _session_feed(session)
    transform = build_target_matrix_correction(
        baseline,
        target_gamma=session.target_gamma,
        target_name=session.target_name,
    )
    if lut_range == "legal":
        transform = wrap_legal_range(transform)
    index = session.next_index()
    cube_path = str(session_dir / f"lut_v{index}.cube")
    write_smallhd_cube(cube_path, args.size, transform)
    session.add_iteration(
        SessionIteration(
            index=index,
            cube_path=cube_path,
            created_at=datetime.now(UTC).isoformat(),
            notes=f"initial {session.target_name} correction, lut-range={lut_range}",
        )
    )
    save_session(session_dir, session)
    print(f"Wrote {cube_path}. Load it on the monitor, then run: verify")


def cmd_verify(args: argparse.Namespace) -> None:
    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    iteration = (
        session.iteration_by_index(args.index)
        if args.index is not None
        else session.current_iteration
    )
    if iteration is None:
        if args.index is None:
            raise SystemExit("Generate a LUT first.")
        raise SystemExit(f"No iteration {args.index} in this session.")

    if args.out is not None:
        out = str(Path(args.out))
    elif args.index is None:
        out = str(session_dir / f"verify_v{iteration.index}.json")
    else:
        recheck_index = len(iteration.verify_rechecks) + 1
        out = str(session_dir / f"verify_v{iteration.index}_recheck_{recheck_index}.json")

    settings = AutoCalibrationSettings(
        patch_csv="measurements/patch_sequence_v1.csv", measurements_json=out, resume=False
    )
    run_patch_capture(default_app_paths(ROOT), settings)
    if args.index is None:
        iteration.verify_path = out
    else:
        iteration.verify_rechecks.append(out)
    save_session(session_dir, session)
    _print_report(session)


def cmd_refine(args: argparse.Namespace) -> None:
    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    iteration = session.current_iteration
    if iteration is None or iteration.verify_path is None:
        raise SystemExit("Run a verify capture for the current LUT first.")
    baseline = read_measurements_json(session.baseline_path)
    verify = read_measurements_json(iteration.verify_path)
    active_lut = read_smallhd_cube(iteration.cube_path, iteration.cube_index_order)
    feed = args.feed or _session_feed(session)
    transform, compensation = refine_step(
        baseline,
        verify,
        active_lut,
        np.array(iteration.compensation),
        target_gamma=session.target_gamma,
        target_name=session.target_name,
        color_damping=args.damping,
        feed=feed,
        mode=args.mode,
    )
    print(f"Refining with feed range = {feed} (mode {args.mode}).")
    index = session.next_index()
    cube_path = str(session_dir / f"lut_v{index}.cube")
    write_smallhd_cube(cube_path, args.size, transform)
    session.add_iteration(
        SessionIteration(
            index=index,
            cube_path=cube_path,
            compensation=compensation.tolist(),
            damping=args.damping,
            created_at=datetime.now(UTC).isoformat(),
        )
    )
    save_session(session_dir, session)
    print(f"Wrote {cube_path}. Load it on the monitor, then run: verify")


def cmd_live_generate(args: argparse.Namespace) -> None:
    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    if session.baseline_path is None:
        raise SystemExit("Run the baseline capture first.")
    baseline_path = Path(session.baseline_path)
    if not baseline_path.exists():
        raise SystemExit(f"Baseline capture is missing: {baseline_path}")

    baseline = read_measurements_json(baseline_path)
    settings = LiveCalibrationSettings(
        levels=args.levels,
        settle_seconds=args.settle / 1000.0,
        timeout=args.timeout,
        max_iters=args.max_iters,
        tol=args.tol,
        gain=args.gain,
    )
    print(
        "Live mode: keep one fixed identity LUT / monitor state active for the entire run. "
        "Do not switch dynamic-range factory/defined-nits choices between measurements."
    )
    result = run_live_gray_characterization(
        default_app_paths(ROOT),
        baseline,
        target_name=session.target_name,
        target_gamma=session.target_gamma,
        settings=settings,
    )
    index = session.next_index()
    live_path = session_dir / f"live_v{index}.json"
    _write_live_characterization_json(live_path, result.characterization)

    transform = build_live_correction(result.characterization, feed=args.lut_range)
    cube_path = str(session_dir / f"lut_v{index}.cube")
    write_smallhd_cube(cube_path, args.size, transform)
    max_residual = max((item.residual for item in result.characterization.results), default=float("nan"))
    session.add_iteration(
        SessionIteration(
            index=index,
            cube_path=cube_path,
            created_at=datetime.now(UTC).isoformat(),
            notes=(
                "live closed-loop gray ramp, "
                f"lut-range={args.lut_range}, characterization={live_path}, "
                f"measurements={result.measurement_count}, max_residual={max_residual:.6f}"
            ),
        )
    )
    save_session(session_dir, session)
    print(f"Wrote live characterization to {live_path}")
    print(f"Wrote {cube_path}. Load it once on the monitor, then run: verify")


def cmd_status(args: argparse.Namespace) -> None:
    _print_report(load_session(_resolve_session_dir(args)))


def cmd_next_step(args: argparse.Namespace) -> None:
    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    step = _next_step(session)
    print(f"Next step for {session.monitor_id}: {step.title}")
    print(step.command)
    for note in step.notes:
        print(f"Note: {note}")


def cmd_select(args: argparse.Namespace) -> None:
    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    try:
        iteration = session.select_iteration(args.index)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    save_session(session_dir, session)
    print(f"Selected iteration {iteration.index}: {iteration.cube_path}")


def cmd_list(args: argparse.Namespace) -> None:
    summaries = discover_session_summaries(args.root)
    if not summaries:
        print(f"No sessions found under {args.root}")
        return

    print(
        f"{'monitor':<18s} {'mode':<19s} {'target':<7s} {'gamma':>5s} "
        f"{'selected':>8s} {'current':>8s} {'profile':>7s} selected LUT"
    )
    for summary in summaries:
        selected = (
            f"v{summary.selected_iteration_index}"
            if summary.selected_iteration_index is not None
            else "-"
        )
        current = (
            f"v{summary.current_iteration_index}"
            if summary.current_iteration_index is not None
            else "-"
        )
        lut = summary.selected_cube_path or "-"
        profile = "yes" if summary.profile_path else "-"
        print(
            f"{summary.monitor_id:<18.18s} {summary.device_mode:<19.19s} "
            f"{summary.target_name:<7.7s} {summary.target_gamma:>5.1f} "
            f"{selected:>8s} {current:>8s} "
            f"{profile:>7s} {lut}"
        )


def cmd_export_selected(args: argparse.Namespace) -> None:
    if getattr(args, "all", False):
        failures = _export_all_selected(args.root, Path(args.out))
        if failures:
            raise SystemExit(1)
        return

    session = load_session(_resolve_session_dir(args))
    destination = _export_selected_lut(session, Path(args.out))
    print(f"Exported selected LUT to {destination}")


def _export_all_selected(root: str, out_dir: Path) -> list[str]:
    failures = []
    for summary in discover_session_summaries(root):
        session = load_session(summary.session_dir)
        try:
            destination = _export_selected_lut(session, out_dir)
        except SystemExit as exc:
            failures.append(f"{summary.monitor_id}: {exc}")
            print(f"FAIL: {summary.monitor_id}: {exc}")
            continue
        print(f"Exported {summary.monitor_id}: {destination}")
    return failures


def _export_selected_lut(session: CalibrationSession, out_dir: Path) -> Path:
    selected = session.selected_iteration
    if selected is None:
        raise SystemExit("No selected LUT. Run select --index <n> first.")
    source = Path(selected.cube_path)
    if not source.exists():
        raise SystemExit(f"Selected LUT does not exist: {source}")

    out_dir.mkdir(parents=True, exist_ok=True)
    destination = out_dir / _export_lut_filename(session, selected.index)
    shutil.copy2(source, destination)
    return destination


def cmd_doctor(args: argparse.Namespace) -> None:
    stage = getattr(args, "stage", "export")
    if getattr(args, "all", False):
        failures = _doctor_all(args.root, stage)
        if failures:
            raise SystemExit(1)
        return

    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    profile_path = _doctor_profile_path(session, args.profile)
    checks = _print_doctor_report(session, session_dir, profile_path, stage)
    if _doctor_failed(checks):
        raise SystemExit(1)


def cmd_link_profile(args: argparse.Namespace) -> None:
    profile = Path(args.profile)
    if not profile.exists():
        raise SystemExit(f"Profile does not exist: {profile}")
    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    linked = session.link_profile(profile)
    save_session(session_dir, session)
    print(f"Linked profile: {linked}")


def cmd_list_presets(_args: argparse.Namespace) -> None:
    print(f"{'preset':<22s} {'mode':<19s} {'target':<7s} {'gamma':>5s} model")
    for name in preset_names():
        preset = get_preset(name)
        print(
            f"{preset.name:<22.22s} {preset.device_mode:<19.19s} "
            f"{preset.target_name:<7.7s} {preset.target_gamma:>5.1f} {preset.model}"
        )


def cmd_apply_preset(args: argparse.Namespace) -> None:
    preset = get_preset(args.preset)
    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    _apply_preset(session, preset)
    save_session(session_dir, session)
    print(f"Applied preset {preset.name} to {session.monitor_id}")


def _apply_preset(session: CalibrationSession, preset: CalibrationPreset) -> None:
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
    session.update_chain_state(preset.chain_state)
    if preset.notes and preset.notes not in session.firmware.notes:
        if session.firmware.notes:
            session.firmware.notes += "\n"
        session.firmware.notes += preset.notes


def cmd_set_chain_state(args: argparse.Namespace) -> None:
    updates = _parse_chain_state_updates(args.set)
    session_dir = _resolve_session_dir(args)
    session = load_session(session_dir)
    session.update_chain_state(updates)
    save_session(session_dir, session)
    for key, value in sorted(updates.items()):
        print(f"{key}={value}")


def _parse_chain_state_updates(items: list[str]) -> dict[str, str]:
    updates = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"Expected KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise SystemExit(f"Expected non-empty KEY=VALUE, got {item!r}")
        updates[key] = value
    return updates


def _write_live_characterization_json(path: Path, characterization) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "target_name": characterization.target_name,
        "target_gamma": characterization.target_gamma,
        "gray_codes": characterization.gray_codes.tolist(),
        "gray_signals": characterization.gray_signals.tolist(),
        "results": [
            {
                "signal": list(result.signal),
                "achieved_xyz": list(result.achieved_xyz),
                "iterations": result.iterations,
                "residual": result.residual,
            }
            for result in characterization.results
        ],
        "model": {
            "native": characterization.model.native.tolist(),
            "black": characterization.model.black.tolist(),
            "white_net_y": characterization.model.white_net_y,
            "signal_codes": characterization.model.signal_codes.tolist(),
            "linear_levels": characterization.model.linear_levels.tolist(),
        },
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _doctor_all(root: str, stage: str = "export") -> list[str]:
    failures = []
    summaries = discover_session_summaries(root)
    if not summaries:
        print(f"No sessions found under {root}")
        return failures
    for index, summary in enumerate(summaries):
        if index:
            print()
        session_dir = Path(summary.session_dir)
        session = load_session(session_dir)
        checks = _print_doctor_report(
            session,
            session_dir,
            _doctor_profile_path(session, None),
            stage,
        )
        if _doctor_failed(checks):
            failures.append(summary.monitor_id)
    if failures:
        print()
        print("Doctor failed for: " + ", ".join(failures))
    return failures


def _print_doctor_report(
    session: CalibrationSession,
    session_dir: Path,
    profile_path: Path | None,
    stage: str = "export",
) -> list[tuple[str, str]]:
    checks = _doctor_checks(session, session_dir, profile_path, stage)
    print(f"Doctor for {session.monitor_id} ({session.model or 'unknown model'}) [{stage}]")
    for severity, message in checks:
        print(f"{severity}: {message}")
    return checks


def _doctor_failed(checks: list[tuple[str, str]]) -> bool:
    return any(severity == "FAIL" for severity, _message in checks)


class NextStep(NamedTuple):
    title: str
    command: str
    notes: tuple[str, ...] = ()


def _next_step(session: CalibrationSession) -> NextStep:
    target = f"--monitor {session.monitor_id}"
    notes = _next_step_notes(session)

    if _missing_chain_state_fields(session):
        return NextStep(
            "fill chain-state details",
            ".venv/bin/python tools/calibrate_session.py set-chain-state "
            f"{target} --set key=value",
            notes,
        )

    if session.baseline_path is None:
        return NextStep(
            "capture baseline",
            f".venv/bin/python tools/calibrate_session.py baseline {target}",
            notes,
        )

    if not Path(session.baseline_path).exists():
        return NextStep(
            "capture baseline",
            f".venv/bin/python tools/calibrate_session.py baseline {target}",
            notes,
        )

    if (
        session.firmware.dynamic_range_step == "measured"
        and (session.dynamic_range_path is None or not Path(session.dynamic_range_path).exists())
    ):
        return NextStep(
            "capture dynamic range",
            f".venv/bin/python tools/calibrate_session.py dynamic-range {target}",
            notes,
        )

    current = session.current_iteration
    if current is None:
        return NextStep(
            "generate first LUT",
            f".venv/bin/python tools/calibrate_session.py generate {target}",
            notes,
        )

    if current.verify_path is None:
        return NextStep(
            f"verify LUT v{current.index}",
            f".venv/bin/python tools/calibrate_session.py verify {target}",
            notes,
        )

    if session.selected_iteration is None:
        return NextStep(
            f"select keeper LUT v{current.index}",
            f".venv/bin/python tools/calibrate_session.py select {target} --index {current.index}",
            notes,
        )

    return NextStep(
        "export selected LUT",
        f".venv/bin/python tools/calibrate_session.py export-selected {target} --out exports",
        notes,
    )


def _next_step_notes(session: CalibrationSession) -> tuple[str, ...]:
    notes: list[str] = []
    if session.profile_path is None:
        notes.append("No profile is linked yet; link one when you have a device profile.")
    if session.firmware.measured_feed_range == "unknown":
        notes.append("Measured feed range is unknown; keep this chain fixed until profiled.")
    if session.selected_iteration is not None:
        notes.append(f"Selected LUT is v{session.selected_iteration.index}.")
    return tuple(notes)


def _missing_chain_state_fields(session: CalibrationSession) -> tuple[str, ...]:
    required = CHAIN_STATE_REQUIRED_FIELDS.get(session.device_mode, ())
    return tuple(field for field in required if _chain_state_value_missing(session, field))


def _resolve_session_dir(args: argparse.Namespace | object) -> Path:
    explicit_dir = getattr(args, "dir", None)
    if explicit_dir:
        return Path(explicit_dir)

    monitor = getattr(args, "monitor", None)
    if monitor:
        root = Path(getattr(args, "root", "sessions"))
        session_dir = root / monitor
        if not (session_dir / "session.json").exists():
            raise SystemExit(f"No session found for monitor {monitor!r} under {root}")
        return session_dir

    raise SystemExit("Pass --dir or --monitor.")


def _add_session_target_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dir", help="Session directory, e.g. sessions/cine7-a")
    group.add_argument("--monitor", help="Monitor id under --root, e.g. cine7-a")
    parser.add_argument("--root", default="sessions", help="Session root used with --monitor")


def _add_session_target_or_all_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dir", help="Session directory, e.g. sessions/cine7-a")
    group.add_argument("--monitor", help="Monitor id under --root, e.g. cine7-a")
    group.add_argument("--all", action="store_true", help="Run across all sessions under --root")
    parser.add_argument("--root", default="sessions", help="Session root used with --monitor or --all")


def _doctor_profile_path(session: CalibrationSession, override: str | None) -> Path | None:
    if override:
        return Path(override)
    if session.profile_path:
        return Path(session.profile_path)
    return None


def _doctor_checks(
    session: CalibrationSession,
    session_dir: Path,
    profile_path: Path | None,
    stage: str = "export",
) -> list[tuple[str, str]]:
    checks: list[tuple[str, str]] = []

    if stage not in {"measure", "export"}:
        checks.append(("FAIL", f"Unknown doctor stage: {stage}"))
        return checks

    if stage == "export":
        checks.extend(_selected_lut_checks(session))

    checks.extend(_baseline_checks(session, stage))

    if session.firmware.manual_adjustments_zeroed:
        checks.append(("PASS", "Manual adjustments recorded as zeroed."))
    else:
        checks.append(("WARN", "Manual adjustments are not recorded as zeroed."))

    if session.firmware.declared_input_range == "unknown":
        checks.append(("WARN", "Declared input range is unknown."))
    else:
        checks.append(("PASS", f"Declared input range: {session.firmware.declared_input_range}"))

    if session.firmware.measured_feed_range == "unknown":
        checks.append(("WARN", "Measured feed range is unknown; profile the signal chain."))
    else:
        checks.append(("PASS", f"Measured feed range: {session.firmware.measured_feed_range}"))

    if (
        session.firmware.declared_input_range != "unknown"
        and session.firmware.measured_feed_range != "unknown"
        and session.firmware.declared_input_range != session.firmware.measured_feed_range
    ):
        checks.append((
            "WARN",
            "Declared input range differs from measured feed range; keep this exact chain stable.",
        ))

    if session.firmware.dynamic_range_step not in {"skipped", "measured"}:
        checks.append(("WARN", "Dynamic-range wizard step is not recorded."))
    else:
        checks.append(("PASS", f"Dynamic-range wizard step: {session.firmware.dynamic_range_step}"))
    checks.extend(_dynamic_range_checks(session))

    if session.firmware.notes.strip():
        checks.append(("PASS", "Firmware/session notes are recorded."))
    else:
        checks.append(("WARN", "Firmware/session notes are empty."))

    if session.target_name in COLOR_TARGETS:
        checks.append(("PASS", f"Calibration target: {session.target_name}"))
    else:
        checks.append(("FAIL", f"Unknown calibration target: {session.target_name}"))

    if session.device_mode in DEVICE_MODES:
        checks.append(("PASS", f"Device mode: {session.device_mode}"))
    else:
        checks.append(("FAIL", f"Unknown device mode: {session.device_mode}"))

    checks.extend(_chain_state_checks(session))

    if profile_path is None:
        checks.append((
            "WARN",
            "No linked profile; run link-profile or pass --profile profiles/<model>/profile.json.",
        ))
    elif profile_path.exists():
        checks.append(("PASS", f"Profile exists: {profile_path}"))
        checks.extend(_profile_consistency_checks(session, profile_path))
    else:
        checks.append(("FAIL", f"Profile is missing: {profile_path}"))

    if not session_dir.exists():
        checks.append(("FAIL", f"Session directory is missing: {session_dir}"))

    return checks


def _selected_lut_checks(session: CalibrationSession) -> list[tuple[str, str]]:
    selected = session.selected_iteration
    if selected is None:
        return [("FAIL", "No selected LUT. Run select --index <n> after a good verify.")]

    checks: list[tuple[str, str]] = []
    selected_path = Path(selected.cube_path)
    if selected_path.exists():
        checks.append(("PASS", f"Selected LUT exists: {selected.cube_path}"))
    else:
        checks.append(("FAIL", f"Selected LUT is missing: {selected.cube_path}"))
    if selected.verify_path or selected.verify_rechecks:
        checks.append(("PASS", f"Selected iteration v{selected.index} has verify evidence."))
    else:
        checks.append(("FAIL", f"Selected iteration v{selected.index} has no verify capture."))
    return checks


def _baseline_checks(session: CalibrationSession, stage: str) -> list[tuple[str, str]]:
    if session.baseline_path is None:
        if stage == "measure":
            return [("PASS", "No baseline recorded yet; ready for baseline capture.")]
        return [("FAIL", "No baseline capture recorded.")]
    if Path(session.baseline_path).exists():
        return [("PASS", f"Baseline exists: {session.baseline_path}")]
    return [("FAIL", f"Baseline is missing: {session.baseline_path}")]


def _dynamic_range_checks(session: CalibrationSession) -> list[tuple[str, str]]:
    if session.firmware.dynamic_range_step != "measured":
        return []

    if session.dynamic_range_path is None:
        return [("WARN", "Dynamic-range step is measured but no capture path is recorded.")]

    path = Path(session.dynamic_range_path)
    if not path.exists():
        return [("FAIL", f"Dynamic-range capture is missing: {session.dynamic_range_path}")]

    try:
        summary = summarize_measurements(read_measurements_json(path))
    except ValueError as exc:
        return [("FAIL", f"Dynamic-range capture cannot be summarized: {exc}")]

    return [(
        "PASS",
        "Dynamic range measured: "
        f"black Y {summary.black_y:.4f}, white Y {summary.white_y:.2f}, "
        f"contrast {summary.contrast_ratio:.0f}:1.",
    )]


def _chain_state_checks(session: CalibrationSession) -> list[tuple[str, str]]:
    required = CHAIN_STATE_REQUIRED_FIELDS.get(session.device_mode, ())
    recommended = CHAIN_STATE_RECOMMENDED_FIELDS.get(session.device_mode, ())
    checks: list[tuple[str, str]] = []
    missing = [field for field in required if _chain_state_value_missing(session, field)]
    if missing:
        checks.append((
            "WARN",
            "Missing chain state for "
            f"{session.device_mode}: {', '.join(missing)}.",
        ))
    else:
        checks.append(("PASS", f"Chain state complete for {session.device_mode}."))

    missing_recommended = [
        field for field in recommended
        if _chain_state_value_missing(session, field)
    ]
    if missing_recommended:
        checks.append((
            "WARN",
            "Recommended chain state missing for "
            f"{session.device_mode}: {', '.join(missing_recommended)}.",
        ))
    elif recommended:
        checks.append(("PASS", f"Recommended chain state recorded for {session.device_mode}."))

    if session.chain_state:
        recorded = ", ".join(f"{key}={value}" for key, value in sorted(session.chain_state.items()))
        checks.append(("PASS", f"Recorded chain state: {recorded}"))
    return checks


def _chain_state_value_missing(session: CalibrationSession, field: str) -> bool:
    value = session.chain_state.get(field)
    return not value or value.strip().upper() == "TBD"


def _profile_consistency_checks(
    session: CalibrationSession,
    profile_path: Path,
) -> list[tuple[str, str]]:
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [("FAIL", f"Profile JSON is invalid: {exc}")]

    checks: list[tuple[str, str]] = []
    selected = session.selected_iteration
    profile_index_order = profile.get("index_order")
    if selected is not None and profile_index_order:
        if profile_index_order == selected.cube_index_order:
            checks.append(("PASS", f"Profile index order matches selected LUT: {profile_index_order}"))
        else:
            checks.append((
                "FAIL",
                "Profile index order "
                f"{profile_index_order} differs from selected LUT {selected.cube_index_order}.",
            ))

    legal_reproduces = profile.get("identity_legal_reproduces_bypass")
    if legal_reproduces is True:
        checks.append(("PASS", "Profile says legal-range identity reproduces bypass."))
    elif legal_reproduces is False:
        checks.append(("WARN", "Profile says legal-range identity does not reproduce bypass."))

    return checks


def _export_lut_filename(session: CalibrationSession, iteration_index: int) -> str:
    monitor = _slug(session.monitor_id)
    model = _slug(session.model) or "unknown-model"
    gamma = str(session.target_gamma).replace(".", "p")
    target = _slug(session.target_name)
    return f"{monitor}_{model}_{target}_gamma{gamma}_v{iteration_index}.cube"


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "unknown"


def _print_report(session: CalibrationSession) -> None:
    target = color_target(session.target_name)
    targets = dict(zip(("red", "green", "blue"), target.primaries_xy, strict=True))
    targets["white"] = target.white_xy
    print(
        f"Session {session.monitor_id} ({session.model})  "
        f"{session.device_mode} {session.target_name} gamma {session.target_gamma}"
    )
    print(f"Firmware: {session.firmware.calibration_target}, "
          f"input range {session.firmware.declared_input_range}, "
          f"dynamic-range step {session.firmware.dynamic_range_step}")
    if session.selected_iteration is not None:
        print(
            f"Selected LUT: v{session.selected_iteration.index} "
            f"{session.selected_iteration.cube_path}"
        )
    print(f"{'iter':>4s} {'white':>8s} {'red':>8s} {'green':>8s} {'blue':>8s} {'gray50 dev':>11s}")
    for iteration in session.iterations:
        if iteration.verify_path is None:
            print(f"{iteration.index:4d}   (awaiting verify capture)")
        else:
            _print_measurement_row(str(iteration.index), iteration.verify_path, targets)
        for recheck_index, recheck_path in enumerate(iteration.verify_rechecks, start=1):
            _print_measurement_row(f"{iteration.index}r{recheck_index}", recheck_path, targets)


def _print_measurement_row(
    label: str,
    measurements_path: str,
    targets: dict[str, tuple[float, float]],
) -> None:
    from smallhd_cal.analysis import xyz_to_xy

    ms = {m.patch.name: m for m in read_measurements_json(measurements_path)}
    errs = []
    for name in ("white", "red", "green", "blue"):
        x, y = xyz_to_xy(ms[name].xyz)
        tx, ty = targets[name]
        errs.append(float(np.hypot(x - tx, y - ty)))
    wy, by = ms["white"].xyz[1], ms["black"].xyz[1]
    gray = next(
        (m for m in ms.values() if m.patch.name.startswith("gray_") and 0.49 < m.patch.r < 0.51),
        None,
    )
    gdev = ((gray.xyz[1] - by) / (wy - by) - gray.patch.r**2.4) if gray else float("nan")
    print(
        f"{label:>4s} {errs[0]:8.4f} {errs[1]:8.4f} {errs[2]:8.4f} "
        f"{errs[3]:8.4f} {gdev:+11.4f}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Session-based SmallHD calibration workflow: init -> baseline -> generate "
        "-> (load LUT on monitor) -> verify -> refine -> (load) -> verify ... until converged."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="Create a new per-monitor calibration session")
    p.add_argument("--dir", required=True)
    p.add_argument("--monitor", required=True, help="Monitor identifier, e.g. cine7-serial123")
    p.add_argument("--model", default="")
    p.add_argument("--preset", choices=preset_names())
    p.add_argument("--gamma", type=float)
    p.add_argument("--target-space", default="rec709", choices=tuple(COLOR_TARGETS))
    p.add_argument("--device-mode", default="smallhd", choices=DEVICE_MODES)
    p.add_argument("--target", default="Generic Rec.709")
    p.add_argument("--input-range", default="unknown", choices=("legal", "full", "auto", "unknown"))
    p.add_argument("--dynamic-range", default="skipped", choices=("skipped", "measured"))
    p.add_argument("--adjustments-zeroed", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("quickstart", help="Create sessions/<monitor> from a personal preset")
    p.add_argument("--monitor", required=True)
    p.add_argument("--preset", required=True, choices=preset_names())
    p.add_argument("--root", default="sessions")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_quickstart)

    p = sub.add_parser("baseline", help="Capture the no-LUT baseline (monitor in bypass)")
    _add_session_target_args(p)
    p.add_argument("--resume", action="store_true")
    p.set_defaults(func=cmd_baseline)

    p = sub.add_parser("dynamic-range", help="Capture black/white dynamic-range measurements")
    _add_session_target_args(p)
    p.add_argument("--csv", default="measurements/patch_sequence_dynamic_range.csv")
    p.add_argument("--out", help="Write the capture to this explicit measurement JSON path")
    p.add_argument("--resume", action="store_true")
    p.set_defaults(func=cmd_dynamic_range)

    p = sub.add_parser("generate", help="Generate the first correction LUT from the baseline")
    _add_session_target_args(p)
    p.add_argument("--size", type=int, default=33)
    p.add_argument(
        "--lut-range",
        default=None,
        choices=("legal", "full"),
        help="Override the LUT range; by default it follows the session's "
        "measured/declared feed range so generate and refine stay in sync.",
    )
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser("verify", help="Capture with the current LUT active on the monitor")
    _add_session_target_args(p)
    p.add_argument(
        "--index",
        type=int,
        help="Recheck a specific LUT iteration without replacing its original verify capture",
    )
    p.add_argument("--out", help="Write the capture to this explicit measurement JSON path")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("refine", help="Generate the next LUT from the latest verify")
    _add_session_target_args(p)
    p.add_argument("--size", type=int, default=33)
    p.add_argument("--damping", type=float, default=0.5)
    p.add_argument(
        "--mode",
        default="channel",
        choices=("channel", "matrix"),
        help="channel: per-channel drive maps fitted from all patches (stateless); "
        "matrix: gray map + damped 3x3 residual (used on the 1703 PX)",
    )
    p.add_argument(
        "--feed",
        default=None,
        choices=("legal", "full"),
        help="Override the feed range; by default it follows the session's "
        "measured/declared feed range. Must match what the source actually sends.",
    )
    p.set_defaults(func=cmd_refine)

    p = sub.add_parser(
        "live-generate",
        help="Live-converge a gray ramp with one fixed identity LUT, then write a LUT",
    )
    _add_session_target_args(p)
    p.add_argument("--size", type=int, default=33)
    p.add_argument("--lut-range", default="full", choices=("legal", "full"))
    p.add_argument("--levels", type=int, default=17)
    p.add_argument("--settle", type=int, default=400, help="Patch settle time in milliseconds")
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--max-iters", type=int, default=8)
    p.add_argument("--tol", type=float, default=0.0015)
    p.add_argument("--gain", type=float, default=0.9)
    p.set_defaults(func=cmd_live_generate)

    p = sub.add_parser("status", help="Show convergence across iterations")
    _add_session_target_args(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("next-step", help="Print the next useful command for a session")
    _add_session_target_args(p)
    p.set_defaults(func=cmd_next_step)

    p = sub.add_parser("select", help="Mark a verified LUT iteration as the keeper for this session")
    _add_session_target_args(p)
    p.add_argument("--index", type=int, required=True)
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("list", help="List monitor sessions and selected LUTs")
    p.add_argument("--root", default="sessions")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("export-selected", help="Copy the selected LUT to an export folder")
    _add_session_target_or_all_args(p)
    p.add_argument("--out", default="exports")
    p.set_defaults(func=cmd_export_selected)

    p = sub.add_parser("doctor", help="Preflight a session before measuring or exporting")
    _add_session_target_or_all_args(p)
    p.add_argument("--profile", help="Expected device profile JSON for this monitor/model")
    p.add_argument(
        "--stage",
        default="export",
        choices=("measure", "export"),
        help="measure: setup/capture readiness; export: selected-LUT readiness",
    )
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("link-profile", help="Remember the device profile JSON for this session")
    _add_session_target_args(p)
    p.add_argument("--profile", required=True)
    p.set_defaults(func=cmd_link_profile)

    p = sub.add_parser("list-presets", help="List personal calibration presets")
    p.set_defaults(func=cmd_list_presets)

    p = sub.add_parser("apply-preset", help="Apply a personal preset to an existing session")
    _add_session_target_args(p)
    p.add_argument("--preset", required=True, choices=preset_names())
    p.set_defaults(func=cmd_apply_preset)

    p = sub.add_parser("set-chain-state", help="Record repeatability details for this signal chain")
    _add_session_target_args(p)
    p.add_argument(
        "--set",
        action="append",
        required=True,
        metavar="KEY=VALUE",
        help="Chain-state key/value to record; repeat for multiple fields",
    )
    p.set_defaults(func=cmd_set_chain_state)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
