#!/usr/bin/env bash
# Publish only the image that passed the real-runtime smoke test.
set -euo pipefail
cd "$(dirname "$0")/.."
: "${GITHUB_REPOSITORY:?}" "${GITHUB_REF_NAME:?}" "${GITHUB_SHA:?}" "${GITHUB_ACTOR:?}" "${GH_TOKEN:?}"
[[ ${GITHUB_EVENT_NAME:-} == push && ${GITHUB_REF:-} == refs/tags/* ]] || {
  echo 'ERROR: publishing is allowed only from a pushed release tag' >&2; exit 1;
}
python3 scripts/check.py --release-tag "$GITHUB_REF_NAME"
# shellcheck source=../upstream.env
source upstream.env
cd dist/release
sha256sum -c HANDOFF-SHA256SUMS
python3 - "$BIFROST_COMMIT" <<'PY'
import json, sys
from pathlib import Path
r = json.loads(Path('smoke.json').read_text())
if r.get('status') != 'passed' or r.get('upstream_commit') != sys.argv[1]:
    raise SystemExit('ERROR: missing or mismatched successful smoke test')
if r.get('image_id') != Path('image-id.txt').read_text().strip():
    raise SystemExit('ERROR: smoke test did not test this image')
PY
docker load --input image.tar.gz
image_id=$(cat image-id.txt)
[[ $(docker image inspect --format '{{.Id}}' "$image_id") == "$image_id" ]]
[[ $(docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$image_id") == "$GITHUB_SHA" ]]

repo=${GITHUB_REPOSITORY,,}
owner=${repo%%/*}
package=${repo#*/}
registry="ghcr.io/$repo"
tag=$GITHUB_REF_NAME

# A 404 is absence; permission/network/server errors must not be treated as absence.
get_or_empty() {
  local endpoint=$1 output=$2
  if gh api --paginate --slurp "$endpoint" > "$output" 2> "$output.err"; then
    rm -f "$output.err"
  elif grep -q 'HTTP 404' "$output.err"; then
    printf '[]\n' > "$output"
    rm -f "$output.err"
  else
    cat "$output.err" >&2
    exit 1
  fi
}
get_or_empty "repos/$GITHUB_REPOSITORY/releases/tags/$tag" existing-release.json
python3 - <<'PY'
import json
if json.load(open('existing-release.json')):
    raise SystemExit('ERROR: release already exists; publish a new -rN revision, do not overwrite it')
PY
owner_type=$(gh api "repos/$GITHUB_REPOSITORY" --jq '.owner.type')
case "$owner_type" in
  Organization) endpoint="orgs/$owner" ;;
  User) endpoint="users/$owner" ;;
  *) echo 'ERROR: unsupported repository owner type' >&2; exit 1 ;;
esac
get_or_empty "$endpoint/packages/container/$package/versions?per_page=100" existing-images.json
python3 - "$tag" <<'PY'
import json, sys
reserved = {sys.argv[1], sys.argv[1] + '-amd64'}
for page in json.load(open('existing-images.json')):
    for version in page:
        if reserved.intersection(version.get('metadata', {}).get('container', {}).get('tags', [])):
            raise SystemExit('ERROR: image tag already exists; publish a new -rN revision')
PY

# Reserve a draft before uploading. Failed publication leaves a draft, not a
# successful release. Never use --clobber or silently republish an old tag.
gh release create "$tag" --repo "$GITHUB_REPOSITORY" --verify-tag --draft \
  --title "Bifrost dynamic $tag" --notes 'Image passed CI; registry publication in progress.'
export DOCKER_CONFIG
DOCKER_CONFIG=$(mktemp -d)
trap 'rm -rf "$DOCKER_CONFIG"' EXIT
printf '%s' "$GH_TOKEN" | docker login ghcr.io --username "$GITHUB_ACTOR" --password-stdin
for suffix in '' '-amd64'; do
  docker tag "$image_id" "$registry:$tag$suffix"
  docker push "$registry:$tag$suffix"
done
digest=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$registry:$tag" | grep -F "$registry@sha256:" | head -n 1)
[[ $digest == "$registry@sha256:"* ]] || { echo 'ERROR: no published image digest' >&2; exit 1; }
docker pull "$digest"
[[ $(docker image inspect --format '{{.Id}}' "$digest") == "$image_id" ]] || {
  echo 'ERROR: published image differs from the tested image' >&2; exit 1;
}
printf '%s\n' "$digest" > image-digest.txt
sha256sum build-contract.tar.gz licenses.tar.gz smoke.json image-digest.txt > SHA256SUMS
cat > release-notes.md <<NOTES
Unofficial dynamic build of Bifrost **$BIFROST_VERSION**, image revision **$tag**.

- Platform: linux/amd64, GOAMD64=v1, Alpine/musl
- Go: $GO_VERSION; core: $CORE_VERSION; build tags: $BUILD_TAGS
- Upstream commit: $BIFROST_COMMIT
- Image source commit: $GITHUB_SHA
- Validation: real Bifrost startup, dashboard, baseline model catalog, native Go PostLLMHook and HTTP hook.

Image: \`$registry:$tag\` (also \`$registry:$tag-amd64\`).

Pinned image: \`$digest\`

Download the build contract for exact Go module versions and compiler settings.
This is not an official Maxim build and does not bundle any production plugins.

After the first publication, set the GHCR package's visibility to **Public** for
anonymous pulls. A public GitHub repository does not automatically make its package public.
NOTES
gh release upload "$tag" --repo "$GITHUB_REPOSITORY" \
  build-contract.tar.gz licenses.tar.gz smoke.json image-digest.txt SHA256SUMS
gh release edit "$tag" --repo "$GITHUB_REPOSITORY" --draft=false --notes-file release-notes.md
if [[ -n ${GITHUB_STEP_SUMMARY:-} ]]; then
  cat release-notes.md >> "$GITHUB_STEP_SUMMARY"
fi
printf '\nPublished %s\n' "$digest"
