#!/usr/bin/env python3
"""Discover and stage a reviewed Bifrost transports release pin update."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
from check import read_manifest

API = "https://api.github.com/repos/maximhq/bifrost/releases?per_page=100&page={}"
MAX_RELEASE_PAGES = 10
UPDATE_KEYS = {"BIFROST_VERSION", "BIFROST_COMMIT", "CORE_VERSION", "HOST_MOD_BLOB", "HOST_SUM_BLOB"}
TRANSITION_KEYS = UPDATE_KEYS | {"GO_VERSION", "GO_IMAGE"}
REVIEWED_OLD_IMAGE = "golang:1.27.0-alpine3.24@sha256:4c9fe60190a2a3350ddc51de80d0224b8a6698d12bdfc999fee45ea9d6c46dbc"
REVIEWED_NEW_IMAGE = "golang:1.27.3-alpine3.24@sha256:f2c872bcee949b7be9c490efd02b48a8a2fbf947cdf533f1c58525ec9d8170fd"
REVIEWED_TRANSITION_VERSION = "v2.3.2"
REVIEWED_TRANSITION_OLD_COMMIT = "9237ec06672406e437b3cb33fc793ffd0403b3ae"
REVIEWED_TRANSITION_COMMIT = "a05ea20e68bf116a35e9f05b1a3d749624b3935e"


def version_key(version: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        raise ValueError("Invalid stable transports release tag")
    return tuple(map(int, match.groups()))


def latest_release(releases) -> str | None:
    found = []
    for release in releases:
        tag = release.get("tag_name", "")
        if release.get("draft") or release.get("prerelease") or not tag.startswith("transports/"):
            continue
        version = tag.removeprefix("transports/")
        try:
            key = version_key(version)
        except ValueError:
            continue
        found.append((key, version))
    return max(found)[1] if found else None


def fetch_releases():
    releases = []
    url = API.format(1)
    # GitHub advertises page 11 but rejects results beyond the first 1,000.
    # Select the semantic maximum stable transports version in this recent window.
    for _ in range(MAX_RELEASE_PAGES):
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            batch = json.load(response)
            link = response.headers.get("Link", "")
        if not isinstance(batch, list):
            raise ValueError("Unexpected GitHub releases response")
        releases.extend(batch)
        next_page = re.search(r'<([^>]+)>;\s*rel="next"', link)
        if not next_page:
            break
        url = next_page.group(1)
    if latest_release(releases) is None:
        raise ValueError("No stable transports release found in the first 1,000 recent releases")
    return releases


def candidate_pins(version: str, previous: dict[str, str], repo_url: str = "https://github.com/maximhq/bifrost.git") -> dict[str, str]:
    with tempfile.TemporaryDirectory() as directory:
        subprocess.run(["git", "init", "-q", directory], check=True)
        subprocess.run(["git", "-C", directory, "fetch", "--depth=1", repo_url, f"refs/tags/transports/{version}"], check=True)
        commit = subprocess.check_output(["git", "-C", directory, "rev-parse", "FETCH_HEAD^{commit}"], text=True).strip()
        subprocess.run(["git", "-C", directory, "checkout", "-q", "--detach", commit], check=True)
        subprocess.run(["git", "-C", directory, "fetch", "--depth=1", repo_url, previous["BIFROST_COMMIT"]], check=True)
        def show(revision, path):
            return subprocess.check_output(["git", "-C", directory, "show", f"{revision}:{path}"])
        old_docker = show(previous["BIFROST_COMMIT"], "transports/Dockerfile")
        docker = show(commit, "transports/Dockerfile")
        old_mod = show(previous["BIFROST_COMMIT"], "transports/go.mod").decode()
        mod_bytes = show(commit, "transports/go.mod")
        old_go = previous["GO_VERSION"]
        go_directive = re.search(r"^go ([0-9]+\.[0-9]+(?:\.[0-9]+)?)$", old_mod, re.M)
        candidate_go = re.search(r"^go ([0-9]+\.[0-9]+(?:\.[0-9]+)?)$", mod_bytes.decode(), re.M)
        image_pattern = rb"(?m)^([ \t]*)FROM golang:([0-9]+\.[0-9]+\.[0-9]+)-alpine3\.24@sha256:[^\s]+(?: AS builder)?$"
        old_image = re.search(image_pattern, old_docker)
        candidate_image = re.search(image_pattern, docker)
        reviewed_transition = (
            version == REVIEWED_TRANSITION_VERSION
            and previous["BIFROST_COMMIT"] == REVIEWED_TRANSITION_OLD_COMMIT
            and commit == REVIEWED_TRANSITION_COMMIT
            and old_go == "1.27.0"
            and candidate_go
            and candidate_go.group(1) == "1.27.3"
        )
        if not go_directive or not candidate_go or candidate_go.group(1) != ("1.27.3" if reviewed_transition else old_go):
            raise ValueError("Upstream Go version changed; manual review required")
        if reviewed_transition:
            if (not old_image or old_image.group(2).decode() != old_go
                    or not candidate_image or candidate_image.group(2).decode() != "1.27.3"
                    or docker != old_docker[:old_image.start()] + candidate_image.group(0) + old_docker[old_image.end():]
                    or previous["GO_IMAGE"] != REVIEWED_OLD_IMAGE
                    or old_image.group(0).decode().strip() != f"FROM {REVIEWED_OLD_IMAGE} AS builder"):
                raise ValueError("Upstream transports Dockerfile changed; manual review required")
            image_pin = re.search(r"golang:\S+", candidate_image.group(0).decode()).group(0)
            if image_pin != REVIEWED_NEW_IMAGE:
                raise ValueError("Upstream Go image changed; manual review required")
        elif docker != old_docker:
            raise ValueError("Upstream transports Dockerfile changed; manual review required")
        core = re.search(r"^\s*require\s+github\.com/maximhq/bifrost/core\s+(v\d+\.\d+\.\d+)\s*$|^\s*github\.com/maximhq/bifrost/core\s+(v\d+\.\d+\.\d+)\s*$", mod_bytes.decode(), re.M)
        if not core:
            raise ValueError("Could not resolve core module version")
        core_version = core.group(1) or core.group(2)
        pins = {"BIFROST_VERSION": version, "BIFROST_COMMIT": commit, "CORE_VERSION": core_version,
                "HOST_MOD_BLOB": subprocess.check_output(["git", "-C", directory, "hash-object", "transports/go.mod"], text=True).strip(),
                "HOST_SUM_BLOB": subprocess.check_output(["git", "-C", directory, "hash-object", "transports/go.sum"], text=True).strip()}
        if reviewed_transition:
            pins["GO_VERSION"] = "1.27.3"
            pins["GO_IMAGE"] = image_pin
        return pins


def render_manifest(original: str, values: dict[str, str]) -> str:
    lines = original.splitlines(keepends=True)
    seen = set()
    result = []
    for line in lines:
        match = re.match(r"([^#=\n]+)=", line)
        if match and match.group(1) in TRANSITION_KEYS and match.group(1) in values:
            key = match.group(1)
            if key in seen:
                raise ValueError("Invalid update values")
            result.append(f"{key}={values[key]}\n")
            seen.add(key)
        else:
            result.append(line)
    if seen != UPDATE_KEYS and seen != TRANSITION_KEYS:
        raise ValueError("Manifest is missing update keys")
    return "".join(result)


def atomic_update(path: Path, candidate: str) -> None:
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
    try:
        temporary.write_text(candidate)
        read_manifest(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    path = ROOT / "upstream.env"
    original = path.read_text()
    current = read_manifest(path)
    tag = latest_release(fetch_releases())
    if not tag or version_key(tag) <= version_key(current["BIFROST_VERSION"]):
        return
    pins = candidate_pins(tag, current)
    atomic_update(path, render_manifest(original, pins))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError, urllib.error.URLError) as error:
        sys.exit(f"ERROR: {error}; manual review required")
