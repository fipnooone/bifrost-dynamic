# bifrost-dynamic

Unofficial dynamically linked build of [Maxim Bifrost](https://github.com/maximhq/bifrost) for native Go plugin support.

The official Bifrost Docker image is statically linked, so custom `.so` plugins cannot be loaded with `plugin.Open()`. This project rebuilds Bifrost dynamically while keeping the upstream source otherwise unchanged.

## Image

[upstream.env](upstream.env) defines the current build target, including the Bifrost and Go versions. Builds target `linux/amd64` on Alpine / musl.

See [Releases](https://github.com/fipnooone/bifrost-dynamic/releases) for published image versions and their compatibility contracts. A build target or merged update does not mean its image has been published.

Image tags follow `v<BIFROST_VERSION>-r<BUILD_REVISION>` (also available with an `-amd64` suffix). `r1` is the build revision for this project, not a Bifrost version.

## Usage

Use the image exactly like the official Bifrost image. This Compose example is pinned to a historical version, not the latest release; select a published version from Releases:

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

1. [Discover upstream Bifrost release](.github/workflows/discover-upstream.yml) runs daily at 08:17 UTC or manually via `workflow_dispatch` on `main`. It proposes an `upstream.env`-only PR for a newer stable transports release.
2. Review the manifest PR and wait for CI, then merge it manually. Upstream Go version or transports Dockerfile changes fail closed and require manual review; discovery does not update toolchain or image pins automatically.
3. After a Bifrost version increase reaches `main`, [Tag reviewed Bifrost release](.github/workflows/tag-bifrost-release.yml) uses the GitHub App to create the version's `r1` tag at the merged commit.
4. That tag triggers the existing [Build workflow](.github/workflows/build.yml), which verifies Bifrost startup and native test plugin execution before publishing the tested image to GHCR and creating a release. Publication is complete only after that workflow succeeds, not when the PR merges.

Rebuilds of the same Bifrost version remain manual: create and push an `r2`, `r3`, or later revision tag at the reviewed commit whose manifest matches the version. For example, a second build of the historical version above would use `v2.2.0-r2`. Same-version manifest changes do not automatically create another tag.

### Automation prerequisites

- Create a GitHub App with repository permissions **Contents: Read and write** and **Pull requests: Read and write**. Install it for **Only select repositories**, selecting this repository.
- Set the repository Actions variable `APP_ID` to the App ID and the repository Actions secret `APP_PRIVATE_KEY` to its generated PEM private key. App-authenticated PRs and tags allow downstream CI to run.
- Protect `main`: require a pull request and the Build workflow's `build` status check before merging, and prevent bypasses (including by the App). The tagging workflow relies on this protection; it does not wait for post-merge CI before creating a tag.

## Disclaimer

This is an unofficial community build and is not affiliated with or endorsed by Maxim.

Bifrost is licensed under the Apache License 2.0. See `LICENSE` and `THIRD_PARTY_NOTICES.md`.
