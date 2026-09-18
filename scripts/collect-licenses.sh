#!/bin/sh
# Copy license/notice files while preserving dependency-relative paths.
set -eu
root=$1
out=$2
mkdir -p "$out"
find "$root" -type d -name cache -prune -o -type f \( \
    -iname 'license' -o -iname 'license.*' -o -iname 'licence*' \
    -o -iname 'copying*' -o -iname 'notice' -o -iname 'notice.*' \
    -o -iname 'third_party_notices*' \) -print |
while IFS= read -r file; do
    relative=${file#"$root"/}
    mkdir -p "$out/$(dirname "$relative")"
    cp "$file" "$out/$relative"
done
