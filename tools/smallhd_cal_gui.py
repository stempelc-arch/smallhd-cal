from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smallhd_cal.gui import run_app
from smallhd_cal.paths import default_app_paths

if __name__ == "__main__":
    run_app(default_app_paths(ROOT))
