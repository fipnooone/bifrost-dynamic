import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
import sys
sys.path.insert(0, str(SCRIPT_DIR))
import update_upstream as update
import tag_release


MANIFEST = """# Review this manifest as a unit
BIFROST_VERSION=v2.2.0
BIFROST_COMMIT=1111111111111111111111111111111111111111
GO_VERSION=1.27.0
CORE_VERSION=v1.9.0
GOOS=linux
GOARCH=amd64
GOAMD64=v1
CGO_ENABLED=1
BUILD_TAGS=netgo

GO_IMAGE=golang:1.27.0-alpine3.23@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
NODE_IMAGE=node:22-alpine3.23@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
RUNTIME_IMAGE=alpine:3.23@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
HOST_MOD_BLOB=2222222222222222222222222222222222222222
HOST_SUM_BLOB=3333333333333333333333333333333333333333
"""


class ManifestUpdateTests(unittest.TestCase):
    def test_updates_only_reviewed_pins_preserving_comments(self):
        original = MANIFEST
        values = {"BIFROST_VERSION": "v2.2.6", "BIFROST_COMMIT": "a" * 40, "CORE_VERSION": "v1.9.1", "HOST_MOD_BLOB": "b" * 40, "HOST_SUM_BLOB": "c" * 40}
        result = update.render_manifest(original, values)
        unchanged = lambda text: [line for line in text.splitlines(keepends=True) if line.split("=", 1)[0] not in values]
        self.assertEqual(unchanged(result), unchanged(original))
        with tempfile.NamedTemporaryFile("w", delete=False) as stream:
            stream.write(result)
            path = Path(stream.name)
        try:
            from check import read_manifest
            manifest = read_manifest(path)
            self.assertEqual({key: manifest[key] for key in values}, values)
        finally:
            path.unlink()

    def test_release_selection_skips_unstable_tags(self):
        releases = [{"tag_name": "transports/not-a-version"}, {"tag_name": "transports/v2.2.5", "draft": False, "prerelease": False}, {"tag_name": "transports/v8.0.0-rc.1", "draft": False, "prerelease": False}, {"tag_name": "transports/v2.2.6", "draft": False, "prerelease": False}]
        self.assertEqual(update.latest_release(releases), "v2.2.6")

    def test_candidate_allows_module_graph_change_but_rejects_docker_and_go(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            def git(*args): return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()
            git("init", "-q")
            git("config", "user.email", "test@example.com")
            git("config", "user.name", "Test")
            (repo / "transports").mkdir()
            (repo / "transports/go.mod").write_text("module transports\ngo 1.27.0\nrequire github.com/maximhq/bifrost/core v1.9.0\n")
            (repo / "transports/go.sum").write_text("old checksum\n")
            (repo / "transports/Dockerfile").write_text("same\n")
            git("add", "."); git("commit", "-qm", "base")
            old = git("rev-parse", "HEAD")
            (repo / "transports/go.mod").write_text("module transports\ngo 1.27.0\nrequire github.com/maximhq/bifrost/core v1.9.1\n")
            (repo / "transports/go.sum").write_text("new checksum\n")
            git("add", "."); git("commit", "-qm", "module update")
            git("tag", "transports/v2.2.6")
            pins = update.candidate_pins("v2.2.6", {"BIFROST_COMMIT": old, "GO_VERSION": "1.27.0"}, str(repo))
            self.assertEqual(pins["CORE_VERSION"], "v1.9.1")
            self.assertEqual(pins["BIFROST_COMMIT"], git("rev-parse", "HEAD"))
            for filename, key in (("go.mod", "HOST_MOD_BLOB"), ("go.sum", "HOST_SUM_BLOB")):
                content = (repo / "transports" / filename).read_bytes()
                expected = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
                self.assertEqual(pins[key], expected)
            for filename, content in (("Dockerfile", "changed\n"), ("go.mod", "module transports\ngo 1.28.0\nrequire github.com/maximhq/bifrost/core v1.9.1\n")):
                git("reset", "--hard", "transports/v2.2.6")
                (repo / "transports" / filename).write_text(content)
                git("add", "."); git("commit", "-qm", "breaking change")
                git("tag", "-f", "transports/v2.2.7")
                with self.assertRaisesRegex(ValueError, "Dockerfile" if filename == "Dockerfile" else "Go version"):
                    update.candidate_pins("v2.2.7", {"BIFROST_COMMIT": old, "GO_VERSION": "1.27.0"}, str(repo))

    def release_response(self, batch, next_url=None):
        response = io.BytesIO(json.dumps(batch).encode())
        response.headers = {"Link": f'<{next_url}>; rel="next"'} if next_url else {}
        return response

    def test_release_pagination_follows_next_even_on_short_page(self):
        first = [{"tag_name": "transports/v2.4.0"}]
        last = [{"tag_name": "transports/v2.3.0"}]
        next_url = "https://api.github.com/repositories/951115072/releases?per_page=100&page=2"
        responses = [self.release_response(first, next_url), self.release_response(last)]
        with patch.object(update.urllib.request, "urlopen", side_effect=responses) as request:
            releases = update.fetch_releases()
        self.assertEqual(len(releases), 2)
        self.assertEqual(update.latest_release(releases), "v2.4.0")
        self.assertEqual([call.args[0].full_url for call in request.call_args_list], [update.API.format(1), next_url])
        for call in request.call_args_list:
            self.assertIsNone(call.args[0].get_header("Authorization"))

    def test_discovery_ignores_environment_token_for_public_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "upstream.env"
            manifest.write_text(MANIFEST)
            responses = [self.release_response([], update.API.format(2)),
                         self.release_response([{"tag_name": "transports/v2.2.0"}])]
            with patch.object(update, "ROOT", root), patch.dict(os.environ, {"GITHUB_TOKEN": "must-not-be-sent"}):
                with patch.object(update.urllib.request, "urlopen", side_effect=responses) as request:
                    update.main()
            self.assertEqual(request.call_count, 2)
            for call in request.call_args_list:
                self.assertIsNone(call.args[0].get_header("Authorization"))
                self.assertEqual(call.args[0].get_header("Accept"), "application/vnd.github+json")
            self.assertEqual(manifest.read_text(), MANIFEST)

    def test_release_pagination_stops_without_next_on_full_page(self):
        response = self.release_response([{"tag_name": "transports/v2.3.0"}] * 100)
        response.headers = {"Link": f'<{update.API.format(1)}>; rel="prev"'}
        with patch.object(update.urllib.request, "urlopen", return_value=response) as request:
            self.assertEqual(len(update.fetch_releases()), 100)
        self.assertEqual(request.call_count, 1)

    def test_release_pagination_caps_advertised_next_at_1000_results(self):
        responses = [self.release_response([{"tag_name": "transports/v2.3.0"}] * 100, update.API.format(page + 1)) for page in range(1, 11)]
        with patch.object(update.urllib.request, "urlopen", side_effect=responses) as request:
            self.assertEqual(len(update.fetch_releases()), 1000)
        self.assertEqual(request.call_count, 10)
        self.assertEqual(request.call_args.args[0].full_url, update.API.format(10))

    def test_release_pagination_fails_without_stable_transports_in_window(self):
        responses = [self.release_response([{"tag_name": "core/v1.0.0"}] * 100, update.API.format(page + 1)) for page in range(1, 11)]
        with patch.object(update.urllib.request, "urlopen", side_effect=responses) as request:
            with self.assertRaisesRegex(ValueError, "No stable transports release"):
                update.fetch_releases()
        self.assertEqual(request.call_count, 10)

    def test_release_pagination_does_not_swallow_http_422(self):
        error = urllib.error.HTTPError(update.API.format(1), 422, "Unprocessable Entity", {}, None)
        with patch.object(update.urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                update.fetch_releases()
        self.assertIs(raised.exception, error)

    def test_failed_atomic_update_preserves_manifest(self):
        original = MANIFEST
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upstream.env"
            path.write_text(original)
            with self.assertRaises(ValueError):
                update.atomic_update(path, "invalid manifest")
            self.assertEqual(path.read_text(), original)
            with patch.object(update.os, "replace", side_effect=OSError("write failed")):
                with self.assertRaises(OSError):
                    update.atomic_update(path, original.replace("BIFROST_VERSION=v2.2.0", "BIFROST_VERSION=v2.2.6"))
            self.assertEqual(path.read_text(), original)
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_tag_cli_compares_event_commits_and_skips_unchanged_or_lower(self):
        original = MANIFEST
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            def git(*args):
                return subprocess.check_output(["git", "-C", tmp, *args], text=True).strip()
            git("init", "-q")
            git("config", "user.email", "test@example.com")
            git("config", "user.name", "Test")
            (repo / "upstream.env").write_text(original)
            git("add", ".")
            git("commit", "-qm", "before")
            before = git("rev-parse", "HEAD")
            for version, expected in (("v2.3.0", "v2.3.0-r1"), ("v2.2.0", None), ("v2.1.9", None)):
                with self.subTest(version=version):
                    (repo / "upstream.env").write_text(original.replace("BIFROST_VERSION=v2.2.0", "BIFROST_VERSION=" + version))
                    git("commit", "-qam", version)
                    head = git("rev-parse", "HEAD")
                    cwd = Path.cwd()
                    try:
                        os.chdir(repo)
                        self.assertEqual(tag_release.changed_version(before, head), expected)
                        with self.assertRaisesRegex(ValueError, "HEAD"):
                            tag_release.changed_version(before, "a" * 40)
                    finally:
                        os.chdir(cwd)
                    if expected is None:
                        env = {**os.environ, "BEFORE_SHA": before, "GITHUB_SHA": head}
                        env.pop("GH_TOKEN", None)
                        result = subprocess.run([sys.executable, str(SCRIPT_DIR / "tag_release.py")], cwd=repo, env=env, capture_output=True)
                        self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertIsNone(tag_release.changed_version("0" * 40, "a" * 40))

    def test_cli_import(self):
        result = subprocess.run([sys.executable, "-c", "import update_upstream; import tag_release"], cwd=SCRIPT_DIR, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode())


class ReleasePipelineTests(unittest.TestCase):
    def test_tag_waits_for_merge_build_and_targets_exact_commit(self):
        workflows = SCRIPT_DIR.parent / ".github/workflows"
        workflow = (workflows / "build.yml").read_text()
        build, rest = workflow.split("  build:\n", 1)[1].split("\n  tag:\n", 1)
        tag = rest.split("\n  publish:\n", 1)[0]
        self.assertIn("    needs: build\n", tag)
        self.assertIn("    if: github.event_name == 'push' && github.ref == 'refs/heads/main'\n", tag)
        self.assertNotIn("continue-on-error", build + tag)
        smoke = next(step for step in build.split("      - ") if "run: python3 scripts/smoke.py" in step)
        self.assertNotIn("if:", smoke)
        self.assertIn('run: python3 scripts/smoke.py "$TEST_IMAGE"\n', smoke)
        for setting in ("ref: ${{ github.sha }}", "fetch-depth: 0", "persist-credentials: false",
                        "permission-contents: write", "BEFORE_SHA: ${{ github.event.before }}",
                        "GITHUB_SHA: ${{ github.sha }}", "run: python3 scripts/tag_release.py"):
            self.assertIn(setting + "\n", tag)
        self.assertIn("      group: bifrost-release-tag\n      cancel-in-progress: false\n", tag)
        self.assertFalse((workflows / "tag-bifrost-release.yml").exists())

    def test_discovery_workflow_does_not_supply_repository_token(self):
        workflow = (SCRIPT_DIR.parent / ".github/workflows/discover-upstream.yml").read_text()
        self.assertIn("run: python3 scripts/update_upstream.py", workflow)
        self.assertNotIn("GITHUB_TOKEN", workflow)


class PublishTagTests(unittest.TestCase):
    target = "a" * 40
    tag = "v2.3.0-r1"
    base = "https://api.github.com/repos/owner/repo/git"

    def publish(self):
        tag_release.publish(self.tag, self.target, "https://api.github.com", "owner/repo", "test-token")

    def response(self, sha=None, object_type="commit"):
        return io.BytesIO(json.dumps({"object": {"type": object_type, "sha": sha or self.target}}).encode())

    def test_missing_tag_creates_exact_commit_reference(self):
        missing = urllib.error.HTTPError(self.base, 404, "Not Found", {}, None)
        with patch.object(tag_release.urllib.request, "urlopen", side_effect=[missing, self.response()]) as request:
            self.publish()
        get, create = [call.args[0] for call in request.call_args_list]
        self.assertEqual(get.full_url, self.base + "/ref/tags/" + self.tag)
        self.assertEqual(get.get_method(), "GET")
        self.assertEqual(create.full_url, self.base + "/refs")
        self.assertEqual(create.get_method(), "POST")
        self.assertEqual(json.loads(create.data), {"ref": "refs/tags/v2.3.0-r1", "sha": self.target})
        self.assertEqual(create.get_header("Authorization"), "Bearer test-token")

    def test_same_commit_is_noop(self):
        with patch.object(tag_release.urllib.request, "urlopen", return_value=self.response()) as request:
            self.publish()
        self.assertEqual(request.call_count, 1)

    def test_conflicting_or_annotated_tag_fails_without_writing(self):
        for sha, object_type in (("b" * 40, "commit"), (self.target, "tag")):
            with self.subTest(object_type=object_type):
                with patch.object(tag_release.urllib.request, "urlopen", return_value=self.response(sha, object_type)) as request:
                    with self.assertRaises(ValueError):
                        self.publish()
                self.assertEqual(request.call_count, 1)

    def test_forbidden_does_not_create_tag(self):
        forbidden = urllib.error.HTTPError(self.base, 403, "Forbidden", {}, None)
        with patch.object(tag_release.urllib.request, "urlopen", side_effect=forbidden) as request:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.publish()
        self.assertEqual(error.exception.code, 403)
        self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main()
