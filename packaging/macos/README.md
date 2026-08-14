# macOS Packaging Notes

Goal: ship SmallHD Calibration as a double-clickable `.app` inside a `.dmg`.

## Intended Build Shape

1. Use PyInstaller to build `SmallHD Calibration.app` from `tools/smallhd_cal_gui.py`.
2. Bundle project defaults such as `measurements/patch_sequence_v0.csv` and starter LUTs.
3. Bundle one compatible ArgyllCMS folder, or keep Argyll external and let the app find it.
4. Use `dmgbuild` or `hdiutil` to create a drag-to-Applications `.dmg`.
5. Later, add code signing and notarization for wider sharing.

In packaged mode, bundled files are treated as read-only resources. Measurements
and generated LUTs should be written under:

```text
~/Documents/SmallHD Calibration/
```

## Recommended Development Install

```bash
.venv/bin/pip install -e ".[dev,packaging]"
```

## Build (app + optional DMG)

Use the build script — it runs PyInstaller, ad-hoc signs the bundle and the
nested `spotread`, then optionally builds the DMG:

```bash
packaging/macos/build_app.sh          # dist/SmallHD Calibration.app
packaging/macos/build_app.sh --dmg    # also dist/SmallHD_Calibration.dmg
```

(Equivalent raw commands: `.venv/bin/pyinstaller packaging/macos/smallhd_cal_gui.spec`
then `.venv/bin/dmgbuild -s packaging/macos/dmgbuild_settings.py "SmallHD Calibration"
dist/SmallHD_Calibration.dmg` — but you must re-sign the nested `spotread` yourself.)

## Architecture

The app is built for the venv's Python architecture. This project's venv is
**x86_64**, so the app and the bundled `spotread` are x86_64: native on Intel,
and under Rosetta on Apple Silicon. For a native arm64 or universal2 app, build
from an arm64 (or universal) Python and bundle a matching `spotread`.

## ArgyllCMS Bundling (done)

The spec bundles only `spotread` (plus `ref/` and `usb/`), not the other ~49
tools, from the **x86_64** Argyll copy in the repo (`Argyll_V3.5.0 3/`; the plain
`Argyll_V3.5.0/` is arm64 and `Argyll_V3.5.0 2/` is dead 32-bit i386). It is
placed at `Argyll_V3.5.0/bin/spotread` inside the app; the app detects
`Argyll*/bin/spotread` under its resource root and picks the arch-matching one.

`spotread` is added as an opaque resource, so PyInstaller does not sign it — the
build script signs it (and the app) ad-hoc afterward. On Apple Silicon an
unsigned nested Mach-O is killed by the OS, so the ad-hoc signature is required
even for local use.

## Building inside an iCloud Drive folder

If the repo lives under `~/Documents` or `~/Desktop` with "Desktop & Documents
Sync" enabled, `codesign` fails with `resource fork, Finder information, or
similar detritus not allowed` — macOS attaches a `com.apple.provenance`
xattr to files in iCloud-synced folders, and `xattr -cr` doesn't stick (the
file provider re-adds it). Build somewhere local instead — e.g. `pyinstaller
--distpath /tmp/smallhd-build/dist --workpath /tmp/smallhd-build/build
packaging/macos/smallhd_cal_gui.spec` — sign there, then copy the finished
`.app`/`.dmg` back; only the *build* needs to happen outside the synced
folder, the finished artifacts are fine to copy in afterward.

## Signing / Gatekeeper

Builds are **ad-hoc signed only** (no Apple Developer ID, no notarization). The
app runs on the build machine. To run it on another Mac, clear the quarantine
attribute after copying it over:

```bash
xattr -dr com.apple.quarantine "/Applications/SmallHD Calibration.app"
```

(or right-click the app → Open the first time). For real distribution, set
`SMALLHD_CODESIGN_IDENTITY` to a Developer ID identity before building, then
notarize and staple the DMG — both need Apple credentials.

## Release Checklist

- Run `pytest` and `ruff`.
- Confirm the GUI opens from source.
- Build the `.app`.
- Launch the `.app` directly.
- Confirm `spotread` detection inside the packaged app.
- Confirm measurements and generated LUTs are written to `~/Documents/SmallHD Calibration/`.
- Capture one black patch and one white patch with a real meter.
- Generate a test correction LUT.
- Build the `.dmg`.
- Mount the `.dmg` and launch the app from it.
- Later: sign and notarize before sharing outside trusted users.
