#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null || { echo "ERROR: Docker with Buildx is required" >&2; exit 1; }
./scripts/prepare.sh
args=()
while IFS= read -r line; do
  [[ -z $line || $line == \#* ]] || args+=(--build-arg "$line")
done < upstream.env
image=${IMAGE:-bifrost-dynamic:local}
revision=$(git rev-parse HEAD 2>/dev/null || printf local)
args+=(--build-arg "BUILD_REVISION=$revision" --build-arg "IMAGE_VERSION=local")
docker buildx build --platform linux/amd64 --target runtime --load -t "$image" "${args[@]}" .
docker buildx build --platform linux/amd64 --target testkit \
  --output type=local,dest=dist/testkit "${args[@]}" .
printf '\nBuilt %s. Run: make smoke IMAGE=%s\n' "$image" "$image"
