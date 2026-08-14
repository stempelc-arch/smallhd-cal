from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from smallhd_cal.live import PanelModel, characterize_gray_ramp
from smallhd_cal.lut import read_smallhd_cube, squeeze_legal, write_smallhd_cube
from smallhd_cal.measurement import Measurement, Patch, write_measurements_json
from smallhd_cal.presets import get_preset, preset_names
from smallhd_cal.refine import refine_step
from smallhd_cal.session import (
    CHAIN_STATE_RECOMMENDED_FIELDS,
    CHAIN_STATE_REQUIRED_FIELDS,
    SessionIteration,
    discover_session_summaries,
    load_session,
    new_session,
    save_session,
    summarize_session,
)
from tests.test_calibration import _simulate_display, _synthetic_display_measurements
from tests.test_live import make_panel, synthetic_baseline
from tests.test_refine import _MIX, _distortion


def _load_calibrate_session_module():
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)
    return calibrate_session


def test_cube_round_trip_and_trilinear(tmp_path: Path) -> None:
    def transform(r: float, g: float, b: float) -> tuple[float, float, float]:
        return r * 0.9, g * 0.8 + 0.1, b * 0.5 + 0.25

    path = tmp_path / "t.cube"
    write_smallhd_cube(path, 17, transform)
    lut = read_smallhd_cube(path)

    assert lut.size == 17
    # Grid nodes are exact; between-node lookups are trilinear.
    assert lut.lookup(0.5, 0.25, 1.0) == pytest.approx(transform(0.5, 0.25, 1.0), abs=1e-9)
    assert lut.lookup(0.33, 0.71, 0.02) == pytest.approx(transform(0.33, 0.71, 0.02), abs=1e-3)


def test_cube_round_trip_red_fastest(tmp_path: Path) -> None:
    def transform(r: float, g: float, b: float) -> tuple[float, float, float]:
        return r, g * 0.5, b

    path = tmp_path / "rf.cube"
    write_smallhd_cube(path, 5, transform, index_order="red-fastest")
    lut = read_smallhd_cube(path, index_order="red-fastest")
    assert lut.lookup(1.0, 1.0, 0.0) == pytest.approx((1.0, 0.5, 0.0), abs=1e-9)


def test_session_round_trip(tmp_path: Path) -> None:
    session = new_session(
        "cine7-a",
        model="Cine 7",
        target_gamma=2.6,
        target_name="dci-p3",
        device_mode="teradek_receiver_tv",
    )
    session.firmware.declared_input_range = "full"
    session.baseline_path = "sessions/cine7-a/baseline.json"
    session.dynamic_range_path = "sessions/cine7-a/dynamic_range.json"
    session.add_iteration(
        SessionIteration(index=1, cube_path="sessions/cine7-a/lut_v1.cube", damping=0.5)
    )

    save_session(tmp_path, session)
    loaded = load_session(tmp_path)

    assert loaded.monitor_id == "cine7-a"
    assert loaded.target_gamma == 2.6
    assert loaded.target_name == "dci-p3"
    assert loaded.device_mode == "teradek_receiver_tv"
    loaded.update_chain_state({"tv_picture_mode": "Filmmaker"})
    assert loaded.chain_state["tv_picture_mode"] == "Filmmaker"
    assert loaded.firmware.declared_input_range == "full"
    assert loaded.dynamic_range_path == "sessions/cine7-a/dynamic_range.json"
    assert loaded.current_iteration.cube_path == "sessions/cine7-a/lut_v1.cube"
    assert loaded.next_index() == 2
    assert np.allclose(loaded.current_iteration.compensation, np.eye(3))


def test_personal_presets_include_known_smallhd_devices() -> None:
    assert "cine7-rec709" in preset_names()
    assert "smallhd-1703px-rec709" in preset_names()
    assert "bolt500xt-tv-rec709" in preset_names()
    assert "bolt500xt-tv-dci-p3" in preset_names()
    assert get_preset("cine7-dci-p3").target_name == "dci-p3"
    assert get_preset("bolt500xt-tv-rec709").chain_state["tv_model"] == "Samsung UN75TU700DF"
    assert get_preset("bolt500xt-tv-dci-p3").chain_state["receiver_model"] == "Teradek Bolt 500 XT"
    assert get_preset("bolt500xt-tv-dci-p3").chain_state["tv_model"] == "Samsung UN75TU700DF"


def test_get_preset_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown preset"):
        get_preset("unknown")


def test_init_command_can_use_preset(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "cine7-p3"
    calibrate_session.cmd_init(
        SimpleNamespace(
            dir=str(session_dir),
            monitor="cine7-p3",
            model="",
            preset="cine7-dci-p3",
            gamma=None,
            target_space="rec709",
            device_mode="smallhd",
            target="Generic Rec.709",
            input_range="unknown",
            dynamic_range="skipped",
            adjustments_zeroed=False,
        )
    )
    loaded = load_session(session_dir)

    assert loaded.model == "SmallHD Cine 7"
    assert loaded.target_name == "dci-p3"
    assert loaded.target_gamma == 2.6
    assert loaded.profile_path == "profiles/cine7/profile.json"
    assert loaded.chain_state["lut_location"] == "SmallHD calibration 3D LUT"


def test_quickstart_command_creates_session_from_preset(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    sessions_root = tmp_path / "sessions"
    calibrate_session.cmd_quickstart(
        SimpleNamespace(
            monitor="cine7-p3",
            preset="cine7-dci-p3",
            root=str(sessions_root),
            force=False,
        )
    )
    loaded = load_session(sessions_root / "cine7-p3")

    assert loaded.monitor_id == "cine7-p3"
    assert loaded.model == "SmallHD Cine 7"
    assert loaded.target_name == "dci-p3"
    assert loaded.profile_path == "profiles/cine7/profile.json"
    assert loaded.chain_state["source_format"] == "SDI feed into the TX (legal range 4-1019)"


def test_quickstart_command_refuses_existing_session(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    sessions_root = tmp_path / "sessions"
    save_session(sessions_root / "cine7-p3", new_session("cine7-p3"))

    with pytest.raises(SystemExit, match="Session already exists"):
        calibrate_session.cmd_quickstart(
            SimpleNamespace(
                monitor="cine7-p3",
                preset="cine7-dci-p3",
                root=str(sessions_root),
                force=False,
            )
        )


def test_next_step_asks_for_missing_required_chain_state() -> None:
    calibrate_session = _load_calibrate_session_module()
    session = new_session(
        "bolt500xt-tv-p3",
        model="TV via Teradek Bolt 500 XT",
        device_mode="teradek_receiver_tv",
    )
    session.update_chain_state({"receiver_model": "Teradek Bolt 500 XT"})

    step = calibrate_session._next_step(session)

    assert step.title == "fill chain-state details"
    assert "set-chain-state --monitor bolt500xt-tv-p3" in step.command


def test_next_step_tracks_capture_and_lut_lifecycle(tmp_path: Path) -> None:
    calibrate_session = _load_calibrate_session_module()
    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.firmware.measured_feed_range = "legal"
    session.update_chain_state({
        "lut_location": "SmallHD calibration 3D LUT",
        "source_format": "1080p23.98 legal",
    })

    step = calibrate_session._next_step(session)
    assert step.title == "capture baseline"
    assert step.command.endswith("baseline --monitor cine7-a")

    baseline = tmp_path / "baseline.json"
    baseline.write_text('{"measurements": []}\n', encoding="utf-8")
    session.baseline_path = str(baseline)

    step = calibrate_session._next_step(session)
    assert step.title == "generate first LUT"
    assert step.command.endswith("generate --monitor cine7-a")

    session.add_iteration(SessionIteration(index=1, cube_path=str(tmp_path / "lut_v1.cube")))
    step = calibrate_session._next_step(session)
    assert step.title == "verify LUT v1"
    assert step.command.endswith("verify --monitor cine7-a")

    session.current_iteration.verify_path = str(tmp_path / "verify_v1.json")
    step = calibrate_session._next_step(session)
    assert step.title == "select keeper LUT v1"
    assert step.command.endswith("select --monitor cine7-a --index 1")

    session.select_iteration(1, selected_at="2026-07-07T18:00:00+00:00")
    step = calibrate_session._next_step(session)
    assert step.title == "export selected LUT"
    assert step.command.endswith("export-selected --monitor cine7-a --out exports")


def test_next_step_command_prints_recommendation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calibrate_session = _load_calibrate_session_module()
    session_dir = tmp_path / "sessions" / "cine7-a"
    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.update_chain_state({
        "lut_location": "SmallHD calibration 3D LUT",
        "source_format": "1080p23.98 legal",
    })
    save_session(session_dir, session)

    calibrate_session.cmd_next_step(
        SimpleNamespace(dir=str(session_dir), monitor=None, root="sessions")
    )

    output = capsys.readouterr().out
    assert "Next step for cine7-a: capture baseline" in output


def test_live_generate_command_records_characterization_and_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calibrate_session = _load_calibrate_session_module()
    session_dir = tmp_path / "sessions" / "cine7-a"
    session = new_session("cine7-a", model="SmallHD Cine 7")
    baseline_path = session_dir / "baseline.json"
    measure = make_panel()
    baseline = synthetic_baseline(measure)
    write_measurements_json(baseline_path, baseline)
    session.baseline_path = str(baseline_path)
    save_session(session_dir, session)

    model = PanelModel.from_baseline(baseline)
    characterization = characterize_gray_ramp(
        measure,
        model,
        target_name="rec709",
        target_gamma=2.4,
        levels=np.linspace(0.0, 1.0, 5),
    )

    def fake_live(*_args, **_kwargs):
        return SimpleNamespace(characterization=characterization, measurement_count=12)

    monkeypatch.setattr(calibrate_session, "run_live_gray_characterization", fake_live)
    calibrate_session.cmd_live_generate(
        SimpleNamespace(
            dir=str(session_dir),
            monitor=None,
            root="sessions",
            levels=5,
            settle=1,
            timeout=2.0,
            max_iters=3,
            tol=0.01,
            gain=0.8,
            lut_range="full",
            size=17,
        )
    )

    loaded = load_session(session_dir)
    assert loaded.current_iteration.index == 1
    assert "live closed-loop gray ramp" in loaded.current_iteration.notes
    assert "lut-range=full" in loaded.current_iteration.notes
    assert (session_dir / "live_v1.json").exists()
    lut = read_smallhd_cube(session_dir / "lut_v1.cube")
    assert lut.lookup(0.0, 0.0, 0.0) == pytest.approx((0.0, 0.0, 0.0), abs=0.003)


def test_dynamic_range_command_records_capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "cine7-a"
    save_session(session_dir, new_session("cine7-a", model="SmallHD Cine 7"))

    def fake_capture(_paths, settings) -> None:
        write_measurements_json(
            settings.measurements_json,
            [
                Measurement(Patch("black", 0.0, 0.0, 0.0), (0.001, 0.002, 0.003)),
                Measurement(Patch("gray_064", 0.25, 0.25, 0.25), (4.5, 5.0, 5.4)),
                Measurement(Patch("gray_128", 0.5, 0.5, 0.5), (20.0, 21.0, 22.0)),
                Measurement(Patch("white", 1.0, 1.0, 1.0), (95.0, 100.0, 108.0)),
            ],
        )

    monkeypatch.setattr(calibrate_session, "run_patch_capture", fake_capture)

    calibrate_session.cmd_dynamic_range(
        SimpleNamespace(
            dir=str(session_dir),
            monitor=None,
            root="sessions",
            csv="measurements/patch_sequence_dynamic_range.csv",
            out=None,
            resume=False,
        )
    )
    loaded = load_session(session_dir)

    assert loaded.firmware.dynamic_range_step == "measured"
    assert loaded.dynamic_range_path == str(session_dir / "dynamic_range.json")
    assert (session_dir / "dynamic_range.json").exists()


def test_session_iteration_lookup_and_recheck_round_trip(tmp_path: Path) -> None:
    session = new_session("cine7-a", model="Cine 7")
    session.add_iteration(
        SessionIteration(
            index=1,
            cube_path="sessions/cine7-a/lut_v1.cube",
            verify_rechecks=["sessions/cine7-a/verify_v1_recheck_1.json"],
        )
    )
    session.add_iteration(SessionIteration(index=2, cube_path="sessions/cine7-a/lut_v2.cube"))

    save_session(tmp_path, session)
    loaded = load_session(tmp_path)

    assert loaded.iteration_by_index(1).cube_path == "sessions/cine7-a/lut_v1.cube"
    assert loaded.iteration_by_index(3) is None
    assert loaded.iteration_by_index(1).verify_rechecks == [
        "sessions/cine7-a/verify_v1_recheck_1.json"
    ]


def test_select_iteration_requires_verify_and_round_trips(tmp_path: Path) -> None:
    session = new_session("cine7-a", model="Cine 7")
    session.add_iteration(SessionIteration(index=1, cube_path="sessions/cine7-a/lut_v1.cube"))
    session.add_iteration(
        SessionIteration(
            index=2,
            cube_path="sessions/cine7-a/lut_v2.cube",
            verify_rechecks=["sessions/cine7-a/verify_v2_recheck_1.json"],
        )
    )

    with pytest.raises(ValueError, match="no verify capture"):
        session.select_iteration(1)

    selected = session.select_iteration(2, selected_at="2026-07-07T18:00:00+00:00")
    assert selected.cube_path == "sessions/cine7-a/lut_v2.cube"

    save_session(tmp_path, session)
    loaded = load_session(tmp_path)

    assert loaded.selected_iteration_index == 2
    assert loaded.selected_iteration.cube_path == "sessions/cine7-a/lut_v2.cube"
    assert loaded.selected_at == "2026-07-07T18:00:00+00:00"


def test_select_iteration_rejects_missing_iteration() -> None:
    session = new_session("cine7-a", model="Cine 7")

    with pytest.raises(ValueError, match="No iteration 99"):
        session.select_iteration(99)


def test_verify_index_records_recheck_without_replacing_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session = new_session("cine7-a", model="Cine 7")
    session.add_iteration(
        SessionIteration(
            index=1,
            cube_path=str(tmp_path / "lut_v1.cube"),
            verify_path=str(tmp_path / "verify_v1.json"),
        )
    )
    session.add_iteration(
        SessionIteration(
            index=2,
            cube_path=str(tmp_path / "lut_v2.cube"),
            verify_path=str(tmp_path / "verify_v2.json"),
        )
    )
    save_session(tmp_path, session)

    captured_paths = []

    def fake_capture(_paths, settings) -> None:
        captured_paths.append(settings.measurements_json)

    monkeypatch.setattr(calibrate_session, "run_patch_capture", fake_capture)
    monkeypatch.setattr(calibrate_session, "_print_report", lambda _session: None)

    calibrate_session.cmd_verify(SimpleNamespace(dir=str(tmp_path), index=1, out=None))
    loaded = load_session(tmp_path)

    assert captured_paths == [str(tmp_path / "verify_v1_recheck_1.json")]
    assert loaded.iteration_by_index(1).verify_path == str(tmp_path / "verify_v1.json")
    assert loaded.iteration_by_index(1).verify_rechecks == [
        str(tmp_path / "verify_v1_recheck_1.json")
    ]
    assert loaded.current_iteration.verify_path == str(tmp_path / "verify_v2.json")


def test_select_command_marks_keeper(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session = new_session("cine7-a", model="Cine 7")
    session.add_iteration(
        SessionIteration(
            index=6,
            cube_path=str(tmp_path / "lut_v6.cube"),
            verify_rechecks=[str(tmp_path / "verify_v6_recheck_1.json")],
        )
    )
    save_session(tmp_path, session)

    calibrate_session.cmd_select(SimpleNamespace(dir=str(tmp_path), index=6))
    loaded = load_session(tmp_path)

    assert loaded.selected_iteration_index == 6
    assert loaded.selected_iteration.cube_path == str(tmp_path / "lut_v6.cube")


def test_summarize_session_reports_selected_and_current(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "cine7-a"
    session = new_session("cine7-a", model="Cine 7")
    session.add_iteration(
        SessionIteration(
            index=1,
            cube_path="sessions/cine7-a/lut_v1.cube",
            verify_path="sessions/cine7-a/verify_v1.json",
        )
    )
    session.add_iteration(
        SessionIteration(
            index=2,
            cube_path="sessions/cine7-a/lut_v2.cube",
            verify_path="sessions/cine7-a/verify_v2.json",
        )
    )
    session.select_iteration(1, selected_at="2026-07-07T18:00:00+00:00")
    session.link_profile("profiles/cine7/profile.json")
    save_session(session_dir, session)

    summary = summarize_session(session_dir)

    assert summary.session_dir == str(session_dir)
    assert summary.monitor_id == "cine7-a"
    assert summary.model == "Cine 7"
    assert summary.target_name == "rec709"
    assert summary.device_mode == "smallhd"
    assert summary.selected_iteration_index == 1
    assert summary.selected_cube_path == "sessions/cine7-a/lut_v1.cube"
    assert summary.current_iteration_index == 2
    assert summary.current_cube_path == "sessions/cine7-a/lut_v2.cube"
    assert summary.profile_path == "profiles/cine7/profile.json"


def test_discover_session_summaries_sorts_sessions(tmp_path: Path) -> None:
    sessions_root = tmp_path / "sessions"
    first = new_session("b-monitor", model="B")
    first.add_iteration(
        SessionIteration(index=1, cube_path="sessions/b-monitor/lut_v1.cube", verify_path="v1.json")
    )
    first.select_iteration(1, selected_at="2026-07-07T18:00:00+00:00")
    save_session(sessions_root / "b-monitor", first)

    second = new_session("a-monitor", model="A")
    second.add_iteration(SessionIteration(index=1, cube_path="sessions/a-monitor/lut_v1.cube"))
    save_session(sessions_root / "a-monitor", second)

    summaries = discover_session_summaries(sessions_root)

    assert [summary.monitor_id for summary in summaries] == ["a-monitor", "b-monitor"]
    assert summaries[0].selected_iteration_index is None
    assert summaries[1].selected_iteration_index == 1


def test_list_command_prints_session_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    sessions_root = tmp_path / "sessions"
    session = new_session("cine7-a", model="Cine 7")
    session.add_iteration(
        SessionIteration(
            index=6,
            cube_path="sessions/cine7-a/lut_v6.cube",
            verify_path="sessions/cine7-a/verify_v6.json",
        )
    )
    session.select_iteration(6, selected_at="2026-07-07T18:00:00+00:00")
    session.link_profile("profiles/cine7/profile.json")
    save_session(sessions_root / "cine7-a", session)

    calibrate_session.cmd_list(SimpleNamespace(root=str(sessions_root)))
    output = capsys.readouterr().out

    assert "monitor" in output
    assert "cine7-a" in output
    assert "yes" in output
    assert "v6" in output
    assert "sessions/cine7-a/lut_v6.cube" in output


def test_resolve_session_dir_accepts_monitor_id(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "cine7-a"
    save_session(session_dir, new_session("cine7-a", model="SmallHD Cine 7"))

    resolved = calibrate_session._resolve_session_dir(
        SimpleNamespace(dir=None, monitor="cine7-a", root=str(tmp_path / "sessions"))
    )

    assert resolved == session_dir


def test_resolve_session_dir_rejects_unknown_monitor(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    with pytest.raises(SystemExit, match="No session found"):
        calibrate_session._resolve_session_dir(
            SimpleNamespace(dir=None, monitor="missing", root=str(tmp_path / "sessions"))
        )


def test_link_profile_command_records_existing_profile(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "cine7-a"
    profile = tmp_path / "profiles" / "cine7" / "profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("{}\n", encoding="utf-8")
    save_session(session_dir, new_session("cine7-a", model="SmallHD Cine 7"))

    calibrate_session.cmd_link_profile(
        SimpleNamespace(dir=str(session_dir), profile=str(profile))
    )
    loaded = load_session(session_dir)

    assert loaded.profile_path == str(profile)


def test_link_profile_command_rejects_missing_profile(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "cine7-a"
    save_session(session_dir, new_session("cine7-a", model="SmallHD Cine 7"))

    with pytest.raises(SystemExit, match="Profile does not exist"):
        calibrate_session.cmd_link_profile(
            SimpleNamespace(dir=str(session_dir), profile=str(tmp_path / "missing.json"))
        )


def test_apply_preset_command_updates_existing_session(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "smallhd-1703px"
    save_session(session_dir, new_session("smallhd-1703px"))

    calibrate_session.cmd_apply_preset(
        SimpleNamespace(dir=str(session_dir), monitor=None, root="sessions", preset="smallhd-1703px-rec709")
    )
    loaded = load_session(session_dir)

    assert loaded.model == "SmallHD 1703 P3X"
    assert loaded.profile_path == "profiles/smallhd-1703px/profile.json"
    assert loaded.chain_state["source_format"] == "Mac HDMI full-range feed, monitor declared Full (matched)"


def test_set_chain_state_command_records_key_values(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "livingroom-tv"
    save_session(
        session_dir,
        new_session(
            "livingroom-tv",
            model="LG OLED via Teradek",
            device_mode="teradek_receiver_tv",
        ),
    )

    calibrate_session.cmd_set_chain_state(
        SimpleNamespace(
            dir=str(session_dir),
            set=[
                "receiver_model=Bolt 6 RX",
                "tv_picture_mode=Filmmaker",
                "lut_location=receiver",
            ],
        )
    )
    loaded = load_session(session_dir)

    assert loaded.chain_state["receiver_model"] == "Bolt 6 RX"
    assert loaded.chain_state["tv_picture_mode"] == "Filmmaker"
    assert loaded.chain_state["lut_location"] == "receiver"


def test_parse_chain_state_updates_rejects_bad_item() -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    with pytest.raises(SystemExit, match="Expected KEY=VALUE"):
        calibrate_session._parse_chain_state_updates(["not-a-pair"])


def test_export_selected_command_copies_selected_lut(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "cine7-a"
    source = session_dir / "lut_v6.cube"
    source.parent.mkdir(parents=True)
    source.write_text("LUT_SIZE 2\n", encoding="utf-8")

    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.add_iteration(
        SessionIteration(
            index=6,
            cube_path=str(source),
            verify_path=str(session_dir / "verify_v6.json"),
        )
    )
    session.select_iteration(6, selected_at="2026-07-07T18:00:00+00:00")
    save_session(session_dir, session)

    out_dir = tmp_path / "exports"
    calibrate_session.cmd_export_selected(SimpleNamespace(dir=str(session_dir), out=str(out_dir)))

    exported = out_dir / "cine7-a_smallhd-cine-7_rec709_gamma2p4_v6.cube"
    assert exported.read_text(encoding="utf-8") == "LUT_SIZE 2\n"


def test_export_selected_command_accepts_monitor_id(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / "cine7-a"
    source = session_dir / "lut_v6.cube"
    source.parent.mkdir(parents=True)
    source.write_text("LUT_SIZE 2\n", encoding="utf-8")

    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.add_iteration(
        SessionIteration(index=6, cube_path=str(source), verify_path=str(session_dir / "v6.json"))
    )
    session.select_iteration(6, selected_at="2026-07-07T18:00:00+00:00")
    save_session(session_dir, session)

    out_dir = tmp_path / "exports"
    calibrate_session.cmd_export_selected(
        SimpleNamespace(dir=None, monitor="cine7-a", root=str(sessions_root), out=str(out_dir))
    )

    assert (out_dir / "cine7-a_smallhd-cine-7_rec709_gamma2p4_v6.cube").exists()


def test_export_selected_all_exports_every_selected_lut(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    sessions_root = tmp_path / "sessions"
    for monitor, model, iteration in (
        ("cine7-a", "SmallHD Cine 7", 6),
        ("smallhd-1703px", "SmallHD 1703 P3X", 1),
    ):
        session_dir = sessions_root / monitor
        source = session_dir / f"lut_v{iteration}.cube"
        source.parent.mkdir(parents=True)
        source.write_text(f"{monitor}\n", encoding="utf-8")
        session = new_session(monitor, model=model)
        session.add_iteration(
            SessionIteration(index=iteration, cube_path=str(source), verify_path="verify.json")
        )
        session.select_iteration(iteration, selected_at="2026-07-07T18:00:00+00:00")
        save_session(session_dir, session)

    out_dir = tmp_path / "exports"
    calibrate_session.cmd_export_selected(
        SimpleNamespace(all=True, dir=None, monitor=None, root=str(sessions_root), out=str(out_dir))
    )

    assert (out_dir / "cine7-a_smallhd-cine-7_rec709_gamma2p4_v6.cube").exists()
    assert (out_dir / "smallhd-1703px_smallhd-1703-p3x_rec709_gamma2p4_v1.cube").exists()


def test_export_selected_all_exits_on_failed_session(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    sessions_root = tmp_path / "sessions"
    save_session(sessions_root / "missing-selection", new_session("missing-selection"))

    with pytest.raises(SystemExit) as exc:
        calibrate_session.cmd_export_selected(
            SimpleNamespace(
                all=True,
                dir=None,
                monitor=None,
                root=str(sessions_root),
                out=str(tmp_path / "exports"),
            )
        )

    assert exc.value.code == 1


def test_export_selected_command_requires_selected_lut(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "cine7-a"
    session = new_session("cine7-a", model="SmallHD Cine 7")
    save_session(session_dir, session)

    with pytest.raises(SystemExit, match="No selected LUT"):
        calibrate_session.cmd_export_selected(SimpleNamespace(dir=str(session_dir), out=str(tmp_path)))


def test_doctor_checks_clean_session_with_profile(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "cine7-a"
    baseline = session_dir / "baseline.json"
    lut = session_dir / "lut_v1.cube"
    profile = tmp_path / "profiles" / "cine7" / "profile.json"
    baseline.parent.mkdir(parents=True)
    profile.parent.mkdir(parents=True)
    baseline.write_text('{"measurements": []}\n', encoding="utf-8")
    lut.write_text("LUT_SIZE 2\n", encoding="utf-8")
    profile.write_text("{}\n", encoding="utf-8")

    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.baseline_path = str(baseline)
    session.firmware.manual_adjustments_zeroed = True
    session.firmware.declared_input_range = "legal"
    session.firmware.measured_feed_range = "legal"
    session.firmware.notes = "Profiled and warmed up."
    session.update_chain_state({
        "lut_location": "SmallHD calibration 3D LUT",
        "source_format": "1080p23.98 legal",
    })
    session.link_profile(profile)
    session.add_iteration(
        SessionIteration(index=1, cube_path=str(lut), verify_path=str(session_dir / "verify_v1.json"))
    )
    session.select_iteration(1, selected_at="2026-07-07T18:00:00+00:00")
    save_session(session_dir, session)

    checks = calibrate_session._doctor_checks(session, session_dir, profile)

    assert not [message for severity, message in checks if severity == "FAIL"]
    assert ("PASS", f"Profile exists: {profile}") in checks
    assert ("PASS", "Chain state complete for smallhd.") in checks


def test_chain_state_checks_warn_for_missing_mode_fields() -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session = new_session(
        "livingroom-tv",
        model="LG OLED via Teradek",
        device_mode="teradek_receiver_tv",
    )
    session.update_chain_state({"receiver_model": "Bolt 6 RX"})

    checks = calibrate_session._chain_state_checks(session)

    assert CHAIN_STATE_REQUIRED_FIELDS["teradek_receiver_tv"]
    assert any(
        severity == "WARN" and "tv_picture_mode" in message and "lut_location" in message
        for severity, message in checks
    )
    assert any(
        severity == "WARN" and "tv_color_space" in message and "eco_settings" in message
        for severity, message in checks
    )


def test_chain_state_checks_treat_tbd_as_missing() -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session = new_session(
        "livingroom-tv",
        model="TV via Teradek Bolt 500 XT",
        device_mode="teradek_receiver_tv",
    )
    session.update_chain_state({
        "receiver_model": "Teradek Bolt 500 XT",
        "tv_model": "TBD",
        "tv_picture_mode": "TBD",
        "hdmi_range": "TBD",
        "source_format": "TBD",
        "lut_location": "TBD",
    })

    checks = calibrate_session._chain_state_checks(session)

    assert any(severity == "WARN" and "tv_model" in message for severity, message in checks)


def test_chain_state_checks_pass_when_recommended_fields_are_recorded() -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session = new_session(
        "bolt500xt-tv-p3",
        model="TV via Teradek Bolt 500 XT",
        device_mode="teradek_receiver_tv",
    )
    session.update_chain_state({
        "receiver_model": "Teradek Bolt 500 XT",
        "tv_model": "Samsung UN75TU700DF",
        "tv_picture_mode": "Movie",
        "hdmi_range": "full",
        "source_format": "1080p23.98",
        "lut_location": "source",
        "tv_color_space": "native or auto",
        "tv_gamma_setting": "BT.1886 or 2.4",
        "tv_backlight_setting": "fixed",
        "hdr_state": "off",
        "eco_settings": "off",
        "motion_processing": "off",
        "receiver_output_format": "1080p23.98",
    })

    checks = calibrate_session._chain_state_checks(session)

    assert CHAIN_STATE_RECOMMENDED_FIELDS["teradek_receiver_tv"]
    assert ("PASS", "Chain state complete for teradek_receiver_tv.") in checks
    assert ("PASS", "Recommended chain state recorded for teradek_receiver_tv.") in checks


def test_dynamic_range_checks_warn_when_measured_without_path() -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.firmware.dynamic_range_step = "measured"

    checks = calibrate_session._dynamic_range_checks(session)

    assert checks == [("WARN", "Dynamic-range step is measured but no capture path is recorded.")]


def test_dynamic_range_checks_fail_for_missing_capture(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.firmware.dynamic_range_step = "measured"
    session.dynamic_range_path = str(tmp_path / "missing_dynamic_range.json")

    checks = calibrate_session._dynamic_range_checks(session)

    assert checks == [("FAIL", f"Dynamic-range capture is missing: {session.dynamic_range_path}")]


def test_dynamic_range_checks_summarize_capture(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    dynamic_range_path = tmp_path / "dynamic_range.json"
    write_measurements_json(
        dynamic_range_path,
        [
            Measurement(Patch("black", 0.0, 0.0, 0.0), (0.001, 0.002, 0.003)),
            Measurement(Patch("gray_064", 0.25, 0.25, 0.25), (4.5, 5.0, 5.4)),
            Measurement(Patch("gray_128", 0.5, 0.5, 0.5), (20.0, 21.0, 22.0)),
            Measurement(Patch("white", 1.0, 1.0, 1.0), (95.0, 100.0, 108.0)),
        ],
    )
    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.firmware.dynamic_range_step = "measured"
    session.dynamic_range_path = str(dynamic_range_path)

    checks = calibrate_session._dynamic_range_checks(session)

    assert checks == [(
        "PASS",
        "Dynamic range measured: black Y 0.0020, white Y 100.00, contrast 50000:1.",
    )]


def test_doctor_uses_linked_profile_when_no_override(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    profile = tmp_path / "profiles" / "cine7" / "profile.json"
    profile.parent.mkdir(parents=True)
    profile.write_text("{}\n", encoding="utf-8")
    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.link_profile(profile)

    assert calibrate_session._doctor_profile_path(session, None) == profile
    assert calibrate_session._doctor_profile_path(session, "override.json") == Path("override.json")


def test_doctor_checks_profile_consistency(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    profile = tmp_path / "profile.json"
    profile.write_text(
        '{"index_order": "blue-fastest", "identity_legal_reproduces_bypass": true}\n',
        encoding="utf-8",
    )
    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.add_iteration(
        SessionIteration(index=1, cube_path="lut_v1.cube", cube_index_order="blue-fastest")
    )
    session.selected_iteration_index = 1

    checks = calibrate_session._profile_consistency_checks(session, profile)

    assert ("PASS", "Profile index order matches selected LUT: blue-fastest") in checks
    assert ("PASS", "Profile says legal-range identity reproduces bypass.") in checks


def test_doctor_checks_profile_index_order_mismatch_fails(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    profile = tmp_path / "profile.json"
    profile.write_text('{"index_order": "red-fastest"}\n', encoding="utf-8")
    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.add_iteration(
        SessionIteration(index=1, cube_path="lut_v1.cube", cube_index_order="blue-fastest")
    )
    session.selected_iteration_index = 1

    checks = calibrate_session._profile_consistency_checks(session, profile)

    assert any(severity == "FAIL" and "differs from selected LUT" in message
               for severity, message in checks)


def test_doctor_checks_missing_selected_lut_fails(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.baseline_path = str(tmp_path / "baseline.json")
    session.add_iteration(
        SessionIteration(
            index=1,
            cube_path=str(tmp_path / "missing.cube"),
            verify_path=str(tmp_path / "verify_v1.json"),
        )
    )
    session.selected_iteration_index = 1

    checks = calibrate_session._doctor_checks(session, tmp_path, None)

    assert ("FAIL", f"Selected LUT is missing: {tmp_path / 'missing.cube'}") in checks
    assert any(severity == "FAIL" and "Baseline is missing" in message for severity, message in checks)


def test_doctor_measure_stage_skips_selected_lut_requirement(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.firmware.manual_adjustments_zeroed = True
    session.firmware.declared_input_range = "full"
    session.firmware.measured_feed_range = "legal"
    session.firmware.notes = "Ready to measure."
    session.update_chain_state({
        "lut_location": "SmallHD calibration 3D LUT",
        "source_format": "Mac HDMI legal feed, monitor declared full",
    })

    checks = calibrate_session._doctor_checks(session, tmp_path, None, stage="measure")

    assert ("PASS", "No baseline recorded yet; ready for baseline capture.") in checks
    assert not any("No selected LUT" in message for _severity, message in checks)
    assert not [message for severity, message in checks if severity == "FAIL"]


def test_doctor_command_prints_and_exits_on_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "cine7-a"
    save_session(session_dir, new_session("cine7-a", model="SmallHD Cine 7"))

    with pytest.raises(SystemExit) as exc:
        calibrate_session.cmd_doctor(SimpleNamespace(dir=str(session_dir), profile=None))

    output = capsys.readouterr().out
    assert exc.value.code == 1
    assert "Doctor for cine7-a" in output
    assert "FAIL: No selected LUT" in output


def test_doctor_measure_stage_allows_pre_baseline_session(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    session_dir = tmp_path / "sessions" / "cine7-a"
    session = new_session("cine7-a", model="SmallHD Cine 7")
    session.firmware.manual_adjustments_zeroed = True
    session.firmware.declared_input_range = "full"
    session.firmware.measured_feed_range = "legal"
    session.firmware.notes = "Ready to capture baseline."
    session.update_chain_state({
        "lut_location": "SmallHD calibration 3D LUT",
        "source_format": "Mac HDMI legal feed, monitor declared full",
    })
    save_session(session_dir, session)

    calibrate_session.cmd_doctor(
        SimpleNamespace(dir=str(session_dir), profile=None, stage="measure")
    )

    output = capsys.readouterr().out
    assert "Doctor for cine7-a" in output
    assert "[measure]" in output
    assert "PASS: No baseline recorded yet; ready for baseline capture." in output
    assert "No selected LUT" not in output


def test_doctor_all_reports_every_session(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    sessions_root = tmp_path / "sessions"
    for monitor in ("cine7-a", "smallhd-1703px"):
        session_dir = sessions_root / monitor
        baseline = session_dir / "baseline.json"
        lut = session_dir / "lut_v1.cube"
        baseline.parent.mkdir(parents=True)
        baseline.write_text('{"measurements": []}\n', encoding="utf-8")
        lut.write_text("LUT_SIZE 2\n", encoding="utf-8")
        session = new_session(monitor, model=monitor)
        session.baseline_path = str(baseline)
        session.firmware.manual_adjustments_zeroed = True
        session.firmware.declared_input_range = "legal"
        session.firmware.measured_feed_range = "legal"
        session.firmware.notes = "ready"
        session.add_iteration(
            SessionIteration(index=1, cube_path=str(lut), verify_path=str(session_dir / "v1.json"))
        )
        session.select_iteration(1, selected_at="2026-07-07T18:00:00+00:00")
        save_session(session_dir, session)

    calibrate_session.cmd_doctor(
        SimpleNamespace(all=True, dir=None, monitor=None, root=str(sessions_root), profile=None)
    )
    output = capsys.readouterr().out

    assert "Doctor for cine7-a" in output
    assert "Doctor for smallhd-1703px" in output


def test_doctor_all_exits_nonzero_when_any_session_fails(tmp_path: Path) -> None:
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("calibrate_session", root / "tools/calibrate_session.py")
    assert spec is not None and spec.loader is not None
    calibrate_session = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calibrate_session)

    sessions_root = tmp_path / "sessions"
    save_session(sessions_root / "broken", new_session("broken"))

    with pytest.raises(SystemExit) as exc:
        calibrate_session.cmd_doctor(
            SimpleNamespace(all=True, dir=None, monitor=None, root=str(sessions_root), profile=None)
        )

    assert exc.value.code == 1


def _simulate_with_cube(lut) -> list[Measurement]:
    measurements = []
    from smallhd_cal.lut import expand_legal

    for m in _synthetic_display_measurements():
        rgb = np.array([m.patch.r, m.patch.g, m.patch.b])
        index = np.array([squeeze_legal(v) for v in rgb])
        stored = np.array(lut(*index))
        drive = np.array([_distortion(v) for v in stored])
        signal = _MIX @ np.array([expand_legal(v) for v in drive])
        measurements.append(
            Measurement(patch=m.patch, xyz=_simulate_display(tuple(np.clip(signal, 0, 1))))
        )
    return measurements


def _simulate_per_channel_plant(lut) -> list[Measurement]:
    """Monitor whose LUT application is per-channel pointwise but nonlinear
    and different per channel (the Cine 7 behavior class)."""
    from smallhd_cal.lut import expand_legal

    curves = (
        lambda v: np.clip(0.02 + 0.93 * v**1.05, 0.0, 1.0),
        lambda v: np.clip(0.98 * v**0.97, 0.0, 1.0),
        lambda v: np.clip(0.04 + 1.02 * v**1.1, 0.0, 1.0),
    )
    measurements = []
    for m in _synthetic_display_measurements():
        rgb = np.array([m.patch.r, m.patch.g, m.patch.b])
        index = np.array([squeeze_legal(v) for v in rgb])
        stored = lut(*index)
        signal = np.array([expand_legal(curves[c](stored[c])) for c in range(3)])
        measurements.append(
            Measurement(patch=m.patch, xyz=_simulate_display(tuple(np.clip(signal, 0, 1))))
        )
    return measurements


def test_channel_mode_corrects_per_channel_plant(tmp_path: Path) -> None:
    from smallhd_cal.calibration import build_rec709_matrix_correction
    from smallhd_cal.lut import wrap_legal_range

    baseline = _synthetic_display_measurements()
    transform = wrap_legal_range(build_rec709_matrix_correction(baseline))

    cube_path = tmp_path / "lut_v1.cube"
    write_smallhd_cube(cube_path, 33, transform)
    active = read_smallhd_cube(cube_path)
    verify = _simulate_per_channel_plant(active)

    refined, compensation = refine_step(baseline, verify, active, np.eye(3), mode="channel")
    assert np.allclose(compensation, np.eye(3))

    final = tmp_path / "lut_v2.cube"
    write_smallhd_cube(final, 33, refined)
    check = {m.patch.name: m for m in _simulate_per_channel_plant(read_smallhd_cube(final))}

    white = check["white"].xyz
    total = sum(white)
    assert white[0] / total == pytest.approx(0.3127, abs=4e-3)
    assert white[1] / total == pytest.approx(0.3290, abs=4e-3)

    red = check["red"].xyz
    total = sum(red)
    assert red[0] / total == pytest.approx(0.64, abs=9e-3)

    gray = check["gray_127"].xyz
    assert gray[1] / white[1] == pytest.approx(0.5**2.4, abs=2e-2)


def test_refine_step_converges_via_written_cubes(tmp_path: Path) -> None:
    from smallhd_cal.calibration import build_rec709_matrix_correction
    from smallhd_cal.lut import wrap_legal_range

    baseline = _synthetic_display_measurements()
    transform = wrap_legal_range(build_rec709_matrix_correction(baseline))
    compensation = np.eye(3)

    for iteration in range(2):
        cube_path = tmp_path / f"lut_v{iteration + 1}.cube"
        write_smallhd_cube(cube_path, 33, transform)
        active = read_smallhd_cube(cube_path)
        verify = _simulate_with_cube(active)
        transform, compensation = refine_step(
            baseline, verify, active, compensation, color_damping=1.0, mode="matrix"
        )

    final_path = tmp_path / "final.cube"
    write_smallhd_cube(final_path, 33, transform)
    check = {m.patch.name: m for m in _simulate_with_cube(read_smallhd_cube(final_path))}

    white = check["white"].xyz
    total = sum(white)
    assert white[0] / total == pytest.approx(0.3127, abs=4e-3)
    assert white[1] / total == pytest.approx(0.3290, abs=4e-3)

    red = check["red"].xyz
    total = sum(red)
    assert red[0] / total == pytest.approx(0.64, abs=9e-3)

    gray = check["gray_127"].xyz
    assert gray[1] / white[1] == pytest.approx(0.5**2.4, abs=2e-2)
