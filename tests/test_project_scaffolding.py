import py_compile
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[1]


def test_development_docs_exist_and_cover_core_topics() -> None:
    docs = {
        "docs/ARCHITECTURE.md": ["measurement", "probe", "GUI", ".cube"],
        "docs/DEVELOPING.md": ["pytest", "ruff", "Packaging"],
        "docs/TESTING.md": ["test_probe.py", "Hardware tests"],
        "packaging/macos/README.md": ["PyInstaller", ".dmg", "ArgyllCMS"],
    }

    for relative_path, required_terms in docs.items():
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        for term in required_terms:
            assert term in text


def test_packaging_scaffold_files_exist() -> None:
    assert (ROOT / "packaging" / "macos" / "smallhd_cal_gui.spec").exists()
    assert (ROOT / "packaging" / "macos" / "dmgbuild_settings.py").exists()


def test_dmgbuild_settings_compile() -> None:
    py_compile.compile(str(ROOT / "packaging" / "macos" / "dmgbuild_settings.py"), doraise=True)


def test_pyproject_declares_dev_and_packaging_extras() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = pyproject["project"]["optional-dependencies"]

    assert "pytest>=8.0" in extras["dev"]
    assert "ruff>=0.5" in extras["dev"]
    assert "pyinstaller>=6.0" in extras["packaging"]
    assert "dmgbuild>=1.6" in extras["packaging"]
