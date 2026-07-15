"""Fail a release build when its tag and embedded versions disagree."""

import argparse
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def _match(path: Path, pattern: str) -> str:
    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    if match is None:
        raise ValueError(f"could not read version from {path.name}")
    return match.group(1)


def embedded_versions() -> set[str]:
    app_version = _match(ROOT / "usagemaxxer.py", r'^VERSION = "([^"]+)"')
    resource = ROOT / "version_info.txt"
    resource_versions = {
        _match(resource, r'filevers=\((\d+, \d+, \d+, \d+)\)'),
        _match(resource, r'prodvers=\((\d+, \d+, \d+, \d+)\)'),
        _match(resource, r'StringStruct\("FileVersion", "([^"]+)"\)'),
        _match(resource, r'StringStruct\("ProductVersion", "([^"]+)"\)'),
    }
    expected_resource = ".".join(app_version.split(".")) + ".0"
    if resource_versions != {expected_resource, expected_resource.replace(".", ", ")}:
        raise ValueError("version_info.txt does not match VERSION")
    return {app_version}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    if not args.tag.startswith("v"):
        raise SystemExit("release tag must start with v")
    version = args.tag[1:]
    if embedded_versions() != {version}:
        raise SystemExit(f"release tag {args.tag} does not match embedded version")


if __name__ == "__main__":
    main()
