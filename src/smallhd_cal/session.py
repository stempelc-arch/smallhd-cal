from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SESSION_FILE = "session.json"
DEVICE_MODES = ("smallhd", "teradek_receiver_tv", "computer_monitor")
CHAIN_STATE_REQUIRED_FIELDS = {
    "smallhd": ("lut_location", "source_format"),
    "teradek_receiver_tv": (
        "receiver_model",
        "tv_model",
        "tv_picture_mode",
        "hdmi_range",
        "source_format",
        "lut_location",
    ),
    "computer_monitor": (
        "os_profile",
        "brightness",
        "hdr_state",
        "true_tone",
        "night_shift",
        "display_preset",
        "lut_location",
    ),
}
CHAIN_STATE_RECOMMENDED_FIELDS = {
    "smallhd": (
        "warmup_minutes",
        "calibration_profile_name",
    ),
    "teradek_receiver_tv": (
        "tv_color_space",
        "tv_gamma_setting",
        "tv_backlight_setting",
        "hdr_state",
        "eco_settings",
        "motion_processing",
        "receiver_output_format",
    ),
    "computer_monitor": (
        "refresh_rate",
        "graphics_output_range",
        "display_profile_version",
    ),
}


@dataclass
class FirmwareSetup:
    """State of the monitor's own calibration wizard during this session.

    The LUT is only valid while the monitor stays in exactly this state, so it
    is recorded as first-class session data. `declared_input_range` is the
    wizard's Step 4 choice and must match `measured_feed_range` (what the
    source actually sends) or the pipeline math breaks.
    """

    calibration_target: str = "Generic Rec.709"
    declared_input_range: str = "unknown"  # legal | full | auto | unknown
    measured_feed_range: str = "unknown"  # legal | full | unknown (from profiling)
    dynamic_range_step: str = "skipped"  # skipped | measured
    manual_adjustments_zeroed: bool = False
    warmed_up_minutes: int | None = None
    notes: str = ""


@dataclass
class SessionIteration:
    """One generate-load-verify cycle."""

    index: int
    cube_path: str
    cube_index_order: str = "blue-fastest"
    compensation: list[list[float]] = field(
        default_factory=lambda: [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    )
    damping: float = 1.0
    verify_path: str | None = None
    verify_rechecks: list[str] = field(default_factory=list)
    created_at: str = ""
    notes: str = ""


@dataclass
class CalibrationSession:
    monitor_id: str
    model: str = ""
    created_at: str = ""
    target_gamma: float = 2.4
    target_name: str = "rec709"
    device_mode: str = "smallhd"
    firmware: FirmwareSetup = field(default_factory=FirmwareSetup)
    baseline_path: str | None = None
    dynamic_range_path: str | None = None
    iterations: list[SessionIteration] = field(default_factory=list)
    selected_iteration_index: int | None = None
    selected_at: str | None = None
    profile_path: str | None = None
    chain_state: dict[str, str] = field(default_factory=dict)

    @property
    def current_iteration(self) -> SessionIteration | None:
        return self.iterations[-1] if self.iterations else None

    @property
    def selected_iteration(self) -> SessionIteration | None:
        if self.selected_iteration_index is None:
            return None
        return self.iteration_by_index(self.selected_iteration_index)

    def next_index(self) -> int:
        return self.iterations[-1].index + 1 if self.iterations else 1

    def add_iteration(self, iteration: SessionIteration) -> None:
        self.iterations.append(iteration)

    def iteration_by_index(self, index: int) -> SessionIteration | None:
        return next((iteration for iteration in self.iterations if iteration.index == index), None)

    def select_iteration(self, index: int, selected_at: str | None = None) -> SessionIteration:
        iteration = self.iteration_by_index(index)
        if iteration is None:
            raise ValueError(f"No iteration {index} in this session.")
        if iteration.verify_path is None and not iteration.verify_rechecks:
            raise ValueError(f"Iteration {index} has no verify capture.")
        self.selected_iteration_index = index
        self.selected_at = selected_at or datetime.now(UTC).isoformat()
        return iteration

    def link_profile(self, profile_path: str | Path) -> str:
        self.profile_path = str(profile_path)
        return self.profile_path

    def update_chain_state(self, updates: dict[str, str]) -> None:
        self.chain_state.update(updates)


@dataclass(frozen=True)
class SessionSummary:
    """Small index row for choosing between monitor sessions."""

    session_dir: str
    monitor_id: str
    model: str
    target_gamma: float
    target_name: str
    device_mode: str
    selected_iteration_index: int | None
    selected_cube_path: str | None
    current_iteration_index: int | None
    current_cube_path: str | None
    profile_path: str | None


def new_session(
    monitor_id: str,
    model: str = "",
    target_gamma: float = 2.4,
    target_name: str = "rec709",
    device_mode: str = "smallhd",
) -> CalibrationSession:
    return CalibrationSession(
        monitor_id=monitor_id,
        model=model,
        created_at=datetime.now(UTC).isoformat(),
        target_gamma=target_gamma,
        target_name=target_name,
        device_mode=device_mode,
    )


def session_path(session_dir: str | Path) -> Path:
    return Path(session_dir) / SESSION_FILE


def save_session(session_dir: str | Path, session: CalibrationSession) -> Path:
    path = session_path(session_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(session), indent=2) + "\n", encoding="utf-8")
    return path


def load_session(session_dir: str | Path) -> CalibrationSession:
    payload = json.loads(session_path(session_dir).read_text(encoding="utf-8"))
    firmware = FirmwareSetup(**payload.pop("firmware"))
    iterations = [SessionIteration(**item) for item in payload.pop("iterations")]
    return CalibrationSession(firmware=firmware, iterations=iterations, **payload)


def summarize_session(session_dir: str | Path) -> SessionSummary:
    session = load_session(session_dir)
    selected = session.selected_iteration
    current = session.current_iteration
    return SessionSummary(
        session_dir=str(Path(session_dir)),
        monitor_id=session.monitor_id,
        model=session.model,
        target_gamma=session.target_gamma,
        target_name=session.target_name,
        device_mode=session.device_mode,
        selected_iteration_index=selected.index if selected is not None else None,
        selected_cube_path=selected.cube_path if selected is not None else None,
        current_iteration_index=current.index if current is not None else None,
        current_cube_path=current.cube_path if current is not None else None,
        profile_path=session.profile_path,
    )


def discover_session_summaries(root: str | Path) -> list[SessionSummary]:
    session_dirs = sorted(path.parent for path in Path(root).glob(f"*/{SESSION_FILE}"))
    summaries: list[SessionSummary] = []
    for session_dir in session_dirs:
        try:
            summaries.append(summarize_session(session_dir))
        except (OSError, ValueError, TypeError, KeyError):
            # A single hand-edited/corrupted session.json shouldn't hide every
            # other monitor's session from the picker — skip just this one.
            continue
    return summaries
