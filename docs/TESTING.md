# Testing Notes

## Existing Test Coverage

189 tests across 18 files in `tests/`, run with `pytest` (see `Run tests` VS
Code task or `.venv/bin/python -m pytest`). Broadly:

- `test_lut.py`, `test_measurement.py`, `test_analysis.py`, `test_probe.py` —
  the low-level building blocks: `.cube` I/O, patch CSV parsing, measurement
  JSON round trips, XYZ/xy conversion, gamma estimation, bundled-ArgyllCMS
  architecture selection.
- `test_calibration.py`, `test_refine.py`, `test_live.py` — the correction
  math: grayscale/Rec.709/DCI-P3 matrix fitting against a simulated
  wide-gamut display, iterative refine convergence, live per-point
  closed-loop convergence.
- `test_session.py`, `test_steps.py`, `test_report.py`, `test_deviceplans.py`
  — session persistence, the generate/verify/refine workflow state machine,
  convergence scoring/readiness warnings, and per-device import checklists.
- `test_sdcard.py`, `test_displays.py`, `test_paths.py`,
  `test_gui_helpers.py`, `test_automation.py` — SD-card transfer logic,
  display enumeration, packaged-app resource-path resolution, and GUI helper
  functions (kept UI-framework-agnostic so they're testable headlessly).
- `test_project_scaffolding.py` — repo layout sanity checks.

## What To Test Next

- Real `spotread` output variants from the ColorChecker Display Plus.
- Chromaticity correction with a nonzero black offset and noisy measurements.

## Hardware Tests

Hardware tests are intentionally manual for now:

1. Confirm the GUI detects the correct `spotread`.
2. Display black and white patches full-screen.
3. Capture both readings.
4. Analyze the measurement JSON.
5. Generate a correction LUT.
6. Import the LUT on the SmallHD and visually confirm it loads.
