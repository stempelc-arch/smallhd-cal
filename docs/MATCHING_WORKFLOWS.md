# Matching Workflows

The project is moving toward calibrating complete viewing chains, not only one
SmallHD monitor. A session records two separate ideas:

- `device_mode`: where the correction LUT lives.
- `target_name`: the color target the display should match.

Because this is a personal tool, prefer presets for known devices over generic
setup screens:

```bash
.venv/bin/python tools/calibrate_session.py list-presets
.venv/bin/python tools/calibrate_session.py quickstart \
  --monitor cine7-p3 \
  --preset cine7-dci-p3
```

Apply a preset to an existing session:

```bash
.venv/bin/python tools/calibrate_session.py apply-preset \
  --monitor cine7-a \
  --preset cine7-rec709
```

When switching between devices, ask the session for the next useful command:

```bash
.venv/bin/python tools/calibrate_session.py next-step --monitor cine7-a
```

Supported target names:

- `rec709`: Rec.709 primaries, D65 white, gamma 2.4 by default.
- `p3-d65`: P3 primaries, D65 white, gamma 2.4 by default.
- `dci-p3`: P3 primaries, DCI white, gamma 2.6 by default.

Supported device modes:

- `smallhd`: LUT is imported through the SmallHD calibration workflow.
- `teradek_receiver_tv`: LUT is intended to live at the receiver/TV part of a
  wireless monitoring chain. The source, receiver, TV picture mode, and HDMI
  range must stay fixed between baseline, verify, and use.
- `computer_monitor`: LUT/profile workflow for a directly attached computer
  display. The OS display profile, graphics output range, brightness, and any
  display picture modes must stay fixed.

Record chain details with:

```bash
.venv/bin/python tools/calibrate_session.py set-chain-state \
  --monitor cine7-a \
  --set lut_location="SmallHD calibration 3D LUT" \
  --set source_format="1080p23.98 legal"
```

Use `doctor --stage measure` before captures, when the session may not have a
baseline or selected LUT yet. Use plain `doctor` before exporting or trusting a
keeper LUT.

## SmallHD

SmallHD's calibration guide says to warm up for 45 minutes, create a new
calibration profile, select a target, choose input range, profile in unity
bypass, import the generated 3D LUT, then either measure dynamic range or skip
to factory measurements before saving.

Use `device_mode=smallhd`. The official guide recommends DCI P3 as the
calibration target, but Rec.709 remains useful when the monitor is matching a
Rec.709 video pipeline.

```bash
.venv/bin/python tools/calibrate_session.py init \
  --dir sessions/cine7-p3 \
  --monitor cine7-p3 \
  --model "SmallHD Cine 7" \
  --device-mode smallhd \
  --target-space dci-p3 \
  --input-range full \
  --dynamic-range skipped \
  --adjustments-zeroed
```

Record whether the dynamic range step was `skipped` or `measured`, and never
mix the two inside one session.

If you choose to measure dynamic range, capture it before generating/refining
the correction LUT:

```bash
.venv/bin/python tools/calibrate_session.py dynamic-range \
  --monitor cine7-p3
```

This records `dynamic_range.json`, sets the session dynamic-range step to
`measured`, and lets `doctor` report black luminance, white luminance, and
contrast.

## Teradek Receiver To TV

Use `device_mode=teradek_receiver_tv` when the correction LUT is for a TV fed
through a Teradek receiver. Treat the receiver, TV picture mode, HDMI range, and
source output format as part of the display. If any part changes, start a new
session or rerun profiling.

For the Teradek Bolt 500 XT chain, start from one of the personal presets:

```bash
.venv/bin/python tools/calibrate_session.py quickstart \
  --monitor bolt500xt-tv-rec709 \
  --preset bolt500xt-tv-rec709
```

For exploring a matching DCI-P3 TV mode:

```bash
.venv/bin/python tools/calibrate_session.py quickstart \
  --monitor bolt500xt-tv-p3 \
  --preset bolt500xt-tv-dci-p3
```

Both presets already record `Samsung UN75TU700DF` as the TV model, but still
leave the picture mode and signal-chain details as `TBD`; fill them before
measuring:

```bash
.venv/bin/python tools/calibrate_session.py set-chain-state \
  --monitor bolt500xt-tv-p3 \
  --set tv_picture_mode="TV picture mode" \
  --set hdmi_range="full or legal" \
  --set source_format="1080p23.98 or actual source" \
  --set lut_location="receiver, TV, or source" \
  --set tv_color_space="auto, native, or custom" \
  --set tv_gamma_setting="BT.1886, 2.4, or actual setting" \
  --set tv_backlight_setting="fixed setting" \
  --set hdr_state="off" \
  --set eco_settings="off" \
  --set motion_processing="off" \
  --set receiver_output_format="actual Bolt RX output"
```

```bash
.venv/bin/python tools/calibrate_session.py init \
  --dir sessions/livingroom-tv-teradek \
  --monitor livingroom-tv-teradek \
  --model "LG OLED via Teradek RX" \
  --device-mode teradek_receiver_tv \
  --target-space rec709 \
  --input-range full \
  --dynamic-range skipped \
  --adjustments-zeroed
```

Then record the chain:

```bash
.venv/bin/python tools/calibrate_session.py set-chain-state \
  --monitor livingroom-tv-teradek \
  --set receiver_model="Teradek receiver model" \
  --set tv_model="TV model" \
  --set tv_picture_mode="Filmmaker" \
  --set hdmi_range="full" \
  --set source_format="1080p23.98" \
  --set lut_location="receiver"
```

For most SDR TV matching, start with `rec709`. Use `p3-d65` only when the whole
signal path and viewing target are intentionally P3.

## Computer Monitor

Use `device_mode=computer_monitor` for a display connected directly to the
computer. Record the OS display profile, brightness, display preset, Night
Shift/True Tone/HDR state, refresh mode, and graphics output range in session
notes. The LUT location may eventually be an ICC/video-card LUT or an app LUT,
so the session should say where the correction is applied.

```bash
.venv/bin/python tools/calibrate_session.py init \
  --dir sessions/edit-monitor \
  --monitor edit-monitor \
  --model "Reference GUI Monitor" \
  --device-mode computer_monitor \
  --target-space rec709 \
  --input-range full \
  --dynamic-range skipped \
  --adjustments-zeroed
```

Then record the chain:

```bash
.venv/bin/python tools/calibrate_session.py set-chain-state \
  --monitor edit-monitor \
  --set os_profile="profile name" \
  --set brightness="120 nits setting" \
  --set hdr_state="off" \
  --set true_tone="off" \
  --set night_shift="off" \
  --set display_preset="reference preset" \
  --set lut_location="OS/app LUT"
```
