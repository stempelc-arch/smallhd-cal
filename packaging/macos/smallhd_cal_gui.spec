# PyInstaller spec for the SmallHD Calibration GUI.
#
# Build with:
#   .venv/bin/pyinstaller packaging/macos/smallhd_cal_gui.spec
#
# This is intentionally conservative. It packages the Python GUI and project
# data, but code signing/notarization and final ArgyllCMS bundling policy should
# be decided before public distribution.

from pathlib import Path

ROOT = Path.cwd()

datas = [
    (str(ROOT / "measurements" / "patch_sequence_v0.csv"), "measurements"),
    # The session GUI/CLI capture with the v1 patch sequence.
    (str(ROOT / "measurements" / "patch_sequence_v1.csv"), "measurements"),
    # Focused black/low-gray/white set for the Measure Dynamic Range step.
    (str(ROOT / "measurements" / "patch_sequence_dynamic_range.csv"), "measurements"),
    # Minimal baseline (black/white/RGB + short gray ramp) for the fast live flow.
    (str(ROOT / "measurements" / "patch_sequence_live_baseline.csv"), "measurements"),
    (str(ROOT / "measurements" / "patch_sequence_verify_extended.csv"), "measurements"),
    # No bundled identity cubes: the legacy LUT_SIZE-format ones hit the bad
    # firmware parser branch; steps.ensure_identity_lut writes the BMD17 one
    # into the user's luts/ on demand.
]

# Bundle just the ArgyllCMS probe (spotread) plus the reference data it looks
# for at ../ref, not the other 49 unused tools in bin/. find_bundled_spotread()
# globs "<app root>/Argyll*/bin/spotread", so keep the bundled folder name/shape.
# spotread is a self-contained Mach-O linking only system frameworks; it is
# copied as an opaque resource (not analyzed) and re-signed after the build.
#
# Source the x86_64 build to match the x86_64 app (this repo also ships arm64 and
# dead 32-bit i386 copies under different suffixes). x86_64 runs natively on
# Intel and under Rosetta on Apple Silicon, so it pairs with the x86_64 app on
# both. The bundled folder is renamed to a clean "Argyll_V3.5.0".
ARGYLL = ROOT / "Argyll_V3.5.0 3"
datas += [
    (str(ARGYLL / "bin" / "spotread"), "Argyll_V3.5.0/bin"),
    (str(ARGYLL / "bin" / "License.txt"), "Argyll_V3.5.0/bin"),
    (str(ARGYLL / "ref"), "Argyll_V3.5.0/ref"),
    (str(ARGYLL / "usb"), "Argyll_V3.5.0/usb"),
]

block_cipher = None

a = Analysis(
    [str(ROOT / "tools" / "smallhd_cal_gui.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SmallHD Calibration",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="SmallHD Calibration",
)
app = BUNDLE(
    coll,
    name="SmallHD Calibration.app",
    icon=None,
    bundle_identifier="com.smallhdcal.app",
)
