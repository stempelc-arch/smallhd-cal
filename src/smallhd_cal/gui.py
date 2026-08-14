"""Guided calibration GUI.

A wizard-style front end over the session workflow. The operator only ever does
three kinds of thing: pick a monitor, measure (baseline / verify), and load a LUT
via SD card. Everything mechanical — generating the first LUT, refining after each
verify, picking the best iteration, exporting — is automatic. A stage strip and
progress bar always show what is happening.

A separate Probe Readout tool gives a live nits reading without taking over the
display, for the monitor's own wizard steps that ask you to read the probe and
type in a value (e.g. HDR Range peak luminance).

Workflow logic lives in report.py (rows, scores, best iteration) and steps.py
(the in-process operations); this module is the Tk shell, the phase machine, the
probe capture loop, and the readout tool.
"""

from __future__ import annotations

import math
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tkinter import messagebox, ttk

from smallhd_cal import report, sdcard, steps
from smallhd_cal.deviceplans import STUDIO_NITS, device_plan, device_plans
from smallhd_cal.displays import Display, choose_external_display, list_displays
from smallhd_cal.measurement import (
    Measurement,
    Patch,
    latest_measurements_by_patch,
    load_patch_sequence,
    read_measurements_json,
    write_measurements_json,
)
from smallhd_cal.paths import AppPaths, resolve_existing_path
from smallhd_cal.presets import get_preset, preset_names
from smallhd_cal.probe import (
    SPOTREAD_ARGS,
    SPOTREAD_INTERACTIVE_ARGS,
    ProbeCommand,
    ProbeError,
    SpotreadSession,
    find_bundled_spotread,
    read_spotread,
)
from smallhd_cal.session import CalibrationSession, discover_session_summaries, load_session

SESSIONS_ROOT = "sessions"
EXPORTS_DIR = "exports"
PATCH_SEQUENCE = "measurements/patch_sequence_v1.csv"
# The live flow only needs black/white/RGB + a short gray ramp to build the start
# model (the sweep re-measures the ramp), so the baseline uses a minimal set.
BASELINE_SEQUENCE = "measurements/patch_sequence_live_baseline.csv"
# Verify sweeps the extended set: the original 11 patches (refine + accuracy%
# read those by name) plus secondaries, half-drive colors, pastels, and skin/
# memory colors so the dE2000 report covers the cube interior, not just its
# edges. ~30 patches ≈ 2 min on the persistent probe session.
VERIFY_SEQUENCE = "measurements/patch_sequence_verify_extended.csv"
SETTLE_MS = 200  # default patch settle before each probe read (adjustable)
MAX_ROUNDS = 12  # stop auto-refining after this many LUT iterations

# Phases of the guided flow.
DEVICE, SETUP, CHOOSE, BASELINE, LOAD, DONE = (
    "device", "setup", "choose", "baseline", "load", "done"
)
STAGES = (("Baseline", BASELINE), ("Calibrate", LOAD), ("Done", DONE))
_STAGE_ORDER = {phase: i for i, (_label, phase) in enumerate(STAGES)}

NEW_MONITOR = "＋ New monitor…"
LIVE_POINTS = 17  # 11 gray levels + 6 color patches the live sweep converges (matches runner)

# The hard-won operating knowledge, surfaced in-app so the settings that make or
# break a run aren't only in someone's head. Kept concise and checkable.
SETUP_GUIDE = """\
MONITOR FIRMWARE
• Use firmware that applies a loaded 3D LUT VERBATIM. SmallHD PageOS 5.x does;
  PageOS 6.3.x "display tuning" reshapes the cube (shadows/white point) and
  degrades calibration — avoid it for calibration if you can.
• Checksum: after loading the correction LUT, the hardware verify should land
  within ~1 dE of the software prediction. A big gap = the firmware is
  reshaping the cube.

BEFORE YOU CHARACTERIZE (every session)
• Load the IDENTITY LUT and activate it (not a leftover correction). The
  SD-card helper on the setup/baseline screens writes it to the card.
• Manual adjustments / picture controls OFF.
• Fix the LUMINANCE (e.g. Studio brightness) to a set value — never variable /
  auto / ambient. Match this value across monitors you want to match.
• Input range: declare it so the feed decodes correctly. Checksum below.
• Keep the monitor state BYTE-IDENTICAL between characterization and install.

BASELINE CHECKSUMS (identity active, before the sweep)
• Black ≈ 0.1 nit, contrast ≈ 1000:1  → range/feed healthy.
  Black lifted to ~0.7–1 nit (~100:1)  → input-range mismatch or leftover LUT.
• Native green y ≈ 0.70, red x ≈ 0.68  → identity is really active (raw panel).
  Green already near Rec.709 (y ≈ 0.60) → a correction LUT is still loaded.

INSTALL & VERIFY
• Import the built LUT, activate it, keep the same luminance/state, then Verify.
• A hardware verify (LUT installed) is the truth; a software prediction is not.
• Don't chase the accuracy % if dE2000 is rising — that means grays/skin are
  getting worse. Keep the lowest-dE2000 iteration.

CALIBRATE THROUGH THE REAL SIGNAL PATH
• A LUT corrects the panel, but the input decode in front of it differs by
  path (HDMI vs SDI vs wireless: range, YCbCr matrix, codec).
• Calibrate — or at least VERIFY — through the input the monitor will actually
  use. For a wireless monitor, calibrate through the wireless link.
• Using a monitor as an HDMI→SDI converter is fine IF its SDI output is CLEAN
  (no LUT / look / overlay / scaling). The baseline checksums confirm it.
"""


PALETTE = {
    "bg": "#ffffff",
    "card": "#ffffff",
    "tint": "#f6f8fa",       # procedure / SD / progress boxes
    "ink": "#1c2333",
    "muted": "#7d8494",
    "hover": "#eef1f5",
    "accent": "#2f6fed",
    "accent_hi": "#2159c8",
    "line": "#e6e8ee",
}

# iOS system palette for the brightness gauge (Apple HIG colours).
IOS = {
    "bg": "#f2f2f7",         # grouped background
    "card": "#ffffff",
    "label": "#1c1c1e",
    "secondary": "#8a8a8e",
    "green": "#34c759",
    "orange": "#ff9500",
    "track": "#e5e5ea",
}

# Body text re-wraps as the window resizes, capped at a readable measure.
MAX_MEASURE = 760


def _fluid(label) -> object:
    """Make a label's wraplength track its allocated width."""
    label.bind(
        "<Configure>",
        lambda e: e.widget.configure(wraplength=min(max(e.width - 4, 200), MAX_MEASURE)),
    )
    return label


def _mix(hex_color: str, other: str, t: float) -> str:
    """Blend two #rrggbb colors, t in [0,1] toward `other`."""
    a = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b, strict=True))


def draw_monitor_icon(canvas: tk.Canvas, w: int, h: int, accent: str, form: str) -> None:
    """Draw a clean, tinted monitor illustration sized to (w, h) on the canvas.

    Three silhouettes so devices read at a glance: a small on-camera monitor,
    a larger field monitor, and a widescreen TV on a stand. Vector primitives —
    crisp at any size, nothing to bundle.
    """
    cx = w / 2
    screen = _mix(accent, "#000000", 0.12)
    bezel = "#2b2f3a"
    glow = _mix(accent, "#ffffff", 0.35)

    if form == "tv":
        bw, bh = w * 0.82, h * 0.56
        x0, y0 = cx - bw / 2, h * 0.14
        canvas.create_rectangle(x0, y0, x0 + bw, y0 + bh, fill=bezel, outline="")
        canvas.create_rectangle(x0 + bw * 0.05, y0 + bh * 0.09,
                                x0 + bw * 0.95, y0 + bh * 0.91, fill=screen, outline="")
        canvas.create_rectangle(x0 + bw * 0.05, y0 + bh * 0.09,
                                x0 + bw * 0.5, y0 + bh * 0.5, fill=glow, outline="")
        canvas.create_rectangle(cx - w * 0.03, y0 + bh, cx + w * 0.03, y0 + bh + h * 0.14,
                                fill=bezel, outline="")
        canvas.create_rectangle(cx - w * 0.16, y0 + bh + h * 0.14,
                                cx + w * 0.16, y0 + bh + h * 0.18, fill=bezel, outline="")
        return

    # on-camera / field monitor: rounded bezel + screen; field is a touch larger.
    scale = 0.62 if form == "oncamera" else 0.78
    bw, bh = w * scale, h * scale * 0.72
    x0, y0 = cx - bw / 2, h * 0.5 - bh / 2
    _round_rect(canvas, x0, y0, x0 + bw, y0 + bh, r=bh * 0.12, fill=bezel)
    inset = bh * 0.12
    _round_rect(canvas, x0 + inset, y0 + inset, x0 + bw - inset, y0 + bh - inset,
                r=bh * 0.06, fill=screen)
    canvas.create_rectangle(x0 + inset, y0 + inset, x0 + bw * 0.55, y0 + bh * 0.55, fill=glow,
                            outline="")
    if form == "oncamera":  # little top mount/handle to read as on-camera
        canvas.create_rectangle(cx - bw * 0.12, y0 - bh * 0.12, cx + bw * 0.12, y0,
                                fill=bezel, outline="")


def _round_rect(canvas: tk.Canvas, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
           x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return canvas.create_polygon(pts, smooth=True, **kw)


class SDCardPanel(ttk.Frame):
    """Guided SD-card transfer: detect the card, copy the LUT, eject.

    Each wizard card that needs a LUT on the monitor embeds one of these with a
    `files_provider` for the LUT(s) that step needs. The panel polls the mounted
    volumes: a fresh card gets a one-click "use this card" (which writes the
    marker sdcard.py recognizes); an initialized card is handled hands-free —
    copy, prune LUTs from earlier steps, eject — so by the time the operator
    reaches for the card it is already safe to pull. All Tk work stays on the
    main thread; the copy/eject runs on a worker and reports back via a queue.
    """

    POLL_MS = 1000

    def __init__(self, parent, app: SmallHDCalApp, files_provider, purpose: str, done_hint: str) -> None:
        super().__init__(parent)
        self.app = app
        self.files_provider = files_provider  # Callable[[], list[Path]]
        self.purpose = purpose                # e.g. "the identity LUT"
        self.done_hint = done_hint            # what to do on the monitor after ejecting
        self._queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self._working = False
        self._done_msg: str | None = None
        self._errors: dict[str, str] = {}     # volume name -> failure message
        self._completed: set[str] = set()     # volumes synced, awaiting unmount
        self._rendered_key: object = None
        self._after_id: str | None = None
        self._cards: list = []                # latest sdcard.scan_cards() result
        self._scan_pending = False            # a background scan is in flight
        self.columnconfigure(0, weight=1)
        self.bind("<Destroy>", self._cancel_poll)
        self._poll()

    # -- polling / state ------------------------------------------------------

    def _cancel_poll(self, _event=None) -> None:
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except tk.TclError:
                pass
            self._after_id = None

    def _poll(self) -> None:
        try:
            self._drain()
            self._maybe_scan()
            self._update()
        finally:
            if self.winfo_exists():
                self._after_id = self.after(self.POLL_MS, self._poll)

    def _maybe_scan(self) -> None:
        # scan_cards() does filesystem I/O (iterdir/resolve/read_text over
        # /Volumes) that can stall on a slow network share or flaky reader; run
        # it off the Tk main thread so a stuck volume never freezes the UI.
        if self._scan_pending:
            return
        self._scan_pending = True

        def work() -> None:
            try:
                cards = sdcard.scan_cards()
            except Exception:  # noqa: BLE001
                cards = []
            self._queue.put(("scan", cards))

        threading.Thread(target=work, daemon=True).start()

    def _drain(self) -> None:
        while True:
            try:
                kind, payload = self._queue.get_nowait()
            except queue.Empty:
                return
            if kind == "scan":
                self._cards = payload
                self._scan_pending = False
                continue
            self._working = False
            if kind == "done":
                volume_name, message = payload
                self._completed.add(volume_name)
                self._done_msg = str(message)
                self.app._log(f"SD card: {message}")
            else:
                volume_name, message = payload
                self._errors[volume_name] = str(message)
                self.app._log(f"SD card error: {message}")

    def _update(self) -> None:
        cards = self._cards
        present = {c.name for c in cards}
        self._completed &= present
        self._errors = {k: v for k, v in self._errors.items() if k in present}

        ready = next((c for c in cards if c.initialized and c.name not in self._completed), None)
        candidates = [c for c in cards if not c.initialized]

        if self._working:
            self._render("working", f"Writing the {self.purpose} to the card…", None, None)
        elif ready is not None and ready.name in self._errors:
            self._render("error", f"⚠ {self._errors[ready.name]}",
                         "Retry ▸", lambda c=ready: self._start_sync(c))
        elif ready is not None:
            # Initialized card inserted: hands-free copy + eject.
            self._start_sync(ready)
            self._render("working", f"Card '{ready.name}' detected — writing the {self.purpose}…", None, None)
        elif candidates:
            names = ", ".join(c.name for c in candidates[1:])
            extra = f"  (also mounted: {names})" if names else ""
            first = candidates[0]
            self._render(
                ("candidate", first.name),
                f"Found '{first.name}'. Set it up once and the app will handle every "
                f"transfer to it automatically.{extra}",
                f"Use '{first.name}' for calibration ▸",
                lambda c=first: self._start_sync(c, initialize=True),
            )
        elif self._done_msg:
            self._render(("done", self._done_msg), f"✓ {self._done_msg}\n\nNext: {self.done_hint}", None, None)
        else:
            self._render("waiting", f"Insert the SD card to receive the {self.purpose} — "
                         "it is detected, written, and ejected automatically.", None, None)

    # -- actions --------------------------------------------------------------

    def _start_sync(self, card, initialize: bool = False) -> None:
        if self._working:
            return
        try:
            files = [Path(f) for f in self.files_provider()]
        except Exception as exc:  # noqa: BLE001
            self._errors[card.name] = str(exc)
            return
        self._working = True
        self._done_msg = None
        self._errors.pop(card.name, None)

        def work() -> None:
            try:
                target = sdcard.initialize_card(card.volume) if initialize else card
                synced, summary = sdcard.sync_card(target, files)
                eject_msg = sdcard.eject(synced.volume)
                self._queue.put(("done", (card.name, f"{summary}. {eject_msg}")))
            except sdcard.SDCardError as exc:
                self._queue.put(("error", (card.name, str(exc))))

        threading.Thread(target=work, daemon=True).start()

    # -- rendering -------------------------------------------------------------

    def _render(self, key: object, text: str, button_text: str | None, command) -> None:
        state_key = (key, text, button_text)
        if state_key == self._rendered_key:
            return
        self._rendered_key = state_key
        for child in self.winfo_children():
            child.destroy()
        box = tk.Frame(self, bg=PALETTE["tint"], highlightbackground=PALETTE["line"],
                       highlightthickness=1)
        box.grid(row=0, column=0, sticky="ew")
        box.columnconfigure(0, weight=1)
        tk.Label(box, text=f"SD CARD — {self.purpose.upper()}", bg=PALETTE["tint"],
                 fg=PALETTE["muted"], font=("Helvetica Neue", 11, "bold")).grid(
            row=0, column=0, sticky="w", padx=14, pady=(10, 4))
        color = "#2e8b2e" if text.startswith("✓") else (
            "#b22222" if text.startswith("⚠") else PALETTE["ink"])
        lbl = tk.Label(box, text=text, bg=PALETTE["tint"], fg=color,
                       font=("Helvetica Neue", 12), justify="left", anchor="w")
        lbl.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))
        _fluid(lbl)
        if button_text:
            ttk.Button(box, text=button_text, command=command).grid(
                row=2, column=0, sticky="w", padx=14, pady=(0, 12))


class _LiveCancelled(Exception):
    """Raised inside the live measure callback when the user cancels."""


def _fmt(value: float | None) -> str:
    return f"{value:.4f}" if value is not None else "—"


@dataclass
class CaptureJob:
    patches: list[Patch]
    output_path: Path
    resume: bool
    on_complete: object  # Callable[[], str]
    after: object  # Callable[[], None], runs on the main thread once recorded
    title: str
    index: int = 0
    measurements: list[Measurement] = field(default_factory=list)
    measured: dict[str, Measurement] = field(default_factory=dict)
    session: object = None  # persistent SpotreadSession for fast reads, or None


class SmallHDCalApp(tk.Tk):
    def __init__(self, paths: AppPaths) -> None:
        super().__init__()
        self.paths = paths
        self.title("SmallHD Calibration")
        self.minsize(760, 640)
        self.geometry("920x780")

        self.sessions_root = Path(SESSIONS_ROOT)
        self.session_dir: Path | None = None
        self.session: CalibrationSession | None = None
        self.summaries: list = []
        self.phase = DEVICE
        self.busy = False
        self._live_stage = None
        self._pending_plan = None  # DevicePlan chosen on the landing screen
        self.page = 0  # sub-page within the current phase (click-through cards)
        self.setup_state: dict[str, bool] = {}  # ticked checklist items, survives paging

        # Advanced knobs (hidden by default).
        self.lut_size = tk.IntVar(value=17)
        self.lut_range = tk.StringVar(value="full")
        self.damping = tk.DoubleVar(value=0.5)
        self.refine_mode = tk.StringVar(value="channel")
        self.timeout = tk.DoubleVar(value=90.0)  # dark patches integrate slowly
        self.settle_ms = tk.IntVar(value=SETTLE_MS)

        self.worker_messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.capture: CaptureJob | None = None
        self.patch_window: tk.Toplevel | None = None
        self._step_after: object | None = None
        self.readout_win: tk.Toplevel | None = None
        self.guide_win: tk.Toplevel | None = None
        self.readout_active = False
        self._last_readout_y: float | None = None
        self.live_cancel = False
        self._live_reads = 0
        self._log_lock = threading.Lock()
        try:
            self._logfile = open(paths.user_data_root / "smallhd_cal.log", "a", encoding="utf-8")  # noqa: SIM115
        except OSError:
            self._logfile = None
        self._flog("app start")

        self._build_chrome()
        self._setup_styles()
        self._reload_sessions(land_on=DEVICE)  # device-first landing
        self.after(100, self._drain_worker_messages)

    # -- theme / polish ------------------------------------------------------

    def _setup_styles(self) -> None:
        """A light, consistent visual theme: type scale, accent, roomy padding."""
        self.configure(background=PALETTE["bg"])
        style = ttk.Style(self)
        try:
            style.theme_use("clam")  # gives us control over colors/padding
        except tk.TclError:
            pass
        base = ("Helvetica Neue", 13)
        style.configure(".", font=base, background=PALETTE["bg"], foreground=PALETTE["ink"])
        style.configure("TFrame", background=PALETTE["bg"])
        style.configure("TLabel", background=PALETTE["bg"], foreground=PALETTE["ink"])
        style.configure("Muted.TLabel", foreground=PALETTE["muted"])
        style.configure("H1.TLabel", font=("Helvetica Neue", 22, "bold"))
        style.configure("H2.TLabel", font=("Helvetica Neue", 16, "bold"))
        style.configure("Section.TLabel", font=("Helvetica Neue", 13, "bold"))
        style.configure("TButton", padding=(14, 8), font=("Helvetica Neue", 12),
                        background=PALETTE["tint"], bordercolor=PALETTE["line"],
                        lightcolor=PALETTE["tint"], darkcolor=PALETTE["tint"], relief="flat")
        style.map("TButton",
                  background=[("active", PALETTE["hover"])])
        style.configure("Accent.TButton", foreground="white", background=PALETTE["accent"],
                        bordercolor=PALETTE["accent"], lightcolor=PALETTE["accent"],
                        darkcolor=PALETTE["accent"],
                        padding=(18, 9), font=("Helvetica Neue", 13, "bold"))
        style.map("Accent.TButton",
                  background=[("active", PALETTE["accent_hi"]), ("disabled", "#b9c3d6")],
                  bordercolor=[("disabled", "#b9c3d6")])
        style.configure("Card.TFrame", background=PALETTE["card"], relief="flat")
        style.configure("Card.TLabel", background=PALETTE["card"], foreground=PALETTE["ink"])
        style.configure("TCheckbutton", background=PALETTE["bg"], font=base)
        style.map("TCheckbutton", background=[("active", PALETTE["bg"])])
        style.configure("TProgressbar", background=PALETTE["accent"],
                        troughcolor=PALETTE["tint"], bordercolor=PALETTE["line"],
                        lightcolor=PALETTE["accent"], darkcolor=PALETTE["accent"])

    # -- static chrome (stage strip, process area, card, details) -----------

    def _build_chrome(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(4, weight=1)

        header = ttk.Frame(self, padding=(24, 18, 24, 10))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="SmallHD Calibration", style="H2.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.session_label = ttk.Label(header, text="", style="Muted.TLabel")
        self.session_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        # Stage strip lives in the header's right column.
        self.stage_frame = ttk.Frame(header)
        self.stage_frame.grid(row=0, column=1, rowspan=2, sticky="e")
        self.stage_labels: dict[str, ttk.Label] = {}
        for col, (label, phase) in enumerate(STAGES):
            if col:
                ttk.Label(self.stage_frame, text="›", foreground=PALETTE["line"]).grid(
                    row=0, column=col * 2 - 1, padx=8
                )
            lbl = ttk.Label(self.stage_frame, text=label, foreground=PALETTE["muted"])
            lbl.grid(row=0, column=col * 2)
            self.stage_labels[phase] = lbl

        tk.Frame(self, bg=PALETTE["line"], height=1).grid(row=1, column=0, sticky="ew")

        # Process area: "Now:" line + progress bar.
        process = ttk.Frame(self, padding=(24, 8, 24, 0))
        process.grid(row=2, column=0, sticky="ew")
        process.columnconfigure(0, weight=1)
        self.process_label = ttk.Label(process, text="", style="Muted.TLabel",
                                       font=("Helvetica Neue", 11))
        self.process_label.grid(row=0, column=0, sticky="w")
        self.progress = ttk.Progressbar(process, mode="determinate", length=100)
        self.progress.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.progress.grid_remove()

        # The card is rebuilt per phase; each phase is short click-through
        # pages, so no scrolling — text re-wraps with the window instead.
        self.card = ttk.Frame(self, padding=(24, 10, 24, 18))
        self.card.grid(row=4, column=0, sticky="nsew")
        self.card.columnconfigure(0, weight=1)

        # Built but never shown: the log Text and iterations tree still receive
        # writes (_log, _render_table); the probe readout stays reachable via
        # the Load card's "Read peak nits" button.
        self.details = ttk.Frame(self, padding=(24, 4, 24, 12))
        self.details.grid(row=5, column=0, sticky="ew")
        self._build_details(self.details)
        self.details.grid_remove()

    def _build_details(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        table = ttk.LabelFrame(parent, text="Iterations", padding=6)
        table.grid(row=0, column=0, sticky="ew")
        table.columnconfigure(0, weight=1)
        cols = ("iter", "white", "red", "green", "blue", "gray50", "verify")
        self.tree = ttk.Treeview(table, columns=cols, show="headings", height=7, selectmode="none")
        for col in cols:
            self.tree.heading(col, text="gray50 dev" if col == "gray50" else col)
            self.tree.column(col, width=64 if col not in ("iter", "verify") else 56, anchor="center")
        self.tree.tag_configure("best", background="#173a17", foreground="#d7ffd7")
        self.tree.grid(row=0, column=0, sticky="ew")

        adv = ttk.LabelFrame(parent, text="Advanced", padding=6)
        adv.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(adv, text="LUT size").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(adv, from_=17, to=65, textvariable=self.lut_size, width=5).grid(row=0, column=1, padx=(4, 14))
        ttk.Label(adv, text="Range").grid(row=0, column=2, sticky="w")
        ttk.Combobox(adv, textvariable=self.lut_range, values=["legal", "full"], state="readonly", width=6).grid(row=0, column=3, padx=(4, 14))
        ttk.Label(adv, text="Damping").grid(row=0, column=4, sticky="w")
        ttk.Spinbox(adv, from_=0.1, to=1.0, increment=0.1, textvariable=self.damping, width=5).grid(row=0, column=5, padx=(4, 14))
        ttk.Label(adv, text="Refine").grid(row=0, column=6, sticky="w")
        ttk.Combobox(adv, textvariable=self.refine_mode, values=["channel", "matrix"], state="readonly", width=8).grid(row=0, column=7, padx=(4, 14))
        ttk.Label(adv, text="Probe timeout").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(adv, from_=5, to=300, textvariable=self.timeout, width=5).grid(row=1, column=1, sticky="w", pady=(6, 0), padx=(4, 0))
        ttk.Label(adv, text="Settle (ms)").grid(row=1, column=2, sticky="w", pady=(6, 0))
        ttk.Spinbox(adv, from_=100, to=1500, increment=50, textvariable=self.settle_ms, width=5).grid(row=1, column=3, sticky="w", pady=(6, 0), padx=(4, 0))

        log_frame = ttk.LabelFrame(parent, text="Log", padding=6)
        log_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        log_frame.columnconfigure(0, weight=1)
        self.log = tk.Text(log_frame, height=8, wrap="word")
        self.log.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(log_frame, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set)

    # -- rendering -----------------------------------------------------------

    def _render(self) -> None:
        if self.session is not None:
            fw = self.session.firmware
            self.session_label.configure(
                text=f"{self.session.monitor_id} · {self.session.target_name} γ{self.session.target_gamma} · "
                f"declared {fw.declared_input_range}/measured {fw.measured_feed_range}"
            )
        else:
            self.session_label.configure(text="")

        # The stage strip (Baseline › Calibrate › Done) only makes sense once a
        # device is chosen; keep the landing/setup/manage screens uncluttered.
        if self.phase in (DEVICE, SETUP, CHOOSE):
            self.stage_frame.grid_remove()
        else:
            self.stage_frame.grid()

        current = _STAGE_ORDER.get(self.phase, -1)
        for phase, lbl in self.stage_labels.items():
            order = _STAGE_ORDER[phase]
            base = lbl.cget("text").lstrip("✓ ")
            if self.phase == DONE or order < current:
                lbl.configure(foreground="#2e8b2e", text=f"✓ {base}" if order < current or phase != DONE else base)
            elif order == current:
                lbl.configure(foreground="#1a1aff", text=base)
            else:
                lbl.configure(foreground="#999", text=base)

        self._render_table()
        for child in self.card.winfo_children():
            child.destroy()
        {
            DEVICE: self._card_device,
            SETUP: self._card_setup,
            CHOOSE: self._card_choose,
            BASELINE: self._card_baseline,
            LOAD: self._card_load,
            DONE: self._card_done,
        }[self.phase]()

    def _big(self, text: str, row: int, **kw) -> ttk.Label:
        lbl = ttk.Label(self.card, text=text, font=kw.pop("font", ("", 13)),
                        justify="left", anchor="w")
        lbl.grid(row=row, column=0, sticky="ew", pady=kw.pop("pady", (0, 8)))
        return _fluid(lbl)

    def _session_plan(self):
        """The DevicePlan this session was started from, or None.

        New sessions record their plan key; older ones are matched by the
        auto-name stem so their cards still show device-specific steps.
        """
        if self.session is None:
            return self._pending_plan
        key = self.session.chain_state.get("device_plan", "")
        if key:
            try:
                return device_plan(key)
            except ValueError:
                pass
        for plan in device_plans():
            if self.session.monitor_id.startswith(plan.session_prefix):
                return plan
        return None

    def _tint_box(self, title: str, row: int, pady=(4, 10)) -> tk.Frame:
        """A flat, softly tinted panel with a small-caps heading."""
        box = tk.Frame(self.card, bg=PALETTE["tint"], highlightbackground=PALETTE["line"],
                       highlightthickness=1)
        box.grid(row=row, column=0, sticky="ew", pady=pady)
        box.columnconfigure(1, weight=1)
        tk.Label(box, text=title.upper(), bg=PALETTE["tint"], fg=PALETTE["muted"],
                 font=("Helvetica Neue", 11, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(10, 6))
        return box

    def _steps_box(self, title: str, items: list[str], row: int) -> None:
        """Numbered on-monitor procedure, rendered verbatim from the device plan."""
        box = self._tint_box(title, row)
        for i, item in enumerate(items, start=1):
            tk.Label(box, text=str(i), bg=PALETTE["tint"], fg=PALETTE["accent"],
                     font=("Helvetica Neue", 12, "bold")).grid(
                row=i, column=0, sticky="ne", padx=(16, 10), pady=(0, 6))
            lbl = tk.Label(box, text=item, bg=PALETTE["tint"], fg=PALETTE["ink"],
                           font=("Helvetica Neue", 12), justify="left", anchor="w")
            lbl.grid(row=i, column=1, sticky="ew", padx=(0, 16), pady=(0, 6))
            _fluid(lbl)
        tk.Frame(box, bg=PALETTE["tint"], height=6).grid(row=len(items) + 1, column=0)

    def _primary(self, text: str, command, row: int) -> ttk.Button:
        btn = ttk.Button(self.card, text=text, command=command, style="Accent.TButton")
        btn.grid(row=row, column=0, sticky="w", pady=(8, 4))
        btn.state(["disabled"] if self.busy else ["!disabled"])
        return btn

    def _card_device(self) -> None:
        ttk.Label(self.card, text="What are you calibrating?", style="H1.TLabel").grid(
            row=0, column=0, sticky="w")
        ttk.Label(self.card, text="Pick a device — the app walks you through its exact connection "
                  "and settings, then calibrates.", style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(2, 12))

        grid = ttk.Frame(self.card)
        grid.grid(row=2, column=0, sticky="ew")
        cols = 2
        for c in range(cols):
            grid.columnconfigure(c, weight=1, uniform="dev")
        for i, plan in enumerate(device_plans()):
            self._device_tile(grid, plan, i // cols, i % cols)

        extra = ttk.Frame(self.card)
        extra.grid(row=3, column=0, sticky="w", pady=(16, 0))
        ttk.Button(extra, text="Resume / manage sessions…",
                   command=lambda: self._goto(CHOOSE)).grid(row=0, column=0)
        ttk.Button(extra, text="Setup guide…", command=self._open_guide).grid(
            row=0, column=1, padx=(8, 0))

    def _device_tile(self, parent, plan, row, col) -> None:
        tile = tk.Frame(parent, bg=PALETTE["card"], highlightthickness=1,
                        highlightbackground=PALETTE["line"], bd=0)
        tile.grid(row=row, column=col, sticky="nsew", padx=6, pady=6, ipadx=6, ipady=10)
        tile.columnconfigure(0, weight=1)

        icon = tk.Canvas(tile, width=150, height=96, bg=PALETTE["card"],
                         highlightthickness=0)
        icon.grid(row=0, column=0, pady=(8, 4))
        accent = plan.accent if not plan.placeholder else PALETTE["muted"]
        draw_monitor_icon(icon, 150, 96, accent, plan.form)

        name = tk.Label(tile, text=plan.label, bg=PALETTE["card"], fg=PALETTE["ink"],
                        font=("Helvetica Neue", 15, "bold"))
        name.grid(row=1, column=0)
        sub = tk.Label(tile, text=plan.subtitle, bg=PALETTE["card"], fg=PALETTE["muted"],
                       font=("Helvetica Neue", 11), wraplength=190, justify="center")
        sub.grid(row=2, column=0, pady=(1, 8))
        if plan.placeholder:
            tk.Label(tile, text="COMING SOON", bg=PALETTE["card"], fg=accent,
                     font=("Helvetica Neue", 9, "bold")).grid(row=3, column=0, pady=(0, 6))

        widgets = [tile, icon, name, sub]
        hover_on = accent if not plan.placeholder else PALETTE["line"]

        def enter(_e=None):
            tile.configure(highlightbackground=hover_on, highlightthickness=2)

        def leave(_e=None):
            tile.configure(highlightbackground=PALETTE["line"], highlightthickness=1)

        def click(_e=None):
            self._choose_device(plan)

        for wdg in widgets:
            wdg.bind("<Enter>", enter)
            wdg.bind("<Leave>", leave)
            wdg.bind("<Button-1>", click)
            wdg.configure(cursor="hand2")

    def _choose_device(self, plan) -> None:
        if plan.placeholder:
            messagebox.showinfo(
                plan.label,
                f"{plan.label} isn't set up yet.\n\nWe'll work out its signal path and "
                "settings, then this button will guide the calibration like the others.",
            )
            return
        self._pending_plan = plan
        self.setup_state = {}
        self._sync_refine_mode_to_plan()
        self._goto(SETUP)

    # Setup is a click-through sequence of short pages in true working order:
    # wire & power (warm-up clock starts) → monitor settings → identity LUT
    # onto the card → identity import at the monitor → start.
    def _setup_pages(self, plan) -> list[tuple[str, str]]:
        pages = [("connect", "Connect & power the signal path"),
                 ("settings", "Monitor settings"),
                 ("card", "Identity LUT onto the SD card")]
        if plan.identity_steps:
            pages.append(("import", "Import the identity LUT on the monitor"))
        # Brightness is tuned LAST: the monitor's New Calibration wizard resets
        # it, so it must be set after the identity LUT is imported and active,
        # right before the baseline captures that state.
        if getattr(plan, "tune_brightness", False):
            pages.append(("brightness", "Fine-tune the brightness on the probe"))
        circled = "①②③④⑤⑥⑦⑧⑨"
        return [(kind, f"{circled[i]}  {title}") for i, (kind, title) in enumerate(pages)]

    def _card_setup(self) -> None:
        plan = self._pending_plan
        pages = self._setup_pages(plan)
        self.page = max(0, min(self.page, len(pages) - 1))
        kind, title = pages[self.page]

        head = ttk.Frame(self.card)
        head.grid(row=0, column=0, sticky="w")
        chip = tk.Canvas(head, width=44, height=30, bg=PALETTE["bg"], highlightthickness=0)
        chip.grid(row=0, column=0, padx=(0, 8))
        draw_monitor_icon(chip, 44, 30, plan.accent, plan.form)
        ttk.Label(head, text=f"{plan.label} — setup  ·  step {self.page + 1} of {len(pages)}",
                  style="H2.TLabel").grid(row=0, column=1)
        ttk.Label(self.card, text=title, font=("Helvetica Neue", 13, "bold"),
                  foreground=plan.accent).grid(row=1, column=0, sticky="w", pady=(8, 4))
        r = 2

        if kind == "connect":
            r = self._checklist(plan.connection, r)
            hint = "Tick every item to continue."
        elif kind == "settings":
            r = self._checklist(plan.settings, r)
            hint = "Tick every item to continue."
        elif kind == "brightness":
            self._steps_box(
                f"{plan.label} — set brightness with the probe (do this LAST)",
                [
                    (
                        "The monitor's New Calibration wizard RESETS the brightness, so tune it now — "
                        "after the identity LUT is imported and active, right before the baseline."
                    ),
                    (
                        "Open the live readout (the app puts a white field on the monitor) and place "
                        "the probe on the centre of the panel."
                    ),
                    (
                        f"Nudge the Studio brightness slider until the readout reads ~{STUDIO_NITS} nits "
                        "(it turns green on target). Matched monitors must land on the same level."
                    ),
                    (
                        "Re-tune the same way after loading the CORRECTION LUT (that wizard resets it "
                        "too) — before you Verify."
                    ),
                ],
                r,
            )
            r += 1
            ttk.Button(self.card, text="Open live probe readout ▸", command=self._open_readout,
                       style="Accent.TButton").grid(row=r, column=0, sticky="w", pady=(8, 0))
            r += 1
            hint = f"Dial the slider to ~{STUDIO_NITS} nits, then Start the baseline."
        elif kind == "card":
            self._identity_sd_panel(r)
            r += 1
            hint = ("Insert the SD card; the app writes the identity LUT and ejects it. "
                    "Continue once the card is ejected (or if it already holds the identity).")
        else:  # import
            self._steps_box(f"{plan.label} — exact procedure", plan.identity_steps, r)
            r += 1
            hint = "Done on the monitor? Start the calibration."

        hint_lbl = ttk.Label(self.card, text=hint, style="Muted.TLabel",
                             justify="left", anchor="w")
        hint_lbl.grid(row=r, column=0, sticky="ew", pady=(6, 0))
        _fluid(hint_lbl)
        r += 1

        last = self.page == len(pages) - 1
        back = (lambda: self._goto(DEVICE)) if self.page == 0 else (
            lambda: self._goto_page(self.page - 1))
        if last:
            self.setup_next_btn = self._pager(
                r, back=back, next_cmd=self._start_from_plan, next_label="Start calibration ▸")
        else:
            self.setup_next_btn = self._pager(
                r, back=back, next_cmd=lambda: self._goto_page(self.page + 1))
        self._refresh_setup_next(pages[self.page][0])

    def _checklist(self, items: list[str], row: int) -> int:
        """Check rows whose ticked state survives page navigation.

        The label is separate from the Checkbutton so long items wrap with the
        window; clicking the label toggles too.
        """
        for item in items:
            var = tk.BooleanVar(value=self.setup_state.get(item, False))

            def toggle(item=item, var=var) -> None:
                self.setup_state[item] = var.get()
                self._refresh_setup_next(self._setup_pages(self._pending_plan)[self.page][0])

            line = ttk.Frame(self.card)
            line.grid(row=row, column=0, sticky="ew", padx=(4, 0), pady=3)
            line.columnconfigure(1, weight=1)
            cb = ttk.Checkbutton(line, variable=var, command=toggle, takefocus=False)
            cb.grid(row=0, column=0, sticky="nw")
            lbl = ttk.Label(line, text=item, justify="left", anchor="w")
            lbl.grid(row=0, column=1, sticky="ew", padx=(4, 0))
            _fluid(lbl)

            def label_toggle(_e, item=item, var=var) -> None:
                var.set(not var.get())
                self.setup_state[item] = var.get()
                self._refresh_setup_next(self._setup_pages(self._pending_plan)[self.page][0])

            lbl.bind("<Button-1>", label_toggle)
            lbl.configure(cursor="hand2")
            row += 1
        return row

    def _refresh_setup_next(self, kind: str) -> None:
        if not hasattr(self, "setup_next_btn"):
            return
        plan = self._pending_plan
        if kind == "connect":
            ready = all(self.setup_state.get(i, False) for i in plan.connection)
        elif kind == "settings":
            ready = all(self.setup_state.get(i, False) for i in plan.settings)
        else:
            # Starting requires the earlier checklists complete.
            ready = all(self.setup_state.get(i, False) for i in plan.checklist())
        self.setup_next_btn.state(["!disabled"] if ready else ["disabled"])

    def _goto_page(self, page: int) -> None:
        self.page = page
        self._render()

    def _pager(self, row: int, *, back, next_cmd, next_label: str = "Next ›") -> ttk.Button:
        """Back/Next navigation row; returns the Next button (for gating)."""
        nav = ttk.Frame(self.card)
        nav.grid(row=row, column=0, sticky="w", pady=(14, 0))
        col = 0
        if back is not None:
            ttk.Button(nav, text="‹ Back", command=back).grid(row=0, column=col)
            col += 1
        nxt = ttk.Button(nav, text=next_label, command=next_cmd, style="Accent.TButton")
        nxt.grid(row=0, column=col, padx=(10 if col else 0, 0))
        return nxt

    def _start_from_plan(self) -> None:
        plan = self._pending_plan
        name = steps.suggested_session_name(
            self.sessions_root, plan.preset_name, stem=plan.session_prefix)
        try:
            self.session_dir = steps.create_session_from_preset(
                self.sessions_root, name, plan.preset_name, plan_key=plan.key)
        except ValueError as exc:
            messagebox.showerror(plan.label, str(exc))
            return
        self._log(f"Created {plan.label} session {self.session_dir}")
        self._reload_session()
        self._enter_flow()

    def _card_choose(self) -> None:
        self._big("Calibrate a monitor", 0, font=("", 15, "bold"))
        row = ttk.Frame(self.card)
        row.grid(row=1, column=0, sticky="w", pady=(0, 8))
        ttk.Label(row, text="Monitor:").grid(row=0, column=0, padx=(0, 6))
        values = [s.monitor_id for s in self.summaries] + [NEW_MONITOR]
        self.choose_var = tk.StringVar(value=NEW_MONITOR if not values[:-1] else values[0])
        self.choose_combo = ttk.Combobox(row, textvariable=self.choose_var, values=values, state="readonly", width=28)
        self.choose_combo.grid(row=0, column=1)
        self.choose_combo.bind("<<ComboboxSelected>>", lambda _e: self._render_new_fields())
        # Prune scratch/test runs without leaving the app.
        self.delete_btn = ttk.Button(row, text="Delete", command=self._delete_session)
        self.delete_btn.grid(row=0, column=2, padx=(8, 0))
        self.new_fields = ttk.Frame(self.card)
        self.new_fields.grid(row=2, column=0, sticky="w")
        self._render_new_fields()
        self._primary("Start ▸", self._start, 3)
        ttk.Button(self.card, text="Setup guide — monitor settings & workflow…",
                   command=self._open_guide).grid(row=4, column=0, sticky="w", pady=(12, 0))

    def _render_new_fields(self) -> None:
        for child in self.new_fields.winfo_children():
            child.destroy()
        is_new = self.choose_var.get() == NEW_MONITOR
        self.delete_btn.state(["disabled"] if is_new else ["!disabled"])
        if not is_new:
            return
        ttk.Label(self.new_fields, text="Preset:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        # Rec.709 mode: only surface Rec.709 presets (P3 code stays for later).
        presets = [n for n in preset_names() if get_preset(n).target_name == "rec709"]
        self.new_preset = tk.StringVar(value=presets[0])
        preset_combo = ttk.Combobox(self.new_fields, textvariable=self.new_preset, values=presets,
                                    state="readonly", width=24)
        preset_combo.grid(row=0, column=1, pady=2)
        preset_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh_suggested_name())
        ttk.Label(self.new_fields, text="Name:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=2)
        self.new_name = tk.StringVar()
        ttk.Entry(self.new_fields, textvariable=self.new_name, width=32).grid(row=1, column=1, pady=2)
        ttk.Label(self.new_fields, text="(auto-named model-target-date; edit if you like)",
                  foreground="#888").grid(row=2, column=1, sticky="w")
        self._refresh_suggested_name()

    def _refresh_suggested_name(self) -> None:
        # Best-effort autofill only: any failure just leaves the name field
        # blank for the operator to type, so it's fine to swallow silently.
        try:
            self.new_name.set(steps.suggested_session_name(self.sessions_root, self.new_preset.get()))
        except Exception:  # noqa: BLE001, S110
            pass

    def _delete_session(self) -> None:
        name = self.choose_var.get()
        if name == NEW_MONITOR:
            return
        if not messagebox.askyesno(
            "Delete session",
            f"Delete the session '{name}' and all its captures and LUTs?\n\nThis cannot be undone.",
        ):
            return
        try:
            steps.delete_session(self.sessions_root, name)
        except (ValueError, OSError) as exc:
            messagebox.showerror("Delete session", str(exc))
            return
        self._log(f"Deleted session {name}")
        self._reload_sessions()

    def _card_baseline(self) -> None:
        plan = self._session_plan()
        if self.page == 1:
            # Identity refresher for resumed sessions (new sessions did this in setup).
            self._big("Identity LUT — SD card & import procedure", 0, font=("", 14, "bold"))
            self._identity_sd_panel(1)
            if plan is not None and plan.identity_steps:
                self._steps_box(f"{plan.label} — exact procedure", plan.identity_steps, 2)
            else:
                self._big(
                    "Load the identity LUT on the monitor (the SD-card helper above puts it "
                    "on the card), set manual adjustments off, and keep the monitor state fixed.",
                    2,
                )
            ttk.Button(self.card, text="‹ Back to Step 1", command=lambda: self._goto_page(0)).grid(
                row=3, column=0, sticky="w", pady=(14, 0))
            return
        self._big("Step 1 — Baseline & live calibration", 0, font=("", 14, "bold"))
        self._big(
            "The monitor must be on the freshly-imported identity LUT (activated in setup), "
            "warmed up, and untouched since. Probe on the centre of the panel, flush, shaded "
            "from room light.\n"
            "This measures the baseline, then automatically converges each colour on target with "
            "the probe — no SD reloads. It runs unattended for a few minutes; press Esc on the "
            "patch window to stop.",
            1,
        )
        self._primary("Measure & calibrate", self._do_baseline, 2)
        ttk.Button(self.card, text="Identity LUT not loaded? SD card & procedure…",
                   command=lambda: self._goto_page(1)).grid(row=3, column=0, sticky="w", pady=(10, 0))

    def _identity_lut_files(self) -> list[Path]:
        return [steps.ensure_identity_lut(self.paths.user_data_root / "luts")]

    def _identity_sd_panel(self, row: int) -> SDCardPanel:
        panel = SDCardPanel(
            self.card, self, self._identity_lut_files,
            purpose="identity LUT",
            done_hint=(f"put the card in the monitor and import {steps.IDENTITY_CUBE_NAME} "
                       "following the exact procedure on this screen, then come back here."),
        )
        panel.grid(row=row, column=0, sticky="ew", pady=(10, 0))
        return panel

    def _card_load(self) -> None:
        session = self.session
        current = session.current_iteration
        rows = report.iteration_rows(session, root=".")
        best = report.best_verified_iteration(rows)
        white, black = report.saved_levels(session)

        status = report.convergence_status(rows)

        r = 0
        self._big("Step 2 — Calibrate", r, font=("", 14, "bold"))
        r += 1

        if self.page == 0 and best is not None:
            acc = report.accuracy_percent(best)
            source = " — predicted, not yet measured on the monitor" if best.is_software else ""
            self._big(
                f"Best so far: v{best.index} — {acc:.0f}% accurate "
                f"({report.accuracy_label(acc)}){source}",
                r, font=("", 13, "bold"),
            )
            r += 1

        if self.page == 0 and status.state in ("converged", "regressed"):
            color = "#2e8b2e" if status.state == "converged" else "#b26b00"
            mark = "✓ " if status.state == "converged" else "⚠ "
            banner = ttk.Label(
                self.card, text=mark + status.message, font=("", 12, "bold"),
                foreground=color, justify="left", anchor="w",
            )
            banner.grid(row=r, column=0, sticky="ew", pady=(0, 8))
            _fluid(banner)
            r += 1

        if current is None:
            self._big(
                "Ready to calibrate. Identity LUT loaded, probe on the panel, monitor held "
                "fixed — this runs the automated live sweep (a few minutes, no SD reloads).",
                r,
            )
            r += 1
            live_btn = ttk.Button(self.card, text="Start live calibration ▸", command=self._start_live)
            live_btn.grid(row=r, column=0, sticky="w", pady=(4, 0))
            live_btn.state(["disabled"] if self.busy else ["!disabled"])
            r += 1
        elif current.verify_path is None or report.is_software_verified(current):
            lut_name = Path(current.cube_path).name
            predicted = ""
            if report.is_software_verified(current):
                rows_now = report.iteration_rows(self.session, root=".")
                row = next((x for x in rows_now if x.index == current.index and not x.is_recheck), None)
                acc = report.accuracy_percent(row) if row else None
                if acc is not None:
                    predicted = f"Software refine predicts {acc:.0f}% — confirm it on the panel.\n"
            plan = self._session_plan()
            # Two pages in working order: at the Mac (write the card), then at
            # the monitor (import, enter levels, verify).
            if self.page == 0:
                self._big(
                    f"{predicted}"
                    f"Load  {lut_name}  onto the monitor. Insert the SD card — it is written "
                    "and ejected automatically — then continue to the on-monitor procedure.",
                    r,
                )
                r += 1
                panel = SDCardPanel(
                    self.card, self, lambda c=current: [Path(c.cube_path)],
                    purpose=f"correction LUT v{current.index}",
                    done_hint=(f"take the card to the monitor and click Next for the exact "
                               f"procedure to import {lut_name}."),
                )
                panel.grid(row=r, column=0, sticky="ew", pady=(6, 0))
                r += 1
                buttons = ttk.Frame(self.card)
                buttons.grid(row=r, column=0, sticky="w", pady=(8, 0))
                r += 1
                ttk.Button(buttons, text="Reveal LUT in Finder",
                           command=lambda: self._reveal(current.cube_path)).grid(row=0, column=0)
                nxt = self._pager(r, back=None,
                                  next_cmd=lambda: self._goto_page(1),
                                  next_label="On-monitor procedure ›")
                nxt.state(["disabled"] if self.busy else ["!disabled"])
                r += 1
            else:
                self._big(f"Install  {lut_name}  on the monitor, then Verify.", r)
                r += 1
                if white or black:
                    lvl = ttk.Label(
                        self.card,
                        text=f"Levels to enter →   White (max): {white or '—'} nits      Black (min): {black or '—'} nits",
                        font=("", 12, "bold"), foreground="#2e8b2e",
                    )
                    lvl.grid(row=r, column=0, sticky="w", pady=(0, 6))
                    r += 1
                if plan is not None and plan.install_steps:
                    self._steps_box(f"{plan.label} — import {lut_name}, exact procedure",
                                    plan.install_steps, r)
                else:
                    self._big(
                        "Import the LUT in the monitor's calibration wizard. At the wizard's "
                        "level / HDR-Range steps, enter the saved values above (first time, "
                        "measure them with “Read peak nits”). Keep the monitor state fixed, "
                        "then click Verify — that measurement, with this LUT active, is what "
                        "the next refine uses.",
                        r,
                    )
                r += 1
                buttons = ttk.Frame(self.card)
                buttons.grid(row=r, column=0, sticky="w", pady=(8, 0))
                r += 1
                widgets = [
                    ttk.Button(buttons, text="‹ Back to SD card", command=lambda: self._goto_page(0)),
                    ttk.Button(buttons, text="Read peak nits (HDR Range)", command=self._open_readout),
                    ttk.Button(buttons, text=f"Verify v{current.index} ▸", command=self._do_verify,
                               style="Accent.TButton"),
                ]
                if best is not None and best.index != current.index:
                    widgets.append(
                        ttk.Button(
                            buttons,
                            text=f"Re-verify v{best.index} (only if v{best.index} is still installed)",
                            command=lambda: self._do_verify_recheck(best.index),
                        )
                    )
                # A hardware capture of THIS LUT is the only evidence of what the
                # monitor really does with an imported cube; refining against it
                # pre-compensates firmware that reshapes the LUT (Cine 7 500 RX).
                if current.verify_rechecks:
                    widgets.append(
                        ttk.Button(
                            buttons,
                            text="Refine from hardware measurement ▸",
                            command=lambda: self._do_refine_from_hardware(current.verify_rechecks[-1]),
                        )
                    )
                for col, w in enumerate(widgets):
                    w.grid(row=0, column=col, padx=(0 if col == 0 else 8, 0))
                    w.state(["disabled"] if self.busy else ["!disabled"])
        else:
            self._big("The latest LUT is verified. Refine once more, or finish and keep the best.", r)
            r += 1
            rf = ttk.Button(self.card, text="Refine again", command=self._do_refine_manual)
            rf.grid(row=r, column=0, sticky="w", pady=(4, 0))
            r += 1
            rf.state(["disabled"] if self.busy else ["!disabled"])

        if self.page == 0 and best is not None:
            self._render_scoreboard(rows, best, base_row=r)
            r += 1
            done = status.state in ("converged", "regressed")
            label = f"✓ Finish — keep v{best.index} ▸" if done else f"Finish — keep best (v{best.index}) ▸"
            fin = ttk.Button(self.card, text=label, command=self._finish)
            fin.grid(row=r, column=0, sticky="w", pady=(10, 0))
            fin.state(["disabled"] if self.busy else ["!disabled"])

    def _card_done(self) -> None:
        session = self.session
        best = session.selected_iteration
        r = 0
        self._big("✓ Calibrated", r, font=("", 15, "bold"))
        r += 1
        if best is not None:
            rows = report.iteration_rows(session, root=".")
            # A hardware recheck measures the LUT as the monitor really applies
            # it; the iteration's own verify may be a software prediction. When
            # both exist the hardware number is the honest one to headline.
            hardware = [x for x in rows if x.index == best.index and x.is_recheck]
            software = next((x for x in rows if x.index == best.index and not x.is_recheck), None)
            row = hardware[-1] if hardware else software
            source = "measured on the monitor" if hardware else (
                "software prediction — not yet verified on the monitor"
                if report.is_software_verified(best) else "measured on the monitor"
            )
            errs = (f"white {_fmt(row.white_err)}, R/G/B {_fmt(row.red_err)}/{_fmt(row.green_err)}/{_fmt(row.blue_err)}"
                    if row else "")
            acc = report.accuracy_percent(row) if row else None
            if acc is not None:
                self._big(f"Accuracy: {acc:.0f}%  ({report.accuracy_label(acc)})", r, font=("", 14, "bold"))
                r += 1
                self._big(f"({source})", r)
                r += 1
            self._big(f"Kept iteration v{best.index}  ({errs}).", r)
            r += 1
        white, black = report.saved_levels(session)
        if white or black:
            self._big(f"Monitor levels — White (max): {white or '—'} nits · Black (min): {black or '—'} nits", r)
            r += 1
        exported = getattr(self, "_last_export", None)
        if exported:
            self._big(f"Exported to  {exported}", r)
            r += 1
            ttk.Button(self.card, text="Reveal exported LUT", command=lambda: self._reveal(exported)).grid(
                row=r, column=0, sticky="w", pady=(4, 0)
            )
            r += 1
            panel = SDCardPanel(
                self.card, self, lambda e=exported: [Path(e)],
                purpose="calibrated LUT",
                done_hint=(f"put the card in the monitor and import {Path(exported).name} "
                           "in its calibration wizard whenever this monitor needs reloading."),
            )
            panel.grid(row=r, column=0, sticky="ew", pady=(6, 0))
            r += 1
        # A finished session can still need a hardware verify (the software
        # refine loop verifies in signal space, and Finish can be clicked before
        # the LUT is ever installed) — offer the recheck rather than stranding it.
        if best is not None:
            ttk.Button(
                self.card,
                text=f"Verify installed LUT (re-verify v{best.index}) ▸",
                command=lambda: self._do_verify_recheck(best.index),
            ).grid(row=r, column=0, sticky="w", pady=(10, 0))
            r += 1
            # Firmware that reshapes an imported cube (Cine 7 500 RX) only shows
            # itself in a hardware capture — refining against it is the fix.
            if best.verify_rechecks:
                ttk.Button(
                    self.card,
                    text="Refine from hardware measurement ▸",
                    command=lambda: self._do_refine_from_hardware(best.verify_rechecks[-1]),
                ).grid(row=r, column=0, sticky="w", pady=(4, 0))
                r += 1
        ttk.Button(self.card, text="Calibrate another ▸", command=self._restart).grid(
            row=r, column=0, sticky="w", pady=(10, 0)
        )

    def _render_scoreboard(self, rows, best, base_row: int) -> None:
        board = self._tint_box("Progress so far", base_row, pady=(10, 0))
        # Rechecks earn a row of their own: on firmware that reshapes imported
        # cubes they are the only honest measurement of an iteration.
        i = 0
        for r in rows:
            if not r.has_verify:
                continue
            i += 1
            is_best = best is not None and r.label == best.label
            mark = "  ✓ best" if is_best else ""
            kind = " (predicted)" if r.is_software else (" (measured)" if r.is_recheck else "")
            acc = report.accuracy_percent(r)
            acc_str = f"{acc:>3.0f}%" if acc is not None else "  —"
            de_str = f"   ·   dE {r.mean_de2000:.1f}" if r.mean_de2000 is not None else ""
            text = (f"v{r.label}:  {acc_str} primaries{de_str}{kind}   ·   white {_fmt(r.white_err)}  "
                    f"R/G/B {_fmt(r.red_err)}/{_fmt(r.green_err)}/{_fmt(r.blue_err)}{mark}")
            color = "#2e8b2e" if is_best else (PALETTE["muted"] if r.is_software else PALETTE["ink"])
            tk.Label(board, text=text, bg=PALETTE["tint"], fg=color,
                     font=("Helvetica Neue", 12), anchor="w").grid(
                row=i, column=0, columnspan=2, sticky="w", padx=14, pady=(0, 2))
        tk.Frame(board, bg=PALETTE["tint"], height=8).grid(row=i + 1, column=0)

    def _render_table(self) -> None:
        if not hasattr(self, "tree"):
            return
        self.tree.delete(*self.tree.get_children())
        if self.session is None:
            return
        rows = report.iteration_rows(self.session, root=".")
        best = report.best_verified_iteration(rows)
        for r in rows:
            if not r.has_verify:
                vals = (r.label, "—", "—", "—", "—", "—", "pending")
            else:
                vals = (r.label, _fmt(r.white_err), _fmt(r.red_err), _fmt(r.green_err),
                        _fmt(r.blue_err), (f"{r.gray50_dev:+.4f}" if r.gray50_dev is not None else "—"),
                        "✓" if not r.is_recheck else "recheck")
            tags = ("best",) if best is not None and r.index == best.index and not r.is_recheck else ()
            self.tree.insert("", tk.END, iid=r.label, values=vals, tags=tags)

    # -- process/progress helpers -------------------------------------------

    def _set_process(self, text: str) -> None:
        self.process_label.configure(text=(f"Now: {text}" if text else ""))

    def _progress_determinate(self, maximum: int, value: int) -> None:
        self.progress.configure(mode="determinate", maximum=max(1, maximum), value=value)
        self.progress.grid()

    def _progress_indeterminate(self, on: bool) -> None:
        if on:
            self.progress.configure(mode="indeterminate")
            self.progress.grid()
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.grid_remove()

    def _progress_hide(self) -> None:
        self.progress.stop()
        self.progress.grid_remove()

    # -- flow entry ----------------------------------------------------------

    def _open_guide(self) -> None:
        if getattr(self, "guide_win", None) is not None and self.guide_win.winfo_exists():
            self.guide_win.lift()
            return
        win = tk.Toplevel(self)
        win.title("Setup guide")
        win.geometry("640x620")
        win.transient(self)
        ttk.Label(win, text="Monitor settings & workflow", font=("", 14, "bold")).pack(
            anchor="w", padx=16, pady=(14, 4)
        )
        frame = ttk.Frame(win)
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 12))
        text = tk.Text(frame, wrap="word", font=("", 11), padx=8, pady=8,
                       relief="flat", height=10)
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(side="left", fill="both", expand=True)
        text.insert("1.0", SETUP_GUIDE)
        text.configure(state="disabled")
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=(0, 12))
        self.guide_win = win

    def _reload_sessions(self, *, land_on: str = CHOOSE) -> None:
        try:
            self.summaries = discover_session_summaries(self.sessions_root)
        except OSError:
            self.summaries = []
        self.phase = land_on
        self._render()

    def _start(self) -> None:
        choice = self.choose_var.get()
        if choice == NEW_MONITOR:
            name = self.new_name.get().strip()
            if not name:
                messagebox.showinfo("New monitor", "Enter a name for the monitor.")
                return
            try:
                self.session_dir = steps.create_session_from_preset(
                    self.sessions_root, name, self.new_preset.get()
                )
            except ValueError as exc:
                messagebox.showerror("New monitor", str(exc))
                return
            self._log(f"Created session {self.session_dir}")
        else:
            self.session_dir = self.sessions_root / choice
        self._reload_session()
        self._enter_flow()

    def _enter_flow(self) -> None:
        s = self.session
        if s is None:
            self.phase = CHOOSE
            self._render()
        elif s.baseline_path is None or not Path(s.baseline_path).exists():
            self.phase = BASELINE
            self._render()
        elif not s.iterations:
            # Baseline captured but no LUT yet — show the load card with a
            # "Start live calibration" button (don't silently auto-fire on resume).
            self.phase = LOAD
            self._render()
        elif s.selected_iteration is not None:
            self.phase = DONE
            self._render()
        else:
            self.phase = LOAD
            self._render()

    def _reload_session(self) -> None:
        if self.session_dir is None:
            self.session = None
            return
        try:
            self.session = load_session(self.session_dir)
        except (OSError, ValueError, KeyError) as exc:
            self.session = None
            self._log(f"Could not load session: {exc}")
            return
        self._sync_refine_mode_to_plan()

    def _sync_refine_mode_to_plan(self) -> None:
        """Adopt the device plan's refine defaults (mode + matrix damping).

        SDI/wireless plans want matrix mode at a gentle damping. Sets the
        Advanced-panel defaults when the session/device context changes; a
        manual override in that panel stays until the next switch.
        """
        plan = self._session_plan()
        if plan is not None:
            self.refine_mode.set(plan.refine_mode)
            self.damping.set(plan.refine_damping)

    def _restart(self) -> None:
        self.session = None
        self.session_dir = None
        self._last_export = None
        self._pending_plan = None
        self._reload_sessions(land_on=DEVICE)

    # -- actions -------------------------------------------------------------

    def _do_baseline(self) -> None:
        if self.busy:
            return
        if not messagebox.askyesno(
            "Baseline",
            "Monitor on the freshly-imported identity LUT (activated, not bypass), "
            "probe on the panel? This captures the pre-correction baseline.",
        ):
            return
        out = report.baseline_capture_path(self.session_dir)
        self._start_capture(
            BASELINE_SEQUENCE, out,
            on_complete=lambda: steps.record_baseline(self.session_dir, out),
            after=self._after_baseline,
            title="Baseline",
        )

    def _after_baseline(self) -> None:
        # Range checksum before committing to a long sweep: a lifted baseline
        # black (~1 nit instead of ~0.16) means the monitor's declared input
        # range doesn't match the feed, and every measurement after is junk.
        warning = None
        try:
            baseline = read_measurements_json(report.baseline_capture_path(self.session_dir))
            warning = (
                report.feed_range_warning(baseline)
                or report.baseline_identity_warning(baseline)
                or report.luminance_target_warning(baseline, STUDIO_NITS)
            )
        except (OSError, ValueError):
            pass
        if warning is not None:
            self._flog(f"baseline range check: {warning}")
            self._log(warning)
            if not messagebox.askyesno(
                "Input range check", warning + "\n\nStart the live sweep anyway?"
            ):
                self._render()
                return
        # Characterize the panel live (adjust the signal per-point with the probe,
        # identity LUT still loaded) and build the correction directly — instead of
        # a blind LUT that the monitor mangles on import.
        self._start_live()

    def _start_live(self) -> None:
        self._flog(f"_start_live called: busy={self.busy} session_dir={self.session_dir}")
        if self.busy:
            self._flog("_start_live returned early (busy)")
            return
        if self.session is None or self.session_dir is None:
            self._flog("_start_live returned early (no session)")
            messagebox.showinfo("Live calibration", "Select a session first.")
            return
        session_dir = self.session_dir
        try:
            size = self.lut_size.get()
            # "auto": run_live_calibration infers legal vs full from the measured
            # baseline black level, so the LUT matches the actual feed.
            lut_range = "auto"
            self._flog(f"live setup: size={size} range={lut_range}")
            self.live_cancel = False
            self._set_busy(True)
            self._set_process("Live calibration: converging colors on target…")
            self._progress_determinate(LIVE_POINTS, 0)
            self._flog("live setup: listing displays…")
            display = choose_external_display(list_displays())
            self._flog(f"live setup: display={getattr(display, 'display_id', None)} "
                       f"geom={getattr(display, 'geometry', None)}")
            self._open_patch_window(display)
            self._flog("live setup: patch window open")
            self._live_reads = 0
            verify_patches = load_patch_sequence(
                resolve_existing_path(VERIFY_SEQUENCE, self.paths.resource_root, self.paths.user_data_root)
            )
        except Exception:  # noqa: BLE001
            import traceback
            self._flog("_start_live SETUP ERROR:\n" + traceback.format_exc())
            self._set_busy(False)
            self._progress_hide()
            self._close_patch_window()
            self._set_process("Live calibration failed to start — see log.")
            self._render()
            return
        self._flog(f"live start: session={session_dir} size={size} range={lut_range}")
        self._log("Live calibration started — identity LUT stays loaded, no SD reloads. Press Esc to cancel.")

        def on_progress(done: int, total: int) -> None:
            self.worker_messages.put(("live-progress", (done, total)))

        def work() -> None:
            session = None
            try:
                # Try to open one persistent probe session (fast); fall back to
                # relaunching spotread per reading if it can't be established.
                try:
                    session = SpotreadSession(
                        _spotread_interactive_command(self.paths),
                        read_timeout=self.timeout.get(), log=self._flog,
                    ).start()
                    self._flog("live: using fast persistent probe session")
                except (ProbeError, OSError) as exc:
                    self._flog(f"live: persistent session unavailable, per-read fallback ({exc})")
                    session = None

                measure = lambda r, g, b: self._live_measure(r, g, b, session)
                cube = steps.run_live_calibration(
                    session_dir, measure,
                    size=size, lut_range=lut_range, on_progress=on_progress,
                )
                self._flog(f"live sweep done: {cube}; starting software refine")
                # Converge the correction in signal space while the monitor is
                # still on the identity LUT: the deterministic import + raw
                # full-domain pipeline means displaying lut.lookup(patch) shows
                # exactly what the loaded cube would — so the refine loop that
                # used to cost an SD trip per round runs here unattended.
                self.worker_messages.put(("live-stage", "Refining in software (no SD trips)…"))
                cube = steps.run_signal_refine(
                    session_dir, measure, verify_patches,
                    size=size, on_progress=on_progress,
                )
                self._flog(f"live done: {cube}")
                self.worker_messages.put(("live-done", cube))
            except _LiveCancelled:
                self._flog("live cancelled by user")
                self.worker_messages.put(("live-cancelled", None))
            except Exception as exc:  # noqa: BLE001
                import traceback
                self._flog("live ERROR:\n" + traceback.format_exc())
                self.worker_messages.put(("live-error", str(exc)))
            finally:
                if session is not None:
                    session.close()

        threading.Thread(target=work, daemon=True).start()

    def _live_measure(self, r: float, g: float, b: float, session=None) -> tuple[float, float, float]:
        # Runs in the worker thread: ask the main thread to show the patch, wait,
        # settle, then read the probe here (blocks the worker, not the UI). Uses
        # the persistent SpotreadSession if given, else relaunches spotread.
        if self.live_cancel:
            raise _LiveCancelled()
        rgb8 = tuple(round(max(0.0, min(1.0, v)) * 255) for v in (r, g, b))
        color = f"#{rgb8[0]:02x}{rgb8[1]:02x}{rgb8[2]:02x}"
        shown = threading.Event()
        self.worker_messages.put(("live-show", (color, shown)))
        shown.wait(timeout=3.0)
        time.sleep(max(0.05, self.settle_ms.get() / 1000.0))
        if self.live_cancel:
            raise _LiveCancelled()

        self._live_reads += 1
        self.worker_messages.put(("live-tick", (self._live_reads, color)))
        last_err: Exception | None = None
        for attempt in range(3):
            # Last attempt goes through a fresh one-shot spotread even when a
            # persistent session is open: a dead pty session must not abort a
            # 30-minute sweep when the instrument itself is still fine.
            use_session = session is not None and attempt < 2
            self._flog(f"read #{self._live_reads} {color} attempt {attempt + 1}"
                       + ("" if use_session else " (one-shot)"))
            try:
                xyz = session.read() if use_session else read_spotread(
                    _probe_command(self.paths), timeout=self.timeout.get()
                ).xyz
                self._flog(f"read #{self._live_reads} ok -> {xyz}")
                return xyz
            except ProbeError as exc:
                last_err = exc
                self._flog(f"read #{self._live_reads} FAILED: {exc}")
                if self.live_cancel:
                    raise _LiveCancelled() from exc
                time.sleep(0.5)
        raise ProbeError(f"Probe failed 3× on patch {color}: {last_err}")

    def _show_live_color(self, color: str) -> None:
        if self.patch_window is not None and self.patch_window.winfo_exists():
            self.patch_window.configure(bg=color)
            self.patch_window.deiconify()
            self.patch_window.lift()

    def _cancel_live(self) -> None:
        self.live_cancel = True
        self._set_process("Cancelling live calibration…")

    def _do_verify(self) -> None:
        if self.busy:
            return
        try:
            target = report.verify_capture_target(self.session, self.session_dir)
        except ValueError as exc:
            messagebox.showinfo("Verify", str(exc))
            return
        # A verify only means anything against the LUT that's actually on the
        # monitor — capturing with a different one installed mislabels the data
        # (test 69: installed v2, verified v3). Confirm before every capture.
        if not messagebox.askyesno(
            "Verify",
            f"This will verify v{target.iteration.index} — is "
            f"lut_v{target.iteration.index}.cube the calibration currently active "
            "on the monitor?\n\nIf you installed a different version, use its "
            "Re-verify (recheck) button instead.",
        ):
            return
        self._start_capture(
            VERIFY_SEQUENCE, target.output_path,
            on_complete=lambda: steps.record_verify(
                self.session_dir, target.iteration.index, target.output_path, is_recheck=False
            ),
            after=self._after_verify,
            title=f"Verify v{target.iteration.index}",
        )

    def _do_verify_recheck(self, index: int) -> None:
        """Re-verify an OLDER iteration (its LUT must be the one on the monitor).

        Records a verify_vN_recheck_M.json against that iteration instead of
        mislabeling the capture as the current (possibly unverified) one, and
        never auto-refines — it's a health check, not a calibration round.
        """
        if self.busy:
            return
        try:
            target = report.verify_capture_target(self.session, self.session_dir, index=index)
        except ValueError as exc:
            messagebox.showinfo("Re-verify", str(exc))
            return
        if not messagebox.askyesno(
            "Re-verify",
            f"Is lut_v{index}.cube the calibration currently active on the monitor?\n\n"
            "The recheck is only meaningful against that exact LUT.",
        ):
            return
        self._recheck_path = target.output_path
        self._start_capture(
            VERIFY_SEQUENCE, target.output_path,
            on_complete=lambda: steps.record_verify(
                self.session_dir, target.iteration.index, target.output_path, is_recheck=True
            ),
            after=self._after_recheck,
            title=f"Re-verify v{target.iteration.index} (recheck)",
        )

    def _after_recheck(self) -> None:
        self._reload_session()
        self._render()
        path = getattr(self, "_recheck_path", None)
        if path is None:
            return
        try:
            line = report.delta_e_line(
                read_measurements_json(path),
                target_name=self.session.target_name,
                target_gamma=self.session.target_gamma,
            )
        except OSError:
            return
        if line:
            self._log(f"recheck {line}")
            self._flog(f"recheck {path}: {line}")

    def _after_verify(self) -> None:
        self._reload_session()
        self._render()  # scoreboard updates immediately
        self._log_verify_delta_e()
        if len(self.session.iterations) >= MAX_ROUNDS:
            self._log(f"Reached {MAX_ROUNDS} rounds; finish to keep the best.")
            self._goto(LOAD)
            return
        # A single hardware verify that's already broadcast-grade needs no
        # further round — on a near-verbatim monitor the extra cube is built
        # and then rejected (v3→v4 here rang the white point). Stop at the first
        # good result instead of always spending a confirming round.
        current = self.session.current_iteration
        if (
            current is not None
            and current.verify_path
            and not report.is_software_verified(current)
            and report.is_shippable(current.verify_path, self.session)
        ):
            self._log(
                f"v{current.index} is already broadcast-grade (dE ≤ {report.SHIP_DE2000:.0f}). "
                "No extra round needed — Finish to keep it."
            )
            self._goto(LOAD)
            return
        # Refining past convergence chases probe noise (hardware v3→v4 rang the
        # white point) and leaves an unverified LUT as the verify target — a
        # mislabeling trap. Once converged/regressed, stop and point at Finish.
        status = report.convergence_status(report.iteration_rows(self.session, root="."))
        if status.state in ("converged", "regressed"):
            self._log(f"{status.message} No further refine — Finish to keep the best.")
            self._goto(LOAD)
            return
        self._run_step(
            "Refining the next LUT",
            lambda: steps.refine_lut(
                self.session_dir, size=self.lut_size.get(),
                damping=self.damping.get(), mode=self.refine_mode.get(),
            ),
            on_done=lambda: self._goto(LOAD),
        )

    def _log_verify_delta_e(self) -> None:
        iteration = self.session.current_iteration if self.session else None
        if iteration is None or not iteration.verify_path:
            return
        try:
            line = report.delta_e_line(
                read_measurements_json(iteration.verify_path),
                target_name=self.session.target_name,
                target_gamma=self.session.target_gamma,
            )
        except OSError:
            return
        if line:
            self._log(f"v{iteration.index} {line}")
            self._flog(f"v{iteration.index} {line}")
        try:
            neutral = report.neutral_axis_warning(
                read_measurements_json(iteration.verify_path),
                target_name=self.session.target_name,
                target_gamma=self.session.target_gamma,
            )
        except OSError:
            neutral = None
        if neutral:
            self._log(neutral)
            self._flog(f"v{iteration.index} neutral-axis: {neutral}")

    def _do_refine_manual(self) -> None:
        self._run_step(
            "Refining the next LUT",
            lambda: steps.refine_lut(
                self.session_dir, size=self.lut_size.get(),
                damping=self.damping.get(), mode=self.refine_mode.get(),
            ),
            on_done=lambda: self._goto(LOAD),
        )

    def _do_refine_from_hardware(self, capture_path: str) -> None:
        if self.busy:
            return
        self._log(f"Refining against hardware capture {Path(capture_path).name}")

        def work() -> str:
            steps.clear_selection(self.session_dir)  # a finished session reopens
            return steps.refine_lut(
                self.session_dir, size=self.lut_size.get(),
                damping=self.damping.get(), mode=self.refine_mode.get(),
                verify_path=capture_path,
            )

        self._run_step(
            "Refining from the hardware measurement",
            work,
            on_done=lambda: self._goto(LOAD),
        )

    def _finish(self) -> None:
        rows = report.iteration_rows(self.session, root=".")
        best = report.best_verified_iteration(rows)
        if best is None:
            messagebox.showinfo("Finish", "Verify at least one LUT first.")
            return

        def work() -> str:
            steps.select_iteration(self.session_dir, best.index)
            message = steps.export_selected(self.session_dir, EXPORTS_DIR)
            self._last_export = message.replace("Exported ", "")
            return message

        self._run_step("Selecting the best and exporting", work, on_done=lambda: self._goto(DONE))

    def _goto(self, phase: str) -> None:
        self._reload_session()
        self.phase = phase
        self.page = 0
        self._render()

    def _reveal(self, path: str) -> None:
        try:
            subprocess.run(["open", "-R", str(path)], check=False)
        except OSError as exc:
            self._log(f"Could not reveal {path}: {exc}")

    # -- probe readout tool (live nits, no display takeover) -----------------

    def _open_readout(self) -> None:
        if self.busy or self.capture is not None:
            messagebox.showinfo("Probe readout", "Finish the current measurement first.")
            return
        if self.readout_win is not None and self.readout_win.winfo_exists():
            self.readout_win.lift()
            return
        win = tk.Toplevel(self)
        win.title("Brightness")
        win.geometry("360x600")
        win.configure(bg=IOS["bg"])
        win.transient(self)

        self.readout_swatch = "white"
        head = tk.Frame(win, bg=IOS["bg"])
        head.pack(fill="x", padx=24, pady=(22, 2))
        tk.Label(head, text="Brightness & levels", bg=IOS["bg"], fg=IOS["label"],
                 font=("Helvetica Neue", 24, "bold")).pack(anchor="w")
        tk.Label(head, text=f"The app paints the swatch on the monitor. White: tune the Studio "
                 f"slider to ~{STUDIO_NITS} nits. Black: read the level for the HDR-range step.",
                 bg=IOS["bg"], fg=IOS["secondary"], font=("Helvetica Neue", 12),
                 justify="left", wraplength=312).pack(anchor="w", pady=(2, 0))

        # White / Black swatch selector (black is for the HDR-range min level).
        seg = tk.Frame(win, bg=IOS["bg"])
        seg.pack(pady=(10, 0))
        self.swatch_btns = {}
        for i, name in enumerate(("white", "black")):
            b = ttk.Button(seg, text=name.capitalize(), width=9,
                           command=lambda n=name: self._set_swatch(n))
            b.grid(row=0, column=i, padx=3)
            self.swatch_btns[name] = b

        # Hero: a live gauge that turns green on target.
        self.readout_gauge = tk.Canvas(win, width=312, height=232, bg=IOS["bg"],
                                       highlightthickness=0)
        self.readout_gauge.pack(padx=24, pady=(10, 2))

        # Directional guidance + secondary reading.
        self.readout_target = tk.Label(win, text="Show white, probe on the panel…",
                                       bg=IOS["bg"], fg=IOS["secondary"],
                                       font=("Helvetica Neue", 16, "bold"), wraplength=312)
        self.readout_target.pack(pady=(2, 1))
        self.readout_sub = tk.Label(win, text="", bg=IOS["bg"], fg=IOS["secondary"],
                                    font=("Helvetica Neue", 12))
        self.readout_sub.pack()

        btns = tk.Frame(win, bg=IOS["bg"])
        btns.pack(pady=(14, 2))
        self.readout_toggle = ttk.Button(btns, text="Pause", command=self._toggle_readout)
        self.readout_toggle.grid(row=0, column=0, padx=5)
        ttk.Button(btns, text="Done", command=self._close_readout,
                   style="Accent.TButton").grid(row=0, column=1, padx=5)
        self.readout_status = tk.Label(win, text="starting…", bg=IOS["bg"],
                                       fg=IOS["secondary"], font=("Helvetica Neue", 11))
        self.readout_status.pack(pady=(4, 0))

        # Grouped "levels" section (for the wizard's HDR-range entry).
        card = tk.Frame(win, bg=IOS["bg"])
        card.pack(fill="x", padx=24, pady=(16, 18))
        tk.Label(card, text="MONITOR WIZARD LEVELS", bg=IOS["bg"], fg=IOS["secondary"],
                 font=("Helvetica Neue", 10, "bold")).pack(anchor="w")
        self.readout_levels = tk.Label(card, text="", bg=IOS["bg"], fg=IOS["green"],
                                       font=("Helvetica Neue", 12), justify="left")
        self.readout_levels.pack(anchor="w", pady=(4, 8))
        self.save_white_btn = ttk.Button(
            card, text="Save current as White (max)", command=lambda: self._save_level("white"))
        self.save_white_btn.pack(fill="x", pady=2)
        self.save_black_btn = ttk.Button(
            card, text="Save current as Black (min)", command=lambda: self._save_level("black"))
        self.save_black_btn.pack(fill="x", pady=2)

        win.protocol("WM_DELETE_WINDOW", self._close_readout)
        self.readout_win = win
        self.readout_active = True
        self._draw_brightness_gauge(None)
        self._refresh_readout_levels()
        self._set_swatch("white")
        self._readout_cycle()

    def _show_readout_swatch(self) -> None:
        """Drive a full-white field onto the monitor to measure against.

        The TX just passes the live feed through, so there is nothing white to
        read while adjusting Studio brightness unless the app supplies it (only
        the RX shows its own swatch). Falls back to the monitor's own patch when
        no external display is detected, so we never take over the Mac's screen.
        """
        display = choose_external_display(list_displays())
        if display is None:
            self._log("Brightness: no external display detected — relying on the monitor's own white patch.")
            return
        self._open_patch_window(display)
        color = "#000000" if self.readout_swatch == "black" else "#ffffff"
        if self.patch_window is not None and self.patch_window.winfo_exists():
            self.patch_window.configure(bg=color)
            self.patch_window.deiconify()
            self.patch_window.lift()
        if self.readout_win is not None and self.readout_win.winfo_exists():
            self.readout_win.lift()  # keep the gauge reachable on the Mac

    def _set_swatch(self, name: str) -> None:
        """Switch the on-monitor swatch (white for brightness, black for HDR min)."""
        self.readout_swatch = name
        for other, btn in getattr(self, "swatch_btns", {}).items():
            btn.configure(style="Accent.TButton" if other == name else "TButton")
        self._show_readout_swatch()
        self._draw_brightness_gauge(self._last_readout_y)
        if self._last_readout_y is not None:
            self._update_brightness_guidance(self._last_readout_y)

    def _draw_brightness_gauge(self, current: float | None) -> None:
        """iOS-style 270° gauge: track, value arc, target tick, centred number."""
        c = getattr(self, "readout_gauge", None)
        if c is None or not c.winfo_exists():
            return
        c.delete("all")
        w, h = int(c["width"]), int(c["height"])
        _round_rect(c, 2, 2, w - 2, h - 2, r=26, fill=IOS["card"], outline="")
        target = STUDIO_NITS
        black_mode = getattr(self, "readout_swatch", "white") == "black"
        cx, cy, r, thick = w / 2, h / 2 + 6, 86, 18
        start, sweep = 225, -270  # 90° gap at the bottom
        c.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=sweep,
                     style="arc", width=thick, outline=IOS["track"])
        num, num_col = "—", IOS["secondary"]
        if current is not None and black_mode:
            num, num_col = f"{current:.2f}", IOS["label"]
        elif current is not None and target > 0:
            frac = max(0.0, min(current / (2 * target), 1.0))
            on_target = abs(current - target) / target <= 0.05
            col = IOS["green"] if on_target else IOS["orange"]
            c.create_arc(cx - r, cy - r, cx + r, cy + r, start=start, extent=sweep * frac,
                         style="arc", width=thick, outline=col)
            num, num_col = f"{current:.0f}", col
        if not black_mode:
            # Target tick at the top (12 o'clock = the halfway point of the sweep).
            ang = math.radians(90)
            c.create_line(cx + (r - thick) * math.cos(ang), cy - (r - thick) * math.sin(ang),
                          cx + (r + thick * 0.5) * math.cos(ang), cy - (r + thick * 0.5) * math.sin(ang),
                          fill=IOS["label"], width=3, capstyle="round")
        c.create_text(cx, cy - 6, text=num, fill=num_col,
                      font=("Helvetica Neue", 40 if black_mode else 48, "bold"))
        c.create_text(cx, cy + 32, text="nits", fill=IOS["secondary"], font=("Helvetica Neue", 14))
        c.create_text(cx, cy + r + 22, text=("black (min)" if black_mode else f"target ~{target}"),
                      fill=IOS["secondary"], font=("Helvetica Neue", 12))

    def _refresh_readout_levels(self) -> None:
        if self.readout_win is None or not self.readout_win.winfo_exists():
            return
        if self.session is None:
            self.readout_levels.configure(text="Start a session to save levels.", fg=IOS["secondary"])
            self.save_white_btn.state(["disabled"])
            self.save_black_btn.state(["disabled"])
            return
        white, black = report.saved_levels(self.session)
        self.readout_levels.configure(
            text=f"White (max)  {white or '—'} nits     ·     Black (min)  {black or '—'} nits",
            fg=IOS["green"],
        )
        self.save_white_btn.state(["!disabled"])
        self.save_black_btn.state(["!disabled"])

    def _save_level(self, which: str) -> None:
        if self.session_dir is None:
            return
        if self._last_readout_y is None:
            messagebox.showinfo("Save level", "Wait for a probe reading first.")
            return
        try:
            message = steps.save_probe_level(self.session_dir, which, self._last_readout_y)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Save level", str(exc))
            return
        self._log(message)
        self._reload_session()
        self._refresh_readout_levels()
        self._render()

    def _toggle_readout(self) -> None:
        self.readout_active = not self.readout_active
        self.readout_toggle.configure(text="Pause" if self.readout_active else "Resume")
        if self.readout_active:
            self.readout_status.configure(text="reading…", fg=IOS["secondary"])
            self._readout_cycle()
        else:
            self.readout_status.configure(text="paused", fg=IOS["secondary"])

    def _readout_cycle(self) -> None:
        if not self.readout_active or self.readout_win is None or not self.readout_win.winfo_exists():
            return
        timeout = self.timeout.get()
        paths = self.paths

        def work() -> None:
            try:
                reading = read_spotread(_probe_command(paths), timeout=timeout)
                self.worker_messages.put(("readout-value", reading.xyz))
            except ProbeError as exc:
                self.worker_messages.put(("readout-error", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def _close_readout(self) -> None:
        self.readout_active = False
        if self.readout_win is not None and self.readout_win.winfo_exists():
            self.readout_win.destroy()
        self.readout_win = None
        if self.capture is None:  # readout owns the white swatch; capture never overlaps it
            self._close_patch_window()

    # -- worker (fast in-process steps) --------------------------------------

    def _run_step(self, process: str, fn, on_done) -> None:
        if self.busy:
            return
        self._set_busy(True)
        self._set_process(process)
        self._progress_indeterminate(True)
        self._step_after = on_done
        self._log(f"{process}…")

        def work() -> None:
            try:
                message = fn()
                self.worker_messages.put(("step-done", (process, message)))
            except Exception as exc:  # noqa: BLE001
                self.worker_messages.put(("step-error", (process, str(exc))))

        threading.Thread(target=work, daemon=True).start()

    # -- probe capture loop --------------------------------------------------

    def _start_capture(self, patch_csv: str, output_path, on_complete, after, title: str) -> None:
        if self.readout_active:
            messagebox.showinfo("Probe readout", "Close the probe readout window first.")
            return
        try:
            patches = load_patch_sequence(
                resolve_existing_path(patch_csv, self.paths.resource_root, self.paths.user_data_root)
            )
        except OSError as exc:
            messagebox.showerror("Capture", f"Could not load patch list: {exc}")
            return
        # Auto-resume only when output_path already holds a genuinely partial
        # capture (some but not all patches) — a crash/force-quit mid-sweep.
        # Each call site targets a fresh path (baseline.json, verify_vN.json,
        # a new recheck index), so a *complete* file there would mean the
        # previous attempt already finished; skip-resuming that would silently
        # reuse stale data instead of measuring again as the operator asked.
        on_disk = read_measurements_json(output_path)
        resume = 0 < len(on_disk) < len(patches)
        existing = on_disk if resume else []
        # Open a fast persistent probe session for this capture (opening the
        # instrument briefly blocks the UI); fall back to per-patch spotread.
        session = None
        try:
            session = SpotreadSession(
                _spotread_interactive_command(self.paths),
                read_timeout=self.timeout.get(), log=self._flog,
            ).start()
            self._flog(f"{title}: using fast persistent probe session")
        except (ProbeError, OSError) as exc:
            self._flog(f"{title}: persistent session unavailable, per-read ({exc})")
        self.capture = CaptureJob(
            patches=patches, output_path=Path(output_path), resume=resume,
            on_complete=on_complete, after=after, title=title,
            measurements=existing, measured=latest_measurements_by_patch(existing),
            session=session,
        )
        self._set_busy(True)
        self._set_process(f"{title}: preparing…")
        self._progress_determinate(len(patches), len(self.capture.measured))
        self._log(f"{title} capture: {len(patches)} patches.")
        self._open_patch_window(choose_external_display(list_displays()))
        self._render()
        self._capture_next()

    def _open_patch_window(self, display: Display | None) -> None:
        if self.patch_window is None or not self.patch_window.winfo_exists():
            self.patch_window = tk.Toplevel(self)
            self.patch_window.title("SmallHD Patch")
            self.patch_window.bind("<Escape>", lambda _e: self._on_patch_escape())
        self.patch_window.configure(cursor="none")
        self.patch_window.overrideredirect(True)
        self.patch_window.attributes("-topmost", True)
        if display is not None:
            self.patch_window.geometry(display.geometry)
            self._log(f"Patch window on display {display.display_id}: {display.geometry}")
        else:
            self.patch_window.attributes("-fullscreen", True)
            self._log("No external display metadata; using Tk fullscreen.")

    def _capture_next(self) -> None:
        job = self.capture
        if job is None:
            return
        while job.index < len(job.patches):
            patch = job.patches[job.index]
            if job.resume and patch.name in job.measured:
                job.index += 1
                continue
            self._set_process(f"{job.title}: measuring {patch.name} ({job.index + 1} of {len(job.patches)})")
            self.progress.configure(value=job.index)
            self._show_patch(patch)
            self.after(max(50, self.settle_ms.get()), lambda p=patch: self._capture_measure(p))
            return
        self._capture_finish()

    def _show_patch(self, patch: Patch) -> None:
        r, g, b = patch.rgb8
        if self.patch_window is not None and self.patch_window.winfo_exists():
            self.patch_window.configure(bg=f"#{r:02x}{g:02x}{b:02x}")
            self.patch_window.deiconify()
            self.patch_window.lift()
            self.patch_window.focus_force()

    def _capture_measure(self, patch: Patch) -> None:
        if self.capture is None:
            return
        timeout = self.timeout.get()
        paths = self.paths
        session = self.capture.session

        def work() -> None:
            try:
                xyz = session.read() if session is not None else read_spotread(
                    _probe_command(paths), timeout=timeout
                ).xyz
            except ProbeError as exc:
                self.worker_messages.put(("capture-error", f"Measurement failed for {patch.name}: {exc}"))
                return
            self.worker_messages.put((
                "capture-measurement",
                Measurement(patch=patch, xyz=xyz, timestamp=datetime.now(UTC).isoformat()),
            ))

        threading.Thread(target=work, daemon=True).start()

    def _capture_finish(self) -> None:
        job = self.capture
        self.capture = None
        self._close_patch_window()
        if job is None:
            return
        if job.session is not None:
            job.session.close()
        try:
            message = job.on_complete()
        except Exception as exc:  # noqa: BLE001
            self._set_busy(False)
            self._progress_hide()
            messagebox.showerror(job.title, str(exc))
            self._log(f"{job.title} failed: {exc}")
            return
        self._set_busy(False)
        self._progress_hide()
        self._log(f"{job.title} complete. {message}")
        self._reload_session()
        job.after()

    def _on_patch_escape(self) -> None:
        if self.capture is not None:
            self._cancel_capture()
        else:
            self._cancel_live()

    def _cancel_capture(self) -> None:
        if self.capture is not None:
            self._log(f"{self.capture.title} cancelled.")
            if self.capture.session is not None:
                self.capture.session.close()
        self.capture = None
        self._close_patch_window()
        self._set_busy(False)
        self._progress_hide()
        self._set_process("Cancelled.")
        self._render()

    def _close_patch_window(self) -> None:
        if self.patch_window is not None and self.patch_window.winfo_exists():
            self.patch_window.destroy()
        self.patch_window = None

    # -- worker pump ---------------------------------------------------------

    def _drain_worker_messages(self) -> None:
        while True:
            try:
                kind, payload = self.worker_messages.get_nowait()
            except queue.Empty:
                break
            if kind == "step-done":
                _process, message = payload
                self._set_busy(False)
                self._progress_hide()
                self._set_process("")
                self._log(str(message))
                self._reload_session()
                after, self._step_after = self._step_after, None
                if after:
                    after()
            elif kind == "step-error":
                process, message = payload
                self._set_busy(False)
                self._progress_hide()
                self._set_process("")
                self._step_after = None
                self._log(f"{process} failed: {message}")
                messagebox.showerror(process, str(message))
                self._render()
            elif kind == "capture-measurement":
                self._save_capture_measurement(payload)
            elif kind == "capture-error":
                # If capture is already None, Escape already ran _cancel_capture()
                # synchronously on the main thread; this message is just the
                # worker's read() noticing the session got closed underneath it
                # (see probe.py's SpotreadSession) — not a real failure to report.
                already_cancelled = self.capture is None
                self._cancel_capture()
                if not already_cancelled:
                    messagebox.showerror("Capture failed", str(payload))
                    self._log(str(payload))
            elif kind == "live-show":
                color, event = payload
                self._show_live_color(color)
                event.set()
            elif kind == "live-stage":
                self._live_stage = str(payload)
                self.progress.configure(value=0)
                self._set_process(self._live_stage)
            elif kind == "live-progress":
                done, total = payload
                self.progress.configure(maximum=max(1, total), value=done)
                stage = getattr(self, "_live_stage", None)
                if stage:
                    self._set_process(f"{stage} {done} of {total} readings")
                else:
                    self._set_process(f"Live calibration: converged {done} of {total} colors")
            elif kind == "live-tick":
                count, color = payload
                self._set_process(f"Live calibration: measurement {count} (patch {color})")
            elif kind == "live-done":
                self._set_busy(False)
                self._progress_hide()
                self._set_process("")
                self._live_stage = None
                self._close_patch_window()
                self._log(f"Live calibration + software refine complete. Load {payload} (one SD trip).")
                self._reload_session()
                self._log_verify_delta_e()
                if self.session is not None and self.session.iterations:
                    health = report.live_health_warning(self.session.iterations[-1].notes)
                    if health is not None:
                        self._log(health)
                        messagebox.showwarning("Convergence health", health)
                self._goto(LOAD)
            elif kind == "live-cancelled":
                self._set_busy(False)
                self._progress_hide()
                self._close_patch_window()
                self._set_process("Live calibration cancelled.")
                self._render()
            elif kind == "live-error":
                self._set_busy(False)
                self._progress_hide()
                self._set_process("")
                self._close_patch_window()
                self._log(f"Live calibration failed: {payload}")
                messagebox.showerror("Live calibration", str(payload))
                self._render()
            elif kind == "readout-value":
                self._show_readout_value(payload)
            elif kind == "readout-error":
                if self.readout_win is not None and self.readout_win.winfo_exists():
                    self.readout_status.configure(text=f"probe error: {payload} — retrying",
                                                  fg=IOS["orange"])
                if self.readout_active:
                    self.after(1500, self._readout_cycle)
        self.after(100, self._drain_worker_messages)

    def _show_readout_value(self, xyz) -> None:
        if self.readout_win is None or not self.readout_win.winfo_exists():
            return
        x, y, z = xyz
        self._last_readout_y = y
        self._draw_brightness_gauge(y)
        total = x + y + z
        if total > 0:
            self.readout_sub.configure(text=f"xy {x / total:.4f}, {y / total:.4f}    ·    Y {y:.1f} cd/m²")
        else:
            self.readout_sub.configure(text=f"Y {y:.1f} cd/m²")
        self._update_brightness_guidance(y)
        self.readout_status.configure(text="● live", fg=IOS["green"])
        if self.readout_active:
            self.after(500, self._readout_cycle)

    def _update_brightness_guidance(self, y: float) -> None:
        if self.readout_win is None or not self.readout_win.winfo_exists():
            return
        if getattr(self, "readout_swatch", "white") == "black":
            self.readout_target.configure(
                text=f"Black level {y:.2f} nits — Save as Black (min).", fg=IOS["label"])
            return
        on_target, message = report.brightness_hint(y, STUDIO_NITS)
        self.readout_target.configure(
            text=message, fg=IOS["green"] if on_target else IOS["orange"]
        )

    def _save_capture_measurement(self, measurement: Measurement) -> None:
        job = self.capture
        if job is None:
            return
        job.measurements = [m for m in job.measurements if m.patch.name != measurement.patch.name]
        job.measurements.append(measurement)
        job.measured[measurement.patch.name] = measurement
        write_measurements_json(job.output_path, job.measurements)
        x, y, z = measurement.xyz
        self._log(f"  {measurement.patch.name}: XYZ {x:.4f}, {y:.4f}, {z:.4f}")
        job.index += 1
        self.progress.configure(value=job.index)
        self._capture_next()

    # -- misc ----------------------------------------------------------------

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.configure(cursor="watch" if busy else "")

    def _flog(self, message: str) -> None:
        """Thread-safe file log — safe from worker threads (Tk _log is not)."""
        if self._logfile is None:
            return
        with self._log_lock:
            try:
                self._logfile.write(f"{datetime.now(UTC).isoformat()} {message}\n")
                self._logfile.flush()
            except OSError:
                pass

    def _log(self, message: str) -> None:
        self._flog(message)
        if hasattr(self, "log"):
            self.log.insert(tk.END, message + "\n")
            self.log.see(tk.END)


def acquire_single_instance_lock(user_data_root) -> object | None:
    """Hold an exclusive lock for the app's lifetime, or None if already held.

    Two app copies sharing the one probe fail confusingly (the second gets
    hid_open_port/Communications failure mid-capture of the first), so refuse
    to start a second instance outright. The lock dies with the process, so a
    crashed app never leaves a stale lock behind.
    """
    import fcntl

    lock_path = Path(user_data_root) / ".app.lock"
    handle = open(lock_path, "w")  # noqa: SIM115 - must outlive this function
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def run_app(paths: AppPaths) -> None:
    # Work relative to the data root so session paths match the CLI's.
    os.chdir(paths.user_data_root)
    lock = acquire_single_instance_lock(paths.user_data_root)
    if lock is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(
            "SmallHD Calibration",
            "Another copy of SmallHD Calibration is already running.\n\n"
            "Two copies cannot share the probe — switch to the running "
            "window instead (check the Dock).",
        )
        root.destroy()
        return
    app = SmallHDCalApp(paths)
    app._single_instance_lock = lock  # keep the handle (and lock) alive
    app.mainloop()


def _probe_command(paths: AppPaths) -> ProbeCommand:
    bundled = find_bundled_spotread(paths.resource_root)
    if bundled is None:
        bundled = find_bundled_spotread(paths.user_data_root)
    if bundled is not None:
        return [str(bundled), *SPOTREAD_ARGS]
    return ["spotread", *SPOTREAD_ARGS]


def _spotread_interactive_command(paths: AppPaths) -> ProbeCommand:
    bundled = find_bundled_spotread(paths.resource_root) or find_bundled_spotread(paths.user_data_root)
    exe = str(bundled) if bundled is not None else "spotread"
    return [exe, *SPOTREAD_INTERACTIVE_ARGS]
