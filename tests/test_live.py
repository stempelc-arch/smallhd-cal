from __future__ import annotations

import numpy as np
import pytest

from smallhd_cal.analysis import xyz_to_xy
from smallhd_cal.calibration import _rgb_to_xyz_matrix, color_target
from smallhd_cal.live import (
    PanelModel,
    build_live_correction,
    characterize_gray_ramp,
    characterize_patch_set,
    converge_color,
)
from smallhd_cal.measurement import Measurement, Patch

# A synthetic wide-gamut, greenish-white panel with a native gamma that differs
# from the target, like the real Cine 7.
NATIVE_PRIMARIES = ((0.672, 0.306), (0.219, 0.703), (0.160, 0.069))
NATIVE_WHITE = (0.3135, 0.3641)
GAMMA_TRUE = 2.1
BLACK = np.array([0.0020, 0.0021, 0.0026])


def make_panel():
    matrix = np.asarray(_rgb_to_xyz_matrix(NATIVE_PRIMARIES, NATIVE_WHITE), dtype=float)

    def measure(r: float, g: float, b: float) -> tuple[float, float, float]:
        lin = np.array([max(0.0, float(v)) ** GAMMA_TRUE for v in (r, g, b)])
        return tuple(float(v) for v in (matrix @ lin + BLACK))

    return measure


def synthetic_baseline(measure) -> list[Measurement]:
    patches = [("black", 0, 0, 0), ("white", 1, 1, 1), ("red", 1, 0, 0),
               ("green", 0, 1, 0), ("blue", 0, 0, 1)]
    ms = [Measurement(Patch(n, r, g, b), measure(r, g, b)) for n, r, g, b in patches]
    for code in (0.06, 0.13, 0.25, 0.38, 0.5, 0.63, 0.75, 0.88):
        ms.append(Measurement(Patch(f"gray_{round(code * 255):03d}", code, code, code),
                              measure(code, code, code)))
    return ms


def test_converge_color_hits_a_neutral_target() -> None:
    measure = make_panel()
    model = PanelModel.from_baseline(synthetic_baseline(measure))
    wx, wy = color_target("rec709").white_xy
    white_dir = np.array([wx / wy, 1.0, (1.0 - wx - wy) / wy])
    target_xyz = model.black + white_dir * (0.25 * model.white_net_y)

    result = converge_color(measure, target_xyz, model)
    assert result.residual < 0.003
    x, y = xyz_to_xy(result.achieved_xyz)
    assert abs(x - wx) < 0.003
    assert abs(y - wy) < 0.003
    assert result.iterations <= 8


def test_live_correction_fixes_gamma_and_white_balance() -> None:
    measure = make_panel()
    model = PanelModel.from_baseline(synthetic_baseline(measure))
    char = characterize_gray_ramp(
        measure, model, target_name="rec709", target_gamma=2.4, levels=np.linspace(0.0, 1.0, 17)
    )
    correction = build_live_correction(char, feed="full")

    wx, wy = color_target("rec709").white_xy
    # Headroom trades peak luminance for an accurate white point, so gamma is
    # tracked relative to the CALIBRATED white (level 1.0), not the native white.
    white_net = measure(*correction(1.0, 1.0, 1.0))[1] - model.black[1]
    for level in (0.25, 0.5, 0.75):
        signal = correction(level, level, level)          # LUT output for this input
        xyz = measure(*signal)                             # panel displays it
        rel_y = (xyz[1] - model.black[1]) / white_net
        assert abs(rel_y - level**2.4) < 0.02              # gamma tracking corrected
        x, y = xyz_to_xy(xyz)
        assert abs(x - wx) < 0.01                          # neutral axis corrected
        assert abs(y - wy) < 0.01


def test_live_ramp_converges_at_the_top_without_clipping() -> None:
    # Regression: a greenish panel needs green pulled down for D65, so a
    # full-luminance neutral target at the top of the ramp is off-gamut and the
    # signals would clip at 1.0. Every level (incl. 1.0) must still converge.
    measure = make_panel()
    model = PanelModel.from_baseline(synthetic_baseline(measure))
    char = characterize_gray_ramp(
        measure, model, target_name="rec709", target_gamma=2.4, levels=np.linspace(0.0, 1.0, 17)
    )
    assert max(r.residual for r in char.results) < 0.01
    # top-end signals are not all pinned at 1.0
    assert not np.allclose(char.gray_signals[-1], 1.0)

    correction = build_live_correction(char, feed="full")
    wx, wy = color_target("rec709").white_xy
    for level in (0.875, 0.95):
        x, y = xyz_to_xy(measure(*correction(level, level, level)))
        assert abs(x - wx) < 0.012
        assert abs(y - wy) < 0.012


def test_live_correction_beats_identity() -> None:
    # The uncorrected (identity) panel tracks its native gamma 2.1 and greenish
    # white; the live correction must be closer to Rec.709 target at mid-gray.
    measure = make_panel()
    model = PanelModel.from_baseline(synthetic_baseline(measure))
    char = characterize_gray_ramp(measure, model, target_name="rec709", target_gamma=2.4)
    correction = build_live_correction(char, feed="full")
    wx, wy = color_target("rec709").white_xy

    raw = xyz_to_xy(measure(0.5, 0.5, 0.5))
    fixed = xyz_to_xy(measure(*correction(0.5, 0.5, 0.5)))
    raw_err = np.hypot(raw[0] - wx, raw[1] - wy)
    fixed_err = np.hypot(fixed[0] - wx, fixed[1] - wy)
    assert fixed_err < raw_err / 3


def test_live_correction_preserves_full_range_black() -> None:
    measure = make_panel()
    model = PanelModel.from_baseline(synthetic_baseline(measure))
    char = characterize_gray_ramp(measure, model, target_name="rec709", target_gamma=2.4)

    full = build_live_correction(char, feed="full")
    legal = build_live_correction(char, feed="legal")

    assert full(0.0, 0.0, 0.0) == pytest.approx((0.0, 0.0, 0.0), abs=0.002)
    assert legal(0.0, 0.0, 0.0) == pytest.approx((16 / 255, 16 / 255, 16 / 255), abs=0.002)


def test_characterize_patch_set_converges_primaries() -> None:
    measure = make_panel()
    model = PanelModel.from_baseline(synthetic_baseline(measure))
    results = characterize_patch_set(
        measure,
        model,
        [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)],
        target_name="rec709",
        target_gamma=2.4,
    )

    assert [item.target_rgb for item in results] == [
        (1.0, 0.0, 0.0),
        (0.0, 1.0, 0.0),
        (0.0, 0.0, 1.0),
    ]
    assert max(item.result.residual for item in results) < 0.004


def test_run_live_calibration_end_to_end(tmp_path) -> None:
    from pathlib import Path

    from smallhd_cal import steps
    from smallhd_cal.lut import read_smallhd_cube
    from smallhd_cal.measurement import write_measurements_json
    from smallhd_cal.session import load_session, new_session, save_session

    measure = make_panel()
    session_dir = tmp_path / "sessions" / "synthetic"
    save_session(session_dir, new_session("synthetic", target_name="rec709", target_gamma=2.4))
    write_measurements_json(session_dir / "baseline.json", synthetic_baseline(measure))
    steps.record_baseline(session_dir, session_dir / "baseline.json")

    progress: list[tuple[int, int]] = []
    cube = steps.run_live_calibration(
        session_dir, measure, size=17, lut_range="full",
        on_progress=lambda done, total: progress.append((done, total)),
    )
    assert Path(cube).exists()
    assert progress and progress[-1][0] == progress[-1][1]  # sweep completed

    session = load_session(session_dir)
    assert len(session.iterations) == 1
    assert "live" in session.iterations[0].notes

    # The written LUT, applied by the (synthetic) panel, lands mid-gray on target.
    lut = read_smallhd_cube(cube, "blue-fastest")
    wx, wy = color_target("rec709").white_xy
    x, y = xyz_to_xy(measure(*lut(0.5, 0.5, 0.5)))
    assert abs(x - wx) < 0.012
    assert abs(y - wy) < 0.012


@pytest.mark.parametrize("start_level", [0.1, 0.9])
def test_converge_is_stable_from_far_start(start_level: float) -> None:
    measure = make_panel()
    model = PanelModel.from_baseline(synthetic_baseline(measure))
    wx, wy = color_target("rec709").white_xy
    white_dir = np.array([wx / wy, 1.0, (1.0 - wx - wy) / wy])
    target_xyz = model.black + white_dir * (0.5 * model.white_net_y)
    result = converge_color(measure, target_xyz, model,
                            start=(start_level, start_level, start_level))
    assert result.residual < 0.004


# --- closed-loop color patches -----------------------------------------------

# A panel whose channels have different gammas and crosstalk: the baseline-fitted
# matrix + shared tone curve is measurably wrong at the drive levels primaries
# need — like the real Cine 7, where the model-only LUT left red ~0.03 off in xy.
IMPERFECT_GAMMAS = (2.25, 2.05, 1.9)
CROSSTALK = 0.05

COLOR_PATCHES = [
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 1.0),
    (1.0, 1.0, 0.0),
    (0.0, 1.0, 1.0),
    (1.0, 0.0, 1.0),
]


def make_imperfect_panel():
    matrix = np.asarray(_rgb_to_xyz_matrix(NATIVE_PRIMARIES, NATIVE_WHITE), dtype=float)
    mix = (1.0 - CROSSTALK) * np.eye(3) + (CROSSTALK / 3.0) * np.ones((3, 3))

    def measure(r: float, g: float, b: float) -> tuple[float, float, float]:
        drive = mix @ np.clip([r, g, b], 0.0, None)
        lin = np.array([drive[i] ** IMPERFECT_GAMMAS[i] for i in range(3)])
        return tuple(float(v) for v in (matrix @ lin + BLACK))

    return measure


def test_measured_color_patches_tighten_primaries() -> None:
    measure = make_imperfect_panel()
    model = PanelModel.from_baseline(synthetic_baseline(measure))
    char = characterize_gray_ramp(
        measure, model, target_name="rec709", target_gamma=2.4, levels=np.linspace(0.0, 1.0, 11)
    )
    open_loop = build_live_correction(char, feed="full")

    char.patch_results = characterize_patch_set(
        measure, model, COLOR_PATCHES, target_name="rec709", target_gamma=2.4
    )
    closed_loop = build_live_correction(char, feed="full")

    target = color_target("rec709")
    open_errs, closed_errs = [], []
    for rgb, (tx, ty) in zip(
        [(1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)], target.primaries_xy
    ):
        ox, oy = xyz_to_xy(measure(*open_loop(*rgb)))
        cx, cy = xyz_to_xy(measure(*closed_loop(*rgb)))
        open_errs.append(float(np.hypot(ox - tx, oy - ty)))
        closed_errs.append(float(np.hypot(cx - tx, cy - ty)))

    assert max(open_errs) > 0.01  # the model-only build really is off on this panel
    # Red and green land tight; blue is pinned at this panel's gamut limit
    # (native blue y 0.069 vs target 0.060 is unreachable — same as the real
    # Cine 7, whose best converged blue was ~0.015) so it must just not regress.
    assert closed_errs[0] < 0.008
    assert closed_errs[1] < 0.008
    assert closed_errs[2] <= open_errs[2] + 1e-9
    assert max(closed_errs) <= max(open_errs)


def test_measured_color_patches_preserve_neutrals() -> None:
    # The row-normalized fit must not disturb the measured neutral axis: gray
    # input maps to the exact same signals with and without color patches.
    measure = make_imperfect_panel()
    model = PanelModel.from_baseline(synthetic_baseline(measure))
    char = characterize_gray_ramp(
        measure, model, target_name="rec709", target_gamma=2.4, levels=np.linspace(0.0, 1.0, 11)
    )
    gray_only = build_live_correction(char, feed="full")
    char.patch_results = characterize_patch_set(
        measure, model, COLOR_PATCHES, target_name="rec709", target_gamma=2.4
    )
    with_patches = build_live_correction(char, feed="full")

    for level in (0.0, 0.25, 0.5, 0.75, 1.0):
        assert with_patches(level, level, level) == pytest.approx(
            gray_only(level, level, level), abs=1e-9
        )


def test_color_fit_falls_back_on_garbage_patches() -> None:
    # A botched measurement (probe knocked off the panel, all-white readings)
    # must not poison the LUT: the fit guard falls back to the model matrix.
    from smallhd_cal.live import ConvergeResult, LivePatchResult

    measure = make_panel()
    model = PanelModel.from_baseline(synthetic_baseline(measure))
    char = characterize_gray_ramp(measure, model, target_name="rec709", target_gamma=2.4)
    clean = build_live_correction(char, feed="full")

    garbage = ConvergeResult(
        signal=(1.0, 1.0, 1.0), achieved_xyz=(0.0, 0.0, 0.0), iterations=1, residual=0.5
    )
    char.patch_results = [
        LivePatchResult((1.0, 0.0, 0.0), (0.0, 0.0, 0.0), garbage),
        LivePatchResult((0.0, 1.0, 0.0), (0.0, 0.0, 0.0), garbage),
        LivePatchResult((0.0, 0.0, 1.0), (0.0, 0.0, 0.0), garbage),
    ]
    guarded = build_live_correction(char, feed="full")

    for rgb in [(1.0, 0.0, 0.0), (0.5, 0.5, 0.5), (0.2, 0.7, 0.3)]:
        assert guarded(*rgb) == pytest.approx(clean(*rgb), abs=1e-9)


def test_run_live_calibration_sweeps_and_persists_color_patches(tmp_path) -> None:
    import json

    from smallhd_cal import steps
    from smallhd_cal.measurement import write_measurements_json
    from smallhd_cal.session import load_session, new_session, save_session

    measure = make_imperfect_panel()
    session_dir = tmp_path / "sessions" / "imperfect"
    save_session(session_dir, new_session("imperfect", target_name="rec709", target_gamma=2.4))
    write_measurements_json(session_dir / "baseline.json", synthetic_baseline(measure))
    steps.record_baseline(session_dir, session_dir / "baseline.json")

    progress: list[tuple[int, int]] = []
    steps.run_live_calibration(
        session_dir, measure, size=17, lut_range="full",
        on_progress=lambda done, total: progress.append((done, total)),
    )

    assert progress[-1] == (17, 17)  # 11 grays + 6 color patches, one total
    payload = json.loads((session_dir / "live_v1.json").read_text())
    assert len(payload["patches"]) == 6
    assert all("signal" in p and "residual" in p for p in payload["patches"])
    session = load_session(session_dir)
    assert "6 color patches" in session.iterations[0].notes


def test_run_signal_refine_improves_on_v1_without_reloads(tmp_path) -> None:
    from pathlib import Path

    from smallhd_cal import steps
    from smallhd_cal.lut import read_smallhd_cube
    from smallhd_cal.measurement import load_patch_sequence, write_measurements_json
    from smallhd_cal.session import load_session, new_session, save_session

    measure = make_imperfect_panel()
    session_dir = tmp_path / "sessions" / "synthetic"
    save_session(session_dir, new_session("synthetic", target_name="rec709", target_gamma=2.4))
    write_measurements_json(session_dir / "baseline.json", synthetic_baseline(measure))
    steps.record_baseline(session_dir, session_dir / "baseline.json")
    steps.run_live_calibration(session_dir, measure, size=17, lut_range="full")

    patches = load_patch_sequence(
        Path(__file__).resolve().parents[1] / "measurements" / "patch_sequence_verify_extended.csv"
    )

    def score_cube(cube_path, order):
        lut = read_smallhd_cube(cube_path, order)
        errs = {}
        for name, rgb in {"white": (1, 1, 1), "red": (1, 0, 0),
                          "green": (0, 1, 0), "blue": (0, 0, 1)}.items():
            X, Y, Z = measure(*lut(*rgb))
            x, y = xyz_to_xy((X, Y, Z))
            targets = dict(zip(("red", "green", "blue"),
                               color_target("rec709").primaries_xy, strict=True))
            targets["white"] = color_target("rec709").white_xy
            tx, ty = targets[name]
            errs[name] = ((x - tx) ** 2 + (y - ty) ** 2) ** 0.5
        return (2 * errs["white"] + errs["red"] + errs["green"] + errs["blue"]) / 5

    session = load_session(session_dir)
    v1 = session.iterations[0]
    v1_score = score_cube(v1.cube_path, v1.cube_index_order)

    progress: list[tuple[int, int]] = []
    final_cube = steps.run_signal_refine(
        session_dir, measure, patches, size=17,
        on_progress=lambda done, total: progress.append((done, total)),
    )
    session = load_session(session_dir)
    final = session.current_iteration
    assert final.cube_path == final_cube
    final_score = score_cube(final.cube_path, final.cube_index_order)

    # Software rounds must not regress v1 and normally improve it; every
    # intermediate iteration got a recorded software verify.
    assert final_score <= v1_score + 1e-6
    assert final_score < 0.006  # converged territory (~95%+ on the accuracy scale)
    verified = [it for it in session.iterations if it.verify_path]
    assert verified and all("software signal-space verify" in (it.notes or "") for it in verified)
    assert progress  # progress callback fired
