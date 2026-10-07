#!/usr/bin/env python3
"""Create the release tag for a validated manifest version increase."""
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.error
import urllib.request

from check import read_manifest, validate_release_tag

ZERO_SHA = "0" * 40


def changed_version(before, head):
    if not re.fullmatch(r"[0-9a-f]{40}", before):
        raise ValueError("Invalid before commit SHA")
    if before == ZERO_SHA:
        return None
    if not re.fullmatch(r"[0-9a-f]{40}", head) or subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() != head:
        raise ValueError("Checked out HEAD does not match event commit SHA")
    old_text = subprocess.check_output(["git", "show", f"{before}:upstream.env"], text=True)
    old = read_manifest_text(old_text)
    current = read_manifest_text(subprocess.check_output(["git", "show", f"{head}:upstream.env"], text=True))
    tag = current["BIFROST_VERSION"] + "-r1"
    validate_release_tag(tag, current)
    if tuple(map(int, current["BIFROST_VERSION"][1:].split("."))) <= tuple(map(int, old["BIFROST_VERSION"][1:].split("."))):
        return None
    return tag


def read_manifest_text(content):
    import tempfile
    with tempfile.NamedTemporaryFile("w", delete=False) as stream:
        stream.write(content)
        path = Path(stream.name)
    try:
        return read_manifest(path)
    finally:
        path.unlink()


def publish(tag, target, api_url, repository, token):
    if not re.fullmatch(r"[0-9a-f]{40}", target) or target == ZERO_SHA:
        raise ValueError("Invalid target commit SHA")
    base = f"{api_url}/repos/{repository}/git"
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    url = base + "/ref/tags/" + tag
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            ref = json.load(response)
    except urllib.error.HTTPError as error:
        if error.code != 404:
            raise
        body = json.dumps({"ref": "refs/tags/" + tag, "sha": target}).encode()
        with urllib.request.urlopen(urllib.request.Request(base + "/refs", data=body, headers=headers, method="POST"), timeout=30) as response:
            ref = json.load(response)
    if ref["object"]["type"] != "commit":
        raise ValueError("Release tag must point directly to a commit; manual review required")
    if ref["object"]["sha"] != target:
        raise ValueError("Conflicting release tag target; manual review required")


def main():
    tag = changed_version(os.environ["BEFORE_SHA"], os.environ.get("GITHUB_SHA", ""))
    if tag:
        publish(tag, os.environ["GITHUB_SHA"], os.environ["GITHUB_API_URL"], os.environ["GITHUB_REPOSITORY"], os.environ["GH_TOKEN"])


if __name__ == "__main__":
    main()
