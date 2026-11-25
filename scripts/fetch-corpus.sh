#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOCK="${COWORLD_MTG_CORPUS_LOCK:-$ROOT/corpus.lock.json}"
DESTINATION="${COWORLD_MTG_CORPUS_DIR:-$ROOT/.private/corpus}"
ALLOWED_PARENT="${COWORLD_MTG_CORPUS_PARENT:-$ROOT/.private}"
# Where the archive lives is the caller's choice, not the repository's: the lock
# pins what the archive must be (its SHA-256), and COWORLD_MTG_CORPUS_URI says
# where this machine reads it from. A lock may still carry an `archive_uri`
# default.
URI="${COWORLD_MTG_CORPUS_URI:-$(jq -r '.archive_uri // empty' "$LOCK")}"
if [[ -z "$URI" ]]; then
  cat >&2 <<'USAGE'
Set COWORLD_MTG_CORPUS_URI to where the private corpus archive can be read:
  gh://<owner>/<repo>/<release tag>/<asset>   release asset of a GitHub repository (uses your `gh` sign-in)
  s3://<bucket>/<key>                         S3 object (uses AWS_PROFILE, default "softmax")
  https://...                                 direct download
  /path/to/coworld-mtg-corpus-v1.tar.zst      local file
The archive must match the SHA-256 in corpus.lock.json. See "Private runtime corpus" in README.md.
USAGE
  exit 1
fi
EXPECTED_SHA256="$(jq -r .sha256 "$LOCK")"

RESOLVED_PATHS=$(python3 - "$DESTINATION" "$ALLOWED_PARENT" <<'PY'
import sys
from pathlib import Path

destination = Path(sys.argv[1]).expanduser().resolve(strict=False)
allowed_parent = Path(sys.argv[2]).expanduser().resolve(strict=False)
for forbidden in (Path("/").resolve(), Path.home().resolve()):
    if destination == forbidden:
        raise SystemExit(f"refusing unsafe corpus destination: {destination}")
if destination == allowed_parent or not destination.is_relative_to(allowed_parent):
    raise SystemExit(
        f"corpus destination {destination} must be below allowed parent {allowed_parent}"
    )
print(destination)
print(allowed_parent)
PY
)
DESTINATION=$(printf '%s\n' "$RESOLVED_PATHS" | sed -n '1p')
ALLOWED_PARENT=$(printf '%s\n' "$RESOLVED_PATHS" | sed -n '2p')
mkdir -p "$ALLOWED_PARENT"
WORK="$(mktemp -d "$ALLOWED_PARENT/.corpus-install.XXXXXX")"
ARCHIVE="$WORK/archive.tar.zst"
TAR_ARCHIVE="$WORK/archive.tar"
EXTRACTED="$WORK/new"
mkdir -p "$EXTRACTED"
trap 'rm -rf "$WORK"' EXIT

case "$URI" in
  s3://*) aws s3 cp "$URI" "$ARCHIVE" --profile "${AWS_PROFILE:-softmax}" --only-show-errors ;;
  gh://*)
    # gh://<owner>/<repo>/<release tag>/<asset name>: a release asset of a
    # private GitHub repository, read with the caller's `gh` sign-in.
    IFS=/ read -r GH_OWNER GH_REPO GH_TAG GH_ASSET <<<"${URI#gh://}"
    if [[ -z "$GH_OWNER" || -z "$GH_REPO" || -z "$GH_TAG" || -z "$GH_ASSET" || "$GH_ASSET" == */* ]]; then
      echo "corpus URI must be gh://<owner>/<repo>/<tag>/<asset>: $URI" >&2
      exit 1
    fi
    gh release download "$GH_TAG" --repo "$GH_OWNER/$GH_REPO" --pattern "$GH_ASSET" --output "$ARCHIVE"
    ;;
  file://*) cp "${URI#file://}" "$ARCHIVE" ;;
  https://*|http://*) curl --fail --location --silent --show-error "$URI" --output "$ARCHIVE" ;;
  *) cp "$URI" "$ARCHIVE" ;;
esac

ACTUAL_SHA256="$(shasum -a 256 "$ARCHIVE" | awk '{print $1}')"
test "$ACTUAL_SHA256" = "$EXPECTED_SHA256" || {
  echo "corpus archive SHA-256 mismatch: expected $EXPECTED_SHA256, got $ACTUAL_SHA256" >&2
  exit 1
}

zstd -q -d -c "$ARCHIVE" > "$TAR_ARCHIVE"
python3 - "$TAR_ARCHIVE" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

with tarfile.open(sys.argv[1]) as archive:
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"unsafe corpus archive path: {member.name}")
        if not (member.isfile() or member.isdir()):
            raise SystemExit(f"unsupported corpus archive member: {member.name}")
PY
tar -xf "$TAR_ARCHIVE" -C "$EXTRACTED"
python3 - "$EXTRACTED" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
manifest = json.loads((root / "manifest.json").read_text())
assert manifest["schema"] == "coworld-mtg-corpus-v1"
for name, expected in manifest["files"].items():
    data = (root / name).read_bytes()
    assert len(data) == expected["bytes"], name
    assert hashlib.sha256(data).hexdigest() == expected["sha256"], name
PY

mkdir -p "$(dirname "$DESTINATION")"
if [[ -e "$DESTINATION" ]]; then
  mv "$DESTINATION" "$WORK/previous"
fi
if ! mv "$EXTRACTED" "$DESTINATION"; then
  if [[ -e "$WORK/previous" ]]; then
    mv "$WORK/previous" "$DESTINATION"
  fi
  exit 1
fi
echo "materialized $DESTINATION ($EXPECTED_SHA256)"
