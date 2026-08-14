# dmgbuild settings for SmallHD Calibration.

import os

application = "SmallHD Calibration.app"
app_path = os.path.abspath(os.path.join("dist", application))

files = [app_path]
symlinks = {"Applications": "/Applications"}

volume_name = "SmallHD Calibration"
format = "UDZO"
size = "300M"
icon_size = 96

window_rect = ((100, 100), (560, 360))
icon_locations = {
    application: (150, 160),
    "Applications": (410, 160),
}
