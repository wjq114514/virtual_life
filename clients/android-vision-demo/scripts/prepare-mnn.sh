#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
version=3.6.1
sha=4b6065c4e2674318f5bf1dc75836ce4d30c17bfe598c4a1b11b7d0b2092b06e6
deps="$root/.deps"
mkdir -p "$deps"
if [[ -d "$deps/MNN" ]]; then
    if [[ -f "$deps/MNN/.vision-demo-source" ]] && [[ "$(cat "$deps/MNN/.vision-demo-source")" == "$version $sha" ]]; then
        echo "MNN $version already prepared: $deps/MNN"
        exit 0
    fi
    echo "Refusing to replace unverified $deps/MNN; move it aside first." >&2
    exit 1
fi
stage="$(mktemp -d "$deps/.mnn-prepare.XXXXXX")"
trap 'rm -rf -- "$stage"' EXIT
curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
    "https://codeload.github.com/alibaba/MNN/tar.gz/refs/tags/$version" -o "$stage/source.tar.gz"
printf '%s  %s\n' "$sha" "$stage/source.tar.gz" | sha256sum --check --status
tar -xzf "$stage/source.tar.gz" -C "$stage"
printf '%s %s\n' "$version" "$sha" > "$stage/MNN-$version/.vision-demo-source"
mv -- "$stage/MNN-$version" "$deps/MNN"
echo "Prepared MNN $version: $deps/MNN (no models downloaded)"
