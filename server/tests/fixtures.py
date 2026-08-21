"""Fixtures for the reconciliation core, and the provenance of every one.

**Everything here starts as bytes captured off a real machine.**  Nothing in
this file generates a record shape.  That rule is not fastidiousness: a
synthetic generator was once written in this repository to match the
collector's own filters, so the generator and the filter agreed with each
other while both disagreed with Claude Code, and two suites stayed green over a
wrong event name that dropped every record and an allow-list that deleted every
user tag.  A generator can prove a socket is listening.  It can never prove a
shape, a field name or a label is handled.

So there are exactly three provenance classes, and every fixture declares
which it is:

  REAL      parsed byte for byte out of `usage/tests/fixtures/`.  No field is
            touched.  Most assertions in the suite are against these.

  DERIVED   a REAL record with NAMED fields overridden, nothing else.  This is
            how a second machine, a skewed clock and a 90%->2% rollover are
            built, because none of them exists in any capture: one macOS
            workstation, one clock, and no 7-day roll.  Every derivation is
            recorded in `DERIVATIONS` with its base and the exact keys it
            changed, and a test prints that ledger, so a derivation cannot
            quietly become "real data" in someone's memory.

  AUTHORED  no captured ancestor at all.  There are exactly two: the batch
            manifest and the stream-C attestation.  Both are designed in
            `srv/wire.py` and nothing on any machine has ever emitted one --
            there is no attestation in this repository's fixtures, no capture
            of one, and no code outside `server/` that writes one.  A test
            asserts that this set is exactly {manifest, attestation}, so the
            day something else is authored, it has to be admitted here first.

The join between the two streams is itself real, and worth stating because it
looks like an assumption and is not.  `real-api-request.jsonl` and
`real-samples.jsonl` were captured in the same session: the ledger's `account`
labels are exactly the samples' `account` labels (asserted), the samples carry
both the label and the `account_uuid`, and every ledger timestamp falls inside
the 5-hour window the samples name.  D9's stamp is therefore applied here from
the real mapping rather than invented.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
ROOT = os.path.dirname(SERVER)
USAGE = os.path.join(ROOT, "usage")
REAL = os.path.join(USAGE, "tests", "fixtures")

# `srv` is imported from SRV_DIR so the mutation harness can point the whole
# suite at a patched copy without editing a line of it.
SRV_DIR = os.environ.get("SRV_DIR") or SERVER
if SRV_DIR not in sys.path:
    sys.path.insert(0, SRV_DIR)
if USAGE not in sys.path:
    sys.path.insert(0, USAGE)


# --------------------------------------------------------------- provenance --

DERIVATIONS = []          # (label, base_description, (changed keys...))
AUTHORED = []             # (kind, why it has no captured ancestor)


def derive(label, base_desc, rec, **over):
    """A REAL record with named fields overridden.  Recorded, not silent."""
    out = dict(rec)
    out.update(over)
    DERIVATIONS.append((label, base_desc, tuple(sorted(over))))
    return out


def author(kind, why):
    AUTHORED.append((kind, why))


# ------------------------------------------------------------------- real ----

def _lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(l) for l in fh if l.strip()]


def replay_samples():
    """65 REAL stream-B samples: one account, one machine, 33 hours.

    The v1 shape -- 15 keys, and no `schema` key at all.
    """
    return _lines(os.path.join(REAL, "replay-real.jsonl"))


def real_samples():
    """14 REAL stream-B samples in the current 22-key `schema: 2` shape.

    Three accounts, one machine, captured beside the stream-A file below.
    """
    return _lines(os.path.join(REAL, "real-samples.jsonl"))


def real_payload_lines():
    """The 7 REAL raw OTLP lines exactly as `recv/otlp-recv` wrote them."""
    return _lines(os.path.join(REAL, "real-api-request.jsonl"))


def real_rows():
    """8 REAL stream-A ledger rows, normalised by the shipping machine's own
    normaliser.

    `cu.otlp.rows_from_payload` is imported from `usage/` rather than copied:
    the server must read what the machine actually writes, and a second copy of
    the column list here would be a place for the two to drift apart in
    silence.  A test compares `wire.LEDGER_FIELDS` against these keys for the
    same reason.
    """
    from cu import otlp
    rows = []
    for payload in real_payload_lines():
        rows.extend(otlp.rows_from_payload(payload))
    return rows


def account_uuid_by_label():
    """The REAL label -> UUID mapping, read out of the captured samples."""
    return {s["account"]: s["account_uuid"] for s in real_samples()}


def stamped_rows():
    """The 8 real rows with D9's `account_uuid` stamp applied.

    The stamp is what the shipper adds on the machine, where `.claude.json` is
    readable.  Stream A has no UUID column of its own and its `account` is a
    label `--tag account=` can overwrite, so without the stamp a row cannot be
    placed against an account at all -- and joining by label or by email on the
    server would be exactly the guess "never sum across accounts" exists to
    prevent.
    """
    by_label = account_uuid_by_label()
    out = []
    for r in real_rows():
        row = dict(r)
        row["account_uuid"] = by_label[r["account"]]
        out.append(row)
    return out


# UUIDs, read from the capture rather than typed.
def uuid_of(label):
    return account_uuid_by_label()[label]


# ------------------------------------------------------- bytes on the wire ---
#
# The door stores what a shipper POSTs, byte for byte, so its fixtures have to
# be BYTES rather than parsed records -- a `json.dumps` here would test the
# door against this file's idea of a line instead of against a machine's.
# Stream B is read straight out of the captured file.  Stream A has no captured
# `ledger.jsonl` (the capture is the OTLP payloads that produce it), so the
# real rows are put through `cu.ledger.append` -- the writer that actually
# writes that file -- and then through `cu.ship.stamp`, which is the exact
# transformation the shipper applies on the way out.  Nothing here invents a
# serialisation.

def real_sample_lines(label=None):
    """REAL stream-B lines, byte for byte, optionally for one account.

    Filtered on `account_uuid` and nothing else, which is `cu.ship._select`'s
    own rule for stream B.
    """
    with open(os.path.join(REAL, "real-samples.jsonl"), "rb") as fh:
        lines = [ln.rstrip(b"\n") for ln in fh if ln.strip()]
    if label is None:
        return lines
    want = uuid_of(label)
    return [ln for ln in lines if json.loads(ln).get("account_uuid") == want]


def wire_lines(label):
    """(client-file bytes, [stamped line]) for one account's stream A.

    The first value is what the shipper's offset counts -- the bytes of its own
    `ledger.jsonl` -- and the second is what goes in the body.  They differ,
    because the stamp is added after the file is read; a manifest's `from`/`to`
    describe the file, never the body.
    """
    import shutil
    import tempfile

    from cu import ledger, ship
    want = uuid_of(label)
    rows = [r for r in real_rows() if r["account"] == label]
    tmp = tempfile.mkdtemp(prefix="ledger-")
    try:
        path = os.path.join(tmp, "ledger.jsonl")
        ledger.append(path, rows)
        with open(path, "rb") as fh:
            raw = fh.read()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return len(raw), [ship.stamp(ln, want)
                      for ln in raw.split(b"\n") if ln.strip()]


REPLAY_ACCOUNT = "00000000-0000-4000-8000-000000000001"

# Every number below was read off the fixtures before a line of the suite ran;
# the reading is shown so a changed fixture fails loudly instead of quietly
# re-baselining the expectations.
#
#   $ jq -r .five_hour_resets_at usage/tests/fixtures/replay-real.jsonl | sort -u
REPLAY_5H_RESETS = (1786388400, 1786406400, 1786424400, 1786471800, 1786489800)
REPLAY_5H_PEAKS = (6.0, 19.0, 15.0, 23.0, 0.0)
REPLAY_5H_COUNTS = (4, 18, 22, 18, 2)
REPLAY_5H_GAPS_TO_CLOSE = (2469, 395, 4989, 39, 17184)
REPLAY_7D_RESETS = 1786564800
REPLAY_7D_PEAK = 76.0
REPLAY_7D_TROUGH = 64.0

# The one record in the whole capture with an unusable window identity:
# `five_hour_resets_at: 1`, every other field null, at a moment when the true
# value was 14.  It is index 16.
REPLAY_BAD_EPOCH_INDEX = 16

# Snapshot staleness, proven five times over.  Index 49 is the worst:
# ts=1786459730 naming resets_at=1786424400, at least 35 330 s (9.81 h) old,
# and its `ts` falls inside the NEXT window's [start, end).  It is the whole
# proof that membership is by `resets_at` and never by `ts`.
REPLAY_STALEST_INDEX = 49
REPLAY_STALEST_TS = 1786459730
REPLAY_STALEST_NAMES = 1786424400
REPLAY_STALEST_LATENESS = 35330

# The joint capture: three accounts, one 5-hour window each.
JOINT_5H_RESETS = {"agent": 1786598400, "alpha": 1786598400, "beta": 1786586400}
JOINT_5H_PEAKS = {"agent": 1.0, "alpha": 10.0, "beta": 49.0}

# After every joint window has closed, inside GRACE.
NOW_JOINT = 1786600000
# After every replay window has closed, and past GRACE for all of them.
NOW_REPLAY = 1786800000


# ------------------------------------------------------------- the wire ------

def manifest(stream, records, host="darwin", machine_id="m-3f9a1c02",
             offset=0, ship=1, to=None, count=None, ident=None, reset=None):
    """AUTHORED: the batch manifest is the only record a shipper writes itself.

    `to`/`count`/`ident` are overridable because the door's tests need a range
    that describes real bytes -- the offsets it stores and echoes in its ack are
    the client's own byte positions, and `offset + 1` cannot stand in for one.

    `reset` is OMITTED unless a test names it, which is the shape a shipper
    written before that key existed sends: the door has to keep taking those,
    or adding the key would jam every stream of every client that has not
    upgraded.
    """
    author("manifest", "the shipper authors it; no capture of one exists")
    if reset is not None:
        return dict(manifest(stream, records, host=host, machine_id=machine_id,
                             offset=offset, ship=ship, to=to, count=count,
                             ident=ident), reset=reset)
    return {
        "ship": ship, "stream": stream, "file": stream + ".jsonl",
        "ident": ident or {"dev": 16777232, "ino": 88104213,
                           "head": "3f9a1c04b7e28d51"},
        "from": offset, "to": offset + 1 if to is None else to,
        "count": len(records) if count is None else count,
        "sent_at": 1786748000.12, "agent": "cu-ship/1",
        "host": host, "machine_id": machine_id,
    }


def batch(stream, records, tenant, host="darwin", machine_id="m-3f9a1c02"):
    from srv.ingest import Batch
    return Batch(manifest(stream, records, host=host, machine_id=machine_id),
                 records, tenant)


def attestation(account_uuid, kind, resets_at, host, spans, machine_id=None,
                shipped=None, emitted_at=None):
    """AUTHORED: stream C.

    Nothing has ever emitted an attestation.  There is none in any fixture in
    this repository, no capture of one on any machine, and no code outside
    `server/` that writes one.  `up_spans` copies the shape of
    `cu.collector.up_spans` -- `[start, stop, clean]`, with `clean` the
    *observed* graceful-stop flag -- which is real; an attestation carrying
    them has never crossed a wire.
    """
    author("attestation",
           "stream C is designed in srv/wire.py; nothing emits one yet")
    return {
        "schema": 1, "kind": "window_close", "account_uuid": account_uuid,
        "window": kind, "resets_at": resets_at, "host": host,
        "machine_id": machine_id or ("m-" + host),
        "up_spans": [list(s) for s in spans],
        "shipped": shipped or {"A": 0, "B": 0},
        "recv_events": 0, "recv_unknown_attributes": 0,
        "ledger_offset": 0, "samples_offset": 0,
        "emitted_at": emitted_at or (resets_at + 300),
    }
