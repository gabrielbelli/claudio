#!/usr/bin/env python3
"""The pkg(8) knowledge the FreeBSD image build needs, and nothing else.

WHAT CARRIES OVER FROM `gabrielbelli/freebsd-oauth2-proxy-oci`, AND WHAT DOES NOT

  That repository stages ONE package.  Its `pkgindex.py` has two commands:
  `find`, which locates a package in the index, and `verify`, which checks the
  blake2b checksum pkg records for it.  `find` REFUSES the moment the package
  it was asked for declares any dependency at all, and says why: "this build
  fetches a single package and does not resolve a dependency graph, so the
  image would be missing them".

  `verify` and `zbase32` carry over unchanged -- the checksum format is the
  same format, and it is the part that would fail silently if it were wrong.
  `find` does not carry over at all, because its refusal is exactly the wall
  this image walks into: oauth2-proxy is one Go binary from a port with no
  RUN_DEPENDS, and this image needs CPython and DuckDB, which are a graph.  So
  `find` is replaced by `resolve`, which walks that graph, and the refusal it
  used to raise becomes the ordinary case.

  `shlibs` is new and has no counterpart there.  It exists because the graph
  brought a second failure with it: a package whose `shlibs_required` is
  satisfied by a package nobody staged produces an image that builds, pushes
  and passes every check up to the moment somebody runs it.

THE THREE THINGS THIS FILE REFUSES TO DO QUIETLY

  A dependency name absent from the index is a NAMED refusal, never a skipped
  edge.  A skipped edge is a missing library in a published image.

  An `--without` name that is not in the closure is a NAMED refusal, not a
  no-op.  An exclusion list is written once and read for years; a stale entry
  that silently stops applying would restore 900 MB to an image whose whole
  README says it does not carry it, and nothing would say so.

  An unsupported checksum version is a refusal, not a skipped check -- the
  reference's rule, kept verbatim.  A checksum check that cannot match is
  worse than none, because it looks like one.
"""

import base64
import hashlib
import hmac
import json
import os
import struct
import sys

# pkg's own base32 alphabet (z-base-32), least significant bit first within
# each byte.  Worked out against a real package rather than assumed -- and this
# is the reference implementation, unchanged, for the same reason it was worked
# out there: a checksum check that can never match looks like a check.
ALPHABET = "ybndrfg8ejkmcpqxot1uwisza345h769"


def zbase32(raw):
    out, acc, nbits = [], 0, 0
    for b in bytearray(raw):
        acc |= b << nbits
        nbits += 8
        while nbits >= 5:
            out.append(ALPHABET[acc & 31])
            acc >>= 5
            nbits -= 5
    if nbits:
        out.append(ALPHABET[acc & 31])
    return "".join(out)


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------

def load(index_path):
    """Every entry in the index, keyed by package name.

    The file is named `packagesite.yaml` and is one JSON object per line.  A
    line that will not parse is skipped, which is the reference's behaviour and
    is safe HERE and only here: the very next thing that happens is a lookup by
    name, and a name that went missing because its line was mangled comes back
    as the "not in the index" refusal rather than as an absence.
    """
    entries = {}
    with open(index_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            name = entry.get("name")
            if name:
                entries[name] = entry
    if not entries:
        sys.exit("%s parsed to nothing; it is not a pkg index" % index_path)
    return entries


def closure(entries, roots, without=()):
    """The transitive dependency closure of `roots`, minus `without`.

    `without` removes NODES, so a package that was only ever reachable through
    an excluded one goes with it, while a package something else also needs
    stays.  That is the whole reason it is a graph walk and not a list
    subtraction: dropping `py312-pandas` must drop `py312-numpy` if and only if
    nothing else in the closure wants it.
    """
    drop = set(without)
    seen, order, queue = set(), [], list(roots)
    missing = []
    while queue:
        name = queue.pop(0)
        if name in seen or name in drop:
            continue
        seen.add(name)
        entry = entries.get(name)
        if entry is None:
            missing.append(name)
            continue
        order.append(name)
        for dep in sorted((entry.get("deps") or {}).keys()):
            if dep not in seen and dep not in drop:
                queue.append(dep)
    if missing:
        sys.exit(
            "these packages are not in the index: %s\n"
            "  A dependency the index does not carry is a missing library in "
            "the published image, so this is a refusal rather than a skipped "
            "edge. Check the ABI and the package names against\n"
            "  https://pkg.freebsd.org/<abi>/latest/" % ", ".join(sorted(missing)))
    return sorted(order)


def cmd_resolve(argv):
    """resolve <index> [--without a,b] <package>...

    Prints one TSV line per package: name, version, repopath, checksum,
    pkgsize, flatsize.  Ordered by name so two runs of the same index produce
    byte-identical output and a diff of two runs is a diff of the repository.
    """
    without = ()
    args = []
    i = 0
    while i < len(argv):
        if argv[i] == "--without":
            i += 1
            if i >= len(argv):
                sys.exit("--without needs a comma-separated list")
            without = tuple(w for w in argv[i].split(",") if w)
        elif argv[i].startswith("--without="):
            without = tuple(w for w in argv[i].split("=", 1)[1].split(",") if w)
        else:
            args.append(argv[i])
        i += 1
    if len(args) < 2:
        sys.exit("usage: pkgindex.py resolve <index> [--without a,b] <package>...")

    entries = load(args[0])
    roots = args[1:]

    full = closure(entries, roots)
    # A stale exclusion is a refusal, not a no-op.  See the module docstring:
    # this list is written once and read for years, and an entry that quietly
    # stopped applying would restore most of a gigabyte to an image whose
    # README says it does not carry it, with nothing anywhere saying so.
    stale = [w for w in without if w not in full]
    if stale:
        sys.exit(
            "--without names %s, which %s not in the dependency closure of "
            "%s.\n"
            "  Excluding something that is not there is a no-op that looks "
            "like a decision. Either the package was renamed, or the port "
            "stopped depending on it and this list should lose the entry."
            % (", ".join(stale), "is" if len(stale) == 1 else "are",
               ", ".join(roots)))

    kept = closure(entries, roots, without=without)
    dropped = [n for n in full if n not in kept]
    if dropped:
        bytes_ = sum(entries[n].get("flatsize") or 0 for n in dropped)
        sys.stderr.write(
            "==> excluding %d package(s), %.0f MiB installed: %s\n"
            % (len(dropped), bytes_ / 1048576.0, " ".join(dropped)))

    total = sum(entries[n].get("flatsize") or 0 for n in kept)
    down = sum(entries[n].get("pkgsize") or 0 for n in kept)
    sys.stderr.write("==> %d package(s), %.0f MiB installed, %.0f MiB download\n"
                     % (len(kept), total / 1048576.0, down / 1048576.0))

    for name in kept:
        e = entries[name]
        sys.stdout.write("\t".join((
            name,
            e.get("version", ""),
            e.get("repopath", ""),
            e.get("sum", ""),
            str(e.get("pkgsize") or 0),
            str(e.get("flatsize") or 0),
        )) + "\n")


def cmd_verify(argv):
    if len(argv) != 2:
        sys.exit("usage: pkgindex.py verify <file> <checksum>")
    path, want = argv
    if not want:
        sys.exit("the index carried no checksum for this package")
    version, _, digest = want.partition("$")
    if version != "2":
        sys.exit("unsupported pkg checksum version %r; refusing to skip the check"
                 % version)
    with open(path, "rb") as fh:
        got = zbase32(hashlib.blake2b(fh.read()).digest())
    if got != digest:
        sys.exit("checksum mismatch for %s\n  expected %s\n  got      %s"
                 % (path, digest, got))


# ---------------------------------------------------------------------------
# the index's own signature -- the root of trust, and it used to be absent
# ---------------------------------------------------------------------------
#
# EVERYTHING DOWNSTREAM DERIVES FROM `packagesite.yaml`: which URL each package
# is fetched from (`repopath`) and the blake2b digest `verify` compares it
# against (`sum`).  So while the index itself was unauthenticated, "verifies
# every blake2b checksum" meant only that the bytes matched what the same
# unverified document said they would be -- an attacker substituting the index
# substitutes both halves at once and every check passes.  The staged tree is
# then `COPY`ed to the root of a published FreeBSD image, so that is arbitrary
# code running as the door, unpacked and parsed inside the one CI job that
# holds `packages: write` and a live registry token.
#
# It needed no new dependency and no `openssl`.  `packagesite.pkg` already
# CONTAINS the two members that close it -- the previous script extracted
# `packagesite.yaml` and discarded `packagesite.yaml.sig` (256 bytes) and
# `packagesite.yaml.pub` (451 bytes) that sat beside it.  Parsing the SPKI DER
# for (n, e) and doing `pow(sig, e, n)` is the same register the ELF reader
# below is written in.
#
# MEASURED AGAINST THE LIVE `FreeBSD:15:amd64/latest` INDEX ON 2026-08-21, and
# every step of it reproduced rather than assumed:
#   * the archive holds exactly those three members;
#   * `sha256(packagesite.yaml.pub)` is b0170035...f438, byte for byte the
#     fingerprint in FreeBSD's own
#     `/usr/share/keys/pkg/trusted/pkg.freebsd.org.2013102301`;
#   * the key is RSA-2048, e=65537, and the recovered EMSA-PKCS1-v1_5 block
#     carries the SHA-256 DigestInfo OID;
#   * and the signed value is pkg's DOUBLE hash -- sha256 of the ASCII HEX
#     STRING of sha256(packagesite.yaml), not of the file's own digest.  Both
#     were tried: the double hash matches and the plain one does not.  A reader
#     who "corrects" that to a single hash gets a check that can never match,
#     which is the reference repository's rule about checksum versions in a
#     second costume.
#
# WHAT THIS DOES NOT CLAIM.  It is a signature check against a PINNED KEY, not
# a trust store: there is no fingerprint directory, no revocation and no key
# rotation path.  If FreeBSD rotates the repository key this refuses by name
# and the fix is to compare the new key against
# `/usr/share/keys/pkg/trusted/` on a real FreeBSD machine and change the
# constant HERE, in one place, deliberately.  A mismatch means the key moved.
# It does not mean the check is broken, and it must never be answered by
# deleting the call.

# FreeBSD's repository key fingerprint, from
# `/usr/share/keys/pkg/trusted/pkg.freebsd.org.2013102301`.  It is the sha256
# of the PEM file exactly as pkg computes it.
PKG_FREEBSD_FINGERPRINT = (
    "b0170035af3acc5f3f3ae1859dc717101b4e6c1d0a794ad554928ca0cbb2f438")

# The DER prefix of an EMSA-PKCS1-v1_5 DigestInfo for SHA-256, i.e.
# SEQUENCE { SEQUENCE { OID 2.16.840.1.101.3.4.2.1, NULL }, OCTET STRING (32) }.
_SHA256_DIGESTINFO = bytes.fromhex("3031300d060960864801650304020105000420")


def _der_tlv(buf, i):
    """One DER tag-length-value at offset `i`, as (tag, value, next_offset).

    Deliberately minimal and deliberately strict about running off the end: a
    lenient parser that returned a short value would recover a wrong modulus
    and then refuse a good signature, which teaches people to skip the check.
    """
    if i + 2 > len(buf):
        raise ValueError("truncated DER")
    tag = buf[i]
    n = buf[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        if k == 0 or k > 4 or i + k > len(buf):
            raise ValueError("unsupported DER length")
        n = int.from_bytes(buf[i:i + k], "big")
        i += k
    if i + n > len(buf):
        raise ValueError("DER value runs past the end of the buffer")
    return tag, buf[i:i + n], i + n


def rsa_pubkey(pem_bytes):
    """(modulus, exponent) out of a PEM SubjectPublicKeyInfo."""
    lines = [l for l in pem_bytes.splitlines() if not l.startswith(b"-----")]
    if not lines:
        raise ValueError("not a PEM public key")
    der = base64.b64decode(b"".join(lines))
    tag, seq, _ = _der_tlv(der, 0)
    if tag != 0x30:
        raise ValueError("SubjectPublicKeyInfo is not a SEQUENCE")
    _algid_tag, _algid, j = _der_tlv(seq, 0)
    tag, bits, _ = _der_tlv(seq, j)
    if tag != 0x03 or not bits or bits[0] != 0:
        raise ValueError("subjectPublicKey is not a whole-octet BIT STRING")
    tag, inner, _ = _der_tlv(bits[1:], 0)
    if tag != 0x30:
        raise ValueError("RSAPublicKey is not a SEQUENCE")
    tag_n, n_bytes, k = _der_tlv(inner, 0)
    tag_e, e_bytes, _ = _der_tlv(inner, k)
    if tag_n != 0x02 or tag_e != 0x02:
        raise ValueError("RSAPublicKey members are not INTEGERs")
    return int.from_bytes(n_bytes, "big"), int.from_bytes(e_bytes, "big")


def rsa_pkcs1v15_sha256_ok(pub_pem, sig, digest):
    """True iff `sig` is a PKCS#1 v1.5 SHA-256 signature over `digest`.

    The recovered block is compared against a RECONSTRUCTED one byte for byte,
    rather than searched for a DigestInfo with `find`.  A search accepts a
    block whose padding is short, or absent, or carries attacker-chosen bytes
    in front -- the classic Bleichenbacher'06 shape -- and it would look
    exactly as green as this does.
    """
    n, e = rsa_pubkey(pub_pem)
    k = (n.bit_length() + 7) // 8
    if len(sig) != k:
        return False
    m = pow(int.from_bytes(sig, "big"), e, n)
    em = m.to_bytes(k, "big")
    di = _SHA256_DIGESTINFO + digest
    pad = k - 3 - len(di)
    if pad < 8:
        return False
    want = b"\x00\x01" + b"\xff" * pad + b"\x00" + di
    return hmac.compare_digest(em, want)


def cmd_verifyindex(argv):
    """verifyindex <packagesite.yaml> <packagesite.yaml.sig> <packagesite.yaml.pub>

    Refuses unless the index is signed by FreeBSD's pinned repository key.
    A refusal, never a warning: the caller's very next act is to derive every
    download URL and every checksum from this file.
    """
    if len(argv) != 3:
        sys.exit("usage: pkgindex.py verifyindex <yaml> <sig> <pub>")
    yaml_path, sig_path, pub_path = argv
    for path in argv:
        if not os.path.isfile(path):
            sys.exit(
                "%s is missing.\n"
                "  `packagesite.pkg` from pkg.freebsd.org carries "
                "packagesite.yaml, .sig and .pub together; an archive without "
                "all three is not one this build will trust. Refusing rather "
                "than falling back to an unverified index." % path)

    pub = open(pub_path, "rb").read()
    got = hashlib.sha256(pub).hexdigest()
    if not hmac.compare_digest(got, PKG_FREEBSD_FINGERPRINT):
        sys.exit(
            "the repository signing key is not the one this build pins.\n"
            "  expected sha256 %s\n"
            "  got      sha256 %s\n"
            "  This means the key ROTATED, not that the check is broken. "
            "Compare the new key against /usr/share/keys/pkg/trusted/ on a "
            "real FreeBSD machine and change PKG_FREEBSD_FINGERPRINT in "
            "pkgindex.py deliberately. Do not answer it by dropping the "
            "check." % (PKG_FREEBSD_FINGERPRINT, got))

    sig = open(sig_path, "rb").read()
    # pkg signs the ASCII HEX of the file's sha256, not the sha256 itself.
    # Measured, both ways, against the live index -- see the note above.
    inner = hashlib.sha256(open(yaml_path, "rb").read()).hexdigest()
    digest = hashlib.sha256(inner.encode("ascii")).digest()
    if not rsa_pkcs1v15_sha256_ok(pub, sig, digest):
        sys.exit(
            "the package index's signature does not verify against the pinned "
            "FreeBSD repository key.\n"
            "  Every download URL and every blake2b checksum in this build is "
            "read out of that file, so a bad signature is not a warning: it "
            "would make 'every checksum verified' mean 'the bytes matched "
            "what the substituted document said they would'.")
    sys.stderr.write(
        "==> index: signature verified against the pinned FreeBSD "
        "repository key (sha256 %s...)%s" % (PKG_FREEBSD_FINGERPRINT[:16],
                                             os.linesep))


# ---------------------------------------------------------------------------
# the shared-library check
# ---------------------------------------------------------------------------
#
# There is no RUN in this build, so nothing ever executes to discover that a
# library is missing; the image builds, pushes and pulls perfectly and fails on
# somebody else's machine at first exec.  Two distinct faults produce that, and
# both are cheap to catch here:
#
#   1. The staged tree and the base image are from DIFFERENT FreeBSD major
#      versions.  Measured, from the real published artefacts: 14.4's base
#      carries `libutil.so.9` and 15.1's carries `libutil.so.10`, and
#      `python3.12` from the FreeBSD:14 repository needs the former.  Cross
#      them and the build still succeeds -- the matrix is what pairs `base`
#      with `abi`, and this is what proves the pairing rather than trusting it.
#
#   2. An `--without` exclusion took a library something kept still needs.
#
# It is an ELF reader rather than a `pkg` metadata comparison on purpose: the
# metadata says what the ports tree believed, and the file says what the
# runtime linker will look for.

_DT_NEEDED, _DT_RPATH, _DT_RUNPATH, _DT_SONAME, _DT_STRTAB = 1, 15, 29, 14, 5


def elf_dynamic(path):
    """(needed, soname, runpath) for an ELF64 file, or None if it is not one.

    Deliberately tiny and deliberately ELF64-only: every FreeBSD architecture
    this image is built for is 64-bit, and a reader that quietly half-handled
    ELF32 would answer "no libraries needed" for a file it could not parse --
    which is the empty result this project refuses everywhere else.
    """
    with open(path, "rb") as fh:
        b = fh.read()
    if len(b) < 64 or b[:4] != b"\x7fELF" or b[4] != 2:
        return None
    end = "<" if b[5] == 1 else ">"
    e_phoff, = struct.unpack_from(end + "Q", b, 32)
    e_phentsize, e_phnum = struct.unpack_from(end + "HH", b, 54)
    loads, dyn = [], None
    for i in range(e_phnum):
        o = e_phoff + i * e_phentsize
        if o + 56 > len(b):
            return None
        p_type, = struct.unpack_from(end + "I", b, o)
        p_offset, p_vaddr = struct.unpack_from(end + "QQ", b, o + 8)
        p_filesz, = struct.unpack_from(end + "Q", b, o + 32)
        if p_type == 1:                                   # PT_LOAD
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == 2:                                 # PT_DYNAMIC
            dyn = (p_offset, p_filesz)
    if dyn is None:
        return ([], None, [])

    def to_off(vaddr):
        for base, off, size in loads:
            if base <= vaddr < base + size:
                return off + (vaddr - base)
        return None

    ents = []
    off, size = dyn
    for o in range(off, min(off + size, len(b) - 15), 16):
        tag, val = struct.unpack_from(end + "qQ", b, o)
        ents.append((tag, val))
        if tag == 0:
            break
    strtab = None
    for tag, val in ents:
        if tag == _DT_STRTAB:
            strtab = to_off(val)
    def string(o):
        if strtab is None:
            return ""
        i = b.index(b"\0", strtab + o)
        return b[strtab + o:i].decode("ascii", "replace")
    needed, soname, runpath = [], None, []
    for tag, val in ents:
        if tag == _DT_NEEDED:
            needed.append(string(val))
        elif tag == _DT_SONAME:
            soname = string(val)
        elif tag in (_DT_RPATH, _DT_RUNPATH):
            runpath.extend(p for p in string(val).split(":") if p)
    return (needed, soname, runpath)


def _walk_elf(root):
    """Every ELF64 file under `root`, as (relative path, dynamic info)."""
    for base, dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(base, f)
            if os.path.islink(p) or not os.path.isfile(p):
                continue
            try:
                info = elf_dynamic(p)
            except (OSError, ValueError, struct.error):
                continue
            if info is not None:
                yield os.path.relpath(p, root), info


def cmd_shlibs(argv):
    """shlibs <staged-rootfs> <base-rootfs>

    Every library the staged tree asks for must be provided by the staged tree
    or by the base image.  Prints what is missing and exits non-zero, naming
    the file that wanted it -- because "libutil.so.9 not found" on its own does
    not say whether the base is wrong or the exclusion list is.
    """
    if len(argv) != 2:
        sys.exit("usage: pkgindex.py shlibs <staged-rootfs> <base-rootfs>")
    staged, base = argv
    for d in (staged, base):
        if not os.path.isdir(d):
            sys.exit("%s is not a directory" % d)

    provided = set()
    for root in (staged, base):
        for rel, (_needed, soname, _rp) in _walk_elf(root):
            name = os.path.basename(rel)
            provided.add(name)
            if soname:
                provided.add(soname)
        # Symlinks are how `libfoo.so.1 -> libfoo.so.1.0` is spelled on disk
        # and `_walk_elf` skips them, so they are collected separately.  A
        # DT_NEEDED naming the symlink would otherwise read as missing.
        for b, _dirs, files in os.walk(root):
            for f in files:
                if os.path.islink(os.path.join(b, f)):
                    provided.add(f)

    missing = {}
    for rel, (needed, _soname, _rp) in _walk_elf(staged):
        for lib in needed:
            if lib not in provided:
                missing.setdefault(lib, []).append(rel)

    if missing:
        sys.stderr.write(
            "These shared libraries are needed by the staged tree and are in "
            "neither it nor the base image:\n")
        for lib in sorted(missing):
            who = missing[lib][:3]
            sys.stderr.write("  %-24s wanted by %s%s\n"
                             % (lib, ", ".join(who),
                                " (+%d more)" % (len(missing[lib]) - 3)
                                if len(missing[lib]) > 3 else ""))
        sys.stderr.write(
            "\nTwo things produce this, and the list above says which:\n"
            "  * a base/ABI mismatch -- 14.x carries libutil.so.9 and 15.x "
            "libutil.so.10, so a FreeBSD:14 package on a 15.x base fails "
            "exactly here;\n"
            "  * an --without exclusion that took a library something kept "
            "still needs.\n")
        return 1
    count = sum(1 for _ in _walk_elf(staged))
    sys.stderr.write("==> shlibs: %d ELF file(s) staged, every DT_NEEDED "
                     "satisfied by the staged tree or the base\n" % count)
    return 0


# ---------------------------------------------------------------------------
# the "did the pruning take something the server imports" check
# ---------------------------------------------------------------------------

def _srv_imports(srv_dir):
    """Every top-level module `server/srv/` imports, derived with `ast`.

    Derived and not written down, for the reason this repository gives every
    time it derives a list: a hand-maintained copy is a second place to forget,
    and the copy is always the one that goes stale.

    A FILE is accepted as well as a directory, and that is the mcp image's
    whole case.  That image contains one module -- `srv/mcp.py`, an HTTP client
    of the read API -- so checking it against the whole package's import list
    would demand `duckdb` be staged, i.e. the check would insist on exactly the
    dependency the image exists to shed.  Pointed at the file it asserts the
    true thing: everything THIS module imports survived the prune.

    THIS IS THE UNION OF BOTH KINDS OF IMPORT, AND THEY ARE NOT ONE QUESTION.
    `_srv_imports_by_kind` is the split; see it for why one of them is a hard
    failure and the other is a decision a component is allowed to make.
    """
    at_import, deferred = _srv_imports_by_kind(srv_dir)
    return sorted(at_import | deferred)


def _srv_imports_by_kind(srv_dir):
    """(imported at import time, imported only inside a function body).

    THE SPLIT EXISTS BECAUSE THE INGEST IMAGE RESTS ON IT, AND THE COMMENT
    THAT ASSERTED IT WAS WRONG.

      `stage-freebsd.sh`'s ingest branch said of `duckdb`: "it is imported
      inside a function, and the check reads module-level imports, which is
      exactly the distinction this component rests on".  It did not read
      module-level imports.  `_srv_imports` walked the whole tree with
      `ast.walk`, which descends into function bodies, so the `import duckdb`
      inside `store.require()` was in the list -- and `stage-freebsd.sh
      ingest`, whose whole point is an image with no engine in it, failed its
      own check.  Measured rather than reasoned: the stage was run against the
      live `FreeBSD:15:amd64` index and refused by name, having already
      downloaded and pruned the tree.

      So the distinction that comment described is now the one the code makes.
      A module imported AT IMPORT TIME must be in the staged tree, always --
      its absence is a `ModuleNotFoundError` before anything serves.  A module
      imported only inside a function body may legitimately be absent, which is
      what `store.require()` exists for: it raises `DuckDBMissing` naming the
      install command, and `serve.py` prints `engine duckdb MISSING -- stream A
      routes will refuse by name`.  But only when the caller SAYS SO on the
      command line, where the decision can be read -- see `cmd_modules`'
      `--deferred-ok`.  An absence nobody declared is still a refusal.

    A CLASS BODY IS IMPORT TIME.  It runs when the module is imported, so an
    import inside one is deferred by nothing; only `def` and `async def` bodies
    are skipped.  A `try`/`if`/`with` at module level is module level too,
    which is exactly the shape `import duckdb` would take if somebody lifted it
    out of the function and guarded it -- and that shape MUST fail here,
    because at that point the interpreter really does need the package at
    startup.
    """
    import ast
    if os.path.isfile(srv_dir):
        paths = [srv_dir]
    else:
        paths = [os.path.join(srv_dir, fn) for fn in sorted(os.listdir(srv_dir))
                 if fn.endswith(".py")]

    def named(node):
        if isinstance(node, ast.Import):
            return [a.name.split(".")[0] for a in node.names]
        if isinstance(node, ast.ImportFrom) and not node.level and node.module:
            return [node.module.split(".")[0]]
        return []

    at_import, deferred = set(), set()

    def walk(node, in_function):
        for child in ast.iter_child_nodes(node):
            (deferred if in_function else at_import).update(named(child))
            walk(child, in_function or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)))

    for path in paths:
        walk(ast.parse(open(path, encoding="utf-8").read()), False)

    at_import.discard("srv")
    deferred.discard("srv")
    # Imported both ways is imported at import time, full stop: the deferred
    # one demands nothing the eager one has not already demanded.
    return at_import, deferred - at_import


def cmd_modules(argv):
    """modules <staged-rootfs> <srv-dir-or-file> [pyver] [--deferred-ok a,b]

    Every module the named source imports must be findable in the staged tree,
    or be built into the interpreter.  The source is `server/srv/` for the two
    door images and `server/srv/mcp.py` alone for the mcp image, which carries
    that one module and must not carry DuckDB.

    This exists because the staging step PRUNES: `lib/python3.12/test` alone is
    132 MiB of a 304 MiB tree and nothing under `srv/` imports it.  Pruning is a
    judgement about somebody else's package, and the failure it can cause --
    `ModuleNotFoundError` on a machine that is not this one -- is exactly the
    shape this project refuses to ship.  So the prune list is checked against
    the import list rather than trusted alongside it.

    `--deferred-ok` IS THE INGEST IMAGE'S WHOLE CASE, AND IT IS DELIBERATELY
    NOT A DEFAULT.

      That image runs the same package as the api with one package fewer:
      `srv/` still contains `import duckdb`, but only inside `store.require()`,
      so nothing on the ship path reaches it.  Named here, that module may be
      missing from the tree.  Not named, it is the ordinary refusal -- because
      an absent import that nobody declared is a `ModuleNotFoundError` waiting
      on somebody else's machine, and the difference between the two is a
      sentence a person wrote, not something this file can infer.

      It refuses itself when it goes stale, which is `--without`'s rule one
      command over: a name that the source does not import at all is a refusal
      (the entry outlived the import), and a name the source imports AT IMPORT
      TIME is a refusal whatever else is true (its absence cannot be survived,
      so declaring it survivable is the one mistake that would publish a broken
      image).  Present in the tree anyway is no error at all: the api stages
      `duckdb` and passes this check with the same argument list.

    THE ONE THING IT CANNOT DERIVE, STATED RATHER THAN GLOSSED: a module
    compiled INTO CPython (`sys`, `time`, `math`, `errno`, `_thread`) has no
    file anywhere, so "not on disk" is not "missing".  The built-in set is read
    from the interpreter running this script -- which is the RUNNER's CPython
    and not FreeBSD's.  That is a real limitation and it is a narrow one: these
    names are built in on every POSIX CPython, the smoke job asserts the
    imports behaviourally on a real FreeBSD, and the direction of a wrong
    answer here is a false alarm rather than a false silence.
    """
    deferred_ok, rest = set(), []
    i = 0
    while i < len(argv):
        if argv[i] == "--deferred-ok":
            i += 1
            if i >= len(argv):
                sys.exit("--deferred-ok needs a comma-separated list")
            deferred_ok = {w for w in argv[i].split(",") if w}
        elif argv[i].startswith("--deferred-ok="):
            deferred_ok = {w for w in argv[i].split("=", 1)[1].split(",") if w}
        else:
            rest.append(argv[i])
        i += 1
    if len(rest) not in (2, 3):
        sys.exit("usage: pkgindex.py modules <staged-rootfs> "
                 "<srv-dir-or-file> [pyver] [--deferred-ok a,b]")
    staged, srv_dir = rest[0], rest[1]
    pyver = rest[2] if len(rest) == 3 else "3.12"
    libdir = os.path.join(staged, "usr", "local", "lib", "python" + pyver)
    if not os.path.isdir(libdir):
        sys.exit("no %s in the staged tree; nothing to check against" % libdir)

    at_import, deferred = _srv_imports_by_kind(srv_dir)

    stale = sorted(n for n in deferred_ok if n not in at_import | deferred)
    if stale:
        sys.exit(
            "--deferred-ok names %s, which %s not imported by %s at all.\n"
            "  A permission for an import that no longer exists is a no-op "
            "that reads as a decision. Either the module was renamed, or the "
            "import went away and this list should lose the entry."
            % (", ".join(stale), "is" if len(stale) == 1 else "are", srv_dir))
    eager = sorted(n for n in deferred_ok if n in at_import)
    if eager:
        sys.exit(
            "--deferred-ok names %s, which %s imported AT IMPORT TIME by "
            "%s.\n"
            "  A module the interpreter loads before anything serves cannot be "
            "absent from the image: that is a ModuleNotFoundError at startup, "
            "not a feature that refuses by name. Either stage the package or "
            "put the import back inside the function that needs it."
            % (", ".join(eager), "is" if len(eager) == 1 else "are", srv_dir))

    builtin = set(sys.builtin_module_names)
    dynload = os.path.join(libdir, "lib-dynload")
    tag = "cpython-" + pyver.replace(".", "")

    missing, how = [], []
    for mod in sorted(at_import | deferred):
        if mod == "duckdb":
            found = os.path.isdir(os.path.join(libdir, "site-packages", "duckdb"))
            where = "site-packages/duckdb"
        elif os.path.isdir(os.path.join(libdir, mod)):
            found, where = True, mod + "/"
        elif os.path.isfile(os.path.join(libdir, mod + ".py")):
            found, where = True, mod + ".py"
        elif os.path.isfile(os.path.join(dynload, "%s.%s.so" % (mod, tag))):
            found, where = True, "lib-dynload/%s.%s.so" % (mod, tag)
        elif mod in builtin:
            found, where = True, "built into the interpreter"
        else:
            found, where = False, None
        if found:
            how.append("  %-12s %s" % (mod, where))
        elif mod in deferred_ok:
            # Named, never silent: an image deliberately short a package is a
            # decision, and a decision that leaves no line in the build log is
            # indistinguishable from an oversight when somebody reads it back.
            how.append("  %-12s ABSENT, and declared so: imported only inside "
                       "a function body" % mod)
        else:
            missing.append(mod)

    sys.stderr.write("==> modules %s imports:"
                     % os.path.basename(srv_dir.rstrip("/")) + os.linesep)
    for line in how:
        sys.stderr.write(line + os.linesep)
    if missing:
        sys.stderr.write(
            "These are imported by %s and are in neither the staged "
            "tree nor the interpreter's built-ins: " % srv_dir
            + ", ".join(missing) +
            os.linesep +
            "  Either the prune list took one of them, or the package set is "
            "short a package. Both produce a ModuleNotFoundError on somebody "
            "else's machine and neither is visible in a built image." +
            os.linesep)
        return 1
    return 0


# `file(1)` is what the reference uses for this and it would work here too, but
# there are 95 ELF files in this tree rather than one, and the question is not
# "is this binary right" but "is EVERY file in the staged tree the architecture
# the matrix asked for".  A single stray file from the wrong repository is the
# failure to catch, and it is the one a spot check on the main binary misses.
_EM = {62: "amd64", 183: "arm64"}


def cmd_arch(argv):
    """arch <staged-rootfs> <amd64|arm64>

    Every ELF file in the staged tree must be the named architecture.  A
    mismatch produces an image that builds, pushes and pulls, and fails at the
    first exec on somebody else's machine -- which is why it is checked here
    and not left to be discovered there.
    """
    if len(argv) != 2:
        sys.exit("usage: pkgindex.py arch <staged-rootfs> <amd64|arm64>")
    root, want = argv
    if want not in ("amd64", "arm64"):
        sys.exit("unknown architecture %r; expected amd64 or arm64" % want)
    if not os.path.isdir(root):
        sys.exit("%s is not a directory" % root)

    counts, wrong = {}, []
    for rel, _info in _walk_elf(root):
        with open(os.path.join(root, rel), "rb") as fh:
            head = fh.read(20)
        end = "<" if head[5] == 1 else ">"
        machine, = struct.unpack_from(end + "H", head, 18)
        name = _EM.get(machine, "e_machine=%d" % machine)
        counts[name] = counts.get(name, 0) + 1
        if name != want:
            wrong.append((rel, name))

    if not counts:
        sys.exit("no ELF files at all under %s; nothing was staged" % root)
    sys.stderr.write("==> arch: " + ", ".join(
        "%s x%d" % (k, counts[k]) for k in sorted(counts)) + os.linesep)
    if wrong:
        sys.stderr.write("Staged for %s, but these are not:" % want + os.linesep)
        for rel, name in wrong[:10]:
            sys.stderr.write("  %-60s %s" % (rel, name) + os.linesep)
        if len(wrong) > 10:
            sys.stderr.write("  ... and %d more" % (len(wrong) - 10) + os.linesep)
        return 1
    return 0


def main(argv):
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    if cmd == "resolve":
        return cmd_resolve(rest) or 0
    if cmd == "verify":
        return cmd_verify(rest) or 0
    if cmd == "verifyindex":
        return cmd_verifyindex(rest) or 0
    if cmd == "shlibs":
        return cmd_shlibs(rest)
    if cmd == "modules":
        return cmd_modules(rest)
    if cmd == "arch":
        return cmd_arch(rest)
    sys.exit("unknown command %r; expected resolve, verify, verifyindex, "
             "shlibs, modules or arch" % cmd)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
