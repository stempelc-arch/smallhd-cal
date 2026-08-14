from __future__ import annotations

import ctypes
from dataclasses import dataclass


@dataclass(frozen=True)
class Display:
    display_id: int
    x: int
    y: int
    width: int
    height: int
    is_main: bool = False

    @property
    def geometry(self) -> str:
        return f"{self.width}x{self.height}{self.x:+d}{self.y:+d}"


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("size", CGSize)]


def list_displays() -> list[Display]:
    try:
        app_services = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
        )
    except OSError:
        return []

    max_displays = 16
    active_displays = (ctypes.c_uint32 * max_displays)()
    display_count = ctypes.c_uint32(0)

    get_active = app_services.CGGetActiveDisplayList
    get_active.argtypes = [
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    get_active.restype = ctypes.c_int32

    if get_active(max_displays, active_displays, ctypes.byref(display_count)) != 0:
        return []

    get_bounds = app_services.CGDisplayBounds
    get_bounds.argtypes = [ctypes.c_uint32]
    get_bounds.restype = CGRect

    main_display_id = int(app_services.CGMainDisplayID())
    displays: list[Display] = []
    for index in range(display_count.value):
        display_id = int(active_displays[index])
        bounds = get_bounds(display_id)
        displays.append(
            Display(
                display_id=display_id,
                x=round(bounds.origin.x),
                y=round(bounds.origin.y),
                width=round(bounds.size.width),
                height=round(bounds.size.height),
                is_main=display_id == main_display_id,
            )
        )

    return displays


def choose_external_display(displays: list[Display]) -> Display | None:
    if not displays:
        return None

    for display in displays:
        if not display.is_main:
            return display

    return displays[0]
