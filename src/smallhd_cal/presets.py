from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CalibrationPreset:
    name: str
    model: str
    device_mode: str
    target_name: str
    target_gamma: float
    calibration_target: str
    declared_input_range: str
    measured_feed_range: str
    dynamic_range_step: str
    manual_adjustments_zeroed: bool
    profile_path: str | None = None
    chain_state: dict[str, str] = field(default_factory=dict)
    notes: str = ""


PRESETS = {
    "cine7-rec709": CalibrationPreset(
        name="cine7-rec709",
        model="SmallHD Cine 7",
        device_mode="smallhd",
        target_name="rec709",
        target_gamma=2.4,
        calibration_target="Generic Rec.709",
        declared_input_range="legal",
        measured_feed_range="legal",
        dynamic_range_step="skipped",
        manual_adjustments_zeroed=True,
        profile_path="profiles/cine7/profile.json",
        chain_state={
            "lut_location": "SmallHD calibration 3D LUT",
            "source_format": "SDI feed into the TX (legal range 4-1019)",
            "color_pipe": "SDR / Rec709 / D65 / gamma 2.4 / Range Legal / YCC Rec709",
        },
        notes=(
            "Cine 7 TX in its real SDI working configuration. Hardware-confirmed "
            "2026-07-13: the SDI feed is LEGAL range — explicit Full lifted black "
            "to ~0.67 nit / 134:1; Legal restores ~0.1 nit / >=700:1. Set the "
            "Color Pipe to SDR/Rec709/D65/2.4/Range Legal/YCC Rec709 and declare "
            "Legal Range in the wizard. Standard-format (LUT_3D_SIZE) BMD import is "
            "deterministic and red-fastest. (The earlier Mac-HDMI diagnosis of a "
            "FULL feed / red-fastest full-domain was for HDMI and is superseded "
            "for the TX over SDI.)"
        ),
    ),
    "cine7-dci-p3": CalibrationPreset(
        name="cine7-dci-p3",
        model="SmallHD Cine 7",
        device_mode="smallhd",
        target_name="dci-p3",
        target_gamma=2.6,
        calibration_target="DCI P3",
        declared_input_range="legal",
        measured_feed_range="legal",
        dynamic_range_step="skipped",
        manual_adjustments_zeroed=True,
        profile_path="profiles/cine7/profile.json",
        chain_state={
            "lut_location": "SmallHD calibration 3D LUT",
            "source_format": "SDI feed into the TX (legal range 4-1019)",
            "color_pipe": "SDR / Rec709 / D65 / Range Legal / YCC Rec709 (pipe stays Rec709; wizard target is DCI P3)",
        },
        notes=(
            "SmallHD guide recommends DCI P3 as the calibration target. Reuse "
            "only with the same SDI Cine 7 chain — the feed is LEGAL range "
            "(hardware-confirmed 2026-07-13; declaring Full lifts black)."
        ),
    ),
    "smallhd-1703px-rec709": CalibrationPreset(
        name="smallhd-1703px-rec709",
        model="SmallHD 1703 P3X",
        device_mode="smallhd",
        target_name="rec709",
        target_gamma=2.4,
        calibration_target="Generic Rec.709",
        declared_input_range="full",
        measured_feed_range="full",
        dynamic_range_step="skipped",
        manual_adjustments_zeroed=True,
        profile_path="profiles/smallhd-1703px/profile.json",
        chain_state={
            "lut_location": "SmallHD calibration 3D LUT",
            "source_format": "Mac HDMI full-range feed, monitor declared Full (matched)",
        },
        notes=(
            "Personal preset for the migrated 1703 PX chain. Standard BMD-format "
            "LUTs import red-fastest, raw full-domain; declare Full on load. "
            "(Legacy-format cubes hit a blue-fastest range-guessing parser — "
            "avoid.) Re-run the BMD diagnostics on this unit before trusting."
        ),
    ),
    "bolt500xt-tv-rec709": CalibrationPreset(
        name="bolt500xt-tv-rec709",
        model="TV via Teradek Bolt 500 XT",
        device_mode="teradek_receiver_tv",
        target_name="rec709",
        target_gamma=2.4,
        calibration_target="Rec.709",
        declared_input_range="full",
        measured_feed_range="unknown",
        dynamic_range_step="skipped",
        manual_adjustments_zeroed=True,
        profile_path=None,
        chain_state={
            "receiver_model": "Teradek Bolt 500 XT",
            "tv_model": "Samsung UN75TU700DF",
            "tv_picture_mode": "TBD",
            "hdmi_range": "TBD",
            "source_format": "TBD",
            "lut_location": "TBD",
            "tv_color_space": "TBD",
            "tv_gamma_setting": "TBD",
            "tv_backlight_setting": "TBD",
            "hdr_state": "TBD",
            "eco_settings": "TBD",
            "motion_processing": "TBD",
            "receiver_output_format": "TBD",
        },
        notes=(
            "Personal placeholder for a TV fed by the Teradek Bolt 500 XT. "
            "Fill in picture mode, HDMI range, source format, processing state, "
            "and where the correction LUT is applied before measuring."
        ),
    ),
    "bolt500xt-tv-dci-p3": CalibrationPreset(
        name="bolt500xt-tv-dci-p3",
        model="TV via Teradek Bolt 500 XT",
        device_mode="teradek_receiver_tv",
        target_name="dci-p3",
        target_gamma=2.6,
        calibration_target="DCI P3",
        declared_input_range="full",
        measured_feed_range="unknown",
        dynamic_range_step="skipped",
        manual_adjustments_zeroed=True,
        profile_path=None,
        chain_state={
            "receiver_model": "Teradek Bolt 500 XT",
            "tv_model": "Samsung UN75TU700DF",
            "tv_picture_mode": "TBD",
            "hdmi_range": "TBD",
            "source_format": "TBD",
            "lut_location": "TBD",
            "tv_color_space": "TBD",
            "tv_gamma_setting": "TBD",
            "tv_backlight_setting": "TBD",
            "hdr_state": "TBD",
            "eco_settings": "TBD",
            "motion_processing": "TBD",
            "receiver_output_format": "TBD",
        },
        notes=(
            "Experimental matching preset for exploring DCI-P3 on a TV fed by "
            "the Teradek Bolt 500 XT. Confirm the TV can be held in a stable "
            "wide-gamut/P3 mode before trusting this for matching."
        ),
    ),
}


def preset_names() -> tuple[str, ...]:
    return tuple(sorted(PRESETS))


def get_preset(name: str) -> CalibrationPreset:
    try:
        return PRESETS[name]
    except KeyError as exc:
        raise ValueError(f"Unknown preset {name!r}; expected one of {preset_names()}.") from exc
