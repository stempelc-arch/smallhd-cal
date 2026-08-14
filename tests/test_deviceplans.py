import pytest

from smallhd_cal.deviceplans import (
    DEVICE_PLANS,
    STUDIO_NITS,
    device_plan,
    device_plans,
)
from smallhd_cal.presets import get_preset


def test_every_plan_maps_to_a_real_preset_and_has_steps() -> None:
    assert device_plans(), "expected at least one planned device"
    for plan in device_plans():
        preset = get_preset(plan.preset_name)  # raises if unknown
        assert preset.target_name == "rec709"
        assert plan.connection and plan.settings
        assert plan.session_prefix
        # The checklist the GUI gates Start on = connection + settings.
        assert plan.checklist() == [*plan.connection, *plan.settings]


def test_real_devices_carry_detailed_on_monitor_procedures() -> None:
    for key in ("cine7-tx", "cine7-rx", "smallhd-1703"):
        plan = device_plan(key)
        identity = " ".join(plan.identity_steps)
        install = " ".join(plan.install_steps)
        # The non-negotiables of the locked recipe appear in every procedure.
        assert "SmallHD_identity_BMD17.cube" in identity
        # The HDR / Dynamic Range step is addressed in both procedures.
        assert "Dynamic Range" in identity and "Dynamic Range" in install
        # The wizard menu path is the real PageOS Generic Calibration flow.
        assert "Generic Calibration" in identity and "Generic Calibration" in install
        assert "ACTIVATE" in identity
        assert "Verify" in install
        # Range declaration is stated explicitly and identically in both.
        assert "LEGAL" in identity and "LEGAL" in install


def test_tx_and_rx_have_the_probe_brightness_step_matrix_and_gentle_damping() -> None:
    for key in ("cine7-tx", "cine7-rx"):
        plan = device_plan(key)
        assert plan.tune_brightness is True
        assert plan.refine_mode == "matrix"
        assert plan.refine_damping == 0.3
    # 1703/TV opt in later, so they stay on the defaults for now.
    assert device_plan("smallhd-1703").tune_brightness is False


def test_rx_procedures_watch_the_white_level_reproducibility() -> None:
    rx = device_plan("cine7-rx")
    assert any("white nits" in s for s in rx.identity_steps)
    assert any("white nits" in s for s in rx.install_steps)


def test_the_planned_devices_are_present() -> None:
    assert set(DEVICE_PLANS) == {"cine7-tx", "cine7-rx", "smallhd-1703", "studio-tv"}
    # Each real-input path is baked in: RX through wireless, TX/1703 through SDI.
    assert any("wireless" in s.lower() for s in device_plan("cine7-rx").connection)
    assert any("SDI" in s for s in device_plan("cine7-tx").connection)


def test_studio_tv_is_a_placeholder() -> None:
    tv = device_plan("studio-tv")
    assert tv.placeholder is True
    # Real devices are not placeholders.
    for key in ("cine7-tx", "cine7-rx", "smallhd-1703"):
        assert device_plan(key).placeholder is False


def test_every_plan_has_an_accent_and_form() -> None:
    for plan in device_plans():
        assert plan.accent.startswith("#") and len(plan.accent) == 7
        assert plan.form in {"oncamera", "field", "tv"}


def test_settings_encode_the_hard_won_rules() -> None:
    plan = device_plan("cine7-tx")
    settings = " ".join(plan.settings).lower()
    assert "5.5.6" in settings          # verbatim firmware
    assert str(STUDIO_NITS) in settings  # fixed shared luminance
    assert "clean" in settings           # clean converter out
    # The explicit Color Pipe values the operator must set (hardware-confirmed).
    assert "range legal" in settings and "ycc standard rec 709" in settings
    assert "white point d65" in settings and "gamma 2.4" in settings
    # Identity moved from a checkbox to its own setup page (identity_steps);
    # power/warm-up starts the clock at connect time.
    assert plan.identity_steps
    assert any("warm-up" in c or "POWERED" in c for c in plan.connection)


def test_device_plan_rejects_unknown_key() -> None:
    with pytest.raises(ValueError):
        device_plan("nope")
