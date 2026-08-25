#!/bin/sh
# Stage everything a FreeBSD `Containerfile` COPYs, and check it before it is
# copied.  One script for all four images, because the package list, the
# exclusions and the prune list are one decision about what each image contains
# and splitting them over five workflow steps would put that decision in five
# places.
#
#   ./stage-freebsd.sh <component> <abi> <arch> <stage-dir> [base-rootfs]
#   ./stage-freebsd.sh ingest FreeBSD:15:amd64 amd64 stage-freebsd-ingest
#   ./stage-freebsd.sh api    FreeBSD:15:amd64 amd64 stage-freebsd-api
#   ./stage-freebsd.sh mcp    FreeBSD:15:amd64 amd64 stage-freebsd-mcp
#   ./stage-freebsd.sh proxy  FreeBSD:15:amd64 amd64 stage-freebsd-proxy
#
# THE COMPONENT IS THE FIRST ARGUMENT AND IT IS REQUIRED, WITH NO DEFAULT.
#
#   There are four images and they differ in the one thing this script
#   decides: which packages go in.  A default would pick one of them for a
#   caller who did not say -- and the wrong pick is not a build failure, it is
#   an mcp image carrying DuckDB and the store's query layer, which is the
#   privilege boundary the split exists to draw.  An unknown component is a
#   NAMED refusal listing the four, so a caller written before this argument
#   existed fails on its next run with a message saying exactly what to add.
#
# `arch` is passed IN and not derived from the ABI, and that is the whole
# reason the architecture check is worth running: derived, it would compare the
# ABI against itself and pass for any pair the matrix could get wrong.  The
# matrix states `base`, `arch` and `abi` independently -- the reference
# repository's rule -- and this is where two of the three are made to agree.
#
# The optional fifth argument is an unpacked `freebsd/freebsd-runtime` rootfs.
# When it is given the shared-library check runs; when it is not, the check is
# SKIPPED AND SAID SO, because a check that silently does not run is worse than
# one that is not there.  CI always passes it.
set -eu

usage='usage: stage-freebsd.sh <ingest|api|mcp|proxy> <abi> <arch> <stage-dir> [base-rootfs]'
COMPONENT="${1:?$usage}"
ABI="${2:?$usage}"
ARCH="${3:?$usage}"
STAGE="${4:?$usage}"
BASE="${5:-}"
# `CDPATH= cd` is one command with CDPATH emptied for its duration, not an
# assignment; shellcheck reads it as the latter.
# shellcheck disable=SC1007
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SRV="${HERE}/../srv"
PYVER="${PYVER:-3.12}"

# ---- what each image contains -------------------------------------------
#
# PKGS         the roots of the closure; the FIRST one names the image's
#              version label, because `fetch-pkgs.sh` reads it from there.
# WITHOUT      comma-separated nodes removed from the graph, or empty.
# PRUNE_LIB    directories under `lib/python<ver>/` deleted after unpacking.
# PRUNE_PATHS  paths relative to the staged rootfs deleted after unpacking.
# MODULES_FROM the file or directory whose imports must survive the prune, or
#              empty for an image with no Python in it.
# DEFERRED_OK  modules that source imports ONLY inside a function body and that
#              this image deliberately does not stage.  Named here rather than
#              defaulted, because the difference between "absent on purpose"
#              and "absent by mistake" is a sentence a person wrote.
# WANT_STORE   stage `/var/lib/claudio-usage`; the ingest and api components
#              share a store, one read-write and one read-only.
# WANT_CONF    stage `/etc/claudio`; those same two read a token file --
#              `tokens.json` for the ingest half, `readers.json` for the api.
# WANT_NGINX   stage nginx's writable directories and its `mime.types`.
PRUNE_LIB=""
PRUNE_PATHS=""
MODULES_FROM=""
DEFERRED_OK=""
WITHOUT=""
WANT_STORE=no
WANT_CONF=no
WANT_NGINX=no

# The CPython prune list, shared by the two images that carry an interpreter.
# Each entry is here because nothing the image runs can reach it, and
# `pkgindex.py modules` is what turns that from a claim into a check -- it
# derives the import list with `ast` and fails if the prune took anything on
# it.
#
#   test          the CPython test suite: 132 MiB of a 304 MiB tree
#   config-3.12   the static library and build machinery for compiling C
#                 extensions, which these images cannot do anyway: no compiler
#                 and no RUN
#   idlelib       the Tk IDE.  Tk is not even in the closure, so it is already
#                 non-functional
#   lib2to3       removed from CPython upstream
#   ensurepip     bundled wheels; there is no pip in these images and nothing
#                 to install with it
#   include       C headers, same reason as config-3.12 (a PRUNE_PATHS entry,
#                 because it is not under the python lib directory)
PY_PRUNE_LIB="test config-3.12 idlelib lib2to3 ensurepip"

case "$COMPONENT" in
    ingest)
        # PYTHON ONLY, AND THAT IS THE OTHER HALF OF THE SPLIT.
        #
        # The ingest service parses a manifest, checks a bearer token, appends
        # the client's own bytes and fsyncs.  It never queries, and `import
        # duckdb` in the whole package happens in exactly one place -- inside
        # `store.require()`, called from `DuckStore.con()` on the first query --
        # so nothing on this path reaches it.  Measured directly on a machine
        # with no duckdb installed: `import srv.serve` succeeds and
        # `store.available()` is False, which is the case `serve.py` already
        # prints `engine duckdb MISSING` for.
        #
        # Same closure as the mcp image, for the same reason.  MEASURED by
        # running this script against the live index on 2026-08-25, on all four
        # matrix ABIs, and the numbers that matter are the STAGED ones rather
        # than the index's `flatsize`:
        #
        #   closure   7 packages, 225.6 MiB installed, 37.8 MiB download
        #   staged     60.5 MiB after the prune (243.7 MiB before it)
        #
        # against the api's 8 / 285.4 / 52.2 and 120.7 MiB staged.  Quoting
        # `flatsize` alone overstates both by a factor of nearly four, because
        # `lib/python3.12/test` and `config-3.12` are 168 MiB of what pkg
        # installs and neither reaches the image.
        #
        # WHAT IS STILL UNVERIFIED: no FreeBSD image has been BUILT or RUN --
        # buildah and podman cannot be installed on the machine this was
        # written on, and a Linux kernel cannot execute FreeBSD binaries.  What
        # has been run here is everything up to `buildah bud`: the signed
        # index, the closure, every blake2b checksum, the unpack, the prune,
        # the ELF architecture of all 94 files, the import set, and every
        # DT_NEEDED against the real unpacked `freebsd-runtime` base.
        PKGS="python312"
        PRUNE_LIB="$PY_PRUNE_LIB"
        PRUNE_PATHS="usr/local/include"
        # THE IMPORT CHECK IS AGAINST THE WHOLE PACKAGE AND NOT AGAINST
        # `serve.py`, EVEN THOUGH ONLY THE SHIP PATH RUNS HERE.  The image
        # carries `server/srv/` entire -- the api image is the same source with
        # one package added -- so every module in it must survive the prune.
        #
        # `duckdb` IS IN THAT LIST, AND THE PREVIOUS TEXT HERE SAID IT WAS NOT.
        # It claimed the check "reads module-level imports", so a function-body
        # `import duckdb` was invisible to it.  It was not: `_srv_imports`
        # walked the tree with `ast.walk`, which descends into function bodies,
        # and this branch therefore FAILED ITS OWN CHECK on every run -- found
        # by running the stage against the live FreeBSD:15:amd64 index, not by
        # reading it.  `pkgindex.py` now really does distinguish the two, and
        # `--deferred-ok` is where this image states which absence is
        # deliberate.  Declaring it is what makes the check bite: put `import
        # duckdb` at module level anywhere in `srv/` and the flag refuses by
        # name rather than shrugging, because at that point the ingest process
        # would need the package before it could serve anything at all.
        MODULES_FROM="$SRV"
        DEFERRED_OK="duckdb"
        WANT_STORE=yes
        WANT_CONF=yes
        ;;
    api)
        # `py312-duckdb` first, because `fetch-pkgs.sh` labels the image with
        # the version of the first package named; `python312` second, and it
        # would come in as a dependency anyway -- naming it makes the
        # interpreter a stated part of the image rather than a consequence of
        # somebody else's RUN_DEPENDS.
        PKGS="py312-duckdb python312"
        # `databases/py-duckdb` declares RUN_DEPENDS on py-numpy and py-pandas.
        # Those two pull openblas, which pulls gcc14 and binutils: 27 packages
        # and 1183 MiB installed, against 8 packages and 285.4 MiB without
        # them.  Re-measured against the live FreeBSD:15:amd64 index on
        # 2026-08-25 and unchanged; the exclusion drops 19 packages and 898 MiB.
        # The staged tree is 120.7 MiB, of which py312-duckdb is 59.9.
        #
        # The evidence that they are not needed, in the order it was gathered:
        #   * upstream's own metadata lists numpy, pandas, pyarrow, fsspec and
        #     ipython under `extra == "all"` -- every one optional;
        #   * `duckdb/__init__.py` in the staged FreeBSD package imports none
        #     of them.  The only submodules that do are `filesystem.py`
        #     (fsspec) and `polars_io.py` (polars), and NEITHER fsspec NOR
        #     polars is a FreeBSD dependency of this package at all -- which is
        #     the proof that those submodules are not eagerly imported, since
        #     the package would be broken out of the box otherwise;
        #   * nothing in `server/srv/` calls `.df()`, `.arrow()`,
        #     `fetchnumpy()` or `to_df()`; the whole query layer is
        #     `.execute(...).fetchall()`;
        #   * the full `server/` suite passes against duckdb 1.5.5 on an
        #     interpreter with numpy, pandas and pyarrow all absent.
        #
        # That is four pieces of evidence and none of them is "we ran this on
        # FreeBSD".  The smoke job is what makes it a measurement rather than
        # an inference, and the exclusion refuses itself when it goes stale --
        # see `pkgindex.py`.
        WITHOUT="py312-numpy,py312-pandas"
        PRUNE_LIB="$PY_PRUNE_LIB"
        PRUNE_PATHS="usr/local/include"
        MODULES_FROM="$SRV"
        # THE STORE DIRECTORY IS STAGED HERE TOO, AND IT IS MOUNTED `:ro`.
        # The api reads it and never writes; the directory still has to exist
        # in the image so that the mount lands on a path with the right
        # ownership, exactly as it does for the ingest service.  Which
        # DIRECTION it is mounted is the compose file's decision and the
        # preflight's warning, not this script's.
        WANT_STORE=yes
        WANT_CONF=yes
        ;;
    mcp)
        # PYTHON ONLY, AND THAT IS THE WHOLE ARGUMENT FOR A SECOND IMAGE.
        #
        # `srv/mcp.py` is an HTTP CLIENT of the read API: it imports json, os,
        # sys and urllib and nothing else, holds a reader token, and touches no
        # file in the store.  Staging `py312-duckdb` here would add a database
        # handle to a process whose entire authority is meant to be one bearer
        # token -- a privilege escalation, and it would also defeat the split.
        #
        # Measured against the live index on 2026-08-25: 7 packages, 225.6 MiB
        # installed, 37.8 MiB download, and 60.5 MiB once staged and pruned --
        # byte-identical to the ingest tree, since the package set and the
        # prune list are the same.  The api is 8 / 285.4 / 52.2 and 120.7 MiB
        # staged; `py312-duckdb` alone is the whole of the difference.
        PKGS="python312"
        PRUNE_LIB="$PY_PRUNE_LIB"
        PRUNE_PATHS="usr/local/include"
        # THE IMPORT CHECK IS SCOPED TO `mcp.py`, NOT TO `srv/`.
        #
        # This image contains one module, so checking it against the whole
        # package's import list would demand `duckdb` be staged -- i.e. the
        # check would insist on exactly the dependency the image exists to shed.
        # Pointed at the file, it asserts the true thing: everything THIS
        # module imports survived the prune.
        MODULES_FROM="${SRV}/mcp.py"
        ;;
    proxy)
        # nginx and pcre2, and nothing else: 2 packages, 9.7 MiB installed,
        # 2.0 MiB download, 6.4 MiB once staged and pruned.
        #
        # THAT EVERY OTHER LIBRARY IS IN THE BASE IS NOW CHECKED RATHER THAN
        # CLAIMED.  `pkgindex.py shlibs` was run against the real unpacked
        # `freebsd/freebsd-runtime` rootfs for all three bases on both
        # architectures: 6 ELF files staged, every DT_NEEDED satisfied, six
        # rows for six -- so libssl, libcrypto, libz, libcrypt, libthr and libc
        # really are the base's.  The same check refuses the mismatch it exists
        # for: a FreeBSD:14 tree against a 15.1 base names libutil.so.9, and a
        # FreeBSD:15 tree against a 14.4 base names libutil.so.10.
        #
        # It is what makes this image worth building rather than telling a
        # FreeBSD operator to run the Linux proxy beside their FreeBSD stack:
        # 6.4 MiB staged on a base that is 34 MiB unpacked, so ~40 MiB, against
        # the 94.2 MB the Linux proxy image measures on this machine.  Both
        # ends uncompressed, because the base is 12 MiB COMPRESSED and quoting
        # that against an uncompressed 93.6 MB would flatter this image by a
        # factor of three.
        PKGS="nginx"
        # Named individually, and each is deleted because nothing this image
        # runs can reach it:
        #   share/vim         syntax files for an editor that is not here
        #   share/man         manual pages, with no man(1) to read them
        #   etc/rc.d          the rc script; this image is not booted
        #   www/nginx-dist    nginx's default "Welcome" site, which is a
        #                     document root this configuration never serves
        #   libexec/nginx/*   the mail and stream dynamic modules.  The
        #                     configuration carries no `load_module`, so they
        #                     are never opened; leaving them in would suggest
        #                     they are part of the contract.
        PRUNE_PATHS="usr/local/share/vim usr/local/share/man usr/local/etc/rc.d
                     usr/local/www/nginx-dist
                     usr/local/libexec/nginx/ngx_mail_module.so
                     usr/local/libexec/nginx/ngx_stream_module.so"
        WANT_NGINX=yes
        ;;
    *)
        echo "stage-freebsd.sh: unknown component '${COMPONENT}'." >&2
        echo "  Expected one of: ingest, api, mcp, proxy." >&2
        echo "  The component is the FIRST argument and has no default: the" >&2
        echo "  images differ in which packages they carry, and guessing would" >&2
        echo "  put DuckDB and the store's query layer in the mcp image, which" >&2
        echo "  is the boundary the split exists to draw." >&2
        echo "  ${usage}" >&2
        exit 2
        ;;
esac

ROOTFS="${STAGE}/rootfs"
rm -rf "$STAGE"
mkdir -p "$STAGE"

echo "### staging ${COMPONENT}: ${PKGS} for ${ABI}"
# `$PKGS` is a space-separated package LIST and is meant to split into one
# argument per package; quoting it would pass the whole list as a single
# package name, which `pkgindex.py resolve` would then refuse by name.
# shellcheck disable=SC2086
PKG_WITHOUT="$WITHOUT" "${HERE}/fetch-pkgs.sh" "$ABI" "$ROOTFS" $PKGS

# ---- prune ---------------------------------------------------------------
before=$(du -sk "$ROOTFS" | awk '{print $1}')
LIB="${ROOTFS}/usr/local/lib/python${PYVER}"
for d in $PRUNE_LIB; do
    if [ -e "${LIB}/${d}" ]; then
        echo "==> prune $(du -sk "${LIB}/${d}" | awk '{print $1/1024 "MiB"}') lib/python${PYVER}/${d}"
        rm -rf "${LIB:?}/${d}"
    else
        # Named, never silent.  A prune entry that stopped matching is a prune
        # list going stale, and the next person reads the list rather than the
        # tree.
        echo "NOTE: prune list names lib/python${PYVER}/${d}, which is not there" >&2
    fi
done
for p in $PRUNE_PATHS; do
    if [ -e "${ROOTFS}/${p}" ]; then
        echo "==> prune $(du -sk "${ROOTFS}/${p}" | awk '{print $1/1024 "MiB"}') ${p}"
        rm -rf "${ROOTFS:?}/${p}"
    else
        echo "NOTE: prune list names ${p}, which is not there" >&2
    fi
done
# Static libraries: nothing in any of these images links anything.
find "$ROOTFS" -name '*.a' -type f -delete
after=$(du -sk "$ROOTFS" | awk '{print $1}')
echo "==> pruned ${before}KiB -> ${after}KiB"

# ---- the directories each image needs to exist ---------------------------
#
# There is no RUN, so `mkdir` and `chown` cannot happen inside the image.  The
# directories are staged here instead and COPYed with `--chown`; the `.keep`
# files exist only because a COPY of an empty directory is not a portable way
# to create one.  Every one of them is shadowed the moment something is mounted
# over it.
if [ "$WANT_STORE" = yes ]; then
    mkdir -p "${STAGE}/store"
    : > "${STAGE}/store/.keep"
fi
if [ "$WANT_CONF" = yes ]; then
    mkdir -p "${STAGE}/conf"
    : > "${STAGE}/conf/.keep"
fi
if [ "$WANT_NGINX" = yes ]; then
    # nginx's paths are COMPILED IN, read out of the real binary rather than
    # guessed: `/var/tmp/nginx/{client_body,proxy,fastcgi,uwsgi,scgi}_temp`,
    # `/var/log/nginx/{access,error}.log` and `/var/run/nginx.pid`.  The
    # package ships the two directories owned by root, and this image runs as
    # 65534, so a worker would fail to buffer a request body -- at run time,
    # under load, on somebody else's machine.  They are moved out of the rootfs
    # and staged as their own COPY so `--chown` can reach them.
    #
    # The pid file is answered in the configuration instead (`pid`), because
    # `/var/run` is the base image's directory and not this build's to chown.
    rm -rf "${ROOTFS}/var/tmp/nginx" "${ROOTFS}/var/log/nginx"
    for d in client_body_temp proxy_temp fastcgi_temp uwsgi_temp scgi_temp; do
        mkdir -p "${STAGE}/run/${d}"
        : > "${STAGE}/run/${d}/.keep"
    done
    mkdir -p "${STAGE}/logs"
    : > "${STAGE}/logs/.keep"

    # `/etc/claudio/nginx.d` is the operator's drop-in directory -- a TLS
    # server block, or a `location` this stack does not ship.  It is staged
    # EMPTY so the mount point exists and is visible in the image: nginx
    # ignores a wildcard `include` that matches nothing, so an image with
    # nothing mounted starts either way, and a directory that is there says
    # what the mount is for where a missing one says nothing at all.
    mkdir -p "${STAGE}/conf/nginx.d"
    : > "${STAGE}/conf/nginx.d/.keep"

    # `mime.types` is shipped as `mime.types-dist` and pkg's post-install
    # script is what copies it into place.  There is no script here and no RUN
    # to run one with, so the copy happens on the BUILD host, where it is
    # ordinary file work.  The main configuration `include`s the file
    # unconditionally, so its absence is not a degradation to shrug at: nginx
    # would refuse to start.  Hence the refusal below rather than a note.
    dist="${ROOTFS}/usr/local/etc/nginx/mime.types-dist"
    if [ -f "$dist" ]; then
        cp "$dist" "${ROOTFS}/usr/local/etc/nginx/mime.types"
        echo "==> mime.types installed from mime.types-dist"
    else
        # Refused rather than noted: the configuration `include`s the file, so
        # an image built without it does not start at all.  Better to fail on
        # the build host, where the package layout can be looked at, than to
        # publish an image that exits on first run.
        echo "stage-freebsd.sh: the nginx package staged no mime.types-dist," >&2
        echo "  so nginx.conf's \`include mime.types\` would fail at startup." >&2
        echo "  The package layout changed; look at ${ROOTFS}/usr/local/etc/nginx/." >&2
        exit 1
    fi
fi

# ---- the checks ----------------------------------------------------------
python3 "${HERE}/pkgindex.py" arch "$ROOTFS" "$ARCH"
if [ -n "$MODULES_FROM" ]; then
    if [ -n "$DEFERRED_OK" ]; then
        python3 "${HERE}/pkgindex.py" modules "$ROOTFS" "$MODULES_FROM" \
            "$PYVER" --deferred-ok "$DEFERRED_OK"
    else
        python3 "${HERE}/pkgindex.py" modules "$ROOTFS" "$MODULES_FROM" "$PYVER"
    fi
else
    # Said out loud, because three of the four components run it and a
    # reader of the log should not have to work out from its absence that this
    # image has no interpreter in it.
    echo "==> modules: ${COMPONENT} carries no Python, so there is no import" >&2
    echo "==>   list to check the prune against." >&2
fi

if [ -n "$BASE" ]; then
    python3 "${HERE}/pkgindex.py" shlibs "$ROOTFS" "$BASE"
else
    echo "SKIP shlibs: no base rootfs given, so the base/ABI pairing and the" >&2
    echo "SKIP   exclusion list are UNCHECKED for this stage. CI passes one." >&2
fi

echo "### staged ${COMPONENT} in ${STAGE} ($(cat "${ROOTFS}.version")), $(du -sh "$ROOTFS" | awk '{print $1}')"
