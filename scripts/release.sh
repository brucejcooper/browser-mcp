#!/usr/bin/env bash
# Make the release for main's version, if it hasn't one yet: a Forgejo release
# v<version> with both images' docker-load tarballs and their SHA256SUMS.
# Run by CI on main, with FORGEJO_TOKEN (the job's own token) and API
# (<server>/api/v1/repos/<owner>/<repo>) set.
set -euo pipefail

version=$(nix eval --raw .#packages.x86_64-linux.default.version)
tag="v$version"
auth=(-H "Authorization: token $FORGEJO_TOKEN")

code=$(curl -sS -o /dev/null -w '%{http_code}' "${auth[@]}" "$API/releases/tags/$tag")
if [ "$code" = 200 ]; then
  echo "$tag is released already"
  exit 0
fi
[ "$code" = 404 ] || { echo "looking up $tag: HTTP $code" >&2; exit 1; }

out=$(mktemp -d)
trap 'rm -rf "$out"' EXIT
for pair in amd64:x86_64-linux arm64:aarch64-linux; do
  arch=${pair%%:*} system=${pair#*:}
  stream=$(nix build --no-link --print-out-paths ".#packages.$system.image")
  # The stream is a docker-archive tarball, as `docker save` writes.
  "$stream" | gzip > "$out/browser-mcp-$version-$arch.tar.gz"
done
(cd "$out" && sha256sum browser-mcp-*.tar.gz > SHA256SUMS && cat SHA256SUMS)

body="Images for linux/amd64 and linux/arm64. Check and load one:

    sha256sum -c --ignore-missing SHA256SUMS
    docker load -i browser-mcp-$version-arm64.tar.gz

It loads as browser-mcp:$version. See the README for Compose."
id=$(jq -n --arg tag "$tag" --arg sha "${GITHUB_SHA:-main}" --arg body "$body" \
  '{tag_name: $tag, target_commitish: $sha, name: $tag, body: $body, draft: true}' |
  curl -fsS -X POST "${auth[@]}" -H "Content-Type: application/json" --data @- "$API/releases" | jq -r .id)
# A failed upload leaves no half-made release behind.
trap 'curl -sS -X DELETE "${auth[@]}" "$API/releases/$id" >/dev/null; rm -rf "$out"' EXIT

for f in "$out"/*; do
  curl -fsS -X POST "${auth[@]}" -F "attachment=@$f" "$API/releases/$id/assets?name=$(basename "$f")" >/dev/null
  echo "uploaded $(basename "$f")"
done
curl -fsS -X PATCH "${auth[@]}" -H "Content-Type: application/json" --data '{"draft": false}' \
  "$API/releases/$id" >/dev/null
trap 'rm -rf "$out"' EXIT
echo "released $tag"
