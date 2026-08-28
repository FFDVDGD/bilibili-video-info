import json
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split("."))


def test_manifest_and_project_release_versions_match() -> None:
    manifest = json.loads((PROJECT_ROOT / "_manifest.json").read_text(encoding="utf-8"))
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as project_file:
        project = tomllib.load(project_file)

    assert manifest["version"] == project["project"]["version"]


def test_manifest_supports_current_maibot_and_sdk() -> None:
    manifest = json.loads((PROJECT_ROOT / "_manifest.json").read_text(encoding="utf-8"))

    host = manifest["host_application"]
    sdk = manifest["sdk"]
    assert _version(host["min_version"]) <= _version("1.2.3") <= _version(host["max_version"])
    assert _version(sdk["min_version"]) <= _version("2.8.0") <= _version(sdk["max_version"])
