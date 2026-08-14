"""Bespoke, device-first calibration plans.

Each planned monitor gets a DevicePlan: which preset to calibrate it with, the
exact SIGNAL-PATH connection to make (so the calibration goes through the input
the monitor actually uses on set — SDI or wireless, not bench HDMI), and the
monitor SETTINGS that must be right before characterizing. The GUI turns each
plan into a button on the landing screen and walks the operator through its
connection + settings checklist before starting the standard calibration flow.

This encodes the hard-won operating knowledge (PageOS 5.x verbatim LUTs, fixed
Studio brightness, identity active, clean-SDI converters, calibrate-through-the-
real-path) as concrete per-device steps rather than a generic guide.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Shared luminance target so matched monitors sit at the same brightness. Change
# in one place; every plan references it.
STUDIO_NITS = 100


@dataclass(frozen=True)
class DevicePlan:
    key: str
    label: str            # button text
    subtitle: str         # one line under the button
    preset_name: str
    connection: list[str]  # the real signal path to wire up, in order
    settings: list[str]    # monitor config to verify before characterizing
    session_prefix: str    # tidy auto-name stem
    # Numbered on-monitor procedures the wizard cards display verbatim. These
    # are the device-specific truth — refine the wording here as the workflow
    # is tested on each unit; nothing else needs to change.
    identity_steps: list[str] = field(default_factory=list)  # import + activate the identity LUT
    install_steps: list[str] = field(default_factory=list)   # import + activate a correction LUT, then Verify
    accent: str = "#3b82f6"   # tile accent color
    form: str = "oncamera"    # icon shape: "oncamera" | "field" | "tv"
    placeholder: bool = False  # planned but not yet worked out
    # Refine algorithm. "channel" (per-channel pointwise) is exact for a monitor
    # that applies the LUT verbatim to the same signal we measure — proven on the
    # Cine 7 over DIRECT HDMI. A CONVERTED feed (SDI/wireless) applies the LUT
    # after a YCbCr colour-space conversion, adding a cross-channel white tint
    # that channel mode has no term for and rings on; "matrix" fits a smooth 3x3
    # residual that can correct it. Overridable in the Advanced panel.
    refine_mode: str = "channel"
    # Matrix-refine damping. Full strength (0.5) overshot the white on the Cine 7
    # TX over SDI (v3→v4 rang past D65), so converted chains use a gentler step
    # that lands the white in one round. Only matrix mode uses this. Advanced-
    # overridable.
    refine_damping: float = 0.5
    # Whether setup includes a live-probe step to fine-tune the monitor's
    # brightness slider onto the shared Studio target before characterizing.
    # On for the Cine 7 TX/RX (both have the slider and must match); the 1703
    # and TV can opt in later.
    tune_brightness: bool = False

    def checklist(self) -> list[str]:
        """Every item the operator confirms before Start (connection + settings)."""
        return [*self.connection, *self.settings]


_FIRMWARE = "Monitor on PageOS 5.5.6 (applies LUTs verbatim; avoid 6.3.x)."
_STUDIO = (
    f"Studio brightness set to FIXED (not variable/auto). The exact ~{STUDIO_NITS}-nit level is "
    "fine-tuned on the probe in a LATER step — the New Calibration wizard resets brightness, so "
    "it can't be locked here."
)
_MANUAL = "Manual adjustments / picture controls OFF."
_CLEAN_OUT = "On any monitor used as a converter: SDI OUT set to CLEAN (no LUT / look / overlay / scaling)."
# The per-input Color Pipe decodes the feed BEFORE the calibration LUT, so it is
# part of the characterized chain: set it explicitly (never Auto) and hold it
# byte-identical at characterize, install, and use. Values hardware-confirmed on
# the Cine 7 TX over SDI, 2026-07-13 (Range Legal — explicit Full lifts black).
_COLOR_PIPE = (
    "Color Pipe for the calibration input set EXPLICITLY (nothing on Auto): "
    "Input Type SDR · Gamut Rec 709 · White Point D65 · Gamma 2.4 · Range Legal · "
    "YCC Standard Rec 709. It is part of the calibrated chain — identical at "
    "characterize, install, and use, or the LUT is invalid."
)
# Powering up is part of CONNECTING, not importing: the ≥30 min warm-up clock
# should run while the operator works through settings and the identity import.
_POWER = ("Everything POWERED ON now — the panel needs ≥30 min of warm-up before "
          "measurements you intend to keep, so start the clock first.")

IDENTITY_CUBE = "SmallHD_identity_BMD17.cube"


def _wizard_identity_steps(device: str, feed_range: str, extra: list[str] | None = None) -> list[str]:
    """The PageOS 5.5.6 identity import, spelled out step by step.

    `feed_range` is the range to DECLARE in the wizard for this device's real
    input — it must match what the feed actually carries, and it must be the
    same declaration at identity import, correction import, and use. The
    baseline checksum step catches a wrong choice immediately.
    """
    return [
        f"Insert the SD card this app prepared ({IDENTITY_CUBE} is on it).",
        f"On the {device}: menu → Settings → Calibration → New Calibration → Generic Calibration.",
        "Choose the calibration target Rec 709, then Accept Calibration Target.",
        f"Set the range to {feed_range}. Use the same range on every import this session.",
        (
            f"At the 'Upload LUT stage / bypass mode Profile Display' step, UPLOAD {IDENTITY_CUBE}. "
            "Do NOT choose bypass 'Profile Display' — the baseline must run through the ACTIVE "
            "identity LUT so it sits in the same backlight state as the later verifies."
        ),
        (
            "HDR / Dynamic Range step: this SCALES the panel's output, so it's a real brightness "
            "lever. If the brightness is inconsistent or won't reach target, don't skip — enter "
            "White (max) and Black (min) here (measure with the readout's Save White / Save Black; "
            "black ≈ 0.1 nit) and enter the SAME two values at EVERY import this session, or the "
            "calibration is invalid. Otherwise Skip / Apply factory."
        ),
        "Save Calibration, then ACTIVATE the new calibration entry.",
        (
            "Confirm the Color Pipe still reads the checklist values (SDR / Rec 709 / D65 / 2.4 / "
            f"Range Legal / YCC Rec 709), manual adjustments OFF, Studio brightness fixed at {STUDIO_NITS}."
        ),
        (
            "Checksum (the app's baseline will verify too): black ≈ 0.1 nit at ~1000:1 contrast, "
            "native green y ≈ 0.70. Black lifted to ~0.7–1 nit = wrong range; "
            "green y ≈ 0.60 = a correction LUT is still active instead of the identity."
        ),
        *(extra or []),
    ]


def _wizard_install_steps(device: str, feed_range: str, extra: list[str] | None = None) -> list[str]:
    """The PageOS 5.5.6 correction-LUT import + hardware verify, step by step."""
    return [
        (
            "Do not touch the monitor's brightness, controls, Color Pipe, or input between the "
            "sweep and this import — the LUT is only valid for the exact state it was measured in."
        ),
        (
            "Insert the SD card (the app wrote the correction LUT and removed older ones, "
            "so exactly one .cube is on it)."
        ),
        f"On the {device}: menu → Settings → Calibration → New Calibration → Generic Calibration.",
        "Choose the calibration target Rec 709, then Accept Calibration Target.",
        f"Set the range to {feed_range} — identical to the identity import.",
        (
            "At the 'Upload LUT stage / bypass mode Profile Display' step, UPLOAD the cube named "
            "in the app (not bypass Profile Display)."
        ),
        (
            "HDR / Dynamic Range step: enter the SAME White (max) and Black (min) you used at the "
            "identity import (the app shows them under 'Levels to enter'). If you skipped there, skip "
            "here too — the choice must be identical across the session."
        ),
        "Save Calibration, then ACTIVATE the imported calibration.",
        (
            "MATCH BRIGHTNESS (matched monitors): show white, open “Read peak nits”, and nudge the "
            "fine-tune brightness slider until the FINAL white reads the shared match target — the "
            "SAME nits on every monitor. Each LUT dims luminance differently, so baselines that "
            "matched won't have matching final whites; this is backlight-only and doesn't change "
            "the colour calibration."
        ),
        (
            "Put the probe back on the centre of the panel and click Verify in the app — "
            "the hardware measurement with this LUT active is the only truth; the next "
            "refine (if any) is built from it."
        ),
        *(extra or []),
    ]


# Range declarations per real input. Direct Mac HDMI was hardware-proven FULL
# (2026-07-09 diagnostics); SDI carries legal video, so through a converter the
# expectation is LEGAL — confirm with the black checksum on first test and
# correct HERE if the hardware says otherwise.
_SDI_RANGE = "LEGAL (hardware-confirmed 2026-07-13 on the Cine 7 TX: explicit FULL lifted black to ~0.67 nit / 134:1; Legal restores ~0.1 nit / >=700:1)"
_WIRELESS_RANGE = "LEGAL (the Teradek link carries legal YCbCr — same checksum rule: black must sit ≈ 0.1 nit)"


DEVICE_PLANS: dict[str, DevicePlan] = {
    "cine7-tx": DevicePlan(
        key="cine7-tx",
        label="Cine 7 TX",
        subtitle="Calibrate through SDI (its real input)",
        preset_name="cine7-rec709",
        connection=[
            "Mac → HDMI → 1703 (used as a clean HDMI→SDI converter).",
            "1703 SDI OUT → Cine 7 TX SDI IN.",
            "Probe centered on the Cine 7 TX panel.",
            _POWER,
        ],
        settings=[
            _FIRMWARE, _STUDIO, _MANUAL, _CLEAN_OUT, _COLOR_PIPE,
        ],
        identity_steps=_wizard_identity_steps(
            "Cine 7 TX", _SDI_RANGE,
            extra=[
                (
                    "Converter check: on the 1703, SDI OUT must be CLEAN — no LUT, look, "
                    "overlay, or scaling. A dirty converter shows up as black lift or a "
                    "shrunken gamut in the baseline checksums."
                ),
            ],
        ),
        install_steps=_wizard_install_steps("Cine 7 TX", _SDI_RANGE),
        session_prefix="cine7-tx",
        accent="#3b82f6",
        form="oncamera",
        refine_mode="matrix",  # SDI feed: correct the YCbCr-conversion white tint
        refine_damping=0.3,  # gentle step so the matrix lands white without ringing
        tune_brightness=True,
    ),
    "cine7-rx": DevicePlan(
        key="cine7-rx",
        label="Cine 7 RX",
        subtitle="Calibrate through the wireless link (its real input)",
        preset_name="cine7-rec709",
        connection=[
            "Mac → HDMI → 1703 (clean HDMI→SDI converter).",
            "1703 SDI OUT → standalone Teradek transmitter.",
            "Teradek → wireless → Cine 7 RX (paired, same region).",
            "Probe centered on the Cine 7 RX panel.",
            _POWER,
        ],
        settings=[
            _FIRMWARE, _STUDIO, _MANUAL, _CLEAN_OUT, _COLOR_PIPE,
            "Wireless is passing video (not just paired) before you start.",
        ],
        identity_steps=_wizard_identity_steps(
            "Cine 7 RX", _WIRELESS_RANGE,
            extra=[
                (
                    "Confirm the wireless link is passing VIDEO from the standalone Teradek "
                    "(a paired link with no picture is the known failure mode)."
                ),
                (
                    "RX-specific: right after ACTIVATING the identity, note the white nits "
                    "(app → Read peak nits). Every later import must come back to this same "
                    "white level — a jump (e.g. ~100 → ~80 nits) means the wizard state did "
                    "not reproduce: stop and re-import instead of measuring on."
                ),
            ],
        ),
        install_steps=_wizard_install_steps(
            "Cine 7 RX", _WIRELESS_RANGE,
            extra=[
                (
                    "RX-specific: check the white nits after activation against the value "
                    "noted at identity import BEFORE clicking Verify — a level jump "
                    "invalidates the verify you are about to take."
                ),
            ],
        ),
        session_prefix="cine7-rx",
        accent="#8b5cf6",
        form="oncamera",
        refine_mode="matrix",  # wireless feed: correct the YCbCr-conversion white tint
        refine_damping=0.3,  # gentle step so the matrix lands white without ringing
        tune_brightness=True,
    ),
    "smallhd-1703": DevicePlan(
        key="smallhd-1703",
        label="SmallHD 1703",
        subtitle="Calibrate through SDI (its real input)",
        preset_name="smallhd-1703px-rec709",
        connection=[
            "Mac → HDMI → a second monitor (clean HDMI→SDI converter).",
            "Converter SDI OUT → 1703 SDI IN.",
            "Probe centered on the 1703 panel.",
            _POWER,
        ],
        settings=[
            _FIRMWARE, _STUDIO, _MANUAL, _CLEAN_OUT, _COLOR_PIPE,
        ],
        identity_steps=_wizard_identity_steps(
            "1703", _SDI_RANGE,
            extra=[
                (
                    "The 1703's menus differ from the Cine 7's — note the actual menu path "
                    "here on the first run so this checklist stays exact."
                ),
            ],
        ),
        install_steps=_wizard_install_steps("1703", _SDI_RANGE),
        session_prefix="smallhd-1703",
        accent="#f59e0b",
        form="field",
        refine_mode="matrix",  # SDI feed: correct the YCbCr-conversion white tint
        refine_damping=0.3,  # gentle step so the matrix lands white without ringing
    ),
    "studio-tv": DevicePlan(
        key="studio-tv",
        label="Studio TV",
        subtitle="Not set up yet — placeholder",
        preset_name="bolt500xt-tv-rec709",
        connection=[
            "TBD — define the source → Teradek → TV signal path on Monday.",
        ],
        settings=[
            "TBD — TV picture mode, backlight, and input settings to be worked out.",
        ],
        session_prefix="studio-tv",
        accent="#10b981",
        form="tv",
        placeholder=True,
    ),
}


def device_plan(key: str) -> DevicePlan:
    try:
        return DEVICE_PLANS[key]
    except KeyError as exc:
        raise ValueError(f"Unknown device plan {key!r}; expected one of {list(DEVICE_PLANS)}.") from exc


def device_plans() -> list[DevicePlan]:
    return list(DEVICE_PLANS.values())
