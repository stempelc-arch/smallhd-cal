from __future__ import annotations

import json
from pathlib import Path

import pytest

from smallhd_cal.presets import get_preset
from smallhd_cal.report import (
    IterationRow,
    accuracy_label,
    accuracy_percent,
    apply_preset,
    best_verified_iteration,
    convergence_status,
    export_filename,
    iteration_rows,
    iteration_score,
    next_action,
    readiness_warnings,
    verify_capture_target,
)
from smallhd_cal.session import (
    SessionIteration,
    load_session,
    new_session,
    save_session,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CINE7_SESSION = REPO_ROOT / "sessions" / "cine7-a"


def _write_capture(path: Path, xyz_by_name: dict[str, tuple[float, float, float]]) -> None:
    def patch(name: str) -> dict[str, float]:
        if name == "white":
            r = 1.0
        elif name == "black":
            r = 0.0
        elif name.startswith("gray_"):
            r = 0.5
        else:
            r = 1.0
        g = r if name in {"white", "black"} or name.startswith("gray_") else 0.0
        b = g
        if name == "red":
            r, g, b = 1.0, 0.0, 0.0
        elif name == "green":
            r, g, b = 0.0, 1.0, 0.0
        elif name == "blue":
            r, g, b = 0.0, 0.0, 1.0
        return {"name": name, "r": r, "g": g, "b": b}

    payload = {
        "measurements": [
            {"patch": patch(name), "xyz": list(xyz), "timestamp": None}
            for name, xyz in xyz_by_name.items()
        ]
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


# --- Regression against the real, converged Cine 7 session -----------------


@pytest.mark.skipif(not CINE7_SESSION.exists(), reason="cine7-a session not present")
def test_iteration_rows_reproduce_cine7_convergence() -> None:
    session = load_session(CINE7_SESSION)
    rows = {row.label: row for row in iteration_rows(session, root=REPO_ROOT)}

    # v6 is the converged, selected iteration; numbers match the CLI status.
    v6 = rows["6"]
    assert v6.is_selected is True
    assert v6.white_err == pytest.approx(0.0011, abs=5e-4)
    assert v6.red_err == pytest.approx(0.0202, abs=5e-4)
    assert v6.green_err == pytest.approx(0.0126, abs=5e-4)
    assert v6.blue_err == pytest.approx(0.0152, abs=5e-4)
    assert v6.gray50_dev == pytest.approx(0.0062, abs=5e-4)

    # The recheck of v6 reproduced it.
    assert rows["6r1"].is_recheck is True
    assert rows["6r1"].white_err == pytest.approx(0.0018, abs=5e-4)

    # v7 regressed hard and is not selected.
    assert rows["7"].is_selected is False
    assert rows["7"].white_err == pytest.approx(0.0460, abs=1e-3)


# --- Scoring / best-iteration selection -------------------------------------


@pytest.mark.skipif(not CINE7_SESSION.exists(), reason="cine7-a session not present")
def test_best_verified_iteration_picks_converged_plateau_on_cine7() -> None:
    session = load_session(CINE7_SESSION)
    rows = iteration_rows(session, root=REPO_ROOT)
    best = best_verified_iteration(rows)
    assert best is not None
    # v5 and v6 are the converged plateau (dE2000 4.13 vs 4.51, a wash); the
    # perceptual score marginally prefers v5, and both beat v4 and the v7
    # regression. Either is a correct keeper — the point is it is NOT v7.
    assert best.index in (5, 6)


def test_saved_levels_and_levels_from_capture(tmp_path: Path) -> None:
    from smallhd_cal.measurement import Measurement, Patch
    from smallhd_cal.report import levels_from_capture, saved_levels

    session = new_session("m")
    assert saved_levels(session) == (None, None)
    session.update_chain_state({"white_level_nits": "66.30", "black_level_nits": "0.67"})
    assert saved_levels(session) == ("66.30", "0.67")

    caps = [
        Measurement(Patch("white", 1.0, 1.0, 1.0), (60.0, 66.3, 72.0)),
        Measurement(Patch("black", 0.0, 0.0, 0.0), (0.6, 0.67, 0.7)),
        Measurement(Patch("red", 1.0, 0.0, 0.0), (40.0, 20.0, 2.0)),
    ]
    white_y, black_y = levels_from_capture(caps)
    assert (white_y, black_y) == (66.3, 0.67)
    assert levels_from_capture(caps[2:]) is None  # no white/black present


@pytest.mark.skipif(not CINE7_SESSION.exists(), reason="cine7-a session not present")
def test_convergence_status_on_cine7_history() -> None:
    session = load_session(CINE7_SESSION)
    rows = iteration_rows(session, root=REPO_ROOT)

    # Through v6, the detector should call convergence on the v5/v6 plateau —
    # where a human stopped — without firing early during the v2/v3 rough rounds.
    through_v6 = [r for r in rows if r.index <= 6]
    status6 = convergence_status(through_v6)
    assert status6.state == "converged"
    assert status6.best_index in (5, 6)

    # It must NOT declare convergence at the rough early rounds.
    through_v3 = [r for r in rows if r.index <= 3]
    assert convergence_status(through_v3).state != "converged"

    # v7 regressed hard; keep the plateau.
    status7 = convergence_status(rows)
    assert status7.state == "regressed"
    assert status7.best_index in (5, 6)


def test_convergence_status_needs_two_verifies() -> None:
    session = new_session("m", target_name="rec709")
    assert convergence_status(iteration_rows(session)).state == "early"


def test_accuracy_percent_scale() -> None:
    perfect = IterationRow("1", 1, False, False, has_verify=True,
                           white_err=0.0, red_err=0.0, green_err=0.0, blue_err=0.0)
    assert accuracy_percent(perfect) == 100.0
    assert accuracy_label(accuracy_percent(perfect)) == "excellent"

    # A converged Rec.709 result on this panel (white nailed, small primary
    # residuals) should read in the low 90s, and never be None.
    converged = IterationRow("6", 6, False, False, has_verify=True,
                             white_err=0.0011, red_err=0.0202, green_err=0.0126, blue_err=0.0152)
    acc = accuracy_percent(converged)
    assert 88 <= acc <= 93

    # Way off -> low percentage, clamped at 0.
    bad = IterationRow("1", 1, False, False, has_verify=True,
                       white_err=0.06, red_err=0.12, green_err=0.15, blue_err=0.05)
    assert 0.0 <= accuracy_percent(bad) < 40

    assert accuracy_percent(IterationRow("x", 1, False, False, has_verify=False)) is None


def test_iteration_score_none_without_full_errors() -> None:
    assert iteration_score(IterationRow("1", 1, False, False, has_verify=False)) is None
    row = IterationRow("2", 2, False, False, has_verify=True, white_err=0.01,
                       red_err=0.02, green_err=0.03, blue_err=0.04)
    # No full-capture dE available, so it falls back to the primary error mapped
    # onto the unified ~dE2000 scale (×200).
    assert iteration_score(row) == pytest.approx(200.0 * (0.01 + (0.02 + 0.03 + 0.04) / 3))
    # A row carrying a perceptual dE2000 is ranked by that directly.
    perceptual = IterationRow("3", 3, False, False, has_verify=True, white_err=0.001,
                              red_err=0.002, green_err=0.003, blue_err=0.004, mean_de2000=1.8)
    assert iteration_score(perceptual) == pytest.approx(1.8)


# --- Convergence-table shape ------------------------------------------------


def test_iteration_row_without_verify_is_marked_pending(tmp_path: Path) -> None:
    session = new_session("m", target_name="rec709")
    session.add_iteration(SessionIteration(index=1, cube_path="lut_v1.cube"))
    (row,) = iteration_rows(session, root=tmp_path)
    assert row == IterationRow("1", 1, is_recheck=False, is_selected=False, has_verify=False)


def test_iteration_rows_reads_relative_capture_against_root(tmp_path: Path) -> None:
    session = new_session("m", target_name="rec709")
    session.add_iteration(SessionIteration(index=1, cube_path="lut_v1.cube", verify_path="v1.json"))
    _write_capture(
        tmp_path / "v1.json",
        {
            "black": (0.0, 0.0, 0.0),
            "white": (0.9505, 1.0, 1.089),  # D65 white -> ~zero error
            "red": (0.4, 0.2, 0.02),
            "green": (0.3, 0.6, 0.1),
            "blue": (0.18, 0.07, 0.95),
            "gray_128": (0.2, 0.21, 0.23),
        },
    )
    (row,) = iteration_rows(session, root=tmp_path)
    assert row.has_verify is True
    assert row.white_err == pytest.approx(0.0, abs=2e-3)
    assert row.gray50_dev is not None


# --- verify_capture_target --------------------------------------------------


def test_verify_target_fresh_uses_current_iteration(tmp_path: Path) -> None:
    session = new_session("m")
    session.add_iteration(SessionIteration(index=3, cube_path="lut_v3.cube"))
    target = verify_capture_target(session, tmp_path)
    assert target.is_recheck is False
    assert target.output_path == tmp_path / "verify_v3.json"


def test_verify_target_recheck_increments(tmp_path: Path) -> None:
    session = new_session("m")
    it = SessionIteration(index=3, cube_path="lut_v3.cube", verify_path="verify_v3.json")
    it.verify_rechecks.append("verify_v3_recheck_1.json")
    session.add_iteration(it)
    target = verify_capture_target(session, tmp_path, index=3)
    assert target.is_recheck is True
    assert target.output_path == tmp_path / "verify_v3_recheck_2.json"


def test_verify_target_without_iteration_raises(tmp_path: Path) -> None:
    session = new_session("m")
    with pytest.raises(ValueError, match="Generate a LUT"):
        verify_capture_target(session, tmp_path)


# --- next_action ------------------------------------------------------------


def test_next_action_walks_the_workflow(tmp_path: Path) -> None:
    session = new_session("m", target_name="rec709")
    assert "baseline" in next_action(session, root=tmp_path).title.lower()

    baseline = tmp_path / "baseline.json"
    baseline.write_text("{}", encoding="utf-8")
    session.baseline_path = "baseline.json"
    assert "generate" in next_action(session, root=tmp_path).title.lower()

    session.add_iteration(SessionIteration(index=1, cube_path="lut_v1.cube"))
    assert "verify" in next_action(session, root=tmp_path).title.lower()

    session.current_iteration.verify_path = "verify_v1.json"
    assert "select" in next_action(session, root=tmp_path).title.lower()

    session.selected_iteration_index = 1
    assert "export" in next_action(session, root=tmp_path).title.lower()


# --- readiness warnings -----------------------------------------------------


def test_readiness_flags_range_mismatch_and_adjustments() -> None:
    session = new_session("m", device_mode="smallhd")
    session.firmware.manual_adjustments_zeroed = False
    session.firmware.declared_input_range = "full"
    session.firmware.measured_feed_range = "legal"
    warnings = " ".join(readiness_warnings(session))
    assert "Manual adjustments" in warnings
    assert "Declared input range differs" in warnings


# --- export naming and preset application -----------------------------------


def test_export_filename_shape() -> None:
    session = new_session("cine7-a", model="SmallHD Cine 7", target_gamma=2.4, target_name="rec709")
    assert export_filename(session, 6) == "cine7-a_smallhd-cine-7_rec709_gamma2p4_v6.cube"


def test_apply_preset_matches_cli(tmp_path: Path) -> None:
    session = new_session("cine7-p3")
    apply_preset(session, get_preset("cine7-dci-p3"))
    save_session(tmp_path / "cine7-p3", session)
    loaded = load_session(tmp_path / "cine7-p3")
    assert loaded.target_name == "dci-p3"
    assert loaded.target_gamma == 2.6
    assert loaded.device_mode == "smallhd"
    assert loaded.firmware.calibration_target == "DCI P3"
    assert loaded.chain_state["lut_location"] == "SmallHD calibration 3D LUT"


# --- pre-sweep feed range check and live convergence health -----------------


def _levels_capture(white_y: float, black_y: float):
    from smallhd_cal.measurement import Measurement, Patch

    return [
        Measurement(Patch("white", 1, 1, 1), (white_y * 0.95, white_y, white_y * 1.08)),
        Measurement(Patch("black", 0, 0, 0), (black_y * 0.9, black_y, black_y * 1.1)),
    ]


def test_feed_range_warning_fires_on_lifted_black() -> None:
    from smallhd_cal.report import feed_range_warning

    # "end of day" baseline #1 under pipe Range=Full misdecode: white 95, black 0.73.
    warning = feed_range_warning(_levels_capture(95.0, 0.73))
    assert warning is not None
    assert "lifting black" in warning
    assert "Color Pipe RANGE" in warning


def test_feed_range_warning_silent_on_matched_feed() -> None:
    from smallhd_cal.report import feed_range_warning

    # test 7 baseline under a matched legal feed: black ~0.16 nits, ~983:1.
    assert feed_range_warning(_levels_capture(160.3, 0.163)) is None
    assert feed_range_warning([]) is None
    assert feed_range_warning(_levels_capture(0.0, 0.0)) is None


def test_live_health_warning_flags_poor_convergence() -> None:
    from smallhd_cal.report import live_health_warning

    bad = live_health_warning("live closed-loop gray ramp, max_residual=0.1180")
    assert bad is not None
    assert "0.118" in bad
    # A healthy hardware run (test 7 v1) must not nag.
    assert live_health_warning("live ..., measurements=44, max_residual=0.0215") is None
    assert live_health_warning("no metric recorded") is None
    assert live_health_warning("") is None


def test_baseline_identity_warning_fires_on_corrected_panel() -> None:
    from smallhd_cal.measurement import Measurement, Patch
    from smallhd_cal.report import baseline_identity_warning

    def green_capture(xyz):
        return [Measurement(Patch("green", 0, 1, 0), xyz)]

    # rx home test 3: characterization ran on top of the previous LUT — green
    # baselined at ~0.03 from target instead of the native ~0.12+.
    warning = baseline_identity_warning(green_capture((32.7, 59.2, 11.5)))  # xy ~(.317,.572)
    assert warning is not None
    assert "identity calibration" in warning

    # Native wide-gamut green (home rx test baseline) stays silent.
    assert baseline_identity_warning(green_capture((20.46, 69.49, 7.41))) is None  # xy ~(.210,.713)
    assert baseline_identity_warning([]) is None


def test_is_software_verified_distinguishes_signal_space_from_hardware() -> None:
    from smallhd_cal.report import is_software_verified

    sw = SessionIteration(index=2, cube_path="lut_v2.cube", verify_path="verify_v2.json",
                          notes="software signal-space verify (30 patches)")
    assert is_software_verified(sw) is True

    hw = SessionIteration(index=2, cube_path="lut_v2.cube", verify_path="verify_v2.json",
                          notes="")
    assert is_software_verified(hw) is False

    # Once a hardware recheck lands, it is no longer software-only.
    sw_then_hw = SessionIteration(index=2, cube_path="lut_v2.cube", verify_path="verify_v2.json",
                                  verify_rechecks=["verify_v2_recheck_1.json"],
                                  notes="software signal-space verify (30 patches)")
    assert is_software_verified(sw_then_hw) is False

    unverified = SessionIteration(index=3, cube_path="lut_v3.cube")
    assert is_software_verified(unverified) is False


def test_hardware_recheck_outranks_software_prediction(tmp_path: Path) -> None:
    """A 95%-predicted LUT that measures 79% installed must not be 'best'."""
    from smallhd_cal.report import SOFTWARE_VERIFY_MARKER, convergence_status

    session = new_session("m", target_name="rec709")
    session.add_iteration(SessionIteration(
        index=1, cube_path="lut_v1.cube", verify_path="v1.json",
        verify_rechecks=["v1r.json"], notes=SOFTWARE_VERIFY_MARKER,
    ))
    # Software prediction: near-perfect primaries.
    _write_capture(tmp_path / "v1.json", {
        "black": (0.0, 0.0, 0.0), "white": (0.9505, 1.0, 1.089),
        "red": (0.4, 0.2, 0.02), "green": (0.3, 0.6, 0.1), "blue": (0.18, 0.07, 0.95),
    })
    # Hardware truth: green badly desaturated (the RX firmware signature).
    _write_capture(tmp_path / "v1r.json", {
        "black": (0.0, 0.0, 0.0), "white": (0.9505, 1.0, 1.089),
        "red": (0.4, 0.2, 0.02), "green": (0.33, 0.55, 0.2), "blue": (0.18, 0.07, 0.95),
    })
    rows = iteration_rows(session, root=tmp_path)
    software = next(r for r in rows if not r.is_recheck)
    hardware = next(r for r in rows if r.is_recheck)
    assert software.is_software is True
    assert hardware.is_software is False

    best = best_verified_iteration(rows)
    assert best is not None and best.is_recheck is True  # hardware wins

    # With only a software prediction, convergence must not claim success.
    session.iterations[0].verify_rechecks = []
    predicted_rows = iteration_rows(session, root=tmp_path)
    status = convergence_status(predicted_rows)
    assert status.state == "early"
    assert "software prediction" in status.message


def test_hardware_verify_clears_the_software_marker(tmp_path: Path) -> None:
    from smallhd_cal import steps
    from smallhd_cal.report import SOFTWARE_VERIFY_MARKER, is_software_verified
    from smallhd_cal.session import save_session

    session_dir = tmp_path / "s"
    session = new_session("s", target_name="rec709")
    session.add_iteration(SessionIteration(
        index=1, cube_path="lut_v1.cube", verify_path="v1.json",
        notes=f"live sweep | {SOFTWARE_VERIFY_MARKER} (30 patches)",
    ))
    save_session(session_dir, session)
    _write_capture(session_dir / "hw.json", {
        "black": (0.0, 0.0, 0.0), "white": (0.9505, 1.0, 1.089),
        "red": (0.4, 0.2, 0.02), "green": (0.3, 0.6, 0.1), "blue": (0.18, 0.07, 0.95),
    })
    steps.record_verify(session_dir, 1, session_dir / "hw.json", is_recheck=False)
    reloaded = load_session(session_dir).iterations[0]
    assert is_software_verified(reloaded) is False
    assert "live sweep" in reloaded.notes  # other notes survive


def _neutral_axis_measurements(*, tint: bool):
    import numpy as np

    from smallhd_cal.calibration import _rgb_to_xyz_matrix, color_target
    from smallhd_cal.measurement import Measurement, Patch

    target = color_target("rec709")
    matrix = _rgb_to_xyz_matrix(target.primaries_xy, target.white_xy)
    scale = 65.0 / float((matrix @ np.ones(3))[1])

    def ideal(name, r, g, b):
        xyz = matrix @ (np.array([r, g, b]) ** 2.4) * scale
        return Measurement(patch=Patch(name, r, g, b), xyz=tuple(xyz), timestamp="t")

    def neutral(name, v):
        xyz = matrix @ (np.array([v, v, v]) ** 2.4) * scale
        if tint:  # push green down / red+blue up = magenta white, like the SDI run
            xyz = xyz * np.array([1.02, 0.94, 1.05])
        return Measurement(patch=Patch(name, v, v, v), xyz=tuple(xyz), timestamp="t")

    return [
        neutral("white", 1.0),
        neutral("gray_224", 0.878),
        neutral("gray_128", 0.502),
        neutral("gray_048", 0.188),
        ideal("red", 1.0, 0.0, 0.0),
        ideal("green", 0.0, 1.0, 0.0),
        ideal("blue", 0.0, 0.0, 1.0),
        ideal("yellow", 1.0, 1.0, 0.0),
    ]


def test_neutral_axis_warning_flags_tinted_white():
    from smallhd_cal.report import neutral_axis_warning

    warning = neutral_axis_warning(_neutral_axis_measurements(tint=True))
    assert warning is not None
    assert "white dE" in warning
    assert "matrix mode" in warning


def test_neutral_axis_warning_silent_when_neutral_is_clean():
    from smallhd_cal.report import neutral_axis_warning

    assert neutral_axis_warning(_neutral_axis_measurements(tint=False)) is None


def _write_ship_capture(path, *, tint: bool):
    import json

    import numpy as np

    from smallhd_cal.calibration import _rgb_to_xyz_matrix, color_target

    target = color_target("rec709")
    matrix = _rgb_to_xyz_matrix(target.primaries_xy, target.white_xy)
    scale = 65.0 / float((matrix @ np.ones(3))[1])
    # >=20 patches so _mean_de2000 will score it (grays + primaries + mixes).
    grays = [round(v / 255, 6) for v in (0, 16, 48, 80, 128, 176, 224, 255)]
    patches = [("gray_%03d" % round(v * 255), v, v, v) for v in grays]
    patches += [
        ("red", 1, 0, 0), ("green", 0, 1, 0), ("blue", 0, 0, 1),
        ("yellow", 1, 1, 0), ("cyan", 0, 1, 1), ("magenta", 1, 0, 1),
        ("red50", 0.5, 0, 0), ("green50", 0, 0.5, 0), ("blue50", 0, 0, 0.5),
        ("skin_1", 0.72, 0.48, 0.36), ("skin_2", 0.58, 0.36, 0.28),
        ("sky", 0.4, 0.6, 0.9), ("orange", 0.9, 0.55, 0.15),
    ]
    rows = []
    for name, r, g, b in patches:
        xyz = matrix @ (np.array([r, g, b]) ** 2.4) * scale
        if tint and name.startswith("gray") or (tint and name == "white"):
            xyz = xyz * np.array([1.02, 0.94, 1.05])  # magenta neutral tint
        rows.append({"patch": {"name": name, "r": r, "g": g, "b": b},
                     "xyz": list(xyz), "timestamp": "t"})
    path.write_text(json.dumps({"measurements": rows}))


def _rec709_session():
    from smallhd_cal.session import CalibrationSession

    return CalibrationSession(monitor_id="tx", target_name="rec709", target_gamma=2.4)


def test_is_shippable_true_for_clean_capture(tmp_path):
    from smallhd_cal.report import is_shippable

    cap = tmp_path / "clean.json"
    _write_ship_capture(cap, tint=False)
    assert is_shippable(cap, _rec709_session()) is True


def test_is_shippable_false_for_tinted_capture(tmp_path):
    from smallhd_cal.report import is_shippable

    cap = tmp_path / "tinted.json"
    _write_ship_capture(cap, tint=True)
    assert is_shippable(cap, _rec709_session()) is False


def test_brightness_hint_on_and_off_target():
    from smallhd_cal.report import brightness_hint

    on, msg = brightness_hint(102, 100)
    assert on is True and "on target" in msg
    on, msg = brightness_hint(198, 100)
    assert on is False and "LOWER" in msg
    on, msg = brightness_hint(80, 100)
    assert on is False and "RAISE" in msg


def test_luminance_target_warning():
    from smallhd_cal.measurement import Measurement, Patch
    from smallhd_cal.report import luminance_target_warning

    def white(nits):
        return [Measurement(patch=Patch("white", 1, 1, 1), xyz=(0.95 * nits, nits, 1.09 * nits), timestamp="t")]

    assert luminance_target_warning(white(104), 100) is None      # TX, close enough
    assert luminance_target_warning(white(198), 100) is not None  # RX, ~2x off
    assert "198 nits" in luminance_target_warning(white(198), 100)
