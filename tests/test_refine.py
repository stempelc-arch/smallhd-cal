import numpy as np
import pytest

from smallhd_cal.calibration import build_rec709_matrix_correction
from smallhd_cal.lut import expand_legal, squeeze_legal, wrap_legal_range
from smallhd_cal.measurement import Measurement, Patch
from smallhd_cal.refine import build_refined_correction
from tests.test_calibration import (
    _simulate_display,
    _synthetic_display_measurements,
)

# Monitor-side LUT application quirks the refinement must cancel: a smooth
# pointwise distortion of stored values plus a desaturating channel mix.
_MIX = np.array(
    [
        [0.88, 0.08, 0.04],
        [0.06, 0.90, 0.04],
        [0.05, 0.07, 0.88],
    ]
)


def _distortion(v: float) -> float:
    return float(np.clip(0.05 + 0.85 * v**1.1, 0.0, 1.0))


def _simulate_with_lut(lut) -> list[Measurement]:
    """Simulate a verify capture: legal feed, LUT lookup, opaque monitor map, panel."""
    measurements = []
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


def test_iterative_refinement_converges_on_target() -> None:
    baseline = _synthetic_display_measurements()

    applied = wrap_legal_range(build_rec709_matrix_correction(baseline))
    verifies = []
    for _iteration in range(2):
        verifies.append(_simulate_with_lut(applied))
        # The simulated monitor is perfectly repeatable, so run undamped;
        # the damping default exists for real import-to-import variability.
        applied = build_refined_correction(baseline, verifies, color_damping=1.0)

    check = {m.patch.name: m for m in _simulate_with_lut(applied)}

    white = check["white"].xyz
    total = sum(white)
    assert white[0] / total == pytest.approx(0.3127, abs=4e-3)
    assert white[1] / total == pytest.approx(0.3290, abs=4e-3)

    red = check["red"].xyz
    total = sum(red)
    assert red[0] / total == pytest.approx(0.64, abs=8e-3)
    assert red[1] / total == pytest.approx(0.33, abs=8e-3)

    gray = check["gray_127"].xyz  # linspace midpoint, signal 0.5
    assert gray[1] / white[1] == pytest.approx(0.5**2.4, abs=2e-2)


def test_refinement_requires_gray_verify_patches() -> None:
    baseline = _synthetic_display_measurements()
    verify = [Measurement(Patch("red", 1.0, 0.0, 0.0), (10.0, 5.0, 1.0))]

    with pytest.raises(ValueError, match="grayscale"):
        build_refined_correction(baseline, [verify])
