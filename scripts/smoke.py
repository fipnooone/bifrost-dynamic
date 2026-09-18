#!/usr/bin/env python3
"""Test the actual runtime image, first without and then with a native plugin.

The fixture upstream is local and uses no real credentials. Linux host networking
is used only in this test; production deployment does not need it.
"""
from __future__ import annotations

from email.message import Message
import hashlib
import http.server
import json
import platform
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from check import ROOT, read_manifest

MARKER = "native-plugin-executed"
MODEL_ID = "smoke/dynamic-probe"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def command(*args: str) -> str:
    result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {(result.stdout + result.stderr)[-2000:]}")
    return result.stdout.strip()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_: object) -> None:
        pass

    def do_GET(self) -> None:
        if urllib.parse.urlsplit(self.path).path != "/v1/models":
            self.send_error(404, "Fixture expects /v1/models; base_url must NOT end in /v1")
            return
        body = json.dumps({"object": "list", "data": [{
            "id": "dynamic-probe", "object": "model", "created": 1, "owned_by": "fixture",
        }]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def request(port: int, path: str) -> tuple[bytes, Message]:
    try:
        with OPENER.open(f"http://127.0.0.1:{port}{path}", timeout=15) as response:
            return response.read(4 * 1024 * 1024), response.headers
    except urllib.error.HTTPError as error:
        body = error.read(1024).decode("utf-8", "replace")
        raise RuntimeError(f"GET {path}: HTTP {error.code}: {body}") from error


def verify_models(body: bytes, headers: Message, plugin: bool) -> None:
    data = json.loads(body)
    models = data.get("data")
    if not isinstance(models, list) or len(models) != 1 or models[0].get("id") != MODEL_ID:
        raise RuntimeError(f"Unexpected model availability: {data!r}")
    header = headers.get("X-Bifrost-Dynamic-Smoke")
    if plugin:
        if header != MARKER:
            raise RuntimeError("Native HTTP hook did not execute; inspect the Bifrost loader logs")
        if models[0].get("name") != MARKER or models[0].get("context_length") != 123456:
            raise RuntimeError("Native PostLLMHook did not enrich /v1/models; health alone is not success")
    elif header is not None or models[0].get("name") == MARKER:
        raise RuntimeError("Baseline unexpectedly has the CI plugin enabled")


def check_linker(text: str) -> None:
    lower = text.lower()
    if "ld-musl" not in lower or any(s in lower for s in ("not found", "error relocating", "not a dynamic", "statically linked")):
        raise RuntimeError(f"Runtime dynamic-loader check failed:\n{text}")


def run_case(image_id: str, upstream_port: int, plugin: Path, enabled: bool, dist: Path) -> None:
    label = "with-plugin" if enabled else "baseline"
    with tempfile.TemporaryDirectory(prefix="bifrost-dynamic-smoke-") as directory:
        app = Path(directory)
        app.chmod(0o777)  # Disposable SQLite/config test directory, never production data.
        config = {"providers": {"smoke": {
            "custom_provider_config": {"base_provider_type": "openai", "is_key_less": True},
            "network_config": {"base_url": f"http://127.0.0.1:{upstream_port}", "max_retries": 0},
        }}}
        if enabled:
            config["plugins"] = [{"name": "dynamic-image-smoke", "enabled": True,
                                  "path": "/smoke/plugin.so", "config": {"marker": MARKER}}]
        (app / "config.json").write_text(json.dumps(config))
        port = free_port()
        args = ["docker", "run", "-d", "--platform", "linux/amd64", "--network", "host",
                "--mount", f"type=bind,src={app},dst=/app/data",
                "-e", f"APP_PORT={port}", "-e", "APP_HOST=127.0.0.1", "-e", "LOG_LEVEL=debug"]
        if enabled:
            args += ["--mount", f"type=bind,src={plugin},dst=/smoke/plugin.so,readonly"]
        cid = command(*args, image_id)
        try:
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if command("docker", "inspect", "--format", "{{.State.Running}}", cid) != "true":
                    raise RuntimeError(f"Bifrost exited during {label} startup")
                try:
                    request(port, "/health")
                    break
                except (OSError, RuntimeError):
                    time.sleep(1)
            else:
                raise RuntimeError(f"Bifrost did not become healthy ({label})")
            html, _ = request(port, "/")
            if b"<html" not in html.lower():
                raise RuntimeError("The embedded dashboard was not served")
            body, headers = request(port, "/v1/models")
            verify_models(body, headers, enabled)
            (dist / f"{label}-models.json").write_bytes(body)
            print(f"PASS: {label}: health, dashboard, model catalog" + (", native typed and HTTP hooks" if enabled else ""), flush=True)
        finally:
            logs = subprocess.run(["docker", "logs", cid], text=True, capture_output=True, timeout=30)
            text = logs.stdout + logs.stderr
            (dist / f"{label}.log").write_text(text)
            if sys.exc_info()[0] is not None:
                print(f"\n--- {label} Bifrost logs (last 60 lines) ---\n" + "\n".join(text.splitlines()[-60:]), file=sys.stderr)
            subprocess.run(["docker", "rm", "-f", "-v", cid], capture_output=True, timeout=30)


def main(image: str) -> None:
    if not shutil.which("docker"):
        raise RuntimeError("Docker is required; no image test has run")
    if platform.system() != "Linux" or platform.machine() not in ("x86_64", "amd64"):
        raise RuntimeError("Run the smoke test on native Linux/x86_64, with a local Docker daemon")
    manifest = read_manifest()
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    record = dist / "smoke.json"
    record.unlink(missing_ok=True)
    plugin = dist / "testkit/smoke.so"
    if not plugin.is_file():
        raise RuntimeError("Missing testkit/smoke.so; run make build first")
    info = json.loads(command("docker", "image", "inspect", image))[0]
    if (info["Os"], info["Architecture"]) != ("linux", "amd64"):
        raise RuntimeError("Wrong runtime image architecture")
    if info["Config"].get("User") != "1000:0":
        raise RuntimeError("Runtime must keep the expected non-root user, 1000:0")
    labels = info["Config"].get("Labels") or {}
    if labels.get("io.bifrost-dynamic.upstream-revision") != manifest["BIFROST_COMMIT"]:
        raise RuntimeError("The image does not match upstream.env")
    image_id = info["Id"]
    linker = command("docker", "run", "--rm", "--entrypoint", "/bin/sh", image_id, "-c", "ldd /app/main 2>&1")
    (dist / "runtime-ldd.txt").write_text(linker + "\n")
    check_linker(linker)
    # Check the actual runtime filesystem, not just Dockerfile intent.
    command("docker", "run", "--rm", "--entrypoint", "/bin/sh", image_id, "-ec",
            "test -s /usr/share/licenses/bifrost/LICENSE; "
            "test -s /usr/share/licenses/bifrost/THIRD_PARTY_NOTICES.md; "
            "test ! -e /out/smoke.so; test ! -e /smoke/plugin.so; "
            "! command -v go; ! command -v gcc")
    upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    worker = threading.Thread(target=upstream.serve_forever, daemon=True)
    worker.start()
    try:
        for enabled in (False, True):
            run_case(image_id, upstream.server_address[1], plugin, enabled, dist)
    finally:
        upstream.shutdown()
        upstream.server_close()
        worker.join()
    record.write_text(json.dumps({"status": "passed", "image_id": image_id,
        "upstream_commit": manifest["BIFROST_COMMIT"], "platform": "linux/amd64",
        "plugin_sha256": hashlib.sha256(plugin.read_bytes()).hexdigest(),
        "checks": ["dynamic-loader", "non-root", "no-compiler-in-runtime", "licenses",
                   "health", "dashboard", "baseline-catalog", "native-post-llm-hook", "native-http-hook"]}, indent=2) + "\n")
    print("PASS: the actual Bifrost image loaded and executed the native plugin")


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise RuntimeError("Usage: smoke.py IMAGE")
        main(sys.argv[1])
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        sys.exit(f"ERROR: {error}")
