# Development Notes

## Setup

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

For editable package installs and packaging tools:

```bash
.venv/bin/pip install -e ".[dev,packaging]"
```

## Checks

Run these before sharing changes:

```bash
.venv/bin/python -m pytest
.venv/bin/ruff check .
.venv/bin/python -m py_compile src/smallhd_cal/gui.py tools/smallhd_cal_gui.py
```

## Adding Features

- Put reusable logic in `src/smallhd_cal/`.
- Keep `tools/` scripts thin and operator-facing.
- Add tests for parser behavior, math behavior, and command construction.
- Avoid putting calibration math directly in the GUI.
- Keep generated files out of source control.

## GUI Guidance

The GUI should stay a control panel, not the source of truth. It should call the
same modules that the command-line tools and tests use.

Useful GUI seams to keep testable:

- Path normalization.
- Probe command selection.
- Measurement replacement by patch name.
- Analysis and LUT generation using shared modules.

## Packaging Direction

Packaging is staged deliberately:

1. Keep source workflow stable.
2. Build a PyInstaller `.app`.
3. Verify ArgyllCMS discovery inside the `.app`.
4. Wrap the `.app` in a `.dmg`.
5. Add signing/notarization when sharing beyond trusted users.

See `packaging/macos/README.md` for the current checklist.
