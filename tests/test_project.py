from __future__ import annotations

import copy
from email.message import Message
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from check import ROOT, read_manifest, validate_release_tag
from smoke import MARKER, MODEL_ID, OPENER, Upstream, check_linker, verify_models
import http.server


class ManifestTests(unittest.TestCase):
    def test_real_manifest(self):
        m = read_manifest()
        self.assertEqual(m['GOARCH'], 'amd64')
        self.assertEqual(m['GOAMD64'], 'v1')

    def test_tag(self):
        m = read_manifest()
        validate_release_tag(m['BIFROST_VERSION'] + '-r1', m)
        validate_release_tag(m['BIFROST_VERSION'] + '-r12', m)

    def test_invalid_tags(self):
        m = read_manifest()
        v = m['BIFROST_VERSION']
        for tag in [v, v + '-r0', v + '-r01', 'v999999.0.0-r1', 'latest', v + '-r1/foo']:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                validate_release_tag(tag, m)

    def test_manifest_rejects_mutable_or_unsafe_inputs(self):
        m = read_manifest()
        mutations = [
            {'GOAMD64': 'v3'}, {'BUILD_TAGS': '$(echo hello)'},
            {'GO_VERSION': '0.0.0'}, {'BIFROST_COMMIT': 'main'},
            {'CORE_VERSION': None}, {'RUNTIME_IMAGE': 'alpine:latest'},
            {'RUNTIME_IMAGE': m['RUNTIME_IMAGE'].replace('alpine:3.', 'alpine:9.')},
        ]
        for index, mutation in enumerate(mutations):
            values = m | mutation
            contents = ''.join(f'{k}={v}\n' for k, v in values.items() if v is not None)
            with self.subTest(case=index), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / 'upstream.env'
                path.write_text(contents)
                with self.assertRaises(ValueError):
                    read_manifest(path)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'upstream.env'
            path.write_text((ROOT / 'upstream.env').read_text() + '\nGOARCH=amd64\n')
            with self.assertRaises(ValueError):
                read_manifest(path)


class SmokeAssertionTests(unittest.TestCase):
    def setUp(self):
        self.models = {'data': [{'id': MODEL_ID, 'name': MARKER, 'context_length': 123456}]}
        self.headers = Message()
        self.headers['x-bifrost-dynamic-smoke'] = MARKER

    def test_valid_hooks(self):
        verify_models(json.dumps(self.models).encode(), self.headers, True)

    def test_missing_typed_hook_is_not_success(self):
        for field in ['name', 'context_length']:
            with self.subTest(field=field):
                models = copy.deepcopy(self.models)
                del models['data'][0][field]
                with self.assertRaisesRegex(RuntimeError, 'PostLLMHook'):
                    verify_models(json.dumps(models).encode(), self.headers, True)

    def test_missing_http_hook_is_not_success(self):
        with self.assertRaisesRegex(RuntimeError, 'HTTP hook'):
            verify_models(json.dumps(self.models).encode(), Message(), True)

    def test_extra_model_fails(self):
        self.models['data'].append({'id': 'invented'})
        with self.assertRaisesRegex(RuntimeError, 'availability'):
            verify_models(json.dumps(self.models).encode(), self.headers, True)

    def test_baseline(self):
        verify_models(json.dumps({'data': [{'id': MODEL_ID}]}).encode(), Message(), False)

    def test_polluted_baseline_fails(self):
        with self.assertRaisesRegex(RuntimeError, 'Baseline'):
            verify_models(json.dumps(self.models).encode(), self.headers, False)

    def test_dynamic_loader(self):
        check_linker('/lib/ld-musl-x86_64.so.1\nlibc.musl-x86_64.so.1 => /lib/ld-musl-x86_64.so.1')

    def test_static_or_missing_library_fails(self):
        for text in ['statically linked', 'not a dynamic executable',
                     '/lib/ld-musl-x86_64.so.1\nError relocating main',
                     '/lib/ld-musl-x86_64.so.1\nlibgcc.so not found']:
            with self.subTest(text=text), self.assertRaises(RuntimeError):
                check_linker(text)

    def test_upstream_path_and_sparse_metadata(self):
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            root = f'http://127.0.0.1:{server.server_address[1]}'
            with OPENER.open(root + '/v1/models', timeout=2) as response:
                model = json.load(response)['data'][0]
                self.assertNotIn('name', model)
                self.assertNotIn('context_length', model)
            with self.assertRaises(urllib.error.HTTPError):
                OPENER.open(root + '/v1/v1/models', timeout=2)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


class PackagingTests(unittest.TestCase):
    def test_license_collector(self):
        with tempfile.TemporaryDirectory() as directory:
            root, out = Path(directory) / 'modules', Path(directory) / 'notices'
            (root / 'a').mkdir(parents=True)
            (root / 'b').mkdir()
            (root / 'a/LICENSE').write_text('A license')
            (root / 'b/NOTICE.txt').write_text('B notice')
            (root / 'b/source.go').write_text('not a license')
            subprocess.run(['sh', str(ROOT / 'scripts/collect-licenses.sh'), str(root), str(out)], check=True)
            self.assertEqual((out / 'a/LICENSE').read_text(), 'A license')
            self.assertEqual((out / 'b/NOTICE.txt').read_text(), 'B notice')
            self.assertFalse((out / 'b/source.go').exists())

    def test_runtime_does_not_copy_probe(self):
        runtime = (ROOT / 'Dockerfile').read_text().split(' AS runtime\n', 1)[1]
        self.assertNotIn('COPY --from=probe', runtime)
        self.assertNotIn('smoke.so', runtime)
        self.assertIn('USER 1000:0', runtime)

    def test_checkout_is_not_fork_patched(self):
        prepare = (ROOT / 'scripts/prepare.sh').read_text()
        self.assertIn('refs/tags/transports/', prepare)
        self.assertIn('HOST_MOD_BLOB', prepare)
        self.assertNotIn('git apply', prepare)


class BuildCacheTests(unittest.TestCase):
    def setUp(self):
        self.dockerfile = (ROOT / 'Dockerfile').read_text()
        self.workflow = (ROOT / '.github/workflows/build.yml').read_text()
        # Join Dockerfile continuations to inspect complete RUN instructions.
        self.runs = [line for line in self.dockerfile.replace('\\\n', '').splitlines()
                     if line.startswith('RUN ')]

    def test_native_builds_share_toolchain_scoped_cache(self):
        builds = [line for line in self.runs if 'go build ' in line]
        self.assertEqual(len(builds), 2)
        mount = ('--mount=type=cache,id=bifrost-dynamic-go-${GO_VERSION}-amd64,'
                 'target=/root/.cache/go-build,sharing=locked')
        for build in builds:
            with self.subTest(build=build):
                self.assertIn(mount, build)
                self.assertIn('export GOCACHE=', build)
                self.assertIn('-mod=readonly -buildvcs=false -trimpath', build)
                self.assertIn('-tags="${BUILD_TAGS}"', build)
                # Outputs and the module graph must not live only in a cache mount.
                self.assertNotIn('target=/out', build)
                self.assertNotIn('target=/go/pkg/mod', build)
        self.assertIn('-linkmode=external', builds[0])
        self.assertIn('grep -q INTERP', builds[0])
        self.assertIn('-buildmode=plugin -o /out/smoke.so', builds[1])

    def test_native_cache_changes_with_c_library_versions(self):
        assignments = re.findall(r'export GOCACHE=("[^"\n]+")', self.dockerfile)
        self.assertEqual(len(assignments), 2)
        self.assertEqual(assignments[0], assignments[1])

        def cache_path(packages):
            # Exercise the actual Dockerfile shell expression with a fake apk.
            command = ('apk() { printf "%s\\n" "$PACKAGES"; }; '
                       f'export GOCACHE={assignments[0]}; printf "%s" "$GOCACHE"')
            return subprocess.run(['sh', '-eu', '-c', command], check=True,
                                  capture_output=True, text=True,
                                  env=dict(os.environ, PACKAGES=packages)).stdout

        first = cache_path('gcc-1\nmusl-dev-1')
        self.assertRegex(first, r'^/root/\.cache/go-build/[0-9a-f]{64}$')
        self.assertEqual(first, cache_path('musl-dev-1\ngcc-1'))
        self.assertNotEqual(first, cache_path('gcc-1\nmusl-dev-2'))
        self.assertNotEqual(first, cache_path('gcc-2\nmusl-dev-1'))

    def test_npm_download_cache_does_not_hide_installed_dependencies(self):
        install = next(line for line in self.runs if 'npm ci' in line)
        self.assertIn('target=/root/.npm', install)
        self.assertNotIn('target=/src/ui/node_modules', install)
        self.assertIn('RUN npm run build-enterprise', self.dockerfile)

    def test_release_metadata_does_not_invalidate_dependency_downloads(self):
        download = self.dockerfile.index('RUN go mod download && go mod verify')
        for arg in ['ARG BIFROST_VERSION', 'ARG BIFROST_COMMIT', 'ARG BUILD_TAGS']:
            with self.subTest(arg=arg):
                self.assertGreater(self.dockerfile.index(arg), download)
        self.assertGreater(self.dockerfile.index('ARG BIFROST_COMMIT'),
                           self.dockerfile.index('go build '))

    def test_separate_probe_cache_does_not_skip_runtime_verification(self):
        probe = self.workflow.split('- name: Build native test plugin', 1)[1]
        probe = probe.split('- name: Run Bifrost', 1)[0]
        self.assertIn('type=gha,scope=bifrost-dynamic-amd64', probe)
        self.assertIn('type=gha,scope=bifrost-dynamic-testkit-amd64', probe)
        self.assertIn('cache-to: type=gha,scope=bifrost-dynamic-testkit-amd64,mode=max', probe)
        self.assertEqual(self.workflow.count(
            'cache-to: type=gha,scope=bifrost-dynamic-amd64,'), 1)
        self.assertNotIn('continue-on-error', self.workflow)
        self.assertIn('run: python3 scripts/smoke.py "$TEST_IMAGE"', self.workflow)
        self.assertLess(self.workflow.index('- name: Run Bifrost'),
                        self.workflow.index('- name: Prepare tested-image handoff'))
        self.assertIn('needs: build', self.workflow.split('  publish:', 1)[1])


if __name__ == '__main__':
    unittest.main()
