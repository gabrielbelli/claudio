#!/bin/sh
# Stage a FreeBSD package CLOSURE into a rootfs, on a machine that is not
# FreeBSD and without pkg(8).
#
#   ./fetch-pkgs.sh <abi> <rootfs-dir> <package>...
#   ./fetch-pkgs.sh FreeBSD:15:amd64 rootfs python312 py312-duckdb
#
# A .pkg is a zstd-compressed tar and pkg.freebsd.org publishes a plain index,
# so both steps are ordinary tar work.  The index is SIGNATURE-CHECKED against
# a pinned FreeBSD repository key before anything is read out of it, and each
# package is then blake2b-checked against that signed index -- two claims, and
# the second is worth nothing without the first.  Ordinary tar work is what
# lets the image be built on a stock Linux runner, and it is the same reason
# `Containerfile.freebsd` has no RUN instruction: nothing FreeBSD ever executes
# during the build.
#
# HOW THIS DIFFERS FROM THE REFERENCE (`gabrielbelli/freebsd-oauth2-proxy-oci`)
#
#   Its `fetch-pkg.sh` -- singular -- fetches ONE package and aborts the moment
#   that package declares a dependency, because oauth2-proxy is a Go binary
#   from a port with no RUN_DEPENDS and staging a graph was not a problem it
#   had.  This image is CPython and DuckDB, which is a graph of 8 packages
#   after exclusions and 27 before, so the abort had to become a walk.  It is
#   `pkgindex.py resolve` that does the walking; see that file for what carried
#   over and what did not.
#
# WHY EXCLUSIONS EXIST AT ALL, AND WHY THEY ARE AN ARGUMENT AND NOT A DEFAULT
#
#   `databases/py-duckdb` declares RUN_DEPENDS on py-numpy and py-pandas, which
#   drag in openblas, gcc14 and binutils: 1183 MiB installed against 285 MiB
#   without them.  Upstream lists numpy and pandas under `extra == "all"`, and
#   nothing in `server/srv/` calls `.df()`, `.arrow()` or `fetchnumpy()` -- the
#   whole query layer is `.execute(...).fetchall()`.  But that is a judgement
#   about somebody else's package, so it is written on the command line where
#   it can be read, refused when it goes stale (see `pkgindex.py`), and
#   asserted at run time by CI rather than assumed here.
#
# WHAT IT DOES NOT DO, STATED SO NOBODY GOES LOOKING
#
#   It runs no pkg script -- no pre/post-install, no `ldconfig`, no
#   `compileall`.  There is nothing to run them with.  Two consequences are
#   handled elsewhere rather than papered over here: `/usr/local/lib` is not on
#   the runtime linker's path without the ldconfig hints file, which
#   `Containerfile.freebsd` answers with `LD_LIBRARY_PATH` and says why; and
#   the .pyc files come from the package itself, because the ports tree builds
#   them.
set -eu

ABI="${1:?usage: fetch-pkgs.sh <abi> <rootfs-dir> <package>...}"
ROOTFS="${2:?usage: fetch-pkgs.sh <abi> <rootfs-dir> <package>...}"
shift 2
[ "$#" -gt 0 ] || { echo "fetch-pkgs.sh: name at least one package" >&2; exit 2; }

BRANCH="${PKG_BRANCH:-latest}"
BASE="https://pkg.freebsd.org/${ABI}/${BRANCH}"
# `CDPATH= cd` is one command with CDPATH emptied for its duration, not an
# assignment; shellcheck reads it as the latter.
# shellcheck disable=SC1007
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
# Comma-separated, empty by default.  Named in the workflow rather than
# defaulted here, so the image's contents are decided in one readable place.
WITHOUT="${PKG_WITHOUT:-}"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# EVERY FETCH IS https AND STAYS https.
#
# `curl -fsSL` follows a redirect into plain `http` without a word -- curl's
# own manual: "By default curl only allows HTTP, HTTPS, FTP and FTPS on
# redirects".  One 302 in front of the index and the rest of the transfer is
# unauthenticated cleartext, which is the cheap way to author both the
# `repopath` every package is fetched from AND the `sum` it is checked
# against.  `--proto '=https'` refuses a non-https URL and `--proto-redir
# '=https'` refuses the downgrade; both fail non-zero, which is the direction
# this project wants.  `--max-redirs` bounds the chase.  The retries are
# separate value and not security: without them a transient 5xx on one of
# eight packages fails the whole stage with no second attempt.
fetch() {
    curl -fsSL --proto '=https' --proto-redir '=https' --max-redirs 3 \
        --retry 3 --retry-all-errors "$@"
}

echo "==> index ${BASE}/packagesite.pkg"
fetch "${BASE}/packagesite.pkg" -o "$work/packagesite.pkg"
# ALL THREE MEMBERS, and the two that used to be discarded are the point.
#
# The index is one JSON object per line, despite the .yaml name -- and it is
# the ROOT OF TRUST for this whole build: `repopath` says which URL each
# package comes from and `sum` is the blake2b digest it is then checked
# against, so an unverified index makes "every checksum verified" mean "the
# bytes matched what the substituted document said they would".  The archive
# has always carried `packagesite.yaml.sig` and `packagesite.yaml.pub`
# alongside; the previous version of this script extracted one member and
# threw the other two away.  `verifyindex` is the refusal, and it runs BEFORE
# anything reads a byte of the index.
tar xf "$work/packagesite.pkg" -C "$work" \
    packagesite.yaml packagesite.yaml.sig packagesite.yaml.pub
python3 "$HERE/pkgindex.py" verifyindex \
    "$work/packagesite.yaml" "$work/packagesite.yaml.sig" \
    "$work/packagesite.yaml.pub"

if [ -n "$WITHOUT" ]; then
    python3 "$HERE/pkgindex.py" resolve "$work/packagesite.yaml" \
        --without "$WITHOUT" "$@" > "$work/closure"
else
    python3 "$HERE/pkgindex.py" resolve "$work/packagesite.yaml" "$@" > "$work/closure"
fi

mkdir -p "$ROOTFS"
: > "${ROOTFS}.manifest"

# `while read` over a FILE, not a pipeline: the loop assigns nothing that has
# to survive it today, but a subshell here is the trap this repository has
# written up more than once and there is no reason to leave it loaded.
# `pkgsize` and `flatsize` are named so the tab-separated line is consumed
# field by field rather than by position; nothing reads them today.  Naming
# them is what keeps a sixth column from landing in `$sum`.
# shellcheck disable=SC2034
while IFS='	' read -r name version repopath sum pkgsize flatsize; do
    [ -n "$name" ] || continue
    echo "==> ${name} ${version}"
    fetch "${BASE}/${repopath}" -o "$work/pkg.pkg"
    python3 "$HERE/pkgindex.py" verify "$work/pkg.pkg" "$sum"
    # Everything except pkg's own metadata, which means nothing outside pkg.
    tar xf "$work/pkg.pkg" -C "$ROOTFS" --exclude '+*'
    printf '%s\t%s\n' "$name" "$version" >> "${ROOTFS}.manifest"
done < "$work/closure"

# The version that goes on the image as `org.opencontainers.image.version`.
# It is the FIRST package named on the command line -- the one the image is
# about -- rather than a synthetic string, so the label answers "which DuckDB
# is in here" without anyone opening the image.
first="$1"
version=$(awk -F'\t' -v n="$first" '$1 == n { print $2 }' "${ROOTFS}.manifest")
[ -n "$version" ] || { echo "fetch-pkgs.sh: ${first} did not stage" >&2; exit 1; }
printf '%s' "$version" > "${ROOTFS}.version"

echo "==> staged $(wc -l < "${ROOTFS}.manifest" | tr -d ' ') package(s) into ${ROOTFS}"
