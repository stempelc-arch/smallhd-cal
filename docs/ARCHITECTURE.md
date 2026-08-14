# Architecture Notes

SmallHD Calibration is split into small modules so the GUI, command-line tools,
and tests all use the same calibration logic.

## Modules

- `smallhd_cal.measurement`
  - Patch CSV parsing.
  - Measurement JSON read/write.
  - Duplicate measurement handling by patch name.
- `smallhd_cal.probe`
  - ArgyllCMS `spotread` discovery.
  - Architecture-aware bundled `spotread` selection.
  - Probe command execution and XYZ parsing.
- `smallhd_cal.analysis`
  - Black/white luminance summary.
  - Contrast ratio, white point xy, D65 xy error, and estimated gamma.
- `smallhd_cal.calibration`
  - Grayscale luminance correction curve.
  - Neutral RGB transform used for first correction LUTs.
- `smallhd_cal.lut`
  - SmallHD-compatible `.cube` writer.
  - RGB triplet order: red fastest, blue slowest.
- `smallhd_cal.gui`
  - Tkinter control panel.
  - File selection, patch preview, patch measurement, analysis, and LUT generation.
- `smallhd_cal.paths`
  - Separates bundled read-only resources from writable user output paths.
  - Uses the source tree as both roots in development.
  - Uses `sys._MEIPASS` plus `~/Documents/SmallHD Calibration/` in packaged mode.
- `smallhd_cal.displays`
  - Uses macOS CoreGraphics to find connected display bounds.
  - Picks the first non-main display for automated SmallHD patch playback.
- `smallhd_cal.automation`
  - Runs unattended patch display, probe capture, measurement saving, analysis,
    and LUT generation.

## Tool Entry Points

- `tools/smallhd_cal_gui.py`
  - Launches the GUI.
- `tools/capture_measurements.py`
  - Command-line probe capture with `--resume`.
- `tools/analyze_measurements.py`
  - Command-line measurement summary.
- `tools/generate_correction_lut.py`
  - Command-line grayscale/gamma correction LUT generation.
- `tools/generate_smallhd_luts.py`
  - Generates identity and visible test LUTs.

## Data Flow

```text
patch_sequence_v0.csv
  -> GUI, auto_calibrate.py, or capture_measurements.py
  -> spotread XYZ readings
  -> measurements_v0.json in the writable user data folder
  -> analyze_measurements.py
  -> generate_correction_lut.py
  -> SmallHD .cube LUT
```

## Measurement JSON Contract

Measurement files use this shape:

```json
{
  "measurements": [
    {
      "patch": {"name": "gray_128", "r": 0.5019, "g": 0.5019, "b": 0.5019},
      "xyz": [18.2, 19.1, 20.7],
      "timestamp": "2026-07-02T12:00:00+00:00"
    }
  ]
}
```

Patch RGB values are normalized `0..1` floats. GUI swatches and OpenCV patch
playback convert those values to `0..255` only at display time.

## Current Calibration Scope

The first generated correction LUT is intentionally neutral:

- Uses grayscale patch luminance only.
- Targets gamma 2.4 by default.
- Applies the same correction curve to R, G, and B.

This is a useful first stage, but it is not full Rec.709/D65 color-volume
correction yet. Future chromaticity work should add tests before changing the
LUT transform.
