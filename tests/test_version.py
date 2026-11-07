"""The package version is the single release version source."""
from pathlib import Path
import tomllib

from comfybridge import __version__


def test_pyproject_version_matches_package():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["version"] == __version__
