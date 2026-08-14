#!/usr/bin/env bash
#
# Build (and optionally package) the SmallHD Calibration macOS app.
#
#   packaging/macos/build_app.sh            # build dist/SmallHD Calibration.app
#   packaging/macos/build_app.sh --dmg      # also build dist/SmallHD_Calibration.dmg
#
# Signing: ad-hoc ("-") by default, which needs no Apple account and is enough
# to run locally on Apple Silicon (unsigned nested Mach-O binaries are killed by
# the OS). For real distribution, export a Developer ID identity first:
#
#   SMALLHD_CODESIGN_IDENTITY="Developer ID Application: You (TEAMID)" \
#     packaging/macos/build_app.sh --dmg
#
# then notarize/staple the .dmg separately (needs your Apple credentials).
set -euo pipefail

cd "$(dirname "$0")/../.."
VENV=".venv/bin"
SIGN_ID="${SMALLHD_CODESIGN_IDENTITY:--}"
APP="dist/SmallHD Calibration.app"
SPOTREAD="$APP/Contents/Resources/Argyll_V3.5.0/bin/spotread"

echo ">> PyInstaller build"
"$VENV/pyinstaller" --noconfirm --clean packaging/macos/smallhd_cal_gui.spec

if [[ ! -x "$SPOTREAD" ]]; then
  # datas usually preserve the +x bit; restore it if not.
  chmod +x "$SPOTREAD"
fi

echo ">> Code signing (identity: $SIGN_ID)"
# Sign inside-out: the nested probe first, then the whole bundle so its seal
# covers the added binary.
codesign --force --timestamp=none --sign "$SIGN_ID" "$SPOTREAD"
codesign --force --deep --timestamp=none --sign "$SIGN_ID" "$APP"
codesign --verify --deep --strict "$APP"
echo ">> codesign verify OK"

if [[ "${1:-}" == "--dmg" ]]; then
  echo ">> dmgbuild"
  rm -f dist/SmallHD_Calibration.dmg
  "$VENV/dmgbuild" -s packaging/macos/dmgbuild_settings.py \
    "SmallHD Calibration" dist/SmallHD_Calibration.dmg
  echo ">> dist/SmallHD_Calibration.dmg"
fi

echo ">> Done: $APP"
