#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 scripts/check.py
# shellcheck source=../upstream.env
source upstream.env

if [[ ! -d .upstream ]]; then
  git init -q .upstream
  git -C .upstream remote add origin https://github.com/maximhq/bifrost.git
fi
[[ -d .upstream/.git ]] || { echo 'ERROR: .upstream is not a Git checkout' >&2; exit 1; }
[[ $(git -C .upstream remote get-url origin) == https://github.com/maximhq/bifrost.git ]] || exit 1
[[ -z $(git -C .upstream status --porcelain --untracked-files=all) ]] || {
  echo 'ERROR: .upstream has local changes; move them aside before building' >&2; exit 1;
}
git -C .upstream fetch --depth=1 origin "refs/tags/transports/${BIFROST_VERSION}"
resolved=$(git -C .upstream rev-parse 'FETCH_HEAD^{commit}')
[[ $resolved == "$BIFROST_COMMIT" ]] || {
  echo "ERROR: upstream tag resolves to $resolved, not the reviewed commit" >&2; exit 1;
}
git -C .upstream checkout -q --detach "$BIFROST_COMMIT"
[[ $(git -C .upstream hash-object transports/go.mod) == "$HOST_MOD_BLOB" ]] || exit 1
[[ $(git -C .upstream hash-object transports/go.sum) == "$HOST_SUM_BLOB" ]] || exit 1
grep -Fxq "go $GO_VERSION" .upstream/transports/go.mod
grep -Eq "github.com/maximhq/bifrost/core[[:space:]]+${CORE_VERSION//./\\.}([[:space:]]|$)" .upstream/transports/go.mod

# Preserve the pinned upstream notices rather than hand-maintaining copies.
mkdir -p .upstream-notices
find .upstream-notices -type f -delete
for name in LICENSE NOTICE NOTICE.txt THIRD_PARTY_NOTICES.md; do
  [[ ! -f .upstream/$name ]] || cp ".upstream/$name" .upstream-notices/
done
[[ -s .upstream-notices/LICENSE && -s .upstream-notices/THIRD_PARTY_NOTICES.md ]]
printf 'Prepared Bifrost %s at %s\n' "$BIFROST_VERSION" "$BIFROST_COMMIT"
