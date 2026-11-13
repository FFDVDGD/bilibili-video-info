import json
import tomllib
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def test_manifest_and_project_release_versions_match() -> None:
    manifest = json.loads((PROJECT_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    assert manifest["version"] == project["project"]["version"]


@pytest.mark.parametrize(
    ("host_version", "sdk_version", "supported"),
    [("1.2.3", "2.8.0", False), ("1.4.3", "2.22.1", True)],
)
def test_manifest_runtime_compatibility(host_version: str, sdk_version: str, supported: bool) -> None:
    manifest = json.loads((PROJECT_ROOT / "_manifest.json").read_text(encoding="utf-8"))

    host = manifest["host_application"]
    sdk = manifest["sdk"]
    assert (_version(host["min_version"]) <= _version(host_version) <= _version(host["max_version"])) is supported
    assert (_version(sdk["min_version"]) <= _version(sdk_version) <= _version(sdk["max_version"])) is supported
