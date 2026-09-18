# bifrost-dynamic

Unofficial dynamically linked build of [Maxim Bifrost](https://github.com/maximhq/bifrost) for native Go plugin support.

The official Bifrost Docker image is statically linked, so custom `.so` plugins cannot be loaded with `plugin.Open()`. This project rebuilds Bifrost dynamically while keeping the upstream source otherwise unchanged.

## Image

Current target:

- Bifrost: `v2.2.0`
- Go: `1.27.0`
- Platform: `linux/amd64`
- Runtime: Alpine / musl

Published images:

```text
ghcr.io/fipnooone/bifrost-dynamic:v2.2.0-r1
ghcr.io/fipnooone/bifrost-dynamic:v2.2.0-r1-amd64
```

`r1` is the build revision for this project, not a Bifrost version.

## Usage

Use the image exactly like the official Bifrost image:

```yaml
services:
  bifrost:
    image: ghcr.io/fipnooone/bifrost-dynamic:v2.2.0-r1
    ports:
      - "8080:8080"
    volumes:
      - ./data:/app/data
```

Bifrost config, environment variables, ports and data paths remain the same as upstream.

## Native plugins

This image is intended for Bifrost native Go plugins built with a compatible:

- Go version
- architecture
- libc
- Bifrost dependency graph

A plugin built for one Bifrost release should not be assumed compatible with another.

## Build and release

GitHub Actions builds and validates the image on pushes and pull requests.

Release tags follow:

```text
v<BIFROST_VERSION>-r<BUILD_REVISION>
```

Example:

```bash
git tag -a v2.2.0-r1 -m "Bifrost 2.2.0 dynamic build 1"
git push origin v2.2.0-r1
```

The release workflow verifies that Bifrost starts and can actually load and execute a native test plugin before publishing the image to GHCR.

## Disclaimer

This is an unofficial community build and is not affiliated with or endorsed by Maxim.

Bifrost is licensed under the Apache License 2.0. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
