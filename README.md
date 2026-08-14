# SmallHD Calibration Project

[![CI](https://github.com/stempelc-arch/smallhd-cal/actions/workflows/ci.yml/badge.svg)](https://github.com/stempelc-arch/smallhd-cal/actions/workflows/ci.yml)

VS Code-ready starter project for experimenting with SmallHD-compatible LUT generation, patch playback, and eventual ColorChecker Display Plus measurement.

## Current goal

Build a video-native calibration workflow for SmallHD monitors:

1. Put the SmallHD into its official calibration / unity bypass state.
2. Display controlled RGB patches.
3. Measure patches with the X-Rite / Calibrite ColorChecker Display Plus.
4. Generate a SmallHD-compatible `.cube` LUT.
5. Import the LUT into the monitor from SD card.

This project does **not** require custom firmware at this stage.

## Project notes

- `docs/CALIBRATION_WORKFLOW.md` is the session-based calibration procedure,
  including monitor-side firmware setup and per-model device profiling.
- `docs/MATCHING_WORKFLOWS.md` covers matching modes for SmallHD, Teradek
  receiver + TV chains, and computer monitors.
- `docs/ARCHITECTURE.md` explains the module boundaries and data flow.
- `docs/DEVELOPING.md` explains setup, checks, and extension guidelines.
- `docs/TESTING.md` lists current coverage and future hardware tests.
- `packaging/macos/README.md` tracks the path toward a shareable `.app` and `.dmg`.

## Open in VS Code

1. `git clone https://github.com/stempelc-arch/smallhd-cal.git && cd smallhd-cal`
2. Open `smallhd-cal.code-workspace` in VS Code.
3. Install the recommended Python extensions when prompted.
4. Run the VS Code task: **Create virtual environment**.

Or from Terminal:

```bash
git clone https://github.com/stempelc-arch/smallhd-cal.git
cd smallhd-cal
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

That covers running the app, the CLI tools, and `pytest`/`ruff`. To also build
the macOS `.app`/`.dmg` (see `packaging/macos/README.md`), install the
packaging extras instead: `.venv/bin/pip install -e ".[dev,packaging]"`.

Captured sessions, measurements, device profiles, and generated LUTs
(`sessions/`, `measurements/*.json`, `profiles/`, `luts/`, `exports/`) are
gitignored — this repo ships code, tests, and docs only. A fresh clone starts
with an empty slate for those; `tools/generate_smallhd_luts.py` regenerates
the deterministic reference LUTs (identity + visible-test) used in the
physical test order below.

## Useful VS Code tasks

Open Command Palette → **Tasks: Run Task**:

- `Create virtual environment`
- `Launch GUI`
- `Auto calibrate`
- `Generate SmallHD test LUTs`
- `Generate correction LUT`
- `Analyze measurements`
- `Generate patch sequence CSV`
- `Capture probe measurements`
- `Run tests`

## Useful debug launchers

Open the Run panel in VS Code:

- `SmallHD Calibration GUI`
- `Auto Calibrate`
- `Generate LUTs`
- `Generate Correction LUT`
- `Analyze Measurements`
- `Patch Player`
- `Capture Measurements`
- `Parse Firmware`

## Probe capture

The capture script uses ArgyllCMS `spotread`. It first looks for a bundled
`Argyll*/bin/spotread` inside this project folder and prefers the binary that
matches the current machine architecture. If no bundled copy is found, it falls
back to `spotread` on `PATH`.

This repo doesn't ship ArgyllCMS (it's a large third-party binary, not project
source — see `packaging/macos/README.md` for the bundling policy used when
building the `.app`). For local development with real hardware, either drop an
`Argyll_<version>/` folder (containing `bin/spotread`) into the project root,
or install ArgyllCMS separately and make sure `spotread` is on `PATH`.
Everything else (tests, LUT generation, the GUI without live capture) works
with no probe or ArgyllCMS installed at all.

With the patch player showing each requested color on the SmallHD, run:

```bash
.venv/bin/python tools/capture_measurements.py \
  --csv measurements/patch_sequence_v0.csv \
  --out measurements/measurements_v0.json \
  --resume
```

The script stores normalized input RGB values and measured XYZ readings as JSON.
It writes after every successful patch, so `--resume` can continue an interrupted
session without repeating completed patches.

After capture, summarize the measurement set:

```bash
.venv/bin/python tools/analyze_measurements.py \
  --measurements measurements/measurements_v0.json
```

This prints black/white luminance, contrast ratio, white point xy, D65 xy error,
and estimated grayscale gamma.

## First correction LUT

After capturing measurements, generate a neutral grayscale/gamma correction LUT:

```bash
.venv/bin/python tools/generate_correction_lut.py \
  --measurements measurements/measurements_v0.json \
  --out luts/SmallHD_correction_gray_gamma24_33.cube \
  --gamma 2.4 \
  --size 33
```

This first correction uses grayscale patch luminance only. It maps measured display
response toward a gamma 2.4 EOTF and applies the same correction curve to red,
green, and blue.

## Rec.709 / D65 chromaticity correction

Once the measurement set includes the full red, green, blue, white, and black
patches (all part of `patch_sequence_v0.csv`), generate a LUT that also corrects
the primaries and white point toward Rec.709 / D65:

```bash
.venv/bin/python tools/generate_correction_lut.py \
  --measurements measurements/measurements_v0.json \
  --out luts/SmallHD_correction_rec709_gamma24_33.cube \
  --mode rec709 \
  --gamma 2.4 \
  --size 33
```

This fits the display's native RGB-to-XYZ matrix from the measured primaries,
maps Rec.709 linear RGB into native linear RGB, and encodes through the measured
grayscale response. If D65 white is not reachable at full output, peak luminance
is reduced slightly to keep the white point accurate. `auto_calibrate.py` and the
GUI expose the same choice via `--mode` and the Correction dropdown.

## Physical SmallHD test order

`luts/` is gitignored and starts empty on a fresh clone; generate the
reference LUTs first:

```bash
.venv/bin/python tools/generate_smallhd_luts.py
```

Then start with the safest LUT test:

1. Import `luts/SmallHD_identity_17.cube`
2. Confirm the image does not visibly change.
3. Import `luts/SmallHD_VISIBLE_TEST_redbias_17.cube`
4. Confirm the image visibly shifts red.
5. Remove the visible test LUT or return to identity.

## Graphical UI

Launch the calibration control panel:

```bash
.venv/bin/python tools/smallhd_cal_gui.py
```

The GUI drives the same per-monitor session workflow as
`tools/calibrate_session.py`, in one window:

- Pick an existing session, or create one from a preset (**New from preset…**).
- **Baseline** captures the no-LUT bypass measurement.
- **Generate** builds the first correction LUT for the session's target
  (Rec.709 or DCI-P3) and range.
- Load the LUT on the monitor via SD card, then **Verify** captures it active.
- **Refine** produces the next LUT; repeat verify/refine until converged.
- The iterations table shows per-primary xy error and gray-50 deviation for
  every verify (matching the CLI's `status`), highlighting the selected keeper.
- **Select keeper** marks the best iteration and **Export** copies it to
  `exports/`. **Recheck** re-verifies an iteration without replacing its
  original capture.

During a capture the patch window takes over the external (SmallHD) display;
press `Escape` to cancel. The convergence math and workflow logic live in
`smallhd_cal/report.py` and `smallhd_cal/steps.py`, so both the GUI and CLI
produce identical numbers.

## Fully automated calibration

For the intended setup, connect the SmallHD as an external display to the Mac and
place the probe on the SmallHD. Then run:

```bash
.venv/bin/python tools/auto_calibrate.py
```

The automated runner picks the first non-main macOS display, shows each patch
there, waits briefly for settling, triggers ArgyllCMS `spotread`, saves
measurements after every patch, analyzes the result, and writes the correction
LUT. If no external display is detected, it falls back to the main display.

## Firmware notes found so far

The uploaded PageOS 6 v6.3.4 firmware contains strings indicating support for:

- calibration 1D LUTs
- calibration 3D LUTs
- LUT sizes including `lut17`, `lut33`, and `lut36`
- SmallHD exported LUT headers
- FPGA-backed video pipeline parameters

SmallHD-compatible LUT header observed in firmware strings:

```text
# SmallHD Exported LUT.
#
# Triplets are ordered RGB
# Red changes fastest
# Blue changes slowest
LUT_SIZE 17
```

**Important:** probe verification on real hardware (PageOS 6) showed the importer
actually indexes LUT entries with **blue changing fastest**, despite the exported
header text above. Loading a red-fastest LUT swaps red and blue on screen.
`write_smallhd_cube` therefore defaults to blue-fastest ordering; pass
`index_order="red-fastest"` when writing a `.cube` for standard tools like Resolve.

Probe verification also showed the LUT stage treats values as **legal-range video
(16-235)**: entries are indexed by raw byte position and stored values are expanded
legal-to-full before driving the panel. `generate_correction_lut.py` compensates by
default (`--lut-range legal`); pass `--lut-range full` to write an unwrapped
correction for other consumers.

The range mapping applied at import is not fully consistent between imports, so the
workflow is iterative: load the generated LUT, capture a verify pass with it active,
then run `tools/refine_correction_lut.py --verify <verify.json>` to fit the monitor's
actual LUT application behavior and regenerate. Keep the monitor state identical
(including skipping or not skipping the same import wizard steps) between the verify
capture and the refined LUT's use.

## Next engineering steps

- Verify ArgyllCMS probe capture from the GUI on the calibration machine.
- Validate the first grayscale/gamma correction LUT on the SmallHD.
- Validate the Rec.709 / D65 chromaticity correction LUT on the SmallHD.
- Add multi-monitor matching mode.

The PyInstaller `.app` + `.dmg` build (see `packaging/macos/`) is working and
ad-hoc signed; sign with a real Developer ID and notarize before sharing
outside trusted machines.
