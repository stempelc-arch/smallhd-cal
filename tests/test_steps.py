from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from smallhd_cal.report import (
    BLACK_LEVEL_KEY,
    WHITE_LEVEL_KEY,
    iteration_rows,
    verify_capture_target,
)
from smallhd_cal.session import load_session
from smallhd_cal.steps import (
    create_session_from_preset,
    export_selected,
    generate_lut,
    record_baseline,
    record_verify,
    refine_lut,
    save_probe_level,
    select_iteration,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CINE7 = REPO_ROOT / "sessions" / "cine7-a"
requires_cine7 = pytest.mark.skipif(not CINE7.exists(), reason="cine7-a session not present")


def test_create_from_preset_and_reject_duplicate(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    session_dir = create_session_from_preset(sessions_root, "cine7-x", "cine7-rec709")
    assert (session_dir / "session.json").exists()
    session = load_session(session_dir)
    assert session.target_name == "rec709"
    assert session.model == "SmallHD Cine 7"

    with pytest.raises(ValueError, match="already exists"):
        create_session_from_preset(sessions_root, "cine7-x", "cine7-rec709")
    # force overwrites
    create_session_from_preset(sessions_root, "cine7-x", "cine7-dci-p3", force=True)
    assert load_session(session_dir).target_name == "dci-p3"


def test_create_from_preset_records_device_plan(tmp_path: Path) -> None:
    session_dir = create_session_from_preset(
        tmp_path / "sessions", "cine7-tx-rec709", "cine7-rec709", plan_key="cine7-tx")
    assert load_session(session_dir).chain_state["device_plan"] == "cine7-tx"


def test_generate_requires_baseline(tmp_path: Path) -> None:
    session_dir = create_session_from_preset(tmp_path / "sessions", "cine7-x", "cine7-rec709")
    with pytest.raises(ValueError, match="baseline"):
        generate_lut(session_dir)


@requires_cine7
def test_full_workflow_headless(tmp_path: Path) -> None:
    """generate -> verify -> refine -> verify -> select -> export, end to end."""
    sessions_root = tmp_path / "sessions"
    session_dir = create_session_from_preset(sessions_root, "cine7-test", "cine7-rec709")

    # Use the real Cine 7 baseline capture as this session's baseline.
    baseline_copy = session_dir / "baseline.json"
    shutil.copy2(CINE7 / "baseline.json", baseline_copy)
    record_baseline(session_dir, baseline_copy)

    # 1) Generate the first LUT.
    generate_lut(session_dir, size=17)
    session = load_session(session_dir)
    assert session.current_iteration.index == 1
    assert Path(session.current_iteration.cube_path).exists()

    # 2) Verify v1 (stand in the real v1 capture at the target path).
    target = verify_capture_target(session, session_dir)
    assert target.output_path == session_dir / "verify_v1.json"
    shutil.copy2(CINE7 / "verify_v1.json", target.output_path)
    record_verify(session_dir, target.iteration.index, target.output_path, is_recheck=False)

    rows = {r.label: r for r in iteration_rows(load_session(session_dir))}
    assert rows["1"].has_verify is True
    assert rows["1"].white_err is not None

    # 3) Refine -> v2. (channel mode keeps compensation identity; the drive
    # maps change the LUT, so v2's cube must differ from v1's.)
    lut_v1_bytes = Path(session.current_iteration.cube_path).read_bytes()
    refine_lut(session_dir, size=17, damping=0.5)
    session = load_session(session_dir)
    assert session.current_iteration.index == 2
    assert session.current_iteration.damping == 0.5
    assert Path(session.current_iteration.cube_path).read_bytes() != lut_v1_bytes

    # 4) Verify v2.
    target2 = verify_capture_target(session, session_dir)
    shutil.copy2(CINE7 / "verify_v2.json", target2.output_path)
    record_verify(session_dir, target2.iteration.index, target2.output_path, is_recheck=False)

    # 5) Select v2 as the keeper.
    select_iteration(session_dir, 2)
    assert load_session(session_dir).selected_iteration_index == 2

    # 6) Export.
    message = export_selected(session_dir, tmp_path / "exports")
    exported = tmp_path / "exports" / "cine7-test_smallhd-cine-7_rec709_gamma2p4_v2.cube"
    assert exported.exists()
    assert str(exported) in message


@requires_cine7
def test_recheck_does_not_replace_original_verify(tmp_path: Path) -> None:
    session_dir = create_session_from_preset(tmp_path / "sessions", "cine7-test", "cine7-rec709")
    shutil.copy2(CINE7 / "baseline.json", session_dir / "baseline.json")
    record_baseline(session_dir, session_dir / "baseline.json")
    generate_lut(session_dir, size=17)

    session = load_session(session_dir)
    fresh = verify_capture_target(session, session_dir)
    shutil.copy2(CINE7 / "verify_v1.json", fresh.output_path)
    record_verify(session_dir, 1, fresh.output_path, is_recheck=False)

    session = load_session(session_dir)
    recheck = verify_capture_target(session, session_dir, index=1)
    assert recheck.is_recheck is True
    assert recheck.output_path == session_dir / "verify_v1_recheck_1.json"
    shutil.copy2(CINE7 / "verify_v2.json", recheck.output_path)
    record_verify(session_dir, 1, recheck.output_path, is_recheck=True)

    session = load_session(session_dir)
    assert session.iteration_by_index(1).verify_path == str(fresh.output_path)
    assert session.iteration_by_index(1).verify_rechecks == [str(recheck.output_path)]


def test_save_probe_level_overrides(tmp_path: Path) -> None:
    session_dir = create_session_from_preset(tmp_path / "sessions", "cine7-x", "cine7-rec709")
    save_probe_level(session_dir, "white", 66.3)
    save_probe_level(session_dir, "black", 0.67)
    session = load_session(session_dir)
    assert session.chain_state[WHITE_LEVEL_KEY] == "66.30"
    assert session.chain_state[BLACK_LEVEL_KEY] == "0.67"
    save_probe_level(session_dir, "white", 70.0)
    assert load_session(session_dir).chain_state[WHITE_LEVEL_KEY] == "70.00"


@requires_cine7
def test_record_verify_populates_levels_once(tmp_path: Path) -> None:
    session_dir = create_session_from_preset(tmp_path / "sessions", "cine7-x", "cine7-rec709")
    shutil.copy2(CINE7 / "baseline.json", session_dir / "baseline.json")
    record_baseline(session_dir, session_dir / "baseline.json")
    generate_lut(session_dir, size=17)
    target = verify_capture_target(load_session(session_dir), session_dir)
    shutil.copy2(CINE7 / "verify_v1.json", target.output_path)
    record_verify(session_dir, 1, target.output_path, is_recheck=False)

    session = load_session(session_dir)
    assert WHITE_LEVEL_KEY in session.chain_state
    assert BLACK_LEVEL_KEY in session.chain_state
    # The saved white level matches the verify capture's white luminance.
    from smallhd_cal.measurement import read_measurements_json
    from smallhd_cal.report import levels_from_capture
    white_y, _black_y = levels_from_capture(read_measurements_json(str(target.output_path)))
    assert float(session.chain_state[WHITE_LEVEL_KEY]) == pytest.approx(white_y, abs=0.01)

    # A later verify must not overwrite the established levels (stay consistent
    # so the same numbers are entered on every upload).
    refine_lut(session_dir, size=17)
    target2 = verify_capture_target(load_session(session_dir), session_dir)
    shutil.copy2(CINE7 / "verify_v2.json", target2.output_path)
    record_verify(session_dir, 2, target2.output_path, is_recheck=False)
    assert load_session(session_dir).chain_state[WHITE_LEVEL_KEY] == session.chain_state[WHITE_LEVEL_KEY]


def test_select_rejects_unverified_iteration(tmp_path: Path) -> None:
    session_dir = create_session_from_preset(tmp_path / "sessions", "cine7-x", "cine7-rec709")
    shutil.copy2(CINE7 / "baseline.json", session_dir / "baseline.json") if CINE7.exists() else None
    if not CINE7.exists():
        pytest.skip("cine7-a session not present")
    record_baseline(session_dir, session_dir / "baseline.json")
    generate_lut(session_dir, size=17)
    with pytest.raises(ValueError, match="no verify"):
        select_iteration(session_dir, 1)


def test_refine_lut_accepts_a_hardware_capture_override(tmp_path) -> None:
    """A recheck capture (what the INSTALLED lut really does) can drive refine."""
    import json

    from smallhd_cal import steps
    from smallhd_cal.session import load_session, new_session, save_session

    session_dir = tmp_path / "sessions" / "s"
    save_session(session_dir, new_session("s", target_name="rec709", target_gamma=2.4))

    def write_capture(path, scale):
        payload = {"measurements": [
            {"patch": {"name": n, "r": r, "g": g, "b": b},
             "xyz": [x * scale, y * scale, z * scale], "timestamp": None}
            for n, r, g, b, x, y, z in [
                ("black", 0, 0, 0, 0.05, 0.05, 0.06),
                ("white", 1, 1, 1, 95.0, 100.0, 108.0),
                ("red", 1, 0, 0, 41.0, 21.0, 1.9),
                ("green", 0, 1, 0, 35.0, 71.0, 11.0),
                ("blue", 0, 0, 1, 18.0, 7.0, 95.0),
                ("gray_128", 0.5, 0.5, 0.5, 20.0, 21.0, 23.0),
            ]]}
        path.write_text(json.dumps(payload), encoding="utf-8")

    write_capture(session_dir / "baseline.json", 1.0)
    steps.record_baseline(session_dir, session_dir / "baseline.json")
    steps.generate_lut(session_dir, size=5)

    session = load_session(session_dir)
    v1 = session.iterations[0]
    write_capture(session_dir / "verify_v1.json", 1.0)
    steps.record_verify(session_dir, 1, session_dir / "verify_v1.json", is_recheck=False)
    # A hardware recheck that measured something different from the software verify.
    write_capture(session_dir / "verify_v1_recheck_1.json", 0.92)
    steps.record_verify(session_dir, 1, session_dir / "verify_v1_recheck_1.json", is_recheck=True)

    message = steps.refine_lut(
        session_dir, size=5, verify_path=str(session_dir / "verify_v1_recheck_1.json")
    )
    assert "lut_v2.cube" in message
    assert (session_dir / "lut_v2.cube").exists()
    session = load_session(session_dir)
    v2 = session.iterations[-1]
    assert v2.index == 2
    assert "refined from hardware capture verify_v1_recheck_1.json" in v2.notes
    assert v1.cube_path != v2.cube_path


def test_suggested_session_name_and_delete(tmp_path) -> None:
    from datetime import UTC, datetime

    from smallhd_cal import steps
    from smallhd_cal.session import load_session

    root = tmp_path / "sessions"
    today = f"{datetime.now(UTC):%Y-%m-%d}"

    name1 = steps.suggested_session_name(root, "cine7-rec709")
    assert name1 == f"smallhd-cine-7-rec709-{today}"

    steps.create_session_from_preset(root, name1, "cine7-rec709")
    # A second run the same day disambiguates rather than colliding.
    name2 = steps.suggested_session_name(root, "cine7-rec709")
    assert name2 == f"smallhd-cine-7-rec709-{today}-2"

    assert steps.session_is_finished(root / name1) is False
    load_session  # keep import used

    steps.delete_session(root, name1)
    assert not (root / name1 / "session.json").exists()
    with pytest.raises(ValueError):
        steps.delete_session(root, name1)
