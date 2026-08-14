# Calibration Workflow

Session-based workflow for calibrating a SmallHD monitor, integrating the
firmware's own calibration procedure (per the official guide:
https://guide.smallhd.com/a/1810483-calibration).

## Why sessions

Each generate/load/verify cycle is recorded in `sessions/<monitor-id>/session.json`
together with the exact `.cube` file written, the verify capture taken with it,
and the fitted compensation matrix. Refinement reads this recorded state — it
never reconstructs history from code, so fitting improvements can't corrupt
older sessions, and multiple monitors can't clobber each other's files.

## One-time per monitor model: device profiling

Different SmallHD models/firmware may apply LUTs differently (the 1703 PX
indexes blue-fastest and treats values as legal-range). Profile before first
calibration of a new model:

```bash
.venv/bin/python tools/profile_device.py generate --dir profiles/cine7
# copy the three diag_*.cube files to SD, import on the monitor
# for each step, activate the named LUT (or none) and run:
.venv/bin/python tools/profile_device.py capture --dir profiles/cine7 --step nolut
.venv/bin/python tools/profile_device.py capture --dir profiles/cine7 --step identity-full
.venv/bin/python tools/profile_device.py capture --dir profiles/cine7 --step swap-marker
.venv/bin/python tools/profile_device.py capture --dir profiles/cine7 --step identity-legal
.venv/bin/python tools/profile_device.py analyze --dir profiles/cine7
```

The analysis reports index order, whether the LUT stage transforms values, and
whether legal-range content reproduces bypass — and writes `profile.json`.

## Monitor-side setup (firmware wizard)

Before the baseline capture:

1. Warm the panel up for 45 minutes with a signal running.
2. Settings > Display > Calibration > New Calibration > Generic.
3. Select the calibration target. SmallHD's guide recommends DCI P3; use
   Rec.709 / D65 / gamma 2.4 when matching a Rec.709 pipeline.
4. **Input range: match what the source actually sends.** A Mac over HDMI often
   sends legal range even for "full range" content — the profiling step reveals
   this (bypass black stuck well above true panel black = legal feed). A
   declared/actual mismatch here caused most of the 1703 PX debugging pain.
5. Zero / disable all Manual Adjustments (Gamma Shift, RGB Gain, RGB
   Lift/Offset, Saturation).
6. After importing the generated 3D LUT, skip or run "Measure Dynamic Range" —
   but record which, and never change it between captures within a session.

Record all of it in the session:

```bash
.venv/bin/python tools/calibrate_session.py init \
  --dir sessions/cine7-a --monitor cine7-a --model "Cine 7" \
  --device-mode smallhd --target-space rec709 \
  --input-range full --dynamic-range skipped --adjustments-zeroed
```

## Calibration loop

With the probe on the panel and the monitor in the wizard's unity bypass:

```bash
# 1. No-LUT baseline
.venv/bin/python tools/calibrate_session.py baseline --dir sessions/cine7-a

# 2. Optional: if you choose "Measure Dynamic Range" in the SmallHD wizard
.venv/bin/python tools/calibrate_session.py dynamic-range --dir sessions/cine7-a

# 3. First correction LUT
.venv/bin/python tools/calibrate_session.py generate --dir sessions/cine7-a

# 4. Copy lut_v1.cube to SD, import + activate on the monitor, then:
.venv/bin/python tools/calibrate_session.py verify --dir sessions/cine7-a

# 5. Next iteration (repeat verify/refine until converged)
.venv/bin/python tools/calibrate_session.py refine --dir sessions/cine7-a

# Convergence table at any time:
.venv/bin/python tools/calibrate_session.py status --monitor cine7-a
```

Stop when successive verifies differ by roughly ±0.002 xy — that is the
monitor's import-to-import variability floor, and further iterations chase
noise. On the 1703 PX this took the initial LUT plus 3-4 refinements.

Once a LUT is confirmed, mark it as the selected calibration for that monitor:

```bash
.venv/bin/python tools/calibrate_session.py select --monitor cine7-a --index 6
```

The selected LUT is separate from the latest experiment, so a later bad import
or test iteration does not hide the last known-good calibration.

To switch between monitor models or see what is currently selected:

```bash
.venv/bin/python tools/calibrate_session.py list
```

To ask the session what to do next:

```bash
.venv/bin/python tools/calibrate_session.py next-step --monitor cine7-a
```

To preflight every saved monitor/session:

```bash
.venv/bin/python tools/calibrate_session.py doctor --all
```

Link the model/device profile once so preflight checks remember it:

```bash
.venv/bin/python tools/calibrate_session.py link-profile \
  --monitor cine7-a \
  --profile profiles/cine7/profile.json
```

To prepare the selected LUT for SD-card transfer:

```bash
.venv/bin/python tools/calibrate_session.py export-selected --monitor cine7-a --out exports
```

To refresh all selected LUT exports:

```bash
.venv/bin/python tools/calibrate_session.py export-selected --all --out exports
```

Before a capture pass, run the measure-stage preflight. This checks setup state
without requiring a selected keeper LUT yet:

```bash
.venv/bin/python tools/calibrate_session.py doctor \
  --monitor cine7-a \
  --stage measure
```

Before exporting or trusting a calibration, run the export-stage preflight
(this is the default). It requires a selected LUT with verify evidence:

```bash
.venv/bin/python tools/calibrate_session.py doctor \
  --monitor cine7-a
```

## Rules that keep a calibration valid

- The monitor state during captures (input range, dynamic-range choice, manual
  adjustments, calibration mode) must not change afterwards — the LUT corrects
  the exact chain that was measured.
- The source's output range must not change either (rerun profiling if the
  signal chain changes).
- Verify captures must be taken with the session's latest LUT active; the
  refinement assumes it.

## Known 1703 PX (PageOS 6) behaviors

- Imports index `.cube` entries blue-fastest despite exported headers saying
  red-fastest.
- LUT values are treated as legal-range video; content within the legal span
  behaved consistently across imports, full-span content did not.
- Import behavior varies slightly between loads; color updates are damped
  (default 0.5) so refinement converges instead of ringing.
