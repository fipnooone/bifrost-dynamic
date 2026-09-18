#!/usr/bin/env python3
"""Small, offline checks for the build contract and release naming."""
from __future__ import annotations

from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
KEYS = {
    "BIFROST_VERSION", "BIFROST_COMMIT", "GO_VERSION", "CORE_VERSION", "GOOS",
    "GOARCH", "GOAMD64", "CGO_ENABLED", "BUILD_TAGS", "GO_IMAGE", "NODE_IMAGE",
    "RUNTIME_IMAGE", "HOST_MOD_BLOB", "HOST_SUM_BLOB",
}


def read_manifest(path: Path = ROOT / "upstream.env") -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid manifest line: {line!r}")
        key, value = line.split("=", 1)
        if key not in KEYS or key in result:
            raise ValueError(f"Unknown or repeated manifest key: {key}")
        # Also safe for shell sourcing and GitHub build-args output. No expansion.
        if not re.fullmatch(r"[A-Za-z0-9_./:@,+-]+", value):
            raise ValueError(f"Invalid value for {key}")
        result[key] = value
    if result.keys() != KEYS:
        raise ValueError(f"Missing manifest keys: {KEYS - result.keys()}")
    for key in ("BIFROST_COMMIT", "HOST_MOD_BLOB", "HOST_SUM_BLOB"):
        if not re.fullmatch(r"[0-9a-f]{40}", result[key]):
            raise ValueError(f"{key} must be a full Git SHA")
    for key in ("BIFROST_VERSION", "CORE_VERSION"):
        if not re.fullmatch(r"v\d+\.\d+\.\d+", result[key]):
            raise ValueError(f"Invalid {key}")
    if not re.fullmatch(r"\d+\.\d+\.\d+", result["GO_VERSION"]):
        raise ValueError("Pin an exact Go patch version")
    for key in ("GO_IMAGE", "NODE_IMAGE", "RUNTIME_IMAGE"):
        if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", result[key]):
            raise ValueError(f"{key} must be digest-pinned")
    if not result["GO_IMAGE"].startswith(f"golang:{result['GO_VERSION']}-alpine"):
        raise ValueError("GO_IMAGE and GO_VERSION disagree")
    go_alpine = re.search(r"-alpine(3\.\d+)", result["GO_IMAGE"])
    runtime_alpine = re.match(r"alpine:(3\.\d+)(?:[.@])", result["RUNTIME_IMAGE"])
    if not go_alpine or not runtime_alpine or go_alpine[1] != runtime_alpine[1]:
        raise ValueError("Compiler and runtime must use the same Alpine release series")
    expected = {"GOOS": "linux", "GOARCH": "amd64", "GOAMD64": "v1", "CGO_ENABLED": "1"}
    if any(result[k] != v for k, v in expected.items()):
        raise ValueError("This project currently targets native linux/amd64, GOAMD64=v1, CGO=1")
    return result


def validate_release_tag(tag: str, manifest: dict[str, str]) -> None:
    if not re.fullmatch(re.escape(manifest["BIFROST_VERSION"]) + r"-r[1-9][0-9]*", tag):
        raise ValueError(f"Release tag must be {manifest['BIFROST_VERSION']}-rN (N >= 1)")


def main() -> None:
    manifest = read_manifest()
    if len(sys.argv) == 3 and sys.argv[1] == "--release-tag":
        validate_release_tag(sys.argv[2], manifest)
    elif len(sys.argv) != 1:
        raise ValueError("Usage: check.py [--release-tag vX.Y.Z-rN]")
    print(f"OK: pinned {manifest['BIFROST_VERSION']} / Go {manifest['GO_VERSION']} / linux/amd64")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as error:
        sys.exit(f"ERROR: {error}")
