from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import numpy as np

from smallhd_cal.analysis import MeasurementSummary, summarize_measurements
from smallhd_cal.calibration import build_correction_transform
from smallhd_cal.displays import choose_external_display, list_displays
from smallhd_cal.live import LiveCharacterization, PanelModel, characterize_gray_ramp
from smallhd_cal.lut import write_smallhd_cube
from smallhd_cal.measurement import (
    Measurement,
    latest_measurements_by_patch,
    load_patch_sequence,
    read_measurements_json,
    write_measurements_json,
)
from smallhd_cal.paths import AppPaths, resolve_existing_path, resolve_output_path
from smallhd_cal.probe import (
    SPOTREAD_ARGS,
    ProbeCommand,
    ProbeError,
    find_bundled_spotread,
    read_spotread,
)

LogFn = Callable[[str], None]


@dataclass(frozen=True)
class AutoCalibrationSettings:
    patch_csv: str = "measurements/patch_sequence_v0.csv"
    measurements_json: str = "measurements/measurements_v0.json"
    output_lut: str = "luts/SmallHD_correction_gray_gamma24_33.cube"
    settle_seconds: float = 1.0
    timeout: float = 60.0
    gamma: float = 2.4
    lut_size: int = 33
    resume: bool = True
    correction_mode: str = "gray"


@dataclass(frozen=True)
class AutoCalibrationResult:
    measurement_count: int
    measurements_path: str
    lut_path: str
    summary: MeasurementSummary


@dataclass(frozen=True)
class LiveCalibrationSettings:
    levels: int = 17
    settle_seconds: float = 0.4
    timeout: float = 60.0
    max_iters: int = 8
    tol: float = 0.0015
    gain: float = 0.9


@dataclass(frozen=True)
class LiveCalibrationResult:
    characterization: LiveCharacterization
    measurement_count: int


class AutomationError(RuntimeError):
    pass


def _bind_escape_cancel(window: tk.Toplevel) -> Callable[[], bool]:
    """Bind Escape on `window` to a flag, and return a function to check it.

    Tk callbacks run inside the Tcl event loop that `root.update()` pumps; an
    exception raised there is caught by Tk's own `report_callback_exception`
    (prints a traceback to stderr) and never propagates back out of `update()`,
    so raising AutomationError directly from the binding does not actually
    cancel anything — the caller's loop just keeps running. Set a flag instead
    and have the loop check it explicitly after every `update()`.
    """
    cancelled = {"flag": False}
    window.bind("<Escape>", lambda _event: cancelled.__setitem__("flag", True))
    return lambda: cancelled["flag"]


def run_patch_capture(
    paths: AppPaths,
    settings: AutoCalibrationSettings,
    log: LogFn = print,
) -> tuple[list[Measurement], str]:
    """Display each patch, measure it, and persist after every reading.

    Returns (measurements, measurements_path). Does not analyze or write LUTs.
    """
    patches = load_patch_sequence(
        resolve_existing_path(settings.patch_csv, paths.resource_root, paths.user_data_root)
    )
    measurements_path = resolve_output_path(settings.measurements_json, paths.user_data_root)
    measurements = read_measurements_json(measurements_path) if settings.resume else []
    measured_by_patch = latest_measurements_by_patch(measurements)
    command = probe_command(paths)
    display = choose_external_display(list_displays())

    if display is None:
        log("No display metadata found; using Tk default screen.")
    else:
        label = "external" if not display.is_main else "main"
        log(f"Using {label} display {display.display_id}: {display.geometry}")

    root = tk.Tk()
    root.withdraw()
    patch_window = tk.Toplevel(root)
    patch_window.title("SmallHD Automated Patch")
    patch_window.configure(cursor="none")
    patch_window.overrideredirect(True)
    if display is not None:
        patch_window.geometry(display.geometry)
    patch_window.attributes("-topmost", True)
    is_cancelled = _bind_escape_cancel(patch_window)

    try:
        for index, patch in enumerate(patches, start=1):
            if patch.name in measured_by_patch:
                log(f"[{index}/{len(patches)}] Skip {patch.name}; already measured")
                continue

            r, g, b = patch.rgb8
            color = f"#{r:02x}{g:02x}{b:02x}"
            log(f"[{index}/{len(patches)}] Show {patch.name} RGB=({r}, {g}, {b})")
            patch_window.configure(bg=color)
            patch_window.deiconify()
            patch_window.lift()
            patch_window.focus_force()
            root.update()
            time.sleep(settings.settle_seconds)
            root.update()
            if is_cancelled():
                raise AutomationError("Automated calibration cancelled from patch window.")

            log(f"[{index}/{len(patches)}] Measure {patch.name}")
            try:
                reading = read_spotread(command, timeout=settings.timeout)
            except ProbeError as exc:
                raise AutomationError(f"Measurement failed for {patch.name}: {exc}") from exc

            measurement = Measurement(
                patch=patch,
                xyz=reading.xyz,
                timestamp=datetime.now(UTC).isoformat(),
            )
            measurements = [
                item for item in measurements if item.patch.name != measurement.patch.name
            ]
            measurements.append(measurement)
            measured_by_patch[patch.name] = measurement
            write_measurements_json(measurements_path, measurements)
            log(f"  XYZ={reading.xyz[0]:.4f} {reading.xyz[1]:.4f} {reading.xyz[2]:.4f}")

        log(f"Wrote measurements to {measurements_path}")
        return measurements, str(measurements_path)
    finally:
        patch_window.destroy()
        root.destroy()


def run_auto_calibration(
    paths: AppPaths,
    settings: AutoCalibrationSettings,
    log: LogFn = print,
) -> AutoCalibrationResult:
    measurements, measurements_path = run_patch_capture(paths, settings, log)
    lut_path = resolve_output_path(settings.output_lut, paths.user_data_root)
    summary = summarize_measurements(measurements)
    transform = build_correction_transform(
        measurements, mode=settings.correction_mode, target_gamma=settings.gamma
    )
    write_smallhd_cube(lut_path, settings.lut_size, transform)
    log(f"Wrote correction LUT to {lut_path}")
    return AutoCalibrationResult(
        measurement_count=len(measurements),
        measurements_path=measurements_path,
        lut_path=str(lut_path),
        summary=summary,
    )


def run_live_gray_characterization(
    paths: AppPaths,
    baseline: list[Measurement],
    *,
    target_name: str = "rec709",
    target_gamma: float = 2.4,
    settings: LiveCalibrationSettings | None = None,
    log: LogFn = print,
) -> LiveCalibrationResult:
    """Live-converge a neutral ramp while one fixed monitor LUT/state is active."""
    settings = settings or LiveCalibrationSettings()
    model = PanelModel.from_baseline(baseline)
    levels = np.linspace(0.0, 1.0, settings.levels)
    command = probe_command(paths)
    display = choose_external_display(list_displays())
    count = 0

    if display is None:
        log("No display metadata found; using Tk default screen.")
    else:
        label = "external" if not display.is_main else "main"
        log(f"Using {label} display {display.display_id}: {display.geometry}")

    root = tk.Tk()
    root.withdraw()
    patch_window = tk.Toplevel(root)
    patch_window.title("SmallHD Live Calibration Patch")
    patch_window.configure(cursor="none")
    patch_window.overrideredirect(True)
    if display is not None:
        patch_window.geometry(display.geometry)
    patch_window.attributes("-topmost", True)
    is_cancelled = _bind_escape_cancel(patch_window)

    def measure(r: float, g: float, b: float) -> tuple[float, float, float]:
        nonlocal count
        count += 1
        rgb8 = tuple(round(max(0.0, min(1.0, v)) * 255.0) for v in (r, g, b))
        color = f"#{rgb8[0]:02x}{rgb8[1]:02x}{rgb8[2]:02x}"
        log(f"[live {count}] Show RGB=({rgb8[0]}, {rgb8[1]}, {rgb8[2]})")
        patch_window.configure(bg=color)
        patch_window.deiconify()
        patch_window.lift()
        patch_window.focus_force()
        root.update()
        time.sleep(settings.settle_seconds)
        root.update()
        if is_cancelled():
            raise AutomationError("Live calibration cancelled from patch window.")

        try:
            reading = read_spotread(command, timeout=settings.timeout)
        except ProbeError as exc:
            raise AutomationError(f"Live measurement failed at RGB={rgb8}: {exc}") from exc
        log(f"  XYZ={reading.xyz[0]:.4f} {reading.xyz[1]:.4f} {reading.xyz[2]:.4f}")
        return reading.xyz

    try:
        characterization = characterize_gray_ramp(
            measure,
            model,
            target_name=target_name,
            target_gamma=target_gamma,
            levels=levels,
            max_iters=settings.max_iters,
            tol=settings.tol,
            gain=settings.gain,
        )
        return LiveCalibrationResult(characterization=characterization, measurement_count=count)
    finally:
        patch_window.destroy()
        root.destroy()


def probe_command(paths: AppPaths) -> ProbeCommand:
    bundled = find_bundled_spotread(paths.resource_root)
    if bundled is None:
        bundled = find_bundled_spotread(paths.user_data_root)
    if bundled is not None:
        return [str(bundled), *SPOTREAD_ARGS]
    return ["spotread", *SPOTREAD_ARGS]
