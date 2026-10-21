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
        if docker != old_docker:
            raise ValueError("Upstream transports Dockerfile changed; manual review required")
        go_directive = re.search(r"^go ([0-9]+\.[0-9]+(?:\.[0-9]+)?)$", old_mod, re.M)
        candidate_go = re.search(r"^go ([0-9]+\.[0-9]+(?:\.[0-9]+)?)$", mod_bytes.decode(), re.M)
        if not go_directive or not candidate_go or candidate_go.group(1) != previous["GO_VERSION"]:
            raise ValueError("Upstream Go version changed; manual review required")
        core = re.search(r"^\s*require\s+github\.com/maximhq/bifrost/core\s+(v\d+\.\d+\.\d+)\s*$|^\s*github\.com/maximhq/bifrost/core\s+(v\d+\.\d+\.\d+)\s*$", mod_bytes.decode(), re.M)
        if not core:
            raise ValueError("Could not resolve core module version")
        core_version = core.group(1) or core.group(2)
        return {"BIFROST_VERSION": version, "BIFROST_COMMIT": commit, "CORE_VERSION": core_version,
                "HOST_MOD_BLOB": subprocess.check_output(["git", "-C", directory, "hash-object", "transports/go.mod"], text=True).strip(),
                "HOST_SUM_BLOB": subprocess.check_output(["git", "-C", directory, "hash-object", "transports/go.sum"], text=True).strip()}


def render_manifest(original: str, values: dict[str, str]) -> str:
    lines = original.splitlines(keepends=True)
    seen = set()
    result = []
    for line in lines:
        match = re.match(r"([^#=\n]+)=", line)
        if match and match.group(1) in UPDATE_KEYS:
            key = match.group(1)
            if key not in values or key in seen:
                raise ValueError("Invalid update values")
            result.append(f"{key}={values[key]}\n")
            seen.add(key)
        else:
            result.append(line)
    if seen != UPDATE_KEYS:
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
