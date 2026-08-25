#!/usr/bin/env python3
"""Selftests for the reconciliation core.

Run:  python3 server/tests/test_all.py
Then: python3 server/tests/mutate.py     -- and read the matrix it prints.

Every fixture here comes from `usage/tests/fixtures/`, captured off a real
machine.  Where a scenario does not exist in any capture -- a second machine, a
skewed clock, a 90%->2% rollover, an attestation -- it is DERIVED from a real
record by named field overrides, or AUTHORED, and `fixtures.py` records which.
A test at the end prints that ledger and asserts the authored set is exactly
{manifest, attestation}.  The rule behind all of it: a value written from an
assumption about a payload, with green tests and nobody told, is this project's
cardinal sin and has been committed six times.

The numbers asserted below were read off the fixtures first and the reading is
shown, so a changed fixture fails loudly rather than quietly re-baselining what
the code is allowed to believe.

An unmutated suite is decoration.  `mutate.py` breaks each behaviour in turn
and requires a named failure; if you add a behaviour here, add its mutation
there.
"""

import contextlib
import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fixtures as fx                                          # noqa: E402

from srv import attribute, coverage, ingest, query, reconcile, window, wire  # noqa: E402,E501
from srv import api, duck, mcp, serve                               # noqa: E402
from srv import store as duckstore                             # noqa: E402

PASS = FAIL = 0
FAILURES = []


def check(name, got, want, tol=None):
    global PASS, FAIL
    if tol is not None:
        ok = got is not None and abs(got - want) <= tol
    else:
        ok = got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


def check_true(name, cond):
    check(name, bool(cond), True)


def fail(name, detail=None):
    """Record a failure that has no got/want pair to compare.

    Some refusals are asserted by their ABSENCE of an outcome -- "serve() must
    not bind a port" -- and there is no value to hold up beside an expected
    one.  Routing those through `check(name, False, True)` prints `got: False
    want: True`, which says nothing about what happened; this prints the
    sentence instead.
    """
    global FAIL
    FAIL += 1
    FAILURES.append(name if detail is None else "%s\n     %s" % (name, detail))


def canon(obj):
    """Deterministic text for a structure whose dict keys may be None.

    `by[dimension]` is keyed by the value a request carried, and a request that
    carried none lands in the None bucket -- deliberately, because dropping it
    or spreading it over the other buckets both move weight onto labels the
    request does not have.  `json.dumps(sort_keys=True)` cannot sort those, so
    keys are stringified for comparison only.
    """
    if isinstance(obj, dict):
        return {repr(k): canon(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [canon(v) for v in obj]
    if isinstance(obj, (str, int, float)) or obj is None:
        return obj
    return repr(obj)


def canon_json(obj):
    return json.dumps(canon(obj), sort_keys=True)


def check_raises(name, exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc:
        check(name, True, True)
        return
    except Exception as e:                        # noqa: BLE001
        check(name, "%s: %s" % (type(e).__name__, e), exc.__name__)
        return
    check(name, "no exception", exc.__name__)


# ------------------------------------------------------------- shorthands ----

def joint(now=fx.NOW_JOINT, samples=None, rows=None, atts=(), rate=None):
    """The three-account joint capture, reconciled."""
    return reconcile.reconcile(
        fx.real_samples() if samples is None else samples,
        fx.stamped_rows() if rows is None else rows,
        list(atts), now, rate=rate)


def acc(rep, label):
    return rep.accounts[fx.uuid_of(label)]


def win(report_account, kind, resets_at):
    for w in report_account.windows:
        if w.kind == kind and w.resets_at == resets_at:
            return w
    raise AssertionError("no %s window %s" % (kind, resets_at))


def att_of(report_account, kind, resets_at):
    for a in report_account.attributions:
        if a.kind == kind and a.resets_at == resets_at:
            return a
    raise AssertionError("no attribution for %s %s" % (kind, resets_at))


# ============================================ 0. FIXTURE PROVENANCE ==========
#
# These come first because every other assertion in the file rests on them.
# If the fixtures are not what this suite thinks they are, nothing below means
# anything -- which is exactly the failure mode a generator written to match
# its own parser produces.

def test_fixtures_are_the_two_real_stream_b_shapes():
    v1 = fx.replay_samples()
    v2 = fx.real_samples()
    check("fixture: replay-real.jsonl is 65 real samples", len(v1), 65)
    check("fixture: real-samples.jsonl is 14 real samples", len(v2), 14)
    check("fixture: the v1 shape is 15 keys", len(v1[0]), 15)
    check("fixture: the v2 shape is 22 keys", len(v2[0]), 22)
    check_true("fixture: v1 carries no `schema` key at all",
               all("schema" not in r for r in v1))
    check_true("fixture: v2 states schema 2",
               all(r["schema"] == 2 for r in v2))
    # The whole reason `ABSENT is not None` exists: v1 is not v2-with-nulls,
    # it is a record written before seven columns existed.
    check_true("fixture: v1's keys are a strict subset of v2's",
               set(v1[0]) < set(v2[0]))


def test_fixture_field_lists_are_derived_not_hardcoded():
    # LEDGER_FIELDS is compared against what the shipping machine's own
    # normaliser actually produces, imported from usage/.  A hand-kept copy
    # here would be a fourth place to forget, which is how six fields were
    # dropped in silence.
    from cu import ledger as cu_ledger, otlp as cu_otlp
    rows = fx.real_rows()
    check("wire: LEDGER_FIELDS is exactly cu.otlp's real row",
          sorted(wire.LEDGER_FIELDS), sorted(rows[0].keys()))
    check("wire: SCHEMA_LEDGER is cu.otlp.SCHEMA", wire.SCHEMA_LEDGER,
          cu_otlp.SCHEMA)
    check("wire: SAMPLE_FIELDS_V1 is the real v1 file's shape",
          sorted(wire.SAMPLE_FIELDS_V1), sorted(fx.replay_samples()[0].keys()))
    check("wire: SAMPLE_FIELDS_V2 is the real v2 file's shape",
          sorted(wire.SAMPLE_FIELDS_V2), sorted(fx.real_samples()[0].keys()))
    # The fingerprint is repeated in srv/ so the package stands alone; drift
    # between the two copies must be a failure, not a discovery.
    for r in rows:
        if wire.ledger_fingerprint(r) != cu_ledger.fingerprint(r):
            check("wire: ledger_fingerprint agrees with cu.ledger", False, True)
            return
    check("wire: ledger_fingerprint agrees with cu.ledger over all 8 real rows",
          True, True)


def test_the_two_streams_really_were_captured_together():
    # D9's stamp is applied from this mapping.  If the two files were not one
    # capture, the stamp would be a guess -- and a guessed account is the one
    # thing "never sum across accounts" cannot survive.
    rows = fx.real_rows()
    labels_a = {r["account"] for r in rows}
    labels_b = {s["account"] for s in fx.real_samples()}
    check("fixture: stream A and stream B name the same three accounts",
          sorted(labels_a), sorted(labels_b))
    check("fixture: the labels are the captured ones",
          sorted(labels_a), ["agent", "alpha", "beta"])
    # Every ledger row lands inside the 5-hour window its account's samples
    # name; that is what makes the joint fixture a joint fixture.
    by_label = {s["account"]: s["five_hour_resets_at"] for s in fx.real_samples()}
    inside = all(by_label[r["account"]] - window.L5 <= r["ts"]
                 < by_label[r["account"]] for r in rows)
    check_true("fixture: every real request falls inside its account's real "
               "5-hour window", inside)


# ================================================= 1. PURITY (static) ========

FORBIDDEN_IMPORTS = ("os", "io", "socket", "sqlite3", "urllib", "http",
                     "subprocess", "pathlib", "time", "datetime", "random",
                     "threading", "shutil", "tempfile", "pickle", "csv",
                     # The list was stdlib-only, which left this check blind to
                     # the one import the storage pass introduced: a module
                     # could `import duckdb` and stay "pure" here.  DuckDB is
                     # permitted in ONE place, and `duck.py` -- which builds
                     # SQL strings and opens nothing -- is deliberately not it.
                     "duckdb")

# The door is the one module that holds the socket and the bytes, so it is
# named here rather than hidden from the check -- and the set is ASSERTED, not
# merely subtracted, so a second impure module has to be admitted in this file
# before it can exist.
# `store.py` is the second and last: it opens a DuckDB connection and reads
# the store's files.  Admitted here, in the suite, before it can exist -- which
# is the whole point of asserting the set rather than subtracting from it.
IMPURE = ("mcp.py", "serve.py", "store.py")
# mcp.py is the third, and it is admitted here rather than hidden: it is an
# HTTP CLIENT of the read API, which is the whole of its authority -- it
# cannot reach the store's files and cannot see an account its reader token
# excludes. Handing it the DuckDB handle would have been fewer moving parts
# and a privilege escalation, since an agent with a database handle is not
# bounded by anything the operator configured.


def test_the_core_is_pure():
    """No I/O, no clock, anywhere in `srv/` except the door.

    Purity here is not tidiness.  It is what lets the storage decision stay
    open, what lets the algorithm change without a migration, and what makes a
    week-late shipment retroactively correct a window that closed days ago:
    every report is re-derived from every record, every time.
    """
    bad, saw = [], []
    for fn in sorted(os.listdir(fx.SRV_DIR + "/srv")):
        if not fn.endswith(".py"):
            continue
        src = open(os.path.join(fx.SRV_DIR, "srv", fn), encoding="utf-8").read()
        hits = []
        for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)",
                             src, re.M):
            top = m.group(1).split(".")[0]
            if top in FORBIDDEN_IMPORTS:
                hits.append(top)
        if hits:
            saw.append(fn)
        if hits and fn not in IMPURE:
            bad.append("%s imports %s" % (fn, ", ".join(sorted(set(hits)))))
    check("purity: srv/ imports nothing that touches a file, a socket or a "
          "clock", bad, [])
    check("purity: the impure set is exactly the door", tuple(saw), IMPURE)


def test_importing_the_core_does_not_import_the_door():
    """`srv/__init__.py` must not name `serve`.

    A pure consumer that writes `import srv` would otherwise pull in
    `http.server`, `fcntl` and a clock through the package's own front door,
    and the purity above would be a statement about files rather than about
    what actually gets loaded.
    """
    src = open(os.path.join(fx.SRV_DIR, "srv", "__init__.py"),
               encoding="utf-8").read()
    named = [ln.strip() for ln in src.splitlines()
             if re.match(r"^\s*(import|from)\s", ln) and "serve" in ln]
    check("purity: srv/__init__ does not import the door", named, [])


def test_now_is_an_argument_and_the_report_is_re_derivable():
    check("purity: two runs over the same records are identical",
          canon_json(joint().as_dict()), canon_json(joint().as_dict()))


# ============================================ 2. THE WIRE FORMAT =============

def test_schema_version_is_on_every_record():
    v1 = fx.replay_samples()[0]
    v2 = fx.real_samples()[0]
    row = fx.stamped_rows()[0]
    check("wire: a v1 sample with no key reads as schema 1",
          wire.schema_of(v1, wire.STREAM_SAMPLES), 1)
    check("wire: a v2 sample states schema 2",
          wire.schema_of(v2, wire.STREAM_SAMPLES), 2)
    check("wire: a ledger row states schema 1",
          wire.schema_of(row, wire.STREAM_LEDGER), 1)
    # Stream A has no such default: a row with no schema is refused, because
    # unlike stream B there are no records on disk in that shape to lose.
    noschema = dict(row)
    del noschema["schema"]
    check("wire: a ledger row with no schema is refused, not defaulted",
          wire.check(noschema, wire.STREAM_LEDGER, fx.NOW_JOINT),
          (False, "no-schema"))


def test_absent_is_not_null():
    """A field added later must be distinguishable from a field that was absent.

    This is the difference the whole `ABSENT` sentinel exists for, and it is
    unrecoverable once lost: write None for both and no later pass can tell
    them apart.
    """
    v1 = fx.replay_samples()[0]
    v2 = fx.real_samples()[0]
    check("wire: ABSENT is not None", wire.ABSENT is None, False)
    check("wire: a v1 record is ABSENT on `agent`, not null",
          wire.get(v1, "agent") is wire.ABSENT, True)
    check("wire: a v2 record's `agent` is a real reading of null",
          (wire.get(v2, "agent") is wire.ABSENT, v2["agent"]), (False, None))
    # The seven columns v2 added.
    missing = wire.absent_fields(v1, wire.STREAM_SAMPLES, 1)
    check("wire: absent_fields names exactly the seven later columns",
          sorted(missing),
          sorted(set(wire.SAMPLE_FIELDS_V2) - set(wire.SAMPLE_FIELDS_V1)))
    check("wire: and nothing is missing from a current-shape record",
          wire.absent_fields(v2, wire.STREAM_SAMPLES, 2), ())


def test_value_guards_mirror_the_recorders():
    now = fx.NOW_REPLAY
    bad = fx.replay_samples()[fx.REPLAY_BAD_EPOCH_INDEX]
    check("wire: the real `resets_at: 1` record is below the epoch floor",
          wire.valid_epoch(bad["five_hour_resets_at"], now),
          (False, "epoch-below-floor"))
    check("wire: a millisecond epoch is beyond the horizon",
          wire.valid_epoch(1786598400000, now)[1], "epoch-beyond-horizon")
    check("wire: a real epoch passes",
          wire.valid_epoch(1786598400, now), (True, None))
    # `used_percentage: 1e30` stored as a high-water mark nothing can beat is
    # how a window freezes for good, with no self-healing path.
    check("wire: a 1e30 percentage is refused", wire.valid_pct(1e30)[1],
          "pct-too-large")
    check("wire: a negative percentage is refused", wire.valid_pct(-1.0)[1],
          "pct-negative")
    check("wire: 7.000000000000001 -- the real float -- passes",
          wire.valid_pct(7.000000000000001), (True, None))
    check("wire: NaN is refused", wire.valid_pct(float("nan"))[1],
          "pct-not-finite")


def test_check_refuses_by_name():
    row = fx.stamped_rows()[0]
    smp = fx.real_samples()[0]
    unstamped = dict(row)
    del unstamped["account_uuid"]
    check("wire: an unstamped ledger row is refused (D9)",
          wire.check(unstamped, wire.STREAM_LEDGER, fx.NOW_JOINT),
          (False, "no-account-uuid"))
    check("wire: a sample posted as a ledger batch is refused",
          wire.check(smp, wire.STREAM_LEDGER, fx.NOW_JOINT),
          (False, "stream-mismatch"))
    check("wire: an unknown ledger schema is refused",
          wire.check(dict(row, schema=99), wire.STREAM_LEDGER, fx.NOW_JOINT),
          (False, "schema-unsupported"))
    check("wire: a ledger row with no request_id is unclassifiable",
          wire.check(dict((k, v) for k, v in row.items() if k != "request_id"),
                     wire.STREAM_LEDGER, fx.NOW_JOINT)[1], "unclassifiable")


def test_manifest_is_checked_before_anything_is_placed():
    recs = fx.stamped_rows()
    m = fx.manifest(wire.STREAM_LEDGER, recs)
    check("wire: a well-formed manifest passes", wire.manifest_check(m),
          (True, None))
    check("wire: an unknown ship version is refused",
          wire.manifest_check(dict(m, ship=99))[1], "ship-version-unsupported")
    check("wire: an unknown stream is refused",
          wire.manifest_check(dict(m, stream="prompts"))[1], "unknown-stream")
    check("wire: an inverted byte range is refused",
          wire.manifest_check(dict(m, **{"from": 10, "to": 2}))[1],
          "manifest-range-inverted")
    # The host is required and is NOT in the decided example line, because
    # stream B's v1 shape has no host column of its own: without a host on the
    # envelope the agreed idempotency key cannot be formed for the 65 records
    # that actually exist.
    check("wire: a manifest with no host is refused",
          wire.manifest_check(dict(m, host=""))[1], "manifest-no-host")


def test_the_stream_b_key_is_the_whole_record_plus_the_host():
    """Why not `(account, ts, five_hour_pct, seven_day_pct)`.

    **Be exact about the evidence here, because it is easy to overstate.**
    That tuple does NOT collapse anything in the captured data: all 65 replay
    records and all 14 current-shape records have distinct tuples.  What the
    capture proves is the near miss -- rows 0 and 1 are two DIFFERENT
    observations, from two different sessions, at the identical second for the
    identical account, and they survive the tuple only because their
    percentages happened to differ by one point.

    What the capture proves outright is the other half: stream B's v1 shape has
    no `host` column, so two machines reporting the same snapshot emit
    byte-identical lines.  The tuple has no host in it either, so it collapses
    them -- erasing the evidence that two machines were up, which is the single
    fact the whole server exists to establish.
    """
    v1 = fx.replay_samples()
    a, b = v1[0], v1[1]
    check("fixture: rows 0 and 1 are one second, one account, two sessions",
          (a["ts"], b["ts"], a["session_id"] == b["session_id"]),
          (1786381901, 1786381901, False))
    check("fixture: they survive the tuple key only by differing in pct",
          (a["five_hour_pct"], b["five_hour_pct"]), (5, 4))
    tuple_key = lambda r: (r["account_uuid"], r["ts"], r["five_hour_pct"],
                           r["seven_day_pct"])                  # noqa: E731
    check("fixture: so the tuple key collapses nothing in the capture -- "
          "the claim that it does is not supported and is not made",
          len({tuple_key(r) for r in v1}), 65)

    # DERIVED: the same real v1 record as seen by a second machine.  In the v1
    # shape there is nothing to derive -- the record is byte-identical -- so the
    # envelope is the only place the second machine exists.
    fx.derive("second-machine-v1-line", "real replay row 0 (no host column)",
              a)
    check("wire: two machines' identical v1 lines collapse under the tuple key",
          tuple_key(a) == tuple_key(dict(a)), True)
    check("wire: and stay two records under the agreed key",
          wire.sample_key(a, "darwin") != wire.sample_key(a, "linux-box"), True)
    check("wire: the agreed key keeps all 65 real records",
          len({wire.sample_key(r, "darwin") for r in v1}), 65)


# ================================================= 3. INGEST AND DEDUPE ======

def _joint_batches():
    rows = fx.stamped_rows()
    smps = fx.real_samples()
    out = []
    for label in ("agent", "alpha", "beta"):
        uuid = fx.uuid_of(label)
        out.append(fx.batch(wire.STREAM_LEDGER,
                            [r for r in rows if r["account_uuid"] == uuid],
                            uuid))
        out.append(fx.batch(wire.STREAM_SAMPLES,
                            [s for s in smps if s["account_uuid"] == uuid],
                            uuid))
    return out


def test_ingest_places_the_real_capture():
    ing = ingest.ingest(_joint_batches(), fx.NOW_JOINT)
    check("ingest: all 8 real ledger rows accepted",
          ing.counts["ledger_accepted"], 8)
    check("ingest: all 14 real samples accepted",
          ing.counts["samples_accepted"], 14)
    check("ingest: nothing refused", ing.counts["refused"], 0)
    check("ingest: no batch refused", ing.counts["batches_refused"], 0)
    check("ingest: three accounts", len(ing.accounts()), 3)
    check("ingest: schemas are counted per stream",
          ing.schemas[wire.STREAM_SAMPLES], {2: 14})


def test_re_ingesting_the_same_batch_is_a_no_op():
    """Not 'nearly'.  A doubled stream-A row doubles attributed weight and
    therefore SHRINKS the residual -- the one number here nobody would
    question."""
    once = ingest.ingest(_joint_batches(), fx.NOW_JOINT)
    twice = ingest.ingest(_joint_batches() + _joint_batches(), fx.NOW_JOINT)
    check("ingest: re-ingest accepts nothing new (stream A)",
          twice.counts["ledger_accepted"], once.counts["ledger_accepted"])
    check("ingest: re-ingest accepts nothing new (stream B)",
          twice.counts["samples_accepted"], once.counts["samples_accepted"])
    check("ingest: the second pass is counted as duplicates, not silence",
          (twice.counts["ledger_duplicates"], twice.counts["samples_duplicates"]),
          (8, 14))
    check("ingest: and never as collisions", twice.counts["ledger_collisions"], 0)
    check("ingest: the ledger collection is identical",
          sorted(once.ledger), sorted(twice.ledger))
    check("ingest: the sample collection is identical",
          sorted(once.samples), sorted(twice.samples))
    # The property that actually matters: the interpretation is unchanged.
    a = reconcile.reconcile_ingest(once, fx.NOW_JOINT).as_dict()
    b = reconcile.reconcile_ingest(twice, fx.NOW_JOINT).as_dict()
    del a["counts"], b["counts"]        # duplicate counts differ, by design
    check("ingest: the reconciliation over a re-ingest is byte-identical",
          canon_json(a), canon_json(b))


def test_a_collision_is_reported_and_never_repaired():
    """Two different requests wearing one request_id.

    DERIVED: two real rows, the second re-stamped with the first's id.  The
    hash is never widened to fix this -- that re-identifies every row already
    stored and doubles the ledger on the next pass -- so it is reported.
    """
    rows = fx.stamped_rows()
    a = rows[1]
    b = fx.derive("collision", "real row 3 (alpha repl_main_thread)", rows[3],
                  request_id=a["request_id"], account_uuid=a["account_uuid"])
    ing = ingest.ingest([fx.batch(wire.STREAM_LEDGER, [a, b],
                                  a["account_uuid"])], fx.NOW_JOINT)
    check("ingest: a collision is counted apart from a duplicate",
          (ing.counts["ledger_collisions"], ing.counts["ledger_duplicates"]),
          (1, 0))
    check("ingest: and it is refused by name",
          [r.reason for r in ing.refusals], ["collision"])


def test_the_token_names_the_tenant_and_a_mismatch_fails_the_whole_batch():
    """D7.  A batch is one file range from one machine, so a disagreeing record
    means the shipper's `accounts=` filter is wrong -- and accepting the rest
    publishes the good half of a misdirected file to an employer."""
    rows = fx.stamped_rows()
    mixed = [r for r in rows if r["account"] in ("agent", "alpha")]
    check_true("fixture: one real ledger file really does interleave accounts",
               len({r["account_uuid"] for r in mixed}) > 1)
    ing = ingest.ingest([fx.batch(wire.STREAM_LEDGER, mixed,
                                  fx.uuid_of("agent"))], fx.NOW_JOINT)
    check("ingest: the whole batch is refused on an identity mismatch",
          ing.counts["batches_refused"], 1)
    check("ingest: and NOTHING from it is placed",
          (len(ing.ledger), ing.counts["ledger_accepted"]), (0, 0))
    check("ingest: the refusal is named `identity`",
          [why for why, _m in ing.batches_refused], ["identity"])


def test_a_manifest_that_disagrees_with_its_body_is_refused():
    rows = fx.stamped_rows()[:2]
    b = fx.batch(wire.STREAM_LEDGER, rows, rows[0]["account_uuid"])
    b.manifest["count"] = 99
    ing = ingest.ingest([b], fx.NOW_JOINT)
    check("ingest: a count mismatch refuses the batch",
          [why for why, _m in ing.batches_refused], ["manifest-count-mismatch"])
    check("ingest: and places nothing", len(ing.ledger), 0)


def test_accounts_never_mix():
    ing = ingest.ingest(_joint_batches(), fx.NOW_JOINT)
    # Read off the capture: agent 3 requests / 2 samples, alpha 3 / 10,
    # beta 2 / 2.
    for label, n_rows, n_smp in (("agent", 3, 2), ("alpha", 3, 10),
                                 ("beta", 2, 2)):
        uuid = fx.uuid_of(label)
        check("ingest: %s's rows are only %s's" % (label, label),
              len(ing.rows_for(uuid)), n_rows)
        check("ingest: %s's samples are only %s's" % (label, label),
              len(ing.samples_for(uuid)), n_smp)
    rep = reconcile.reconcile_ingest(ing, fx.NOW_JOINT)
    check("reconcile: three accounts, reported separately", len(rep.accounts), 3)
    # The guard is where the summing would happen, not at the door.
    smps = fx.real_samples()
    check_raises("reconcile: window reconstruction refuses a foreign sample",
                 ValueError, window.reconstruct,
                 [(s, "darwin") for s in smps], fx.NOW_JOINT,
                 fx.uuid_of("agent"))
    check_raises("reconcile: total_movement has no all-accounts form",
                 ValueError, rep.total_movement, None)
    check("reconcile: and it answers for one account",
          rep.total_movement(fx.uuid_of("alpha"), "5h"), 10.0)


# =========================================== 4. WINDOW RECONSTRUCTION ========

def _replay_windows(now=fx.NOW_REPLAY):
    smps = [(s, "darwin") for s in fx.replay_samples()]
    return window.reconstruct(smps, now, account_uuid=fx.REPLAY_ACCOUNT)


def test_replay_reconstructs_the_five_real_windows():
    """65 real samples, one account, 33 hours -> five 5-hour windows.

    Read off the fixture:
      resets   1786388400 1786406400 1786424400 1786471800 1786489800
      samples           4         18         22         18          2
      peaks             6         19         15         23          0
    """
    wins, refusals = _replay_windows()
    w5 = [w for w in wins if w.kind == "5h"]
    check("window: five 5-hour windows", tuple(w.resets_at for w in w5),
          fx.REPLAY_5H_RESETS)
    check("window: their peaks are the real ones",
          tuple(w.peak for w in w5), fx.REPLAY_5H_PEAKS)
    check("window: with the real sample counts",
          tuple(w.samples_n for w in w5), fx.REPLAY_5H_COUNTS)
    check("window: bounds are resets_at - 18000",
          tuple(w.start for w in w5),
          tuple(r - 18000 for r in fx.REPLAY_5H_RESETS))
    # `resets_at: 1` is refused by name and creates no window.  A percentage
    # without a valid window identity is unusable: the zero baseline only holds
    # relative to a known start.
    check("window: the real `resets_at: 1` record is refused by name",
          [why for _k, why, _r in refusals], ["epoch-below-floor"])
    check("window: and it makes no sixth window",
          1 in [w.resets_at for w in w5], False)
    # One 7-day window, and the fixture has no 7-day rollover anywhere.
    w7 = [w for w in wins if w.kind == "7d"]
    check("window: one 7-day window", [w.resets_at for w in w7],
          [fx.REPLAY_7D_RESETS])
    check("window: its peak and trough are the real ones",
          (w7[0].peak, w7[0].trough), (fx.REPLAY_7D_PEAK, fx.REPLAY_7D_TROUGH))


def test_membership_is_by_resets_at_and_never_by_ts():
    """The staleness lemma, proven by one real record.

    Row 49 is ts=1786459730 naming resets_at=1786424400 -- a snapshot at least
    35 330 s (9.81 h) old, reported after the window it names had closed.  Its
    OWN timestamp falls inside the NEXT window's [start, end).  Filing it by
    `ts` files a real 14% reading against a window it says nothing about.
    """
    rec = fx.replay_samples()[fx.REPLAY_STALEST_INDEX]
    check("fixture: row 49 is the stalest snapshot in the capture",
          (rec["ts"], rec["five_hour_resets_at"]),
          (fx.REPLAY_STALEST_TS, fx.REPLAY_STALEST_NAMES))
    check("fixture: it is at least 9.81 h old",
          rec["ts"] - rec["five_hour_resets_at"], fx.REPLAY_STALEST_LATENESS)
    nxt = 1786471800
    check_true("fixture: and its ts lands inside the NEXT window",
               nxt - window.L5 <= rec["ts"] < nxt)
    wins, _ = _replay_windows()
    w_named = [w for w in wins if w.kind == "5h" and w.resets_at == 1786424400][0]
    w_next = [w for w in wins if w.kind == "5h" and w.resets_at == nxt][0]
    # 22 samples name 1786424400; filtering by ts would drop the stale ones.
    check("window: the stale sample stays in the window it names",
          w_named.samples_n, 22)
    check("window: and does not leak into the window its clock points at",
          w_next.samples_n, 18)
    # It is reported, not silently tolerated: the worst lateness in that
    # window is row 49's 35 330 s, and it is what GRACE is sized against.
    check("window: lateness is reported per host, in seconds",
          w_named.stale, {"darwin": fx.REPLAY_STALEST_LATENESS})


def test_max_is_the_only_sound_operator():
    """Pooled by ts, the real capture contains six in-window decreases.

    Every one is a stale snapshot, not a decrease -- utilisation is
    non-decreasing inside a window.  So each sample is a LOWER bound on the
    window's terminal value, and re-clamping across machines is exactly `max`.
    """
    smps = [s for s in fx.replay_samples()
            if s["five_hour_resets_at"] == 1786406400]
    by_ts = sorted(smps, key=lambda s: s["ts"])
    decreases = sum(1 for a, b in zip(by_ts, by_ts[1:])
                    if b["five_hour_pct"] < a["five_hour_pct"])
    check_true("fixture: this real window decreases when ordered by ts",
               decreases >= 2)
    check("fixture: its last reading by ts is NOT its peak",
          by_ts[-1]["five_hour_pct"] == max(s["five_hour_pct"] for s in smps),
          False)
    wins, _ = _replay_windows()
    w = [x for x in wins if x.kind == "5h" and x.resets_at == 1786406400][0]
    check("window: the window's value is the max, not the last", w.peak, 19.0)


def test_a_forward_resets_at_is_a_rollover():
    """Close the window, open the next.

    Most of the feature lives in this branch.  Locally, deleting it drops the
    replay from 49 records to 22 and turns a 90%->2% roll into one record
    followed by permanent silence.  Here it is what makes the SET of windows an
    observable.
    """
    wins, _ = _replay_windows()
    w5 = [w for w in wins if w.kind == "5h"]
    check("window: every window but the last is closed BY the next one",
          [w.close_basis for w in w5],
          ["rollover"] * 4 + ["elapsed"])
    check("window: all five are closed", [w.closed for w in w5], [True] * 5)
    check("window: and the superseding reset is named",
          [w.superseded_by for w in w5],
          list(fx.REPLAY_5H_RESETS[1:]) + [None])

    # DERIVED: a 90% -> 2% roll.  No capture contains one -- the sharpest real
    # drop is 23 -> 0 -- so two real records carry authored percentages and
    # nothing else.
    base = fx.real_samples()[1]
    hi = fx.derive("rollover-90", "real sample 1 (alpha)", base,
                   five_hour_pct=90, five_hour_resets_at=1786598400,
                   ts=1786598000)
    lo = fx.derive("rollover-2", "real sample 1 (alpha)", base,
                   five_hour_pct=2, five_hour_resets_at=1786616400,
                   ts=1786598500)
    wins2, _ = window.reconstruct([(hi, "darwin"), (lo, "darwin")],
                                  1786620000, account_uuid=base["account_uuid"])
    w5 = [w for w in wins2 if w.kind == "5h"]
    check("window: a 90%->2% roll is TWO windows, not one and then silence",
          [(w.resets_at, w.peak) for w in w5],
          [(1786598400, 90.0), (1786616400, 2.0)])
    check("window: the first is closed by the rollover",
          w5[0].close_basis, "rollover")
    check("window: and the drop is not read as a decrease inside one window",
          w5[1].peak, 2.0)


def test_windows_are_activity_anchored_and_the_gap_is_reported():
    """W1.  Consecutive resets step by exactly 18000 twice and then by 47400,
    leaving 29 400 s -- 8 h 10 m -- belonging to no window at all."""
    wins, _ = _replay_windows()
    check("window: the real gap between windows is 8 h 10 m",
          window.gaps(wins), [("5h", 1786424400, 1786453800, 29400)])
    check("fixture: two consecutive resets really do differ by exactly L5",
          fx.REPLAY_5H_RESETS[1] - fx.REPLAY_5H_RESETS[0], window.L5)
    check("fixture: and one pair does not",
          fx.REPLAY_5H_RESETS[3] - fx.REPLAY_5H_RESETS[2], 47400)


def test_the_baseline_of_a_closed_five_hour_window_is_zero_by_construction():
    """W2.  Four real rows land within seconds of a window start and read 0:
    rows 4, 21, 43 and 62, at 268 s, 120 s, 221 s and 27 s after it opened."""
    smps = fx.replay_samples()
    for i, offset in ((4, 268), (21, 120), (43, 221), (62, 27)):
        r = smps[i]
        check("fixture: row %d reads 0 at start+%ds" % (i, offset),
              (r["five_hour_pct"],
               r["ts"] - (r["five_hour_resets_at"] - window.L5)), (0, offset))
    wins, _ = _replay_windows()
    w5 = [w for w in wins if w.kind == "5h"][0]
    w7 = [w for w in wins if w.kind == "7d"][0]
    check("window: a 5-hour window's baseline is zero by construction",
          (w5.baseline, w5.baseline_basis), (0.0, "zero-by-construction"))
    check("window: so movement and peak are the same quantity",
          w5.movement_lo, w5.peak)
    # The 7-day window gets the weaker statement, because it is the honest one:
    # no capture ever observes a 7-day window from 0 (the lowest is 41).
    check("window: a 7-day window's baseline is its smallest observation",
          (w7.baseline, w7.baseline_basis),
          (fx.REPLAY_7D_TROUGH, "at-most-smallest-observation"))
    check("window: and its length is assumed, not confirmed",
          (window.L7_CONFIRMED, w7.bounds_basis), (False, "assumed-length"))
    check("window: while L5 is confirmed by the data",
          (window.L5_CONFIRMED, w5.bounds_basis), (True, "confirmed"))


def test_how_tight_the_bound_is_travels_with_it():
    """The only measure of whether `max` reached the terminal value.

    Real gaps to close: 2469, 395, 4989, 39, 17184 s.  39 s is strong evidence
    the peak IS the terminal value; 17 184 s is none at all.
    """
    wins, _ = _replay_windows()
    w5 = [w for w in wins if w.kind == "5h"]
    check("window: the gap to close is reported per window",
          tuple(w.observation_gap_s for w in w5), fx.REPLAY_5H_GAPS_TO_CLOSE)


def test_clock_skew_between_machines_is_one_sided_and_detected():
    """A snapshot cannot predate the window it names, so `resets_at - ts > L`
    is impossible without the observing machine's clock being behind.

    Zero such rows exist in the capture -- so the real assertion is that this
    machine's clock is NOT measurably wrong, and the detection is exercised on
    a DERIVED second machine.
    """
    smps = fx.replay_samples()
    real_skew = [i for i, s in enumerate(smps)
                 if s["five_hour_resets_at"] > wire.EPOCH_FLOOR
                 and s["five_hour_resets_at"] - s["ts"] > window.L5]
    check("fixture: the captured machine's clock is not measurably behind",
          real_skew, [])

    base = fx.real_samples()[1]                       # alpha, darwin, pct 0
    late = fx.derive("skewed-machine", "real sample 1 (alpha, host darwin)",
                     base, host="linux-box",
                     ts=base["five_hour_resets_at"] - window.L5 - 3600,
                     five_hour_pct=12)
    pairs = [(s, "darwin") for s in fx.real_samples()
             if s["account_uuid"] == base["account_uuid"]]
    pairs.append((late, "linux-box"))
    wins, _ = window.reconstruct(pairs, fx.NOW_JOINT,
                                 account_uuid=base["account_uuid"])
    w = [x for x in wins if x.kind == "5h"][0]
    check("window: the skewed machine is named, with how far behind it is",
          w.skew, {"linux-box": 3600})
    check("window: and its reading is still a member of the window it names",
          w.samples_n, len(pairs))
    # Re-clamping across machines is `max`, whatever their clocks say: the
    # skewed machine's 12 beats the on-time machine's 10 even though its
    # timestamp is an hour before the window could have opened.
    check("window: re-clamping across machines is max, not last-by-ts",
          (w.peak, sorted(w.peak_hosts)), (12.0, ["linux-box"]))
    check("window: both machines are known to the window",
          sorted(w.hosts), ["darwin", "linux-box"])


def test_a_sample_with_no_usable_timestamp_costs_a_field_not_the_report():
    """`wire.check` deliberately does not validate `ts`: membership is by
    `resets_at`.  So such a record reaches reconstruction, and an unguarded
    min() over an empty sequence would take every account's report down."""
    base = fx.real_samples()[1]
    noclock = fx.derive("no-timestamp", "real sample 1 (alpha)", base, ts=None)
    wins, _ = window.reconstruct([(noclock, "darwin")], fx.NOW_JOINT,
                                 account_uuid=base["account_uuid"])
    w = [x for x in wins if x.kind == "5h"][0]
    check("window: it still forms a window", w.resets_at,
          base["five_hour_resets_at"])
    check("window: with the timestamp-derived fields empty, not invented",
          (w.peak_ts, w.first_ts, w.observation_gap_s), (None, None, None))


# ================================================ 5. ATTRIBUTION ============

def test_attribution_happens_only_after_a_window_closes():
    rep = joint()
    a = acc(rep, "alpha")
    # The 7-day windows of the joint capture are still open at NOW_JOINT.
    open_a = att_of(a, "7d", 1786636800)
    check("attribute: an open window is refused by name",
          open_a.refused, "window-open")
    check("attribute: with no attributed and no residual",
          (open_a.attributed_pp, open_a.residual_pp), (None, None))
    closed = att_of(a, "5h", 1786598400)
    check("attribute: a closed window is attributed", closed.refused, None)


def test_the_residual_is_the_product_and_is_never_redistributed():
    """attributed + residual == movement, exactly.  The obvious implementation
    -- attributed_i = movement * w_i / sum(w) -- makes the residual identically
    zero for every window for ever, and the numbers then look complete and say
    nothing."""
    rep = joint()
    for label in ("agent", "alpha", "beta"):
        a = att_of(acc(rep, label), "5h", fx.JOINT_5H_RESETS[label])
        check("attribute: %s -- attributed + residual is the movement" % label,
              a.attributed_pp + a.residual_pp, a.movement_pp, tol=1e-9)
        # The partition dimensions sum to the attributed figure exactly; the
        # residual appears in none of them.
        for dim in attribute.PARTITION_DIMENSIONS:
            tot = sum(b["attributed_pp"] for b in a.by[dim].values())
            check("attribute: %s -- `by %s` sums to attributed, not to "
                  "movement" % (label, dim), tot, a.attributed_pp, tol=1e-9)


def test_the_fit_names_the_window_that_pinned_it():
    """The window that sets the rate has a residual of zero BY CONSTRUCTION.
    A zero residual that is an artefact of the fit must not read as a
    measurement, so the report says which window it was."""
    rep = joint()
    a = acc(rep, "alpha")
    at = att_of(a, "5h", 1786598400)
    check("attribute: the pinning window is named, per kind",
          a.rate_pinned_by["5h"], (fx.uuid_of("alpha"), "5h", 1786598400))
    check("attribute: and the attribution says the rate came from itself",
          at.rate_is_from_this_window, True)
    check("attribute: which is why its residual is exactly zero",
          at.residual_pp, 0.0, tol=1e-9)
    check_true("attribute: the assumption is stated in the basis",
               "assumes at least one of them had no unwatched usage"
               in a.rate_basis["5h"])


def test_a_second_window_carries_a_real_residual():
    """DERIVED: the real alpha window, replayed a window later with a real
    movement and HALF its real traffic.  One machine cannot produce this
    fixture -- two closed windows for one account is what a server is for."""
    uuid = fx.uuid_of("alpha")
    smps = [s for s in fx.real_samples() if s["account_uuid"] == uuid]
    rows = [r for r in fx.stamped_rows() if r["account_uuid"] == uuid]
    nxt = 1786616400                                  # the following window
    smps2 = list(smps)
    for i, s in enumerate(smps):
        smps2.append(fx.derive("second-window-sample", "real alpha sample", s,
                               ts=s["ts"] + window.L5,
                               five_hour_resets_at=nxt))
    rows2 = list(rows)
    for r in rows[:1]:                                # only ONE of three seen
        rows2.append(fx.derive("second-window-request", "real alpha row", r,
                               ts=r["ts"] + window.L5,
                               request_id=r["request_id"][::-1]))
    rep = reconcile.reconcile(smps2, rows2, [], 1786620000)
    a = rep.accounts[uuid]
    first = att_of(a, "5h", 1786598400)
    second = att_of(a, "5h", nxt)
    check("attribute: both windows moved 10 pp",
          (first.movement_pp, second.movement_pp), (10.0, 10.0))
    check("attribute: the rate is pinned by the window with more traffic",
          a.rate_pinned_by["5h"], (uuid, "5h", 1786598400))
    check("attribute: so the sparse window carries a real residual",
          second.residual_pp > 0.0, True)
    check("attribute: and the residual is NOT folded into the buckets",
          sum(b["attributed_pp"] for b in second.by["host"].values()),
          second.attributed_pp, tol=1e-9)
    check("attribute: attributed + residual is still exactly the movement",
          second.attributed_pp + second.residual_pp, second.movement_pp,
          tol=1e-9)
    check("attribute: the residual fraction is reported",
          second.residual_fraction,
          second.residual_pp / second.movement_pp, tol=1e-12)


def test_requests_with_no_visible_movement_are_not_clamped_away():
    """An integer percentage that does not move while requests happen.

    The published figure is quantised to whole points: 43 requests and $2.92
    have been observed producing no visible change.  With a rate fitted
    elsewhere, more traffic under an unmoved percentage over-attributes -- and
    that is REPORTED, negative, not clamped to zero.  Clamping would silently
    turn a loose lower bound into a claim that the residual is zero.
    """
    uuid = fx.uuid_of("alpha")
    smps = [s for s in fx.real_samples() if s["account_uuid"] == uuid]
    rows = [r for r in fx.stamped_rows() if r["account_uuid"] == uuid]
    rate = acc(joint(), "alpha").rate                 # {'5h': 10 pp / $0.0592663}

    extra = [fx.derive("no-movement-request", "real alpha row", r,
                       request_id=r["request_id"][::-1],
                       ts=r["ts"] + 5) for r in rows]
    rep = reconcile.reconcile(smps, rows + extra, [], fx.NOW_JOINT, rate=rate)
    a = att_of(rep.accounts[uuid], "5h", 1786598400)
    check("attribute: the extra requests are counted", a.requests_n, 6)
    check("attribute: the percentage did not move", a.movement_pp, 10.0)
    check("attribute: so attribution exceeds it", a.attributed_pp > 10.0, True)
    check("attribute: the residual is reported NEGATIVE, not clamped",
          a.residual_pp < 0.0, True)
    check("attribute: and flagged", a.over_attributed, True)
    check_true("attribute: with the reason -- a lower bound, not wrong requests",
               any("LOWER bound" in n for n in a.notes))
    # A window that moved less than the quantiser's resolution says nothing
    # about the rate and must not be allowed to pin it at zero.
    wins, _ = _replay_windows()
    flat = [w for w in wins if w.kind == "5h" and w.resets_at == 1786489800][0]
    check("fixture: the real capture has a window that moved 0 pp",
          flat.movement_lo, 0.0)
    check("attribute: a 0 pp window is not eligible to pin the rate",
          attribute.fit_rate([(flat, 1.0)])[0], None)


def test_the_weighting_is_cost_and_says_it_is_provisional():
    rows = fx.stamped_rows()
    check("attribute: the weight is cost_usd_reported",
          attribute.WEIGHT_FIELD, "cost_usd_reported")
    check("attribute: a real row's weight is its reported cost",
          attribute.weight_of(rows[1]), (rows[1]["cost_usd_reported"], False))
    missing = fx.derive("no-cost", "real row 1", rows[1],
                        cost_usd_reported=None)
    check("attribute: a row with no cost weighs 0 and is COUNTED",
          attribute.weight_of(missing), (0.0, True))
    rep = reconcile.reconcile(
        [s for s in fx.real_samples()
         if s["account_uuid"] == fx.uuid_of("alpha")],
        [r for r in fx.stamped_rows()
         if r["account_uuid"] == fx.uuid_of("alpha")] + [missing],
        [], fx.NOW_JOINT)
    # The note is on EVERY attribution, including the refused ones: a weight
    # nobody has validated, reported without that sentence, becomes a fact by
    # repetition.
    for a in rep.accounts[fx.uuid_of("alpha")].attributions:
        check_true("attribute: the provisional note is on the %s attribution"
                   % a.kind, attribute.PROVISIONAL_NOTE in a.notes)
    check_true("attribute: it names cost_usd_reported and the open question",
               "cost_usd_reported" in attribute.PROVISIONAL_NOTE
               and "token class" in attribute.PROVISIONAL_NOTE)


def test_with_nothing_to_fit_on_it_refuses_rather_than_inventing_a_rate():
    """The replay account has 65 real samples and not one request.

    A made-up rate produces a residual that is a made-up number with a
    percentage sign on it, so shares of observed weight are reported and
    percentages are not.
    """
    rep = reconcile.reconcile(fx.replay_samples(), [], [], fx.NOW_REPLAY)
    a = rep.accounts[fx.REPLAY_ACCOUNT]
    check("attribute: no rate could be fitted, for either kind",
          a.rate, {"5h": None, "7d": None})
    for at in a.attributions:
        check("attribute: %s %s is refused by name" % (at.kind, at.resets_at),
              at.refused, "no-rate")
        check("attribute: with no attributed figure at all",
              at.attributed_pp, None)
    check_true("attribute: and the movement is still reported",
               att_of(a, "5h", 1786471800).movement_pp == 23.0)


def test_requests_in_no_window_at_all_are_reported_and_attributed_nowhere():
    """The 8 h 10 m that belongs to no window is not an edge case: it is what
    activity-anchored windows produce, and folding those requests into a
    neighbour is the difference between a residual that means something and
    one that has absorbed an accounting error."""
    uuid = fx.uuid_of("alpha")
    smps = [s for s in fx.real_samples() if s["account_uuid"] == uuid]
    rows = [r for r in fx.stamped_rows() if r["account_uuid"] == uuid]
    orphan = fx.derive("orphan-request", "real alpha row", rows[0],
                       ts=1786500000, request_id=rows[0]["request_id"][::-1])
    rep = reconcile.reconcile(smps, rows + [orphan], [], fx.NOW_JOINT)
    a = rep.accounts[uuid]
    check("reconcile: the orphan is listed as claimed by no 5h window",
          [r["request_id"] for r in a.unclaimed["5h"]], [orphan["request_id"]])
    at = att_of(a, "5h", 1786598400)
    check("reconcile: and it is in no window's request set", at.requests_n, 3)
    check_true("reconcile: the report says so in words",
               any("fall in no 5h window" in n for n in a.notes))


# ================================================== 6. COVERAGE =============

ALPHA_W = 1786598400
ALPHA_START = ALPHA_W - window.L5
PAST_GRACE = ALPHA_W + window.GRACE + 1


def _alpha(now, atts, extra_samples=(), rate=None):
    uuid = fx.uuid_of("alpha")
    smps = [s for s in fx.real_samples() if s["account_uuid"] == uuid]
    rows = [r for r in fx.stamped_rows() if r["account_uuid"] == uuid]
    rep = reconcile.reconcile(smps + list(extra_samples), rows, list(atts),
                              now, rate=rate)
    return rep.accounts[uuid]


def test_with_no_attestation_coverage_is_unknown_and_not_zero():
    """0.0 is a claim -- 'nobody was listening'.  None is the truth -- 'nobody
    said'.  That is today's normal state: nothing emits an attestation yet."""
    a = _alpha(fx.NOW_JOINT, [])
    at = att_of(a, "5h", ALPHA_W)
    check("coverage: unknown, not zero", at.coverage.fraction, None)
    check("coverage: the basis says so", at.coverage.basis, "unknown")
    check("coverage: the machine is silent, not dark",
          (sorted(at.coverage.hosts_silent), sorted(at.coverage.hosts_dark)),
          (["darwin"], []))
    check_true("coverage: and the account report says it in words",
               any("coverage is unknown" in n for n in a.notes))
    check("coverage: attribution still happens -- silence is not a refusal",
          at.refused, None)


def test_one_machine_reporting_and_one_dark():
    """DERIVED second machine, AUTHORED attestations.

    darwin attests and covers half the window.  linux-box attests for the
    PREVIOUS window and says nothing about this one, past the grace -- which is
    a positive statement that it was not listening, and is why `dark` is
    separate from `silent`.
    """
    uuid = fx.uuid_of("alpha")
    base = fx.real_samples()[1]
    second = fx.derive("second-machine", "real sample 1 (alpha, host darwin)",
                       base, host="linux-box")
    atts = [
        fx.attestation(uuid, "5h", ALPHA_W, "darwin",
                       [[ALPHA_START, ALPHA_START + 9000, True]]),
        fx.attestation(uuid, "5h", ALPHA_W - window.L5, "linux-box",
                       [[ALPHA_START - window.L5, ALPHA_START, True]]),
    ]
    a = _alpha(PAST_GRACE, atts, extra_samples=[second])
    at = att_of(a, "5h", ALPHA_W)
    cov = at.coverage
    # 9000 s of 18000.
    check("coverage: half the window had a machine reporting", cov.fraction, 0.5)
    check("coverage: the reporting machine is named",
          sorted(cov.hosts_reporting), ["darwin"])
    check("coverage: the machine that attests elsewhere and not here is DARK",
          sorted(cov.hosts_dark), ["linux-box"])
    check("coverage: nothing is pending past the grace",
          sorted(cov.hosts_pending), [])
    check("coverage: the uncovered half is reported as an interval",
          cov.uncovered, [(ALPHA_START + 9000, ALPHA_W)])
    check_true("coverage: and the attribution says part of the residual is "
               "time nobody was watching",
               any("no machine reporting" in n for n in at.notes))
    check("coverage: it still attributes -- poor coverage is a caveat, not a "
          "refusal", at.refused, None)


def test_two_machines_overlapping_are_one_second_of_coverage():
    uuid = fx.uuid_of("alpha")
    atts = [
        fx.attestation(uuid, "5h", ALPHA_W, "darwin",
                       [[ALPHA_START, ALPHA_START + 9000, True]]),
        fx.attestation(uuid, "5h", ALPHA_W, "linux-box",
                       [[ALPHA_START + 5000, ALPHA_START + 14000, True]]),
    ]
    a = _alpha(PAST_GRACE, atts)
    cov = att_of(a, "5h", ALPHA_W).coverage
    # union([0,9000], [5000,14000]) = 14000 of 18000.  Summing instead gives
    # 18000 and reports a fully-covered window that had a 4000 s hole in it.
    check("coverage: overlapping spans are unioned, never summed",
          cov.fraction, 14000 / 18000.0, tol=1e-12)
    check("coverage: both machines are reporting",
          sorted(cov.hosts_reporting), ["darwin", "linux-box"])


def test_full_coverage_reports_no_caveat():
    uuid = fx.uuid_of("alpha")
    atts = [fx.attestation(uuid, "5h", ALPHA_W, "darwin",
                           [[ALPHA_START, ALPHA_W, True]])]
    a = _alpha(PAST_GRACE, atts)
    at = att_of(a, "5h", ALPHA_W)
    check("coverage: a fully covered window is 1.0", at.coverage.fraction, 1.0)
    check("coverage: with nothing uncovered", at.coverage.uncovered, [])
    check("coverage: and no coverage caveat on the attribution",
          [n for n in at.notes if "no machine reporting" in n], [])


def test_a_window_with_no_coverage_at_all_refuses_to_attribute():
    """The guard on the residual.

    A window during which none of your machines was listening produces a large
    residual, and it is byte-identical to browser usage in the output.  So it
    is refused BY NAME rather than published as evidence of a surface that may
    not exist.
    """
    uuid = fx.uuid_of("alpha")
    atts = [fx.attestation(uuid, "5h", ALPHA_W - window.L5, "darwin",
                           [[ALPHA_START - window.L5, ALPHA_START, True]])]
    a = _alpha(PAST_GRACE, atts)
    at = att_of(a, "5h", ALPHA_W)
    check("coverage: nobody was listening, and that will not change",
          (at.coverage.fraction, at.coverage.settled_zero), (0.0, True))
    check("coverage: so attribution is refused by name", at.refused,
          "no-coverage")
    check("coverage: and no residual is published",
          (at.attributed_pp, at.residual_pp), (None, None))
    check("coverage: the movement is still reported -- it is real",
          at.movement_pp, 10.0)
    check_true("coverage: with the reason in words",
               any("indistinguishable from usage on a surface that may not "
                   "exist" in n for n in at.notes))


def test_a_machine_that_has_not_spoken_yet_is_pending_not_dark():
    """GRACE is 72 h against a worst observed snapshot lateness of 9.81 h.  A
    machine that wakes within three days is not late, and calling it dark would
    invent an unwatched surface out of a laptop in a bag."""
    uuid = fx.uuid_of("alpha")
    base = fx.real_samples()[1]
    second = fx.derive("second-machine", "real sample 1 (alpha)", base,
                       host="linux-box")
    atts = [
        fx.attestation(uuid, "5h", ALPHA_W, "darwin",
                       [[ALPHA_START, ALPHA_START + 9000, True]]),
        fx.attestation(uuid, "5h", ALPHA_W - window.L5, "linux-box",
                       [[ALPHA_START - window.L5, ALPHA_START, True]]),
    ]
    early = _alpha(ALPHA_W + 60, atts, extra_samples=[second])
    cov = att_of(early, "5h", ALPHA_W).coverage
    check("coverage: inside the grace the mute machine is PENDING",
          (sorted(cov.hosts_pending), sorted(cov.hosts_dark)),
          (["linux-box"], []))
    check("coverage: so the fraction is a lower bound and says so",
          cov.lower_bound, True)
    late = _alpha(PAST_GRACE, atts, extra_samples=[second])
    cov = att_of(late, "5h", ALPHA_W).coverage
    check("coverage: past the grace the same silence is DARK",
          (sorted(cov.hosts_pending), sorted(cov.hosts_dark)),
          ([], ["linux-box"]))
    check("coverage: and the fraction is then final",
          cov.lower_bound, False)
    check("window: GRACE is 72 h, 7.3x the worst observed lateness",
          (window.GRACE, window.GRACE / float(fx.REPLAY_STALEST_LATENESS) > 7),
          (259200, True))


def test_coverage_zero_inside_the_grace_still_attributes():
    """settled_zero needs BOTH: no coverage and nothing pending.  A window that
    nobody has reported on yet is not the same as one nobody was watching."""
    uuid = fx.uuid_of("alpha")
    atts = [fx.attestation(uuid, "5h", ALPHA_W - window.L5, "darwin",
                           [[ALPHA_START - window.L5, ALPHA_START, True]])]
    a = _alpha(ALPHA_W + 60, atts)
    at = att_of(a, "5h", ALPHA_W)
    check("coverage: zero inside the grace is not settled",
          at.coverage.settled_zero, False)
    check("coverage: so the window is still attributed", at.refused, None)


# ====================== 7. REPAIRED ADVERSARIAL FINDINGS ====================
#
# These arrived as `test_reconcile_adversarial.py`, a deliberately red file
# holding concrete wrong numbers.  They live here now because a repaired defect
# pinned in a permanently-red suite is pinned by nothing: nobody reads a red
# file for a new red line, and `mutate.py` refuses to run against a red tree, so
# the fix would never be mutated either.  Every one has a mutation beside it.
#
# The numbers below were measured on the tree BEFORE and AFTER the repair and
# both are stated, because "the residual is now 0.74" says nothing without the
# 0.87 it replaced.

def _adv_rows(reset, n, cost=1.0, host="darwin", tag=""):
    """DERIVED ledger rows: one REAL captured request, re-stamped and re-timed.

    There is no capture in which stream A and the replay account coexist -- one
    workstation, one account per file -- and the alternative to deriving is not
    testing attribution against the 65 real samples at all.
    """
    base = fx.real_rows()[1]                    # repl_main_thread, real
    return [fx.derive("adversarial-request", "real-api-request.jsonl row 1",
                      base, account_uuid=fx.REPLAY_ACCOUNT,
                      ts=reset - 9000 + k,
                      request_id="%s%d-%d" % (tag, reset, k),
                      cost_usd_reported=cost, host=host)
            for k in range(n)]


def _adv_samples(resets, cap=None, cap_window=None, drop_7d=False):
    """REAL replay samples, filtered to named windows.

    `cap` truncates one window's OBSERVATION -- samples above the cap are
    simply never seen -- which is exactly what a laptop that sleeps through the
    end of a window produces.  It changes nothing about the traffic.
    """
    out = []
    for s in fx.replay_samples():
        r = s.get("five_hour_resets_at")
        if r not in resets:
            continue
        p = s.get("five_hour_pct")
        if cap is not None and r == cap_window and p is not None and p > cap:
            continue
        if drop_7d:
            s = fx.derive("adversarial-5h-only", "replay-real.jsonl sample", s,
                          seven_day_pct=None, seven_day_resets_at=None)
        out.append(s)
    return out


def _adv(samples, rows, now=None):
    return reconcile.reconcile(samples, rows, [], now or fx.NOW_REPLAY)


def test_the_rate_is_fitted_per_window_kind_never_across_two_plans():
    """A 7-day window and a 5-hour window have DIFFERENT DENOMINATORS.

    `reconcile` fitted ONE rate per account over both kinds and applied it to
    both -- the file's own rule, "never sum across accounts: different plans,
    different denominators", broken one level down between the two plans of a
    single account.  It was not a tie either: the 7-day window's weight is a
    SUPERSET of every 5-hour window's inside it and its movement is
    `peak - trough` because its baseline cannot be zero, so its ratio is
    structurally the smallest and `min` chose it the moment one weekly window
    had closed -- permanently thereafter, since the reconciler re-derives from
    byte zero.

    16 DERIVED rows at cost 1.0, 4 in each of four REAL replay 5-hour windows.
      before: one rate 0.75 (the 7-day one).  Window 1786471800 moved 23 pp,
              was attributed 3.0, residual fraction 0.87 -- "87% of this window
              came from a surface you are not watching", from traffic that is
              entirely in the ledger.
      after:  5h rate 1.5, 7d rate 0.75.  Same window attributed 6.0.
    """
    rows = []
    for reset in (1786388400, 1786406400, 1786424400, 1786471800):
        rows.extend(_adv_rows(reset, 4))
    a = _adv(fx.replay_samples(), rows).accounts[fx.REPLAY_ACCOUNT]

    check("attribute: the rate is a mapping per window kind, not one number",
          sorted(a.rate), ["5h", "7d"])
    check("attribute: a 5-hour rate is pinned by a 5-HOUR window",
          a.rate_pinned_by["5h"][1], "5h")
    check("attribute: and the 7-day rate by a 7-day one",
          a.rate_pinned_by["7d"][1], "7d")
    check("attribute: the two rates really do differ -- 2x here",
          (a.rate["5h"], a.rate["7d"]), (1.5, 0.75))
    # The 7-day window explains its own movement from the same 16 rows.
    check("attribute: 7d residual with full instrumentation",
          att_of(a, "7d", fx.REPLAY_7D_RESETS).residual_pp, 0.0, tol=1e-9)
    worst = att_of(a, "5h", 1786471800)
    check("attribute: the 5-hour figure is no longer scaled by the weekly "
          "plan's denominator (was 3.0)", worst.attributed_pp, 6.0, tol=1e-9)
    check("attribute: so its residual fraction is 0.74, not 0.87",
          worst.residual_fraction, 17.0 / 23.0, tol=1e-9)
    check("attribute: and the row carries the 5-hour rate",
          worst.rate, a.rate["5h"])

    # A scalar override is the same error with the caller's hand on it, so it
    # raises rather than being applied to every window of the account.
    check_raises("attribute: a bare number rate is refused, not spread over "
                 "both plans", ValueError, reconcile.reconcile,
                 fx.replay_samples(), rows, [], fx.NOW_REPLAY, rate=1.0)
    check_raises("attribute: including per account", ValueError,
                 reconcile.reconcile, fx.replay_samples(), rows, [],
                 fx.NOW_REPLAY, rate={fx.REPLAY_ACCOUNT: 1.0})
    ok = reconcile.reconcile(fx.replay_samples(), rows, [], fx.NOW_REPLAY,
                             rate={"5h": 2.0}).accounts[fx.REPLAY_ACCOUNT]
    check("attribute: a per-kind override is taken, and only for that kind",
          (ok.rate["5h"], ok.rate["7d"]), (2.0, 0.75))


def test_a_window_nobody_watched_to_the_end_cannot_pin_the_rate():
    """`movement_lo` is `max` over the samples that were SEEN.

    `fit_rate` excluded a window whose movement was below the quantiser's
    resolution and looked at `observation_gap_s` not at all, so a machine that
    slept through the end of a window reported a peak far below the terminal
    value, that window's ratio collapsed, and `min` selected precisely it.  The
    weakness then propagated multiplicatively into every other window.

    REAL replay samples, DERIVED rows, 7-day fields nulled so this is measured
    in isolation.  Windows 1786406400 (19 rows) and 1786471800 (23 rows, and an
    observation gap of 39 s -- `window.py` calls that "strong evidence the peak
    is the terminal value").
      before: capping the FIRST window's observation at 8% -- changing nothing
              about the traffic -- dropped the rate from 1.0 to 0.1579 and gave
              the second window attributed 3.63 of movement 23.0, residual
              19.37, residual fraction 0.84, with nothing in its notes about a
              bound because its own gap is 39 s.
      after:  the capped window's gap is 17202 s (96% of the window), so it is
              not eligible to pin anything; the rate stays 1.0.
    """
    pin, victim = 1786406400, 1786471800
    rows = _adv_rows(pin, 19) + _adv_rows(victim, 23)

    clean = _adv(_adv_samples((pin, victim), drop_7d=True),
                 rows).accounts[fx.REPLAY_ACCOUNT]
    check("attribute: fully observed, the victim's residual is zero",
          att_of(clean, "5h", victim).residual_fraction, 0.0, tol=1e-9)

    blind = _adv(_adv_samples((pin, victim), cap=8, cap_window=pin,
                              drop_7d=True), rows).accounts[fx.REPLAY_ACCOUNT]
    check("fixture: capping the observation really does open a 17202 s gap",
          win(blind, "5h", pin).observation_gap_s, 17202)
    check("attribute: the blind window does not pin the rate",
          blind.rate_pinned_by["5h"], (fx.REPLAY_ACCOUNT, "5h", victim))
    check("attribute: so the rate is unchanged by the blindness",
          blind.rate["5h"], 1.0, tol=1e-9)
    v = att_of(blind, "5h", victim)
    check("attribute: and a window observed to its last 39 s does not inherit "
          "another window's blindness as residual (was 0.84)",
          v.residual_fraction, 0.0, tol=1e-9)

    # Part two: whatever pinned it, every row that used the rate says which
    # window that was and how good its evidence was.  `rate_is_from_this_window
    # = False` was the only hint, and a boolean is not a caveat.
    check_true("attribute: every attributed row names the window that pinned "
               "its rate", any("pinned by" in n for n in v.notes))
    check_true("attribute: with that window's own gap in the sentence",
               any("last observed 39 s before it closed" in n for n in v.notes))
    check("attribute: and the pinning window's stats travel as data too",
          (v.rate_pinned_stats["resets_at"],
           v.rate_pinned_stats["observation_gap_s"]), (victim, 39))

    # The guard is a PREFERENCE, not a floor: every 5-hour window in the joint
    # capture has a 78% gap, and a floor would refuse to fit a rate at all on
    # the only real multi-account fixture there is.
    j = acc(joint(), "alpha")
    check("attribute: with every candidate blind, the tightest still pins it",
          j.rate_pinned_by["5h"], (fx.uuid_of("alpha"), "5h", ALPHA_W))
    check_true("attribute: and the basis says outright that it did",
               "NO candidate met that observation bound" in j.rate_basis["5h"])


def test_two_resets_at_closer_than_a_window_cannot_both_be_genuine():
    """One stray `resets_at` and 19 requests were attributed twice.

    `_reconstruct_kind` bucketed by `resets_at` and derived each bucket's
    `start` as `reset - length` independently, so two windows overlapped and
    every request in the overlap was a member of both -- the double counting the
    whole server exists to prevent, inside one account on one machine.  `gaps()`
    said nothing because it only tests `b.start > a.end`.

    Not an exotic input: `replay-real.jsonl` row 16 carries
    `five_hour_resets_at: 1`, so the capture ALREADY contains a corrupt
    `resets_at` and is caught only because EPOCH_FLOOR happens to reject that
    value.  One inside the plausible range passes every guard in `wire.py`.

      before: two windows; 19 requests counted 38 times across one kind; the
              1 pp sliver was eligible (>= QUANT_RESOLUTION_PP) with the full
              19.0 of weight and pinned the rate at 0.0526, 19x too low; the
              real window's residual went from 0.00 (0%) to 18.00 (95%).
      after:  the sliver's bucket carries 1 sample against the real window's
              18, so it is refused BY NAME and there is one window.
    """
    real = 1786406400
    rows = _adv_rows(real, 19)
    good = _adv_samples((real,), drop_7d=True)
    ghost = fx.derive("adversarial-ghost-window",
                      "replay-real.jsonl sample of window 1786406400",
                      good[-1], five_hour_resets_at=real + 100,
                      five_hour_pct=1)

    rep = _adv(good + [ghost], rows)
    a = rep.accounts[fx.REPLAY_ACCOUNT]
    check("window: a request is a member of at most one 5-hour window",
          sum(at.requests_n for at in a.attributions if at.kind == "5h"),
          len(rows))
    check("window: the stray bucket is gone, not attributed beside the real one",
          [w.resets_at for w in a.windows if w.kind == "5h"], [real])
    check("window: and it is refused BY NAME, not dropped",
          [(k, why) for k, why, _r in a.refusals],
          [("5h", "resets-at-overlaps-neighbour")])
    check("attribute: so the real window's residual is not manufactured by an "
          "overlap (was 0.95)",
          att_of(a, "5h", real).residual_fraction, 0.0, tol=1e-9)
    check("attribute: nor its rate 19x too low (was 0.0526)",
          a.rate["5h"], 1.0, tol=1e-9)

    # The tie: equal evidence behind both resets_at, so nothing separates them
    # and the refusal cannot choose.  This is the branch the CLAMP exists for --
    # membership must be a partition by construction, not as a consequence of
    # the refusal rule happening to fire.
    # One request lands inside the 100 s sliver, so the sliver has weight and
    # is otherwise a perfectly eligible candidate -- which is what makes the
    # span guard in `fit_rate` load-bearing rather than decorative.
    inside_sliver = fx.derive("adversarial-request",
                              "real-api-request.jsonl row 1", fx.real_rows()[1],
                              account_uuid=fx.REPLAY_ACCOUNT, ts=real + 50,
                              request_id="sliver-1", cost_usd_reported=10.0,
                              host="darwin")
    tie_rows = rows + [inside_sliver]
    tie = _adv(good[-1:] + [ghost], tie_rows).accounts[fx.REPLAY_ACCOUNT]
    w5 = [w for w in tie.windows if w.kind == "5h"]
    check("window: on a tie both are kept, because there is no evidence either "
          "way", [w.resets_at for w in w5], [real, real + 100])
    check("window: and the later one's start is CLAMPED to the earlier's close",
          (w5[1].start, w5[1].end), (real, real + 100))
    check("window: so membership is still a partition",
          sum(at.requests_n for at in tie.attributions if at.kind == "5h"),
          len(tie_rows))
    check_true("window: and the clamp is stated on the window it happened to",
               any("clamped to the previous resets_at" in n for n in w5[1].notes))
    check("fixture: the sliver really does carry weight, so only the span "
          "guard can keep it out", att_of(tie, "5h", real + 100).weight_total,
          10.0)
    check("attribute: a 100 s sliver cannot pin a rate for an 18000 s window",
          tie.rate_pinned_by["5h"], (fx.REPLAY_ACCOUNT, "5h", real))


def test_a_late_attestation_never_overwrites_a_newer_one():
    """Last EMITTED wins, not last arrived.

    `_add_attestation` did last-write-wins with no comparison at all, and
    `emitted_at` -- declared in `wire.ATTEST_FIELDS` -- was read by no line of
    code in the package.  Batches arrive out of order routinely: a machine
    catching up on a backlog ships old ranges after new ones.

    Two attestations from host `darwin` for the real alpha window, past GRACE:
    one covering the whole window (emitted W+300), one covering none of it
    (emitted W+10).
      before: stale batch last -> coverage 0.0, refused `no-coverage`, printing
              "no machine was observed listening during this window" -- a claim
              that machine explicitly contradicted in a record the server holds.
              Fresh batch last -> coverage 1.0, refused None.  Identical
              records, identical counts, opposite verdicts.
    """
    uuid = fx.uuid_of("alpha")
    fresh = fx.attestation(uuid, "5h", ALPHA_W, "darwin",
                           [[ALPHA_START, ALPHA_W, True]],
                           emitted_at=ALPHA_W + 300)
    stale = fx.attestation(uuid, "5h", ALPHA_W, "darwin", [],
                           emitted_at=ALPHA_W + 10)
    for order, label in (([fresh, stale], "stale last"),
                         ([stale, fresh], "fresh last")):
        ing = ingest.ingest(
            [fx.batch(wire.STREAM_ATTEST, order, uuid)], PAST_GRACE)
        at = att_of(_alpha(PAST_GRACE, ing.attestations_for(uuid)),
                    "5h", ALPHA_W)
        check("ingest: %s -- the newer emission is the one held" % label,
              (at.coverage.fraction, at.refused), (1.0, None))
    ing = ingest.ingest(
        [fx.batch(wire.STREAM_ATTEST, [fresh, stale], uuid)], PAST_GRACE)
    check("ingest: an arrival older than what is held is counted apart from an "
          "idempotent re-emission",
          (ing.counts["attestations_superseded_by_older"],
           ing.counts["attestations_replaced"]), (1, 0))
    check("ingest: and refused by name, so it is in the report",
          [r.reason for r in ing.refusals], ["attestation-older-than-held"])
    # A field a decision rests on is validated like one.
    noemit = dict(fresh)
    del noemit["emitted_at"]
    check("wire: an attestation with no emitted_at is refused",
          wire.check(noemit, wire.STREAM_ATTEST, PAST_GRACE)[1],
          "attestation-epoch-not-a-number")
    # Re-ingesting the identical batch must still be a no-op.
    twice = ingest.ingest([fx.batch(wire.STREAM_ATTEST, [fresh], uuid),
                           fx.batch(wire.STREAM_ATTEST, [fresh], uuid)],
                          PAST_GRACE)
    check("ingest: an identical re-emission is idempotent, not a merge",
          (twice.counts["attestations_accepted"],
           twice.counts["attestations_replaced"],
           twice.counts["attestations_merged"]), (1, 1, 0))
    check("ingest: and the collection is unchanged",
          twice.attestations[wire.attest_key(fresh)]["up_spans"],
          fresh["up_spans"])


def test_unreadable_up_spans_are_refused_by_name_not_read_as_darkness():
    """`_clip` dropped a span it could not parse with a bare `continue`.

    `wire.check` validated an attestation's `resets_at`, `window` and
    `machine_id` and never looked at `up_spans` at all, so a malformed-span
    attestation was accepted cleanly and counted as `attestations_accepted`.
    The consequence was not an understated number, it was an inverted verdict:
    fraction 0.0, `hosts_dark` {'darwin'}, refused `no-coverage`.  This module's
    own docstring calls `dark` "A positive statement" -- that the machine was
    not listening.  The machine said the opposite; the parser could not read it.
    """
    uuid = fx.uuid_of("alpha")
    bad = fx.attestation(uuid, "5h", ALPHA_W, "darwin",
                         [["1786580400", "1786598400", True]])
    check("wire: unreadable spans refuse the record by name",
          wire.check(bad, wire.STREAM_ATTEST, PAST_GRACE),
          (False, "attestation-unreadable-spans"))
    check("wire: a span whose `clean` flag is not a bool is unreadable too",
          wire.check(fx.attestation(uuid, "5h", ALPHA_W, "darwin",
                                    [[ALPHA_START, ALPHA_W, "yes"]]),
                     wire.STREAM_ATTEST, PAST_GRACE)[1],
          "attestation-unreadable-spans")
    check("wire: while naming NO spans is a real thing to say, and passes",
          wire.check(fx.attestation(uuid, "5h", ALPHA_W, "darwin", []),
                     wire.STREAM_ATTEST, PAST_GRACE), (True, None))
    ing = ingest.ingest([fx.batch(wire.STREAM_ATTEST, [bad], uuid)],
                        PAST_GRACE)
    check("ingest: so it is refused rather than accepted and emptied",
          (ing.counts["attestations_accepted"], ing.counts["refused"]), (0, 1))

    # Belt and braces, and reachable: `reconcile` takes attestations directly
    # as well, so a caller that never went through the door must still not be
    # able to report `fraction 0.0` without saying how many spans it failed to
    # read.
    a = _alpha(PAST_GRACE, [bad])
    at = att_of(a, "5h", ALPHA_W)
    check("coverage: the unreadable spans are COUNTED, not dropped",
          (at.coverage.fraction, at.coverage.spans_unreadable), (0.0, 1))
    check("coverage: so this is not settled_zero and attribution is not refused",
          (at.coverage.settled_zero, at.refused), (False, None))
    check_true("coverage: and the basis says the spans could not be read",
               "unreadable" in at.coverage.basis)
    check_true("attribute: with a note that this is not evidence of a dark "
               "machine",
               any("NOT evidence that a machine was off" in n for n in at.notes))


def test_a_row_with_no_usable_timestamp_is_named_rather_than_vanishing():
    """It was in no window's request set and in no `unclaimed` list either.

    `wire.check` deliberately does not validate `ts` for stream A, `rows_in`
    filters on it, and `reconcile`'s `unclaimed` applied the SAME filter before
    testing window membership -- so the row left no trace anywhere while its
    weight was subtracted from `attributed_pp` and landed in the residual, the
    one number sold as evidence of an unwatched surface.  `requests_n` against
    the per-window counts was the only hint, and nothing reconciled the two.
    """
    uuid = fx.uuid_of("alpha")
    rows = [r for r in fx.stamped_rows() if r["account_uuid"] == uuid]
    smps = [s for s in fx.real_samples() if s["account_uuid"] == uuid]
    noclock = fx.derive("undatable-request", "real alpha row", rows[0],
                        ts=None, request_id=rows[0]["request_id"][::-1])
    a = reconcile.reconcile(smps, rows + [noclock], [],
                            fx.NOW_JOINT).accounts[uuid]
    check("reconcile: the account still counts it", a.requests_n, 4)
    check("reconcile: it is named in `undatable`",
          [r["request_id"] for r in a.undatable], [noclock["request_id"]])
    check("reconcile: it is in no window", att_of(a, "5h", ALPHA_W).requests_n, 3)
    check("reconcile: and in no unclaimed list either -- that is the point",
          (a.unclaimed["5h"], a.unclaimed["7d"]), ([], []))
    check_true("reconcile: the report says so in words",
               any("no usable timestamp" in n for n in a.notes))
    # `ts: true` is an int in Python and would be placed as the integer 1.
    truthy = fx.derive("undatable-request", "real alpha row", rows[0],
                       ts=True, request_id=rows[0]["request_id"][::-1] + "b")
    b = reconcile.reconcile(smps, rows + [truthy], [],
                            fx.NOW_JOINT).accounts[uuid]
    check("reconcile: a boolean ts is undatable, not the epoch 1",
          [r["request_id"] for r in b.undatable], [truthy["request_id"]])
    check("reconcile: and it is in `undatable` ONLY -- not in both buckets",
          (b.unclaimed["5h"], b.unclaimed["7d"]), ([], []))
    check("window: `contains` agrees with `rows_in` and with `undatable`",
          window.contains(win(b, "5h", ALPHA_W), True), False)


def test_the_top_level_report_carries_the_window_refusals():
    """`all_refusals` was accumulated and thrown away.

    `reconcile()` built it, extended it, and then returned `refusals=[]`; the
    variable was never read.  `reconcile_ingest` then REPLACED the field with
    the ingest refusals, so window-level refusals reached the top-level report
    on neither entry point -- and `as_dict()`, which exists to serialise that
    report, said nothing had been refused while the account report named the
    real `resets_at: 1` record from the capture.  The anti-silence surface
    reporting a silence.
    """
    rep = reconcile.reconcile(fx.replay_samples(), [], [], fx.NOW_REPLAY)
    check("reconcile: the top-level report names the refusal",
          [(r.stream, r.reason, r.detail) for r in rep.refusals],
          [(wire.STREAM_SAMPLES, "epoch-below-floor", "5h")])
    check("reconcile: and it survives serialisation",
          rep.as_dict()["refusals"],
          [(wire.STREAM_SAMPLES, "epoch-below-floor", "5h")])
    check("reconcile: while the account report still carries its own",
          [why for _k, why, _r in rep.accounts[fx.REPLAY_ACCOUNT].refusals],
          ["epoch-below-floor"])
    # The ingest entry point must EXTEND, not replace: both kinds of refusal
    # are refusals.
    smps = fx.replay_samples()
    unstamped = dict(smps[0])
    del unstamped["account_uuid"]
    ing = ingest.ingest([fx.batch(wire.STREAM_SAMPLES, smps + [unstamped],
                                  fx.REPLAY_ACCOUNT)], fx.NOW_REPLAY)
    rep2 = reconcile.reconcile_ingest(ing, fx.NOW_REPLAY)
    check("reconcile_ingest: the door's refusal and the window's are both there",
          sorted({r.reason for r in rep2.refusals}),
          ["epoch-below-floor", "no-account-uuid"])


# ================================================== 8. THE DOOR ============
#
# The one impure module: it holds the socket and the bytes.  Everything below
# posts REAL captured lines -- `fx.real_sample_lines` reads them out of the
# capture byte for byte, `fx.wire_lines` puts the real rows through
# `cu.ledger.append` and `cu.ship.stamp`, which are the writer and the
# transformation a machine actually applies -- so what the door is tested
# against is what a shipper emits, not a description of it.

DOOR_TMP = []


def door_root():
    d = tempfile.mkdtemp(prefix="srv-door-")
    DOOR_TMP.append(d)
    return d


def door_for(labels=("alpha", "beta"), **kw):
    """(door, store, root) with one token per named account."""
    root = door_root()
    tokens = {"tok-" + lbl: fx.uuid_of(lbl) for lbl in labels}
    store = serve.Store(root)
    return (serve.Door(store, serve.Tenants(mapping=tokens), **kw), store, root)


def ndjson(man, records):
    """A body in exactly `cu.ship.post_batch`'s shape: manifest, then lines."""
    body = json.dumps(man, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8") + b"\n"
    for r in records:
        body += r + b"\n"
    return body


def ship_a(label, offset=0, machine="m-3f9a1c02", host="darwin"):
    """(manifest, [line], body) for one account's real stream A."""
    consumed, lines = fx.wire_lines(label)
    man = fx.manifest(wire.STREAM_LEDGER, lines, host=host, machine_id=machine,
                      offset=offset, to=offset + consumed)
    return man, lines, ndjson(man, lines)


def ship_b(label, offset=0, machine="m-3f9a1c02", host="darwin"):
    lines = fx.real_sample_lines(label)
    man = fx.manifest(wire.STREAM_SAMPLES, lines, host=host, machine_id=machine,
                      offset=offset, to=offset + sum(len(l) + 1 for l in lines))
    return man, lines, ndjson(man, lines)


def stored(root, label, name):
    path = os.path.join(root, "accounts", fx.uuid_of(label), name)
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def test_door_accepts_a_real_batch_and_stores_the_bytes_verbatim():
    """The whole point of requirement 4, asserted on the bytes.

    Not "the records round-trip" -- the LINES, compared as bytes against what
    the shipper put on the wire.  A `json.loads`/`json.dumps` at the door would
    pass a round-trip test and still reorder every key, reformat every float
    and re-escape every non-ASCII character, which is a third schema invented
    in transit with nothing left to diff it against.
    """
    door, store, root = door_for()
    man, lines, body = ship_a("alpha")
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None, body,
                            now=fx.NOW_JOINT)
    check("door: a real stream-A batch is accepted", status, 200)
    check("door: it says how many records it took", ack["accepted"], len(lines))
    check("door: the stored file is the shipped lines, byte for byte",
          stored(root, "alpha", "ledger.jsonl"),
          b"".join(l + b"\n" for l in lines))
    check("door: and nothing was written for any other account",
          stored(root, "beta", "ledger.jsonl"), None)
    # The ack's offset is the CLIENT's byte position, not the server's.
    check("door: the ack carries the durably-held client offset",
          ack["offset"], man["to"])
    check_true("door: and the store's free bytes",
               isinstance(ack["disk_free_bytes"], int)
               and ack["disk_free_bytes"] > 0)
    # DURABLE BEFORE THE ACK: by the time the caller has a 200, the offset is
    # in the offsets FILE, not in a dictionary that dies with the process.
    # Held in memory it would read identically here and be gone after a crash,
    # which is the difference between a resend and a permanent gap.
    on_disk = json.load(open(store.offsets_path(fx.uuid_of("alpha"))))
    check("door: and the offset is on disk before the ack goes back",
          [e["offset"] for e in on_disk.values()], [man["to"]])
    check("door: recorded against the file identity the client described",
          [e["ident"] for e in on_disk.values()], [man["ident"]])
    # "nothing on record" is a third answer, not a no: a first shipment told
    # `false` reads as "your file is not the one I have".
    check("door: a first shipment is told there was nothing on record",
          ack["ident_matches"], None)


def test_the_manifest_is_kept_because_stream_b_v1_has_no_host():
    """Storing the records and dropping the envelope is silent loss.

    `wire.sample_key` hashes the record together with the shipping host, and
    `ingest.add_batch` reads that host off the manifest -- stream B's v1 shape
    has no `host` column of its own, which is proven by 65 real records.  So
    the manifest is stored beside the records with the byte range it covers,
    and `read_batches` puts the pair back together as the `ingest.Batch` the
    core consumes.
    """
    door, store, root = door_for()
    man, lines, body = ship_b("alpha", machine="m-3f9a1c02", host="darwin")
    status, _ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None, body,
                             now=fx.NOW_JOINT)
    check("door: a real stream-B batch is accepted", status, 200)

    raw = stored(root, "alpha", "manifests.jsonl").rstrip(b"\n")
    kept = json.loads(raw)
    check("door: the envelope keeps the shipping host", kept["host"], "darwin")
    check("door: and the byte range those records occupy",
          (kept["recv"]["byte_from"], kept["recv"]["byte_to"]),
          (0, len(stored(root, "alpha", "samples.jsonl"))))
    # `cu.ship.stamp`'s trick, one hop on: strip the suffix and the client's
    # own manifest bytes are back.
    client_bytes = json.dumps(man, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8")
    spliced = b',"recv":' + json.dumps(
        kept["recv"], sort_keys=True, separators=(",", ":")).encode("utf-8")
    check("door: the manifest is spliced, not re-serialised",
          raw.replace(spliced, b""), client_bytes)

    batches, problems = serve.read_batches(root, fx.uuid_of("alpha"))
    check("door: read_batches reports no problem", problems, [])
    check("door: and hands back one batch", len(batches), 1)
    ing = ingest.ingest(batches, fx.NOW_JOINT)
    check("door: every real sample survives the round trip",
          ing.counts["samples_accepted"], len(lines))
    check("door: carrying the shipping host, which v1 could not carry itself",
          sorted({h for _r, h in ing.samples_for(fx.uuid_of("alpha"))}),
          ["darwin"])


def test_the_door_feeds_reconcile_unchanged():
    """Door -> disk -> read_batches -> ingest -> reconcile, on real bytes.

    The seam is asserted end to end because a door that stores something the
    core cannot read is a failure nobody sees until a report is empty.
    """
    door, store, root = door_for()
    for label in ("alpha", "beta"):
        for _m, _l, body in (ship_a(label), ship_b(label)):
            status, _ = door.ship("tok-" + label, serve.CONTENT_TYPE, None,
                                  body, now=fx.NOW_JOINT)
            check("door: %s batch accepted" % label, status, 200)

    for label in ("alpha", "beta"):
        uuid = fx.uuid_of(label)
        batches, problems = serve.read_batches(root, uuid)
        check("door: %s reads back with no problem" % label, problems, [])
        rep = reconcile.reconcile_ingest(
            ingest.ingest(batches, fx.NOW_JOINT), fx.NOW_JOINT)
        got = rep.accounts[uuid]
        want_rows = len([r for r in fx.stamped_rows()
                         if r["account_uuid"] == uuid])
        check("door: %s keeps every request" % label, got.requests_n, want_rows)
        check("door: %s reconstructs its real 5-hour window" % label,
              [w.resets_at for w in got.windows if w.kind == "5h"],
              [fx.JOINT_5H_RESETS[label]])
        check("door: %s reaches its real peak" % label,
              win(got, "5h", fx.JOINT_5H_RESETS[label]).peak,
              fx.JOINT_5H_PEAKS[label])
        check("door: and only its own account is in the report",
              sorted(rep.accounts), [uuid])


def test_the_token_names_the_tenant():
    """Requirement 2, and it is a WHOLE-batch verdict.

    A batch is one byte range of one file on one machine, so a record naming
    another account means the shipper aimed the file at the wrong destination
    -- and the agreeing records in it are no more trustworthy than the
    disagreeing one.  Accepting the good half is how one account's data ends up
    in another account's directory.
    """
    door, store, root = door_for()
    _man, lines, body = ship_a("alpha")
    status, ack = door.ship("tok-beta", serve.CONTENT_TYPE, None, body,
                            now=fx.NOW_JOINT)
    check("door: a payload identity that disagrees with the token is 409",
          status, 409)
    check("door: named", ack.get("reason"), "identity")
    check("door: nothing is written for the token's tenant",
          stored(root, "beta", "ledger.jsonl"), None)
    check("door: nor for the payload's", stored(root, "alpha", "ledger.jsonl"),
          None)
    check("door: and nothing is held", store.held(fx.uuid_of("beta"), _man),
          (0, None))

    # One disagreeing record among many, which is the shape that tempts
    # partial acceptance.
    good = fx.real_sample_lines("beta")
    mixed = good + [fx.real_sample_lines("alpha")[0]]
    man = fx.manifest(wire.STREAM_SAMPLES, mixed, to=len(mixed) * 200)
    status, ack = door.ship("tok-beta", serve.CONTENT_TYPE, None,
                            ndjson(man, mixed), now=fx.NOW_JOINT)
    check("door: one disagreeing record fails the whole batch", status, 409)
    check("door: even the agreeing records are not written",
          stored(root, "beta", "samples.jsonl"), None)
    check("door: refusals are counted by name",
          store.counters.snapshot()["batches_refused"], {"identity": 2})


def test_a_record_naming_no_account_is_the_cores_rule_not_a_stricter_one():
    """Absent is not disagreement, and the door must not out-strict the core.

    `ingest.add_batch` lets a record with no `account_uuid` through the
    batch-level check and refuses it by name one line later
    (`wire.check` -> `no-account-uuid`), where it is counted in the report.  If
    the door 409'd it instead, a shipper that forgot to stamp would jam its own
    offset for ever against a server that is not the one deciding, and a batch
    the core would accept would never reach it.
    """
    door, store, root = door_for()
    lines = fx.real_sample_lines("alpha")
    unstamped = dict(json.loads(lines[0]))
    del unstamped["account_uuid"]
    raw = json.dumps(unstamped, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    recs = lines + [raw]
    man = fx.manifest(wire.STREAM_SAMPLES, recs, to=len(recs) * 200)
    status, _ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None,
                             ndjson(man, recs), now=fx.NOW_JOINT)
    check("door: a record naming no account is accepted at the door", status, 200)
    batches, _p = serve.read_batches(root, fx.uuid_of("alpha"))
    ing = ingest.ingest(batches, fx.NOW_JOINT)
    check("door: and refused by name downstream, where it is counted",
          [r.reason for r in ing.refusals], ["no-account-uuid"])


def test_a_resend_is_idempotent():
    """If the client does not get a 200 it must be safe to resend the bytes.

    Two flavours, and both have to hold.  A range this door provably already
    holds is skipped, so the file does not grow; a range it does NOT know it
    holds -- the crash between the append and the offset write -- is appended
    again and deduped by the core, so the report is identical either way.  The
    second is the one that matters: the file growing is visible and cheap,
    while refusing to write something already written would be a wager on an
    offset that had not survived.
    """
    door, store, root = door_for()
    man, lines, body = ship_a("alpha")
    door.ship("tok-alpha", serve.CONTENT_TYPE, None, body, now=fx.NOW_JOINT)
    first = stored(root, "alpha", "ledger.jsonl")

    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None, body,
                            now=fx.NOW_JOINT)
    check("door: the same range again is 200, not an error", status, 200)
    check("door: named as the duplicate it is", ack["duplicate"], True)
    check("door: nothing was appended", stored(root, "alpha", "ledger.jsonl"),
          first)
    check("door: the ack still carries the held offset", ack["offset"], man["to"])
    check("door: and it is counted under its own name",
          store.counters.snapshot()["batches_skipped_as_held"], 1)

    # The crash between the append and the offset write: the offset the client
    # is told about never landed, so the client resends and the door does not
    # know it is a resend.
    store.save_offsets(fx.uuid_of("alpha"), {})
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None, body,
                            now=fx.NOW_JOINT)
    check("door: an unrecognised resend is written rather than gambled on",
          (status, ack["duplicate"]), (200, False))
    check("door: so the file does grow", len(stored(root, "alpha", "ledger.jsonl")),
          2 * len(first))
    batches, problems = serve.read_batches(root, fx.uuid_of("alpha"))
    ing = ingest.ingest(batches, fx.NOW_JOINT)
    check("door: and the core deduplicates it away completely",
          (ing.counts["ledger_accepted"], ing.counts["ledger_duplicates"],
           ing.counts["ledger_collisions"]),
          (len(lines), len(lines), 0))
    check("door: read_batches reports no problem over the duplicate", problems, [])


def test_a_torn_line_costs_one_line_and_never_two():
    """Partial-write recovery.

    A file whose last byte is not a newline is a batch interrupted mid-write --
    the only way to get one, since every append here is fsynced before anything
    is acknowledged.  The fragment is terminated BEFORE the next batch goes on,
    so the damage stays inside the torn line instead of gluing it onto the
    front of a good record and costing two.  Nothing is lost either way: that
    batch was never acknowledged, so the client still holds the range.
    """
    door, store, root = door_for()
    man, lines, body = ship_a("alpha")
    os.makedirs(os.path.join(root, "accounts", fx.uuid_of("alpha")),
                mode=0o700, exist_ok=True)
    fragment = lines[0][:40]
    with open(os.path.join(root, "accounts", fx.uuid_of("alpha"),
                           "ledger.jsonl"), "wb") as fh:
        fh.write(fragment)

    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None, body,
                            now=fx.NOW_JOINT)
    check("door: the batch after a torn write is accepted", status, 200)
    check("door: and says the tail was terminated", ack["torn_line_terminated"],
          True)
    check("door: counted", store.counters.snapshot()["torn_lines_terminated"], 1)
    blob = stored(root, "alpha", "ledger.jsonl")
    check("door: the fragment is closed off, not glued to the next record",
          blob.split(b"\n")[0], fragment)
    check("door: and every new record is intact",
          blob.split(b"\n")[1:-1], lines)

    batches, problems = serve.read_batches(root, fx.uuid_of("alpha"))
    check("door: the manifest's range skips the fragment", problems, [])
    ing = ingest.ingest(batches, fx.NOW_JOINT)
    check("door: so the torn line costs nothing but itself",
          ing.counts["ledger_accepted"], len(lines))


def test_gzip_is_the_same_bytes():
    door, store, root = door_for()
    _man, lines, body = ship_a("alpha")
    packed = gzip.compress(body)
    check_true("door: the fixture actually compresses", len(packed) < len(body))
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, "gzip", packed,
                            now=fx.NOW_JOINT)
    check("door: a gzipped body is accepted", status, 200)
    check("door: and lands as the identical bytes",
          stored(root, "alpha", "ledger.jsonl"),
          b"".join(l + b"\n" for l in lines))

    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, "deflate", body,
                            now=fx.NOW_JOINT)
    check("door: an encoding it does not speak is refused, not guessed at",
          (status, ack.get("reason")), (415, "unsupported-content-encoding"))
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, "gzip",
                            b"not gzip at all", now=fx.NOW_JOINT)
    check("door: an undecodable gzip body is named",
          (status, ack.get("reason")), (400, "gzip-undecodable"))


def test_an_oversized_body_is_refused_before_it_is_expanded():
    """Both caps, and the second is the one that matters.

    "The network is trusted" is a statement about who is listening, not a
    reason to let a 200-byte request allocate a gigabyte: gzip's ceiling ratio
    turns the wire cap into ~1000x of memory, so the expansion is capped
    separately and the body is never fully decompressed to find out.
    """
    _man, _lines, body = ship_a("alpha")
    packed = gzip.compress(body)
    # The wire cap admits the compressed body and refuses the raw one, so the
    # two limits are exercised apart rather than one standing in for both.
    door, store, root = door_for(max_body=len(packed) + 16,
                                 max_decompressed=len(body) - 1)
    check_true("door: the raw fixture is over the wire cap",
               len(body) > door.max_body)
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None, body,
                            now=fx.NOW_JOINT)
    check("door: an oversized body is 413", (status, ack.get("reason")),
          (413, "body-too-large"))
    check("door: and nothing is written", stored(root, "alpha", "ledger.jsonl"),
          None)

    check_true("door: while it fits under the wire cap once compressed",
               len(packed) <= door.max_body)
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, "gzip", packed,
                            now=fx.NOW_JOINT)
    check("door: the expansion is capped too", (status, ack.get("reason")),
          (413, "body-too-large"))
    check("door: still nothing written", stored(root, "alpha", "ledger.jsonl"),
          None)

    # And the cap is enforced DURING the expansion, not after it.  Refusing a
    # 64 MiB body once it is already a 64 MiB `bytes` is a refusal that has
    # already paid the whole cost, which is the only cost a zip bomb has.  This
    # is asserted on the allocation rather than on the status, because the
    # status is identical either way -- which is exactly why it would rot.
    bomb = gzip.compress(b"\0" * (64 * 1024 * 1024))
    door.max_body = len(bomb) + 16
    door.max_decompressed = 1024 * 1024
    tracemalloc.start()
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, "gzip", bomb,
                            now=fx.NOW_JOINT)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    check("door: a bomb is refused", (status, ack.get("reason")),
          (413, "body-too-large"))
    check_true("door: having expanded only as far as the cap (peak %.1f MiB "
               "of a 64 MiB body)" % (peak / 1048576.0), peak < 16 * 1024 * 1024)


def test_a_nearly_full_disk_refuses_rather_than_fills():
    """507, and the free bytes in EVERY ack.

    A store that fills silently loses every record shipped afterwards; one that
    refuses loudly loses none, because an unacknowledged batch stays on the
    machine that made it.  The number is in the ack on the way up as well as at
    the refusal, because a figure that only appears once it is too late is not
    a warning.
    """
    door, store, root = door_for()
    _man, lines, body = ship_a("alpha")

    class _Full(object):
        f_bavail, f_frsize = 4, 1024        # 4 KiB free

    store.statvfs = lambda _p: _Full()
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None, body,
                            now=fx.NOW_JOINT)
    check("door: a batch that would eat the reserve is 507",
          (status, ack.get("reason")), (507, "disk-nearly-full"))
    check("door: the refusal says how much is left", ack["disk_free_bytes"], 4096)
    check("door: and nothing is written", stored(root, "alpha", "ledger.jsonl"),
          None)
    check("door: counted by name",
          store.counters.snapshot()["batches_refused"], {"disk-nearly-full": 1})

    store.statvfs = os.statvfs
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None, body,
                            now=fx.NOW_JOINT)
    check("door: and the identical bytes land once there is room again",
          (status, stored(root, "alpha", "ledger.jsonl")),
          (200, b"".join(l + b"\n" for l in lines)))

    class _Unaskable(object):
        pass

    def _boom(_p):
        raise OSError("no statvfs here")

    store.statvfs = _boom
    ok, free = store.room_for(1)
    check("door: a store that cannot ask about free space keeps accepting",
          (ok, free), (True, None))


def test_every_other_refusal_is_named():
    """The reason strings are the API; a 400 with no name is a mystery."""
    door, store, root = door_for()
    man, lines, body = ship_a("alpha")

    cases = [
        ("no token at all", None, serve.CONTENT_TYPE, None, body,
         401, "no-bearer-token"),
        ("a token nobody issued", "tok-nope", serve.CONTENT_TYPE, None, body,
         401, "unknown-token"),
        ("the wrong content type", "tok-alpha", "application/json", None, body,
         415, "unsupported-media-type"),
        ("an empty body", "tok-alpha", serve.CONTENT_TYPE, None, b"   ",
         400, "body-empty"),
        ("a manifest that is not JSON", "tok-alpha", serve.CONTENT_TYPE, None,
         b"{oops\n" + lines[0] + b"\n", 400, "manifest-unparseable"),
        ("a manifest with no host", "tok-alpha", serve.CONTENT_TYPE, None,
         ndjson({k: v for k, v in man.items() if k != "host"}, lines),
         400, "manifest-no-host"),
        ("a ship version from the future", "tok-alpha", serve.CONTENT_TYPE, None,
         ndjson(dict(man, ship=99), lines), 400, "ship-version-unsupported"),
        ("a stream nobody serves", "tok-alpha", serve.CONTENT_TYPE, None,
         ndjson(dict(man, stream="prompts"), lines), 400, "unknown-stream"),
        ("a count that disagrees with the body", "tok-alpha", serve.CONTENT_TYPE,
         None, ndjson(dict(man, count=len(lines) + 1), lines),
         400, "manifest-count-mismatch"),
        ("a blank line among the records", "tok-alpha", serve.CONTENT_TYPE, None,
         ndjson(man, lines[:1] + [b""] + lines[1:]), 400, "body-blank-line"),
        ("a record that is not JSON", "tok-alpha", serve.CONTENT_TYPE, None,
         ndjson(dict(man, count=2), [lines[0], b"{tor"]),
         400, "record-unparseable"),
        ("a record that is not an object", "tok-alpha", serve.CONTENT_TYPE, None,
         ndjson(dict(man, count=2), [lines[0], b"[1,2]"]),
         400, "record-not-an-object"),
    ]
    for name, tok, ctype, enc, payload, want_status, want_reason in cases:
        status, ack = door.ship(tok, ctype, enc, payload, now=fx.NOW_JOINT)
        check("door: %s -> %s" % (name, want_reason),
              (status, ack.get("reason")), (want_status, want_reason))
    check("door: not one of them wrote a byte",
          stored(root, "alpha", "ledger.jsonl"), None)
    check("door: every refusal carries the free bytes a monitor watches",
          sorted({type(door.ship(t, c, e, p, now=fx.NOW_JOINT)[1]
                       ["disk_free_bytes"]).__name__
                  for _n, t, c, e, p, _s, _r in cases}), ["int"])


def test_an_unusable_tenant_mapping_is_refused_by_name():
    """The tenant becomes a directory name, so it is validated once, at load.

    It comes from the operator's own file rather than from a payload, which is
    why this is a refusal with a message and not a 409: a hand-edited
    `"../../etc"` is a typo to fix, and a door that silently ignored it would
    answer 401 to a machine whose token is right there in the file.
    """
    store = serve.Store(door_root())
    tenants = serve.Tenants(mapping={
        "good": fx.uuid_of("alpha"),
        "traverse": "../../etc",
        "dotted": ".hidden",
        "empty": "",
        "": fx.uuid_of("beta"),
        "not-a-string": 7,
    })
    check("door: only the usable mapping survives", tenants.tenants(),
          [fx.uuid_of("alpha")])
    check("door: and every refusal says which entry and why",
          len(tenants.refused), 5)
    door = serve.Door(store, tenants)
    check("door: a refused mapping's token is simply unknown",
          door.ship("traverse", serve.CONTENT_TYPE, None, b"{}",
                    now=fx.NOW_JOINT)[1]["reason"], "unknown-token")
    check("door: and health surfaces the refusals rather than hiding them",
          len(door.health()["tokens_refused"]), 5)
    check_true("door: no token value is ever echoed",
               all("traverse" not in json.dumps(r)
                   for r in door.health()["tokens_refused"]))


def test_a_rotated_client_file_resets_the_offset_rather_than_orphaning_it():
    """`tail.start_offset`'s rule, from the other end.

    The offset is keyed on machine+stream+filename and the file's IDENTITY is
    the value, so a rotation resets that key instead of leaving a dead one
    behind for every rotation the client ever does.  It also means the
    duplicate skip above can only fire on a file the client has proven is the
    same file.
    """
    door, store, root = door_for()
    man, lines, body = ship_a("alpha")
    door.ship("tok-alpha", serve.CONTENT_TYPE, None, body, now=fx.NOW_JOINT)
    check("door: the offset is held", store.held(fx.uuid_of("alpha"), man),
          (man["to"], True))

    rotated = dict(man, ident={"dev": 1, "ino": 2, "head": "ffff"})
    check("door: a different identity is not the same file",
          store.held(fx.uuid_of("alpha"), rotated), (man["to"], False))
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None,
                            ndjson(rotated, lines), now=fx.NOW_JOINT)
    check("door: so the same range is written again rather than skipped",
          (status, ack["duplicate"], ack["ident_matches"]), (200, False, False))
    check("door: while a first shipment is told 'nothing on record', not 'no'",
          serve.Store(door_root()).held(fx.uuid_of("alpha"), man), (0, None))
    check("door: and the file grew", len(stored(root, "alpha", "ledger.jsonl")),
          2 * sum(len(l) + 1 for l in lines))


def test_a_range_below_the_held_offset_is_appended_whole_and_counted():
    """An overlap is never trimmed, because trimming needs the file parsed.

    Deciding which LINES fall inside an overlapping byte range means reading
    the stored file back and interpreting it, which is the one thing this door
    does not do.  The duplicate half costs one dedupe downstream and is counted
    here, so it is a number rather than a mystery in a file size.

    And the mirror image, stated because it is NOT an alarm: a `from` above
    what the door holds is normal.  `cu.ship._ship_stream` advances its offset
    over a byte range that held only other accounts' rows without posting
    anything, so a forward jump is the everyday state of a multi-account
    ledger.  Calling it a gap would be a warning written from an assumption.
    """
    door, store, root = door_for()
    man, lines, body = ship_a("alpha")
    door.ship("tok-alpha", serve.CONTENT_TYPE, None, body, now=fx.NOW_JOINT)
    over = dict(man, **{"from": man["from"], "to": man["to"] + 10})
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None,
                            ndjson(over, lines), now=fx.NOW_JOINT)
    check("door: an overlapping range is accepted whole and said so",
          (status, ack["overlapped"]), (200, True))
    check("door: counted", store.counters.snapshot()["ranges_overlapping"], 1)
    check("door: the offset moves to the far end", ack["offset"], man["to"] + 10)

    ahead = dict(man, **{"from": man["to"] + 1000, "to": man["to"] + 1200})
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None,
                            ndjson(ahead, lines), now=fx.NOW_JOINT)
    check("door: a forward jump is not an alarm",
          (status, ack["overlapped"]), (200, False))
    check("door: it is still counted as one overlap and no more",
          store.counters.snapshot()["ranges_overlapping"], 1)


def test_an_interrupted_batch_is_named_rather_than_shortened():
    """`read_batches` returns problems; it never quietly yields a short batch.

    A manifest whose byte range is not fully on disk is a batch that was
    interrupted -- its client never got an ack and resent it, so its records
    arrive again under a later manifest.  Yielding the fragment as if it were
    the batch would be a count nobody could question.
    """
    door, store, root = door_for()
    _man, lines, body = ship_a("alpha")
    door.ship("tok-alpha", serve.CONTENT_TYPE, None, body, now=fx.NOW_JOINT)
    path = os.path.join(root, "accounts", fx.uuid_of("alpha"), "ledger.jsonl")
    with open(path, "r+b") as fh:
        fh.truncate(os.path.getsize(path) - 20)
    batches, problems = serve.read_batches(root, fx.uuid_of("alpha"))
    check("door: an interrupted batch is named", [p[0] for p in problems],
          ["interrupted-batch"])
    check("door: and not handed over as a shorter one", batches, [])


def test_a_client_that_says_it_restarted_a_file_is_not_told_it_was_held():
    """The discard the door used to answer 200 to, and the signal that stops it.

    `cu.ship._ship_stream` detects `start > size` -- truncated under us -- and
    re-reads from zero.  The file IDENTITY is dev + ino + a hash of the first
    256 bytes, and an in-place rewrite (`open(path, "w")`, which is what any
    hand-rolled pruning does) keeps dev and ino, while one surviving head
    record keeps the hash.  So the client posted [0, N) with an ident the door
    recognised, the door held prev >> N, and the whole batch was dropped on the
    floor with a success status -- and the client advanced its own offset on
    that 200.  Reproduced against the door: resend [0,200) carrying a genuinely
    different record -> `200 accepted 0 duplicate True offset 500`, and the new
    record was not in `ledger.jsonl`.

    Same evidence, opposite failure direction: on the client re-reading costs a
    parse, here discarding costs the records.  So the client says so, and the
    door appends and lets `request_id`/`sample_key` dedupe.
    """
    door, store, root = door_for()
    _man, lines, body = ship_a("alpha")
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None, body,
                            now=fx.NOW_JOINT)
    held = ack["offset"]
    check("door: the first batch lands", (status, ack["accepted"] > 0),
          (200, True))

    # A genuinely different record, in a range the door already holds, from a
    # file whose identity is unchanged -- an in-place rewrite.
    fresh = dict(json.loads(lines[0]), request_id="r-after-the-rewrite")
    raw = ui_line(fresh)
    same_ident = fx.manifest(wire.STREAM_LEDGER, [raw], offset=0,
                             to=len(raw) + 1)
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None,
                            ndjson(same_ident, [raw]), now=fx.NOW_JOINT)
    check("door: with no reset claimed, a held range is still skipped",
          (status, ack["duplicate"], ack["offset"]), (200, True, held))
    check_true("door: ...and the record really is not on disk",
               b"r-after-the-rewrite" not in (stored(root, "alpha",
                                                     "ledger.jsonl") or b""))
    check("door: the skip is counted under its own name, never as a duplicate",
          store.counters.snapshot()["batches_skipped_as_held"], 1)

    # The same bytes, with the signal the client already computed.
    reset_man = fx.manifest(wire.STREAM_LEDGER, [raw], offset=0,
                            to=len(raw) + 1, reset=True)
    status, ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None,
                            ndjson(reset_man, [raw]), now=fx.NOW_JOINT)
    check("door: a claimed restart is appended, not skipped",
          (status, ack["duplicate"], ack["accepted"]), (200, False, 1))
    check_true("door: ...so the record is on disk",
               b"r-after-the-rewrite" in stored(root, "alpha", "ledger.jsonl"))
    check("door: and the held offset does not go backwards",
          store.held(fx.uuid_of("alpha"), reset_man)[0], held)
    check("door: the skip counter did not move for the accepted batch",
          store.counters.snapshot()["batches_skipped_as_held"], 1)

    # The core deduplicates the overlap away, which is what makes appending the
    # safe direction rather than a trade.
    batches, problems = serve.read_batches(root, fx.uuid_of("alpha"))
    check("door: the store reads back with no problem", problems, [])
    ing = ingest.ingest(batches, fx.NOW_JOINT)
    check("door: and the extra record is the only new row",
          ing.counts["ledger_accepted"], len(lines) + 1)

    # `reset` is a value a decision rests on, so it is validated like one.
    ok, why = wire.manifest_check(dict(reset_man, reset="yes"))
    check("wire: a non-boolean reset is refused by name", (ok, why),
          (False, "manifest-reset-not-a-boolean"))
    ok, why = wire.manifest_check(_man)
    check("wire: and a manifest with no reset key at all is still accepted",
          (ok, why), (True, None))


def test_stream_c_reaches_its_own_file():
    """The third stream has a file too, and it is not the other two's.

    Nothing on any machine emits an attestation yet -- `fx.attestation` is
    AUTHORED and says so -- but the door either routes all three streams or it
    routes two and silently mixes the third.
    """
    door, store, root = door_for()
    att = fx.attestation(fx.uuid_of("alpha"), "5h", fx.JOINT_5H_RESETS["alpha"],
                         "darwin", [(fx.JOINT_5H_RESETS["alpha"] - 18000,
                                     fx.JOINT_5H_RESETS["alpha"], True)],
                         emitted_at=fx.NOW_JOINT - 100)
    raw = json.dumps(att, sort_keys=True, separators=(",", ":")).encode("utf-8")
    man = fx.manifest(wire.STREAM_ATTEST, [raw], to=len(raw) + 1)
    status, _ack = door.ship("tok-alpha", serve.CONTENT_TYPE, None,
                             ndjson(man, [raw]), now=fx.NOW_JOINT)
    check("door: an attestation batch is accepted", status, 200)
    check("door: into its own file", stored(root, "alpha", "attestations.jsonl"),
          raw + b"\n")
    check("door: and not into either of the others",
          (stored(root, "alpha", "ledger.jsonl"),
           stored(root, "alpha", "samples.jsonl")), (None, None))
    batches, _p = serve.read_batches(root, fx.uuid_of("alpha"))
    ing = ingest.ingest(batches, fx.NOW_JOINT)
    check("door: and the core places it", ing.counts["attestations_accepted"], 1)


def test_the_door_speaks_the_shippers_words():
    """Drift between the two sides is a test failure, never a discovery."""
    from cu import ship as cuship
    check("door: the content type is the shipper's", serve.CONTENT_TYPE,
          cuship.CONTENT_TYPE)
    check("door: the path is the one the shipper is configured with",
          serve.PATH_SHIP, "/v1/ship")
    check("door: every stream the wire defines has a file",
          tuple(sorted(serve.STREAM_FILES)), tuple(sorted(wire.STREAMS)))
    check("door: and no two streams share one",
          len(set(serve.STREAM_FILES.values())), len(wire.STREAMS))
    check("door: the shipper's manifest passes the door's manifest check",
          wire.manifest_check(cuship.manifest(
              wire.STREAM_LEDGER, "/x/ledger.jsonl",
              {"dev": 1, "ino": 2, "head": "ab"}, 0, 10, 1, "darwin", "m-1",
              fx.NOW_JOINT)),
          (True, None))


def test_the_door_serves_http():
    """The adapter, over a real socket.

    Everything above calls `Door.ship` directly, which is where the policy is;
    this is the one test that proves the handler in front of it reads the
    headers it claims to, answers on the path it claims to, and does not
    corrupt a body on the way in.
    """
    root = door_root()
    counters = serve.Counters()
    store = serve.Store(root, counters=counters)
    tenants = serve.Tenants(mapping={"tok-alpha": fx.uuid_of("alpha")})
    serve.ShipHandler.door = serve.Door(store, tenants, counters=counters)
    serve.ShipHandler.quiet = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.ShipHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def post(body, token="tok-alpha", ctype=serve.CONTENT_TYPE, enc=None,
             path=serve.PATH_SHIP):
        headers = {"Content-Type": ctype}
        if token:
            headers["Authorization"] = "Bearer " + token
        if enc:
            headers["Content-Encoding"] = enc
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                     data=body, method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as fh:
                return fh.status, json.loads(fh.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        _man, lines, body = ship_a("alpha")
        status, ack = post(gzip.compress(body), enc="gzip")
        check("door/http: a gzipped POST over the wire is accepted", status, 200)
        check("door/http: with every record", ack["accepted"], len(lines))
        check("door/http: and the bytes are the shipper's",
              stored(root, "alpha", "ledger.jsonl"),
              b"".join(l + b"\n" for l in lines))

        status, ack = post(body, token="tok-nope")
        check("door/http: an unknown token is 401", (status, ack.get("reason")),
              (401, "unknown-token"))
        status, ack = post(body, path="/v1/anything")
        check("door/http: another path is 404", (status, ack.get("reason")),
              (404, "not-found"))

        # The cap is enforced before the body is read, so this must not hang.
        serve.ShipHandler.door.max_body = 32
        status, ack = post(body)
        check("door/http: an oversized body is refused without being read",
              (status, ack.get("reason")), (413, "body-too-large"))
        serve.ShipHandler.door.max_body = serve.MAX_BODY

        with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port,
                                    timeout=10) as fh:
            health = json.loads(fh.read())
            server_hdr = fh.headers.get("Server")
        check("door/http: healthz counts what landed", health["records_written"],
              len(lines))

        # The `Server` header, compared EXACTLY.  BaseHTTPRequestHandler joins
        # `server_version` and `sys_version` with a space unconditionally, so
        # the empty `sys_version` that suppresses the "Python/3.x" half left
        # `claudio-door/1 ` -- with a trailing space -- on every response this
        # door has ever sent, refusals and acks alike.  Harmless on the wire
        # and permanent in every capture and bug report that quotes it.
        #
        # An exact comparison and not a `startswith`, which the trailing space
        # passes: the defect IS the suffix, so a check that ignores suffixes
        # cannot see it.
        check("door/http: the Server header is exactly the door's name",
              server_hdr, "claudio-door/%d" % serve.DOOR_VERSION)
        check("door/http: and names the refusals",
              health["batches_refused"], {"unknown-token": 1, "not-found": 1,
                                          "body-too-large": 1})
        check_true("door/http: and reports the free bytes",
                   isinstance(health["disk_free_bytes"], int))
    finally:
        httpd.shutdown()
        httpd.server_close()


# ================================================ 9. THE QUERY LAYER ========
#
# `srv/query.py`.  Pure, like everything but the door: rows in, answers out.
# Every number below was read off the real capture before the assertion was
# written, and the reading is shown.
#
#   8 real stream-A rows, 3 accounts   agent 3, alpha 3, beta 2
#   query_source across all 8          generate_session_title 3,
#                                      repl_main_thread 3, prompt_suggestion 1,
#                                      sdk 1
#   models                             claude-sonnet-5 5, claude-haiku-4-5 3
#   terminal_type                      iTerm.app 7, xterm-256color 1
#
#   $ jq -r .query_source usage/tests/fixtures/real-api-request.jsonl  (via
#     cu.otlp.rows_from_payload; the file is OTLP payloads, not ledger rows)

QROWS = None            # the 8 stamped rows, built once
QREP = None             # the joint report over them


def _qrows():
    global QROWS
    if QROWS is None:
        QROWS = fx.stamped_rows()
    return QROWS


def _qrep():
    global QREP
    if QREP is None:
        QREP = joint()
    return QREP


def _qsel(spec, rows=None):
    q = query.compile_query(spec)
    if query.refused(q):
        return q
    return query.select(q, _qrows() if rows is None else rows)


def test_q_the_column_taxonomy_is_exhaustive_and_read_off_the_capture():
    """Every ledger column is classified exactly once, and the
    NEVER-OBSERVED set is DERIVED from the fixture rather than believed.

    This is the `agent_summary` guard, mechanised.  Seven columns are read by
    `cu.otlp.rows_from_payload` and are null in all 8 real rows -- a facet
    built on one of them renders an empty control that reads as "I have never
    used an MCP tool" rather than "this attribute has never been observed".
    The day a capture carries one, this fails and the constant has to be edited
    in the same commit as the fixture.
    """
    classes = (query.OBSERVED_TEXT + query.OBSERVED_NUMERIC + query.ID_FIELDS
               + query.STRUCTURED_FIELDS + query.NEVER_OBSERVED)
    check("query: every ledger column is classified exactly once",
          sorted(classes), sorted(query.LEDGER_COLUMNS))
    check("query: and no column is classified twice",
          len(classes), len(set(classes)))

    rows = _qrows()
    derived = sorted(c for c in query.LEDGER_COLUMNS
                     if all(r.get(c) is None for r in rows))
    check("query: NEVER_OBSERVED is exactly what the capture never carries",
          derived, sorted(query.NEVER_OBSERVED))

    # And the type claims are checked against the same rows, so a column
    # classified numeric because its name sounds numeric fails here.
    bad = []
    for r in rows:
        for c in query.OBSERVED_NUMERIC:
            if r.get(c) is not None and not isinstance(r[c], (int, float)):
                bad.append(c)
        for c in query.OBSERVED_TEXT:
            if r.get(c) is not None and not isinstance(r[c], str):
                bad.append(c)
    check("query: every observed-numeric column is a number in the capture, "
          "and every observed-text column a string", sorted(set(bad)), [])


def test_q_the_overhead_deny_list_is_the_readers_and_every_name_is_captured():
    """Repeated from `cu.config`, never guessed, and pinned in both
    directions.

    Six invented names once matched nothing while reading as coverage, and
    `agent_summary` after them.  So: the two copies must agree, AND every name
    must appear in a captured payload.
    """
    from cu import config as cu_config
    check("query: the overhead deny-list matches the reader's",
          sorted(query.OVERHEAD_SOURCES), sorted(cu_config.OVERHEAD_SOURCES))
    seen = {r.get("query_source") for r in _qrows()}
    check("query: and every name in it appears in the real capture",
          sorted(n for n in query.OVERHEAD_SOURCES if n not in seen), [])


def test_q_three_empty_answers_are_three_different_answers():
    """`no-data`, `filtered-to-nothing`, and a refusal.

    `show --by nonsense` groups every row under `(none)` and prints a tidy
    one-row table -- a refusal wearing an answer's clothes.  Here the three are
    different objects with different fields, and the filtered case names the
    clause that did it.
    """
    empty = _qsel({"where": {"model": "claude-sonnet-5"}}, rows=[])
    check("query: an empty corpus is no-data", empty.empty_because, "no-data")
    check("query: and nothing is blamed for it", empty.sole_cause, None)

    none = _qsel({"where": {"model": "claude-opus-nonexistent"}})
    check("query: a query that matched nothing is filtered-to-nothing",
          none.empty_because, "filtered-to-nothing")
    check("query: over rows that were really scanned", none.scanned, 8)
    check("query: and the clause that emptied it is named",
          none.sole_cause, "model in ['claude-opus-nonexistent']")

    bad = query.compile_query({"where": {"nonsense": 1}})
    check_true("query: an unknown column REFUSES rather than grouping "
               "everything under (none)", query.refused(bad))
    check("query: by name", bad.reason, "unknown-column")
    check_true("query: and the remedy lists the real columns",
               "query_source" in (bad.remedy or ""))


def test_q_filtered_to_nothing_names_which_clause_when_it_can_and_says_so_when_it_cannot():
    """An intersection of six facets that returns nothing is otherwise six
    equally plausible suspects."""
    # haiku is 3 of the 8 rows and repl_main_thread is 3, and no row is both:
    # every haiku request in the capture is a generate_session_title, and every
    # repl_main_thread one is sonnet.  So each clause eliminates 5 and neither
    # eliminates all 8.
    sel = _qsel({"where": {"model": "claude-haiku-4-5",
                           "query_source": "repl_main_thread"}})
    check("query: an empty intersection of two satisfiable clauses",
          sel.empty_because, "filtered-to-nothing")
    check("query: no single clause is blamed", sel.sole_cause, None)
    check_true("query: and the note says the intersection is what is empty",
               any("intersection" in n for n in sel.notes))
    check("query: with the per-clause elimination counts kept",
          sorted(sel.eliminated.values()), [5, 5])


def test_q_never_sum_across_accounts():
    """The rule that outranks every convenience in this module.

    Three real accounts in one row set.  A breakdown over them refuses; a
    breakdown BY account_uuid does not, because each bucket is one account and
    there is no total row anywhere in the module.
    """
    rows = _qrows()
    r = query.breakdown(rows, "model", report=_qrep())
    check_true("query: a breakdown over three accounts refuses",
               query.refused(r))
    check("query: by name", r.reason, "crosses-accounts")
    check_true("query: naming all three", r.detail.count("-") >= 3)

    ok = query.breakdown(rows, "account_uuid", report=_qrep())
    check_true("query: grouping BY the account identity is allowed",
               not query.refused(ok))
    check("query: one bucket per account, and no total row",
          sorted(len(ok.buckets) for _ in [0]), [3])

    one = query.breakdown(rows, "model", report=_qrep(),
                          account_uuid=fx.uuid_of("alpha"))
    check_true("query: and one named account is answerable",
               not query.refused(one))
    check("query: over that account's rows only", one.matched, 3)


def test_q_grouping_by_a_label_across_accounts_is_refused_by_its_own_name():
    """`account` is what `--tag account=` put there and anyone can overwrite
    it; `email` survives in `.claude.json` after a login is orphaned.

    Two accounts sharing a label would silently become one bucket, which is the
    cross-account sum arriving through the back door.  Inside one account both
    are perfectly good keys, so this is a cross-account refusal and not a ban.
    """
    for col in ("account", "email"):
        r = query.breakdown(_qrows(), col, report=_qrep())
        check("query: grouping across accounts by `%s` is refused" % col,
              getattr(r, "reason", None), "label-is-not-an-identity")
        check_true("query: and it says the label is overwritable (%s)" % col,
                   "account_uuid" in (r.remedy or ""))
    inside = query.breakdown(_qrows(), "account", report=_qrep(),
                             account_uuid=fx.uuid_of("beta"))
    check("query: inside one account the label groups fine",
          sorted(inside.buckets), ["beta"])


def test_q_every_aggregate_carries_its_coverage():
    """An attributed figure without coverage is the confident-wrong-number
    failure: a window nobody watched and a window with a browser session behind
    it produce the same residual.

    Today NOTHING emits an attestation, so the honest value is None with a
    basis that says so -- never 0.0, which is the claim "nobody was listening".
    """
    b = query.breakdown(_qrows(), "query_source", report=_qrep(),
                        account_uuid=fx.uuid_of("alpha"))
    cov = b.coverage
    check("query: the breakdown carries a coverage statement",
          cov.__class__.__name__, "CoverageStatement")
    check("query: with an unknown fraction, not a zero", cov.fraction, None)
    check("query: and a basis that says why", cov.basis, "no-attestation")
    check_true("query: in words as well",
               any("unknown, not zero" in n for n in cov.notes))
    # alpha's 3 rows all fall inside its one closed 5-hour window
    # (1786598400 - 18000 = 1786580400 <= 1786584428.218 < 1786598400).
    check("query: placement is always computable, whatever coverage says",
          (cov.rows_total, cov.rows_in_closed_windows, cov.rows_unclaimed,
           cov.rows_undatable), (3, 3, 0, 0))
    check("query: and 3 of 3 rows were placeable", cov.placed_fraction, 1.0)

    p = query.paginate(query.compile_query({}), _qrows(), limit=2)
    check_true("query: a page carries one too",
               p.coverage.__class__.__name__ == "CoverageStatement")
    check("query: and with no report supplied it says exactly that",
          p.coverage.basis, "no-report-supplied")
    check("query: rather than claiming everything was placed",
          p.coverage.placed_fraction, None)


def test_q_coverage_travels_from_the_attestation_to_the_breakdown():
    """AUTHORED attestation, half of alpha's window.

    The point is that the number reaches the aggregate: a breakdown of the
    requests in a window covered half the time must say 0.5, not inherit the
    silence of a module that never looked.
    """
    uuid = fx.uuid_of("alpha")
    att = fx.attestation(uuid, "5h", ALPHA_W, "darwin",
                         [[ALPHA_START, ALPHA_START + 9000, True]])
    rows = [r for r in _qrows() if r["account_uuid"] == uuid]
    smps = [s for s in fx.real_samples() if s["account_uuid"] == uuid]
    rep = reconcile.reconcile(smps, rows, [att], PAST_GRACE)
    b = query.breakdown(rows, "model", report=rep, account_uuid=uuid)
    # 9000 s of 18000.
    check("query: the aggregate reports the window's real coverage",
          b.coverage.fraction, 0.5)
    check("query: from the attestations", b.coverage.basis, "attestations")
    check("query: naming the machine that reported",
          sorted(b.coverage.hosts_reporting), ["darwin"])


def test_q_dark_is_a_claim_and_silence_is_not_and_the_aggregate_keeps_them_apart():
    """The composed statement inherits `coverage.py`'s four host states rather
    than flattening them.

    One attestation, naming the FIRST replay window and covering it wholly.
    The second window is never named by a host that attests, past the grace --
    which is `dark`, a positive statement that the machine was not listening,
    and it composes into a real length-weighted mean.  That is not the same
    situation as an account that has never attested at all, where the fraction
    is None: 0.0 is the claim "nobody was listening" and None is "nobody said".
    """
    uuid = fx.REPLAY_ACCOUNT
    smps = fx.replay_samples()
    w0, w1 = fx.REPLAY_5H_RESETS[0], fx.REPLAY_5H_RESETS[1]
    att = fx.attestation(uuid, "5h", w0, "darwin", [[w0 - window.L5, w0, True]])
    rows = []
    for i, ts in enumerate((w0 - 100, w1 - 100)):
        rows.append(fx.derive(
            "query-replay-request",
            "real row 0 (agent, repl_main_thread) placed in a replay window",
            _qrows()[0], account_uuid=uuid, ts=ts,
            request_id="q%015d" % i))
    rep = reconcile.reconcile(smps, rows, [att], fx.NOW_REPLAY)
    cov = query.coverage_for(rep, uuid, rows)
    # One window covered 18000/18000, one covered 0/18000, so 0.5 -- and both
    # halves are statements a machine made.
    check("query: a covered window and a dark one compose to a real fraction",
          cov.fraction, 0.5)
    check("query: from attestations", cov.basis, "attestations")
    check("query: over both windows the rows touch",
          (cov.windows_n, cov.windows_with_coverage_n), (2, 2))
    check("query: the dark machine is named as dark, not as silent",
          (sorted(cov.hosts_dark), sorted(cov.hosts_silent)), (["darwin"], []))

    # The same rows with no attestation anywhere: unknown, not zero.
    silent = query.coverage_for(
        reconcile.reconcile(smps, rows, [], fx.NOW_REPLAY), uuid, rows)
    check("query: with nothing attested it is unknown, not 0.0",
          (silent.fraction, silent.basis), (None, "no-attestation"))
    check("query: and the machine is silent, not dark",
          (sorted(silent.hosts_dark), sorted(silent.hosts_silent)),
          ([], []))


def test_q_one_unattested_window_makes_the_whole_fraction_unknown():
    """The `partly-unknown` branch, which until now was pinned by NOTHING.

    `coverage_for`'s docstring states the rule -- the fraction is None the
    moment ANY touched window has no attestation -- and the alternative is
    seductive and wrong: a length-weighted mean over the windows that spoke,
    presented as the coverage of a set that includes windows nobody described,
    is a confident number about an unknown.  It is the same error as
    `fraction: 0.0` for "nobody said", one level up.

    `reconcile` cannot produce this state today and the source says so: the
    account-level `for_window` returns None only when NO attestation exists at
    all, so every window of an account is unknown together or none is.  The
    state is reached the way `coverage_for` really is reached from
    `reconcile.reconcile`, which takes attestations from any caller -- one
    window described, one not -- and it is constructed here rather than left
    as a comment claiming a branch works.
    """
    uuid = fx.REPLAY_ACCOUNT
    smps = fx.replay_samples()
    w0, w1 = fx.REPLAY_5H_RESETS[0], fx.REPLAY_5H_RESETS[1]
    att = fx.attestation(uuid, "5h", w0, "darwin", [[w0 - window.L5, w0, True]])
    rows = []
    for i, ts in enumerate((w0 - 100, w1 - 100)):
        rows.append(fx.derive(
            "query-replay-request",
            "real row 0 (agent, repl_main_thread) placed in a replay window",
            _qrows()[0], account_uuid=uuid, ts=ts,
            request_id="qp%014d" % i))
    rep = reconcile.reconcile(smps, rows, [att], fx.NOW_REPLAY)
    both = query.coverage_for(rep, uuid, rows)
    check("query: with both windows described the fraction is real",
          (both.fraction, both.basis), (0.5, "attestations"))

    # Now one of the two windows carries no coverage statement at all.
    acc = rep.accounts[uuid]
    dropped = 0
    for a in acc.attributions:
        if a.kind == "5h" and a.resets_at == w1:
            a.coverage = None
            dropped += 1
    check("query: exactly one window's coverage was removed", dropped, 1)

    cov = query.coverage_for(rep, uuid, rows)
    check("query: one unattested window makes the fraction unknown",
          cov.fraction, None)
    check("query: and the basis names the split rather than averaging",
          cov.basis, "partly-unknown")
    check("query: the described window is still counted as described",
          (cov.windows_n, cov.windows_with_coverage_n), (2, 1))
    check_true("query: and the note says how many spoke and what they said",
               any("1 of the 2 windows" in n and "1.00" in n
                   for n in cov.notes))
    # That 1.00 is the whole point of the branch: it is the number the
    # described window really reports, and it is exactly the number that must
    # NOT become the statement's fraction, because half the set is undescribed.
    check("query: the tempting figure stays inside the note",
          cov.fraction, None)
    check("query: unknown is still never zero", cov.fraction == 0.0, False)


def test_q_pagination_returns_every_row_exactly_once():
    """Keyset over `(ts, request_id)`.  Both directions, page size 3."""
    for order in ("asc", "desc"):
        q = query.compile_query({"order": order})
        seen, cur, pages = [], None, 0
        while True:
            p = query.paginate(q, _qrows(), cursor=cur, limit=3)
            seen.extend(r["request_id"] for r in p.items)
            pages += 1
            if not p.has_more or pages > 10:
                break
            cur = p.next_cursor
        want = sorted(r["request_id"] for r in _qrows())
        check("query/%s: every row exactly once across pages" % order,
              sorted(seen), want)
        check("query/%s: and in three pages of 3, 3, 2" % order, pages, 3)
        ts = [r["ts"] for r in _qrows()]
        check("query/%s: ordered by ts" % order,
              [r for r in seen], [rid for _t, rid in sorted(
                  ((r["ts"], r["request_id"]) for r in _qrows()),
                  reverse=(order == "desc"))])
        check("query/%s: and the last page carries no cursor onward" % order,
              p.next_cursor, None)
        check("query/%s: pages bound the answer, not the scan" % order,
              p.scanned, len(ts))


def test_q_a_boolean_is_not_the_integer_one():
    """`True == 1` in Python and `schema` is 1 in all 8 rows.

    A silent `where={'schema': True}` matching every row is the kind of thing
    that is found years later, so equality compares the type as well.  The same
    guard is why `window.contains` uses `_num` rather than a bare isinstance:
    `ts: true` would otherwise be placed as the epoch second 1.
    """
    check("query: a boolean does not match the integer it equals",
          _qsel({"where": {"schema": True}}).matched, 0)
    check("query: while the integer itself does",
          _qsel({"where": {"schema": 1}}).matched, 8)


def test_q_pagination_is_stable_under_a_concurrent_append():
    """The store accepts a week-late shipment and re-derives from byte zero, so
    a row can be appended whose `ts` belongs in the middle of a page served
    yesterday.  An offset would shift every later row by one and skip a real
    request in silence.

    Keyset cannot do that -- and the one thing it genuinely cannot show, a row
    arriving BEHIND the cursor, is stated on every page rather than hidden.
    """
    q = query.compile_query({"order": "asc"})
    p1 = query.paginate(q, _qrows(), limit=3)
    late = fx.derive("query-late-arrival",
                     "real row 0 (agent), shipped late with an earlier ts",
                     _qrows()[0], ts=_qrows()[0]["ts"] - 1000,
                     request_id="qlate00000000001")
    later = fx.derive("query-late-arrival",
                      "real row 0 (agent), arriving after the last page",
                      _qrows()[0], ts=max(r["ts"] for r in _qrows()) + 1000,
                      request_id="qlate00000000002")
    grown = _qrows() + [late, later]

    seen = list(r["request_id"] for r in p1.items)
    cur = p1.next_cursor
    for _ in range(10):
        p = query.paginate(q, grown, cursor=cur, limit=3)
        seen.extend(r["request_id"] for r in p.items)
        if not p.has_more:
            break
        cur = p.next_cursor
    check("query: no row already served is served again",
          len(seen), len(set(seen)))
    check_true("query: every original row still appears exactly once",
               all(r["request_id"] in seen for r in _qrows()))
    check_true("query: a row appended AFTER the cursor is picked up",
               "qlate00000000002" in seen)
    check_true("query: one that arrived BEHIND it is not -- keyset cannot",
               "qlate00000000001" not in seen)
    check_true("query: and every page says so, with the remedy",
               any("SHIPPED LATE" in n and "since/until" in n
                   for n in p.notes))
    check("query: `matched` is recomputed per page, so a corpus that moved is "
          "visible", (p1.matched, p.matched), (8, 10))


def test_q_a_cursor_from_another_query_is_refused_not_applied():
    """Applying page 2's cursor to a differently-filtered query would skip
    whatever sorts before it -- an empty page that reads as the end of the
    result."""
    q1 = query.compile_query({"order": "asc"})
    p = query.paginate(q1, _qrows(), limit=3)
    q2 = query.compile_query({"order": "asc",
                              "where": {"model": "claude-sonnet-5"}})
    r = query.paginate(q2, _qrows(), cursor=p.next_cursor)
    check("query: a cursor is bound to the query that issued it",
          getattr(r, "reason", None), "cursor-does-not-match-this-query")
    q3 = query.compile_query({"order": "desc"})
    r3 = query.paginate(q3, _qrows(), cursor=p.next_cursor)
    check("query: and to its direction", getattr(r3, "reason", None),
          "cursor-does-not-match-this-query")
    r4 = query.paginate(q1, _qrows(), cursor="nonsense")
    check("query: an unreadable cursor is refused, not ignored",
          getattr(r4, "reason", None), "cursor-unreadable")


def test_q_a_row_with_no_usable_timestamp_is_counted_on_every_page():
    """It matched the query and it cannot take a position in a time ordering.

    `reconcile` keeps exactly this third bucket for exactly this reason: a row
    in no window and no unclaimed list is weight sitting in the residual, which
    is the one figure here sold as evidence of an unwatched machine.  Dropping
    it from a page silently would be the same disappearance one layer up.
    """
    nots = fx.derive("query-undatable-request",
                     "real row 0 (agent) with its clock unusable",
                     _qrows()[0], ts=None, request_id="qnots00000000001")
    rows = _qrows() + [nots]
    p = query.paginate(query.compile_query({"order": "asc"}), rows, limit=20)
    check("query: it is matched", p.matched, 9)
    check("query: it is counted", p.undatable_n, 1)
    check_true("query: it is not in the page",
               "qnots00000000001" not in [r["request_id"] for r in p.items])
    check_true("query: and the page says why",
               any("no usable timestamp" in n for n in p.notes))
    sel = query.select(query.compile_query({}), rows)
    check_true("query: the selection still holds it",
               nots in sel.rows and nots in sel.undatable)


def test_q_lookup_by_id_and_what_not_finding_one_means():
    """`request_id` is a paste-in field; `prompt_id` is a turn.

    Not finding an id must never read as "this request cost nothing".
    """
    rows = _qrows()
    rid = rows[0]["request_id"]
    got = query.lookup(rows, "request_id", rid)
    check("query: a request_id lookup returns its row", got.matched, 1)
    # prompt_id f1f70004... appears on 3 of the 8 rows: one turn, three
    # requests -- which is what makes it the grouping unit rather than a facet.
    turn = query.lookup(rows, "prompt_id", "f1f70004-0000-4000-8000-000000000004")
    check("query: a prompt_id lookup returns the whole turn", turn.matched, 3)
    sess = query.lookup(rows, "session_id", "f1f70003-0000-4000-8000-000000000003")
    check("query: and a session_id its session", sess.matched, 3)

    miss = query.lookup(rows, "request_id", "0000000000000000")
    check("query: an id that is not here is filtered-to-nothing",
          miss.empty_because, "filtered-to-nothing")
    check_true("query: and says what that does and does not mean",
               any("never have received it" in n for n in miss.notes))
    check("query: a lookup on a non-id column is refused",
          getattr(query.lookup(rows, "model", "x"), "reason", None),
          "not-an-id-column")


def test_q_free_text_searches_the_low_cardinality_columns_and_never_the_ids():
    """A substring of a 16-hex content hash is not a question anybody has, and
    offering it invites a full scan for no recall."""
    hit = _qsel({"text": "iTerm"})
    check("query: free text matches a label column", hit.matched, 7)
    ci = _qsel({"text": "iterm"})
    check("query: case-insensitively", ci.matched, 7)
    rid = _qrows()[0]["request_id"]
    miss = _qsel({"text": rid})
    check("query: and never reaches the ids", miss.matched, 0)
    r = query.compile_query({"text": "x", "text_in": ["request_id"]})
    check("query: asking for one is refused by name", r.reason,
          "text-search-on-an-id-column")
    check_true("query: with the lookup named as the remedy",
               "where=" in (r.remedy or ""))
    n = query.compile_query({"text": "x", "text_in": ["duration_ms"]})
    check("query: as is text over a numeric column", n.reason,
          "text-search-on-a-non-text-column")


def test_q_facets_are_derived_from_the_rows_and_never_from_a_list():
    """A facet built on a column nothing has ever populated renders an empty
    control that reads as 'I use no MCP tools'.

    So `facets` omits it, and `field_availability` states it -- the difference
    between "no value matched" and "this attribute has never been observed" is
    a field on the result, not an inference for the reader.
    """
    f = query.facets(_qrows())
    check("query: query_source facet, counted off the capture",
          f["query_source"],
          [("generate_session_title", 3), ("repl_main_thread", 3),
           ("prompt_suggestion", 1), ("sdk", 1)])
    check("query: models", f["model"],
          [("claude-sonnet-5", 5), ("claude-haiku-4-5", 3)])
    check("query: user tags are facets too, derived from the data present",
          f.get("tags.test"), [("4", 1)])
    for col in query.NEVER_OBSERVED:
        check_true("query: `%s` has never been observed, so it is not a facet"
                   % col, col not in f)
    for col in query.ID_FIELDS:
        check_true("query: `%s` is a lookup key, not a facet" % col,
                   col not in f)
    check("query: and asking for one explicitly still does not build it -- "
          "request_id has one row per value",
          query.facets(_qrows(), columns=["request_id", "model"]),
          {"model": [("claude-sonnet-5", 5), ("claude-haiku-4-5", 3)]})
    avail = query.field_availability(_qrows())["columns"]
    check("query: and availability says so out loud",
          avail["mcp_tool"]["never_observed"], True)
    check("query: while a populated column reports its cardinality",
          (avail["model"]["never_observed"], avail["model"]["distinct_n"]),
          (False, 2))


def test_q_a_breakdown_by_a_never_observed_column_says_so():
    """`profile` is null in all 8 rows -- that session ran with no profile.

    One bucket of everything is not a breakdown, and it must not read as "every
    request had the same profile".
    """
    b = query.breakdown(_qrows(), "profile", report=_qrep(),
                        account_uuid=fx.uuid_of("alpha"))
    check("query: one bucket, and it is the None one", sorted(
        repr(k) for k in b.buckets), ["None"])
    check("query: the column status says it has never been populated",
          b.column_status["never_observed"], True)
    check_true("query: and a note says that is not the same as one value",
               any("never been populated" in n for n in b.notes))
    # A tag key no row carries is the strongest form of the same thing: it is
    # ABSENT from the availability map entirely, and defaulting that to False
    # would report it as a populated column with one empty bucket.
    t = query.breakdown(_qrows(), "tags.nobody-sets-this", report=_qrep(),
                        account_uuid=fx.uuid_of("alpha"))
    check("query: a tag key nothing carries is never-observed too",
          t.column_status["never_observed"], True)
    check("query: with no value invented for it",
          t.column_status["rows_with_a_value"], 0)


def test_q_per_account_fans_out_and_produces_no_total():
    """Not even for tokens.

    The rule could be written "percentages never, absolute quantities freely",
    and it is not, because a rule with an exception in it is applied by
    somebody who remembers only the exception.
    """
    out = query.breakdown_per_account(_qrows(), "query_source", report=_qrep())
    check("query: one breakdown per account",
          sorted(out), sorted(fx.account_uuid_by_label().values()))
    check("query: over that account's rows only, agent 3 / alpha 3 / beta 2",
          sorted(b.matched for b in out.values()), [2, 3, 3])
    check("query: each carries its own coverage",
          sorted({b.coverage.basis for b in out.values()}), ["no-attestation"])
    check_true("query: and nothing in the result is a total",
               all(b.account_uuid in out for b in out.values()))


def test_q_the_bucket_key_checks_presence_and_not_truthiness():
    """`show` computes this as `r.get(k) or tags.get(k) or "(none)"`, and the
    `or` chain has a real bug in it: a legitimately falsy value falls through
    to the tag lookup and then to the unlabelled bucket.

    The divergence is deliberate and named here so a figure differing from
    `show`'s has an explanation that is not "one of them is wrong".
    """
    falsy = fx.derive("query-falsy-label",
                      "real row 0 (agent) with an empty profile and a tag of "
                      "the same name",
                      _qrows()[0], profile="", tags={"profile": "fallback"})
    b = query.breakdown([falsy], "profile", account_uuid=falsy["account_uuid"])
    check("query: an empty-string value is its own bucket, not the tag's",
          sorted(b.buckets), [""])
    # And the fallback still works when the column carries nothing at all.
    tagged = fx.derive("query-falsy-label",
                       "real row 0 (agent) with no profile and a profile tag",
                       _qrows()[0], profile=None, tags={"profile": "fallback"})
    b2 = query.breakdown([tagged], "profile",
                         account_uuid=tagged["account_uuid"])
    check("query: a tag of the same name is the fallback when there is no "
          "column value", sorted(b2.buckets), ["fallback"])


def test_q_no_bucket_ever_carries_a_combined_token_figure():
    """Cache reads were ~89% of this user's tokens, so a figure that folds the
    four classes together is wrong by about an order of magnitude for anyone
    with a warm cache."""
    b = query.breakdown(_qrows(), "model", report=_qrep(),
                        account_uuid=fx.uuid_of("alpha"))
    for k, bucket in b.buckets.items():
        check_true("query: no combined `tokens` key in bucket %r" % k,
                   "tokens" not in bucket)
        for f in query.TOKEN_FIELDS:
            check_true("query: %s is carried separately in %r" % (f, k),
                       f in bucket)
    # alpha's three rows: cache_read 51241, cost 0.0592663 (hand-summed off
    # the capture).
    tot = {f: sum(v[f] for v in b.buckets.values())
           for f in query.TOKEN_FIELDS}
    check("query: cache reads are counted, and are the big number",
          tot["cache_read_tokens"], 51241)
    check("query: cost is the same weight the attribution uses",
          round(sum(v["cost"] for v in b.buckets.values()), 7), 0.0592663)


def test_q_null_token_columns_are_absent_and_not_a_confident_zero():
    """Token columns are null when the payload did not state them.  Summing
    them as 0 is right; not saying how many were null is not."""
    nulled = fx.derive("query-null-tokens",
                       "real row 0 (agent) with its output token count absent",
                       _qrows()[0], output_tokens=None)
    b = query.breakdown([nulled], "model", account_uuid=nulled["account_uuid"])
    bucket = list(b.buckets.values())[0]
    check("query: the null row is counted", bucket["tokens_null_rows"], 1)
    check_true("query: and named in a note",
               any("output_tokens in 1 row" in n for n in b.notes))


def test_q_overhead_is_counted_and_named_never_filtered_from_under_a_total():
    """`show`'s footer, kept: name the sources THESE rows contain, and every
    source counted as the user's work, so a new overhead source announces
    itself the first time it is seen."""
    b = query.breakdown(_qrows(), "model", report=_qrep(),
                        account_uuid=fx.uuid_of("alpha"))
    # alpha's 3 rows: generate_session_title 1, prompt_suggestion 1,
    # repl_main_thread 1.
    check("query: overhead requests counted", b.overhead["requests"], 2)
    check("query: and the sources these rows really contain are named",
          b.overhead["sources_seen"],
          ["generate_session_title", "prompt_suggestion"])
    check("query: with the user's own work named too",
          b.overhead["counted_as_your_work"], ["repl_main_thread"])
    check("query: and the rows are still in the totals",
          sum(v["requests"] for v in b.buckets.values()), 3)


def test_q_the_reconciled_view_carries_movement_residual_and_coverage():
    """`server/` computes attribution and has no display at all; this is the
    re-shaping, and it recomputes nothing."""
    v = query.windows_view(_qrep(), fx.uuid_of("beta"))
    check("query: both kinds of window appear", len(v["windows"]), 2)
    w5 = [w for w in v["windows"] if w["kind"] == "5h"][0]
    # beta's real 5-hour window: peak 49, baseline 0 by construction.
    check("query: movement is passed through", w5["movement_pp"], 49.0)
    check("query: with the quantiser interval intact",
          (w5["movement_quant_lo"], w5["movement_quant_hi"]), (48.5, 50.0))
    check("query: attributed and residual are both present",
          (round(w5["attributed_pp"], 6), round(w5["residual_pp"], 6)),
          (49.0, 0.0))
    check("query: and coverage sits in the same row",
          w5["coverage"]["fraction"], None)
    check_true("query: with the provisional weighting note attached",
               any("PROVISIONAL" in n for n in w5["notes"]))
    w7 = [w for w in v["windows"] if w["kind"] == "7d"][0]
    check("query: the open 7-day window is refused, by name",
          w7["refused"], "window-open")
    check_true("query: and still reports its movement rather than a blank",
               w7["movement_pp"] is not None)


def test_q_the_reconciled_view_refuses_rather_than_pooling_accounts():
    v = query.windows_view(_qrep(), None)
    check("query: windows without an account are refused",
          getattr(v, "reason", None), "no-account")
    u = query.windows_view(_qrep(), "not-an-account")
    check("query: an unknown account is refused, with the known ones listed",
          getattr(u, "reason", None), "unknown-account")
    k = query.windows_view(_qrep(), fx.uuid_of("beta"), kind="30d")
    check("query: an invented window kind is refused",
          getattr(k, "reason", None), "unknown-window-kind")


def test_q_a_negative_residual_reaches_the_view_with_its_sign():
    """Movement is a LOWER bound, so over-attribution means the bound is loose,
    not that the requests are wrong.  Clamping at zero would delete the only
    evidence that the weighting is off."""
    uuid = fx.uuid_of("beta")
    smps = [s for s in fx.real_samples() if s["account_uuid"] == uuid]
    rows = [r for r in _qrows() if r["account_uuid"] == uuid]
    # beta moved 49 pp over 0.0948947 of weight; a rate 10x the fit
    # over-attributes by construction.
    rep = reconcile.reconcile(smps, rows, [], fx.NOW_JOINT,
                              rate={"5h": 5163.618199962696})
    v = query.residual_view(rep, uuid)
    w = v["windows"][0]
    check_true("query: the residual is reported negative", w["residual_pp"] < 0)
    check("query: and flagged", w["refused"], None)
    check_true("query: with the reason in words",
               any("LOWER bound" in n for n in w["notes"]))
    check_true("query: and the provisional note on the view itself",
               "PROVISIONAL" in v["note"])


def test_q_a_refused_window_stays_in_the_residual_view():
    """Dropping them would leave a table of windows that all reconcile, which
    is the tidiest possible way to hide the ones that could not."""
    uuid = fx.uuid_of("alpha")
    smps = [s for s in fx.real_samples() if s["account_uuid"] == uuid]
    rows = [r for r in _qrows() if r["account_uuid"] == uuid]
    # An attestation covering NONE of the window, past the grace: settled zero.
    att = fx.attestation(uuid, "5h", ALPHA_W, "darwin", [])
    rep = reconcile.reconcile(smps, rows, [att], PAST_GRACE)
    v = query.residual_view(rep, uuid)
    w = [x for x in v["windows"] if x["kind"] == "5h"][0]
    check("query: the window is still listed", w["resets_at"], ALPHA_W)
    check("query: refused by name", w["refused"], "no-coverage")
    check("query: with no residual invented for it", w["residual_pp"], None)
    check("query: and its movement still reported", w["movement_pp"], 10.0)


# ---- the storage seam -------------------------------------------------------

def test_q_narrowing_is_a_superset_and_the_predicate_is_the_truth():
    """The property an index rests on: narrowing may only widen.

    A storage layer that returns MORE than the narrowing describes costs time
    and changes no answer, because the predicate is re-applied to every
    candidate.  One that returns fewer loses rows with no symptom -- the
    cardinal sin arriving through the fast path -- so the test is that
    narrowing-then-predicate equals predicate alone, over the real rows.
    """
    specs = [
        {"account_uuid": fx.uuid_of("alpha")},
        {"since": 1786584400, "until": 1786584500},
        {"where": {"model": "claude-sonnet-5"}},
        {"where": {"query_source": ["repl_main_thread", "sdk"]}},
        {"where": {"terminal_type": "iTerm.app"}, "text": "darwin"},
        {"not_where": {"query_source": "generate_session_title"}},
        {"ranges": {"duration_ms": (1000, 5000)}},
        {"where": {"session_id": "f1f70003-0000-4000-8000-000000000003"}},
        # A user tag: `tags` is a dict inside the row and no index column
        # addresses it, so a narrowing that claimed it would hand the predicate
        # a candidate set with every row missing.
        {"where": {"tags.test": "4"}},
    ]
    for spec in specs:
        q = query.compile_query(spec)
        nar = query.narrowing(q)
        # What an index built on INDEX_COLUMNS would hand back.
        cands = []
        for r in _qrows():
            if nar.account_uuid and r.get("account_uuid") != nar.account_uuid:
                continue
            if nar.ts_lo is not None and not (r.get("ts") >= nar.ts_lo):
                continue
            if nar.ts_hi is not None and not (r.get("ts") < nar.ts_hi):
                continue
            if any(r.get(c) not in vs for c, vs in nar.equalities.items()):
                continue
            cands.append(r)
        via_index = [r["request_id"] for r in query.select(q, cands).rows]
        direct = [r["request_id"] for r in query.select(q, _qrows()).rows]
        check("query: narrowing changes no answer for %r" % (spec,),
              sorted(via_index), sorted(direct))
    q = query.compile_query({"text": "x", "not_where": {"model": "y"},
                             "ranges": {"duration_ms": (0, 1)}})
    nar = query.narrowing(q)
    check("query: and the clauses an index cannot serve are named",
          sorted(nar.unsupported),
          ["free text", "not_where model", "range duration_ms"])


def test_q_the_plan_reports_the_measured_cost_and_refuses_only_a_stated_budget():
    """Measured on this machine: 7.706 s/GiB parsing every line and applying
    this module's predicate, over a **789.3** byte mean ledger row, so 200 ms
    buys about 27k rows.  Five years of one heavy user at the pessimistic rate
    is 62 M rows and 45.6 GiB, where the same scan is 351 s.

    Both numbers here were re-measured and both moved, so the reading is shown:
    737 bytes is the row the CLIENT writes and 789.3 is the row in the STORE,
    which carries the `account_uuid` the shipper stamps on.  The old pair
    (6.675 s/GiB over 737 B) was measuring the wrong end of the wire, and it
    had been quoted in three places.

    The refusal fires only when the caller states a budget.  A 12-second
    whole-history aggregate is a legitimate thing to ask for, and a module that
    decides which questions are worth waiting for has replaced the measurement
    with a policy.
    """
    check("query: the interactive ceiling is derived from the measurement, "
          "not typed", query.INTERACTIVE_ROWS_PARSE,
          int((0.2 / 7.706) * (1024 ** 3) / 789.3))
    check("query: the mean row is the STORE's row, stamp included -- the "
          "client's 737 is a different measurement of a different thing",
          query.LEDGER_MEAN_ROW_BYTES, 789.3)
    q = query.compile_query({})
    # 5 M rows * 789.3 B = 3.68 GiB; * 7.706 s/GiB = 28.3 s.
    p = query.plan(q, candidate_rows=5000000)
    check("query: the estimate is the measured rate times the bytes",
          round(p.estimate_s, 1), 28.3)
    check("query: and it is not interactive", p.interactive, False)
    check_true("query: but it is not refused without a budget",
               not query.refused(p))
    r = query.plan(q, candidate_rows=5000000, budget_s=0.2)
    check("query: with a budget it is refused, by name", r.reason,
          "scan-budget-exceeded")
    check_true("query: naming the narrowing that would fix it",
               "account_uuid" in (r.remedy or "")
               and "since/until" in (r.remedy or ""))
    small = query.plan(q, candidate_rows=1000, budget_s=0.2)
    check("query: a small candidate set is interactive and answered",
          small.interactive, True)
    none = query.plan(q)
    check("query: with no candidate count there is no invented estimate",
          none.estimate_s, None)


def test_q_every_refusal_reason_the_module_can_emit_is_declared():
    """Derived from the source, not listed by hand.

    A hand-written list is a fourth place to forget -- the same guard claudio
    puts on its own usage text, for the same reason.
    """
    src = open(os.path.join(fx.SRV_DIR, "srv", "query.py"),
               encoding="utf-8").read()
    emitted = sorted(set(re.findall(r'Unanswerable\(\s*"([a-z0-9-]+)"', src)))
    check("query: REASONS is exactly what the module can return",
          emitted, sorted(query.REASONS))


# ================================================ 10. THE READ API ==========
#
# `srv/api.py` -- the contract omini is written against -- and the wiring in
# `srv/serve.py` that puts it on a socket.  Every test below ships the REAL
# capture through the REAL door and then asks the endpoints their questions, so
# what is asserted is the whole path -- bytes on disk, `read_batches`,
# `ingest`, `reconcile`, DuckDB over the same files, JSON -- rather than a
# description of it.
#
# `srv/ui.py` is DELETED and is not coming back.  A renderer shipped inside the
# server that produces its numbers is one opinion about them, and every honesty
# property it carried was expressed as HOW IT DREW THINGS, which is
# unenforceable the moment somebody else draws them.  So each is asserted here
# as a property of the PAYLOAD.  The three tests that could only ever have been
# about a page went with the file; every one whose other half asserted a
# payload kept that half.
#
# What the fixtures make true, and every one of these is a state a front end
# has to render honestly rather than an edge case:
#
#   * `coverage.fraction is None` for every window, because nothing emits an
#     attestation.  One test ships an AUTHORED one to prove a real fraction
#     reaches the payload when there is one.
#   * every 7d window in the joint capture is open, so its attribution is
#     refused `window-open`.
#   * each account's single closed 5h window pins its own rate, so its
#     residual is 0.0 BY CONSTRUCTION -- the one number in the whole payload
#     most able to be read as a measurement when it is not.
#
# Everything that reads stream A goes through DuckDB.  Where it is not
# installed the stream-A tests SELF-SKIP, exactly as the `duck` tests do, and
# the reconciler half still runs -- so the count is machine-dependent and no
# doc quotes a total.

UI_LABELS = ("alpha", "beta", "agent")


def ui_line(rec):
    """One derived/authored record as a body line.

    `fixtures.py` reads captured bytes off disk wherever a capture exists;
    this is for the two records that have no captured byte form at all -- an
    attestation, which nothing has ever emitted, and a row derived to carry
    `source: synthetic`, which by definition no capture contains.
    """
    return json.dumps(rec, sort_keys=True, separators=(",", ":")).encode("utf-8")


def ui_store(labels=UI_LABELS, now=fx.NOW_JOINT, extra=()):
    """A store with the real capture shipped into it, through the real door.

    `extra` is [(label, stream, [line])] for records with no captured form.
    """
    root = door_root()
    store = serve.Store(root)
    tokens = {"tok-" + lbl: fx.uuid_of(lbl) for lbl in labels}
    door = serve.Door(store, serve.Tenants(mapping=tokens))
    for lbl in labels:
        for _man, _lines, body in (ship_a(lbl), ship_b(lbl)):
            door.ship("tok-" + lbl, serve.CONTENT_TYPE, None, body, now=now)
    for lbl, stream, lines in extra:
        # Its own machine_id, and the reason is the door working correctly: a
        # second batch naming the same machine, stream and file over a byte
        # range the door already holds is the ack that got lost, and is
        # skipped.  An extra record is a different client file.
        man = fx.manifest(stream, lines, machine_id="m-ui-extra", offset=0,
                          to=sum(len(x) + 1 for x in lines))
        door.ship("tok-" + lbl, serve.CONTENT_TYPE, None, ndjson(man, lines),
                  now=now)
    return root, store, api.Api(serve.View(root),
                                duck_store=duckstore.DuckStore(root),
                                counters=store.counters)


def ui_call(a, path, qs="", now=fx.NOW_JOINT):
    """One GET, exactly as the handler decodes it."""
    multi = urllib.parse.parse_qs(qs, keep_blank_values=True)
    return a.handle(path, {k: v[-1] for k, v in multi.items()}, multi, now=now)


def result_of(doc):
    """The result, or None on a refusal -- which carries no `result` at all."""
    return doc.get("result")


def walk_keys(obj, out=None):
    """Every string key anywhere in a payload.

    String keys only: `schemas` is keyed by schema VERSION, an int, and this
    walk is looking for field names.
    """
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str):
                out.add(k)
            walk_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_keys(v, out)
    return out


def code_without_prose(path):
    """Source with its comments AND its docstrings removed.

    A static "the SQL never says X" check that greps the raw file matches the
    paragraph EXPLAINING why it must never say X -- which is how the counters
    test one directory over came to assert a comment rather than a setting.
    `tokenize` is what separates the code from the prose about it.
    """
    import io as _io
    import tokenize as _tok
    src = open(path, encoding="utf-8").read()
    out, prev_end, prev_type = [], (1, 0), _tok.INDENT
    for tok in _tok.generate_tokens(_io.StringIO(src).readline):
        ttype, text, start, end, _line = tok
        if ttype == _tok.COMMENT:
            continue
        # A STRING whose statement position makes it a docstring: it follows
        # an INDENT, a NEWLINE or the start of the file, i.e. it is an
        # expression statement rather than a value in an expression.
        if ttype == _tok.STRING and prev_type in (_tok.INDENT, _tok.NEWLINE,
                                                  _tok.NL, _tok.DEDENT,
                                                  _tok.ENCODING):
            prev_type = ttype
            prev_end = end
            continue
        if start != prev_end:
            out.append(" ")
        out.append(text)
        prev_type = ttype
        prev_end = end
    return "".join(out)


def have_duck():
    return duckstore.available()


V1 = "/api/v1/"


# ---- the envelope -----------------------------------------------------------

def test_api_the_four_outcomes_are_told_apart_by_key_presence():
    """Not by the status code, and not by an empty array.

    `no-data`, `filtered-to-nothing` and `unanswerable` are three different
    answers with three different messages, and a query engine returns zero rows
    for all three.  The discriminator is KEY PRESENCE: `unanswerable` carries
    no `result` key AT ALL, so a client doing `for (row of p.result.rows)`
    raises on a refusal instead of rendering a tidy empty table -- which is the
    exact failure ("flattening one into an empty list") the shape exists to
    prevent.
    """
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")

    st, ok = ui_call(a, V1 + "accounts")
    check("api/envelope: an answer with rows is `ok`",
          (st, ok["outcome"]), (200, "ok"))
    check("api/envelope: ...and carries no `empty` and no `refusal`",
          ("empty" in ok, "refusal" in ok), (False, False))

    st, ref = ui_call(a, V1 + "windows", "account=nobody")
    check("api/envelope: an unanswerable question is a refusal",
          (st, ref["outcome"]), (404, "unanswerable"))
    check("api/envelope: and carries NO result key at all, so a client "
          "looping over result.rows raises rather than drawing an empty table",
          "result" in ref, False)
    check("api/envelope: with no `empty` either", "empty" in ref, False)

    # A PARTIAL skip: `ok` and `unanswerable` were asserted above and needed no
    # connection.  Both EMPTY outcomes are below, so a bare `return` here drops
    # two thirds of the three-way distinction -- the headline guarantee of the
    # DuckDB swap -- without a word.
    if not have_duck():
        _skip("api: the two EMPTY outcomes of the four (duckdb not installed)")
        return
    st, filt = ui_call(a, V1 + "search", "account=%s&since=1&until=2" % u)
    check("api/envelope: a filter that removes every row is 200 and "
          "`filtered-to-nothing`, not a refusal and not `no-data`",
          (st, filt["outcome"]), (200, "filtered-to-nothing"))
    check("api/envelope: its result is PRESENT and empty, so a table renders "
          "as an empty table", filt["result"]["rows"], [])
    check_true("api/envelope: and the empty statement says which of the two "
               "this is", filt["empty"]["kind"] == "filtered-to-nothing")

    st, nod = ui_call(a, V1 + "search", "account=" + fx.uuid_of("alpha"))
    check("api/envelope: every outcome is in the declared closed set",
          sorted({ok["outcome"], ref["outcome"], filt["outcome"],
                  nod["outcome"]}) == sorted(set(
                      [o for o in api.OUTCOMES
                       if o in {ok["outcome"], ref["outcome"],
                                filt["outcome"], nod["outcome"]}])), True)


def test_api_no_data_blames_no_clause_for_an_empty_corpus():
    """0-of-0 is not "this filter emptied it".

    `query.select`'s `if scanned and not kept` conjunction, expressed on the
    wire: on `no-data` the `eliminated` map is `{}` and `sole_cause` is `null`,
    because blaming a clause for an empty corpus sends somebody off to widen a
    filter that was never the problem.
    """
    if not have_duck():
        _skip("api: no-data accounting (duckdb not installed)")
        return
    root, _store, a = ui_store()
    led = os.path.join(root, "accounts", fx.uuid_of("alpha"), "ledger.jsonl")
    qs = ("account=%s&where.model=nothing-like-this" % fx.uuid_of("alpha"))

    # TWO ways to have no rows, and the guard is only load-bearing on the
    # second.  With the file MISSING the engine returns `eliminated: {}`
    # already, so a reset that did nothing would look correct; with the file
    # PRESENT AND EMPTY the accounting really does return a clause map --
    # `{account_uuid=...: 0, model in [...]: 0}` -- and 0-of-0 would then be
    # rendered as "these clauses eliminated everything".
    open(led, "w").close()
    _st, doc = ui_call(a, V1 + "search", qs)
    e = doc["empty"]
    check("api/empty: a present but empty ledger is `no-data`",
          doc["outcome"], "no-data")
    check("api/empty: and no clause is blamed for an empty corpus",
          (e["eliminated"], e["sole_cause"]), ({}, None))
    check("api/empty: scanned is honestly zero", e["scanned"], 0)
    check_true("api/empty: and it says what to do", bool(e["remedy"]))

    os.remove(led)
    _st, gone = ui_call(a, V1 + "search", qs)
    g = gone["empty"]
    check("api/empty: a missing ledger is `no-data` too",
          gone["outcome"], "no-data")
    check("api/empty: with no clause blamed either",
          (g["eliminated"], g["sole_cause"]), ({}, None))
    check_true("api/empty: the message separates it from an account that made "
               "no requests", "not the same as" in g["message"])
    check_true("api/empty: and the engine's own sentence is used, which is the "
               "only one that can tell a missing ledger from an empty one",
               "no ledger file exists" in g["message"])


def test_api_filtered_to_nothing_names_the_clause_that_did_it():
    """Three empty answers, and this is the one that can point at a cause.

    The per-clause elimination counts come out of `duck.accounting_sql` in the
    SAME scan -- one extra conditional aggregate per clause, not a query per
    clause -- so naming the culprit costs nothing.
    """
    if not have_duck():
        _skip("api: filtered-to-nothing (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    _st, doc = ui_call(a, V1 + "search",
                       "account=%s&where.model=no-such-model" % u)
    e = doc["empty"]
    check("api/empty: a filter that removes everything is named as such",
          doc["outcome"], "filtered-to-nothing")
    # The engine's own clause label, read off `query.clauses_of`: it names the
    # PREDICATE.  The remedy names the PARAMETER, which is the thing the caller
    # can actually change.
    check("api/empty: the sole cause is the clause that did it",
          e["sole_cause"], "model in ['no-such-model']")
    check_true("api/empty: scanned is the rows it really read", e["scanned"] > 0)
    check("api/empty: matched is zero", e["matched"], 0)
    check_true("api/empty: the message quotes the count and the clause",
               "single clause" in e["message"]
               and "model in [" in e["message"])
    check("api/empty: and the remedy names the URL parameter to change, not "
          "the predicate", e["remedy"], "remove or widen `where.model`")


def test_api_every_refusal_carries_a_remedy_and_a_declared_reason():
    """A refusal with no action in it reads as a fault.

    Every refusal path is walked -- transport and query alike -- and each must
    arrive with a reason from the declared set, a non-null remedy and the
    catalogue that lists them.  `detail` may be null; `remedy` may not.
    """
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    cases = [
        (V1 + "nowhere", "", 404, "unknown-endpoint"),
        ("/api/overview", "", 404, "unknown-endpoint"),
        (V1 + "windows", "", 400, "no-account"),
        (V1 + "windows", "account=nobody", 404, "unknown-account"),
        (V1 + "window", "account=%s&kind=5h&resets_at=1" % u, 404,
         "unknown-window"),
        (V1 + "window", "account=%s&kind=9y&resets_at=1" % u, 400,
         "no-window-named"),
        (V1 + "aggregate", "account=%s" % u, 400, "no-group-by"),
        (V1 + "search", "account=%s&stream=q" % u, 400, "unknown-query-key"),
    ]
    if have_duck():
        cases += [
            (V1 + "aggregate", "by=model", 400, "crosses-accounts"),
            (V1 + "aggregate", "by=account", 400, "label-is-not-an-identity"),
            (V1 + "aggregate", "account=%s&by=nonsense" % u, 400,
             "unknown-column"),
            (V1 + "search", "account=%s&since=yesterday" % u, 400,
             "param-not-a-number"),
            (V1 + "search", "account=%s&cursor=c1|bogus" % u, 400,
             "cursor-unreadable"),
            (V1 + "histogram", "account=%s&metric=vibes" % u, 400,
             "unknown-column"),
            # No `account=`: a bucket's `value` is a SUM, and a sum over three
            # accounts is the cross-account total that exists nowhere here.
            # `/aggregate` refused this question by name from the start and
            # `/histogram` answered it, 200 ok, over every ledger in the store.
            (V1 + "histogram", "interval=86400", 400, "crosses-accounts"),
            (V1 + "values", "fields=nope", 400, "unknown-column"),
            (V1 + "search", "account=deadbeef-0000-4000-8000-000000000000",
             404, "unknown-account"),
            (V1 + "search", "account=%s&until=NaN" % u, 400,
             "param-not-a-number"),
            (V1 + "histogram",
             "account=%s&interval=1&since=0&until=999999999" % u, 400,
             "too-many-buckets"),
            (V1 + "lookup", "field=model&value=x", 400, "not-an-id-column"),
        ]
    for path, qs, status, reason in cases:
        st, body = ui_call(a, path, qs)
        check("api/refusal: %s?%s is refused %s" % (path, qs, reason),
              (st, body.get("outcome"), body.get("refusal", {}).get("reason")),
              (status, "unanswerable", reason))
        r = body.get("refusal") or {}
        check_true("api/refusal: ...with a remedy, never null (%s)" % reason,
                   isinstance(r.get("remedy"), str) and bool(r["remedy"]))
        check_true("api/refusal: ...a declared reason (%s)" % reason,
                   r.get("reason") in api.REFUSAL_REASONS)
        check_true("api/refusal: ...and the catalogue that lists them",
                   r.get("catalogue") == V1 + "capabilities")
        check("api/refusal: ...and no result key to flatten it into (%s)"
              % reason, "result" in body, False)


def test_api_a_remedy_is_an_action_a_url_client_can_actually_take():
    """Non-null was asserted; USABLE was not, and the gap held a real bug.

    `srv/store.py` and `srv/query.py` are the module API and word their
    remedies in Python -- `pass account_uuid=`, `call breakdown_per_account()`.
    Both are correct for a caller holding a `DuckStore` and neither is a thing
    a client holding a URL can do.  Measured before the fix: `/aggregate?by=
    model` refused `crosses-accounts` advising "pass account_uuid=", following
    it verbatim sent `?account_uuid=<uuid>`, this API reads `?account=`, the
    parameter was ignored and the IDENTICAL refusal came back.  The old
    assertion passed throughout -- the remedy was a non-null string the whole
    time, and a non-null string that does not work is the confident-wrong
    answer in the one field that exists to prevent it.

    So this walks the refusal, follows its remedy, and requires the follow-up
    to answer.  A test that only re-read the remedy's text would pin the
    spelling; this pins that the advice WORKS.
    """
    # The guard is the first statement after the docstring, so a bare `return`
    # made this test run ZERO assertions and print NOTHING -- indistinguishable
    # in the output from a test that asserted forty things.
    if not have_duck():
        _skip("api: a remedy is an action a client can take "
              "(duckdb not installed)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")

    st, body = ui_call(a, V1 + "aggregate", "by=model")
    r = body.get("refusal") or {}
    check("api/remedy: the cross-account question is still refused",
          (st, r.get("reason")), (400, "crosses-accounts"))
    remedy = r.get("remedy") or ""
    # The two module spellings, named literally.  If either reaches the wire
    # the client has been handed a Python call, not a request it can make.
    for py in ("account_uuid=", "breakdown_per_account()"):
        check_true("api/remedy: no module-API spelling on the wire (%s)" % py,
                   py not in remedy)
    check_true("api/remedy: names the query parameter this API reads",
               "?account=" in remedy)

    # Follow it.  This is the assertion the old one could not make.
    st2, body2 = ui_call(a, V1 + "aggregate", "by=model&account=%s" % u)
    check("api/remedy: following the remedy answers the question",
          (st2, body2.get("outcome")), (200, "ok"))

    # And the alternative it offers is a route that exists and answers.
    check_true("api/remedy: the alternative it names is a real endpoint",
               V1 + "aggregate/per-account" in remedy)
    st3, body3 = ui_call(a, V1 + "aggregate/per-account", "by=model")
    check("api/remedy: ...and that endpoint answers too",
          (st3, body3.get("outcome")), (200, "ok"))

    # The per-account fan-out builds its refusal dicts by hand rather than
    # through `_as_refusal`, so it is a second seam onto the wire and is
    # translated at its own call site.  Nothing in its payload may carry a
    # module spelling either.
    blob = json.dumps(body3)
    for py in ("breakdown_per_account()", "pass account_uuid="):
        check_true("api/remedy: per-account fan-out is respelled too (%s)" % py,
                   py not in blob)

    # ...and a substring-ABSENCE check is satisfied vacuously by `null`, so it
    # says nothing about the field being there at all.  The fan-out is the one
    # route that reports a refusal PER ACCOUNT, nested under
    # `result.accounts[<uuid>].refusal` where no top-level walk reaches it:
    # `test_api_every_refusal_carries_a_remedy_and_a_declared_reason` inspects
    # `body["refusal"]`, and this payload is a 200 `ok` with no top-level
    # refusal at all.  An ordinary typo reaches it.
    _st4, b4 = ui_call(a, V1 + "aggregate/per-account", "by=nonsense")
    nested = [v["refusal"] for v in b4["result"]["accounts"].values()
              if isinstance(v, dict) and "refusal" in v]
    # Load-bearing: without it the loop below passes vacuously the day this
    # branch stops being reachable, which is how the seam went unguarded.
    check_true("api/remedy: the fan-out produced nested refusals to check",
               len(nested) > 0)
    for r4 in nested:
        check_true("api/remedy: a NESTED refusal carries a remedy too, "
                   "never null",
                   isinstance(r4.get("remedy"), str) and bool(r4["remedy"]))
        check_true("api/remedy: a NESTED refusal names a declared reason",
                   r4.get("reason") in api.REFUSAL_REASONS)
        check_true("api/remedy: a NESTED refusal points at the catalogue",
                   r4.get("catalogue") == api.CATALOGUE)


def test_suite_every_skip_announces_itself_and_is_counted():
    """A guard that returns without a word is the cardinal sin in the suite.

    Three `have_duck()` guards were a bare `return`.  One of them --
    `test_api_a_remedy_is_an_action_a_url_client_can_actually_take`, the test
    written for the remedy bug -- had the guard as its first statement, so
    without duckdb it ran ZERO assertions and printed NOTHING.  The runner
    prints `184 test functions` either way, so no byte of the output told a
    reader that a test had vanished.  The two others silently dropped both
    EMPTY outcomes and the whole cross-account refusal half respectively: the
    two headline guarantees of the DuckDB swap, quietly untested.

    It also blinds `mutate.py`, which reports a control as SURVIVED -- "nothing
    asserts" -- when the assertion merely never ran.  A skip that prints
    nothing cannot be counted, so this is the property that makes that guard
    complete.

    Derived with `ast`, not a regex, and asserted over the WHOLE suite rather
    than over the three sites that were wrong: a hand-listed set of exceptions
    is a second place to forget, and the thing it forgets is a test that no
    longer runs.  `_skip` rather than `print`, because a printed skip is not
    counted and the summary line has to be able to say how many there were.
    """
    import ast as _ast
    src = open(os.path.join(HERE, "test_all.py"), encoding="utf-8").read()
    tree = _ast.parse(src)

    def announces(node):
        """Does this `if` body reach `_skip(...)` before it returns?"""
        for stmt in node.body:
            for sub in _ast.walk(stmt):
                if (isinstance(sub, _ast.Call)
                        and isinstance(sub.func, _ast.Name)
                        and sub.func.id == "_skip"):
                    return True
            if isinstance(stmt, _ast.Return):
                return False
        return False

    silent = []
    guarded = 0
    for fn in _ast.walk(tree):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        for node in _ast.walk(fn):
            if not isinstance(node, _ast.If):
                continue
            # `if not have_duck(): ...` -- the skipping shape.  `if
            # have_duck(): ...` is the opposite, an extra-assertions block,
            # and needs no announcement because nothing is dropped.
            t = node.test
            if not (isinstance(t, _ast.UnaryOp)
                    and isinstance(t.op, _ast.Not)
                    and isinstance(t.operand, _ast.Call)
                    and isinstance(t.operand.func, _ast.Name)
                    and t.operand.func.id == "have_duck"):
                continue
            # Only guards that actually skip something -- i.e. that return.
            if not any(isinstance(x, _ast.Return)
                       for stmt in node.body for x in _ast.walk(stmt)):
                continue
            guarded += 1
            if not announces(node):
                silent.append("%s (line %d)" % (fn.name, node.lineno))

    check("suite: every `if not have_duck(): return` announces itself",
          sorted(silent), [])
    # Load-bearing, exactly as `len(nested) > 0` is in the remedy test: if the
    # walk stops matching -- the guard renamed, the shape changed -- `silent`
    # is empty and the assertion above passes having examined nothing.
    check_true("suite: ...and the walk really found the guards (%d)" % guarded,
               guarded >= 20)
    # The counted mechanism, not a bare `print`: `SKIPPED` is what the summary
    # line and `mutate.py`'s refusal both read.  The needle is assembled from
    # pieces so this line does not match ITSELF -- the first draft did, and a
    # self-matching scan is a test that can only ever fail.
    needle = 'print("' + "     " + "SKIP" + "  "
    check("suite: no skip is announced by an uncounted print",
          [ln.strip() for ln in src.splitlines()
           if needle in ln and "%" not in ln], [])


def test_mutate_refuses_a_skipping_baseline_as_well_as_a_red_one():
    """The harness that proves the suite is not decoration was decoration.

    Run as documented on a machine without duckdb, the matrix reported
    `118 caught, 42 SURVIVED, 1 BAD PATCH`; under an interpreter that has
    duckdb, the identical matrix over the identical tree reported
    `161 caught, 0 survived, 0 bad patches`.  All 42 were FALSE.  They did not
    survive because nothing asserts the behaviour -- they survived because the
    assertions self-skipped, and `mutate.py` then printed, for each,
    "SURVIVED <id> -- nothing asserts: <behaviour>".

    The 42 were not peripheral: `store-crosses-accounts`,
    `api-cross-account-total`, `api-histogram-crosses-accounts`,
    `store-empty-answers-merged`, `api-two-empties-become-one`,
    `api-coverage-stripped`, `api-remedy-keeps-the-module-spelling` -- every
    guarantee the DuckDB swap put at risk, each reported as unguarded when it
    was merely unrun.  `main()` refused a RED tree and had no guard against a
    SKIPPING one.

    Driven through the real `main()` with `run_suite` substituted, because the
    property is about what `main` DOES with the output, not about the string.
    """
    import importlib
    sys.path.insert(0, HERE)
    mut = importlib.import_module("mutate")
    green = "1076 passed, 0 failed, 43 skipped, 185 test functions, 7.00s"
    skipline = "     SKIP  api: values (duckdb not installed)"

    def drive(out_text):
        """`main()` over a canned baseline, returning (rc, printed)."""
        real_run, real_out = mut.run_suite, sys.stdout
        m = re.search(r"(\d+) passed, (\d+) failed", out_text)
        mut.run_suite = lambda _d: (int(m.group(1)), int(m.group(2)), out_text)
        buf = io.StringIO()
        sys.stdout = buf
        try:
            # A filter that selects exactly one mutation, so that if a guard
            # DOES let the run through, the test costs one row and not the
            # whole matrix.
            rc = mut.main([mut.MUTATIONS[0][0]])
        finally:
            mut.run_suite, sys.stdout = real_run, real_out
        return rc, buf.getvalue()

    rc, printed = drive(green + "\n" + skipline)
    check("mutate: a SKIPPING baseline is refused, exit 2", rc, 2)
    check_true("mutate: ...and says so, naming the count",
               "refusing to mutate a SKIPPING tree" in printed
               and "1 baseline test(s) self-skipped" in printed)
    check_true("mutate: ...and reports NO survivor from a run it did not do",
               "SURVIVED  " not in printed)
    check_true("mutate: ...and echoes the skip so the reader sees which",
               "api: values" in printed)

    # The baseline LINE itself must carry the count: a reader skimming one
    # line is how 40 skips went unnoticed under a "1073 passed, 0 failed".
    check_true("mutate: the baseline line states the skip count",
               "baseline: 1076 passed, 0 failed, 1 skipped" in printed)

    # The red-tree guard still works, and is still distinct from the new one.
    rc2, printed2 = drive("5 passed, 2 failed, 0 skipped, 9 test functions, "
                          "1.00s")
    check("mutate: a RED baseline is still refused, exit 2", rc2, 2)
    check_true("mutate: ...by the red guard, not the skipping one",
               "refusing to mutate a red tree" in printed2
               and "SKIPPING" not in printed2)

    # And a clean baseline is NOT refused -- without this the two assertions
    # above pass for a `main()` that refuses everything, which would be a
    # harness that never runs.
    rc3, printed3 = drive("1869 passed, 0 failed, 0 skipped, 185 test "
                          "functions, 13.00s")
    check_true("mutate: a clean baseline is allowed through to the matrix",
               "refusing" not in printed3)


def test_store_the_pooled_opt_out_never_reaches_a_route():
    """`pooled=True` is one earned exception, not a door.

    `store.facets` and `store.field_availability` refuse to pool accounts, and
    `pooled=True` switches that off.  It exists for exactly one caller:
    `store.breakdown` asks for the grouping column's cardinality on the
    `by="account_uuid"` path, which is the one cross-account grouping this
    module ALLOWS -- one bucket is one account, no total row.  Refusing that
    would break the permitted case in the name of the forbidden one.

    But an escape hatch reachable from `api.py` is the hole all over again,
    arriving through the tidier door of "the store already checks it".  So the
    hatch is asserted to be unreachable from the layer that serves URLs,
    rather than trusted to stay where it was put -- the same reasoning as
    `_crosses_accounts` being extracted in the first place.
    """
    import ast as _ast
    apath = os.path.join(fx.SRV_DIR, "srv", "api.py")
    tree = _ast.parse(open(apath, encoding="utf-8").read())
    offenders = []
    for node in _ast.walk(tree):
        if not isinstance(node, _ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "pooled":
                offenders.append("api.py line %d" % node.lineno)
    check("store: `api.py` never asks the store to pool accounts",
          offenders, [])

    # Load-bearing: if the keyword is ever renamed, the walk above matches
    # nothing and passes having checked nothing.  Assert the hatch still
    # exists, with exactly the one caller that earned it.
    spath = os.path.join(fx.SRV_DIR, "srv", "store.py")
    ssrc = open(spath, encoding="utf-8").read()
    stree = _ast.parse(ssrc)
    passes = [n.lineno for n in _ast.walk(stree)
              if isinstance(n, _ast.Call)
              and any(kw.arg == "pooled"
                      and isinstance(kw.value, _ast.Constant)
                      and kw.value.value is True for kw in n.keywords)]
    check("store: exactly one caller passes pooled=True", len(passes), 1)
    takers = [n.name for n in _ast.walk(stree)
              if isinstance(n, _ast.FunctionDef)
              and any(a.arg == "pooled" for a in n.args.args)]
    check("store: and exactly the three functions accept it",
          sorted(takers),
          ["_refuse_if_crosses", "facets", "field_availability"])


def test_api_an_unknown_query_parameter_is_refused_not_dropped():
    """A misspelled parameter used to come back as the UNFILTERED answer, ok.

    `spec_from` picked the keys it knew and never looked at the remainder, so
    the filter vanished and nothing in the payload said so -- only the `spec`
    echo went quiet, a diff the client would have to compute for itself.
    Measured on the real fixtures, alpha's 3 rows against the store's 8:
    `?where.model=claude-sonnet-5` matched 2, every misspelling of it matched
    3, `outcome: ok` throughout.

    The sharpest case is the scoping parameter, because `?account=` is now the
    guard that stops a cross-account total:

        ?stream=a&account=<alpha>       -> 3 rows, 1 account,  1 email
        ?stream=a&account_uuid=<alpha>  -> 8 rows, 3 accounts, 3 emails, ok

    `account_uuid=` is this module's own keyword and the spelling
    `store._crosses_accounts` put in its remedy until `wire_remedy()` landed,
    so a client that followed the remedy verbatim was silently unscoped and
    handed the pooled answer.  And `query.compile_query`, the declared oracle,
    refuses an unknown spec key BY NAME while `unknown-query-key` is published
    in `capabilities.refusal_reasons` -- a refusal the contract promised and
    the wire could not produce.
    """
    if not have_duck():
        _skip("api: unknown query parameters (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")

    def rows_of(qs):
        st, b = ui_call(a, V1 + "search", qs)
        r = (b.get("result") or {}).get("rows") or []
        return st, b, len(r), len({x.get("account_uuid") for x in r})

    # The correct spellings still answer, and still filter.
    st, _b, n_all, acc_all = rows_of("stream=a&account=" + u)
    check("api/params: the correct scoping parameter answers", st, 200)
    check("api/params: ...scoped to one account", (n_all, acc_all), (3, 1))
    st, _b, n_f, _x = rows_of("account=%s&where.model=claude-sonnet-5" % u)
    check("api/params: a real `where.` clause filters", (st, n_f), (200, 2))

    # The module spelling is REFUSED, not silently unscoped.  This is the
    # assertion that would have failed before the fix, with 8 rows and three
    # accounts behind a 200.
    st, b, n_bad, acc_bad = rows_of("stream=a&account_uuid=" + u)
    check("api/params: ?account_uuid= is refused, not silently dropped",
          (st, b.get("refusal", {}).get("reason")), (400, "unknown-query-key"))
    check_true("api/params: ...and no result is served at all",
               "result" not in b and (n_bad, acc_bad) == (0, 0))
    # `.get` throughout: when this regresses there is no `refusal` key at all,
    # and a KeyError would abort the test at the first assertion -- so the
    # report would name one symptom and stay silent about the eight routes
    # checked below.
    ref = b.get("refusal") or {}
    rem = ref.get("remedy") or ""
    check_true("api/params: ...and the remedy names the spelling that works",
               "?account=" in rem)
    check_true("api/params: ...and says which parameters this route reads",
               "stream" in rem and "cursor" in rem)
    check_true("api/params: ...and names the offending key in `detail`",
               "account_uuid" in (ref.get("detail") or ""))

    # Every misspelling measured live, each of which returned the unfiltered
    # answer as `ok`.
    for bad in ("wher.model", "wheres.model", "sinc", "untill", "tex",
                "limitt", "ordre", "strem", "bogus"):
        st, b, n, _x = rows_of("account=%s&%s=1" % (u, bad))
        check("api/params: ?%s= is refused rather than ignored" % bad,
              (st, b.get("refusal", {}).get("reason")),
              (400, "unknown-query-key"))

    # `where.<col>` and `not.<col>` are open-ended -- the column is part of
    # the key -- so the prefix must survive, and an unknown COLUMN inside one
    # is still refused a layer down by `compile_query` rather than here.
    st, b, _n, _x = rows_of("account=%s&not.model=claude-sonnet-5" % u)
    check("api/params: `not.` is a known prefix, not an unknown key", st, 200)
    st, b, _n, _x = rows_of("account=%s&where.nonsense=1" % u)
    check("api/params: an unknown COLUMN is still `unknown-column`",
          (st, b.get("refusal", {}).get("reason")), (400, "unknown-column"))

    # `where.` is meaningful only where a spec is built.  Accepted everywhere
    # it is the same silent drop one door along: `/accounts?where.model=x`
    # answering the unfiltered list as `ok`.
    st, b = ui_call(a, V1 + "accounts", "where.model=claude-sonnet-5")
    check("api/params: a filter on a route that cannot filter is refused",
          (st, b.get("refusal", {}).get("reason")), (400, "unknown-query-key"))

    # And the correctly-spelled key that a route cannot honour is refused by
    # name rather than popped: `/aggregate/per-account?account=` asked to
    # scope a fan-out and used to be granted by being ignored -- 200 ok, three
    # accounts in the result.
    st, b = ui_call(a, V1 + "aggregate/per-account", "by=model&account=" + u)
    check("api/params: scoping the per-account fan-out is refused by name",
          (st, b.get("refusal", {}).get("reason")), (400, "unknown-query-key"))
    check_true("api/params: ...and names the route that CAN be scoped",
               V1 + "aggregate?account=" in
               ((b.get("refusal") or {}).get("remedy") or ""))
    st, b = ui_call(a, V1 + "aggregate/per-account", "by=model")
    check("api/params: ...while the unscoped fan-out still answers",
          (st, b["outcome"]), (200, "ok"))

    # Every route, not just `/search`: six of them never call `spec_from` at
    # all and were equally silent, which is why the check sits in `handle`.
    for route in sorted(api.ROUTE_PARAMS):
        st, b = ui_call(a, V1 + route, "zzz_not_a_parameter=1")
        check("api/params: /%s refuses an unknown key too" % route,
              (st, b.get("refusal", {}).get("reason")),
              (400, "unknown-query-key"))
        # Every clause of the remedy must be TRUE OF THIS ROUTE.  A refusal
        # that tells `/accounts` to pass `?account=`, or offers
        # `where.<column>=` to a route that cannot filter, is the same
        # confident wrong instruction the refusal replaced -- just better
        # punctuated.
        rem = (b.get("refusal") or {}).get("remedy") or ""
        check("api/params: /%s offers the filter prefixes iff it filters"
              % route,
              "where.<column>=" in rem, route in api.SPEC_ROUTES)
        check("api/params: /%s offers ?account= iff it reads one" % route,
              "?account=<uuid>" in rem,
              "account" in (api.ROUTE_PARAMS[route] or ()))
        for p_name in (api.ROUTE_PARAMS[route] or ()):
            check_true("api/params: /%s names `%s` in its remedy"
                       % (route, p_name), p_name in rem)


def test_api_the_route_parameter_table_is_derived_not_trusted():
    """`ROUTE_PARAMS` is a hand-written list, so it is checked against the code.

    A hand-maintained catalogue is a second place to forget, and forgetting
    here is worse than the bug it fixes: an undeclared parameter that a route
    really reads is now REFUSED, so the route breaks outright.  `/diagnostics`
    proved it immediately -- it reads `account` one call deep through
    `_account_or_refusal`, the first draft of the table said it read nothing,
    and two tests went red.  That is the failure this derivation makes
    permanent rather than a thing caught once by luck.

    Equality, not containment, in both directions: an undeclared key breaks a
    real route, and a declared key nothing reads silently accepts junk again.
    """
    import ast as _ast
    src = open(os.path.join(fx.SRV_DIR, "srv", "api.py"),
               encoding="utf-8").read()
    tree = _ast.parse(src)
    cls = [n for n in _ast.walk(tree)
           if isinstance(n, _ast.ClassDef) and n.name == "Api"][0]
    methods = {f.name: f for f in cls.body
               if isinstance(f, _ast.FunctionDef)}
    module = {f.name: f for f in tree.body
              if isinstance(f, _ast.FunctionDef)}
    known = dict(module)
    known.update(methods)

    def reads(fn, seen):
        """Every constant params/multi key `fn` reads, following its calls.

        Transitive, because the reads that go missing are the ones a helper
        makes: `_known_account_or_refusal(snap, params)` is where `account`
        lives for six routes.
        """
        if fn.name in seen:
            return set()
        seen.add(fn.name)
        out = set()
        for n in _ast.walk(fn):
            if isinstance(n, _ast.Call):
                f = n.func
                nm = (f.attr if isinstance(f, _ast.Attribute)
                      else f.id if isinstance(f, _ast.Name) else "")
                if (nm == "get" and isinstance(f, _ast.Attribute)
                        and isinstance(f.value, _ast.Name)
                        and f.value.id in ("params", "multi")
                        and n.args
                        and isinstance(n.args[0], _ast.Constant)):
                    out.add(n.args[0].value)
                if (nm in ("_int_param", "_num_param") and len(n.args) > 1
                        and isinstance(n.args[1], _ast.Constant)):
                    out.add(n.args[1].value)
                if nm in known and nm != fn.name:
                    out |= reads(known[nm], seen)
            if (isinstance(n, _ast.Subscript)
                    and isinstance(n.value, _ast.Name)
                    and n.value.id in ("params", "multi")
                    and isinstance(n.slice, _ast.Constant)):
                out.add(n.slice.value)
        return out

    derived = {}
    for name, fn in methods.items():
        if not name.startswith("_v1_"):
            continue
        route = name[len("_v1_"):].replace("__", "/").replace("_", "-")
        derived[route] = reads(fn, set())

    check("api/params: the table names exactly the routes that exist",
          sorted(api.ROUTE_PARAMS), sorted(derived))
    for route in sorted(derived):
        check("api/params: /%s declares exactly what it reads" % route,
              sorted(api.ROUTE_PARAMS.get(route) or ()),
              sorted(derived[route]))
    # Load-bearing: if the walk stops finding reads, every comparison above
    # is [] == [] and passes having checked nothing.
    check_true("api/params: ...and the derivation really found reads (%d)"
               % sum(len(v) for v in derived.values()),
               sum(len(v) for v in derived.values()) >= 30)

    # `SPEC_ROUTES` decides which routes accept `where.<col>=`/`not.<col>=`,
    # and is the same hand-written-list risk: a route that starts building a
    # spec and is not added has its FILTERS refused, which is a working
    # request turned into a 400.
    def calls_spec_from(fn, seen):
        if fn.name in seen:
            return False
        seen.add(fn.name)
        for n in _ast.walk(fn):
            if isinstance(n, _ast.Call):
                f = n.func
                nm = (f.attr if isinstance(f, _ast.Attribute)
                      else f.id if isinstance(f, _ast.Name) else "")
                if nm == "spec_from":
                    return True
                if nm in known and nm != fn.name \
                        and calls_spec_from(known[nm], seen):
                    return True
        return False

    spec_routes = sorted(
        name[len("_v1_"):].replace("__", "/").replace("_", "-")
        for name, fn in methods.items()
        if name.startswith("_v1_") and calls_spec_from(fn, set()))
    check("api/params: SPEC_ROUTES is exactly the routes that build a spec",
          sorted(api.SPEC_ROUTES), spec_routes)

    # And the published catalogue is the SAME table, not a second copy.  The
    # two were listed separately and had already drifted on four routes --
    # `/aggregate` published neither `limit` nor `order`, and
    # `/aggregate/per-account` published neither `account`, `limit`, `order`
    # nor `text` -- so a client built from `/capabilities` knew less than the
    # server accepted.  Now that an undeclared key is REFUSED, a drift between
    # them decides whether a request works.
    for route, params in api.ROUTE_PARAMS.items():
        published = api.ENDPOINT_PARAMS[api.PATH_V1 + route]
        check("api/params: /%s publishes exactly what it enforces" % route,
              sorted(x for x in published
                     if not x.startswith(("where.", "not."))),
              sorted(params))
        check("api/params: /%s publishes the filter prefixes iff it filters"
              % route,
              [x for x in published if x.startswith(("where.", "not."))] != [],
              route in api.SPEC_ROUTES)


def test_api_the_refusal_vocabulary_is_derived_from_the_source():
    """The closed set is served, and it is derived rather than hand-listed.

    A hardcoded copy is a second place to forget, which is the same defect as
    the allow-list one layer down.  `query.REASONS` is already derived from its
    own `Unanswerable(` call sites; this asserts the API's union of that with
    its transport reasons is exactly what `capabilities` publishes, and that
    every transport reason is one this module can really emit.
    """
    _root, _store, a = ui_store()
    _st, doc = ui_call(a, V1 + "capabilities")
    served = doc["result"]["refusal_reasons"]
    check("api/capabilities: the served vocabulary is query's plus transport's",
          sorted(served),
          sorted(set(query.REASONS) | set(api.TRANSPORT_REASONS)))
    src = open(os.path.join(fx.SRV_DIR, "srv", "api.py"),
               encoding="utf-8").read()
    emitted = set(re.findall(r'_refusal\(\s*\n?\s*"([a-z0-9-]+)"', src))
    declared = set(api.TRANSPORT_REASONS)
    # `reader-failed`, `no-view` and `duckdb-missing` are raised by the door's
    # handler around this module rather than inside it, so they are named here
    # rather than found by the scan -- and asserted against `serve.py`'s source
    # so a rename in one file fails rather than silently un-declares a reason.
    ssrc = open(os.path.join(fx.SRV_DIR, "srv", "serve.py"),
                encoding="utf-8").read()
    from_door = set(re.findall(r'api\._refusal\(\s*\n?\s*"([a-z0-9-]+)"', ssrc))
    check("api/capabilities: every transport reason is emitted somewhere",
          sorted(declared - emitted - from_door), [])


def test_api_the_legacy_routes_are_gone_and_say_where_the_api_moved():
    """404, with the version in the remedy.

    Their only consumer was `srv/ui.py`, which is deleted; keeping a second
    envelope alive for a client that does not exist is ceremony that has to be
    tested for ever.  A 404 with no route out of it is how a client author
    concludes the server is broken, so the remedy names the new base and the
    catalogue.
    """
    _root, _store, a = ui_store()
    for name, moved_to in api.LEGACY_ROUTES.items():
        st, body = ui_call(a, "/api/" + name, "account=" + fx.uuid_of("alpha"))
        check("api/legacy: /api/%s is gone" % name,
              (st, body["refusal"]["reason"]), (404, "unknown-endpoint"))
        check_true("api/legacy: ...and the remedy names the versioned base",
                   V1 in body["refusal"]["remedy"])
        # The successor, not just "it moved": this is the one thing the server
        # knows and the client cannot guess.
        check_true("api/legacy: ...and the route that replaced it (%s -> %s)"
                   % (name, moved_to),
                   V1 + moved_to in body["refusal"]["remedy"])
        check_true("api/legacy: ...which is a route that really exists",
                   V1 + moved_to in a.routes())
    check("api/legacy: no unversioned route survives",
          [r for r in a.routes() if not r.startswith(V1)], [])


def test_api_a_route_name_and_its_path_round_trip():
    """One bijection, spelled once in each direction.

    `__` is a slash and a single `_` is a hyphen, so `aggregate/per-account`
    and `_v1_aggregate__per_account` are one string.  A second hand-maintained
    table of routes is a place to forget an endpoint -- which is how a route
    ends up reachable and undocumented, or documented and 404.
    """
    _root, _store, a = ui_store()
    for route in a.routes():
        tail = route[len(V1):]
        back = a._route_name(a._method_suffix(tail))
        check("api/routes: %s round-trips through both transforms" % route,
              back, tail)
        st, _body = ui_call(a, route, "")
        check_true("api/routes: ...and %s is reachable" % route, st != 404)
    check_true("api/routes: the hyphenated route is served under its real name",
               V1 + "aggregate/per-account" in a.routes())


# ---- coverage ---------------------------------------------------------------

def test_api_coverage_known_is_true_exactly_when_a_fraction_exists():
    """The tri-state is carried by a BOOLEAN, and the null corroborates it.

    JSON `null` alone is not enough: `x || 0`, `Number(null)`, `d3.sum` and
    every charting library in existence turn it into zero without a word.  And
    `0.0` is not "nobody said" -- it is the positive claim that NOBODY WAS
    LISTENING.  So the two must not be reachable from one another, and both
    directions of the iff are asserted.
    """
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    seen_unknown = seen_known = 0
    docs = [ui_call(a, V1 + "windows", "account=" + u)[1],
            ui_call(a, V1 + "accounts")[1]]
    if have_duck():
        docs += [ui_call(a, V1 + "search", "account=" + u)[1],
                 ui_call(a, V1 + "aggregate", "account=%s&by=model" % u)[1],
                 ui_call(a, V1 + "histogram",
                         "account=%s&interval=3600" % u)[1]]
    for doc in docs:
        for cov in _coverages(doc):
            if _check_coverage_shape(cov, "a coverage slot"):
                continue
            if cov["known"]:
                seen_known += 1
                check_true("api/coverage: known implies a fraction",
                           cov["fraction"] is not None)
            else:
                seen_unknown += 1
                check_true("api/coverage: unknown implies no fraction",
                           cov["fraction"] is None)
                check("api/coverage: and NEVER 0.0, which is the claim that "
                      "nobody was listening", cov["fraction"] == 0.0, False)
    check_true("api/coverage: the unknown case really occurs on this capture",
               seen_unknown > 0)

    # The other direction, on a store where a machine HAS spoken: an authored
    # attestation, because nothing in this repository emits one.
    att = fx.attestation(fx.uuid_of("alpha"), "5h", 1786598400, "darwin",
                         [[1786580400, 1786589400, True]])
    _r2, _s2, a2 = ui_store(extra=[("alpha", wire.STREAM_ATTEST,
                                    [ui_line(att)])])
    _st, v = ui_call(a2, V1 + "windows", "account=%s&kind=5h" % u)
    w = v["result"]["windows"][0]
    check("api/coverage: an attestation covering half the window reports half",
          w["coverage"]["fraction"], 0.5)
    check("api/coverage: named as reporting", w["coverage"]["hosts_reporting"],
          ["darwin"])


def test_api_the_basis_says_whether_the_number_was_measured():
    """`basis_is` is `measured` for exactly one basis, and it is served.

    A client that hardcoded "no-attestation means unknown" is a client that
    renders a basis this server adds later as if it were measured, so the
    mapping is DATA at `/api/v1/capabilities` and the client holds no copy.
    """
    _root, _store, a = ui_store()
    _st, caps = ui_call(a, V1 + "capabilities")
    m = caps["result"]["coverage_basis"]
    check_true("api/coverage: the parameterised basis family is served too, "
               "as data, so a client still holds no copy of the rule",
               bool(caps["result"]["coverage_basis_prefixes"]))
    check("api/coverage: the basis map is served whole",
          sorted(m), sorted(api.COVERAGE_BASIS))
    check("api/coverage: and `attestations` is the only measured one",
          sorted(k for k, v in m.items() if v == "measured"), ["attestations"])
    seen = 0
    docs = [ui_call(a, V1 + "windows", "account=" + fx.uuid_of("alpha"))[1],
            ui_call(a, V1 + "accounts")[1],
            ui_call(a, V1 + "window",
                    "account=%s&kind=5h&resets_at=1786598400"
                    % fx.uuid_of("alpha"))[1]]
    for doc in docs:
        for cov in _coverages(doc):
            if _check_coverage_shape(cov, "a coverage slot"):
                continue
            seen += 1
            check_true("api/coverage: every basis on the wire is declared "
                       "by the SERVED catalogue, prefix family included",
                       _basis_declared(caps, cov["basis"]))
            check("api/coverage: basis_is agrees with the served map",
                  cov["basis_is"], api.basis_is(cov["basis"]))
            if cov["basis_is"] == "unknown":
                check("api/coverage: an unknown basis never carries a number",
                      cov["fraction"], None)
    check_true("api/coverage: and the per-window objects were really walked "
               "-- 3 of these routes carry nothing else", seen >= 3)

    # The basis vocabulary is DERIVED from the modules that emit it, so a new
    # one cannot be added without a catalogue entry.  A hand-maintained
    # catalogue is a second place to forget, which is the same defect as the
    # allow-list one layer down.
    emitted = _bases_in(os.path.join(fx.SRV_DIR, "srv", "coverage.py"))
    emitted |= _bases_in(os.path.join(fx.SRV_DIR, "srv", "query.py"),
                         only_coverage_funcs=True)
    check_true("api/coverage: the derivation found the bases at all",
               len(emitted) >= 6)
    check("api/coverage: every basis the core can emit is in the catalogue",
          sorted(b for b in emitted if not _basis_declared(caps, b)), [])


def test_api_placed_fraction_is_null_rather_than_a_confident_one():
    """Never 1.0 for no rows, and never 0.

    0/0 is not 0, and a statement with no report behind it has not placed the
    rows at all -- answering 1.0 there is the confident wrong number this whole
    project exists to avoid.
    """
    if not have_duck():
        _skip("api: placement (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    _st, none = ui_call(a, V1 + "search", "account=%s&since=1&until=2" % u)
    cov = none["result"]["coverage"]
    check("api/coverage: a result with no rows places nothing",
          (cov["placement"]["rows_total"],
           cov["placement"]["placed_fraction"]), (0, None))
    check("api/coverage: and claims no window", cov["basis"],
          "no-window-touched")

    _st, some = ui_call(a, V1 + "search", "account=" + u)
    p = some["result"]["coverage"]["placement"]
    check("api/coverage: a result with rows counts them all four ways",
          p["rows_total"],
          (p["in_closed_windows"] + p["in_open_windows"] + p["unclaimed"]
           + p["undatable"]))


def test_api_coverage_describes_the_rows_it_travelled_with():
    """Computed over the SELECTED rows, in the same call that produced them.

    `_api_requests` once computed coverage over the account's WHOLE row set,
    before the filter ran, and the page rendered it directly beneath the
    matched count while `_api_breakdown` -- which passed `sel.rows` -- got the
    right answer.  One screen contradicted itself in two adjacent panels: `3%
    of this window had a machine reporting - basis: attestations` above `nobody
    said - basis: no-window-touched`, over the same two rows.  The confident
    number was the wrong one.
    """
    if not have_duck():
        _skip("api: coverage follows the filter (duckdb missing)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    _st, allr = ui_call(a, V1 + "search", "account=" + u)
    ts = sorted(r["ts"] for r in allr["result"]["rows"])
    check("api/coverage: the account has three captured rows",
          (allr["result"]["page"]["matched"],
           allr["result"]["coverage"]["placement"]["rows_total"]), (3, 3))

    q = "account=%s&since=%r" % (u, ts[-1])
    _st, one = ui_call(a, V1 + "search", q)
    check("api/coverage: a filter to one row matches one row",
          one["result"]["page"]["matched"], 1)
    check("api/coverage: and its coverage is over that row, not all three",
          one["result"]["coverage"]["placement"]["rows_total"], 1)
    check_true("api/coverage: and it says so on the wire rather than leaving "
               "it to be assumed",
               "not the account" in one["result"]["coverage"]["describes"])

    _st, brk = ui_call(a, V1 + "aggregate", q + "&by=model")
    check("api/coverage: the search and the aggregate beneath it agree about "
          "the rows they describe",
          (one["result"]["coverage"]["placement"]["rows_total"],
           one["result"]["coverage"]["basis"]),
          (brk["result"]["coverage"]["placement"]["rows_total"],
           brk["result"]["coverage"]["basis"]))


def test_api_coverage_travels_on_every_answer_including_the_empty_ones():
    """An empty answer still has a placement and an attestation story.

    A front end that renders coverage only when rows came back is a front end
    that shows an unqualified "0 requests" for a filter that removed
    everything.
    """
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    shapes = [(V1 + "windows", "account=" + u)]
    if have_duck():
        shapes += [
            (V1 + "search", "account=" + u),
            (V1 + "search", "account=%s&since=1&until=2" % u),
            (V1 + "aggregate", "account=%s&by=model" % u),
            (V1 + "aggregate", "account=%s&by=model&since=1&until=2" % u),
            (V1 + "histogram", "account=%s&interval=3600" % u),
            (V1 + "window", "account=%s&kind=5h&resets_at=1786598400" % u),
            (V1 + "lookup", "field=request_id&value=nope&account=" + u),
        ]
    for path, qs in shapes:
        _st, doc = ui_call(a, path, qs)
        cov = (doc.get("result") or {}).get("coverage")
        check_true("api/coverage: %s carries a coverage object" % path,
                   isinstance(cov, dict))
        check_true("api/coverage: ...with the tri-state boolean on it (%s)"
                   % path, isinstance(cov.get("known"), bool))
        check_true("api/coverage: ...and the server's own notes (%s)" % path,
                   isinstance(cov.get("notes"), list))


def test_api_the_coverage_notes_are_the_servers_own_words():
    """Verbatim and in order, so a client writes no sentence of its own.

    A fixed sentence in a client goes stale against a store that contradicts
    it -- which is exactly what happened: a page asserting that nothing emits
    an attestation, printed beside a strip reading `basis: attestations`.
    """
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    snap = serve.View(_root).load(fx.NOW_JOINT)
    want = query.coverage_for(snap.report, u, list(snap.rows(u)))
    _st, doc = ui_call(a, V1 + "windows", "account=" + u)
    got = doc["result"]["coverage"]["notes"]
    check("api/coverage: the notes are the core's, in the core's order",
          got, list(want.notes))
    check_true("api/coverage: and on this capture they say unknown is not zero",
               any("not zero" in n for n in got))


# ---- no cross-account total -------------------------------------------------

def test_api_no_cross_account_total_exists_in_any_payload():
    """Not one, anywhere, not even for tokens.

    Two accounts are two plans and two denominators.  Every key in every
    payload is walked, and the only ones that may mention a total are totals
    WITHIN one window or one bucket.
    """
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    shapes = [(V1 + "accounts", ""), (V1 + "windows", "account=" + u),
              (V1 + "window", "account=%s&kind=5h&resets_at=1786598400" % u),
              (V1 + "diagnostics", ""), (V1 + "capabilities", "")]
    if have_duck():
        shapes += [(V1 + "search", "account=" + u),
                   (V1 + "aggregate", "account=%s&by=model" % u),
                   (V1 + "aggregate/per-account", "by=model"),
                   (V1 + "histogram", "account=%s&interval=3600" % u),
                   # Account-SCOPED, like every other shape on this list.
                   # Left account-less these two now return refusals, and a
                   # refusal carries no `result`, so the key walk below would
                   # silently stop inspecting their payloads -- the walk would
                   # keep passing while covering two routes less.  The
                   # account-less form is asserted to REFUSE further down.
                   (V1 + "values", "fields=model&account=" + u),
                   (V1 + "fields", "account=" + u)]
    keys = set()
    for path, qs in shapes:
        _st, body = ui_call(a, path, qs)
        keys |= walk_keys(body)
    # `no_total_note` and `no_cross_account_total` are the REFUSAL to total,
    # not a total; `total` is the per-account fan-out's explicit null, asserted
    # below.  Everything else that mentions one must be within a single window
    # or a single bucket.
    check("api: the only totals in any payload are within one window or bucket",
          sorted(k for k in keys
                 if "total" in k
                 and k not in ("no_total_note", "no_cross_account_total")),
          ["rows_total", "total", "weight_total"] if have_duck()
          else ["rows_total", "weight_total"])

    # A PARTIAL skip: the key-name walk above ran.  What is dropped below is
    # the half that requires every stream-A route to REFUSE the account-less
    # question, which is the load-bearing half -- a key-name walk cannot see a
    # total that is not called one.
    if not have_duck():
        _skip("api: cross-account refusals, not just key names "
              "(duckdb not installed)")
        return

    # A KEY-NAME walk cannot see a total that is not called one.  Every
    # stream-A route is asked its account-less question and the answer is
    # required to be a REFUSAL naming the accounts -- because the alternative
    # measured on the real fixtures was `/histogram?interval=86400&metric=cost`
    # answering 0.2466444 over three accounts, which is exactly
    # 0.0592663 + 0.0948947 + 0.0924834, the per-account fan-out summed.
    # `/values` and `/fields` are on this list because they were NOT, and the
    # hole was opened by the DuckDB swap: `git show HEAD:server/srv/serve.py`
    # has six routes and neither existed.  The guard was
    # `store._crosses_accounts`, reached only from `breakdown()` and
    # `histogram()`; `facets()` and `field_availability()` never reached it,
    # so `/values?fields=model` answered `{claude-sonnet-5: 5,
    # claude-haiku-4-5: 3}` with `rows_with_a_value: 8` over three accounts --
    # byte-identical to the per-account fan-out summed by hand -- and
    # `/fields` reported one pooled `cardinality` per column, `email` among
    # them with `distinct_n: 3`.  A count is not called a total, so the
    # key-name walk above is structurally blind to it: this loop is the only
    # thing that can see it, and it fed `values` through the walk without
    # requiring a refusal.
    for path, qs in ((V1 + "values", "fields=model"),
                     (V1 + "values", ""),
                     (V1 + "fields", "")):
        st, doc = ui_call(a, path, qs)
        check("api: %s?%s refuses the account-less question" % (path, qs),
              (st, doc.get("refusal", {}).get("reason")),
              (400, "crosses-accounts"))
        check_true("api: ...and carries no result to render (%s)" % path,
                   "result" not in doc)
        # A refusal must point at something that works, or it is a complaint.
        # `.get`, not `[...]`: when this regresses the payload has no
        # `refusal` at all, and a KeyError aborts the whole test at the FIRST
        # route -- so the report would name `/values` and stay silent about
        # `/fields`, which is the same class of half-told failure the loop
        # exists to catch.
        rem = (doc.get("refusal") or {}).get("remedy") or ""
        check_true("api: ...and its remedy is a URL spelling (%s)" % path,
                   "?account=" in rem and "account_uuid=" not in rem)
        st2, doc2 = ui_call(a, path, (qs + "&" if qs else "") + "account=" + u)
        check("api: ...and naming one account answers it (%s)" % path,
              (st2, doc2["outcome"]), (200, "ok"))

    for metric in ("cost", "input_tokens", "output_tokens",
                   "cache_read_tokens", "duration_ms", "requests"):
        st, doc = ui_call(a, V1 + "histogram",
                          "interval=86400&metric=" + metric)
        check("api: /histogram refuses to bucket %s across accounts" % metric,
              (st, doc.get("refusal", {}).get("reason")),
              (400, "crosses-accounts"))
        check_true("api: ...and names them, so the caller knows which to ask "
                   "for (%s)" % metric,
                   all(x in (doc["refusal"]["detail"] or "")
                       for x in (fx.uuid_of(l) for l in UI_LABELS)))
        # And the sum it refuses to produce really is reachable one account at
        # a time, so the refusal is about the question rather than the data.
        per = []
        for lbl in UI_LABELS:
            _s2, one = ui_call(a, V1 + "histogram",
                               "account=%s&interval=86400&metric=%s"
                               % (fx.uuid_of(lbl), metric))
            per.append(sum(b["value"] for b in one["result"]["buckets"]))
        check_true("api: ...over data that would have totalled to something "
                   "(%s)" % metric, sum(per) > 0)

    _st, fan = ui_call(a, V1 + "aggregate/per-account", "by=model")
    check("api: the per-account fan-out produces no total at all",
          fan["result"]["total"], None)
    check_true("api: ...and says why on the wire",
               "denominators" in fan["result"]["no_total_note"])
    check("api: it answered for every account",
          sorted(fan["result"]["accounts"]),
          sorted(fx.uuid_of(l) for l in UI_LABELS))
    check("api: and a cross-account aggregate is refused by name",
          ui_call(a, V1 + "aggregate", "by=model")[1]["refusal"]["reason"],
          "crosses-accounts")
    check("api: grouping across accounts by a LABEL is refused by its own name",
          ui_call(a, V1 + "aggregate", "by=account")[1]["refusal"]["reason"],
          "label-is-not-an-identity")
    check("api: ...but the same grouping WITHIN one account is allowed",
          ui_call(a, V1 + "aggregate", "account=%s&by=account" % u)[1]
          ["outcome"], "ok")


def test_api_no_bucket_ever_carries_a_combined_token_figure():
    """The four token classes stay four.

    A `tokens` key would be a fifth, and the four are not interchangeable: a
    cache read is ~89% of this user's tokens and is priced differently.
    """
    if not have_duck():
        _skip("api: token classes (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    _st, doc = ui_call(a, V1 + "aggregate",
                       "account=%s&by=model" % fx.uuid_of("alpha"))
    for b in doc["result"]["buckets"]:
        check("api/aggregate: no bucket carries a combined token figure",
              "tokens" in b["stats"], False)
        for f in query.TOKEN_FIELDS:
            check_true("api/aggregate: %s is present as its own class" % f,
                       f in b["stats"])


# ---- the re-derivation ------------------------------------------------------

def test_api_the_reader_re_derives_and_never_caches():
    """Because a window's state is a function of the clock.

    A cached report served two hours later reports as OPEN a window that closed
    an hour ago, and the caveat that would make that honest ("as of 14:02") is
    one nobody reads on a figure that looks live.  So every request re-derives
    from byte zero, which is also what lets a week-late shipment correct a
    window that closed days ago -- the same property from the other end.
    """
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    _st, early = ui_call(a, V1 + "windows", "account=%s&kind=5h" % u,
                         now=1786590000)
    _st, late = ui_call(a, V1 + "windows", "account=%s&kind=5h" % u,
                        now=fx.NOW_JOINT)
    check("api: the same window is open before its resets_at",
          early["result"]["windows"][0]["state"], "open")
    check("api: and closed after it, from the same bytes",
          late["result"]["windows"][0]["state"], "closed")
    check("api: the open one refuses to attribute",
          early["result"]["windows"][0]["refused"], "window-open")
    check("api: every payload counts the derivations it did",
          late["meta"]["derivations"], 2)
    _st, again = ui_call(a, V1 + "windows", "account=%s&kind=5h" % u,
                         now=fx.NOW_JOINT)
    check("api: and two reads of one store at one `now` are identical",
          canon_json(again["result"]["windows"]),
          canon_json(late["result"]["windows"]))


def test_api_the_accounts_answer_from_the_real_capture():
    """The numbers a landing screen needs, read off the fixtures first.

      $ jq -r '.account, .five_hour_pct' usage/tests/fixtures/real-samples.jsonl
        alpha peaks at 10, beta at 49, agent at 1; one 5h window each
    """
    _root, _store, a = ui_store()
    st, doc = ui_call(a, V1 + "accounts")
    check("api/accounts: answers", (st, doc["outcome"]), (200, "ok"))
    accounts = doc["result"]["accounts"]
    check("api/accounts: one entry per account, and no total row",
          len(accounts), 3)
    by_uuid = {x["account_uuid"]: x for x in accounts}
    for lbl, peak in (("alpha", 10.0), ("beta", 49.0), ("agent", 1.0)):
        x = by_uuid[fx.uuid_of(lbl)]
        check("api/accounts: %s's closed 5h window peaked at %s" % (lbl, peak),
              x["last_closed"]["5h"]["peak"], peak)
        check("api/accounts: %s's 7d window is open, so it refuses" % lbl,
              x["open"]["7d"]["refused"], "window-open")
        check("api/accounts: %s is named by its own samples" % lbl,
              x["identity"]["account"], lbl)
        check("api/accounts: with the denominator's name beside it",
              x["identity"]["rate_limit_tier"], "default_raven")
    check_true("api/accounts: the gauge is an observed peak, never a reading",
               "OBSERVED PEAK" in doc["result"]["peak_note"])
    check_true("api/accounts: and its staleness threshold declares itself a "
               "display choice", "not a measurement" in doc["result"]["stale_note"])


def test_api_open_and_last_closed_are_separate_keys_with_the_age_beside_them():
    """The slot that answers "how much of my plan is gone" needs an OPEN window.

    `accountCard` drew the gauge for `open || closed`, so with no open window
    reconstructed it silently fell back to the newest closed one, of any age,
    at full width.  An account whose every record was 40 days old rendered `7d
    closed - 92 %` with nothing on the card saying 40 days.  The page is gone;
    the obligation is not, and it is now an API contract -- a client that finds
    no open window must have nothing to fall back on.
    """
    _root, _store, a = ui_store()
    _st, doc = ui_call(a, V1 + "accounts")
    r = doc["result"]
    check_true("api/accounts: the payload states the staleness threshold",
               "stale_after_s" in r)
    check_true("api/accounts: ...and says it is a choice, not a measurement",
               "not a measurement" in r["stale_note"])
    check_true("api/accounts: the threshold is also in meta, for a client that "
               "reads only that", "stale_after_s" in doc["meta"])
    for x in r["accounts"]:
        check_true("api/accounts: every account states its own silence",
                   "last_sample_age_s" in x)
        check_true("api/accounts: open and last_closed are separate keys",
                   "open" in x and "last_closed" in x)
        check_true("api/accounts: and the age is present even when nothing is "
                   "open", x["last_sample_age_s"] is not None
                   or x["samples_n"] == 0)
    check_true("api/capabilities: the threshold is declared with the other "
               "constraints too",
               ui_call(a, V1 + "capabilities")[1]["result"]["constraints"]
               ["stale_after_s"] == api.STALE_AFTER_S)


def test_api_every_attributed_figure_travels_with_its_coverage():
    """The whole point, asserted on every window of every payload.

    A window nobody watched and a window with a browser session behind it
    produce the same residual, and only coverage separates them -- so a row
    carrying `attributed_pp` without a coverage object beside it is the
    confident-wrong-number failure, whatever the number says.
    """
    _root, _store, a = ui_store()
    seen, attributed = 0, 0
    for lbl in UI_LABELS:
        _st, v = ui_call(a, V1 + "windows", "account=" + fx.uuid_of(lbl))
        for w in v["result"]["windows"]:
            seen += 1
            check_true("api: window %s %s carries a coverage object"
                       % (w["kind"], w["resets_at"]), "coverage" in w)
            if w.get("attributed_pp") is not None:
                attributed += 1
                check("api: which on this capture is 'nobody said', never 0.0",
                      w["coverage"]["fraction"], None)
    check("api: every window in the joint capture was inspected", seen, 6)
    check("api: three of them attributed", attributed, 3)


def test_api_the_residual_and_its_pin_survive_the_endpoint():
    """A pass-through, and the flag that stops a 0.0 reading as evidence.

    On this capture every closed window pins its own rate, so every residual is
    exactly zero BY CONSTRUCTION.  `rate_is_from_this_window` is what a client
    turns into the warning beside it, so it has to reach the client -- and the
    endpoint must add nothing to the row and drop nothing from it.
    """
    _root, _store, a = ui_store()
    rep = reconcile.reconcile_ingest(
        ingest.ingest([b for lbl in UI_LABELS
                       for b in serve.read_batches(_root, fx.uuid_of(lbl))[0]],
                      fx.NOW_JOINT), fx.NOW_JOINT)
    want = query.windows_view(rep, fx.uuid_of("alpha"), kind="5h",
                              include_by=True)["windows"][0]
    _st, got = ui_call(a, V1 + "window",
                       "account=%s&kind=5h&resets_at=1786598400"
                       % fx.uuid_of("alpha"))
    # The endpoint reshapes the by-maps, ADDS the two coverage keys the
    # contract requires wherever a fraction travels, and touches nothing else.
    # Asserted in three parts rather than one loose comparison -- every other
    # field byte for byte, the by-maps carrying the same data under the wire
    # shape, and coverage carrying everything the core computed plus `known`
    # and `basis_is`.
    skip = ("by", "by_tag", "coverage")
    rest_got = {k: v for k, v in got["result"]["window"].items()
                if k not in skip}
    rest_want = {k: v for k, v in want.items() if k not in skip}
    check("api: the endpoint serves the query layer's own row, unaltered",
          canon_json(rest_got), canon_json(rest_want))
    cov_got = dict(got["result"]["window"]["coverage"])
    added = {k: cov_got.pop(k) for k in ("known", "basis_is")}
    check("api: coverage keeps every field the core computed, unchanged",
          canon_json(cov_got), canon_json(want["coverage"]))
    check("api: ...and gains exactly the tri-state boolean and its "
          "classification", added, {"known": False, "basis_is": "unknown"})
    # TWO SHAPES, AND THIS ASSERTION USED TO PIN THE BUG.  `by` is nested
    # (`{dimension: {value: stats}}`) and `by_tag` is FLAT (`{"k=v" or None:
    # stats}`), and the expectation below applied `by`'s conversion to both --
    # the same mistake the endpoint made, spelled out here as the expected
    # value, so a test written for this bug agreed with it.  The endpoint's
    # output was `{None: [...], "test=4": [...]}`: a dict still keyed by `str`
    # and `None`, which `json.dumps(sort_keys=True)` cannot order at all.
    check("api: by carries exactly the core's buckets, in wire shape",
          canon_json(got["result"]["window"].get("by")),
          canon_json({dim: api.bucketise(vals)
                      for dim, vals in (want.get("by") or {}).items()}))
    check("api: by_tag is the FLAT map put into wire shape whole, not "
          "iterated as if its tag values were dimension names",
          canon_json(got["result"]["window"].get("by_tag")),
          canon_json(api.bucketise(dict(want.get("by_tag") or {}))))
    check("api: the residual is zero here",
          got["result"]["window"]["residual_pp"], 0.0)
    if have_duck():
        check_true("api: and the window's request list is there",
                   "rows" in got["result"]["requests"])
    else:
        # The window's own figures come from the reconciler; only the request
        # list is stream A.  It is refused BY NAME with no `rows` key, so a
        # client cannot read the absence as "this window had no requests".
        req = got["result"]["requests"]
        check("api: without the engine the request list is refused by name",
              req["unavailable"]["reason"], "duckdb-missing")
        check("api: ...and carries no rows key to mistake for an empty window",
              "rows" in req, False)
    check("api: and the row says that zero is arithmetic, not a measurement",
          got["result"]["window"]["rate_is_from_this_window"], True)


def test_api_a_lower_bound_is_declared_a_floor_rather_than_a_reading():
    """Three values the core computes, names, and used to hand to nobody.

    `lower_bound` is set when a host is PENDING, with the comment "what is
    computed now can only rise".  A window with fraction 0.0 and one pending
    host rendered as "0% of this window had a machine reporting" -- the DARK
    claim, a positive statement that nobody was listening, printed over the
    state the core defines as "nobody has said yet".
    """
    reset = fx.JOINT_5H_RESETS["alpha"]
    here = fx.attestation(fx.uuid_of("alpha"), "5h", reset, "darwin",
                          [[reset - 18000, reset - 9000, True]],
                          emitted_at=fx.NOW_JOINT - 100)
    elsewhere = fx.attestation(fx.uuid_of("alpha"), "5h", reset - 18000,
                               "laptop", [[reset - 36000, reset - 18000, True]],
                               emitted_at=fx.NOW_JOINT - 100)
    _root, _store, a = ui_store(
        extra=[("alpha", wire.STREAM_ATTEST,
                [ui_line(here), ui_line(elsewhere)])])
    _st, v = ui_call(a, V1 + "windows",
                     "account=%s&kind=5h" % fx.uuid_of("alpha"))
    cov = [w["coverage"] for w in v["result"]["windows"]
           if w["resets_at"] == reset][0]
    check("api: a host that has not spoken for this window is pending",
          cov["hosts_pending"], ["laptop"])
    check("api: so the fraction is declared a floor", cov["lower_bound"], True)

    _st, diag = ui_call(a, V1 + "diagnostics")
    check("api/diag: the store took the attestations",
          diag["result"]["counts"]["attestations_accepted"], 2)
    check("api/diag: so the note that says none exist is not made",
          diag["result"]["attestation_note"], None)
    _r2, _s2, a2 = ui_store()
    _st, diag2 = ui_call(a2, V1 + "diagnostics")
    check_true("api/diag: and it IS made on a store with none",
               "nobody said" in (diag2["result"]["attestation_note"] or ""))


def test_api_the_lower_bound_note_travels_with_every_plan_figure():
    """A plan percentage is a floor over quantised, possibly-stale snapshots.

    The server computes `used_percentage` as `utilization * 100` and the
    process caches it, so a sample can be arbitrarily stale and the only sound
    cross-machine operator is `max`.  A figure derived from them is a lower
    bound, never a reading, and the payload says so rather than leaving it to a
    client's tooltip.
    """
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    for path, qs in ((V1 + "windows", "account=" + u),
                     (V1 + "window",
                      "account=%s&kind=5h&resets_at=1786598400" % u),
                     (V1 + "search", "account=%s&stream=b" % u)):
        _st, doc = ui_call(a, path, qs)
        check_true("api: %s carries the lower-bound note" % path,
                   "LOWER BOUND" in (doc["result"].get("lower_bound_note")
                                     or ""))
    _st, acc = ui_call(a, V1 + "accounts")
    check_true("api: and every payload's meta carries it too",
               "LOWER BOUND" in acc["meta"]["lower_bound_note"])


# ---- the store's own problems ----------------------------------------------

def test_api_an_unreadable_batch_is_named_in_every_payload():
    """`store_problems` travels in every payload and used to render on one.

    Reproduced: a batch truncated mid-record, then the landing screen.  The
    payload carried `store_problems: [{"reason": "interrupted-batch"}]` and the
    card rendered `no window of any kind has closed in this store yet` --
    byte-identical to an account that has never shipped a sample.  A reader was
    shown "this identity used nothing" while the store held the records and the
    reader knew it had failed to read them.
    """
    root, _store, a = ui_store()
    path = os.path.join(root, "accounts", fx.uuid_of("alpha"), "samples.jsonl")
    with open(path, "r+b") as fh:
        fh.truncate(os.path.getsize(path) - 30)

    shapes = [(V1 + "accounts", ""), (V1 + "diagnostics", ""),
              (V1 + "windows", "account=" + fx.uuid_of("alpha")),
              (V1 + "capabilities", "")]
    if have_duck():
        shapes += [(V1 + "search", "account=" + fx.uuid_of("alpha")),
                   (V1 + "aggregate",
                    "account=%s&by=model" % fx.uuid_of("alpha"))]
    for path_, qs in shapes:
        _st, doc = ui_call(a, path_, qs)
        if path_ == V1 + "capabilities":
            # The catalogue is a description of the server, not a read of the
            # store, so it carries no meta -- and a client must not read its
            # silence as "no problems".
            check("api: the catalogue makes no claim about the store",
                  "meta" in doc, False)
            continue
        check_true("api: %s carries meta.store_problems" % path_,
                   "store_problems" in doc.get("meta", {}))
        probs = doc["meta"]["store_problems"]
        # No `if "reason" in p` filter.  It used to have one, and the
        # DuckDB half wrote `problem` rather than `reason`, so every problem
        # that half produced was invisible to the one test that walks every
        # payload for them.  One array, one shape, or the filter is the guard.
        check("api: %s names the unreadable batch" % path_,
              [p["reason"] for p in probs], ["interrupted-batch"])
        check("api: ...and whose it was",
              [p["account_uuid"] for p in probs if "account_uuid" in p],
              [fx.uuid_of("alpha")])


# ---- stream A, through DuckDB ----------------------------------------------

def test_api_search_pages_by_keyset_and_says_what_a_page_cannot_show():
    """Keyset over `(ts, request_id)`, both immutable per row.

    This store is designed to accept a week-late shipment, and under an offset
    a row appended into the middle of a page served yesterday shifts every
    later row by one and skips a real request in silence.  What keyset cannot
    show -- a row arriving BEHIND the cursor -- is printed on every page with
    the remedy.
    """
    if not have_duck():
        _skip("api: keyset paging (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    seen, cursor, pages = [], None, 0
    while True:
        qs = "account=%s&limit=1" % u + ("&cursor=" + urllib.parse.quote(cursor)
                                         if cursor else "")
        _st, doc = ui_call(a, V1 + "search", qs)
        r = doc["result"]
        seen += [x["request_id"] for x in r["rows"]]
        pages += 1
        check_true("api/search: every page states what it cannot show",
                   r["page"]["late_arrival_possible"] is True
                   and bool(r["page"]["remedy"]))
        cursor = r["page"]["next_cursor"]
        if not cursor or pages > 10:
            break
    check("api/search: every row is served exactly once across the pages",
          sorted(seen), sorted(set(seen)))
    check("api/search: and all three of them arrived", len(seen), 3)
    check_true("api/search: the keyset rule is stated on the page",
               any("(ts, request_id)" in n for n in
                   doc["result"]["page"]["notes"]))
    check("api/capabilities: offset paging is declared absent",
          ui_call(a, V1 + "capabilities")[1]["result"]["constraints"]
          ["offset_paging"], False)


def test_api_search_can_be_asked_to_verify_the_engine_against_the_oracle():
    """The narrowing contract, checkable on demand.

    A storage layer may return a superset and must NEVER return a subset: a
    stale or partial index that omits a matching row produces a smaller number
    with no symptom.  `verify=1` re-runs the pure predicate over the same rows
    and refuses `engine-disagreement` rather than answering, because an answer
    two engines disagree about is the one thing this seam must not serve
    quietly.
    """
    if not have_duck():
        _skip("api: verify (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    _st, doc = ui_call(a, V1 + "search", "account=%s&verify=1" % u)
    check("api/search: the two engines agree over the real capture",
          doc["result"]["engine"]["verified"], True)
    check("api/search: and the engine names itself and its predicate",
          (doc["result"]["engine"]["name"],
           doc["result"]["engine"]["predicate"]), ("duckdb", "sql"))
    _st, plain = ui_call(a, V1 + "search", "account=%s" % u)
    check("api/search: without verify= the claim is null, not a bare True",
          plain["result"]["engine"]["verified"], None)

    # And the half that matters: a storage layer returning a SUBSET must be
    # refused, not reported.  Agreement over the real capture proves nothing on
    # its own -- `verified = True` hardcoded passes it -- so the engine is
    # forced to drop a row and the answer must become a refusal.
    class Lossy(duckstore.DuckStore):
        def select(self, q, account_uuid=None, columns=None):
            sel = duckstore.DuckStore.select(self, q,
                                             account_uuid=account_uuid,
                                             columns=columns)
            if not query.refused(sel) and sel.rows:
                sel.rows = list(sel.rows)[:-1]
            return sel

    lossy = api.Api(serve.View(_root), duck_store=Lossy(_root),
                    counters=_store.counters)
    st, ref = ui_call(lossy, V1 + "search", "account=%s&verify=1" % u)
    check("api/search: an engine that drops a row is refused, not reported",
          (st, ref["refusal"]["reason"]), (400, "engine-disagreement"))
    check_true("api/search: ...and the refusal says the store is unchanged",
               "record of truth" in ref["refusal"]["remedy"])
    check("api/search: ...carrying no result that would be the smaller answer",
          "result" in ref, False)
    _st, unchecked = ui_call(lossy, V1 + "search", "account=%s" % u)
    check("api/search: without verify= the same engine answers, which is why "
          "verify exists at all", unchecked["outcome"], "ok")


def test_api_values_names_a_never_observed_column_instead_of_omitting_it():
    """The third state, spelled out.

    `query.facets` OMITS a column no row carried, and that rule stays inside
    the function.  The ENDPOINT lists it with `values: []` and
    `never_observed: true`, because omitting it from the sidebar and reporting
    it in a separate `never_observed_here` array is precisely how a client
    re-creates the empty-control bug on its own side: an empty facet list reads
    as "I use no MCP tools" rather than "this has never been observed".
    """
    if not have_duck():
        _skip("api: values (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    _st, doc = ui_call(a, V1 + "values",
                       "account=%s&fields=query_source,mcp_tool,model" % u)
    byname = {f["name"]: f for f in doc["result"]["fields"]}
    check("api/values: query_source carries its real counts",
          sorted(byname["query_source"]["values"]),
          [["generate_session_title", 1], ["prompt_suggestion", 1],
           ["repl_main_thread", 1]])
    check("api/values: a never-observed column is LISTED, not omitted",
          "mcp_tool" in byname, True)
    check("api/values: with an empty value list and the third state set",
          (byname["mcp_tool"]["values"],
           byname["mcp_tool"]["never_observed"]), ([], True))
    check_true("api/values: and a note saying what the emptiness means",
               "never been observed" in byname["mcp_tool"]["note"])
    check("api/values: a column that HAS values is not never-observed",
          byname["model"]["never_observed"], False)
    check_true("api/values: the counts are over the filtered result and it "
               "says so", "FILTERED" in doc["result"]["note"].upper())
    check("api/values: and the spec is echoed back beside them",
          doc["result"]["applies_to"]["spec"]["account_uuid"], u)


def test_api_values_describes_a_user_tag_as_a_real_column():
    """`tags.<key>` is a column here, and it must arrive with its cardinality.

    The stats came from `field_availability` over the PINNED ledger list, which
    has no `tags.project` in it, so a requested tag field came back with
    `rows_with_a_value: null` and `never_observed: false` -- an unlabelled
    control, which is the empty-control bug wearing a different hat.  User tags
    are also the thing this project has lost in silence before: the collector's
    allow-list deleted every one of them.
    """
    if not have_duck():
        _skip("api: tag columns (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    # `agent` is the account whose captured rows carry a user tag -- alpha's
    # and beta's carry none.  Read off the fixtures:
    #   $ jq -r '.tags | keys[]' usage/tests/fixtures/real-api-request.jsonl
    #     -> "test", on the agent account's three rows only
    u = fx.uuid_of("agent")
    snap = serve.View(_root).load(fx.NOW_JOINT)
    keys = sorted({k for r in snap.rows(u)
                   for k in (r.get("tags") or {})})
    check("api/values: the capture carries exactly the tag key it was read for",
          keys, ["test"])
    field = query.TAG_PREFIX + keys[0]
    _st, doc = ui_call(a, V1 + "values", "account=%s&fields=%s" % (u, field))
    f = doc["result"]["fields"][0]
    check("api/values: the tag column is described by name", f["name"], field)
    check_true("api/values: with a real count of the rows that carried it",
               isinstance(f["rows_with_a_value"], int)
               and f["rows_with_a_value"] > 0)
    check_true("api/values: and its distinct values",
               isinstance(f["distinct_n"], int) and f["distinct_n"] > 0)
    check("api/values: so it is not reported as never observed",
          f["never_observed"], False)
    check_true("api/values: and its values come from the rows",
               bool(f["values"]))


def test_api_values_says_when_it_truncated_the_tail():
    """A top-N that looks complete is a coverage claim."""
    if not have_duck():
        _skip("api: truncation (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    _st, doc = ui_call(a, V1 + "values",
                       "account=%s&fields=query_source&limit=1" % u)
    f = doc["result"]["fields"][0]
    check("api/values: one of three values is shown", len(f["values"]), 1)
    check("api/values: and the cut is declared", f["truncated"], True)
    check("api/values: with the size of the tail it did not show",
          f["other_values_n"], 2)
    _st, whole = ui_call(a, V1 + "values",
                         "account=%s&fields=query_source&limit=20" % u)
    g = whole["result"]["fields"][0]
    check("api/values: an untruncated list says so", g["truncated"], False)
    check("api/values: with no hidden tail", g["other_values_n"], 0)


def test_api_fields_reports_absent_as_unknown_rather_than_zero():
    """A pinned read cannot separate an absent key from an explicit null.

    Both are SQL NULL.  Reporting `absent: 0` would be the confident claim that
    every null was written deliberately, so it is `null` and the meaning is
    published in `capabilities.null_means`.
    """
    if not have_duck():
        _skip("api: fields (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    _st, doc = ui_call(a, V1 + "fields", "account=" + fx.uuid_of("alpha"))
    fields = {f["name"]: f for f in doc["result"]["fields"]}
    check("api/fields: every pinned ledger column is described",
          sorted(fields), sorted(query.LEDGER_COLUMNS))
    for name, f in fields.items():
        check("api/fields: %s reports `absent` as unknown, never 0" % name,
              f["cardinality"]["absent"], None)
    check("api/fields: request_id is not groupable -- one row per value",
          fields["request_id"]["groupable"], False)
    check("api/fields: prompt_id is, because a turn is a real unit",
          fields["prompt_id"]["groupable"], True)
    check("api/fields: no measurement is offered as a bucket",
          [n for n, f in fields.items()
           if f["groupable"] and n in query.OBSERVED_NUMERIC], [])
    check("api/fields: mcp_tool has never carried a value here",
          fields["mcp_tool"]["never_observed_here"], True)
    _st, caps = ui_call(a, V1 + "capabilities")
    check_true("api/capabilities: and what that null means is published",
               "cannot separate" in
               caps["result"]["null_means"]["fields[].cardinality.absent"])


def test_api_the_null_meanings_are_data_and_cover_the_nulls_that_travel():
    """A generic renderer consults the map; adding a key is a one-line edit."""
    _root, _store, a = ui_store()
    _st, caps = ui_call(a, V1 + "capabilities")
    nm = caps["result"]["null_means"]
    check("api/capabilities: the map is served whole",
          sorted(nm), sorted(api.NULL_MEANS))
    for path in ("coverage.fraction", "coverage.placement.placed_fraction",
                 "accounts[].last_sample_age_s"):
        check_true("api/capabilities: %s has a stated meaning" % path,
                   isinstance(nm.get(path), str) and bool(nm[path]))
    check_true("api/capabilities: and the coverage one says unknown, not zero",
               "NOT zero" in nm["coverage.fraction"])


def test_api_histogram_emits_the_empty_buckets_and_never_re_buckets():
    """A missing bucket and a zero bucket are different claims.

    A chart draws them identically only if the server withholds one, so every
    bucket in range is emitted with `rows: 0`.  And a range too wide for the
    interval is REFUSED rather than silently re-bucketed -- re-bucketing
    changes the answer, and a chart drawn at an interval nobody asked for has
    peaks that are an artefact of the server's opinion.
    """
    if not have_duck():
        _skip("api: histogram (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    _st, all_rows = ui_call(a, V1 + "search", "account=" + u)
    ts = sorted(r["ts"] for r in all_rows["result"]["rows"])
    lo = int(ts[0] // 3600 * 3600)
    hi = lo + 3600 * 5
    _st, doc = ui_call(a, V1 + "histogram",
                       "account=%s&interval=3600&since=%d&until=%d"
                       % (u, lo, hi))
    b = doc["result"]["buckets"]
    check("api/histogram: every bucket in the range is emitted", len(b), 5)
    check("api/histogram: on epoch multiples of the interval",
          [x["bucket"] % 3600 for x in b], [0] * 5)
    check_true("api/histogram: including the empty ones, as an explicit zero",
               any(x["rows"] == 0 for x in b))
    check_true("api/histogram: and it says why it withheld none",
               "different claims" in doc["result"]["zero_fill_note"])
    check("api/histogram: the rows add up to what search matched",
          sum(x["rows"] for x in b), all_rows["result"]["page"]["matched"])

    st, ref = ui_call(a, V1 + "histogram",
                      "account=%s&interval=1&since=0&until=999999999" % u)
    check("api/histogram: too many buckets is a refusal, never a re-bucket",
          (st, ref["refusal"]["reason"]), (400, "too-many-buckets"))
    check_true("api/histogram: and the remedy names a wider interval",
               "interval" in ref["refusal"]["remedy"])

    # The cap is checked against the span that will actually be FILLED, so a
    # half-open range is capped too.  It used to be checked only when BOTH
    # bounds were given, which left `interval=1&since=0` unbounded: the fill
    # loop built a bucket per second from the epoch to the newest row and the
    # request hung instead of refusing.  A cap that can be walked around is a
    # hint, not a cap.
    st, half = ui_call(a, V1 + "histogram",
                       "account=%s&interval=1&since=0" % u)
    check("api/histogram: a range bounded only by the data is capped too",
          (st, half["refusal"]["reason"]), (400, "too-many-buckets"))
    check("api/capabilities: and the cap is published",
          ui_call(a, V1 + "capabilities")[1]["result"]["constraints"]
          ["max_buckets"], api.MAX_BUCKETS)


def test_api_the_histogram_asserts_no_timezone_and_uses_epoch_arithmetic():
    """`to_timestamp()` returns TIMESTAMPTZ and needs `pytz` to reach Python.

    That is a SECOND non-stdlib dependency acquired by writing an ordinary
    date histogram, and it would assert a time zone the data does not carry.
    Asserted statically as well as behaviourally, because the behavioural half
    passes right up until somebody writes the convenient version.
    """
    code = code_without_prose(os.path.join(fx.SRV_DIR, "srv", "store.py"))
    for banned in ("to_timestamp", "strftime", "date_trunc", "AT TIME ZONE"):
        check("api/histogram: the SQL never reaches for %s" % banned,
              banned in code, False)
    check_true("api/histogram: it buckets by integer division on the epoch",
               "floor(ts /" in code)


def test_api_lookup_says_what_not_finding_an_id_means():
    """A miss is not a request with no cost."""
    if not have_duck():
        _skip("api: lookup (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    _st, all_rows = ui_call(a, V1 + "search", "account=" + u)
    rid = all_rows["result"]["rows"][0]["request_id"]
    _st, hit = ui_call(a, V1 + "lookup",
                       "field=request_id&value=%s&account=%s" % (rid, u))
    check("api/lookup: a real id finds its row",
          (hit["outcome"], hit["result"]["n"]), ("ok", 1))
    _st, miss = ui_call(a, V1 + "lookup",
                        "field=request_id&value=deadbeefdeadbeef&account=" + u)
    check("api/lookup: a miss is filtered-to-nothing, not no-data",
          miss["outcome"], "filtered-to-nothing")
    check_true("api/lookup: and says what a miss does NOT mean",
               "not the same as a request with no cost"
               in miss["empty"]["message"])
    st, ref = ui_call(a, V1 + "lookup", "field=model&value=x")
    check("api/lookup: a non-id column is refused by name",
          (st, ref["refusal"]["reason"]), (400, "not-an-id-column"))


def test_api_synthetic_rows_are_counted_by_name():
    """A generated row once became the top consumer in a real account's report.

    So `source` is a permanent column and the count fires the moment one is in
    the result.
    """
    # Derived BEFORE the engine gate, so the provenance ledger records it on
    # every machine: a derivation that only exists when an optional dependency
    # is installed makes the audit at the end of this file machine-dependent.
    base = fx.stamped_rows()[0]
    synth = fx.derive("ui-synthetic-request", "the first real ledger row",
                      base, request_id="ui00000000000dead",
                      ts=base["ts"] + 1.0, source="synthetic")
    if not have_duck():
        _skip("api: synthetic rows (duckdb not installed)")
        return
    _root, _store, a = ui_store(
        extra=[("agent", wire.STREAM_LEDGER, [ui_line(synth)])])
    _st, doc = ui_call(a, V1 + "search", "account=" + fx.uuid_of("agent"))
    check("api/search: a synthetic row is counted, by name",
          doc["result"]["synthetic_rows"], 1)
    check_true("api/search: and the note says what one is",
               "not captured" in doc["result"]["synthetic_note"])


def test_api_stream_b_is_served_from_the_reconciler_with_its_host():
    """A stream-B record without its shipping host is unattributable.

    The v1 shape has no `host` column -- 65 real records on disk are in that
    shape -- and `wire.sample_key` hashes the record together with the
    MANIFEST's host.  So a `SELECT` over `samples.jsonl` in place reads neither
    the host nor the dedupe, and this endpoint answers from the core instead.
    """
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    _st, doc = ui_call(a, V1 + "search", "account=%s&stream=b" % u)
    check("api/search: stream B answers", doc["outcome"], "ok")
    check("api/search: from the reconciler, and it says so",
          doc["result"]["engine"]["name"], "reconciler")
    rows = doc["result"]["rows"]
    check_true("api/search: it returned samples", len(rows) > 0)
    # The REAL host off the batch manifest, not merely "a non-empty string":
    # `ship_b` ships with host "darwin", and a placeholder would satisfy a
    # truthiness check while making every one of these records unattributable.
    check("api/search: every stream-B row carries the host from its manifest",
          sorted({r.get("shipping_host") for r in rows}), ["darwin"])
    check("api/search: which is the host the batch was really shipped with",
          fx.manifest(wire.STREAM_SAMPLES, [b"x"], machine_id="m-3f9a1c02",
                      offset=0, to=2)["host"], "darwin")
    for r in rows:
        check_true("api/search: and the record as it was shipped",
                   isinstance(r.get("record"), dict))
    check_true("api/search: the row shape explains why the host is separate",
               "no host of its own"
               in doc["result"]["row_shape"]["shipping_host"])
    _st, caps = ui_call(a, V1 + "capabilities")
    check_true("api/capabilities: and the engine split is published",
               "dedupe" in caps["result"]["streams"]["b"]["why_not_sql"])


# ---- the dependency ---------------------------------------------------------

def test_api_a_missing_duckdb_refuses_by_name_and_never_answers_zero():
    """"0 requests" is a plausible answer for an account that has not shipped.

    So a storage layer that degrades into one is indistinguishable from a
    working one over a quiet store -- which is why `store.require()` raises and
    every stream-A route turns that into a 503 naming the install command,
    rather than an empty result.
    """
    root, store_, _a = ui_store()

    class Absent(duckstore.DuckStore):
        def con(self):
            raise duckstore.DuckDBMissing(duckstore.INSTALL_HINT)

    u = fx.uuid_of("alpha")
    # BOTH ways the engine can be absent, because they are two different code
    # paths: no handle was configured at all, and a handle whose connection
    # raises.  Testing only the second left `duck_store is None` asserted by
    # nothing.
    engines = [("a handle whose connection raises", Absent(root)),
               ("no handle at all", None)]
    for how, handle in engines:
        a = api.Api(serve.View(root), duck_store=handle,
                    counters=store_.counters)
        for path, qs in ((V1 + "search", "account=" + u),
                         (V1 + "aggregate", "account=%s&by=model" % u),
                         (V1 + "histogram", "account=%s&interval=3600" % u),
                         (V1 + "values", "fields=model"),
                         (V1 + "fields", ""),
                         (V1 + "lookup", "field=request_id&value=x")):
            st, doc = ui_call(a, path, qs)
            check("api/duckdb: %s refuses rather than answering nothing (%s)"
                  % (path, how),
                  (st, doc["outcome"], doc["refusal"]["reason"]),
                  (503, "unanswerable", "duckdb-missing"))
            check_true("api/duckdb: ...naming the install command (%s)" % how,
                       "pip install duckdb" in doc["refusal"]["remedy"])
            check("api/duckdb: ...and carrying no result to mistake for an "
                  "answer (%s)" % how, "result" in doc, False)

        # The reconciler half still works: stream B, the windows and the
        # accounts need no engine at all, so a machine without duckdb is not a
        # dead server.
        for path, qs in ((V1 + "accounts", ""),
                         (V1 + "windows", "account=" + u),
                         (V1 + "search", "account=%s&stream=b" % u)):
            _st, doc = ui_call(a, path, qs)
            check("api/duckdb: %s still answers without the engine (%s)"
                  % (path, how), doc["outcome"], "ok")

        # And the one route that spans both engines degrades PER PART: the
        # window's own figures come from the reconciler, and the request list
        # inside it is a refusal object with NO `rows` key -- an empty list
        # there reads as a window that had no requests in it.
        _st, win = ui_call(a, V1 + "window",
                           "account=%s&kind=5h&resets_at=1786598400" % u)
        check("api/duckdb: the window itself still answers (%s)" % how,
              win["outcome"], "ok")
        check("api/duckdb: with its own figures intact (%s)" % how,
              win["result"]["window"]["residual_pp"], 0.0)
        req = win["result"]["requests"]
        check("api/duckdb: but the request list is refused by name (%s)" % how,
              req["unavailable"]["reason"], "duckdb-missing")
        check("api/duckdb: and carries NO rows key to read as an empty window "
              "(%s)" % how, "rows" in req, False)
        check("api/duckdb: nor a count that would read as zero (%s)" % how,
              "n" in req, False)

        # AND THE COVERAGE BESIDE IT, which used to publish a full placement
        # CENSUS OF ZEROS about the very rows three keys above it says it
        # could not read.  `query.coverage_for` counts per row, so an empty
        # list gave `rows_total: 0`, `windows.touched: 0` -- about the window
        # sitting in `result.window` on the same payload -- and
        # `basis: "no-window-touched"`, which is the POSITIVE claim that none
        # of these rows fell in any window.  Measured byte-identical to the
        # answer for a window that genuinely holds no requests, so two states
        # produced one object and it asserted the false one.
        #
        # Asserted ABSENT, never zero: asserting zero would pin the bug.
        # `.get`, not `[...]`: a mutation that restores the census makes
        # `unavailable` absent, and a KeyError is counted as a failure exactly
        # as an assertion is -- so the row would read `caught` with nobody
        # able to see WHICH property was violated.  A named failure is the
        # difference between a matrix that reports and one that is believed.
        cov = win["result"]["coverage"]
        check("api/duckdb: the coverage over those rows is refused by name "
              "(%s)" % how,
              (cov.get("unavailable") or {}).get("reason"), "duckdb-missing")
        for k in ("placement", "windows", "fraction", "known", "basis"):
            check("api/duckdb: ...and carries no %s to read as a measurement "
                  "(%s)" % (k, how), k in cov, False)

        # The contrast that made it a contradiction rather than merely a wrong
        # number: `/windows` builds its coverage from the RECONCILER's rows,
        # which need no engine, so at the same moment over the same account it
        # reported the true placement.  Two routes over one store, both `ok`.
        _st, plural = ui_call(a, V1 + "windows", "account=" + u)
        pl = plural["result"]["coverage"]["placement"]
        check_true("api/duckdb: while /windows still reports real placement "
                   "for the same account (%s)" % how,
                   isinstance(pl["rows_total"], int) and pl["rows_total"] > 0)


def test_api_capabilities_states_the_engine_and_the_constraints():
    """omini hardcodes nothing: everything it would copy is served."""
    _root, _store, a = ui_store()
    _st, doc = ui_call(a, V1 + "capabilities")
    r = doc["result"]
    check("api/capabilities: it names its own version",
          (r["server"]["api_version"], doc["api_version"]),
          (api.API_VERSION, api.API_VERSION))
    check("api/capabilities: it lists every route it serves",
          sorted(e["path"] for e in r["endpoints"]), sorted(a.routes()))
    check("api/capabilities: the engine is named",
          r["engine"]["name"], "duckdb")
    check("api/capabilities: and its availability is stated as a fact",
          r["engine"]["available"], duckstore.available())
    if duckstore.available():
        check("api/capabilities: with no install hint when it is present",
              r["engine"]["install_hint"], None)
        check_true("api/capabilities: and a real version string",
                   isinstance(r["engine"]["version"], str))
    else:
        check_true("api/capabilities: and the install command when it is not",
                   "pip install duckdb" in (r["engine"]["install_hint"] or ""))
    check("api/capabilities: the cross-account rule is published as a "
          "constraint",
          r["constraints"]["no_cross_account_total"], True)
    check("api/capabilities: pagination is declared keyset",
          r["constraints"]["pagination"], "keyset")
    check("api/capabilities: the auth posture is stated rather than implied",
          (r["auth"]["read"], r["auth"]["write"], r["auth"]["transport"]),
          ("none", "bearer-routing-label", "plaintext"))
    check("api/capabilities: every declared outcome is one this API can emit",
          sorted(r["outcomes"]), sorted(api.OUTCOMES))


def test_api_every_payload_survives_the_handler_s_own_serialiser():
    """The door serialises with `sort_keys=True`, and that is a real constraint.

    A dict keyed by `str` and `None` together CANNOT be ordered, so
    `json.dumps` raises and the response is a 500 with the store perfectly
    healthy.  `by_tag` is exactly such a dict -- a request that carried no tag
    lands in the `None` bucket, deliberately -- and the fault was latent on
    every payload carrying one, invisible to every test, because the tests
    called the API in-process and never serialised what came back.  So this
    asserts the payload through the SAME call the handler makes.
    """
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    # `agent`, NOT `alpha`, AND THAT IS THE WHOLE OF THE TEST.
    #
    # This assertion was written for exactly this bug, named it in its
    # docstring, called the right route -- and passed over broken code for as
    # long as it existed, because all three of `alpha`'s captured rows carry
    # `tags: {}`.  That makes `by_tag` `{None: ...}`: one key, homogeneous,
    # sorts perfectly.  The one row in the whole corpus that carries a tag
    # belongs to `agent`, so a `str` key and a `None` key never met in one
    # window anywhere in the suite and the fixture was the bug's alibi.
    # Nothing is fabricated to fix that -- the mixed case is real captured
    # data and was simply never asked for.  `mixed_probe` below proves the
    # input can still exhibit the defect, so this cannot go quietly vacuous
    # again.
    mixed = fx.uuid_of("agent")
    shapes = [(V1 + "capabilities", ""), (V1 + "accounts", ""),
              (V1 + "diagnostics", ""), (V1 + "health", ""),
              (V1 + "windows", "account=" + u),
              (V1 + "window", "account=%s&kind=5h&resets_at=1786598400" % u),
              (V1 + "window",
               "account=%s&kind=5h&resets_at=1786598400" % mixed),
              (V1 + "windows", "account=" + mixed),
              (V1 + "search", "account=%s&stream=b" % u),
              (V1 + "windows", "account=nobody")]
    if have_duck():
        shapes += [(V1 + "search", "account=" + u),
                   (V1 + "search", "account=%s&since=1&until=2" % u),
                   (V1 + "aggregate", "account=%s&by=model" % u),
                   (V1 + "aggregate/per-account", "by=model"),
                   (V1 + "histogram", "account=%s&interval=3600" % u),
                   (V1 + "values", "fields=model,mcp_tool"),
                   (V1 + "fields", ""),
                   (V1 + "lookup", "field=request_id&value=x&account=" + u)]
    for path, qs in shapes:
        _st, doc = ui_call(a, path, qs)
        try:
            json.dumps(doc, sort_keys=True, default=str)
            ok = True
        except TypeError as exc:
            ok = "raised %s" % exc
        check("api/wire: %s?%s serialises exactly as the handler serialises it"
              % (path, qs), ok, True)

    # THE PROBE, so the list above cannot go vacuous the way it did.  The
    # `agent` window must really carry a `None` bucket AND a `str` bucket, or
    # the shape this whole test exists for is not among the payloads it
    # checked.  Asserted on the WIRE shape, since that is what is served.
    _st, mix = ui_call(a, V1 + "window",
                       "account=%s&kind=5h&resets_at=1786598400" % mixed)
    buckets = mix["result"]["window"]["by_tag"]
    check_true("api/wire: the probe window really carries a null tag bucket "
               "and a named one, or this test proves nothing",
               isinstance(buckets, list)
               and any(b["value_is_null"] for b in buckets)
               and any(not b["value_is_null"] for b in buckets))
    # Shape-guarded, so a regression FAILS BY NAME rather than raising.  The
    # mutation that restores the bug makes `by_tag` a dict again, and
    # iterating a dict yields its keys -- one of which is `None` -- so the
    # unguarded version died with "'NoneType' object is not subscriptable".
    # An exception counts as a failure exactly as an assertion does, which is
    # how a matrix row reads `caught` without anyone learning what broke.
    flags = (sorted((b["value"] is None, b["value_is_null"]) for b in buckets)
             if isinstance(buckets, list)
                and all(isinstance(b, dict) for b in buckets)
             else "by_tag was not a list of buckets: %r" % (buckets,))
    check("api/wire: the null bucket is flagged rather than stringified into "
          "a label indistinguishable from a tag called \"None\"",
          flags, sorted([(False, False), (True, True)]))

    # AND THE PROPERTY, rather than a sample of it: no dict anywhere in any
    # payload may be keyed by `None`, at any depth.  `by_tag` was reached
    # through a loop that treated it as `by`'s NESTED shape when it is flat,
    # so the values were mangled AND the outer dict kept its mixed keys; a
    # test that only asserted the buckets would have passed on a payload that
    # still could not be serialised.
    def none_keyed(obj, path="$"):
        out = []
        if isinstance(obj, dict):
            if any(k is None for k in obj):
                out.append(path)
            for k, v in obj.items():
                out.extend(none_keyed(v, "%s.%s" % (path, k)))
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                out.extend(none_keyed(v, "%s[%d]" % (path, i)))
        return out

    for path, qs in shapes:
        _st, doc = ui_call(a, path, qs)
        check("api/wire: %s?%s carries no None-keyed dict at any depth"
              % (path, qs), none_keyed(doc), [])


def test_api_a_null_bucket_keeps_its_own_flag_rather_than_a_label():
    """`None` and the string "None" are two different buckets.

    Stringifying the null key would make a request that carried no tag
    indistinguishable from one tagged `None`, which is the same class of
    failure as rendering an unknown coverage as zero.
    """
    got = api.bucketise({"a": 1, None: 2})
    check("api/wire: a by-map reaches the wire as a list, not a mixed-key dict",
          [(b["value"], b["value_is_null"], b["stats"]) for b in got],
          [("a", False, 1), (None, True, 2)])
    check("api/wire: and the null bucket sorts last, deterministically",
          got[-1]["value_is_null"], True)
    _root, _store, a = ui_store()
    _st, doc = ui_call(a, V1 + "window",
                       "account=%s&kind=5h&resets_at=1786598400"
                       % fx.uuid_of("alpha"))
    by = doc["result"]["window"].get("by") or {}
    for dim, buckets in by.items():
        check_true("api/wire: window.by[%s] is a list of buckets" % dim,
                   isinstance(buckets, list))
        for b in buckets:
            check_true("api/wire: ...each flagging its own nullness (%s)" % dim,
                       isinstance(b, dict) and "value_is_null" in b)


def test_api_the_door_and_the_contract_agree_on_the_base_path():
    """Two constants, one string, compared rather than trusted.

    `serve.PATH_API_V1` is what the door's banner and its 404 detail print;
    `api.PATH_V1` is what the router matches.  They are spelled in two files
    because one is the socket's business and the other is the contract's, and a
    comment saying "these are the same" is a claim about the code rather than a
    property of it -- which is the shape of defect this suite exists to catch.
    """
    check("api: the door's advertised base is the contract's base",
          serve.PATH_API_V1, api.PATH_V1)
    check("api: and the legacy prefix is the versioned one's parent",
          api.PATH_V1.startswith(serve.PATH_API), True)
    _root, _store, a = ui_store()
    for route in a.routes():
        check_true("api: %s really sits under the advertised base" % route,
                   route.startswith(serve.PATH_API_V1))


def test_the_readme_documents_the_one_file_an_operator_has_to_write():
    """`tokens.json` was undocumented, and the door refuses everything without it.

    Following `server/README.md` literally started a door that answered `401`
    to every batch, printed a warning naming a file the README never mentions,
    and gave the reader nothing to create.  That is the whole shipping path --
    `logging=remote` -> `ship_url` -> this door -- failing at the one step an
    operator cannot guess, with the two ends of the wire (the account UUID, and
    which conf layer the client reads the destination from) unstated as well.

    Derived from `serve.py`, not written out here: the filename and the option
    are read off the argument parser, so renaming either fails this test rather
    than leaving the README describing a file that no longer exists.
    """
    srv_py = os.path.join(fx.SRV_DIR, "srv", "serve.py")
    if not os.path.exists(srv_py):
        # A NAMED failure, never a traceback: the mutation harness counts an
        # exception as a catch, so a test that cannot find its input reports
        # every row as caught. That defect is recorded one test over.
        check("tokens/docs: serve.py was found", srv_py, "a file that exists")
        return
    srv = open(srv_py, encoding="utf-8").read()
    m = re.search(r'os\.path\.join\(args\.root,\s*"([^"]+)"\)', srv)
    check_true("tokens/docs: the default token filename was extracted from serve.py",
               bool(m))
    fname = m.group(1) if m else None
    opt = re.search(r'p\.add_argument\("(--tokens)"', srv)
    check_true("tokens/docs: and so was the option that overrides it", bool(opt))

    readme = os.path.join(os.path.dirname(fx.SRV_DIR), "server", "README.md")
    if not os.path.exists(readme):
        readme = os.path.join(fx.SRV_DIR, "README.md")
    if not os.path.exists(readme):
        check("tokens/docs: the README was found", readme, "a file that exists")
        return
    text = open(readme, encoding="utf-8").read()
    check_true("tokens/docs: the README names the file the door needs",
               bool(fname) and fname in text)
    check_true("tokens/docs: ...and the option that moves it",
               bool(opt) and opt.group(1) in text)
    # The shape, so a reader can write the file rather than infer it, and the
    # two ends of the wire: where the UUID comes from and which conf layer the
    # client reads the destination from. Both were absent and both are the
    # kind of thing somebody gets wrong once, silently, per deployment.
    check_true("tokens/docs: ...and the shape of it",
               "account uuid" in text.lower() or "account_uuid" in text.lower())
    check_true("tokens/docs: ...and where the account UUID comes from",
               "accountUuid" in text)
    check_true("tokens/docs: ...and the client keys that point at this door",
               "ship_url" in text and "ship_token" in text)


def test_api_the_readme_names_exactly_the_routes_that_exist():
    """The endpoint table is derived from the source, in both directions.

    This project pins its docs against its code everywhere else, and the reason
    is the same here: a route reachable and undocumented is one nobody uses, a
    route documented and 404 is one somebody writes a client against.  The
    table in `server/README.md` is the contract a front-end author reads before
    they read any Python.
    """
    readme = os.path.join(os.path.dirname(fx.SRV_DIR), "server", "README.md")
    if not os.path.exists(readme):
        readme = os.path.join(fx.SRV_DIR, "README.md")
    if not os.path.exists(readme):
        # A NAMED failure, not a traceback.  An exception is counted as a
        # failure by the mutation harness exactly as an assertion is, so a
        # test that cannot find its input manufactures a "caught" verdict for
        # every mutation in the matrix -- which is what this one did.
        check("api/docs: the README the route table lives in was found",
              readme, "a file that exists")
        return
    text = open(readme, encoding="utf-8").read()
    documented = set(re.findall(r"`GET (/api/v1/[a-z/-]+)[?`]", text))
    _root, _store, a = ui_store()
    served = set(a.routes())
    check("api/docs: every route the API serves is in the README table",
          sorted(served - documented), [])
    check("api/docs: and every route the README promises really exists",
          sorted(documented - served), [])
    check_true("api/docs: the unversioned base is documented as gone",
               "/api/v1/; see" in text or "are **gone**" in text)
    check_true("api/docs: and /healthz is documented as unenveloped",
               "unenveloped" in text)
    # THE CLAIM THAT WAS FALSE.  Both the route table and `_v1_health`'s own
    # note said `/api/v1/health` asks "the same question as /healthz" -- and
    # the two differed on precisely the property that decides whether half the
    # API works, because only one of them named the engine.  They are the same
    # question in MORE DETAIL now, and `/healthz` is the surface that can be
    # asked without a token.  Flattened first, for the reason the `usage/`
    # refuted-claim scan gives: a line break inside the sentence makes a
    # literal `in` blind to it.
    flat = " ".join(text.split())
    check("api/docs: the README no longer calls /api/v1/health the same "
          "question as /healthz", "same question as `/healthz`" in flat, False)
    check_true("api/docs: it says a 200 from /healthz is not a statement that "
               "stream A can be queried",
               "not a statement that" in flat and "engine.available" in flat)


# ---- over a real socket -----------------------------------------------------

def test_api_the_store_is_served_over_http_beside_the_door():
    """One process answers `POST /v1/ship` and `GET /api/v1/*`.

    So this asserts the handler serves JSON, that a refusal keeps its status
    and its reason over the wire, that `/healthz` keeps its own unenveloped
    shape for the operators and scripts that read it, and that the door still
    works with the reader bolted on.
    """
    root, store_, a = ui_store()
    counters = serve.Counters()
    tenants = serve.Tenants(mapping={"tok-alpha": fx.uuid_of("alpha")})
    serve.ShipHandler.door = serve.Door(store_, tenants, counters=counters)
    serve.ShipHandler.api = a
    serve.ShipHandler.quiet = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.ShipHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def get(path):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
        try:
            with urllib.request.urlopen(req, timeout=10) as fh:
                return fh.status, dict(fh.headers), fh.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read()

    try:
        status, _h, _b = get("/")
        check("api/http: there is no bundled page any more -- omini is the "
              "front end and this server serves data", status, 404)

        status, headers, body = get(V1 + "accounts")
        doc = json.loads(body)
        check("api/http: the endpoints are JSON",
              (status, headers["Content-Type"]), (200, "application/json"))
        check("api/http: over the real store",
              len(doc["result"]["accounts"]), 3)
        check("api/http: enveloped, with the outcome on it",
              doc["outcome"], "ok")

        status, _h, body = get("/api/overview")
        check("api/http: the unversioned routes are gone, with a way forward",
              (status, json.loads(body)["refusal"]["reason"]),
              (404, "unknown-endpoint"))

        status, _h, body = get(V1 + "windows?account=nobody")
        check("api/http: a refusal keeps its status and its reason",
              (status, json.loads(body)["refusal"]["reason"]),
              (404, "unknown-account"))

        status, _h, body = get(V1 + "nowhere")
        check("api/http: an unknown endpoint is 404 and lists the real ones",
              (status, json.loads(body)["refusal"]["reason"]),
              (404, "unknown-endpoint"))

        status, _h, body = get("/healthz")
        health = json.loads(body)
        check("api/http: the door's own report is untouched, unversioned and "
              "unenveloped", (status, health["door"]), (200, serve.DOOR_VERSION))
        check("api/http: ...and carries no API envelope for a script to trip "
              "over", "outcome" in health, False)
        # THE ENGINE, ON THE ONE SURFACE SERVED BEFORE READER AUTH.
        #
        # `/api/v1/capabilities` and `/api/v1/health` both name it and both
        # need a token -- and `readers.json` is not an exotic configuration,
        # it is the only one `serve()` permits on a non-loopback bind, i.e.
        # every container.  Measured before this existed: the key sets of an
        # engine-present and an engine-absent door were IDENTICAL, so the
        # health check this repository's own README recommends
        # (`urlopen('/healthz').status == 200`) reported healthy for a door
        # whose entire stream-A half refuses every question.  That matters
        # most for the FreeBSD image, whose engine has never been executed
        # outside one amd64 smoke job that is deliberately not allowed to
        # block a publish.
        # `.get`, so its absence is a NAMED failure rather than a KeyError:
        # an exception is counted exactly as an assertion is, and a row
        # reading `caught` with no reason attached is a matrix that is
        # believed rather than read.
        eng = health.get("engine") or {}
        check("api/http: /healthz names the engine, because nothing else "
              "answers without a token",
              (eng.get("name"), eng.get("available")),
              ("duckdb", duckstore.available()))
    finally:
        serve.ShipHandler.api = None
        httpd.shutdown()
        httpd.server_close()


def test_api_the_door_still_takes_a_shipment_with_the_reader_attached():
    """The door is unchanged: the read API is bolted on, never in the way."""
    root = door_root()
    store_ = serve.Store(root)
    counters = serve.Counters()
    tenants = serve.Tenants(mapping={"tok-alpha": fx.uuid_of("alpha")})
    door = serve.Door(store_, tenants, counters=counters)
    serve.ShipHandler.door = door
    serve.ShipHandler.api = api.Api(serve.View(root),
                                    duck_store=duckstore.DuckStore(root),
                                    counters=counters)
    serve.ShipHandler.quiet = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.ShipHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        _man, lines, body = ship_b("alpha")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/ship" % port, data=body,
            headers={"Authorization": "Bearer tok-alpha",
                     "Content-Type": serve.CONTENT_TYPE})
        with urllib.request.urlopen(req, timeout=10) as fh:
            ack = json.loads(fh.read())
        check("api/http: the door accepted the batch",
              (ack["ok"], ack["accepted"]), (True, len(lines)))
        check("api/http: and durably holds the offset it acked",
              ack["offset"] > 0, True)
        # And the reader sees what the door just took, in the same process.
        req = urllib.request.Request(
            "http://127.0.0.1:%d%saccounts" % (port, V1))
        with urllib.request.urlopen(req, timeout=10) as fh:
            doc = json.loads(fh.read())
        check("api/http: the reader sees the batch the door just took",
              [x["account_uuid"] for x in doc["result"]["accounts"]],
              [fx.uuid_of("alpha")])
    finally:
        serve.ShipHandler.api = None
        httpd.shutdown()
        httpd.server_close()


# ---- store problems: per derivation, not per process ------------------------

def test_api_store_problems_describe_the_store_not_the_query_history():
    """A running log of every torn line seen since boot is not a description.

    `DuckStore.problems` was a plain list on an object that lives for the
    process, appended to and never cleared.  Measured against a live door over
    a store holding exactly THREE unparseable lines: after 1500 `/search`
    requests the envelope reported 3116 entries claiming 3118 unparseable
    lines, the payload had grown from 51 kB to 1.34 MB on every response
    including `/health`, RSS had gone from 11 MB to 143 MB, and a query scoped
    to a HEALTHY account reported another account's tear.  Two entries went on
    per `/search`, because `_v1_search` calls `select` and then `page` and each
    notices the same file.

    Three separate claims are pinned here, and each was false in its own way:
    the count describes the FILE, it stops describing it once the file is
    repaired, and it does not leak into an account it is not about.
    """
    if not have_duck():
        _skip("api: store problems (duckdb not installed)")
        return
    root, _store, a = ui_store()
    u, other = fx.uuid_of("alpha"), fx.uuid_of("beta")
    led = os.path.join(root, "accounts", u, "ledger.jsonl")
    good = open(led, "rb").read()
    with open(led, "ab") as fh:
        fh.write(b'{"request_id":"deadbeef","ts":178658')

    def probs(path, qs=""):
        _st, doc = ui_call(a, path, qs)
        return [p for p in doc["meta"]["store_problems"]
                if p.get("reason") == "unparseable-lines"]

    for i in range(1, 6):
        got = probs(V1 + "search", "account=" + u)
        check("api/problems: call %d still reports ONE torn file" % i,
              len(got), 1)
        check("api/problems: ...with the line count, not the call count (%d)"
              % i, got[0]["count"], 1)
    check("api/problems: and it names the file and whose it is",
          (probs(V1 + "search", "account=" + u)[0]["file"],
           probs(V1 + "search", "account=" + u)[0]["account_uuid"]),
          (led, u))

    # The reader-only half.  `/accounts` runs no stream-A query at all, and
    # the torn line was invisible there until some other caller happened to
    # run one -- the anti-silence surface silent in exactly the case it exists
    # for.
    check("api/problems: a reconciler-only route names it too",
          len(probs(V1 + "accounts")), 1)
    check("api/problems: and /windows does",
          len(probs(V1 + "windows", "account=" + u)), 1)

    # Not another account's.  This is the leak, and it is asserted on the
    # engine's own list rather than on the envelope: the reconciler re-derives
    # the WHOLE store on every request, so its entry for alpha's file is in
    # every payload by design and is honest.  What must not happen is the
    # storage layer -- which opened one file, beta's -- contributing a problem
    # about a file it never touched.  It did, for the whole life of the
    # process, and a query for an ABSENT account, which opens no file at all,
    # once reported 16 problems belonging to three other accounts.
    ui_call(a, V1 + "search", "account=" + other)
    check("api/problems: a query scoped to a healthy account leaves the "
          "engine reporting nothing", list(a.duck_store.problems), [])
    ui_call(a, V1 + "search", "account=deadbeef-0000-4000-8000-000000000000")
    check("api/problems: and a query for an account with no file at all "
          "reports nothing either", list(a.duck_store.problems), [])
    for i in range(1, 6):
        ui_call(a, V1 + "search", "account=" + u)
        check("api/problems: the engine's own list does not grow with the "
              "query count (call %d)" % i, len(a.duck_store.problems), 1)

    # Repaired.  It kept reporting the fault until the process restarted,
    # which directly contradicts "nothing is cached; every request re-derives
    # from byte zero".
    with open(led, "wb") as fh:
        fh.write(good)
    check("api/problems: a repaired file stops being reported, without a "
          "restart", probs(V1 + "search", "account=" + u), [])
    check("api/problems: on the reader-only route as well",
          probs(V1 + "accounts"), [])


def test_api_every_store_problem_is_one_shape():
    """One array, one shape, or a client renders `undefined` for half of it.

    `snap.problems` writes `reason` and the DuckDB side wrote `problem`, so
    `meta.store_problems` was a heterogeneous array and the one test that
    walked every payload for store problems filtered on `if "reason" in p` --
    which made every DuckDB-produced problem invisible to it.
    """
    if not have_duck():
        _skip("api: problem shape (duckdb not installed)")
        return
    root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    with open(os.path.join(root, "accounts", u, "ledger.jsonl"), "ab") as fh:
        fh.write(b'{"request_id":"deadbe')
    path = os.path.join(root, "accounts", u, "samples.jsonl")
    with open(path, "r+b") as fh:
        fh.truncate(os.path.getsize(path) - 30)
    _st, doc = ui_call(a, V1 + "search", "account=" + u)
    probs = doc["meta"]["store_problems"]
    check_true("api/problems: both readers really reported", len(probs) >= 2)
    check("api/problems: every entry carries `reason`",
          [p for p in probs if "reason" not in p], [])
    check("api/problems: and the two readers are both represented",
          sorted({p["reason"] for p in probs}),
          ["interrupted-batch", "unparseable-lines"])


# ---- an account that is not there -------------------------------------------

def test_api_an_unknown_account_is_refused_rather_than_answered_no_data():
    """Two different states must not produce one message.

    `/search?account=<never-seen>` answered 200 `no-data` with "no ledger file
    exists for ...: the shipper may not have reached stream A on any machine
    yet" -- byte-identical, bar the uuid, to the answer for an account that
    genuinely exists and has shipped stream B and not stream A.  Built both and
    compared.  The message positively asserts the state that is false: it tells
    the reader an identity exists and its shipper is behind, when no such
    account has ever been seen.  `unknown-account` was already published and
    already reachable on `/windows`; it was unreachable on this half.
    """
    if not have_duck():
        _skip("api: unknown account (duckdb not installed)")
        return
    root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    os.remove(os.path.join(root, "accounts", u, "ledger.jsonl"))

    for path, qs in ((V1 + "search", "account=" + u),
                     (V1 + "aggregate", "account=%s&by=model" % u),
                     (V1 + "values", "account=%s&fields=model" % u)):
        st, doc = ui_call(a, path, qs)
        check("api/account: %s over a REAL account with no ledger is no-data"
              % path, (st, doc["outcome"]), (200, "no-data"))
        check_true("api/account: ...and says the shipper may be behind",
                   "shipper" in doc["empty"]["message"])

    ghost = "deadbeef-0000-4000-8000-000000000000"
    for path, qs in ((V1 + "search", "account=" + ghost),
                     (V1 + "aggregate", "account=%s&by=model" % ghost),
                     (V1 + "values", "account=%s&fields=model" % ghost),
                     (V1 + "histogram", "account=%s&interval=3600" % ghost),
                     (V1 + "lookup", "account=%s&value=x" % ghost),
                     (V1 + "fields", "account=" + ghost),
                     (V1 + "diagnostics", "account=" + ghost)):
        st, doc = ui_call(a, path, qs)
        check("api/account: %s over an account nobody has ever seen is a "
              "named 404" % path,
              (st, doc.get("refusal", {}).get("reason")),
              (404, "unknown-account"))
        check_true("api/account: ...and lists the accounts there are",
                   fx.uuid_of("beta") in (doc["refusal"]["remedy"] or ""))
    check("api/account: the reconciler half answers the same question the "
          "same way", ui_call(a, V1 + "windows", "account=" + ghost)[1]
          ["refusal"]["reason"], "unknown-account")

    # The account is a CLAUSE, not only a file list.  The per-account layout
    # is the narrowing today, so dropping the clause changes no answer and no
    # behavioural test can see it -- which is exactly the shape of a guard
    # that rots. It is what would still be there if the path check were ever
    # relaxed, so it is pinned where it lives.
    st = duckstore.DuckStore(root)
    other = fx.uuid_of("beta")
    sel = st.lookup("request_id", "b9b0f28c079e314a", account_uuid=other)
    check("store/lookup: the compiled query carries the account clause",
          [c for c in sel.clauses if c.startswith("account_uuid=")],
          ["account_uuid=" + other])
    plain = st.lookup("request_id", "b9b0f28c079e314a")
    check("store/lookup: and carries none when no account was named",
          [c for c in plain.clauses if c.startswith("account_uuid=")], [])


def test_api_account_is_never_joined_into_a_path():
    """`?account=` reached `os.path.join` verbatim, and `join` discards a root.

    `os.path.join(accounts_dir, "/private/tmp/x", "ledger.jsonl")` is
    `/private/tmp/x/ledger.jsonl`.  Proven against a live door on 127.0.0.1:
    `GET /api/v1/lookup?account=<abs path>&field=request_id&value=...` returned
    the full row -- model, email address, cost -- and
    `?account=<abs path>` on `/diagnostics` returned every JSON key name in
    that file, including one literally called `secret_key`, plus its row count
    and byte size.  The read API is unauthenticated by design; its stated
    contract is "every record in the store", not every `ledger.jsonl` this
    process can read.
    """
    if not have_duck():
        _skip("api: path containment (duckdb not installed)")
        return
    root, _store, a = ui_store()
    outside = tempfile.mkdtemp(prefix="srv-outside-")
    DOOR_TMP.append(outside)
    row = {"request_id": "priv000000000001", "ts": 1786584663.0,
           "model": "secret-model", "email": "victim@example.com",
           "cost_usd_reported": 4.25, "account_uuid": "someone-else",
           "secret_key": "hunter2"}
    with open(os.path.join(outside, "ledger.jsonl"), "w") as fh:
        fh.write(json.dumps(row) + "\n")

    hostile = [outside, outside + "/", "../../../../" + outside.strip("/"),
               "..", "../..", fx.uuid_of("alpha") + "/../" + fx.uuid_of("beta")]
    for acct in hostile:
        st, doc = ui_call(a, V1 + "lookup",
                          "account=%s&field=request_id&value=priv000000000001"
                          % urllib.parse.quote(acct, safe=""))
        check("api/path: lookup with account=%r is refused, not served"
              % acct[:24],
              (st, doc.get("refusal", {}).get("reason")),
              (404, "unknown-account"))
        check_true("api/path: ...and no row from outside the store appears",
                   "hunter2" not in json.dumps(doc, default=str))
        st, dia = ui_call(a, V1 + "diagnostics",
                          "account=" + urllib.parse.quote(acct, safe=""))
        check_true("api/path: diagnostics leaks no key name from outside it "
                   "either (%r)" % acct[:24],
                   "secret_key" not in json.dumps(dia, default=str))
        st, sea = ui_call(a, V1 + "search",
                          "account=" + urllib.parse.quote(acct, safe=""))
        check("api/path: and search does not scan it",
              (st, sea.get("refusal", {}).get("reason")),
              (404, "unknown-account"))

    # The layer below, directly: a path check that only the API applies is one
    # refactor from being gone.
    st = duckstore.DuckStore(root)
    check("store/path: Paths.stream refuses an absolute account name",
          st.paths.stream(outside, "ledger.jsonl"), None)
    check("store/path: ...and a relative escape",
          st.paths.stream("../..", "ledger.jsonl"), None)
    check("store/path: ...and ledgers() therefore opens nothing",
          st.paths.ledgers(outside), [])
    check_true("store/path: while a real account still resolves",
               st.paths.stream(fx.uuid_of("alpha"), "ledger.jsonl") is not None)

    # TWO guards, and each is asserted by the case the OTHER one cannot see.
    # Without this pair the mutation matrix reports both as caught while
    # either alone would do -- which is a claim about redundancy, not about
    # either guard.
    #
    # Membership only: these resolve INSIDE the store, so containment is
    # perfectly happy with them. `.` would read a `ledger.jsonl` sitting
    # directly under `accounts/`, and `<alpha>/../<beta>` reads beta's file
    # while every clause and every count says alpha.
    for name in (".", "", fx.uuid_of("alpha") + "/../" + fx.uuid_of("beta")):
        check("store/path: an account name that is a PATH is refused, "
              "however far inside the store it lands (%r)" % name,
              st.paths.stream(name, "ledger.jsonl"), None)

    # Containment only: a symlinked account directory IS in `accounts()` --
    # `os.path.isdir` follows it -- so membership accepts it and only the
    # realpath check refuses.
    link = os.path.join(root, "accounts", "symlinked-account")
    os.symlink(outside, link)
    check_true("store/path: the symlinked directory really is listed as an "
               "account", "symlinked-account" in st.paths.accounts())
    check("store/path: ...and is still refused, because it resolves outside "
          "the store", st.paths.stream("symlinked-account", "ledger.jsonl"),
          None)
    st2 = duckstore.DuckStore(root)
    _r, _s, a2 = ui_store()
    a2.duck_store = st2
    a2.view = serve.View(root)
    _c, doc = ui_call(a2, V1 + "search", "account=symlinked-account")
    check_true("api/path: and a query naming it leaks no row from outside "
               "the store", "hunter2" not in json.dumps(doc, default=str))
    os.remove(link)


# ---- the no-data sentence ---------------------------------------------------

def test_api_a_no_data_answer_is_not_explained_by_an_unrelated_caveat():
    """The engine's own sentence, or the generic one -- never `notes[0]`.

    `notes[0]` is the engine's no-data sentence only when the ledger file is
    ABSENT.  Whenever the file exists and yields zero usable rows, `page()`
    unconditionally appends the keyset-pagination note and `breakdown` appends
    the list-rates note, so the first note is boilerplate -- and `/search` told
    a user whose result was empty about cursors and late arrivals while
    `/aggregate` told them about API list rates.  The fallback whose own
    comment explains why it is worded to be true of both cases was UNREACHABLE
    on both routes.
    """
    if not have_duck():
        _skip("api: no-data sentence (duckdb not installed)")
        return
    root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    led = os.path.join(root, "accounts", u, "ledger.jsonl")

    for label, make in (("0-byte", lambda: open(led, "w").close()),
                        ("torn-only",
                         lambda: open(led, "w").write('{"request_id'))):
        make()
        for path, qs, wrong in (
                (V1 + "search", "account=" + u, "pages are keyed on"),
                (V1 + "aggregate", "account=%s&by=model" % u, "list rates"),
                (V1 + "histogram", "account=%s&interval=3600" % u, None),
                (V1 + "values", "account=%s&fields=model" % u, None)):
            _st, doc = ui_call(a, path, qs)
            m = doc["empty"]["message"]
            check("api/no-data: %s over a %s ledger is no-data"
                  % (path, label), doc["outcome"], "no-data")
            check_true("api/no-data: ...explained by the no-data sentence "
                       "(%s, %s)" % (path, label),
                       "no request row has reached this store" in m
                       and "not the same as" in m)
            if wrong:
                check("api/no-data: ...and NOT by the %r caveat (%s)"
                      % (wrong, path), wrong in m, False)

    # The other half of the same conjunction: where the file really is missing,
    # the engine's own sentence still wins, because it is the only one that can
    # tell the two apart.
    os.remove(led)
    _st, gone = ui_call(a, V1 + "search", "account=" + u)
    check_true("api/no-data: a MISSING ledger keeps the engine's sentence",
               "no ledger file exists" in gone["empty"]["message"])


# ---- coercion ----------------------------------------------------------------

def test_api_a_value_the_pinned_type_could_not_hold_is_counted_not_assumed():
    """A well-formed line with a wrong-typed value was silent in both
    directions.

    `ignore_errors=true` nulls the VALUE and keeps the row -- `request_id` is
    intact, so the malformed predicate never fires and no counter moves.
    Measured: `input_tokens: 9223372036854775808` -> NULL, `1e30` -> NULL,
    `"abc"` -> NULL, and `scanned=2, matched=2, store.problems=[]` for every
    one.  `_token_nulls` then counted that row and the aggregate note said
    "token columns are null where the payload did not state them" -- a false
    sentence about a figure the payload stated precisely.  The other direction
    is as quiet and adds rather than subtracts: `"7"` -> 7 and `true` -> 1.
    """
    if not have_duck():
        _skip("api: coerced values (duckdb not installed)")
        return
    root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    led = os.path.join(root, "accounts", u, "ledger.jsonl")
    shapes = [
        ("too-big-for-bigint", {"input_tokens": 9223372036854775808}),
        ("float-out-of-range", {"input_tokens": 1e30}),
        ("a-string-that-is-not-a-number", {"input_tokens": "abc"}),
        ("a-string-that-is", {"input_tokens": "7"}),
        ("a-boolean", {"input_tokens": True}),
        ("a-fraction", {"input_tokens": 1.5}),
        ("a-structure-in-a-varchar", {"model": {"a": 1}}),
        ("a-timestamp-as-text", {"ts": "2026-08-12T10:00:00Z"}),
    ]
    with open(led, "a") as fh:
        for i, (_name, over) in enumerate(shapes):
            r = dict({"request_id": "c0e0%012d" % i, "ts": 1786584663.0,
                      "account_uuid": u, "model": "claude-sonnet-5"}, **over)
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")

    _st, dia = ui_call(a, V1 + "diagnostics", "account=" + u)
    cv = dia["result"]["engine"]["coerced_values"]
    check_true("api/coerced: the diagnostics route counts them at all",
               cv["rows"] >= len(shapes))
    by = {c["column"]: c for c in cv["columns"]}
    check("api/coerced: and names the columns", sorted(by),
          ["input_tokens", "model", "ts"])
    check("api/coerced: with one entry per shape in input_tokens",
          by["input_tokens"]["rows"], 6)
    check_true("api/coerced: the count travels as a store problem too, so a "
               "client that never asks for diagnostics is still told",
               any(p.get("reason") == "coerced-values"
                   for p in dia["meta"]["store_problems"]))

    # And the sentence that used to be false.
    _st, agg = ui_call(a, V1 + "aggregate", "account=%s&by=model" % u)
    notes = " ".join(agg["result"]["notes"])
    check_true("api/coerced: the aggregate still says the nulls are summed as "
               "absent", "summed as absent" in notes)
    check("api/coerced: but no longer asserts the payload was silent",
          "did not state" in notes, False)
    check_true("api/coerced: and names where the difference is counted",
               "diagnostics" in notes)


def test_api_a_fault_in_the_reader_names_its_type_and_leaks_no_sql():
    """A 500 must not hand the caller the statement it was building.

    Several routes take caller-supplied identifiers, and an engine exception
    over one of those carried DuckDB's message -- the whole generated SQL and
    its candidate-binding list -- into `refusal.detail`, on an API that asks
    for no token and may be reachable on a network.  The operator still gets
    every byte of it, on the process's own stderr.
    """
    root = door_root()
    store_ = serve.Store(root)
    tenants = serve.Tenants(mapping={"tok-alpha": fx.uuid_of("alpha")})

    class Exploding(object):
        # `scope` is part of `Api.handle`'s signature: the door hands every
        # request the caller's account list, or None for "everything". A stub
        # that omits it fails with a TypeError the handler would report as
        # `reader-failed` -- a test double drifting from the interface it
        # stands in for, which reads as the very fault this test is about.
        def handle(self, path, params, multi, now=None, scope=None):
            raise RuntimeError("BinderException: SELECT count(\"nope\") "
                               "FROM read_json('/private/tmp/x')")

    serve.ShipHandler.door = serve.Door(store_, tenants)
    serve.ShipHandler.api = Exploding()
    serve.ShipHandler.quiet = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.ShipHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    import io as _io
    err = _io.StringIO()
    real = sys.stderr
    sys.stderr = err
    try:
        try:
            urllib.request.urlopen(
                "http://127.0.0.1:%d%ssearch" % (port, V1), timeout=10)
            doc, code = None, 200
        except urllib.error.HTTPError as exc:
            doc, code = json.loads(exc.read()), exc.code
    finally:
        sys.stderr = real
        serve.ShipHandler.api = None
        httpd.shutdown()
        httpd.server_close()
    check("api/http: a reader fault is a named 500",
          (code, doc["refusal"]["reason"]), (500, "reader-failed"))
    check("api/http: detail is the exception TYPE and nothing else",
          doc["refusal"]["detail"], "RuntimeError")
    check("api/http: no SQL reaches the caller",
          "read_json" in json.dumps(doc), False)
    check_true("api/http: and the operator gets the whole of it on stderr",
               "read_json" in err.getvalue())


def test_api_an_unknown_field_is_an_unanswerable_question_not_a_fault():
    """`/values?fields=` bypassed `compile_query` and reached the SQL builder.

    Measured: `?fields=nope` was HTTP 500 `reader-failed` whose remedy says
    "this is a fault in the reader", with the generated SQL in `detail` --
    while `/aggregate?by=nope` answers the identical question correctly as 400
    `unknown-column` naming the real columns.  One typo in a five-field sidebar
    request destroyed the whole payload rather than naming the field.
    """
    if not have_duck():
        _skip("api: unknown field (duckdb not installed)")
        return
    _root, _store, a = ui_store()
    u = fx.uuid_of("alpha")
    for bad in ("nope", 'model","source', "*", "a\") AS x, (SELECT 42) AS y",
                "; DROP TABLE x", "ledger.jsonl"):
        st, doc = ui_call(a, V1 + "values",
                          "account=%s&fields=%s"
                          % (u, urllib.parse.quote(bad, safe="")))
        check("api/values: fields=%r is refused by name" % bad[:18],
              (st, doc.get("refusal", {}).get("reason")),
              (400, "unknown-column"))
        check_true("api/values: ...naming the columns there are (%r)"
                   % bad[:18], "model" in (doc["refusal"]["remedy"] or ""))
        check("api/values: ...and no SQL text reaches the caller (%r)"
              % bad[:18], "read_json" in json.dumps(doc), False)
    st, ok = ui_call(a, V1 + "values", "account=%s&fields=model,source" % u)
    check("api/values: while the real ones still answer", (st, ok["outcome"]),
          (200, "ok"))
    st, tag = ui_call(a, V1 + "values",
                      "account=%s&fields=tags.account" % u)
    check("api/values: and a user tag is a real field here", st, 200)


def test_api_a_window_nobody_named_is_zero_and_a_window_nobody_watched_is_not():
    """Both halves of the tri-state live in the per-window coverage slot.

    `fraction: 0.0` with basis `no-attestation-names-it` is the POSITIVE claim
    that an attesting host did not name this window; `fraction: null` is
    "nobody said".  On the wire the only thing separating them was the JSON
    null -- which `x || 0`, `Number(null)`, `d3.sum` and every charting library
    turn into the same zero without a word.  So the boolean says which, on the
    rows that carry `attributed_pp` and `residual_pp`.
    """
    u = fx.uuid_of("alpha")
    att = fx.attestation(u, "5h", 1786598400, "darwin",
                         [[1786580400, 1786589400, True]])
    _r, _s, a = ui_store(extra=[("alpha", wire.STREAM_ATTEST,
                                 [ui_line(att)])])
    _st, v = ui_call(a, V1 + "windows", "account=" + u)
    named = [w for w in v["result"]["windows"]
             if (w["coverage"] or {}).get("basis") == "attestations"]
    unnamed = [w for w in v["result"]["windows"]
               if (w["coverage"] or {}).get("basis")
               == "no-attestation-names-it"]
    check_true("api/coverage: the capture really produces both shapes",
               named and unnamed)
    for w in named:
        check("api/coverage: a named window is known", w["coverage"]["known"],
              True)
        check("api/coverage: ...and measured", w["coverage"]["basis_is"],
              "measured")
    for w in unnamed:
        check("api/coverage: a window an attesting host did not name is a "
              "ZERO that is known", 
              (w["coverage"]["known"], w["coverage"]["fraction"]), (True, 0.0))

    # And on a store with no attestation at all, the same slot is the other
    # half: null, known false, never 0.0.
    _r2, _s2, a2 = ui_store()
    _st, v2 = ui_call(a2, V1 + "windows", "account=" + u)
    for w in v2["result"]["windows"]:
        check("api/coverage: with nobody attesting the window says unknown",
              (w["coverage"]["known"], w["coverage"]["fraction"]),
              (False, None))
        check("api/coverage: ...and never 0.0, which is the other claim",
              w["coverage"]["fraction"] == 0.0, False)


def _coverages(doc, out=None):
    """Every coverage object anywhere in a payload.

    Selected on the KEY alone.  It used to require `"known" in v` as well,
    which turned "every coverage object carries the boolean" into "every
    coverage object that carries the boolean carries it" -- and the objects it
    therefore skipped were every per-window one, i.e. the ones sitting beside
    `attributed_pp` and `residual_pp`.  The assertions were already right; the
    filter was the bug's own alibi.
    """
    out = [] if out is None else out
    if isinstance(doc, dict):
        for k, v in doc.items():
            if k == "coverage" and isinstance(v, dict):
                out.append(v)
            else:
                _coverages(v, out)
    elif isinstance(doc, list):
        for v in doc:
            _coverages(v, out)
    return out


def _coverage_is_refusal(cov):
    """A `coverage` slot holding a REFUSAL rather than a statement.

    `/api/v1/window` computes its coverage over the request rows inside the
    window, and with no engine those rows were never read -- so that slot is a
    refusal object with no `placement`, no `windows` and no `fraction`, in
    exactly the shape `requests` beside it already uses.

    Told apart HERE and not skipped in the walker.  `_coverages` selects on
    the key alone, deliberately: it used to require `"known" in v` as well,
    which turned "every coverage object carries the boolean" into "every
    coverage object that carries the boolean carries it" and gave the bug its
    own alibi.  Adding `and "unavailable" not in v` to the walker would be the
    identical mistake in a new costume, so the walker still returns
    everything and the callers assert that each object is one of exactly two
    well-formed shapes.
    """
    return isinstance(cov, dict) and "unavailable" in cov


def _check_coverage_shape(cov, where):
    """Every coverage slot is a statement OR a named refusal -- never between.

    The half that matters: a refusal must carry NONE of the numbers, because a
    placement census of zeros over rows nobody read is indistinguishable from
    a real window that holds no requests, and `basis: "no-window-touched"` is
    a positive claim that none of those rows fell in any window.
    """
    if not _coverage_is_refusal(cov):
        check_true("api/coverage: %s is a statement carrying the tri-state"
                   % where, "known" in cov and "basis" in cov)
        return False
    check_true("api/coverage: %s is refused BY NAME" % where,
               isinstance(cov["unavailable"], dict)
               and bool(cov["unavailable"].get("reason")))
    check("api/coverage: %s carries no number to read as a measurement"
          % where,
          sorted(k for k in ("placement", "windows", "fraction", "known",
                             "basis") if k in cov), [])
    return True


def _bases_in(path, only_coverage_funcs=False):
    """Every string a module assigns to a name or keyword called `basis`.

    Derived rather than listed, for the reason every other derived list here
    exists: a hand-maintained catalogue is a second place to forget, and the
    thing it forgets renders as something else.  `ast`, not a regex, because
    the bases are written three different ways -- a keyword argument, a tuple
    unpack (`fraction, basis = None, "no-attestation"`) and a conditional
    expression carrying a `%d` -- and a regex that reads two of the three is
    the same false confidence as no check at all.
    """
    import ast as _ast
    tree = _ast.parse(open(path, encoding="utf-8").read())
    out = set()
    if only_coverage_funcs:
        # `query.py` has a second, unrelated `basis`: `Plan.basis` is a
        # sentence about how a cost estimate was reached.  Scoped by function
        # name rather than by pattern, because the two are the same word and
        # only their neighbourhood tells them apart.
        tree = _ast.Module(
            body=[n for n in _ast.walk(tree)
                  if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))
                  and "coverage" in n.name],
            type_ignores=[])

    def strings(node):
        for n in _ast.walk(node):
            if isinstance(n, _ast.Constant) and isinstance(n.value, str):
                out.add(n.value)

    for n in _ast.walk(tree):
        if isinstance(n, _ast.keyword) and n.arg == "basis":
            strings(n.value)
        elif isinstance(n, _ast.Assign):
            names, values = [], []
            for t in n.targets:
                if isinstance(t, _ast.Tuple):
                    names += list(t.elts)
                    values = (list(n.value.elts)
                              if isinstance(n.value, _ast.Tuple) else [n.value])
                else:
                    names.append(t)
                    values = [n.value]
            for i, nm in enumerate(names):
                if isinstance(nm, _ast.Name) and nm.id == "basis":
                    strings(values[i] if i < len(values) else n.value)
    return out


def _basis_declared(caps, basis):
    """Classifiable from the SERVED catalogue alone, exact key or prefix."""
    m = caps["result"]["coverage_basis"]
    if basis in m:
        return True
    for pre in caps["result"]["coverage_basis_prefixes"]:
        if isinstance(basis, str) and basis.startswith(pre):
            return True
    return False


# =========================================== 11. PROVENANCE LEDGER ==========
#
# `zz` because `TESTS` is sorted by name and these two must run LAST: they
# audit what every test above put into the provenance ledger, and an audit that
# runs in the middle reports half the file and passes.


def test_zz_the_authored_set_is_exactly_manifest_and_attestation():
    """Nothing else in this suite may be authored.

    Everything else is a real capture or a real capture with named fields
    overridden.  If this fails, something was invented -- which is how a metric
    name the collector never emitted and six query_source values Claude Code
    never sent both read as coverage while matching nothing.
    """
    kinds = sorted({k for k, _why in fx.AUTHORED})
    check("provenance: only the manifest and the attestation are authored",
          kinds, ["attestation", "manifest"])
    check_true("provenance: and both are marked in srv/ as authored",
               "AUTHORED" in open(os.path.join(fx.SRV_DIR, "srv", "wire.py"),
                                  encoding="utf-8").read().upper())


DERIVED_EXPECTED = {
    "adversarial-5h-only": ("seven_day_pct", "seven_day_resets_at"),
    "adversarial-ghost-window": ("five_hour_pct", "five_hour_resets_at"),
    "adversarial-request": ("account_uuid", "cost_usd_reported", "host",
                            "request_id", "ts"),
    "undatable-request": ("request_id", "ts"),
    "collision": ("account_uuid", "request_id"),
    "no-cost": ("cost_usd_reported",),
    "no-movement-request": ("request_id", "ts"),
    "no-timestamp": ("ts",),
    "orphan-request": ("request_id", "ts"),
    "query-falsy-label": ("profile", "tags"),
    "query-late-arrival": ("request_id", "ts"),
    "query-null-tokens": ("output_tokens",),
    "query-replay-request": ("account_uuid", "request_id", "ts"),
    "query-undatable-request": ("request_id", "ts"),
    "duck-numeric-never-observed": ("event_sequence", "request_id"),
    "duck-undatable-request": ("request_id", "ts"),
    "duck-string-never-observed": ("event_sequence", "request_id"),
    "ui-synthetic-request": ("request_id", "source", "ts"),
    "rollover-2": ("five_hour_pct", "five_hour_resets_at", "ts"),
    "rollover-90": ("five_hour_pct", "five_hour_resets_at", "ts"),
    "second-machine": ("host",),
    "second-machine-v1-line": (),
    "second-window-request": ("request_id", "ts"),
    "second-window-sample": ("five_hour_resets_at", "ts"),
    "skewed-machine": ("five_hour_pct", "host", "ts"),
}


def test_zz_every_derivation_is_declared_field_by_field():
    """The ledger is pinned, not merely printed.

    A derivation that quietly grows a field is how a fixture stops being real
    data while still being called real data.  So the exact set of overridden
    keys is asserted per derivation: adding one means editing this table, in
    the same commit, where a reviewer sees it.
    """
    seen = {}
    for label, base, keys in fx.DERIVATIONS:
        seen.setdefault(label, set()).update(keys)
        if not base:
            check("provenance: %s names no base record" % label, base, "a base")
    got = {k: tuple(sorted(v)) for k, v in seen.items()}
    check("provenance: every derivation, and exactly the fields it overrides",
          got, {k: tuple(sorted(v)) for k, v in DERIVED_EXPECTED.items()})
    for label in sorted(got):
        print("     derived  %-24s -> %s"
              % (label, ", ".join(got[label]) or "(nothing -- the v1 shape "
                 "has no host column to change)"))


# ================================= 11. THE DUCKDB QUERY LAYER ================
#
# `srv/duck.py` (pure: the SQL) and `srv/store.py` (impure: the connection).
# The tests live in `tests/test_duck.py` because they are a section of their
# own, not because they run apart: they are registered here, with this
# harness's `check`, so one command still runs everything and one count still
# means everything.
#
# `skip` is loud on purpose.  DuckDB is optional on a developer's machine and
# mandatory on the server, so the tests that need a connection say so by name
# when they do not run, and the ones that do not need it -- the pinned column
# list, the SQL text, the refusals, the offline guarantee -- always run.  A
# suite that quietly drops half its assertions when a module is missing is the
# decoration this project keeps writing tests against.

SKIPPED = []


def _skip(name):
    SKIPPED.append(name)


import test_duck as td                                          # noqa: E402


def _duck_tests():
    out = []
    for name in sorted(dir(td)):
        if not name.startswith("test_"):
            continue
        fn = getattr(td, name)
        # Bound BY NAME, never by position: `(check, skip)` and
        # `(check, check_true)` are both two arguments, and a positional bind
        # would hand a test the wrong function and pass anyway.
        want = fn.__code__.co_varnames[:fn.__code__.co_argcount]
        avail = {"check": check, "check_true": check_true, "skip": _skip}

        def run(fn=fn, want=want, avail=avail):
            return fn(*[avail[a] for a in want])
        run.__name__ = name
        out.append(run)
    return out


# ---- the container, and the boundary it must not cross ----------------------

def _server_dir():
    """The real `server/` directory, never the mutation harness's copy.

    `fx.SRV_DIR` is repointed at a temp tree by `mutate.py`, which copies `srv/`
    and `README.md` and nothing else.  These tests are about files that live
    beside them, so they read the real directory -- which is also correct, since
    a mutation of `srv/` cannot change what a Dockerfile pins.
    """
    return os.path.dirname(HERE)


def test_the_container_pins_the_engine_the_measurements_were_written_against():
    """The Dockerfile's pin, `duck.py`'s prose and the image tag say one version.

    `srv/duck.py` does not use DuckDB so much as refuse five specific things it
    does silently, and every one of those refusals cites a behaviour "measured
    on 1.5.5" -- a column dropped by inference, `ignore_errors` emitting a NULL
    row, BIGINT coercing rather than refusing, what binds and what does not, and
    NaN ordering above infinity.  A floating version in the container would
    leave those sentences in place while removing the evidence for them, which
    this project treats as the same defect as an untested assertion.

    So the version is DERIVED from `duck.py`'s own comments rather than
    hardcoded here -- a hardcoded copy would be a fourth place to forget.
    """
    srv_dir = _server_dir()
    dockerfile = os.path.join(srv_dir, "Dockerfile")
    compose = os.path.join(srv_dir, "compose.yml")
    for path in (dockerfile, compose):
        if not os.path.exists(path):
            # Named, not raised.  An exception counts as a failure to the
            # mutation harness exactly as an assertion does, so a test that
            # cannot find its input would manufacture a "caught" verdict for
            # every row in the matrix.
            check("container: %s exists" % os.path.basename(path),
                  path, "a file that exists")
            return

    duck_src = open(os.path.join(fx.SRV_DIR, "srv", "duck.py"),
                    encoding="utf-8").read()
    measured = sorted(set(re.findall(r"[Mm]easured on (\d+\.\d+\.\d+)",
                                     duck_src)))
    check_true("container: duck.py cites a measured engine version at all",
               len(measured) == 1)
    if len(measured) != 1:
        return
    version = measured[0]

    df = open(dockerfile, encoding="utf-8").read()
    pins = re.findall(r"^ARG DUCKDB_VERSION=(\S+)\s*$", df, re.M)
    check("container: the Dockerfile pins exactly one DuckDB version",
          len(pins), 1)
    if len(pins) == 1:
        check("container: ...and it is the one duck.py's measurements cite",
              pins[0], version)

    # The install must be pinned at the point of installation too: an `ARG` that
    # nothing interpolates is a comment.
    check_true("container: the pin reaches the pip install",
               'duckdb==${DUCKDB_VERSION}' in df)
    # And nothing is copied in, because the repository is bind-mounted.  An
    # image carrying a snapshot of the source is one a developer can test the
    # previous commit through while reading the current one.
    check("container: the image carries no source (no COPY/ADD)",
          re.findall(r"^\s*(COPY|ADD)\s", df, re.M), [])

    cy = open(compose, encoding="utf-8").read()
    check_true("container: the image tag names the pinned version",
               ("claudio-server:duckdb-" + version) in cy)

    # Every check below is scoped to ONE SERVICE's block, never grepped over
    # the whole file, and that is not fastidiousness -- it is a bug this suite
    # caught in its own first draft.  `--require-duckdb` is named in the
    # comment that explains it, so a whole-file `in` passed with the flag
    # deleted from the command: co-occurrence asserted instead of
    # configuration, the exact shape of the `account.<name>.ship_*` doc guard
    # that let three mutations through green.
    def service_block(name):
        m = re.search(r"^  %s:\n(.*?)(?=^  \S|\Z)" % re.escape(name),
                      cy, re.M | re.S)
        return m.group(1) if m else None

    suite_block = service_block("suite")
    if suite_block is None:
        check("container: compose.yml declares a `suite` service",
              "suite", "a service that exists")
    else:
        # The one obvious command must be the STRICT one.  A container built
        # specifically to supply DuckDB has no business reporting success over
        # a run that skipped it.  Read off the `command:` line, not the block.
        cmd = re.search(r"^    command:.*$", suite_block, re.M)
        check_true("container: the suite service's command runs the suite",
                   cmd is not None and "server/tests/test_all.py" in cmd.group(0))
        check_true("container: ...with --require-duckdb, so a skipping run "
                   "cannot pass here",
                   cmd is not None and "--require-duckdb" in cmd.group(0))

    door_block = service_block("door")
    if door_block is None:
        check("container: compose.yml declares a `door` service",
              "door", "a service that exists")
    else:
        # The publish mapping is the whole of the network difference between
        # this and facing the internet.  `--host 0.0.0.0` inside a container is
        # the container's own namespace; the host-side bind is what decides who
        # can reach it.
        # From the `ports:` list alone.  Over the whole service block the
        # `--port` argument's own `"8787"` matches too, and a guard that counts
        # it is reporting on the command line while claiming to report on the
        # network.
        ports = re.search(r"^    ports:\n((?:      -.*\n)+)", door_block, re.M)
        published = (re.findall(r'-\s*"?([^"\n]+?)"?\s*$',
                                ports.group(1), re.M) if ports else [])
        check("container: the door publishes to loopback on the host",
              published, ["127.0.0.1:8787:8787"])


def test_a_skipping_run_says_so_after_the_summary():
    """The banner: its content, its position, and the shape `mutate.py` reads.

    Measured on this tree, same source, two interpreters: 1164 passed / 44
    skipped without duckdb, 2082 passed / 0 skipped with it -- 918 assertions
    that did not run.  They are announced, so this is not a silent failure; but
    "1164 passed, 0 failed" is what a reader acts on, and 44 SKIP lines scroll
    off the top of a terminal while a summary does not.

    The banner is pinned rather than trusted because it is the one line of
    output whose ABSENCE is invisible: a run with no banner and a run that
    skipped nothing look identical.
    """
    saved = list(SKIPPED)
    try:
        del SKIPPED[:]
        check("suite: a run that skipped nothing prints no banner",
              _skip_banner(), None)

        SKIPPED.append("api: values (duckdb not installed)")
        SKIPPED.append("something else entirely")
        banner = _skip_banner()
        check_true("suite: a skipping run prints a banner at all",
                   isinstance(banner, str) and banner.strip() != "")
        if not isinstance(banner, str):
            # Named, then stop.  Falling through would raise a TypeError out of
            # the test, which the mutation harness counts as a failure exactly
            # as an assertion -- so every later row would read "caught" on the
            # strength of a traceback rather than an assertion.
            return
        check_true("suite: ...naming how many of how many self-skipped",
                   "2 of %d test functions SELF-SKIPPED" % len(TESTS)
                   in banner)
        check_true("suite: ...and how many of those were the engine",
                   "(1 of them for duckdb)" in banner)
        check_true("suite: ...and the interpreter that could not import it",
                   sys.executable in banner)
        check_true("suite: ...and that the run is not evidence about the "
                   "engine",
                   "NOT EVIDENCE ABOUT THE ENGINE" in banner)
        # A reason with no action is a fault report, not a remedy -- the same
        # rule the API's refusals are held to.  Both remedies, because a
        # developer with no container runtime must still have one.
        check_true("suite: ...and the container command",
                   "docker compose -f server/compose.yml run --rm suite"
                   in banner)
        check_true("suite: ...and the no-container remedy beside it",
                   "pip install duckdb" in banner)

        # `mutate.py` collects baseline skips with `startswith("SKIP")` after
        # stripping.  A banner line that looked like one would inflate the
        # count it refuses on, and the refusal would then name a skip that does
        # not exist.
        offenders = [ln for ln in banner.splitlines()
                     if ln.strip().startswith("SKIP")]
        check("suite: no banner line can be mistaken for a SKIP line",
              offenders, [])
    finally:
        del SKIPPED[:]
        SKIPPED.extend(saved)


def test_a_skipping_run_exits_non_zero_unless_told_not_to():
    """The exit code, which is the only part of a skipping run a script reads.

    `make check` runs this file, and `make check` is what this project
    documents as the release gate. With the old default -- banner, exit 0 --
    it reported success over 918 of 2073 assertions that had never run, on a
    machine with no engine. Measured both ways on this tree: plain interpreter
    1164 passed / 44 skipped, engine interpreter 2082 passed / 0 skipped.

    It is `claudio usage doctor`'s ruling one repository half over: every WARN
    it prints sets a non-zero exit, "because it used to print `WARN ledger
    rows: 0` and exit 0".

    Driven through `main()` with the module's own globals stubbed, rather than
    by spawning an interpreter without duckdb -- which is not something a
    machine that HAS duckdb can arrange, and this suite must pin the behaviour
    on both kinds of machine.
    """
    saved_skipped = list(SKIPPED)
    saved_tests = list(TESTS)
    try:
        # One trivial test, and a skip recorded as if it had self-skipped.
        del TESTS[:]
        TESTS.append(lambda: None)

        # `main` reads and writes the module's own PASS/FAIL/FAILURES, so every
        # sub-run is bracketed: zeroed on the way in so its exit code is about
        # the sub-run, and RESTORED EXACTLY on the way out.  An earlier draft
        # only zeroed, which erased this suite's real failure count -- a run
        # printing FAIL lines above `0 failed`.  A test that quietly repairs
        # the summary is worse than no test at all, and that is the shape this
        # whole file exists to refuse.
        def run(args, skips=("api: values (duckdb not installed)",)):
            global PASS, FAIL
            keep = (PASS, FAIL, list(FAILURES), list(SKIPPED))
            del SKIPPED[:]
            SKIPPED.extend(skips)
            PASS = FAIL = 0
            del FAILURES[:]
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    rc = main(args)
            finally:
                PASS, FAIL = keep[0], keep[1]
                del FAILURES[:]
                FAILURES.extend(keep[2])
                del SKIPPED[:]
                SKIPPED.extend(keep[3])
            return rc, buf.getvalue()

        rc_clean, _ = run([], skips=())
        check("suite: a run that skipped nothing exits 0", rc_clean, 0)

        rc, text = run([])
        check("suite: a skipping run exits non-zero by default", rc, 1)
        check_true("suite: ...and says why, naming how many it skipped",
                   "this run skipped 1 test function(s)" in text)
        check_true("suite: ...and names the way to accept it anyway",
                   "--allow-skips" in text)

        rc2, text2 = run(["--allow-skips"])
        check("suite: --allow-skips accepts a skipping run", rc2, 0)
        check_true("suite: ...and still says out loud what it accepted",
                   "skipped 1 test function(s)" in text2)

        # The flag `compose.yml` passes is still accepted -- it predates this
        # change and an environment that supplies the engine saying so is
        # documentation. It must NOT be an unknown argument.
        rc3, _ = run(["--require-duckdb"])
        check("suite: --require-duckdb is still accepted (and now redundant)",
              rc3, 1)

        # ...and an argument nothing knows is still refused BY NAME rather
        # than ignored, which is the rule the API half spends several tests on.
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc4 = main(["--allow-skips", "--nonsense"])
        check("suite: an unknown argument is refused", rc4, 2)
        check_true("suite: ...by name", "--nonsense" in err.getvalue())
    finally:
        del SKIPPED[:]
        SKIPPED.extend(saved_skipped)
        del TESTS[:]
        TESTS.extend(saved_tests)


# =============================================================== runner =====

def test_api_reader_auth_scopes_by_refusal_not_by_filter():
    """A reader token names which accounts it may read, and an account outside
    that scope is REFUSED BY NAME.

    The distinction is the whole point. An empty result for an account this
    token may not see is byte-indistinguishable from an account that has
    shipped nothing, and telling those apart is why this API spells `no-data`,
    `filtered-to-nothing` and `unanswerable` as three different words. A
    permission boundary that expresses itself as absence is a fourth silence.

    Exercised over HTTP against a live door, because the gate lives in the
    request handler and an in-process call would never reach it.
    """
    root, store_, a = ui_store()
    alpha = fx.uuid_of("alpha")
    beta = fx.uuid_of("beta")
    counters = serve.Counters()
    tenants = serve.Tenants(mapping={"tok-alpha": alpha})
    door = serve.Door(store_, tenants, counters=counters)
    door.readers = serve.Readers(mapping={
        # "*" is the ORDINARY case and the one a front end holds: omini reads
        # everything, and isolation between people is omini's job, the way
        # Graylog streams and Kibana index patterns are. The scoped token is
        # the exception -- a reporting script, a per-team reader.
        "r-all": serve.ALL_ACCOUNTS,
        "r-alpha": [alpha],
    })
    serve.ShipHandler.door = door
    serve.ShipHandler.api = a
    serve.ShipHandler.quiet = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.ShipHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def get(path, token=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path))
        if token:
            req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=10) as fh:
                return fh.status, json.loads(fh.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        status, doc = get(V1 + "accounts")
        check("reader-auth: no token is 401, not an empty answer",
              (status, doc.get("refusal", {}).get("reason")),
              (401, "unauthorised"))
        check("reader-auth: the 401 carries a remedy a caller can act on",
              bool(doc.get("refusal", {}).get("remedy")), True)

        status, doc = get(V1 + "accounts", token="nope")
        check("reader-auth: an unknown token is 401 by the same name",
              (status, doc.get("refusal", {}).get("reason")),
              (401, "unauthorised"))

        # THE PERMITTED CASES ASK A RECONCILER ROUTE, NOT `search`, AND THAT IS
        # A CORRECTION RATHER THAN A CONVENIENCE.
        #
        # The gate is `ShipHandler._reader_auth`, which runs in `do_GET`
        # before any route is dispatched and never looks at the engine.  These
        # three assertions used to ask `/search`, which is a stream-A route, so
        # on an interpreter with no DuckDB they got the perfectly correct
        # `503 duckdb-missing` and reported it as an auth failure -- four red
        # assertions in a configuration this repository PUBLISHES and documents
        # (`1164 passed, 0 failed, 44 skipped`), for a reason with nothing to do
        # with reader auth.  A test that cannot distinguish "this token was
        # refused" from "this build has no query engine" is not testing the
        # thing it is named after.
        #
        # `windows` is account-scoped, goes through the identical gate, and
        # needs no engine.  The stream-A half is not lost: the 403 below still
        # asks `/search`, because a refusal that happens before dispatch is
        # exactly what has to be proved about a route the engine cannot serve
        # -- and the engine-present case is asserted at the bottom, under a
        # loud skip.
        for acct in (alpha, beta):
            status, doc = get(V1 + "windows?account=" + acct, token="r-all")
            check("reader-auth: a universal token reads every account (%s)"
                  % acct[:8], (status, doc.get("outcome")), (200, "ok"))
        status, doc = get(V1 + "accounts", token="r-all")
        check("reader-auth: and may ask a store-wide question",
              (status, doc.get("outcome")), (200, "ok"))

        status, doc = get(V1 + "windows?account=" + alpha, token="r-alpha")
        check("reader-auth: a scoped token reads the account it covers",
              (status, doc.get("outcome")), (200, "ok"))

        status, doc = get(V1 + "search?account=" + beta, token="r-alpha")
        check("reader-auth: another account is REFUSED BY NAME, not emptied",
              (status, doc.get("refusal", {}).get("reason")),
              (403, "account-not-permitted"))
        check("reader-auth: a refusal carries no result key to render as empty",
              "result" in doc, False)
        check("reader-auth: ...and the refusal beats the engine, so a store "
              "with no DuckDB still says WHY", status, 403)

        status, doc = get(V1 + "windows?account=" + beta, token="r-all")
        check("reader-auth: a '*' token reads any account",
              (status, doc.get("outcome")), (200, "ok"))

        # The half that does need an engine, asserted when there is one and
        # named when there is not.  Without this the move above would quietly
        # stop proving that a permitted token can reach a stream-A route at
        # all.
        if duckstore.available():
            status, doc = get(V1 + "search?account=" + alpha, token="r-all")
            check("reader-auth: a permitted token reaches a stream-A route too",
                  (status, doc.get("outcome")), (200, "ok"))
        else:
            _skip("reader-auth: stream-A route under a permitted token "
                  "(duckdb not installed)")

        # Operators and health checks are deliberately outside the gate.
        req = urllib.request.Request("http://127.0.0.1:%d/healthz" % port)
        with urllib.request.urlopen(req, timeout=10) as fh:
            check("reader-auth: /healthz stays open for operators",
                  fh.status, 200)
    finally:
        httpd.shutdown()
        serve.ShipHandler.door = None
        serve.ShipHandler.api = None


def _swell_ledger(root, uuid, n):
    """Append `n` more rows to an account's ledger, derived from its own first
    row so the shape is the captured one and only the identity moves.

    A three-row account cannot show a race: the window in which two threads are
    both inside DuckDB is the time the scan takes, and three rows is not
    enough of it.  The measured shapes were 303 rows against 60, so that
    asymmetry is what is built here -- it is also what makes a crossed answer
    VISIBLE, since one account's `matched` is not the other's.
    """
    path = os.path.join(root, "accounts", uuid, "ledger.jsonl")
    with open(path, "r", encoding="utf-8") as fh:
        base = json.loads(fh.readline())
    with open(path, "a", encoding="utf-8") as fh:
        for i in range(n):
            row = dict(base)
            row["request_id"] = "%016x" % (abs(hash((uuid, i))) & ((1 << 64) - 1))
            row["ts"] = base["ts"] + i + 1
            row["ts_ns"] = int(row["ts"] * 1e9)
            fh.write(json.dumps(row, sort_keys=True) + "\n")


def test_api_two_requests_at_once_do_not_read_each_others_results():
    """Concurrent reads must not share a DuckDB result slot.

    THE DEFECT.  `DuckStore.con()` opened one `duckdb.connect()` and handed
    that same handle to every caller.  The door is a `ThreadingHTTPServer` with
    `daemon_threads`, so every request runs in its own thread, and a
    `DuckDBPyConnection` holds the pending result of the last `execute` ON THE
    CONNECTION -- so two request threads consumed each other's rows.

    Measured against a COMPLETELY STATIC store, with no writer running and
    nothing appending, so this is not the read-while-append path and the store
    lock is innocent.  8 threads, 480 requests: 71 answered HTTP 500
    `reader-failed` and 30 answered HTTP 200 `ok` carrying another question's
    figures -- `matched: 303` beside an EMPTY rows list, and one account's
    request answered with the other account's `matched`.

    WHY BOTH HALVES ARE ASSERTED SEPARATELY.  They are two defects wearing one
    cause, and only the second one matters.  Pinning the 500s alone would leave
    the silent wrong answer unguarded, and a future `except Exception: return
    no-data` would turn such a test green while making the product strictly
    worse -- an empty result where a figure crossed accounts is the exact
    shape this envelope was designed to make impossible.

    WHY THIS TEST HAD TO BE WRITTEN AT ALL.  Every `threading.Thread` in this
    file ran `serve_forever` or `shutdown`; not one issued two API requests at
    the same moment, so a defect that exists only when two threads are inside
    `DuckStore` together was structurally invisible here -- and the mutation
    matrix inherited the blindness.  That is the `by_tag` 500's blind spot one
    axis over: there it was "never serialised what came back", here it is
    "never asked two questions at once".
    """
    if not have_duck():
        _skip("api/concurrency: two readers at once (duckdb not installed)")
        return
    root, store_, a = ui_store()
    alpha, beta = fx.uuid_of("alpha"), fx.uuid_of("beta")
    _swell_ledger(root, alpha, 300)
    _swell_ledger(root, beta, 57)

    tenants = serve.Tenants(mapping={"tok-alpha": alpha})
    serve.ShipHandler.door = serve.Door(store_, tenants,
                                        counters=serve.Counters())
    serve.ShipHandler.door.readers = None
    serve.ShipHandler.api = a
    serve.ShipHandler.quiet = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.ShipHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def get(uuid):
        url = ("http://127.0.0.1:%d%ssearch?account=%s&limit=400"
               % (port, V1, uuid))
        try:
            with urllib.request.urlopen(url, timeout=30) as fh:
                return fh.status, json.loads(fh.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    try:
        # The serial truth, one question at a time, before any thread exists.
        truth = {}
        for uuid in (alpha, beta):
            st, doc = get(uuid)
            check("api/concurrency: the serial answer for %s is ok" % uuid[:8],
                  (st, doc.get("outcome")), (200, "ok"))
            page = doc["result"]["page"]
            truth[uuid] = (page["matched"], len(doc["result"]["rows"]))
        check_true("api/concurrency: the two accounts have different figures, "
                   "so a crossed answer is visible",
                   truth[alpha] != truth[beta])

        faults, wrong, foreign = [], [], []
        lock = threading.Lock()

        def hammer(seed):
            for i in range(40):
                uuid = alpha if (seed + i) % 2 else beta
                st, doc = get(uuid)
                if st != 200 or doc.get("outcome") != "ok":
                    with lock:
                        faults.append((st, (doc.get("refusal") or {})
                                       .get("reason")))
                    continue
                rows = doc["result"]["rows"]
                got = (doc["result"]["page"]["matched"], len(rows))
                if got != truth[uuid]:
                    with lock:
                        wrong.append((uuid[:8], truth[uuid], got))
                bad = sorted({r.get("account_uuid") for r in rows} - {uuid})
                if bad:
                    with lock:
                        foreign.append((uuid[:8], bad))

        threads = [threading.Thread(target=hammer, args=(s,))
                   for s in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 240 requests. Unpatched this produced 37 faults and 14 wrong answers,
        # so it fires almost at once rather than only under sustained load.
        check("api/concurrency: no request faulted (%d seen)" % len(faults),
              faults[:4], [])
        check("api/concurrency: and every 200 carries ITS OWN figures "
              "(%d disagreed)" % len(wrong), wrong[:4], [])
        check("api/concurrency: no row crossed an account boundary",
              foreign[:4], [])
    finally:
        httpd.shutdown()
        httpd.server_close()
        serve.ShipHandler.door = None
        serve.ShipHandler.api = None


def _uuids_in(obj, out=None):
    """Every account-shaped UUID anywhere in a payload, at any depth."""
    if out is None:
        out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            _uuids_in(k, out)
            _uuids_in(v, out)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _uuids_in(v, out)
    elif isinstance(obj, str):
        for m in re.finditer(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                obj):
            out.add(m.group(0))
    return out


def test_api_a_scoped_token_that_names_no_account_is_refused_not_answered():
    """Scope is decided by the TOKEN, not by the `account` query parameter.

    THE HOLE.  The gate read `params.get("account")` and `Readers.permits`
    returned True for `account is None`, on a docstring that delegated the
    omitted case to "the ROUTE ... and `store` already refuses to total across
    accounts".  `store` refuses to TOTAL across accounts; it returns ROWS
    across them happily.  Measured through the running stack with a token
    scoped to one account:

        /search?text=<other account's address>  200 ok, their rows
        /lookup?field=session_id&value=<theirs> 200 ok, their rows
        /accounts                               200 ok, both identities
        /diagnostics                            200 ok, both

    and identically through the MCP surface, which forwards the caller's
    token: `tools/call search` returned `isError: false` and another account's
    rows to an agent.  Every existing assertion asked with `?account=`, so the
    scope held only against a caller who cooperated by naming the account it
    was not allowed to read.

    A REFUSAL, NOT A NARROWED ANSWER, for this file's standing reason: a
    silently narrowed result is indistinguishable from an account that has
    shipped nothing, and telling those apart is why `no-data`,
    `filtered-to-nothing` and `unanswerable` are three different words.

    Asked of EVERY route rather than of the four that were measured, because
    the next route added is the next hole and a list of four would not have
    caught it.
    """
    root, store_, a = ui_store()
    alpha, beta, agent = (fx.uuid_of("alpha"), fx.uuid_of("beta"),
                          fx.uuid_of("agent"))
    counters = serve.Counters()
    door = serve.Door(store_, serve.Tenants(mapping={"tok-alpha": alpha}),
                      counters=counters)
    door.readers = serve.Readers(mapping={"r-alpha": [alpha],
                                          "r-all": serve.ALL_ACCOUNTS})
    serve.ShipHandler.door = door
    serve.ShipHandler.api = a
    serve.ShipHandler.quiet = True
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), serve.ShipHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]

    def get(path, token, qs=""):
        url = "http://127.0.0.1:%d%s%s" % (port, path, ("?" + qs) if qs else "")
        req = urllib.request.Request(url)
        req.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(req, timeout=20) as fh:
                return fh.status, json.loads(fh.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    outside = {beta, agent}
    try:
        answered, leaked = [], []
        for route in a.routes():
            slug = route[len(V1):]
            st, doc = get(route, "r-alpha")
            free = slug in api.SCOPE_FREE_ROUTES
            reason = (doc.get("refusal") or {}).get("reason")
            if not free:
                if not (st == 403
                        and reason == "account-required-for-this-token"):
                    answered.append((slug, st, reason))
            found = _uuids_in(doc) & outside
            if found:
                leaked.append((slug, sorted(found)))
        check("reader-scope: every record route refuses a scoped token that "
              "names no account", answered, [])
        # The assertion that survives any later change to WHICH routes refuse:
        # whatever the answer is, it must never name an account outside the
        # scope.
        check("reader-scope: and no payload names an account outside the "
              "token's scope", leaked, [])

        # The three exceptions still answer, because refusing them would make
        # a scoped token unable to read its own answers: `capabilities` is
        # where a client learns what a null means here.
        for slug in api.SCOPE_FREE_ROUTES:
            st, doc = get(V1 + slug, "r-alpha")
            check("reader-scope: %s is still answered (it reads no record)"
                  % slug, (st, doc.get("outcome")), (200, "ok"))

        # The remedy is an ACTION, and the action has to work.
        st, doc = get(V1 + "windows", "r-alpha")
        check("reader-scope: the refusal names the accounts the token covers",
              alpha in doc["refusal"]["remedy"], True)
        check("reader-scope: ...and carries no result key to render as empty",
              "result" in doc, False)
        st, doc = get(V1 + "windows", "r-alpha", "account=" + alpha)
        check("reader-scope: following the remedy answers",
              (st, doc.get("outcome")), (200, "ok"))

        # TWO ROUTES CANNOT BE SCOPED AT ALL, and the generic remedy would
        # send their callers into a SECOND refusal.
        # `aggregate/per-account` refuses `?account=` by design;
        # `accounts` reads no `account` parameter, so passing one is
        # `unknown-query-key`.  Each gets its own sentence, and each names a
        # route that really exists and really answers.
        for slug, must_name in (("aggregate/per-account",
                                 V1 + "aggregate?account="),
                                ("accounts", V1 + "windows?account=")):
            st, doc = get(V1 + slug, "r-alpha",
                          "by=model" if slug != "accounts" else "")
            check("reader-scope: %s is refused by the same name" % slug,
                  (st, doc["refusal"]["reason"]),
                  (403, "account-required-for-this-token"))
            check_true("reader-scope: ...with a remedy that names a route that "
                       "answers, not one that refuses again (%s)" % slug,
                       must_name in doc["refusal"]["remedy"])
            check_true("reader-scope: ...and the covered UUIDs, which is how a "
                       "scoped caller learns its own scope (%s)" % slug,
                       alpha in doc["refusal"]["remedy"])
        # And the remedy is followed, so it is an action rather than a claim.
        st, doc = get(V1 + "windows", "r-alpha", "account=" + alpha)
        check("reader-scope: the accounts remedy really answers",
              (st, doc.get("outcome")), (200, "ok"))
        st, doc = get(V1 + "aggregate", "r-alpha",
                      "account=%s&by=model" % alpha)
        check_true("reader-scope: ...and so does the per-account one",
                   st == 200 or doc["refusal"]["reason"] == "duckdb-missing")

        # And nothing changed for the ordinary token a front end holds.
        st, doc = get(V1 + "accounts", "r-all")
        check("reader-scope: a '*' token still asks a store-wide question",
              (st, doc.get("outcome")), (200, "ok"))
        check_true("reader-scope: ...and still sees every account",
                   outside <= _uuids_in(doc))
    finally:
        httpd.shutdown()
        httpd.server_close()
        serve.ShipHandler.door = None
        serve.ShipHandler.api = None


def test_api_meta_accounts_is_scoped_to_the_token_and_says_so():
    """`meta` enumerated the whole store on every payload, whatever the scope.

    `_meta` attached `accounts` -- identity, email address,
    `organization_uuid`, `account_uuid` -- for every account in the snapshot,
    built from the store and never from the caller.  Measured:
    `/api/v1/search?limit=100` with a token scoped to alpha returned alpha's
    rows and a `meta.accounts` naming beta, `beta@example.com` and both of its
    UUIDs, with `meta.root` -- the store's absolute path -- beside it.

    IT IS NOT FIXED BY FIXING THE ROUTE GATE.  It is attached AFTER a route has
    correctly answered about the account it was allowed to answer about, so it
    is the one leak that survives every per-route repair.

    Narrowed WITH A NOTE rather than silently: an unqualified short list is a
    second wrong answer, because "this store holds one account" is a claim and
    a scoped reader cannot tell it from the truth.
    """
    root, store_, a = ui_store()
    alpha, beta, agent = (fx.uuid_of("alpha"), fx.uuid_of("beta"),
                          fx.uuid_of("agent"))

    # In-process, because `handle`'s `scope` argument IS the contract the door
    # fills in -- and asserting it here means any future transport inherits it.
    st, doc = a.handle(V1 + "windows", {"account": alpha},
                       {"account": [alpha]}, now=fx.NOW_JOINT, scope=[alpha])
    check("reader-scope/meta: a permitted question still answers",
          (st, doc.get("outcome")), (200, "ok"))
    named = {m["account_uuid"] for m in doc["meta"]["accounts"]}
    check("reader-scope/meta: meta names only the covered account",
          sorted(named), [alpha])
    check("reader-scope/meta: and no identity outside the scope survives "
          "anywhere in the payload",
          sorted(_uuids_in(doc) & {beta, agent}), [])
    check("reader-scope/meta: nor an address",
          "beta@example.com" in json.dumps(doc, default=str), False)
    check("reader-scope/meta: the store's path is not published to a scoped "
          "reader", doc["meta"]["root"], None)
    check_true("reader-scope/meta: the short list SAYS it is short",
               "not every account in the store"
               in doc["meta"]["accounts_are"])
    check("reader-scope/meta: and names the scope it was narrowed to",
          doc["meta"]["scope"], [alpha])

    # THE CASE THE DOOR CANNOT SEE, and the reason `_scope_refusal` carries
    # its own `account-not-permitted` branch rather than leaving it to
    # `ShipHandler._reader_auth`.  Asked in process, with no handler in front
    # of it: the rule has to hold for a caller that reaches `handle` directly,
    # which is how this suite asks it and is the shape a second transport
    # would take.
    st, doc = a.handle(V1 + "windows", {"account": beta}, {"account": [beta]},
                       now=fx.NOW_JOINT, scope=[alpha])
    check("reader-scope/meta: an out-of-scope account is refused with no "
          "door in front of it",
          (st, doc["refusal"]["reason"]), (403, "account-not-permitted"))
    check("reader-scope/meta: ...and carries no result key",
          "result" in doc, False)
    check("reader-scope/meta: ...and does not name the account's identity, "
          "only the uuid the caller itself sent",
          "beta@example.com" in json.dumps(doc, default=str), False)

    # The ordinary `"*"` token -- `scope=None` -- is byte-for-byte unchanged.
    st, wide = a.handle(V1 + "windows", {"account": alpha},
                        {"account": [alpha]}, now=fx.NOW_JOINT)
    check("reader-scope/meta: an unscoped read still names every account",
          sorted(m["account_uuid"] for m in wide["meta"]["accounts"]),
          sorted([alpha, beta, agent]))
    check("reader-scope/meta: ...and still carries the store root",
          wide["meta"]["root"], a.view.root)
    check("reader-scope/meta: ...and gains no note about a scope",
          [k for k in ("accounts_are", "scope") if k in wide["meta"]], [])


def test_store_names_its_spill_directory_rather_than_inheriting_the_cwd():
    """DuckDB's `temp_directory` default is `.tmp`, relative to the CWD.

    Measured inside the api container: `Containerfile.linux.api` set no
    `WORKDIR`, so cwd is `/`, the image runs as 65534 against a root-owned
    `/`, and the store mount is `:ro`.  Forcing a spill there gives

        IO Error: Failed to create directory ".tmp": Permission denied

    which reaches the caller as `reader-failed` with `detail` reduced to the
    exception TYPE -- deliberately, since the text goes to stderr -- so the
    operator sees a fault with no cause, and the cause is a directory name.
    Every measurement this design rests on points at the query that triggers
    it: 45.6 GiB, 62 M rows, `GROUP BY` over the whole corpus.

    A relative path is the bug, so the property asserted is absoluteness, not
    a particular directory.
    """
    root = door_root()
    st = duckstore.DuckStore(root)
    check_true("store/spill: the temp directory is an absolute path (%s)"
               % st.temp_dir, os.path.isabs(st.temp_dir))
    check("store/spill: and is never DuckDB's cwd-relative default",
          st.temp_dir.endswith(os.sep + ".tmp"), False)

    named = os.path.join(door_root(), "spill-here")
    st2 = duckstore.DuckStore(root, temp_dir=named)
    check("store/spill: an operator's path is used as given",
          st2.temp_dir, named)
    check("store/spill: ...and the environment names the same knob",
          duckstore.TEMP_DIR_ENV, "CLAUDIO_DUCKDB_TEMP_DIR")

    # A REFUSAL AT BOOT, not a 500 on the first large question. It writes and
    # unlinks one byte, because `os.access` answers about the permission bits
    # and not about a read-only mount, a full filesystem, or a path that is
    # really a file.
    check("store/spill: a writable directory is not a problem",
          st2.temp_dir_problem(), None)
    check_true("store/spill: ...and the probe left nothing behind",
               os.listdir(named) == [])

    blocked = os.path.join(door_root(), "blocked")
    with open(blocked, "w", encoding="utf-8") as fh:
        fh.write("not a directory\n")
    why = duckstore.DuckStore(root, temp_dir=blocked).temp_dir_problem()
    check_true("store/spill: an unusable directory is NAMED, never silent",
               why is not None and blocked in why)
    check_true("store/spill: ...and the message names the variable that "
               "fixes it", duckstore.TEMP_DIR_ENV in (why or ""))

    if not have_duck():
        _skip("store/spill: the setting reaches the connection "
              "(duckdb not installed)")
        return
    # And it is really applied, on the base connection AND on the per-thread
    # cursor every query actually runs on.
    st3 = duckstore.DuckStore(root, temp_dir=named, memory_limit="512MB")
    got = st3.con().execute("SELECT current_setting('temp_directory')"
                            ).fetchone()[0]
    check("store/spill: the cursor a query runs on sees the setting",
          got, named)
    box = {}

    def ask():
        box["v"] = st3.con().execute(
            "SELECT current_setting('temp_directory')").fetchone()[0]

    t = threading.Thread(target=ask)
    t.start()
    t.join()
    check("store/spill: ...and so does a second thread's own cursor",
          box.get("v"), named)


# The write primitives, named once.  A grep for these over `srv/store.py` was
# run by hand while the split was built and reported "no `open(`, no `write(`,
# no `os.remove/rename/mkdir/makedirs/unlink/replace`, no `shutil`, no `flock`"
# -- a true statement about that revision, and a claim with nothing keeping it
# true, which is exactly the shape this repository keeps writing tests against.
STORE_WRITE_PRIMITIVES = (
    "open(", "os.remove", "os.rename", "os.replace", "os.unlink", "os.mkdir",
    "os.makedirs", "os.rmdir", "os.truncate", "shutil.", "fcntl.",
    "os.O_CREAT", "os.O_WRONLY", "os.O_APPEND",
)


def test_the_reader_writes_nothing_into_the_store():
    """`srv/store.py` is the READ half of the split, and the mount is `:ro`.

    Proven from inside the running api container -- `EROFS` on an append, on a
    `door.lock` flock and on a plain file -- but that proves the MOUNT, and a
    mount is an operator's configuration.  This asserts the property of the
    SOURCE, so that an api handed a writable store by mistake still writes
    nothing to it.

    THE ONE EXCEPTION IS NAMED RATHER THAN EXCLUDED FROM THE SCAN.
    `temp_dir_problem` writes and unlinks one byte, and it does it in the
    DuckDB SPILL directory, which is not the store and is never under it.  A
    guard that simply allowed `open(` anywhere in this file would be satisfied
    by a write into `accounts/`; one that forbade it outright would have to be
    deleted the first time a probe was needed, and a deleted guard asserts
    nothing.  So the primitives are confined BY LOCATION and the path the
    exception may touch is asserted separately.
    """
    import ast as _ast
    path = os.path.join(fx.SRV_DIR, "srv", "store.py")
    src = open(path, encoding="utf-8").read()
    lines = src.splitlines(keepends=True)
    span = None
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.FunctionDef) and node.name == "temp_dir_problem":
            span = (node.lineno, node.end_lineno)
    check_true("store/readonly: the spill probe is findable, so the "
               "exception is a real span and not a guess", span is not None)
    for i in range(span[0] - 1, span[1]):
        lines[i] = "\n"
    tmp = os.path.join(door_root(), "store-without-the-probe.py")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write("".join(lines))
    # Comments and docstrings name these primitives on purpose -- this test's
    # own prose does -- so the scan is of CODE.  Greping the raw file is how
    # the counters test one directory over came to assert a comment.
    code = code_without_prose(tmp)
    found = sorted({p for p in STORE_WRITE_PRIMITIVES if p in code})
    check("store/readonly: no write primitive outside the spill probe",
          found, [])
    probe = "".join(src.splitlines(keepends=True)[span[0] - 1:span[1]])
    check_true("store/readonly: ...and the probe touches only self.temp_dir",
               "self.temp_dir" in probe and "self.root" not in probe
               and "self.paths" not in probe)
    check("store/readonly: the engine opens no database file either",
          "duckdb.connect()" in code, True)

    # BEHAVIOURAL, because a scan is a statement about spelling.  Ask every
    # shape of question and assert the store's bytes and timestamps are
    # untouched -- directory mtimes included, so a file created and removed
    # inside the store would still be caught.
    if not have_duck():
        _skip("store/readonly: the behavioural half (duckdb not installed)")
        return
    root, _store, a = ui_store()
    alpha = fx.uuid_of("alpha")

    def snapshot():
        out = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            st = os.stat(dirpath)
            out[dirpath] = (st.st_mtime_ns, sorted(dirnames), sorted(filenames))
            for fn in sorted(filenames):
                p = os.path.join(dirpath, fn)
                fs = os.stat(p)
                out[p] = (fs.st_size, fs.st_mtime_ns)
        return out

    before = snapshot()
    for route, qs in (("search", "account=" + alpha),
                      ("aggregate", "account=%s&by=model" % alpha),
                      ("histogram", "account=%s&interval=3600" % alpha),
                      ("values", "account=%s&fields=model" % alpha),
                      ("fields", "account=" + alpha),
                      ("lookup", "account=%s&field=session_id&value=x" % alpha),
                      ("diagnostics", "account=" + alpha),
                      ("windows", "account=" + alpha),
                      ("accounts", "")):
        ui_call(a, V1 + route, qs)
    check("store/readonly: no query changed a byte or a timestamp in the store",
          snapshot(), before)


def test_api_reader_auth_is_inert_until_configured_and_never_echoes_a_token():
    """Absent `readers.json` leaves the API open; a malformed one is refused by
    POSITION, never by printing the secret it could not parse.

    `Tenants` has had that property from the start -- a refusal names entry N
    and the tenant it claimed, so a typo is fixable without the file's secrets
    landing in a terminal someone screenshots. The read side is the same file
    shape with the same hazard.
    """
    r = serve.Readers()
    check("reader-auth: no file means not configured, so the gate is inert",
          (r.configured, r.scope("anything")), (False, None))

    r = serve.Readers(mapping={"s3cret-token": "not-a-scope"})
    check("reader-auth: an unusable scope is refused", len(r.refused), 1)
    check("reader-auth: the refusal names the position, not the token",
          r.refused[0][0], "entry 1")
    blob = repr(r.refused)
    check("reader-auth: the token value never appears in the refusal",
          "s3cret-token" in blob, False)

    good = fx.uuid_of("alpha")
    r = serve.Readers(mapping={"t": [good], "u": serve.ALL_ACCOUNTS})
    check("reader-auth: a scoped token permits its own account",
          r.permits(r.scope("t"), good), True)
    check("reader-auth: and refuses another",
          r.permits(r.scope("t"), fx.uuid_of("beta")), False)
    check("reader-auth: '*' permits anything",
          r.permits(r.scope("u"), fx.uuid_of("beta")), True)


def test_api_an_exposed_door_refuses_to_serve_an_unauthenticated_read_api():
    """Binding off loopback with no reader auth is a hard refusal, not a warning.

    The read API returns every record in the store, email addresses included.
    On loopback that is a development convenience; on any other address it is
    a breach, and the two decisions -- how exposed, and who may read -- are
    made together or the exposure wins by default. `_is_loopback` compares text
    rather than resolving, because a name that resolves to 127.0.0.1 today can
    resolve elsewhere tomorrow and this is the check that decides.
    """
    for host, loop in (("127.0.0.1", True), ("localhost", True),
                       ("127.0.1.1", True), ("::1", True),
                       ("0.0.0.0", False), ("192.168.1.10", False),
                       ("example.internal", False), ("", False)):
        check("reader-auth: %r loopback is %s" % (host, loop),
              serve._is_loopback(host), loop)

    root = tempfile.mkdtemp(prefix="omini-expose-")
    tok = os.path.join(root, "tokens.json")
    with open(tok, "w", encoding="utf-8") as fh:
        json.dump({"t": fx.uuid_of("alpha")}, fh)
    # `ready` is the escape hatch, and it is load-bearing HERE rather than a
    # convenience: if the refusal below is ever removed, `serve()` binds and
    # runs forever, so without this the mutation that proves this test guards
    # anything would HANG the suite instead of failing it. A test that hangs
    # under mutation is a test nobody can use to check its own control.
    err = io.StringIO()
    old = sys.stderr
    sys.stderr = err
    stop = threading.Event()

    def _ready(bound):
        stop.set()
        threading.Thread(target=bound.shutdown, daemon=True).start()

    try:
        rc = serve.serve("0.0.0.0", 0, root, tok, quiet=True, ready=_ready,
                         readers_path=os.path.join(root, "readers.json"))
    finally:
        sys.stderr = old
    check("reader-auth: the refusal happens BEFORE anything binds",
          stop.is_set(), False)
    check("reader-auth: an exposed door with no readers file refuses to start",
          rc, 4)
    text = err.getvalue()
    check("reader-auth: and says what it would have exposed",
          "email addresses" in text, True)
    check("reader-auth: and names both remedies",
          ("readers.json" in text and "--no-api" in text), True)


# ---------------------------------------------------------------------------
# the split: the writer's lock, the reader's absence of one, and the flag pair
# ---------------------------------------------------------------------------


def _split_root(with_accounts=True):
    """A store root, optionally with the `accounts/` a writer would create."""
    root = tempfile.mkdtemp(prefix="omini-split-")
    if with_accounts:
        os.makedirs(os.path.join(root, "accounts"), mode=0o700)
        os.makedirs(os.path.join(root, "offsets"), mode=0o700)
    with open(os.path.join(root, "tokens.json"), "w", encoding="utf-8") as fh:
        json.dump({"t": fx.uuid_of("alpha")}, fh)
    with open(os.path.join(root, "readers.json"), "w", encoding="utf-8") as fh:
        json.dump({"r": serve.ALL_ACCOUNTS}, fh)
    return root


@contextlib.contextmanager
def _running_door(root, **kw):
    """`serve()` in a thread, on a real port, shut down cleanly afterwards.

    The `ready` callback is handed the bound PORT and not the server, so there
    is nothing in it to call `shutdown()` on.  The server object is captured by
    wrapping the class `serve()` constructs -- restored in the `finally`,
    because a suite that left a subclass installed would be testing its own
    wrapper from then on.
    """
    box, out = {}, {}
    started = threading.Event()
    real = serve.ThreadingHTTPServer

    class Captured(real):
        def __init__(self, *a, **k):
            real.__init__(self, *a, **k)
            box["httpd"] = self

    serve.ThreadingHTTPServer = Captured

    def _ready(port):
        box["port"] = port
        started.set()

    def _run():
        try:
            out["rc"] = serve.serve(
                "127.0.0.1", 0, root,
                os.path.join(root, "tokens.json"), quiet=True, ready=_ready,
                readers_path=os.path.join(root, "readers.json"), **kw)
        finally:
            started.set()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    started.wait(20)
    try:
        yield box, out
    finally:
        if "httpd" in box:
            box["httpd"].shutdown()
        thread.join(20)
        serve.ThreadingHTTPServer = real


def _serve_expecting_refusal(label, root, **kw):
    """`serve()` that is expected to REFUSE, run so that a bind FAILS BY NAME.

    The obvious shape -- call `serve()` on the main thread and assert the exit
    code -- is a test that cannot check its own control.  If the refusal it
    pins is ever removed, `serve()` does not return a different number: it
    binds a port and calls `serve_forever`, and the suite HANGS.  A hang is not
    a failing test; nobody can tell it from a slow machine, CI kills the job
    with no finding attached, and the one line it was guarding is the one line
    in the split that can lose data.  Proven: reinstating the defect this was
    written for (`if False:` where `if ship_enabled:` belongs, which is exactly
    how the lock arrived in this tree) hung the run instead of failing it.

    So the call goes on a daemon thread, the server class is wrapped to capture
    the instance the way `_running_door` does, and a bind is shut down and
    reported as the named failure it is.  Returns `(rc, stderr text)`.
    """
    box, out = {}, {}
    done = threading.Event()
    real = serve.ThreadingHTTPServer

    class Captured(real):
        def __init__(self, *a, **k):
            real.__init__(self, *a, **k)
            box["httpd"] = self

    err = io.StringIO()

    def _run():
        try:
            out["rc"] = serve.serve(
                "127.0.0.1", 0, root, os.path.join(root, "tokens.json"),
                quiet=True, ready=lambda p: box.setdefault("port", p),
                readers_path=os.path.join(root, "readers.json"), **kw)
        except Exception as exc:                     # pragma: no cover
            out["exc"] = exc
        finally:
            done.set()

    old = sys.stderr
    sys.stderr = err
    serve.ThreadingHTTPServer = Captured
    try:
        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        done.wait(20)
        if not done.is_set():
            # It bound. Take it down before saying so, or every later test on
            # this root meets a live server holding the lock.
            if "httpd" in box:
                box["httpd"].shutdown()
            done.wait(20)
            thread.join(20)
            fail("%s: serve() BOUND A PORT where a refusal was required "
                 "(port %s) -- the refusal is gone, not merely renumbered"
                 % (label, box.get("port")))
            return None, err.getvalue()
        thread.join(20)
    finally:
        sys.stderr = old
        serve.ThreadingHTTPServer = real
    if "exc" in out:
        fail("%s: serve() raised %r instead of refusing"
             % (label, out["exc"]))
        return None, err.getvalue()
    check_true("%s: and it never bound a port" % label, "httpd" not in box)
    return out.get("rc"), err.getvalue()


def test_the_store_lock_is_the_writers_and_a_reader_takes_none():
    """THE ONE EDIT IN THE SPLIT THAT COULD LOSE DATA, ASSERTED FROM BOTH ENDS.

    `lock_root` ran unconditionally in `serve()` and refused a second process
    of ANY kind on one root.  Its docstring says why, and the reason is
    narrower than the lock was: two processes on one root would not corrupt the
    record files -- every append is `O_APPEND` -- but they would interleave
    read-modify-write on the OFFSETS, and an offset is what the ack promises.
    Offsets are written in exactly one place, `Store.accept`, on the ship path.
    So it is the WRITER's lock, and the reader-only api service takes none.

    TWO WRITERS MUST STILL BE REFUSED, and that is the first half here --
    exit 3, the same code and the same message as before, over three shapes:
    two ship-enabled processes, and a whole door beside an ingest-only one.

    THE SECOND HALF IS THE ONE A GREP CANNOT DO.  "The reader takes no lock" is
    not "the reader started while a lock was held" -- it could have taken a
    second lock on a different path, or taken and released one, and both would
    pass a naive test.  `lock_root` is replaced by a recorder for the duration,
    so what is asserted is the CALL: the writer makes exactly one and the
    reader makes none at all.
    """
    root = _split_root()

    # -- 1. two writers, refused --------------------------------------------
    fd = serve.lock_root(root)
    check_true("split-lock: the first writer takes the lock", fd is not None)
    check("split-lock: a second lock_root on the same root is refused",
          serve.lock_root(root), None)

    # Ship-enabled with the lock already held, both shapes. `_serve_expecting_
    # refusal` is what makes these assertions checkable: a serve() that binds
    # instead of refusing is reported by name rather than hanging the suite.
    rc_api_off, err_api_off = _serve_expecting_refusal(
        "split-lock/ingest-only", root, api_enabled=False)
    rc_whole, err_whole = _serve_expecting_refusal(
        "split-lock/whole-door", root)
    check("split-lock: an ingest-only door on a locked root exits 3",
          rc_api_off, 3)
    check("split-lock: a whole door on a locked root exits 3 too", rc_whole, 3)
    check_true("split-lock: and the refusal still names door.lock",
               "door.lock" in err_api_off and "door.lock" in err_whole)

    # -- 2. the reader, while that lock is still held ------------------------
    calls = []
    real_lock = serve.lock_root

    def _recorded(r):
        calls.append(r)
        return real_lock(r)

    serve.lock_root = _recorded
    try:
        with _running_door(root, ship_enabled=True, api_enabled=False) as (b, o):
            pass
    finally:
        serve.lock_root = real_lock
    # The writer above could not start (the lock is still held by `fd`), which
    # is fine: what is being counted is the ATTEMPT.
    check("split-lock: a ship-enabled door asks for the lock exactly once",
          len(calls), 1)

    calls = []
    serve.lock_root = _recorded
    try:
        with _running_door(root, ship_enabled=False) as (box, out):
            check_true("split-lock: the reader BOUND a port while the writer's "
                       "lock is held", isinstance(box.get("port"), int))
            # Guarded, because the failure this test exists to catch is a
            # reader that DOES take the lock -- and such a reader never binds,
            # so an unguarded `box["port"]` turns the finding into a KeyError
            # traceback. A raise is not a worse failure than the assertion; it
            # is a worse REPORT of the same one, and it buries the named line
            # directly above it. Proven by the `split-lock-taken-by-reader`
            # row of the mutation matrix, which printed exactly that KeyError.
            if isinstance(box.get("port"), int):
                req = urllib.request.Request(
                    "http://127.0.0.1:%d/healthz" % box["port"])
                with urllib.request.urlopen(req, timeout=10) as fh:
                    health = json.loads(fh.read())
                check("split-lock: ...and it says which half it is",
                      health.get("role"), "reader")
    finally:
        serve.lock_root = real_lock
    check("split-lock: a reader NEVER calls lock_root", calls, [])
    check("split-lock: and the reader exited 0, not 3", out.get("rc"), 0)

    os.close(fd)
    shutil.rmtree(root, ignore_errors=True)


def test_a_reader_creates_nothing_and_refuses_a_store_it_cannot_see():
    """`Store(create=False)` and exit 9, which are two halves of one fact.

    A reader is handed the store volume READ ONLY.  `Store.__init__` calls
    `os.makedirs(..., exist_ok=True)` twice and `lock_root` calls it a third
    time; on a read-only mount whose directories are absent that is EROFS out
    of the top of `serve.py`, aborting a process that was never going to write
    a byte, with a permission error an operator reads as a broken mount rather
    than as the correct configuration it is.

    The other half is the damage the split CREATES.  `accounts_in` swallows
    `OSError` and returns `[]`, and `Paths.accounts` does the same -- both
    deliberately, on the write path, where an account directory that does not
    exist yet is not an error.  On the READ path with a mistyped `--root`, or a
    bind mount that did not land, those two zeros compose into a server that
    answers `no-data` about every account in a store it is not looking at.
    That is byte-indistinguishable from a store nobody has shipped to, which is
    this project's cardinal sin.  So it is refused by name, with the three
    things it can mean.
    """
    # -- create=False writes nothing ----------------------------------------
    bare = tempfile.mkdtemp(prefix="omini-nocreate-")
    st = serve.Store(bare, create=False)
    check("split-create: Store(create=False) creates no directories",
          sorted(os.listdir(bare)), [])
    check("split-create: ...and still answers the questions health asks",
          (st.root, st.reserve == serve.DISK_RESERVE,
           isinstance(st.free_bytes(), int)),
          (os.path.abspath(bare), True, True))
    st2 = serve.Store(bare)
    check("split-create: the default still creates them, for the writer",
          sorted(os.listdir(bare)), ["accounts", "offsets"])
    check_true("split-create: ...and it is the same class either way",
               type(st) is type(st2))
    shutil.rmtree(bare, ignore_errors=True)

    # -- exit 9 --------------------------------------------------------------
    empty = _split_root(with_accounts=False)
    # Bounded, so that REMOVING this refusal fails by name instead of hanging
    # the suite on a `serve_forever` -- see `_serve_expecting_refusal`.
    rc, text = _serve_expecting_refusal("split-root", empty, ship_enabled=False)
    check("split-root: a reader over a store with no accounts/ exits 9", rc, 9)
    check_true("split-root: ...naming the root it was given",
               os.path.abspath(empty) in text)
    for meaning in ("--root", "bind mount", "never run"):
        check_true("split-root: ...and what it can mean (%s)" % meaning,
                   meaning in text)
    check_true("split-root: ...and why silence would be worse",
               "no-data" in text)
    # A WRITER over the same root does NOT refuse: it creates them, which is
    # exactly the asymmetry the refusal rests on.  Without this the test would
    # pass over a `serve()` that refused every empty root.
    fd = serve.lock_root(empty)
    check_true("split-root: a writer creates accounts/ rather than refusing",
               fd is not None)
    serve.Store(empty)
    check_true("split-root: ...and now the reader's precondition holds",
               os.path.isdir(os.path.join(empty, "accounts")))
    if fd is not None:
        os.close(fd)
    shutil.rmtree(empty, ignore_errors=True)


def test_the_two_flags_name_two_services_and_both_together_are_refused():
    """`--no-api` and `--no-ship`, and the empty intersection between them.

    One process, two halves.  Each flag switches one off and names the service
    the other half is; both on is the whole door and is still the default, so
    nothing that ran before the split runs differently.

    BOTH OFF IS REFUSED BY NAME (exit 8) RATHER THAN STARTED.  A process with
    both halves off comes up, binds a port and answers `/healthz` -- so
    everything that pings it reports a healthy service, while it serves neither
    shipments nor reads.  That is a plausible zero with a port number on it.
    """
    root = _split_root()
    rc, text = _serve_expecting_refusal("split-flags", root,
                                        api_enabled=False, ship_enabled=False)
    check("split-flags: both flags together is exit 8", rc, 8)
    for named in ("--no-api", "--no-ship", "/healthz"):
        check_true("split-flags: ...and the refusal names %s" % named,
                   named in text)
    check("split-flags: ...and no lock was left behind",
          os.path.exists(os.path.join(root, "door.lock")), False)

    # THE FLAGS ARE WIRED THROUGH `main`, which is what an image actually
    # invokes.  A `serve()` that took the keyword while argparse never passed
    # it would leave every assertion above true and every container wrong.
    parsed = {}
    real = serve.serve

    def _spy(*a, **kw):
        parsed.clear()
        parsed.update(kw)
        return 0

    serve.serve = _spy
    try:
        serve.main(["--root", root])
        check("split-flags: no flag is the whole door",
              (parsed.get("api_enabled"), parsed.get("ship_enabled")),
              (True, True))
        serve.main(["--root", root, "--no-api"])
        check("split-flags: --no-api is the ingest service",
              (parsed.get("api_enabled"), parsed.get("ship_enabled")),
              (False, True))
        serve.main(["--root", root, "--no-ship"])
        check("split-flags: --no-ship is the api service",
              (parsed.get("api_enabled"), parsed.get("ship_enabled")),
              (True, False))
        serve.main(["--root", root, "--no-ui"])
        check("split-flags: --no-ui is still the old spelling of --no-api",
              (parsed.get("api_enabled"), parsed.get("ship_enabled")),
              (False, True))
    finally:
        serve.serve = real
    shutil.rmtree(root, ignore_errors=True)


def test_a_reader_refuses_a_shipment_by_name_and_never_acknowledges_one():
    """503 `no-ship`, and it is neither a 404 nor an ack.

    A 404 would tell a shipper this URL does not exist, and a shipper that
    concludes that stops trying -- it exists, and this process is not the one
    that serves it.  An ack would be worse: the client advances its offset on a
    200 and those bytes are then gone from its point of view, while nothing
    was written.  The offset is untouched either way, so the client still holds
    every byte and resends the identical range to the process that does.

    AND THE READER'S `/healthz` SAYS ITS COUNTERS ARE STRUCTURALLY ZERO.
    `records_written: 0` is true of a reader and says nothing at all about the
    store, so a monitor reading the same key set off both halves of a split
    door would conclude a healthy stack had ingested nothing.  Every existing
    key keeps its meaning; two are added.
    """
    root = _split_root()
    with _running_door(root, ship_enabled=False) as (box, _out):
        port = box["port"]

        def post(path, body=b"{}\n", token="t"):
            req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                         data=body, method="POST")
            req.add_header("Authorization", "Bearer " + token)
            req.add_header("Content-Type", serve.CONTENT_TYPE)
            try:
                with urllib.request.urlopen(req, timeout=10) as fh:
                    return fh.status, json.loads(fh.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        status, doc = post(serve.PATH_SHIP)
        check("split-noship: a shipment to a reader is 503 no-ship",
              (status, doc.get("reason")), (503, "no-ship"))
        check("split-noship: ...and it is NOT an acknowledgement",
              doc.get("ok"), False)
        check("split-noship: ...and carries no offset to advance to",
              "offset" in doc, False)
        check_true("split-noship: ...and says where to ship instead",
                   "read-write" in (doc.get("detail") or ""))

        # A DIFFERENT PATH IS STILL A 404, so the refusal above is about the
        # ROUTE existing here rather than about POST being refused wholesale.
        status, doc = post("/v1/nonsense")
        check("split-noship: an unknown POST path is still 404 not-found",
              (status, doc.get("reason")), (404, "not-found"))

        with urllib.request.urlopen(
                "http://127.0.0.1:%d/healthz" % port, timeout=10) as fh:
            health = json.loads(fh.read())
        check("split-health: the reader names its role", health.get("role"),
              "reader")
        check_true("split-health: ...and says its write counters mean nothing",
                   "structurally zero" in (health.get("counters_are") or ""))
        check("split-health: ...and every existing key is still there",
              [k for k in ("root", "disk_free_bytes", "disk_reserve_bytes",
                           "tenants_configured", "tokens_refused", "engine")
               if k not in health], [])

    with _running_door(root, api_enabled=False) as (box, _out):
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/healthz" % box["port"], timeout=10) as fh:
            health = json.loads(fh.read())
        check("split-health: the writer names its role", health.get("role"),
              "writer")
        # AND CARRIES NO SUCH CAVEAT, which is the half that makes the caveat
        # readable: a sentence on every payload is a sentence nobody reads.
        check("split-health: ...and has no counters_are, because its counters "
              "mean what they say", "counters_are" in health, False)
    shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# the OCI images: what is in them, and what they must never quietly do
# ---------------------------------------------------------------------------

# These are static guards over `server/oci/`, and they are resolved from `HERE`
# -- this file's own directory -- rather than from `fx.SRV_DIR`.  That is not a
# stylistic choice.  `mutate.py` copies `srv/` and the README into a temporary
# tree and points `SRV_DIR` at it; a test that resolved these files through
# `SRV_DIR` would raise `FileNotFoundError` in EVERY mutated run, an exception
# is counted as a failure exactly as an assertion is, and every row of the
# matrix would read `caught` whether or not anything asserted the behaviour.
# That is precisely the defect this harness was found to have once already, and
# it hid three real survivors.  Resolved from `HERE` these read the real files
# in a mutated run, pass, and contribute nothing false to any row.
#
# Each one fails BY NAME when its input is absent, for the same reason.

OCI = os.path.join(os.path.dirname(HERE), "oci")
REPO = os.path.dirname(os.path.dirname(HERE))


def _oci_containerfiles():
    """Every `Containerfile.*` in `server/oci/`, as (name, source)."""
    if not os.path.isdir(OCI):
        return []
    out = []
    for fn in sorted(os.listdir(OCI)):
        # `Containerfile.linux.dockerignore` starts with "Containerfile" and is
        # not one.  Excluded by suffix rather than by counting dots, because
        # `.containerignore` is the same file under buildah's name for it and a
        # dot-counting rule would let that one through.
        if fn.endswith((".dockerignore", ".containerignore")):
            continue
        if fn.startswith("Containerfile"):
            out.append((fn, open(os.path.join(OCI, fn),
                                 encoding="utf-8").read()))
    return out


def _sh_code(text):
    """A shell file with its full-line comments removed.

    The same trap `code_without_prose` exists for one language over: a static
    "the entrypoint never says `--no-api`" check that greps the raw file
    matches the paragraph explaining why it must never say it.  Only full-line
    comments are stripped and that is stated rather than glossed -- a trailing
    `#` inside a line would survive -- because stripping a `#` that sits inside
    a quoted string is exactly the wrong kind of clever for a guard.
    """
    return "\n".join(l for l in text.splitlines()
                      if not l.lstrip().startswith("#"))


def _sysexit(fn, args):
    """`None` if `fn(args)` returned, otherwise the `SystemExit`'s message.

    These commands refuse with `sys.exit("...")`, so "did it refuse, and did
    it say why" is one question about one object.
    """
    try:
        fn(args)
        return None
    except SystemExit as exc:
        return exc.code if exc.code is not None else "exit()"


# A synthetic RSA-1024 key, generated here rather than committed.
#
# What is under test is the VERIFIER, and a key this test owns the private
# half of exercises two branches the real FreeBSD key cannot reach at all: a
# signature over the single hash rather than pkg's double one, and a forged
# PKCS#1 block with short padding.  Deterministically seeded, so a failure is
# reproducible; 1024 bits because the block only has to be wide enough for a
# SHA-256 DigestInfo plus eight octets of padding, and a bigger modulus buys
# the test nothing but seconds.
_RSA_CACHE = {}


def _rsa_key_1024():
    if _RSA_CACHE:
        return _RSA_CACHE
    import random
    rnd = random.Random(20260821)

    def probably_prime(n):
        if n < 2:
            return False
        for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
            if n % p == 0:
                return n == p
        d, r = n - 1, 0
        while d % 2 == 0:
            d //= 2
            r += 1
        for a in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
            x = pow(a, d, n)
            if x in (1, n - 1):
                continue
            for _ in range(r - 1):
                x = x * x % n
                if x == n - 1:
                    break
            else:
                return False
        return True

    def prime():
        while True:
            c = rnd.getrandbits(512) | (1 << 511) | 1
            if probably_prime(c):
                return c

    e = 65537
    while True:
        p, q = prime(), prime()
        if p == q:
            continue
        phi = (p - 1) * (q - 1)
        # `e` must be coprime to phi, i.e. phi % e MUST NOT be zero.  Written
        # the other way round first, which loops for ever rather than failing.
        if phi % e == 0:
            continue
        _RSA_CACHE.update({"n": p * q, "e": e, "d": pow(e, -1, phi)})
        return _RSA_CACHE


def _spki_pem(n, e):
    """(n, e) as a PEM SubjectPublicKeyInfo, DER built by hand.

    Written out rather than taken from a library, because the whole point of
    `pkgindex.py`'s parser is that it needs no library.
    """
    import base64

    def tlv(tag, val):
        if len(val) < 0x80:
            return bytes([tag, len(val)]) + val
        ln = len(val).to_bytes((len(val).bit_length() + 7) // 8, "big")
        return bytes([tag, 0x80 | len(ln)]) + ln + val

    def integer(x):
        b = x.to_bytes((x.bit_length() + 8) // 8, "big")
        return tlv(0x02, b)

    rsa = tlv(0x30, integer(n) + integer(e))
    # rsaEncryption OID 1.2.840.113549.1.1.1, NULL parameters.
    algid = tlv(0x30, bytes.fromhex("06092a864886f70d010101") + tlv(0x05, b""))
    spki = tlv(0x30, algid + tlv(0x03, b"\x00" + rsa))
    b64 = base64.encodebytes(spki).decode("ascii").strip()
    return ("-----BEGIN PUBLIC KEY-----\n" + b64
            + "\n-----END PUBLIC KEY-----\n").encode("ascii")


def _rsa_sign_pkcs1_sha256(key, digest):
    """A correct EMSA-PKCS1-v1_5 SHA-256 signature over `digest`."""
    k = (key["n"].bit_length() + 7) // 8
    di = bytes.fromhex("3031300d060960864801650304020105000420") + digest
    em = b"\x00\x01" + b"\xff" * (k - 3 - len(di)) + b"\x00" + di
    return pow(int.from_bytes(em, "big"), key["d"], key["n"]).to_bytes(k, "big")


def test_oci_the_files_this_section_asserts_about_exist():
    """The named-failure half.  Without it every check below is vacuous."""
    check_true("oci: server/oci/ exists", os.path.isdir(OCI))
    for fn in ("entrypoint.sh", "preflight.py", "entrypoint-mcp.sh",
               "entrypoint-proxy.sh", "compose.yml"):
        check("oci: the shared %s was found" % fn,
              os.path.isfile(os.path.join(OCI, fn)) and fn,
              fn)
    # The proxy's configuration.  Two of the three are shared with the Linux
    # proxy image byte for byte -- the routes are one decision -- and the third
    # is the base-specific main configuration.
    for fn in ("claudio.conf", "claudio-locations.conf", "claudio-tls.conf",
               "nginx.freebsd.conf", "nginx.linux.conf"):
        path = os.path.join(OCI, "nginx", fn)
        check("oci: nginx/%s was found" % fn,
              os.path.isfile(path) and fn, fn)
    names = [n for n, _ in _oci_containerfiles()]
    check_true("oci: at least one Containerfile was found", bool(names))
    # ONE IMAGE PER COMPONENT PER BASE, and all eight are named here so that
    # every loop below has something to loop over.  A component whose
    # Containerfile went missing would otherwise make its guards vacuous rather
    # than red.
    for base in ("linux", "freebsd"):
        for comp in COMPONENTS:
            fn = "Containerfile.%s.%s" % (base, comp)
            check("oci: %s was found" % fn, fn in names and fn, fn)
    # AND NO BARE `Containerfile.<os>`.  That name meant "the door" -- one
    # process holding the write lock, the store read-write and the query engine
    # -- and the whole point of the split is that no such image exists.  Left
    # behind it would build and run and look right.
    for base in ("linux", "freebsd"):
        check("oci: there is no whole-door Containerfile.%s any more" % base,
              "Containerfile.%s" % base in names, False)


# The component a Containerfile builds, derived from its name rather than
# written down: `Containerfile.<os>.<x>` is component `<x>`.  Derived, because
# a hand-kept mapping is a second place to forget and the copy is always the
# one that goes stale -- and because it pairs `Containerfile.linux.mcp` with
# `Containerfile.freebsd.mcp` with no edit here.
#
# A BARE `Containerfile.<os>` USED TO MEAN "door", AND IT DELIBERATELY MEANS
# NOTHING NOW.  The door was one process holding the write lock, the store
# read-write and the query engine at once; it is two images.  Returning `None`
# rather than a default is what makes `_oci_components` refuse a file nobody
# has classified, instead of quietly filing it under the component whose
# guards are weakest.
def _component_of(name):
    parts = name.split(".")
    if len(parts) < 3:
        return None
    return parts[2]


# The two components that touch the store, and the two that must never.
# Written here once, so a third store-holding component has one place to be
# admitted rather than four loops to be added to.
STORE_COMPONENTS = ("ingest", "api")
COMPONENTS = ("ingest", "api", "mcp", "proxy")


def _os_of(name):
    parts = name.split(".")
    return parts[1] if len(parts) > 1 else None


def test_oci_no_containerfile_declares_a_healthcheck():
    """`HEALTHCHECK` is silently DROPPED under `--format oci`.

    The OCI image spec has no field for one, so buildah drops it and the
    published config would simply not contain it -- while a Docker-format build
    of the identical file keeps it.  That is how the omission goes unnoticed:
    it appears to work locally and vanishes on publish.  This project ships two
    images and one of them is built with buildah; a health check present in one
    and absent in the other is a health check nothing may rely on.
    """
    files = _oci_containerfiles()
    check_true("oci/health: there were Containerfiles to check", bool(files))
    for name, text in files:
        code = "\n".join(l for l in text.splitlines()
                          if not l.lstrip().startswith("#"))
        check("oci/health: %s declares no HEALTHCHECK" % name,
              "HEALTHCHECK" in code.upper(), False)
        # THE RUN-TIME EQUIVALENT, AND IT IS NOT ALWAYS `/healthz`.  The two
        # store halves serve that route and their comments quote it; the mcp
        # component has no unauthenticated route at all -- that is the point of
        # it -- and the proxy's healthy answer to `/` is a 404 from the
        # catch-all.  So what is asserted is that the file says HOW to check
        # this image at run time, not that every image has one route in common.
        check_true("oci/health: %s documents the run-time equivalent instead"
                   % name,
                   "--health-cmd" in text or "healthcheck:" in text)


def test_oci_the_proxy_notices_an_upstream_that_moved():
    """`claudio.conf` argues -- correctly -- that the upstream addresses must
    NOT be deferred into a variable with a `resolver`, because that turns
    nginx's loud startup refusal into a steady silent 502.  That argument is
    about STARTUP, and the other direction is the one that bites later.

    nginx resolves each upstream name once, at configuration load, so an
    address that moves afterwards is never noticed.  Reproduced on the running
    stack: `docker compose restart ingest api` swapped 172.28.0.2 and
    172.28.0.3, nginx kept the old pair, and both routes crossed -- and with
    `restart: unless-stopped` on every service, a crashed or recreated backend
    reaches that state with nobody watching.

    The watcher is in the ENTRYPOINT and not in the nginx configuration, which
    is the whole reason it is compatible with that argument: the addresses are
    still resolved once per configuration load, and this only decides WHEN a
    load happens.

    Controlled measurement, both directions, on the live stack:

        CLAUDIO_PROXY_WATCH=0   crossed at t+0, still crossed indefinitely
        watcher on              crossed at t+0, correct at t+18s (one tick)

    with the before and after address triples in the proxy's own log.
    """
    src = open(os.path.join(OCI, "entrypoint-proxy.sh"), encoding="utf-8").read()
    check_true("oci/proxy-watch: the entrypoint reloads nginx rather than "
               "restarting it", "-s reload" in src)
    check_true("oci/proxy-watch: ...on a timer of its own",
               "CLAUDIO_PROXY_WATCH_SECONDS" in src)
    check_true("oci/proxy-watch: ...and it can be turned off by name",
               "CLAUDIO_PROXY_WATCH" in src)

    # A RESOLUTION FAILURE IS NOT A MOVE, and this is the assertion that keeps
    # the watcher from making things worse: a backend that is briefly down
    # resolves to nothing, and reloading on that walks nginx into the startup
    # refusal the whole design relies on not happening unattended.
    check_true("oci/proxy-watch: a name that resolves to NOTHING stands the "
               "watcher down instead of reloading", '"=;"' in src)
    # A FAILED RELOAD IS NOT A SUCCESSFUL ONE. Recording the new addresses as
    # seen after a reload that failed leaves the proxy permanently crossed with
    # a log line saying it was fixed.
    lines = [ln.strip() for ln in src.splitlines()]
    marks = [i for i, ln in enumerate(lines) if ln == '_WATCH_SEEN="$_now"']
    check("oci/proxy-watch: the new addresses are recorded in exactly one "
          "place", len(marks), 1)
    check_true("oci/proxy-watch: ...and it is the SUCCESS arm of the reload, "
               "so a failed reload retries instead of claiming it fixed "
               "something",
               bool(marks) and lines[marks[0] - 1].startswith('if "$NGINX" -s reload'))
    # AND IT NEVER KILLS THE PROXY. A watcher that dies leaves the proxy
    # working exactly as it did before this existed, so its own failure must
    # not be fatal.
    check("oci/proxy-watch: the watcher never exits the container",
          [ln.strip() for ln in src.splitlines()
           if ln.strip().startswith("exit ") and "_WATCH" in ln], [])

    # THE COMPOSE FILE MUST BE ABLE TO REACH THE SWITCH. A documented kill
    # switch that is unset in the supported way of starting the stack is a
    # documented switch nobody can use.
    comp = open(os.path.join(OCI, "compose.yml"), encoding="utf-8").read()
    check_true("oci/proxy-watch: compose passes the switch through",
               "CLAUDIO_PROXY_WATCH:" in comp)


def test_oci_the_proxy_health_check_requires_an_answer():
    """A health check whose failure mode is a plausible pass is worse than none.

    The first draft was `... | grep -qE ' (502|504) ' && exit 1` -- unhealthy
    only on a bad gateway.  Proven inside the running proxy against a port
    nothing listens on: it reported HEALTHY.  And measured on the real stack
    with the mcp stopped, `GET /mcp` came back with an EMPTY status line rather
    than a 502, because nginx dropped the connection -- so the case that
    catches "nothing answered" is not a hypothetical arm, it is the arm the
    real failure takes.

    What it asserts is that a BACKEND answered, not that it answered 200:
    `/sender` is a 404 from the ingest door, `/api/v1/health` a 401 from the
    api and `/mcp` a 404 from the mcp, and all three are correct.
    """
    # SLICED AS TEXT, not parsed. PyYAML is not in the standard library and
    # nothing else in this suite needs it; making a guard depend on a
    # third-party module is how a guard becomes a skip on the machine that
    # most needs it.
    comp = open(os.path.join(OCI, "compose.yml"), encoding="utf-8").read()
    svc = comp.split("\n  proxy:\n", 1)
    check_true("oci/proxy-health: compose.yml declares a proxy service",
               len(svc) == 2)
    block = re.split(r"\n  [a-z]", svc[1])[0] if len(svc) == 2 else ""
    hc = block.split("    healthcheck:", 1)
    check_true("oci/proxy-health: ...with a health check on it", len(hc) == 2)
    cmd = re.split(r"\n    [a-z]", hc[1])[0] if len(hc) == 2 else ""
    # COMMENTS STRIPPED, AND THIS LINE IS THE FINDING. Written without it,
    # every assertion below was satisfied by the PARAGRAPH EXPLAINING the
    # setting rather than by the setting: deleting the `*)` arm and deleting
    # the `sleep 1` both left the suite green, because the comment above the
    # command names them both. That is the counters test one directory over,
    # which matched `host:\s*0\.0\.0\.0` in the comment saying why that
    # setting was load-bearing -- and it is the reason `code_without_prose`
    # exists for the Python half.
    cmd = "\n".join(ln for ln in cmd.splitlines()
                    if not ln.strip().startswith("#"))
    check_true("oci/proxy-health: it is a shell command",
               "CMD-SHELL" in cmd)
    # ONE ROUTE PER UPSTREAM, derived from the proxy's own location file rather
    # than listed here a second time: a route added there and not here would
    # make this check silently narrower than the proxy it guards.
    loc = open(os.path.join(OCI, "nginx", "claudio-locations.conf"),
               encoding="utf-8").read()
    proxied = sorted(set(re.findall(r"location\s+=?\s*(/\S*?)\s*\{[^}]*?"
                                    r"proxy_pass", loc, re.S)))
    check_true("oci/proxy-health: the locations file really names proxied "
               "routes (%r)" % (proxied,), len(proxied) >= 3)
    missing = [r for r in proxied
               if r.rstrip("/") and r.rstrip("/") not in cmd]
    check("oci/proxy-health: every proxied route is probed", missing, [])
    check_true("oci/proxy-health: a bad gateway is unhealthy",
               "50[24]" in cmd)
    # THE ARM THAT MATTERS. Without it an empty answer -- which is what a dead
    # upstream really produces -- reads as healthy.
    # THE WHOLE LINE, not a substring. `"*) exit 1" in cmd` is satisfied by
    # `HTTP/*50[24]*) exit 1 ;;` -- the 502 arm ends in exactly those
    # characters -- so deleting the catch-all left the assertion green while
    # restoring the defect. Measured by mutation, which is what a mutation
    # matrix is for.
    check_true("oci/proxy-health: and so is an answer that is not HTTP at all",
               any(ln.strip() == "*) exit 1 ;;" for ln in cmd.splitlines()))
    # busybox `nc` exits the moment its stdin reaches EOF, so a bare
    # `printf | nc` sends the request, closes, and reads nothing: measured as
    # three empty status lines against a perfectly healthy stack.
    check_true("oci/proxy-health: stdin is held open past the write, or "
               "busybox nc reads nothing", "sleep 1" in cmd)


def test_oci_the_entrypoint_never_papers_over_the_bind_refusal():
    """The door refuses an unauthenticated read API off loopback, exit 4.

    Inside a container the bind IS non-loopback -- the port mapping is what
    decides reachability, and binding loopback inside would make the port
    answer nothing -- so that refusal is the ordinary first experience of these
    images with no `readers.json` mounted.  An entrypoint that added
    `--no-api`, or fell back to `--host 127.0.0.1`, would turn a refusal that
    names its own remedy into either a mystery or a running door serving every
    record in the store to anyone who can reach it.

    THE ENTRYPOINT NOW SAYS `--no-api`, AND THE PROPERTY IS UNCHANGED.  It says
    it in exactly one place -- the `CLAUDIO_SERVER_ROLE` case -- for an image
    that DECLARED itself the ingest service.  "Quietly" was always the whole of
    the prohibition: a role an operator can read in `docker inspect`, change
    with `-e`, and see printed on every start is not a paper-over.  What would
    be is a fallback, so the `door` branch is asserted to add nothing and an
    unknown role is asserted to be refused rather than defaulted.

    RUN, NOT READ.  The role is turned into argv by a shell `case`, and the way
    to be wrong about it is a branch that falls through -- which a grep for the
    flag cannot see and a stub `preflight.py` can.
    """
    path = os.path.join(OCI, "entrypoint.sh")
    if not os.path.isfile(path):
        check("oci/refusal: the entrypoint was found", path, "a file that exists")
        return
    text = open(path, encoding="utf-8").read()
    code = _sh_code(text)
    check("oci/refusal: the entrypoint's CODE never says '127.0.0.1'",
          "127.0.0.1" in code, False)
    check("oci/refusal: ...and never says '--no-ui'", "--no-ui" in code, False)
    # The two role flags appear in the `case` and NOWHERE ELSE, which is what
    # stops one being appended unconditionally further down.
    for flag in ("--no-api", "--no-ship"):
        check("oci/refusal: %r appears exactly once in the code" % flag,
              code.count(flag), 1)
    check_true("oci/refusal: it does pass --readers through",
               "--readers" in code)
    check_true("oci/refusal: and it execs rather than forking a wrapper",
               "exec " in code)

    # -- what each role actually becomes ------------------------------------
    #
    # A stub `preflight.py` beside a copy of the entrypoint: the entrypoint
    # resolves an interpreter, execs `${HERE}/preflight.py`, and the stub
    # prints the argv it was handed.  So this asserts the argv the door would
    # really have received, not a string in a file.
    work = tempfile.mkdtemp(prefix="omini-role-")
    shutil.copy(path, os.path.join(work, "entrypoint.sh"))
    with open(os.path.join(work, "preflight.py"), "w", encoding="utf-8") as fh:
        fh.write("import sys\nprint('ARGV', ' '.join(sys.argv[1:]))\n")

    def run(role, extra=()):
        env = dict(os.environ)
        env["CLAUDIO_SERVER_STORE"] = os.path.join(work, "store")
        if role is not None:
            env["CLAUDIO_SERVER_ROLE"] = role
        else:
            env.pop("CLAUDIO_SERVER_ROLE", None)
        proc = subprocess.run(
            ["/bin/sh", os.path.join(work, "entrypoint.sh")] + list(extra),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        argv = ""
        for line in proc.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("ARGV "):
                argv = line[5:]
        return proc.returncode, argv, proc.stderr.decode("utf-8", "replace")

    rc, argv, _err = run(None)
    check("oci/role: no role is the WHOLE door -- neither flag", rc, 0)
    check("oci/role: ...and its argv carries neither flag",
          [f for f in ("--no-api", "--no-ship") if f in argv.split()], [])

    rc, argv, _err = run("ingest")
    check("oci/role: ingest exits 0", rc, 0)
    check("oci/role: ...and adds --no-api and only --no-api",
          [f for f in ("--no-api", "--no-ship") if f in argv.split()],
          ["--no-api"])

    rc, argv, _err = run("api")
    check("oci/role: api exits 0", rc, 0)
    check("oci/role: ...and adds --no-ship and only --no-ship",
          [f for f in ("--no-api", "--no-ship") if f in argv.split()],
          ["--no-ship"])

    # THE OPERATOR'S OWN FLAGS COME LAST, which is the whole reason this is an
    # environment variable rather than a `CMD`: argparse takes the last
    # occurrence of a repeated option, so overriding one flag must not drop the
    # one that makes the image what it is.
    rc, argv, _err = run("api", ["--port", "9000"])
    words = argv.split()
    check("oci/role: an argv override keeps the role flag",
          "--no-ship" in words, True)
    check("oci/role: ...and the operator's flags are last",
          words[-2:], ["--port", "9000"])

    # AND AN UNKNOWN ROLE IS REFUSED, NOT DEFAULTED.  Falling back to the whole
    # door would start both halves, the store lock and the query engine for
    # somebody who typed one word wrong, with a banner as the only evidence.
    rc, argv, err = run("reader")
    check("oci/role: an unknown role is refused by name", rc, 11)
    check("oci/role: ...and nothing was launched", argv, "")
    check_true("oci/role: ...and the refusal names the value it was given",
               "reader" in err)
    for named in ("ingest", "api", "door"):
        check_true("oci/role: ...and lists %s" % named, named in err)
    shutil.rmtree(work, ignore_errors=True)


def test_oci_the_preflight_refuses_but_never_reconfigures():
    """One decision, one implementation.

    The preflight checks the store and nothing else.  Reader auth and the bind
    address are `serve.py`'s, already refused by name with the path of the file
    to write; a second copy here would be the one that drifts, because it is
    the one nobody runs the suite against.
    """
    path = os.path.join(OCI, "preflight.py")
    if not os.path.isfile(path):
        check("oci/preflight: the preflight was found", path,
              "a file that exists")
        return
    code = code_without_prose(path)
    for forbidden in ("--no-api", "--no-ui", "127.0.0.1", "duckdb"):
        check("oci/preflight: its CODE never says %r" % forbidden,
              forbidden in code, False)
    check_true("oci/preflight: it does exec the door",
               "execv" in code and "serve.py" in code)
    # An ephemeral store is a WARNING and never a refusal: a throwaway door
    # over a throwaway store is a legitimate experiment, and refusing it would
    # make the honest case unreachable.  An unwritable one IS a refusal,
    # because the alternative is `lock_root`'s makedirs raising out of the top
    # of serve.py -- a traceback about `door.lock` for a fault about `chown`.
    check_true("oci/preflight: an unmounted store is said out loud",
               "NOTHING IS MOUNTED THERE" in open(path, encoding="utf-8").read())


def test_oci_the_default_store_path_is_one_string_everywhere():
    """Derived from the preflight's own constant, not hardcoded a fifth time.

    Four files name this path and a fifth would be a fifth place to forget, so
    the constant is read out of `preflight.py` and the others are checked
    against it.
    """
    path = os.path.join(OCI, "preflight.py")
    if not os.path.isfile(path):
        check("oci/store-path: the preflight was found", path,
              "a file that exists")
        return
    src_ = open(path, encoding="utf-8").read()
    m = re.search(r'^DEFAULT_STORE = "([^"]+)"', src_, re.M)
    if not m:
        check("oci/store-path: preflight.py declares DEFAULT_STORE",
              None, "a quoted path")
        return
    store = m.group(1)
    check_true("oci/store-path: it is an absolute path", store.startswith("/"))
    ep = os.path.join(OCI, "entrypoint.sh")
    if os.path.isfile(ep):
        check_true("oci/store-path: entrypoint.sh defaults to %s" % store,
                   store in open(ep, encoding="utf-8").read())
    files = _oci_containerfiles()
    check_true("oci/store-path: there were Containerfiles to check", bool(files))
    # THE TWO STORE HALVES NAME IT AND THE OTHER TWO MUST NOT, WHICH IS THE
    # STRONGER HALF.
    #
    # This used to require every Containerfile to name the store path, which
    # was right while there was one image and is exactly backwards now.  The
    # mcp component is an HTTP CLIENT of the read API -- a reader token and
    # nothing else -- and the proxy parses no record at all; a store path in
    # either is either a mount that should not be there or a comment implying
    # one, and both are how a boundary erodes.  Asserted on the whole file and
    # not just its COPY lines on purpose: a mount point mentioned in prose is
    # how the next person learns to add the mount.
    holders = [(n, t) for n, t in files
               if _component_of(n) in STORE_COMPONENTS]
    check_true("oci/store-path: at least one store-holding image was found",
               bool(holders))
    check("oci/store-path: and it is both halves of the split, not one",
          sorted({_component_of(n) for n, _ in holders}),
          sorted(STORE_COMPONENTS))
    for name, text in holders:
        check_true("oci/store-path: %s names %s" % (name, store),
                   store in text)
    for name, text in files:
        if _component_of(name) in STORE_COMPONENTS:
            continue
        check("oci/store-path: %s never names %s, because that component has "
              "no store" % (name, store), store in text, False)
    readme = os.path.join(os.path.dirname(HERE), "README.md")
    if not os.path.isfile(readme):
        check("oci/store-path: the README was found", readme,
              "a file that exists")
        return
    check_true("oci/store-path: server/README.md names %s" % store,
               store in open(readme, encoding="utf-8").read())


def test_oci_the_pinned_engine_is_the_version_the_measurements_cite():
    """A floating DuckDB does not merely risk a test; it invalidates prose.

    `srv/duck.py` refuses five things DuckDB does silently, and each refusal is
    written against a behaviour MEASURED on one version.  The pin is read out
    of the Containerfile and the version out of duck.py's own "measured on"
    sentences, so neither can move without the other.
    """
    duck_src = open(os.path.join(fx.SRV_DIR, "srv", "duck.py"),
                    encoding="utf-8").read()
    cited = sorted(set(re.findall(r"easured on (\d+\.\d+\.\d+)", duck_src)))
    check("oci/pin: duck.py cites exactly one measured engine version",
          len(cited), 1)
    if not cited:
        return
    files = _oci_containerfiles()
    check_true("oci/pin: there were Containerfiles to check", bool(files))
    pinned = 0
    for name, text in files:
        for got in re.findall(r"^ARG DUCKDB_VERSION=(\S+)", text, re.M):
            pinned += 1
            check("oci/pin: %s pins the version duck.py measured" % name,
                  got, cited[0])
    check_true("oci/pin: at least one image pins a DuckDB version at all",
               pinned > 0)


def test_oci_the_images_carry_the_server_and_nothing_else():
    """`claudio`, the receiver and the shipper are a WORKSTATION install.

    They have no third-party dependency at all and are installed with `make
    install`; shipping them in an image would suggest a container is a
    supported way to run them, which it is not.  Only the SOURCE operands of a
    COPY are inspected -- the destinations are all under `/opt/claudio-server`,
    so grepping the whole line would match the word `claudio` every time.
    """
    files = _oci_containerfiles()
    check_true("oci/contents: there were Containerfiles to check", bool(files))
    for name, text in files:
        sources = []
        for line in text.splitlines():
            s = line.strip()
            if not s.upper().startswith("COPY "):
                continue
            parts = [w for w in s.split()[1:] if not w.startswith("--")]
            sources.extend(parts[:-1])          # the last operand is the dest
        # WHAT EACH COMPONENT COPIES, WHICH IS NOW THREE DIFFERENT ANSWERS.
        #
        #   ingest the package, so `server/srv/` wholesale;
        #   api    the same, because it IS the same source with one package
        #          added -- which is the honest reason two images exist;
        #   mcp    `server/srv/mcp.py` and NOTHING else out of the package --
        #          it is an HTTP client of the read API and a second module
        #          would be the beginning of a second reader;
        #   proxy  no Python at all.
        #
        # The negative for mcp is the load-bearing half: `server/srv/` on its
        # own line would put `store.py`, `duck.py` and `api.py` in an image
        # whose whole authority is one bearer token.
        comp = _component_of(name)
        srv_sources = [x for x in sources if "server/srv" in x]
        if comp in STORE_COMPONENTS:
            check_true("oci/contents: %s copies srv/" % name, bool(srv_sources))
        elif comp == "mcp":
            check("oci/contents: %s copies mcp.py and no other module of the "
                  "package" % name, sorted(srv_sources), ["server/srv/mcp.py"])
        else:
            check("oci/contents: %s copies nothing out of server/srv/" % name,
                  srv_sources, [])
        # Every image carries the licence of the software in it.
        check_true("oci/contents: %s copies the LICENCE" % name,
                   any(x.rstrip("/") == "LICENCE" for x in sources))
        for banned in ("usage/", "test.sh", "server/tests", "Makefile",
                       "Formula"):
            check("oci/contents: %s never copies %s" % (name, banned),
                  any(banned in s for s in sources), False)
        # `claudio` the script, at the repository root: matched as a whole
        # operand so that `server/oci/entrypoint.sh` does not trip it.
        check("oci/contents: %s never copies the claudio script" % name,
              any(s.rstrip("/") == "claudio" for s in sources), False)


def test_oci_the_exit_codes_do_not_collide_with_the_door_s():
    """`serve.py` already owns 1, 3 and 4; a wrapper reusing one is a lie.

    Exit 4 in particular is the refusal an operator is told to look for, so a
    preflight that also exited 4 for an unwritable store would send them to
    write a `readers.json` that was never the problem.
    """
    pre = os.path.join(OCI, "preflight.py")
    ep = os.path.join(OCI, "entrypoint.sh")
    for path, want in ((pre, 5), (ep, 6)):
        if not os.path.isfile(path):
            check("oci/exit: %s was found" % os.path.basename(path), path,
                  "a file that exists")
            continue
        text = open(path, encoding="utf-8").read()
        check_true("oci/exit: %s uses %d for its own refusal"
                   % (os.path.basename(path), want), str(want) in text)
    door = open(os.path.join(fx.SRV_DIR, "srv", "serve.py"),
                encoding="utf-8").read()
    for code in ("return 3", "return 4"):
        check_true("oci/exit: the door still owns `%s`" % code, code in door)


def test_oci_the_readme_documents_the_image_it_publishes():
    """An untested assertion in a README is an untested assertion.

    What has to be there is what an operator cannot discover by running it: the
    two mounts, the refusal they will meet first, and the fact that the
    workstation half is deliberately absent.
    """
    readme = os.path.join(os.path.dirname(HERE), "README.md")
    if not os.path.isfile(readme):
        check("oci/docs: server/README.md was found", readme,
              "a file that exists")
        return
    text = open(readme, encoding="utf-8").read()
    flat = " ".join(text.split())
    for needed in ("readers.json", "/etc/claudio", "/healthz",
                   "ghcr.io", "make install"):
        check_true("oci/docs: the README names %s" % needed, needed in flat)
    check_true("oci/docs: it says the door exits 4 rather than serving an "
               "unauthenticated API off loopback",
               "exit 4" in flat or "exits 4" in flat)
    check_true("oci/docs: it says the workstation half is not in the image",
               "not in the image" in flat or "is NOT containerised" in flat
               or "not containerised" in flat)


# ---------------------------------------------------------------------------
# the FreeBSD image
# ---------------------------------------------------------------------------
#
# It exists because the deployment this door is for is a jail or a container on
# a FreeBSD host, and an operator who runs FreeBSD should not have to run Linux
# to accept shipments from their own workstations.  The two images are meant to
# be INTERCHANGEABLE -- same entrypoint, same environment, same mounts, same
# uid, same exit codes -- so most of what is asserted below is asserted about
# BOTH of them at once.
#
# Nothing here has ever been built.  buildah and podman are not installed on
# the machine this was written on and cannot be, so every claim about the built
# IMAGE is CI's to make; what a test can hold is the source, the scripts and
# the workflow, and that is what these do.

REPO = os.path.dirname(os.path.dirname(HERE))
WORKFLOW = os.path.join(REPO, ".github", "workflows", "images.yml")


def _pkgindex():
    """`server/oci/pkgindex.py` imported as a module, or None."""
    path = os.path.join(OCI, "pkgindex.py")
    if not os.path.isfile(path):
        return None
    import importlib.util
    spec = importlib.util.spec_from_file_location("_pkgindex_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_oci_the_freebsd_image_stages_and_never_runs():
    """`RUN` is the one instruction this image may not have.

    `RUN` executes a binary inside the image and a Linux kernel cannot execute
    FreeBSD binaries -- qemu-user crosses architectures, not operating systems.
    `FROM` and `COPY` execute nothing, which is the whole reason six FreeBSD
    images build on ordinary Linux runners in seconds.  Adding one `RUN` does
    not slow the build down; it makes the build impossible, and the obvious
    "fix" is a self-hosted FreeBSD runner nobody has.

    Asserted on the CODE and not the raw text, because the paragraph above this
    one in the Containerfile says the word repeatedly.
    """
    files = [(n, t) for n, t in _oci_containerfiles() if _os_of(n) == "freebsd"]
    check_true("oci/freebsd: there were FreeBSD Containerfiles to check",
               bool(files))
    # ALL THREE, not just the door.  The impossibility is the same one for each
    # of them -- there is no FreeBSD kernel on the runner -- and the component
    # most likely to acquire a `RUN` is the proxy, where every tutorial in
    # existence says `RUN mkdir` and `RUN chown`.  Those two are done by
    # `stage-freebsd.sh` on the build host instead, and a reader who did not
    # know that would reach for the instruction first.
    for name, text in files:
        code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
        runs = [l for l in code if l.strip().upper().startswith("RUN ")]
        check("oci/freebsd: %s has no RUN instruction" % name, runs, [])
        # FLATTENED, and that is the same trap the doc scan under `usage/` was
        # rewritten for: the sentence is wrapped across two comment lines in
        # two of these three files, so a literal `in text` finds it in the file
        # that happens to fit it on one line and misses it in the others --
        # reporting the paragraph as absent where it is spelled out exactly.
        flat = " ".join(l.lstrip("# ") for l in text.splitlines()
                        if l.lstrip().startswith("#")).replace("  ", " ")
        flat = " ".join(flat.split())
        check_true("oci/freebsd: %s says why, so nobody adds one back" % name,
                   "qemu-user crosses architectures, not operating systems"
                   in flat)
        # The positive half: without it this test passes over an empty file.
        #
        # `FROM ${BASE_REF}` now, because CI resolves the moving base tag to a
        # digest and builds from THAT -- so the assertion is about the pre-FROM
        # ARG's readable default, which is the base a hand build takes and the
        # one `base.name` records.
        check_true("oci/freebsd: %s builds FROM freebsd-runtime" % name,
                   re.search(r"^ARG BASE_REF=\S*freebsd/freebsd-runtime[:@]",
                             text, re.M) is not None)
        check_true("oci/freebsd: %s does it through a build arg, so CI can pin "
                   "the digest" % name, "FROM ${BASE_REF}" in text)
        # THE STAGED TREES ARE THREE DIRECTORIES, NEVER ONE.  Staging is
        # destructive -- `stage-freebsd.sh` starts with `rm -rf` on its stage
        # directory -- so two components sharing a default would mean the
        # second build silently consuming the first's tree, or worse, building
        # the mcp image out of the door's staged DuckDB.
        stages = re.findall(r"^ARG STAGE=(\S+)", text, re.M)
        check("oci/freebsd: %s declares exactly one STAGE default" % name,
              len(stages), 1)
    stage_defaults = [re.search(r"^ARG STAGE=(\S+)", t, re.M).group(1)
                      for _n, t in files
                      if re.search(r"^ARG STAGE=(\S+)", t, re.M)]
    check("oci/freebsd: and no two components stage into the same directory",
          len(set(stage_defaults)), len(stage_defaults))
    for fn in ("fetch-pkgs.sh", "stage-freebsd.sh", "pkgindex.py"):
        check_true("oci/freebsd: %s is beside it" % fn,
                   os.path.isfile(os.path.join(OCI, fn)))


def test_oci_the_freebsd_image_puts_usr_local_lib_on_the_linker_path():
    """MEASURED, and the single easiest thing to delete as obviously redundant.

    FreeBSD's runtime linker searches DT_RPATH/DT_RUNPATH, `LD_LIBRARY_PATH`,
    the hints file `/var/run/ld-elf.so.hints`, then `/lib:/usr/lib`.
    `/usr/local/lib` is reached ONLY through the hints file, which `ldconfig`
    writes at boot -- and this image never boots and has no `RUN` to invoke
    `ldconfig` with.

    From the real published artefacts, read on 2026-08-21:
      * `freebsd/freebsd-runtime:15.1` and `:14.4` ship NO
        `/var/run/ld-elf.so.hints`;
      * `usr/local/bin/python3.12` from the FreeBSD package has an EMPTY
        DT_RUNPATH and needs `libpython3.12.so.1.0` and `libintl.so.8`, both in
        `/usr/local/lib`.

    So without the ENV the image fails at the first exec with `ld-elf.so.1:
    Shared object "libpython3.12.so.1.0" not found` -- after building, pushing
    and pulling cleanly.  There is no `RUN` for `ldconfig` and there cannot be.
    """
    files = [(n, t) for n, t in _oci_containerfiles() if _os_of(n) == "freebsd"]
    check_true("oci/ldpath: there were FreeBSD Containerfiles to check",
               bool(files))
    # EVERY FreeBSD image needs it, and the proxy needs it for a different
    # library than the two Python ones: `nginx` links `libpcre2-8.so.0`, which
    # lives in `/usr/local/lib` and in no directory the runtime linker searches
    # without the hints file.  One image getting this and another not is the
    # shape of failure that survives a build, a push and a pull.
    for name, text in files:
        code = "\n".join(l for l in text.splitlines()
                          if not l.lstrip().startswith("#"))
        env = [l for l in code.splitlines()
               if l.strip().upper().startswith("ENV ")
               and "LD_LIBRARY_PATH" in l]
        check("oci/ldpath: %s sets LD_LIBRARY_PATH exactly once" % name,
              len(env), 1)
        if env:
            check_true("oci/ldpath: ...and %s names /usr/local/lib" % name,
                       "/usr/local/lib" in env[0])


def test_oci_the_two_images_offer_the_same_contract():
    """Interchangeable, or the operator learns two things instead of one.

    Whatever else differs between the bases, an operator types the same
    `docker run`/`podman run`: same entrypoint, same uid, same port, same two
    paths.  Derived by comparing the two files against each other rather than
    against a written-down expectation, so the test cannot drift away from
    whichever one is right.
    """
    files = dict(_oci_containerfiles())
    for base in ("linux", "freebsd"):
        want = "Containerfile.%s.ingest" % base
        if want not in files:
            check("oci/contract: %s was found" % want, want,
                  "a Containerfile that exists")
            return

    # PAIRED BY COMPONENT, DERIVED FROM THE FILE NAMES.
    #
    # There are four components now, and the contract is per component: the
    # ingest pair are interchangeable across the two bases, and so are the api,
    # mcp and proxy pairs -- but an ingest image and an mcp image are NOT meant
    # to agree about USER, EXPOSE or ENTRYPOINT, and comparing them would
    # either fail honestly or force a false agreement.
    #
    # Derived rather than listed, so the mcp and proxy pairs come under this
    # guard the moment the Linux halves land, with no edit here.  A FreeBSD
    # component with no Linux counterpart is not a failure today -- it is the
    # ordinary state while the split lands -- but it is COUNTED, so "every pair
    # agreed" cannot be a statement about an empty set.
    pairs = {}
    for name in files:
        comp, osname = _component_of(name), _os_of(name)
        if comp and osname:
            pairs.setdefault(comp, {})[osname] = name
    both = {c: v for c, v in pairs.items() if len(v) == 2}
    check_true("oci/contract: at least one component is built for both bases",
               bool(both))

    def directive(text, name):
        out = []
        for line in text.splitlines():
            s = line.strip()
            if s.upper().startswith(name + " "):
                out.append(" ".join(s.split()[1:]))
        return out

    for comp, byos in sorted(both.items()):
        for name in ("ENTRYPOINT", "USER", "EXPOSE"):
            a = directive(files[byos["linux"]], name)
            b = directive(files[byos["freebsd"]], name)
            check("oci/contract: the %s images agree on %s" % (comp, name),
                  b, a)
    # And no CMD in either: everything after the image name is appended to the
    # door's argv, and argparse takes the last occurrence of a repeated option,
    # so one flag can be overridden without retyping the rest.  A CMD carrying
    # the full flag list is replaced wholesale and the flag people forget to
    # retype is `--readers`.
    for name, text in files.items():
        check("oci/contract: %s declares no CMD" % name,
              directive(text, "CMD"), [])
    # EVERY IMAGE RUNS AN ENTRYPOINT AT AN ABSOLUTE PATH IT ALSO COPIES.
    #
    # It used to be "both name /opt/claudio-server/entrypoint.sh", which is a
    # door fact: the mcp image runs its own entrypoint and the proxy runs nginx
    # directly, with no wrapper to go wrong.  What is true of all three -- and
    # is what that assertion was reaching for -- is that the ENTRYPOINT is an
    # absolute path and is not a shell string that resolves through PATH, since
    # PATH inside a container is whatever the base happened to set.
    for name, text in sorted(files.items()):
        eps = directive(text, "ENTRYPOINT")
        check("oci/contract: %s declares exactly one ENTRYPOINT" % name,
              len(eps), 1)
        if eps:
            check_true("oci/contract: %s's ENTRYPOINT is exec form" % name,
                       eps[0].startswith("["))
            first = re.findall(r'"([^"]+)"', eps[0])
            check_true("oci/contract: %s's ENTRYPOINT is an absolute path"
                       % name, bool(first) and first[0].startswith("/"))

    # THE LABELS, WHICH THE FILTER ABOVE COULD NOT SEE.
    #
    # This test's docstring is "Interchangeable, or the operator learns two
    # things instead of one", and it compared ENTRYPOINT, USER and EXPOSE --
    # so every divergence in the one thing a user reads WITHOUT running the
    # image went unasserted.  Four were live at once: a `licenses` of MIT over
    # a BSD-2-Clause `LICENCE` the image itself carries; an `image.version`
    # reporting DuckDB's version as the product's; `com.claudio.engine` and
    # `com.claudio.stream-a` on the image that has never been executed and on
    # neither the one that proves its engine every build; and `image.revision`
    # as a config LABEL on one and a manifest ANNOTATION on the other.
    #
    # Key SETS, so a key present in one and absent in the other is a named
    # failure.  Values are deliberately NOT compared: the description and the
    # engine's provenance differ honestly between the two bases.
    def labels(text):
        out, buf, collecting = {}, "", False
        for line in text.splitlines():
            st = line.strip()
            if st.upper().startswith("LABEL "):
                collecting, buf = True, st[6:]
            elif collecting:
                buf += " " + st
            if collecting and not st.endswith("\\"):
                collecting = False
                for tok in re.findall(r'([A-Za-z0-9._-]+)="', buf.replace("\\", " ")):
                    out[tok] = True
        return set(out)

    for comp, byos in sorted(both.items()):
        la = labels(files[byos["linux"]])
        lb = labels(files[byos["freebsd"]])
        check_true("oci/contract: the Linux %s image declares labels at all"
                   % comp, bool(la))
        check("oci/contract: the %s images declare the same label KEYS "
              "(only-in-linux, only-in-freebsd)" % comp,
              (sorted(la - lb), sorted(lb - la)), ([], []))
    all_labels = {n: labels(t) for n, t in files.items()}
    check("oci/contract: and no image claims a capability it cannot derive",
          sorted({k for ks in all_labels.values() for k in ks
                  if k.startswith("com.claudio.stream")}), [])
    for name, ks in sorted(all_labels.items()):
        for want in ("org.opencontainers.image.revision",
                     "org.opencontainers.image.version",
                     "org.opencontainers.image.source",
                     "org.opencontainers.image.base.digest",
                     "com.claudio.component"):
            check_true("oci/contract: %s carries %s as a LABEL" % (name, want),
                       want in ks)
        # `com.claudio.engine` IS REQUIRED OF EVERY IMAGE, AND THE RULE MOVED
        # FROM ITS PRESENCE TO ITS VALUE.
        #
        # It used to be required of exactly the images that pin a DuckDB
        # version, on the grounds that a label the proxy has to invent is
        # ceremony.  The split makes that backwards: "this image has no query
        # engine in it" is the CLAIM THE SPLIT RESTS ON for the ingest and mcp
        # components, and an absent label states it to nobody -- a registry
        # listing shows the same nothing for "no engine" and for "we forgot".
        # So every image declares it, and the value NAMES duckdb iff the file
        # pins a version.  A pin with no duckdb in the label, or a duckdb in
        # the label with no pin, is the disagreement worth catching.
        pins = "ARG DUCKDB_VERSION=" in files[name]
        check_true("oci/contract: %s declares com.claudio.engine" % name,
                   "com.claudio.engine" in ks)
        m = re.search(r'com\.claudio\.engine="([^"]*)"', files[name])
        check("oci/contract: %s's engine label names duckdb iff it pins a "
              "duckdb version" % name,
              bool(m) and "duckdb" in m.group(1).lower(), pins)
    # The component label says what the file name says, so a copied
    # Containerfile whose label was not updated is caught here rather than in a
    # registry listing.
    for name, text in sorted(files.items()):
        m = re.search(r'com\.claudio\.component="([^"]+)"', text)
        check("oci/contract: %s's component label matches its file name" % name,
              m.group(1) if m else None, _component_of(name))

    # THE BASE, SPELLED TWICE IN EACH FILE, SO THE TWO SPELLINGS ARE COMPARED.
    #
    # `ARG BASE_REF` is what `FROM` takes and `ARG BASE_NAME` is what
    # `org.opencontainers.image.base.name` records.  CI overrides the first
    # with a digest and the second with the readable tag, so they cannot
    # disagree there -- but a hand build takes both defaults, and a default
    # that drifted would publish an image whose label names a base it was not
    # built from.  Compared by the repository/tag part, since one carries a
    # registry prefix and the other need not.
    def base_args(text):
        # Every `ARG NAME=value` default, so `${FREEBSD_VERSION}` inside
        # `BASE_NAME` resolves the way a build with no --build-arg resolves
        # it.  Without the substitution the two spellings could never be
        # compared at all and this guard would have to be deleted -- which is
        # how a guard becomes a comment.
        defaults = dict(re.findall(r"^ARG ([A-Z_]+)=(\S+)", text, re.M))

        def resolve(v):
            for k, dv in defaults.items():
                if "${%s}" % k not in v:
                    continue
                v = v.replace("${%s}" % k, dv)
            for pre in ("docker.io/", "library/"):
                if v.startswith(pre):
                    v = v[len(pre):]
            return v

        return {k: resolve(defaults[k]) for k in ("BASE_REF", "BASE_NAME")
                if k in defaults}

    for name, text in sorted(files.items()):
        got = base_args(text)
        check("oci/contract: %s declares both base spellings" % name,
              sorted(got), ["BASE_NAME", "BASE_REF"])
        if len(got) == 2:
            check("oci/contract: %s names one base, not two" % name,
                  got["BASE_NAME"], got["BASE_REF"])

    # THE LICENCE, DERIVED FROM THE REPOSITORY'S OWN `LICENCE` FILE.
    #
    # It said MIT in three places -- both Containerfiles and the workflow's
    # annotation list -- which is the reference repository's licence copied
    # without re-checking it against this project's facts.  The field is an
    # SPDX expression that registry UIs, SBOM generators and compliance
    # scanners read as authoritative, and the image `COPY`s the contradicting
    # text a few lines above the label.  Derived rather than hardcoded, for
    # the `LOGGING_LEGACY` and `CLAUDIO_*` guards' reason: a written-down copy
    # is a fourth place to forget.
    spdx = {"BSD 2-Clause License": "BSD-2-Clause",
            "BSD 3-Clause License": "BSD-3-Clause",
            "MIT License": "MIT",
            "Apache License": "Apache-2.0"}
    lic_path = os.path.join(REPO, "LICENCE")
    if not os.path.isfile(lic_path):
        check("oci/licence: the repository's LICENCE was found", lic_path,
              "a file that exists")
    else:
        head = open(lic_path, encoding="utf-8").readline().strip()
        want = spdx.get(head)
        if want is None:
            check("oci/licence: the LICENCE's first line maps to an SPDX id",
                  head, "one of " + ", ".join(sorted(spdx)))
        else:
            got = {}
            for name, text in files.items():
                m = re.search(
                    r'org\.opencontainers\.image\.licenses="([^"]*)"', text)
                got[name] = m.group(1) if m else None
            wf = os.path.join(REPO, ".github", "workflows", "images.yml")
            if os.path.isfile(wf):
                for m in re.finditer(
                        r'org\.opencontainers\.image\.licenses=([^\s"\\]+)',
                        open(wf, encoding="utf-8").read()):
                    got["images.yml annotation"] = m.group(1)
            check_true("oci/licence: every place that states a licence was "
                       "found", len(got) >= 3)
            for where, value in sorted(got.items()):
                check("oci/licence: %s states the SPDX id of the LICENCE this "
                      "repository actually ships" % where, value, want)


def test_oci_the_package_index_is_the_root_of_trust_and_is_verified():
    """The index used to be fetched and trusted, and everything derives from it.

    `packagesite.yaml` says which URL each package is fetched from
    (`repopath`) AND what its blake2b digest should be (`sum`).  So while the
    index itself was unauthenticated, "verifies every blake2b checksum" --
    which `server/README.md`, `Containerfile.freebsd` and the workflow all
    claimed -- meant only that the bytes matched what the same unverified
    document said they would.  Substituting the index substitutes both halves
    at once and every check passes.

    It needed no dependency: `packagesite.pkg` already CONTAINS
    `packagesite.yaml.sig` and `packagesite.yaml.pub`, and the previous script
    extracted one member and discarded those two.

    Exercised, not described.  The signing key is generated here rather than
    committed, so the suite stays hermetic and needs no network: what is under
    test is the VERIFIER, and a synthetic RSA key exercises every branch of it
    including the two nobody can reach with the real key -- a forged padding
    block, and a signature over the single hash rather than pkg's double one.
    """
    path = os.path.join(OCI, "pkgindex.py")
    if not os.path.isfile(path):
        check("oci/index: pkgindex.py was found", path, "a file that exists")
        return
    pk = _pkgindex()
    if pk is None:
        check("oci/index: pkgindex.py imported", path, "an importable module")
        return

    # -- the pin ------------------------------------------------------------
    fp = getattr(pk, "PKG_FREEBSD_FINGERPRINT", None)
    check_true("oci/index: the FreeBSD repository key is pinned as a 64-hex "
               "sha256",
               isinstance(fp, str) and len(fp) == 64
               and all(c in "0123456789abcdef" for c in fp))
    src = open(path, encoding="utf-8").read()
    check_true("oci/index: and the pin says where it comes from, so a "
               "mismatch reads as a rotated key rather than a broken check",
               "/usr/share/keys/pkg/trusted/" in src)

    # -- the verifier, over a key this test owns ----------------------------
    key = _rsa_key_1024()
    pem = _spki_pem(key["n"], key["e"])
    check("oci/index: the SPKI parser recovers the modulus and exponent",
          pk.rsa_pubkey(pem), (key["n"], key["e"]))

    body = b'{"name":"python312","sum":"2$abc","repopath":"All/python312.pkg"}\n'
    inner = hashlib.sha256(body).hexdigest()
    double = hashlib.sha256(inner.encode("ascii")).digest()
    single = hashlib.sha256(body).digest()

    good = _rsa_sign_pkcs1_sha256(key, double)
    check("oci/index: a real signature over pkg's DOUBLE hash verifies",
          pk.rsa_pkcs1v15_sha256_ok(pem, good, double), True)
    # pkg signs the ASCII HEX STRING of the file's sha256, not the sha256.
    # Measured both ways against the live FreeBSD:15:amd64/latest index on
    # 2026-08-21: the double hash matches and the plain one does not.  A
    # reader who "simplifies" that to one hash gets a check that can never
    # match, which is a check that looks like a check.
    check("oci/index: a signature over the SINGLE hash does NOT verify, which "
          "is the simplification most likely to be made",
          pk.rsa_pkcs1v15_sha256_ok(pem, _rsa_sign_pkcs1_sha256(key, single),
                                    double), False)
    check("oci/index: a flipped byte in the index does not verify",
          pk.rsa_pkcs1v15_sha256_ok(pem, good, single), False)
    check("oci/index: a truncated signature does not verify",
          pk.rsa_pkcs1v15_sha256_ok(pem, good[:-1], double), False)

    # THE FORGERY THE `find`-BASED CHECK WOULD HAVE ACCEPTED.  A block that
    # carries a valid SHA-256 DigestInfo for the right digest but whose PKCS#1
    # padding is short and stuffed with attacker-chosen bytes -- the classic
    # Bleichenbacher'06 shape.  Only reachable because this test holds the
    # private key; it is why the verifier reconstructs the whole block and
    # compares it byte for byte instead of searching for the DigestInfo.
    k = (key["n"].bit_length() + 7) // 8
    di = pk._SHA256_DIGESTINFO + double
    forged_em = (b"\x00\x01" + b"\xff" * 4 + b"\x00" + di)
    forged_em = forged_em + b"\x41" * (k - len(forged_em))
    forged = pow(int.from_bytes(forged_em, "big"), key["d"], key["n"])
    check("oci/index: a short-padding block carrying the right DigestInfo is "
          "REFUSED; a DigestInfo search would have taken it",
          pk.rsa_pkcs1v15_sha256_ok(
              pem, forged.to_bytes(k, "big"), double), False)

    # -- the command, and each of its three refusals ------------------------
    td = tempfile.mkdtemp()
    try:
        y = os.path.join(td, "packagesite.yaml")
        sg = os.path.join(td, "packagesite.yaml.sig")
        pb = os.path.join(td, "packagesite.yaml.pub")
        open(y, "wb").write(body)
        open(sg, "wb").write(good)
        open(pb, "wb").write(pem)
        real = pk.PKG_FREEBSD_FINGERPRINT
        try:
            pk.PKG_FREEBSD_FINGERPRINT = hashlib.sha256(pem).hexdigest()
            with contextlib.redirect_stderr(io.StringIO()):
                accepted = _sysexit(pk.cmd_verifyindex, [y, sg, pb])
            check("oci/index: verifyindex accepts a correctly signed index",
                  accepted, None)
            open(y, "wb").write(body + b"tampered\n")
            check_true("oci/index: and REFUSES a tampered one by name",
                       "signature does not verify"
                       in str(_sysexit(pk.cmd_verifyindex, [y, sg, pb])))
            open(y, "wb").write(body)
            check_true("oci/index: refuses a missing .sig rather than falling "
                       "back to an unverified index",
                       "Refusing rather than falling back"
                       in str(_sysexit(pk.cmd_verifyindex,
                                       [y, sg + ".nope", pb])))
            pk.PKG_FREEBSD_FINGERPRINT = real
            check_true("oci/index: and refuses a key that is not the pinned "
                       "one, saying it means the key rotated",
                       "ROTATED"
                       in str(_sysexit(pk.cmd_verifyindex, [y, sg, pb])))
        finally:
            pk.PKG_FREEBSD_FINGERPRINT = real
    finally:
        shutil.rmtree(td, ignore_errors=True)

    # -- and the caller actually calls it, before it reads the index --------
    fetch = os.path.join(OCI, "fetch-pkgs.sh")
    if not os.path.isfile(fetch):
        check("oci/index: fetch-pkgs.sh was found", fetch,
              "a file that exists")
        return
    code = _sh_code(open(fetch, encoding="utf-8").read())
    check_true("oci/index: fetch-pkgs.sh extracts the .sig and the .pub, "
               "which it used to throw away",
               "packagesite.yaml.sig" in code and "packagesite.yaml.pub" in code)
    # The INVOCATION, not the word.  A first draft asserted that the string
    # "verifyindex" appeared somewhere before "resolve", and a mutation that
    # commented the call out but left the token in a disabled line survived it
    # green.  So this looks for a line that actually runs `pkgindex.py
    # verifyindex`, and takes the position of that line.
    def call_line(verb):
        for i, line in enumerate(code.splitlines()):
            st = line.strip()
            if st.startswith((":", "#")):
                continue
            if "pkgindex.py" in st and verb in st.split():
                return i
            # The invocation is wrapped, so the verb can be on the same line
            # as `pkgindex.py` or the line is continued -- both are covered by
            # matching the verb as a whole word on a line naming the script.
            if "pkgindex.py" in st and st.rstrip("\\").strip().endswith(verb):
                return i
        return -1

    vi, rs = call_line("verifyindex"), call_line("resolve")
    check_true("oci/index: fetch-pkgs.sh really RUNS pkgindex.py verifyindex",
               vi != -1)
    check_true("oci/index: and it runs resolve too, or the ordering check "
               "below has nothing to order", rs != -1)
    check_true("oci/index: the verification happens BEFORE anything derives a "
               "URL or a checksum from the index",
               vi != -1 and rs != -1 and vi < rs)

    # -- https, and it stays https ------------------------------------------
    #
    # `curl -fsSL` follows an https -> http redirect without a word (curl's
    # own manual: "By default curl only allows HTTP, HTTPS, FTP and FTPS on
    # redirects"), so one 302 in front of the index downgrades the rest of the
    # transfer to cleartext -- and the attacker then authors both the
    # `repopath` and the `sum` that `verify` compares against.
    check_true("oci/index: the fetcher refuses a non-https URL",
               "--proto '=https'" in code or '--proto =https' in code)
    check_true("oci/index: and refuses an https -> http redirect",
               "--proto-redir '=https'" in code
               or "--proto-redir =https" in code)
    check("oci/index: with no bare `curl -fsSL` left to bypass it",
          [l.strip() for l in code.splitlines()
           if "curl -fsSL" in l and "--proto" not in l], [])


def test_oci_the_linux_engine_is_pinned_to_a_file_and_not_only_a_version():
    """A version pin does not pin the bytes.

    `pip install duckdb==1.5.5` survives a different file published under the
    same version, an index or mirror substitution, and an inherited
    `PIP_INDEX_URL`.  The asymmetry is what made it worth closing: the FreeBSD
    half blake2b-checks every package it stages, and the Linux half -- the one
    whose image is built and pulled today -- checked nothing about the bytes
    of its one third-party dependency.
    """
    req = os.path.join(OCI, "requirements-linux.txt")
    cf = os.path.join(OCI, "Containerfile.linux.api")
    for p in (req, cf):
        if not os.path.isfile(p):
            check("oci/pip: %s was found" % os.path.basename(p), p,
                  "a file that exists")
            return
    rtext = open(req, encoding="utf-8").read()
    ctext = open(cf, encoding="utf-8").read()

    hashes = re.findall(r"--hash=sha256:([0-9a-f]{64})", rtext)
    check_true("oci/pip: a hash is pinned for each architecture's wheel",
               len(hashes) >= 2 and len(set(hashes)) == len(hashes))
    pinned = re.findall(r"^duckdb==([0-9][0-9.]*)", rtext, re.M)
    arg = re.findall(r"^ARG DUCKDB_VERSION=([0-9][0-9.]*)", ctext, re.M)
    check_true("oci/pip: the requirements file names one version", len(pinned) == 1)
    check_true("oci/pip: and the Containerfile names one", len(arg) == 1)
    # Two places, so they are compared.  The build ALSO catches this at run
    # time by re-importing, but a static failure names the two files.
    check("oci/pip: requirements-linux.txt and ARG DUCKDB_VERSION agree",
          pinned, arg)

    for flag, why in (("--require-hashes", "or the hashes are decoration"),
                      ("--no-deps", "or a future mandatory dependency arrives "
                                    "unhashed"),
                      ("--only-binary=:all:", "or an sdist compiles the "
                                              "engine from source"),
                      ("--index-url", "or an inherited PIP_INDEX_URL "
                                      "redirects the fetch")):
        check_true("oci/pip: the install passes %s, %s" % (flag, why),
                   flag in ctext)
    # No `pip install` ANYWHERE in the file that is not the hashed one.  The
    # filter strips full-line comments first, for `_sh_code`'s reason: the
    # thirty lines of prose above the install line say `pip install duckdb==X`
    # while explaining why the version alone is not enough, and a guard that
    # matched the explanation would fail on a correct file and pass on a
    # wrong one the moment somebody reworded the comment.
    installs = [l.strip() for l in _sh_code(ctext).splitlines()
                if "pip install" in l]
    check_true("oci/pip: there is exactly one pip install line", len(installs) == 1)
    check("oci/pip: and it is the hashed one",
          [l for l in installs if "--require-hashes" not in l], [])


def test_oci_pkgindex_refuses_by_name_and_never_skips():
    """Four refusals, exercised rather than described.

    Each one is a place where the quiet alternative produces a published image
    that is wrong: a skipped checksum, a dependency edge that was not followed,
    an exclusion that silently stopped applying, and a checksum format nobody
    checked.  The reference repository's rule, carried over: a check that
    cannot fire is worse than no check, because it looks like one.
    """
    pk = _pkgindex()
    if pk is None:
        check("oci/pkgindex: pkgindex.py was found", None, "a file that exists")
        return

    # A tiny index, written here rather than fetched: this is about the graph
    # walk and the refusals, and a test that needed the network would be a test
    # that gets deleted.
    def index(entries):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e) + "\n")
        return path

    def pkg(name, deps=(), version="1.0"):
        return {"name": name, "version": version,
                "repopath": "All/%s-%s.pkg" % (name, version),
                "sum": "2$abc", "pkgsize": 1, "flatsize": 10,
                "deps": {d: {"version": "1.0"} for d in deps}}

    path = index([pkg("app", ["libx", "shared"]),
                  pkg("libx", ["shared", "only-libx"]),
                  pkg("shared"), pkg("only-libx")])
    try:
        got = pk.closure(pk.load(path), ["app"])
        check("oci/pkgindex: the closure is transitive",
              got, ["app", "libx", "only-libx", "shared"])

        # Dropping a NODE, not subtracting a list: `only-libx` goes because
        # nothing else reaches it, `shared` stays because `app` still does.
        # A list subtraction would keep the first and a naive subtree delete
        # would take the second, and both produce an image that is missing a
        # library or 900 MB larger than its README says.
        got = pk.closure(pk.load(path), ["app"], without=["libx"])
        check("oci/pkgindex: an exclusion takes what only it reached",
              got, ["app", "shared"])

        # A dependency the index does not carry is a REFUSAL. Skipping the edge
        # is a missing library in a published image, and nothing would say so.
        broken = index([pkg("app", ["absent"])])
        try:
            rc = "did not exit"
            try:
                pk.closure(pk.load(broken), ["app"])
            except SystemExit as exc:
                rc = str(exc)
            check_true("oci/pkgindex: a dependency absent from the index is "
                       "refused by name", "absent" in rc and "not in the index" in rc)
        finally:
            os.unlink(broken)

        # An empty file is not an empty closure.
        empty = index([])
        try:
            rc = "did not exit"
            try:
                pk.load(empty)
            except SystemExit as exc:
                rc = str(exc)
            check_true("oci/pkgindex: an index that parsed to nothing is "
                       "refused, not read as empty",
                       "not a pkg index" in rc)
        finally:
            os.unlink(empty)
    finally:
        os.unlink(path)

    # The checksum half.  `verify` is the one piece carried over from
    # `gabrielbelli/freebsd-oauth2-proxy-oci` unchanged, and z-base-32 is the
    # part that would fail silently if it were wrong -- so it is checked
    # against a digest computed here rather than against a constant.
    fd, blob = tempfile.mkstemp()
    with os.fdopen(fd, "wb") as fh:
        fh.write(b"the bytes of a package")
    try:
        good = "2$" + pk.zbase32(hashlib.blake2b(
            open(blob, "rb").read()).digest())
        ok = True
        try:
            pk.cmd_verify([blob, good])
        except SystemExit as exc:
            ok = str(exc)
        check("oci/pkgindex: a correct checksum verifies", ok, True)

        rc = "did not exit"
        try:
            pk.cmd_verify([blob, "2$wrongwrongwrong"])
        except SystemExit as exc:
            rc = str(exc)
        check_true("oci/pkgindex: a wrong checksum is a mismatch, by name",
                   "checksum mismatch" in rc)

        rc = "did not exit"
        try:
            pk.cmd_verify([blob, "9$whatever"])
        except SystemExit as exc:
            rc = str(exc)
        check_true("oci/pkgindex: an unsupported checksum version REFUSES "
                   "rather than skipping the check",
                   "refusing to skip the check" in rc)

        rc = "did not exit"
        try:
            pk.cmd_verify([blob, ""])
        except SystemExit as exc:
            rc = str(exc)
        check_true("oci/pkgindex: no checksum at all is refused too",
                   "no checksum" in rc)
    finally:
        os.unlink(blob)


def test_oci_the_prune_list_is_checked_against_what_the_server_imports():
    """`stage-freebsd.sh` deletes 183 MiB out of somebody else's package.

    `lib/python3.12/test` alone is 132 MiB of a 304 MiB tree and nothing under
    `srv/` can reach it -- but that is a judgement, and the failure it can
    cause is `ModuleNotFoundError` on a machine that is not this one, which no
    amount of building catches.  So the import list is DERIVED from `srv/` with
    `ast` and the staged tree is checked against it.

    Asserted both ways.  A checker that only ever answers "fine" is the
    decoration this project keeps writing tests against, so the negative half
    builds a tree with a module missing and requires the refusal.
    """
    pk = _pkgindex()
    if pk is None:
        check("oci/prune: pkgindex.py was found", None, "a file that exists")
        return
    srv_dir = os.path.join(fx.SRV_DIR, "srv")
    mods = pk._srv_imports(srv_dir)
    check_true("oci/prune: the import list is derived from srv/ and is not "
               "empty", len(mods) > 5)
    for expected in ("json", "http", "duckdb"):
        check_true("oci/prune: ...and names %s" % expected, expected in mods)

    root = tempfile.mkdtemp()
    try:
        lib = os.path.join(root, "usr", "local", "lib", "python3.12")
        os.makedirs(os.path.join(lib, "lib-dynload"))
        os.makedirs(os.path.join(lib, "site-packages", "duckdb"))
        for mod in mods:
            if mod == "duckdb":
                continue
            open(os.path.join(lib, mod + ".py"), "w").close()
        rc = pk.cmd_modules([root, srv_dir, "3.12"])
        check("oci/prune: a complete tree passes", rc, 0)

        # Now take one away.  `json` is chosen because it is a PACKAGE in the
        # real tree, so a checker that only looked for `<name>.py` would still
        # answer yes on the real one -- the failure this half exists to catch.
        os.unlink(os.path.join(lib, "json.py"))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = pk.cmd_modules([root, srv_dir, "3.12"])
        check("oci/prune: a tree with a module missing is REFUSED", rc, 1)
        check_true("oci/prune: ...and the refusal names the module",
                   "json" in err.getvalue())
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_oci_an_absent_import_is_a_refusal_unless_the_component_declared_it():
    """THE INGEST IMAGE RESTS ON A DISTINCTION THAT DID NOT EXIST, AND THIS IS IT.

    `stage-freebsd.sh`'s ingest branch said of `duckdb`: "it is imported inside
    a function, and the check reads module-level imports, which is exactly the
    distinction this component rests on".  `_srv_imports` walked the tree with
    `ast.walk`, which descends into function bodies, so `duckdb` was in the
    list and `stage-freebsd.sh ingest` -- the whole point of which is an image
    with no engine in it -- failed its own check.  Found by RUNNING the stage
    against the live FreeBSD:15:amd64 index, after it had downloaded, unpacked
    and pruned 243 MiB; not by reading the comment, which read perfectly well.

    So the distinction is real now, and it has two halves that must both hold.
    A module imported AT IMPORT TIME may never be absent: that is a
    `ModuleNotFoundError` before anything serves, and no flag excuses it.  A
    module imported only inside a function body may be absent WHEN THE
    COMPONENT SAYS SO -- which is `store.require()`'s whole design, raising
    `DuckDBMissing` with the install command rather than a traceback.

    And the declaration refuses itself when it goes stale, which is
    `--without`'s rule one command over.  Both staleness shapes are asserted,
    because a permission that quietly stopped applying is how 900 MB comes back
    into an image whose README says it does not carry it.
    """
    pk = _pkgindex()
    if pk is None:
        check("oci/deferred: pkgindex.py was found", None, "a file that exists")
        return
    srv_dir = os.path.join(fx.SRV_DIR, "srv")
    at_import, deferred = pk._srv_imports_by_kind(srv_dir)

    check("oci/deferred: duckdb is imported by srv/ ONLY inside a function",
          ("duckdb" in deferred, "duckdb" in at_import), (True, False))
    for mod in ("json", "http", "os"):
        check_true("oci/deferred: ...while %s is imported at import time" % mod,
                   mod in at_import)
    # The union is unchanged, so every caller that wants "everything srv/
    # touches" still gets it.
    check_true("oci/deferred: the union still names duckdb",
               "duckdb" in pk._srv_imports(srv_dir))

    # A CLASS BODY IS IMPORT TIME AND A FUNCTION BODY IS NOT.  Asserted on a
    # written-out module rather than on `srv/`, because `srv/` has no class-body
    # import today and the rule has to hold the day somebody adds one: a class
    # body executes on import, so an import inside it is deferred by nothing.
    tmp = tempfile.mkdtemp()
    try:
        probe = os.path.join(tmp, "probe.py")
        with open(probe, "w") as fh:
            fh.write("import eager_top\n"
                     "class C:\n"
                     "    import eager_in_class\n"
                     "def f():\n"
                     "    import lazy_in_func\n"
                     "    from lazy_from import thing\n"
                     "if True:\n"
                     "    import eager_in_if\n")
        a, d = pk._srv_imports_by_kind(probe)
        check("oci/deferred: a module-level, class-body and if-body import are "
              "all import time",
              sorted(a), ["eager_in_class", "eager_in_if", "eager_top"])
        check("oci/deferred: ...and only a function body defers",
              sorted(d), ["lazy_from", "lazy_in_func"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- the check itself, over a tree with no duckdb in it ---------------
    #
    # `cmd_modules` REFUSES WITH `sys.exit(<message>)`, WHICH IS A `SystemExit`
    # THROUGH THE MIDDLE OF THIS SUITE, and calling it bare is how a named
    # failure becomes no run at all.  Found by the mutation matrix and not by
    # reading: `duck-imports-the-driver` -- which makes `srv/duck.py` import
    # duckdb at import time, exactly the condition the second refusal below
    # exists for -- reported `BAD PATCH ... suite did not run` instead of
    # `caught`, because the process exited on the `--deferred-ok duckdb` call a
    # few lines down.  Two costs, and the second is the serious one: the matrix
    # cannot tell a mutation that killed the suite from one nothing asserts,
    # and a REAL regression here would black out every test after this point,
    # which is `test.sh`'s `grep -c` blackout of ~325 assertions in another
    # language.
    #
    # So every call goes through this, and a refusal becomes an rc and a
    # message like any other.  The staleness cases below keep their explicit
    # `try`, because there the SystemExit IS the assertion.
    def modules(args):
        err = io.StringIO()
        try:
            with contextlib.redirect_stderr(err):
                rc = pk.cmd_modules(args)
        except SystemExit as exc:
            # `sys.exit("text")` is rc 1 with the text on stderr; reproducing
            # that here rather than re-raising is what keeps one refusal from
            # ending the run.
            return 1, err.getvalue() + str(exc)
        return rc, err.getvalue()

    root = tempfile.mkdtemp()
    try:
        lib = os.path.join(root, "usr", "local", "lib", "python3.12")
        os.makedirs(os.path.join(lib, "lib-dynload"))
        for mod in at_import | deferred:
            if mod == "duckdb":
                continue                        # the whole point: it is absent
            open(os.path.join(lib, mod + ".py"), "w").close()

        rc, text = modules([root, srv_dir, "3.12"])
        check("oci/deferred: an undeclared absence is REFUSED", rc, 1)
        check_true("oci/deferred: ...and the refusal names it",
                   "duckdb" in text)

        rc, text = modules([root, srv_dir, "3.12", "--deferred-ok", "duckdb"])
        check("oci/deferred: a DECLARED absence passes -- this is the ingest "
              "image", rc, 0)
        # Named, never silent.  An image deliberately short a package must
        # leave a line in the build log, or it reads as an oversight later.
        check_true("oci/deferred: ...and the log says the module is absent on "
                   "purpose", "duckdb" in text and "ABSENT" in text)

        # THE FLAG DOES NOT EXCUSE ANYTHING ELSE.  Without this the permission
        # would be a blanket one, which is the shape that publishes a broken
        # image while looking like a decision.
        os.unlink(os.path.join(lib, "json.py"))
        rc, text = modules([root, srv_dir, "3.12", "--deferred-ok", "duckdb"])
        check("oci/deferred: an import-time module is still REFUSED with the "
              "flag set", rc, 1)
        check_true("oci/deferred: ...and the refusal names json, not duckdb",
                   "json" in text)
        open(os.path.join(lib, "json.py"), "w").close()

        # ---- the two staleness refusals -----------------------------------
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                pk.cmd_modules([root, srv_dir, "3.12",
                                "--deferred-ok", "nothing_imports_this"])
            check("oci/deferred: a permission for an import that does not "
                  "exist is refused", "returned", "SystemExit")
        except SystemExit as exc:
            check_true("oci/deferred: a permission for an import that does not "
                       "exist is refused, by name",
                       "nothing_imports_this" in str(exc))

        try:
            with contextlib.redirect_stderr(io.StringIO()):
                pk.cmd_modules([root, srv_dir, "3.12", "--deferred-ok", "json"])
            check("oci/deferred: a permission for an IMPORT-TIME module is "
                  "refused", "returned", "SystemExit")
        except SystemExit as exc:
            check_true("oci/deferred: a permission for an IMPORT-TIME module "
                       "is refused, by name", "json" in str(exc))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    # ---- and the staging script is what actually passes it ----------------
    stage = os.path.join(OCI, "stage-freebsd.sh")
    if not os.path.isfile(stage):
        check("oci/deferred: stage-freebsd.sh was found", stage,
              "a file that exists")
        return
    text = open(stage, encoding="utf-8").read()
    check_true("oci/deferred: the script really passes --deferred-ok to "
               "pkgindex.py modules",
               re.search(r"pkgindex\.py\" modules[^\n]*(\n[^\n]*)?--deferred-ok",
                         text) is not None)

    def branch(name):
        m = re.search(r"^    %s\)\n(.*?)^        ;;" % re.escape(name),
                      text, re.M | re.S)
        return m.group(1) if m else None

    # ONLY THE INGEST COMPONENT DECLARES ONE, and that is the assertion that
    # keeps the flag from spreading.  The api stages the engine, so it has
    # nothing to excuse; the mcp image is checked against `mcp.py`, which does
    # not import duckdb at all -- and if either of them ever needed the flag,
    # something has changed that a person should look at.
    for comp in COMPONENTS:
        b = branch(comp)
        if b is None:
            continue
        declared = re.search(r'^\s*DEFERRED_OK="([^"]*)"', b, re.M)
        got = declared.group(1) if declared else ""
        check("oci/deferred: %s declares %r" % (comp, "duckdb" if comp == "ingest" else ""),
              got, "duckdb" if comp == "ingest" else "")


def test_oci_the_mcp_image_cannot_reach_the_store():
    """The split is a BOUNDARY, and this is where it is enforced.

    `srv/mcp.py` is an HTTP client of the read API: a reader token, and no
    store access at all.  That is the whole of its authority -- it cannot see
    an account the token's scope excludes, cannot reach the store's files, and
    cannot answer a question the API refuses.  Giving its image the DuckDB
    engine or the store's query layer would be a privilege escalation, and it
    would also defeat the split.

    Three assertions, and the second is the one that makes it structural.  The
    Containerfile could simply not COPY those modules -- but a later edit
    could, and nothing would say so.  The ignore file keeps them out of the
    build CONTEXT, so a `COPY server/srv/` added there fails the build by name.
    """
    files = dict(_oci_containerfiles())
    mcps = {n: t for n, t in files.items() if _component_of(n) == "mcp"}
    check_true("oci/mcp: an mcp Containerfile was found", bool(mcps))

    for name, text in sorted(mcps.items()):
        code = "\n".join(l for l in text.splitlines()
                          if not l.lstrip().startswith("#"))
        # `duckdb` and the store's own modules, in the CODE and not the prose:
        # this file explains at length why it does not carry them.
        for banned in ("duckdb", "store.py", "duck.py", "query.py", "api.py",
                       "serve.py", "preflight.py", "readers.json",
                       "tokens.json"):
            check("oci/mcp: %s's CODE never names %r" % (name, banned),
                  banned in code, False)
        # AND IT RUNS THE MODULE AS A SCRIPT.  `python -m srv.mcp` imports
        # `srv/__init__.py` first, which imports six modules of the reconciler
        # -- stdlib-only, so no DuckDB, but six modules in an image whose
        # stated contents are "the MCP surface and nothing else", and
        # `reconcile` re-exports from `ingest`, so the set only grows.
        #
        # ASSERTED ON THE ENTRYPOINT AS WELL AS THE CONTAINERFILE, and the
        # entrypoint is the half that matters: the Containerfile only COPYs the
        # file, and the `-m` would be written where the process is launched.
        # Checked here first and found by mutation -- rewriting the entrypoint
        # to `-m srv.mcp` left this test green when it looked only at the
        # Containerfile, which is a guard asserting the wrong file.
        check("oci/mcp: %s never runs it as `-m srv.mcp`" % name,
              "srv.mcp" in code, False)
    ep = os.path.join(OCI, "entrypoint-mcp.sh")
    if not os.path.isfile(ep):
        check("oci/mcp: entrypoint-mcp.sh was found", ep, "a file that exists")
    else:
        ep_code = _sh_code(open(ep, encoding="utf-8").read())
        check("oci/mcp: the entrypoint never runs it as `-m srv.mcp`",
              "srv.mcp" in ep_code, False)
        check("oci/mcp: ...and never passes -m at all",
              re.search(r"\bexec\b[^\n]*\s-m\s", ep_code) is not None, False)
        check_true("oci/mcp: ...it execs the flat script instead",
                   re.search(r"exec[^\n]*mcp\.py", ep_code) is not None)

    # THE CONTEXT, which is the half a later edit cannot get around.
    for name in sorted(mcps):
        ign = os.path.join(OCI, name + ".containerignore")
        if not os.path.isfile(ign):
            ign = os.path.join(OCI, name + ".dockerignore")
        if not os.path.isfile(ign):
            check("oci/mcp: %s has an ignore file" % name, None,
                  "an ignore file beside the Containerfile")
            continue
        body = [l.strip() for l in open(ign, encoding="utf-8").read().splitlines()
                if l.strip() and not l.strip().startswith("#")]
        check_true("oci/mcp: %s's context starts by excluding everything"
                   % os.path.basename(ign), "*" in body)
        allowed = [l[1:] for l in body if l.startswith("!")]
        check_true("oci/mcp: ...and re-includes srv/mcp.py",
                   any(a.endswith("/server/srv/mcp.py") or
                       a.endswith("server/srv/mcp.py") for a in allowed))
        # The negative, which is the point: no line re-includes the package as
        # a whole, so `store.py` is not in the context at any path.
        for a in allowed:
            check("oci/mcp: ...and no line re-includes the whole package (%s)"
                  % a, a.rstrip("*/").endswith("server/srv"), False)

    # And the module itself still is what all of this assumes: no import of a
    # sibling and no import of duckdb.  That is asserted elsewhere too, from
    # the other direction; here it is what makes the flat COPY legitimate.
    mcp_src = os.path.join(fx.SRV_DIR, "srv", "mcp.py")
    if not os.path.isfile(mcp_src):
        check("oci/mcp: srv/mcp.py was found", mcp_src, "a file that exists")
        return
    import ast as _ast
    tree = _ast.parse(open(mcp_src, encoding="utf-8").read())
    relative = [n for n in _ast.walk(tree)
                if isinstance(n, _ast.ImportFrom) and n.level]
    check("oci/mcp: srv/mcp.py imports no sibling module, so it runs flat",
          [n.module for n in relative], [])
    tops = sorted({a.name.split(".")[0]
                   for n in _ast.walk(tree) if isinstance(n, _ast.Import)
                   for a in n.names}
                  | {n.module.split(".")[0] for n in _ast.walk(tree)
                     if isinstance(n, _ast.ImportFrom) and not n.level
                     and n.module})
    check("oci/mcp: ...and imports the standard library only",
          [m for m in tops if m in ("duckdb", "srv")], [])


def test_oci_the_ingest_and_mcp_images_carry_no_duckdb():
    """THE MEASURED HALF OF THE SPLIT, ASSERTED THE WAY `usage/` ASSERTS ITS OWN.

    `usage/tests/test_all.py` has a test called "nothing under usage/ imports
    duckdb", and its shape is the one to copy: the engine is this project's
    first non-stdlib dependency and it is permitted in ONE place, so every
    boundary around it is asserted rather than described.

    Two of the four components need no engine, and that was MEASURED rather
    than assumed.  `import duckdb` appears exactly once in the whole package --
    inside `store.require()`, called from `DuckStore.con()` on the first query
    -- so nothing imports it at module level, and `serve.py` starts, binds and
    takes shipments on a stdlib-only interpreter.  `serve.py` already prints
    `engine duckdb MISSING -- stream A routes will refuse by name` for exactly
    that case.

    WHY THIS IS ABOUT AUTHORITY AND NOT ABOUT MEGABYTES.  Built and measured on
    the machine this was written on: ingest 215 MB, api 302 MB, mcp 213 MB,
    proxy 94.2 MB -- so the engine is 87 MB of the api, and the ingest and mcp
    images are ~76% and ~99% base image respectively.  The saving is real and
    modest.  What the split buys is that the process holding the write lock and
    the store read-write has no query engine in it, and the process an agent
    talks to has neither.
    """
    files = dict(_oci_containerfiles())
    engineless = {n: t for n, t in files.items()
                  if _component_of(n) in ("ingest", "mcp")}
    check_true("oci/no-engine: there were engineless Containerfiles to check",
               bool(engineless))
    check("oci/no-engine: ...and both components are represented",
          sorted({_component_of(n) for n in engineless}), ["ingest", "mcp"])

    for name, text in sorted(engineless.items()):
        code = _sh_code(text)
        # THE CODE, NOT THE PROSE.  Both of these files argue at length about
        # DuckDB and why it is absent, so a guard that matched the explanation
        # would fail on a correct file and pass on a wrong one the moment
        # somebody reworded a comment.
        for banned in ("pip install", "duckdb", "DUCKDB_VERSION"):
            check("oci/no-engine: %s's CODE never says %r" % (name, banned),
                  [l for l in code.splitlines() if banned in l], [])
        # AND THE LABEL SAYS SO, so a registry listing distinguishes "no
        # engine" from "nobody wrote a label".
        m = re.search(r'com\.claudio\.engine="([^"]*)"', text)
        check_true("oci/no-engine: %s declares an engine label" % name, bool(m))
        if m:
            check("oci/no-engine: ...and it does not name duckdb (%s)" % name,
                  "duckdb" in m.group(1).lower(), False)

    # THE STAGING SCRIPT IS THE OTHER HALF ON FreeBSD, where there is no `pip`
    # to grep for: the packages are chosen by a `case` branch, and the wrong
    # pick is not a build failure but an image quietly carrying the engine.
    stage = os.path.join(OCI, "stage-freebsd.sh")
    if not os.path.isfile(stage):
        check("oci/no-engine: stage-freebsd.sh was found", stage,
              "a file that exists")
    else:
        text = open(stage, encoding="utf-8").read()
        for comp in ("ingest", "mcp"):
            m = re.search(r"^    %s\)\n(.*?)^        ;;" % comp, text,
                          re.M | re.S)
            check_true("oci/no-engine: the %s staging branch was found" % comp,
                       m is not None)
            if m:
                pkgs = re.search(r'^\s*PKGS="([^"]*)"', m.group(1), re.M)
                check("oci/no-engine: ...and stages nothing matching duckdb",
                      [x for x in (pkgs.group(1).split() if pkgs else [])
                       if "duckdb" in x], [])

    # AND THE api DOES CARRY IT, which is what stops all of the above being a
    # statement about a project that dropped DuckDB entirely.
    apis = {n: t for n, t in files.items() if _component_of(n) == "api"}
    check_true("oci/no-engine: an api Containerfile was found", bool(apis))
    for name, text in sorted(apis.items()):
        check_true("oci/no-engine: %s pins a duckdb version" % name,
                   "ARG DUCKDB_VERSION=" in text)


def test_oci_the_proxy_sends_the_two_halves_to_two_upstreams():
    """`/sender` and `/api/` are one route each to two DIFFERENT processes.

    A single upstream serving both -- which is what this configuration had
    while the door was one image -- puts the query engine and the write lock in
    one process, which is exactly the thing the split exists to undo.  And it
    is the failure that would go unnoticed: every request would keep working.

    The other two ways to get it wrong are loud by comparison.  `/sender` at
    the api meets a 503 `no-ship`; `/api/` at the ingest service meets a 503
    `no-view`.  Both are named refusals somebody would find in an afternoon.
    Pointing both at one whole door is the silent one, so the upstream NAMES
    are asserted to be different from each other and each route to the right
    one of them.
    """
    conf = _nginx_conf("claudio.conf")
    loc = _nginx_conf("claudio-locations.conf")
    if conf is None or loc is None:
        check("oci/split-proxy: the two configurations were found", None,
              "claudio.conf and claudio-locations.conf")
        return

    ups = dict(re.findall(r"upstream\s+(\S+)\s*\{\s*server\s+([^;]+);", conf))
    check("oci/split-proxy: three upstreams, one per service that answers",
          sorted(ups), ["claudio_api", "claudio_ingest", "claudio_mcp"])
    # A DIFFERENT HOST EACH, read off the upstream bodies. Two upstream names
    # pointing at one `server` line is the same defect with more typing.
    check("oci/split-proxy: ...and no two of them name the same address",
          len(set(ups.values())), 3)

    code = "\n".join(l for l in loc.splitlines()
                      if not l.lstrip().startswith("#"))

    def target(header):
        m = re.search(r"^\s*location\s+%s\s*\{(.*?)^    \}"
                      % re.escape(header), code, re.M | re.S)
        if not m:
            return None
        p_ = re.search(r"proxy_pass\s+http://([A-Za-z0-9_.:-]+)", m.group(1))
        return p_.group(1) if p_ else None

    check("oci/split-proxy: /sender reaches the WRITER", target("= /sender"),
          "claudio_ingest")
    check("oci/split-proxy: /api/ reaches the READER", target("/api/"),
          "claudio_api")
    check("oci/split-proxy: /mcp reaches the mcp service", target("= /mcp"),
          "claudio_mcp")
    check("oci/split-proxy: and no two routes share an upstream",
          len({target("= /sender"), target("/api/"), target("= /mcp")}), 3)

    # TLS IS SHIPPED NOW AND THE CERTIFICATE PATH IS IN ONE PLACE.  It used to
    # be an example file the operator mounted, because nginx EXITS when
    # `ssl_certificate` names a file that is not there.  `entrypoint-proxy.sh`
    # answers that -- a mounted certificate if there is one, a self-signed pair
    # it generates and announces if there is not -- so the secure configuration
    # is the default rather than the one you reach after reading a paragraph.
    tls = _nginx_conf("claudio-tls.conf")
    if tls is None:
        check("oci/split-proxy: nginx/claudio-tls.conf was found", None,
              "a file that exists")
        return
    certs = re.findall(r"^\s*ssl_certificate(?:_key)?\s+(\S+);", tls, re.M)
    check("oci/split-proxy: the TLS server names exactly two files",
          len(certs), 2)
    check_true("oci/split-proxy: ...both under the read-only secret mount",
               all(c.startswith("/etc/claudio/") for c in certs))
    check_true("oci/split-proxy: ...and it includes the shared route list, "
               "rather than copying it",
               re.search(r"include\s+claudio-locations\.conf;", tls) is not None)
    # THE DOMAIN IS NOT IN THE NGINX CONFIGURATION AT ALL.  `server_name _`
    # matches any host, which is right for a single-vhost proxy and is what
    # makes this file identical on every deployment; the domain lives in one
    # environment variable, read by the entrypoint for the generated
    # certificate's CN.
    check_true("oci/split-proxy: the TLS server matches any host",
               re.search(r"server_name\s+_\s*;", tls) is not None)

    ep = os.path.join(OCI, "entrypoint-proxy.sh")
    if not os.path.isfile(ep):
        check("oci/split-proxy: entrypoint-proxy.sh was found", ep,
              "a file that exists")
        return
    ep_text = open(ep, encoding="utf-8").read()
    ep_code = _sh_code(ep_text)
    check_true("oci/split-proxy: the entrypoint reads the domain from one "
               "variable", "CLAUDIO_DOMAIN" in ep_code)
    check_true("oci/split-proxy: ...and the certificate directory from another",
               "CLAUDIO_TLS_DIR" in ep_code)
    # IT MUST NEVER OVERWRITE A CERTIFICATE THAT IS ALREADY THERE. That would
    # replace a real certificate with a self-signed one on a restart -- every
    # client's trust broken by a container coming back.
    check_true("oci/split-proxy: it generates only when BOTH files are absent",
               re.search(r"if\s+\[\s+-f\s+\"\$CERT\"\s+\]\s+&&\s+"
                         r"\[\s+-f\s+\"\$KEY\"\s+\]", ep_code) is not None)
    check_true("oci/split-proxy: ...and refuses half a pair rather than "
               "completing it", "exit 10" in ep_code)
    check_true("oci/split-proxy: ...and says the generated one is self-signed",
               "SELF-SIGNED" in ep_text)
    # The exit codes do not collide: serve.py owns 1, 3, 4, 8 and 9,
    # preflight.py owns 5, the entrypoints own 6, the mcp ambient token is 7
    # and an unknown server role is 11.
    for collide in ("exit 1", "exit 3", "exit 4", "exit 5", "exit 7",
                    "exit 8", "exit 9", "exit 11"):
        check("oci/split-proxy: the proxy entrypoint never exits %r, which is "
              "somebody else's code" % collide,
              re.search(r"%s\b" % collide, ep_code) is not None, False)


def test_oci_the_compose_stack_publishes_only_the_proxy():
    """Four services, one `ports:`, and three different answers about the store.

    This is the file an operator runs, so it is where the split either holds or
    silently does not.  Four properties, and each of them is a way the stack
    could come up looking perfectly healthy while the boundary was gone:

      * only the PROXY publishes a port.  A `ports:` on the api is an
        unauthenticated-by-default read API on the host's interfaces.
      * the store is READ-WRITE on ingest and READ-ONLY on api.  `:ro` is not
        what enforces it -- `--no-ship` and `Store(create=False)` are -- but a
        mount that was meant to say `:ro` and does not is the day somebody
        starts that image without the role and gets a second writer.
      * the mcp service gets NO volume at all.  Not the store read-only, not
        the token files.  A directory that is not mounted cannot be read.
      * the mcp service gets NO ambient token.  Its authority is the caller's
        forwarded bearer token and nothing else.
    """
    path = os.path.join(OCI, "compose.yml")
    if not os.path.isfile(path):
        check("oci/compose: server/oci/compose.yml was found", path,
              "a file that exists")
        return
    text = open(path, encoding="utf-8").read()

    # Parsed by INDENTATION rather than with a YAML library, because this
    # project has no third-party dependency outside the server's engine and a
    # test may not add one.  The shape asked of it is shallow: the service
    # names, and the block of lines under each.
    m = re.search(r"^services:\n(.*?)(?=^\S)", text, re.M | re.S)
    check_true("oci/compose: the file declares services", m is not None)
    if not m:
        return
    body = m.group(1)
    blocks, current = {}, None
    for line in body.splitlines():
        head = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if head:
            current = head.group(1)
            blocks[current] = []
        elif current and line.strip():
            blocks[current].append(line)
    check("oci/compose: four services, one per component",
          sorted(blocks), ["api", "ingest", "mcp", "proxy"])

    for name, lines in sorted(blocks.items()):
        block = "\n".join(lines)
        has_ports = re.search(r"^\s+ports:", block, re.M) is not None
        check("oci/compose: %s publishes a port iff it is the proxy" % name,
              has_ports, name == "proxy")
        if has_ports:
            maps = re.findall(r'^\s+-\s+"([^"]+)"', block, re.M)
            check_true("oci/compose: ...and every mapping it declares is "
                       "pinned to loopback",
                       bool(maps) and all(x.startswith("127.0.0.1:")
                                          for x in maps))

    def mounts(name):
        return re.findall(r"^\s+-\s+(\S+:\S+)\s*$", "\n".join(blocks[name]),
                          re.M)

    store_mounts = {n: [x for x in mounts(n) if "claudio-usage" in x]
                    for n in blocks}
    check_true("oci/compose: ingest mounts the store read-WRITE",
               len(store_mounts["ingest"]) == 1
               and not store_mounts["ingest"][0].endswith(":ro"))
    check_true("oci/compose: api mounts the store read-ONLY",
               len(store_mounts["api"]) == 1
               and store_mounts["api"][0].endswith(":ro"))
    check("oci/compose: mcp mounts NOTHING, not even the store read-only",
          mounts("mcp"), [])
    check("oci/compose: ...and declares no volumes key at all",
          re.search(r"^\s+volumes:", "\n".join(blocks["mcp"]), re.M) is not None,
          False)

    # THE TWO TOKEN FILES ARE SEPARATE AND EACH SERVICE GETS ONE.  A machine
    # that ships is not thereby entitled to read, and a reader ships nothing.
    check_true("oci/compose: ingest is given the shipping tokens and not the "
               "readers file",
               any("tokens.json" in x for x in mounts("ingest"))
               and not any("readers.json" in x for x in mounts("ingest")))
    check_true("oci/compose: api is given the readers file and not the "
               "shipping tokens",
               any("readers.json" in x for x in mounts("api"))
               and not any("tokens.json" in x for x in mounts("api")))

    check("oci/compose: the mcp service is given NO ambient token",
          re.search(r"^\s+CLAUDIO_API_TOKEN:", "\n".join(blocks["mcp"]), re.M)
          is not None, False)
    check_true("oci/compose: ...and does point at the api, not the ingest "
               "service",
               re.search(r"CLAUDIO_API:\s*http://api:", "\n".join(blocks["mcp"]))
               is not None)

    # AND THE api WAITS FOR THE WRITER TO HAVE CREATED `accounts/`.  A reader
    # over a store with no accounts directory exits 9 -- correctly -- so
    # without this the api crash-loops on a fresh store until the ingest
    # service happens to get there first. `service_started` is not enough.
    # Comments stripped first: this block carries five lines of prose
    # explaining why `service_started` is not enough, and a guard that matched
    # the explanation would pass on a file that said the right thing and did
    # the wrong one.
    api_block = "\n".join(l for l in blocks["api"]
                          if not l.lstrip().startswith("#"))
    check_true("oci/compose: api depends on ingest being HEALTHY, not merely "
               "started",
               re.search(r"ingest:\s*\n\s+condition:\s*service_healthy",
                         api_block) is not None)
    check("oci/compose: ...and never on service_started",
          "service_started" in api_block, False)


def test_oci_the_mcp_route_left_the_store_images_and_the_imports_say_so():
    """THE PAIRED GUARD IS SPENT, AND THIS IS WHAT REPLACES IT.

    There used to be an IFF here: "the door image excludes `srv/mcp.py` exactly
    when `serve.py` has stopped importing it".  Its own docstring said it would
    turn red on the day the route moved.  That day came -- `POST /mcp` is the
    mcp component's, served by `mcp.serve_http` in its own image -- so the
    condition it balanced no longer has two coherent states to balance, and an
    IFF with one live side is a test that can only ever assert the obvious.

    Two flat assertions instead, and each one is a thing that could regress on
    its own.

    FIRST, `serve.py` IMPORTS NO `mcp`.  The route it briefly served looped
    back to `http://127.0.0.1:<its own port>` -- which after the split is the
    WRONG PROCESS -- and, worse, put an MCP surface inside the process holding
    the DuckDB handle, the store's query layer and (when ship is enabled) the
    writer's lock.  An MCP surface's whole stated authority is one bearer token
    handed to it per request.

    SECOND, THE MCP IMAGE'S CONTEXT CARRIES `mcp.py` AND NOT THE PACKAGE.  That
    is the structural half: `store.py`, `duck.py`, `query.py`, `api.py` and
    `serve.py` are not in the build context at all, so a `COPY server/srv/`
    added to that Containerfile fails the build by name.

    What is deliberately NOT asserted is that the store images exclude
    `mcp.py`.  They carry it, on purpose: excluding one file from a
    `COPY server/srv/` needs a second COPY list, and a list is a place to
    forget.  What matters is that nothing in those images RUNS it, which is the
    third assertion below.
    """
    serve_src = os.path.join(fx.SRV_DIR, "srv", "serve.py")
    if not os.path.isfile(serve_src):
        check("oci/mcp-route: srv/serve.py was found", serve_src,
              "a file that exists")
        return
    text = open(serve_src, encoding="utf-8").read()
    imports_mcp = re.search(r"^\s*from\s+(\.|srv)\s+import\b[^\n]*\bmcp\b",
                            text, re.M)
    check("oci/mcp-route: serve.py imports no mcp module", bool(imports_mcp),
          False)
    # AND SERVES NO SUCH ROUTE EITHER.  The import is how it would come back;
    # the route is what would be wrong.  Asserted on the code and not the prose,
    # because this file explains at length why the route is not here.
    code = "\n".join(l for l in text.splitlines()
                      if not l.lstrip().startswith("#"))
    check("oci/mcp-route: ...and declares no /mcp path constant",
          "PATH_MCP" in code, False)
    check("oci/mcp-route: ...and no handler for one",
          re.search(r"def _mcp\b", code) is not None, False)

    # THE CONTEXT, which is the half a later edit cannot get around.
    for name, _t in _oci_containerfiles():
        if _component_of(name) != "mcp":
            continue
        ign = os.path.join(OCI, name + ".containerignore")
        if not os.path.isfile(ign):
            ign = os.path.join(OCI, name + ".dockerignore")
        if not os.path.isfile(ign):
            check("oci/mcp-route: %s has an ignore file" % name, None,
                  "an ignore file beside the Containerfile")
            continue
        body = [l.strip() for l in
                open(ign, encoding="utf-8").read().splitlines()
                if l.strip() and not l.strip().startswith("#")]
        allowed = [l[1:] for l in body if l.startswith("!")]
        check_true("oci/mcp-route: %s re-includes srv/mcp.py"
                   % os.path.basename(ign),
                   any(a.endswith("server/srv/mcp.py") for a in allowed))
        for a in allowed:
            check("oci/mcp-route: ...and no line re-includes the whole package "
                  "(%s)" % a, a.rstrip("*/").endswith("server/srv"), False)

    # THE STORE IMAGES CARRY IT AND MUST NEVER RUN IT.  `entrypoint.sh` had an
    # `mcp` mode that exec'd `-m srv.mcp`; it is deleted rather than left to
    # rot into a `No module named` traceback in an image whose context no
    # longer has to carry the module for that reason.
    ep = os.path.join(OCI, "entrypoint.sh")
    if not os.path.isfile(ep):
        check("oci/mcp-route: entrypoint.sh was found", ep,
              "a file that exists")
        return
    ep_code = _sh_code(open(ep, encoding="utf-8").read())
    check("oci/mcp-route: the shared entrypoint has no mcp mode",
          "mcp" in ep_code.lower(), False)


def test_oci_the_staging_script_takes_a_component_and_refuses_an_unknown_one():
    """One script, three package lists, and no default.

    The component decides which packages go in the image, and the wrong pick is
    not a build failure -- it is an mcp image carrying DuckDB and the store's
    query layer, which is the boundary the split exists to draw.  So it is the
    first argument, it is required, and an unknown value is a NAMED refusal
    listing the three.

    Exercised rather than described: the refusal happens before anything is
    fetched, so this test needs no network and no FreeBSD.
    """
    path = os.path.join(OCI, "stage-freebsd.sh")
    if not os.path.isfile(path):
        check("oci/stage: stage-freebsd.sh was found", path,
              "a file that exists")
        return
    text = open(path, encoding="utf-8").read()

    # The refusal, run for real.  `/bin/sh` is enough: the script exits in its
    # `case` before it reaches curl, python or the network.
    proc = subprocess.run(["/bin/sh", path, "nonsense", "FreeBSD:15:amd64",
                           "amd64", os.path.join(tempfile.gettempdir(),
                                                 "stage-refusal-check")],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    check("oci/stage: an unknown component is refused", proc.returncode != 0,
          True)
    err = proc.stderr.decode("utf-8", "replace")
    check_true("oci/stage: ...and the refusal names the component it was given",
               "nonsense" in err)
    for comp in COMPONENTS:
        check_true("oci/stage: ...and names %s as one of the four" % comp,
                   comp in err)
    check_true("oci/stage: ...and it refused before staging anything",
               not os.path.isdir(os.path.join(tempfile.gettempdir(),
                                              "stage-refusal-check")))

    # AND IT HAS NO DEFAULT, which cannot be shown by running it: give the
    # parameter a default and every wrong call still refuses, because the
    # arguments shift and the ABI ends up in a required slot.  What a default
    # really costs is the bare `stage-freebsd.sh` staging one of the three
    # without being asked, and the only place that is visible is the
    # assignment.  Found by mutation: `${1:-door}` left this test green.
    assign = re.search(r'^COMPONENT="\$\{1(:[-?])', text, re.M)
    check("oci/stage: the component parameter is required, with no default",
          assign.group(1) if assign else None, ":?")

    # WHAT EACH COMPONENT STAGES, read out of the script's own `case`.
    # Derived from the file rather than restated: the point of the check is
    # that the mcp list has no DuckDB in it, and a copy of the list here would
    # be a second place for that to be true.
    def branch(name):
        m = re.search(r"^    %s\)\n(.*?)^        ;;" % re.escape(name),
                      text, re.M | re.S)
        return m.group(1) if m else None

    for comp, wants, forbids in (
            # THE INGEST BRANCH STAGES NO ENGINE, AND THAT IS THE HALF THIS
            # LOOP EXISTS FOR NOW.  `import duckdb` happens in exactly one
            # place in the package -- inside `store.require()`, on the first
            # query -- so the ship path never reaches it, and staging it here
            # would put a C++ engine in the one process that holds the write
            # lock.
            ("ingest", ["python312"], ["duckdb"]),
            ("api", ["py312-duckdb", "python312"], []),
            ("mcp", ["python312"], ["duckdb"]),
            ("proxy", ["nginx"], ["duckdb", "python"])):
        b = branch(comp)
        if b is None:
            check("oci/stage: the %s branch was found" % comp, None,
                  "a case branch")
            continue
        pkgs = re.search(r'^\s*PKGS="([^"]*)"', b, re.M)
        check_true("oci/stage: the %s branch names its packages" % comp,
                   pkgs is not None)
        if not pkgs:
            continue
        named = pkgs.group(1).split()
        check("oci/stage: %s stages %s" % (comp, ", ".join(wants)),
              named, wants)
        for bad in forbids:
            check("oci/stage: %s stages nothing matching %r" % (comp, bad),
                  any(bad in n for n in named), False)

    # The mcp branch checks its prune against `mcp.py` and not against `srv/`,
    # which is what stops the check demanding the very dependency the image
    # exists to shed.
    b = branch("mcp")
    if b:
        check_true("oci/stage: the mcp branch checks its imports against "
                   "mcp.py alone", "mcp.py" in b)


def _nginx_conf(name):
    path = os.path.join(OCI, "nginx", name)
    if not os.path.isfile(path):
        return None
    return open(path, encoding="utf-8").read()


def test_oci_the_proxy_serves_the_three_routes_and_keeps_the_api_prefix():
    """The front end is what makes the three images one system.

        /sender  -> door  POST /v1/ship
        /api/    -> door  GET  /api/v1/*
        /mcp     -> mcp   POST /mcp

    THE `/api/` PREFIX IS LOAD-BEARING AND IS THE FIRST THING SOMEBODY WILL
    TIDY.  The door describes itself with ABSOLUTE paths -- OpenAPI `paths`,
    `capabilities.endpoints[].path`, and the `remedy` on every refusal -- so a
    matching external prefix makes all three correct as they stand.  Rename it
    and the MCP surface breaks first and SILENTLY, because it generates its
    entire tool list from those paths: every tool would be built against a path
    that 404s from outside.  So the assertion is that this location has no URI
    part in its `proxy_pass` and that nothing rewrites the prefix.
    """
    text = _nginx_conf("claudio-locations.conf")
    if text is None:
        check("oci/proxy: nginx/claudio-locations.conf was found", None,
              "a file that exists")
        return
    code = "\n".join(l for l in text.splitlines()
                      if not l.lstrip().startswith("#"))

    def block(header):
        m = re.search(r"^\s*location\s+%s\s*\{(.*?)^    \}"
                      % re.escape(header), code, re.M | re.S)
        return m.group(1) if m else None

    ship = block("= /sender")
    api = block("/api/")
    mcp = block("= /mcp")
    check_true("oci/proxy: there is a /sender location", ship is not None)
    check_true("oci/proxy: there is an /api/ location", api is not None)
    check_true("oci/proxy: there is a /mcp location", mcp is not None)

    if ship:
        check_true("oci/proxy: /sender reaches the door's /v1/ship",
                   re.search(r"proxy_pass\s+http://\S+/v1/ship;", ship)
                   is not None)
    if api:
        # NO URI part: `proxy_pass http://upstream;` passes the request URI
        # through unchanged, and `proxy_pass http://upstream/;` does not.
        m = re.search(r"proxy_pass\s+(\S+);", api)
        check_true("oci/proxy: /api/ has a proxy_pass", m is not None)
        if m:
            target = m.group(1)
            # `http://upstream` passes the request URI through unchanged;
            # `http://upstream/` REPLACES the matched prefix with `/`, which
            # silently serves `/api/v1/search` to the door as `/v1/search`.
            # Written first as a count of slashes after `rstrip("/")`, which is
            # satisfied by the trailing slash it exists to catch -- found by
            # mutation, and this is the shape that cannot be: the whole value
            # must be scheme and host, with nothing after them at all.
            check("oci/proxy: ...with NO URI part, not even a bare `/`, so the "
                  "/api/ prefix reaches the door unchanged",
                  re.match(r"^https?://[A-Za-z0-9_.:-]+$", target) is not None,
                  True)
            check("oci/proxy: ...and it is not rewritten either",
                  "rewrite" in api, False)
    if mcp:
        check_true("oci/proxy: /mcp reaches the mcp service's /mcp",
                   re.search(r"proxy_pass\s+http://\S+/mcp;", mcp) is not None)
        # Buffered, a streamed reply arrives all at once when the upstream
        # closes, which to a client is indistinguishable from a hang.
        check_true("oci/proxy: ...with proxy_buffering off",
                   re.search(r"proxy_buffering\s+off\s*;", mcp) is not None)

    # `/healthz` asks for no token and names the store root, the engine's
    # availability and the free bytes on the filesystem.  It is an operator's
    # question and is deliberately not published; the configuration says so
    # where an operator reads it rather than simply omitting the route.
    health = re.search(r"location\s+=\s+/healthz\s*\{(.*?)\}", code, re.S)
    check_true("oci/proxy: /healthz is answered explicitly, not by omission",
               health is not None)
    if health:
        check("oci/proxy: ...and it is not proxied anywhere",
              "proxy_pass" in health.group(1), False)
    check_true("oci/proxy: ...and the file says why it is not exposed",
               "asks for no token" in text or "no token" in text)

    # A 404 WITH NO ROUTE OUT OF IT is how a client author concludes the server
    # is broken, so the catch-all names the routes that do exist.
    catch = re.search(r"location\s+/\s*\{(.*?)\}", code, re.S)
    check_true("oci/proxy: there is a catch-all", catch is not None)
    if catch:
        body = catch.group(1)
        for named in ("/sender", "/api/v1", "/mcp"):
            check_true("oci/proxy: ...and it names %s" % named, named in body)


def test_oci_the_proxy_body_limits_are_the_door_s_own_numbers():
    """Derived from `serve.py`, because a second copy is a second place to
    forget -- and both ways of getting it wrong are invisible at the time.

    A SMALLER limit at the proxy means a shipper meets a 413 from nginx
    carrying none of the door's own explanation of what it refused and why.  A
    LARGER one means nginx buffers the whole body before the door refuses it.
    Equal, the refusal comes from the process that knows why.
    """
    text = _nginx_conf("claudio-locations.conf")
    if text is None:
        check("oci/proxy: nginx/claudio-locations.conf was found", None,
              "a file that exists")
        return
    # Read off the MODULE, not out of the source text: `serve` is already
    # imported here, so the number compared is the number the door will
    # actually enforce -- and it stays right if the constant is ever written as
    # an expression, a `min()` or a value read from somewhere else.
    # TWO MODULES, BECAUSE THE TWO LIMITS BELONG TO TWO PROCESSES NOW.  The
    # 32m is the ingest service's `serve.MAX_BODY`; the 1m is the mcp service's
    # `mcp.MAX_BODY`, which moved there with the route.  Read off the MODULES
    # rather than out of either source text, so the numbers compared are the
    # numbers those processes will enforce.
    door = getattr(serve, "MAX_BODY", None)
    mcp_max = getattr(mcp, "MAX_BODY", None)
    check_true("oci/proxy: serve.py declares MAX_BODY", isinstance(door, int))
    check_true("oci/proxy: mcp.py declares MAX_BODY", isinstance(mcp_max, int))
    check("oci/proxy: and serve.py no longer declares one for a route it does "
          "not serve", hasattr(serve, "MCP_MAX_BODY"), False)
    if not isinstance(door, int) or not isinstance(mcp_max, int):
        return
    limits = re.findall(r"client_max_body_size\s+(\d+)m\s*;", text)
    check_true("oci/proxy: the configuration sets client_max_body_size",
               len(limits) >= 2)
    if len(limits) >= 2:
        check("oci/proxy: the server-wide limit is the door's MAX_BODY",
              int(limits[0]) * 1024 * 1024, door)
        check("oci/proxy: and the /mcp limit is the mcp service's MAX_BODY",
              int(limits[1]) * 1024 * 1024, mcp_max)


def test_oci_the_proxy_runs_unprivileged_and_says_what_that_costs():
    """A process that is not root cannot bind below 1024.

    So the proxy listens on 8080 and 8443 and the PUBLISH mapping is where they
    become 80 and 443.  Running it as root to get the low port would trade the
    one privilege this stack does not need for a number the runtime maps for
    free -- and the `user` directive would then be meaningful, which is the
    other half of the same decision.
    """
    main = _nginx_conf("nginx.freebsd.conf")
    shared = _nginx_conf("claudio.conf")
    if main is None or shared is None:
        check("oci/proxy: the two configurations were found", None,
              "nginx.freebsd.conf and claudio.conf")
        return
    code = "\n".join(l for l in main.splitlines()
                      if not l.lstrip().startswith("#"))
    # No `user` directive: with a non-root master nginx warns on every start,
    # and a warning nobody can act on trains people to ignore warnings.
    check("oci/proxy: the main configuration sets no `user`",
          re.search(r"^\s*user\s+\S+\s*;", code, re.M) is not None, False)
    # `daemon off;` belongs to the ENTRYPOINT, and the two are mutually
    # exclusive: nginx refuses to start on a duplicate `daemon` directive.
    check("oci/proxy: ...and no `daemon` directive either, because the "
          "entrypoint passes it",
          re.search(r"^\s*daemon\s+", code, re.M) is not None, False)
    # The pid file's compiled-in path is `/var/run/nginx.pid`, which this
    # image's uid cannot write and this build has no RUN to chown.
    check_true("oci/proxy: the pid file is moved somewhere the uid can write",
               re.search(r"^\s*pid\s+/var/tmp/nginx/", code, re.M) is not None)
    listens = re.findall(r"^\s*listen\s+(?:\[::\]:)?(\d+)", shared, re.M)
    check_true("oci/proxy: it listens on at least one port", bool(listens))
    for port in listens:
        check_true("oci/proxy: %s is above 1024, which an unprivileged process "
                   "can bind" % port, int(port) > 1024)

    files = dict(_oci_containerfiles())
    for name, text in sorted(files.items()):
        if _component_of(name) != "proxy":
            continue
        users = [l.split()[1] for l in text.splitlines()
                 if l.strip().upper().startswith("USER ")]
        check("oci/proxy: %s runs as one numeric uid:gid" % name,
              users, ["65534:65534"])


def test_oci_the_mcp_entrypoint_refuses_an_ambient_token_over_http():
    """A SERVED MCP MUST NOT SEE MORE THAN THE PERSON CALLING IT.

    Over HTTP every caller presents their own bearer token and the door applies
    that reader's scope, from the same `readers.json` that gates `/api/v1/*`.
    An ambient `CLAUDIO_API_TOKEN` in the container's environment breaks that in
    the one direction that matters: a caller who sends NO credential inherits
    the container's, and the surface starts answering questions the caller was
    never entitled to ask.

    Refused rather than ignored, because the two differ for the operator.
    Ignoring it leaves a token in `docker inspect` and in the compose file,
    doing nothing, waiting for somebody to "fix" the code that ignores it.
    """
    path = os.path.join(OCI, "entrypoint-mcp.sh")
    if not os.path.isfile(path):
        check("oci/mcp-entry: entrypoint-mcp.sh was found", path,
              "a file that exists")
        return
    text = open(path, encoding="utf-8").read()
    code = _sh_code(text)
    check_true("oci/mcp-entry: it refuses when a token is in the environment",
               "CLAUDIO_API_TOKEN" in code and "exit 7" in code)
    check_true("oci/mcp-entry: ...and the message says which transport the "
               "variable belongs to", "stdio" in text)
    # The stdio transport KEEPS it, and that is where it is correct: there the
    # launcher is the caller, so one process means one identity.
    check_true("oci/mcp-entry: stdio is still reachable", "stdio)" in code)
    # It knows nothing about the store, and must not learn: this component has
    # no store, no readers file and no tokens file.
    for forbidden in ("--readers", "--tokens", "/var/lib/claudio-usage",
                      "preflight", "--no-api"):
        check("oci/mcp-entry: its CODE never says %r" % forbidden,
              forbidden in code, False)
    # And it execs, rather than forking a wrapper that would swallow the exit
    # status of the thing it launched.
    check_true("oci/mcp-entry: it execs the module", "exec " in code)
    # The exit codes do not collide: `serve.py` owns 1, 3 and 4, `preflight.py`
    # owns 5, both entrypoints own 6 for a missing interpreter, and 7 is this
    # refusal.  An operator told to look for 4 must not meet a 4 from here.
    for collide in ("exit 1", "exit 3", "exit 4", "exit 5"):
        check("oci/mcp-entry: it never exits with %r, which is somebody "
              "else's code" % collide, collide in code, False)


def test_oci_the_workflow_publishes_both_images_and_pairs_base_with_abi():
    """The matrix is the only place the target list is written down.

    Two properties, and the second is the one that cannot be caught anywhere
    else.  A FreeBSD image's package ABI must track its base's MAJOR version --
    a 14.x image takes its packages from the FreeBSD:14 repository -- and
    crossing them is SILENT at build time, because nothing executes during a
    FreeBSD build.  Measured from the real published layers: 14.4 carries
    `libutil.so.9`, 15.1 carries `libutil.so.10`, and `python3.12` from the
    FreeBSD:14 repository asks for the former.  The build catches it with
    `pkgindex.py shlibs`; this catches it in the file a human edits.

    Parsed with a regex and not a YAML library on purpose: `server/` has one
    third-party dependency and it is DuckDB, in `srv/store.py`, and a test that
    imported PyYAML would make the suite need a second one to run at all.
    """
    if not os.path.isfile(WORKFLOW):
        check("oci/ci: .github/workflows/images.yml was found", WORKFLOW,
              "a file that exists")
        return
    text = open(WORKFLOW, encoding="utf-8").read()
    # Full-line `#` comments removed for the negative checks below, and only
    # for those: the workflow EXPLAINS why a single emulated multi-platform
    # build does not work, so a grep of the raw text matches the paragraph
    # warning against the thing it is looking for.  The same trap `_sh_code`
    # exists for one language over.
    code = "\n".join(l for l in text.splitlines()
                      if not l.lstrip().startswith("#"))

    rows = re.findall(
        r"\{\s*slug:\s*(\S+?),\s*base:\s*'([^']+)',\s*arch:\s*(\w+),"
        r"\s*abi:\s*'([^']+)'\s*\}", text)
    check_true("oci/ci: the FreeBSD matrix was found at all", len(rows) >= 2)
    seen_arch = set()
    for slug, base, arch, abi in rows:
        parts = abi.split(":")
        check("oci/ci: %s abi is FreeBSD:<major>:<arch>" % slug, len(parts), 3)
        if len(parts) != 3:
            continue
        check("oci/ci: %s (base %s) takes packages from the matching major "
              "repository" % (slug, base), parts[1], base.split(".")[0])
        check("oci/ci: %s arch %s matches its abi" % (slug, arch), parts[2],
              {"amd64": "amd64", "arm64": "aarch64"}.get(arch, arch))
        check_true("oci/ci: %s slug names its base" % slug,
                   base.replace(".", "") in slug.replace(".", ""))
        seen_arch.add(arch)
    check("oci/ci: both architectures are built", sorted(seen_arch),
          ["amd64", "arm64"])

    # Linux is built one architecture per NATIVE runner, and that is measured
    # rather than preferred: a single emulated multi-platform build installs
    # the amd64 DuckDB wheel and then segfaults importing it.  A workflow that
    # went back to one buildx run would publish an image whose engine has never
    # been imported.
    check_true("oci/ci: Linux amd64 is built on an amd64 runner",
               "ubuntu-latest" in text)
    check_true("oci/ci: Linux arm64 is built on an arm64 runner",
               "ubuntu-24.04-arm" in text)
    check("oci/ci: no emulated multi-platform Linux build",
          "linux/amd64,linux/arm64" in code, False)

    # vfs is not a performance knob: with overlay, buildah leaves
    # `user.overlay.origin` -- an xattr with an EMPTY value -- on directories a
    # COPY merges into, and podman on FreeBSD indexes value[0] when applying
    # it, so the PULL panics.
    check_true("oci/ci: the storage driver is vfs", "STORAGE_DRIVER: vfs" in text)
    check_true("oci/ci: ...and the file says why, so it is not tidied away",
               "index out of range [0]" in text)

    # Which branches CI runs on, and the rule is the opposite of what it looks
    # like. `pre-release` is the PUBLISHED squash and is exactly where CI
    # belongs: it is the branch that produces the images anyone pulls.
    #
    # `accounts-and-tags` is the local development branch. It is deliberately
    # never pushed -- its detailed history is the reason the published branch is
    # squashed at all -- so a trigger naming it would fire on nothing, and the
    # name sitting in a workflow is an invitation to push it. That is the
    # invariant worth pinning, and it is about the PRIVATE branch, not the
    # public one.
    branches = re.search(r"branches:\s*\[([^\]]*)\]", text)
    check_true("oci/ci: the push trigger names its branches", bool(branches))
    if branches:
        named = branches.group(1)
        check("oci/ci: CI runs on the published branch", "pre-release" in named, True)
        check("oci/ci: and never names the private development branch",
              "accounts-and-tags" in named, False)

    # THE STAGING STEP NAMES ITS COMPONENT.  `stage-freebsd.sh` takes the
    # component first and has no default, so a workflow written before that
    # argument existed fails on its next run -- loudly, which is the right
    # direction, but the suite is where it should be caught rather than a red
    # CI job on a branch somebody is trying to publish from.
    #
    # A MATRIX EXPRESSION IS ALSO AN ANSWER, AND THEN THE AXIS IS WHAT GETS
    # CHECKED.  The FreeBSD job stages all four components, so the literal in
    # that line is `${{ matrix.component }}` and asserting "one of the four"
    # against the string `${{` would fail on a correct workflow.  What matters
    # is unchanged -- that the argument is there and that it can only ever
    # expand to a component -- so an expression is accepted and the axis it
    # names is then required to be exactly the four.  A `component:` axis
    # holding three of them, which is how a component silently stops being
    # built, fails here rather than in a registry listing nobody reads.
    # The alternation is ordered: an expression carries a SPACE inside its
    # braces (`${{ matrix.component }}`), so a bare `\S+` stops at `"${{` and
    # the assertion then fails on a correct workflow -- which is how this read
    # for one run.
    stage_calls = re.findall(
        r"stage-freebsd\.sh\s+(?:\\\s*)?(\"?\$\{\{[^}]*\}\}\"?|\S+)", code)
    check_true("oci/ci: the workflow stages the FreeBSD tree", bool(stage_calls))
    for got in stage_calls:
        expr = re.match(r'^"?\$\{\{\s*matrix\.(\w+)\s*\}\}"?$', got)
        if expr:
            axis = expr.group(1)
            check("oci/ci: ...naming its component from a matrix axis",
                  axis, "component")
            m = re.search(r"^\s*component:\s*\[([^\]]*)\]", text, re.M)
            check_true("oci/ci: ...and that axis is declared", m is not None)
            if m:
                named = sorted(x.strip() for x in m.group(1).split(",") if x.strip())
                check("oci/ci: ...and it is exactly the four components",
                      named, sorted(COMPONENTS))
        else:
            check_true("oci/ci: ...naming a component (%s)" % got,
                       got.strip('"') in COMPONENTS)

    check_true("oci/ci: a testing tag is published", ":testing" in text)
    check_true("oci/ci: an immutable per-commit tag is published too",
               "sha-${short}" in text)


def test_oci_the_workflow_gates_the_images_on_the_suites_and_pins_its_actions():
    """Three properties of `images.yml` that nothing else can see.

    Regex and not PyYAML, for the reason the test above gives: `server/` has
    one third-party dependency and it is DuckDB, and a test that imported a
    YAML library would make the suite need a second one to run at all.

    1. THE IMAGE JOBS CANNOT RUN ON A RED TREE.  `needs: test` on both builders
       is the whole of it, and it is asserted per job rather than by grepping
       the file for the word: `needs:` appears five times in this workflow and
       four of them are about something else.

    2. THE SUITE THAT RUNS IS THE STRICT ONE.  `server/tests/test_all.py` exits
       non-zero on a self-skip by itself -- it was caught exiting 0 over 918
       assertions that had never run -- and `--require-duckdb` is passed on top
       of that, because a redundant guard is only redundant while the other one
       holds.

    3. EVERY ACTION IS PINNED TO A COMMIT.  A major tag such as `@v5` is a
       moving reference its publisher re-points at will, and these jobs carry
       `packages: write` and a registry token; `@v5` is therefore an unreviewed
       third party with the ability to push an image under this repository's
       name.  Asserted as a SHAPE -- forty hex characters -- and not as a list
       of the four actions used today, because a list would say nothing about
       the fifth one somebody adds.
    """
    if not os.path.isfile(WORKFLOW):
        check("oci/ci: .github/workflows/images.yml was found", WORKFLOW,
              "a file that exists")
        return
    text = open(WORKFLOW, encoding="utf-8").read()

    # SCOPED TO THE TEXT AFTER `jobs:`, and that is not fastidiousness: `on:`
    # has children at the same two-space indent, so the unscoped version of
    # the line below reported `push`, `pull_request`, `schedule` and
    # `workflow_dispatch` as jobs and then failed each of them for declaring no
    # permissions -- a guard confidently asserting something about the wrong
    # part of the file.
    jtext = text[text.index("\njobs:\n") + 1:]

    # A job block runs from `^  <name>:` to the next line starting with two
    # spaces and a lower-case letter -- which is the next job, and never one of
    # the `  # ===== name =====` banners between them.  `header` is everything
    # before `steps:`, so a `needs:` found in it is THIS job's.
    def job_block(name):
        m = re.search(r"^  %s:\n(.*?)(?=^  [a-z]|\Z)" % re.escape(name),
                      jtext, re.M | re.S)
        return m.group(1) if m else None

    jobs = re.findall(r"^  ([a-z][\w-]*):\n", jtext, re.M)
    check("oci/ci: the workflow declares the five jobs", sorted(jobs),
          ["freebsd", "linux", "manifest", "smoke", "test"])

    # ---- 1. the image jobs are gated on the suites ------------------------
    for name in ("linux", "freebsd"):
        block = job_block(name)
        if block is None:
            check("oci/ci: the %s job was found" % name, name, "a job")
            continue
        header = block.split("    steps:")[0]
        needs = re.search(r"^    needs:\s*(.+)$", header, re.M)
        check_true("oci/ci: the %s job declares needs:" % name, needs is not None)
        if needs:
            check_true("oci/ci: ...and it is the test job, so a red suite "
                       "publishes nothing at all -- not even an immutable tag",
                       "test" in needs.group(1))

    # ---- 2. the test job runs the release gate, strictly ------------------
    tb = job_block("test")
    if tb is None:
        check("oci/ci: the test job was found", "test", "a job")
    else:
        check_true("oci/ci: the test job runs `make check`, so a suite added "
                   "to the Makefile is a suite CI runs", "make check" in tb)
        check_true("oci/ci: ...with --require-duckdb, so a run that skipped "
                   "918 assertions cannot pass CI", "--require-duckdb" in tb)
        check_true("oci/ci: ...and it installs the engine at the version the "
                   "images pin, read out of the Containerfile",
                   "ARG DUCKDB_VERSION=" in tb)
        # The two Containerfiles are separate files with separate ARGs and
        # nothing but this compares them.
        check_true("oci/ci: ...and asserts the two Containerfiles pin the "
                   "same engine", "Containerfile.freebsd.api" in tb
                   and "Containerfile.linux.api" in tb)
        # jq alone gates mixed mode, JSON validity and every usage-recording
        # test in `test.sh`, and its absence is a printed skip and an exit 0.
        check_true("oci/ci: ...and refuses to run without jq, whose absence "
                   "test.sh handles by skipping and passing", "jq" in tb)

    # ---- 3. every action is pinned to a commit ----------------------------
    uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)(.*)$", text, re.M)
    check_true("oci/ci: the workflow uses at least one action", len(uses) >= 4)
    for ref, rest in uses:
        _, _, version = ref.partition("@")
        check_true("oci/ci: `%s` is pinned to a commit, not a moving tag"
                   % ref.split("@")[0],
                   bool(re.match(r"^[0-9a-f]{40}$", version)))
        # The trailing comment is what makes a 40-character hex string
        # readable.  (It is also what Dependabot would rewrite, if this
        # repository configured it, which it does not.)
        check_true("oci/ci: ...and the pin says which version it is",
                   bool(re.search(r"#\s*v?\d", rest)))

    # ---- least privilege --------------------------------------------------
    #
    # Per job, not once for the file. A workflow-level `permissions:` block
    # would grant every job the union, and the test and smoke jobs have no
    # business holding a token that can push an image.
    for name in jobs:
        block = job_block(name) or ""
        header = block.split("    steps:")[0]
        # COMMENT LINES INCLUDED, and that is the whole correctness of it.
        # The first version matched a run of `      key: value` lines and
        # stopped at the first line that was not one -- so a permission
        # written UNDER a comment inside the block was simply not seen, and
        # the "asks for nothing beyond" assertion below would have passed over
        # a job quietly granted `id-token: write`.  The block now runs to the
        # first line that is not indented six spaces or more.
        perms = re.search(r"^    permissions:\n((?:      .*\n)+)",
                          header, re.M)
        check_true("oci/ci: the %s job declares its own permissions" % name,
                   perms is not None)
        if not perms:
            continue
        granted = dict(re.findall(r"^      ([A-Za-z][\w-]*):\s*(\S+)\s*$",
                                  perms.group(1), re.M))
        # `id-token` and `attestations` are what `actions/attest-build-
        # provenance` needs, and they are allowed ONLY on the jobs that push
        # something to attest.  Nothing published is otherwise
        # distinguishable, on the registry, from anything a leaked
        # `write:packages` token pushed under the same moving name -- and this
        # server has no TLS, no token expiry and no rate limiting, so the
        # artefact's authenticity is the only integrity control in the chain.
        signing = {"linux", "freebsd", "manifest"}
        allowed = {"contents", "packages"}
        if name in signing:
            allowed |= {"id-token", "attestations"}
        check("oci/ci: %s asks for nothing beyond %s"
              % (name, ", ".join(sorted(allowed))),
              sorted(set(granted) - allowed), [])
        check("oci/ci: %s does not ask for write on contents" % name,
              granted.get("contents"), "read")
        if name in ("test", "smoke"):
            check("oci/ci: %s cannot push an image" % name,
                  granted.get("packages"), None)
            check("oci/ci: %s cannot sign anything either" % name,
                  sorted(set(granted) & {"id-token", "attestations"}), [])
        else:
            check("oci/ci: %s can attest what it pushes" % name,
                  (granted.get("id-token"), granted.get("attestations")),
                  ("write", "write"))
    check("oci/ci: there is no workflow-level permissions block granting "
          "every job the union",
          bool(re.search(r"^permissions:", text, re.M)), False)

    # ---- the tag scheme ---------------------------------------------------
    mb = job_block("manifest") or ""
    # `:latest` means "the newest RELEASE". This repository has no tags, so it
    # must not exist at all yet -- a `:latest` pointing at a development build
    # is a wrong image served under the one name that promises it is not.
    tagcase = mb.find("refs/tags/*)")
    check_true("oci/ci: the manifest job has a refs/tags/* branch",
               tagcase != -1)
    for m in re.finditer(r"^.*:latest.*$", mb, re.M):
        if m.group(0).lstrip().startswith("#"):
            continue                        # the paragraph explaining the rule
        check_true("oci/ci: :latest is pushed only from the release branch, "
                   "so it does not exist before there is a release",
                   tagcase != -1 and m.start() > tagcase)
    # The moving tags name ONE branch: with two branches in the push trigger an
    # unscoped `:testing` means "whichever built last".
    check_true("oci/ci: the moving tags are scoped to one branch",
               "DEV_BRANCH" in text and 'refs/heads/${DEV_BRANCH}' in mb)

    # ---- the registry credential ------------------------------------------
    #
    # ASSERTED AS A SHAPE, not as a list of the three login lines that exist
    # today, for the same reason the action pin is.  A `-p` value sits in the
    # process's argv: readable by any other process in the job for the
    # lifetime of the command, and liable to be echoed by a `set -x` somebody
    # adds while debugging.  GitHub's log masking hides a secret in OUTPUT and
    # does nothing whatever about argv.  Two of the three logins used `-p` and
    # the third -- in the same file -- already used `--password-stdin`.
    for pat, why in ((r'-p\s+"\$\{\{\s*secrets\.',
                      "a secret passed with -p is in the process's argv"),
                     (r'--password\s+"\$\{\{\s*secrets\.',
                      "a secret passed with --password is in the argv too")):
        offenders = [l.strip() for l in text.splitlines()
                     if re.search(pat, l) and not l.strip().startswith("#")]
        check("oci/ci: no run: block hands a secret on the command line -- %s"
              % why, offenders, [])
    logins = [l.strip() for l in text.splitlines()
              if re.search(r"\b(docker|buildah) login\b", l)
              and not l.strip().startswith("#")]
    check_true("oci/ci: the logins this asserts about were found",
               len(logins) >= 3)
    check("oci/ci: and every one of them reads the token from stdin",
          [l for l in logins if "--password-stdin" not in l], [])

    # ---- the base images --------------------------------------------------
    #
    # `python:3.12-slim-bookworm` and `freebsd-runtime:<v>` are MOVING
    # references their publishers re-point at will, and they are the largest
    # executable input in either image -- measured moving within eight days in
    # August 2026.  This file already argues that exact point at length for
    # GitHub Actions and pins all four of them to commits; the same reasoning
    # was not applied to the input contributing orders of magnitude more code,
    # so two builds of one commit produced different images with nothing able
    # to say so.  Each build job now resolves the tag to a digest, builds FROM
    # that digest, and records both.
    for job in ("linux", "freebsd"):
        b = job_block(job) or ""
        check_true("oci/ci: the %s job resolves its base to a digest" % job,
                   "steps.base.outputs.digest" in b
                   and "steps.base.outputs.ref" in b)
        check_true("oci/ci: ...builds FROM that digest rather than the tag",
                   "BASE_REF=${{ steps.base.outputs.ref }}" in b)
        check_true("oci/ci: ...and records it as image.base.digest",
                   "base.digest" in b or "BASE_DIGEST=" in b)
        check_true("oci/ci: the %s job stamps claudio's own VERSION on the "
                   "image, not a third party's" % job,
                   "VERSION=${{ steps.ver.outputs.version }}" in b)
        check_true("oci/ci: ...read from the claudio script rather than "
                   "written down here",
                   "sed -n 's/^VERSION=//p' claudio" in b)
        check_true("oci/ci: and derives image.source from this repository, so "
                   "a fork does not name someone else's",
                   "SOURCE_URL=https://github.com/${{ github.repository }}" in b)

    # ---- the manifest lists are assembled from digests ---------------------
    #
    # They were assembled from the `sha-<short>-…` TAG NAMES, resolved BY NAME
    # minutes after being pushed, on the strength of those tags being
    # immutable.  They are not: any re-run at the same commit re-points them,
    # and the concurrency group is per-REF with `cancel-in-progress: false`, so
    # a branch push and a tag push at one commit are two runs writing the same
    # names -- either able to assemble the other's images under `:testing`.
    adds = [l.strip() for l in mb.splitlines()
            if "manifest add" in l and not l.strip().startswith("#")]
    check_true("oci/ci: the manifest job assembles something at all",
               len(adds) >= 3)
    check("oci/ci: and every reference it adds is a DIGEST, never a tag",
          [l for l in adds if "@$(cat" not in l], [])
    for job in ("linux", "freebsd"):
        b = job_block(job) or ""
        check_true("oci/ci: the %s job writes the digest it pushed into the "
                   "marker the manifest job reads" % job,
                   "steps.push.outputs.digest" in b)

    # ---- provenance --------------------------------------------------------
    att = re.findall(r"uses:\s*actions/attest-build-provenance@([0-9a-f]{40})",
                     text)
    check_true("oci/ci: what is published is attested", len(att) >= 2)
    check_true("oci/ci: ...and the attesting action is pinned to a commit "
               "like every other one", all(len(x) == 40 for x in att))
    # The whole expression, not the first token: `${{ steps... }}` has a
    # space after the braces, so a `\S+` capture returns the literal `${{`
    # and every assertion about the value is then about that.
    subjects = [m.strip() for m in
                re.findall(r"^\s*subject-digest:\s*(.+?)\s*$", text, re.M)]
    check_true("oci/ci: the attestations name a subject at all", bool(subjects))
    check("oci/ci: and every one of them is a DIGEST, never a moving tag",
          [x for x in subjects
           if "outputs.digest" not in x and "outputs.m_" not in x], [])
    names = [m.strip() for m in
             re.findall(r"^\s*subject-name:\s*(.+?)\s*$", text, re.M)]
    check("oci/ci: each attestation is scoped to this image's own repository",
          [x for x in names if "IMAGE_NAME" not in x], [])



def _wf_jobs(text):
    """Every job in `images.yml`, as `{name: block}`.

    The same scoping the test above explains at length: `on:` has children at
    the same two-space indent, so anything derived from the whole file reports
    `push` and `schedule` as jobs.
    """
    jtext = text[text.index("\njobs:\n") + 1:]
    out = {}
    for m in re.finditer(r"^  ([a-z][\w-]*):\n(.*?)(?=^  [a-z]|\Z)",
                         jtext, re.M | re.S):
        out[m.group(1)] = m.group(2)
    return out


def _wf_steps(block):
    """One job's steps, as a list of text blocks.

    A step starts at a line that is exactly six spaces and `- `; everything up
    to the next such line -- `run:` body, `with:` block and comments included
    -- belongs to it.  Splitting on steps is what makes "this condition is on
    THAT step" answerable at all, and the guard below is entirely about which
    step a condition sits on.
    """
    m = re.search(r"^    steps:\n(.*)\Z", block, re.M | re.S)
    if not m:
        return []
    body = m.group(1)
    starts = [mm.start() for mm in re.finditer(r"^      - ", body, re.M)]
    return [body[s:(starts[i + 1] if i + 1 < len(starts) else len(body))]
            for i, s in enumerate(starts)]


def test_oci_the_workflow_publishes_every_component_and_not_one_of_them():
    """The split is only real if all four halves are PUBLISHED.

    THIS IS THE GUARD THE PREVIOUS SHAPE COULD NOT BE.  An earlier revision
    built all four components on every FreeBSD cell and pushed exactly one of
    them: `matrix.component == 'api'` sat on the login, the push, the
    attestation and the marker steps, so 18 of 24 cells did the whole build,
    ran every check and threw the image away.  Every assertion in the sibling
    test above passed -- the component axis was declared, `stage-freebsd.sh`
    named it, `:testing` and `sha-${short}` were both in the file -- because
    every one of them asks about the axis or about a string, and none of them
    asks what happens to the artefact at the END of a cell.

    So this one is about the steps.  A condition that narrows to one component
    is legitimate on a step that CHECKS something only that component has (the
    api is the only image with an engine to compare a version against); it is
    the bug itself on a step that pushes, attests or records.

    The other half is the naming.  `IMAGE_NAME` is a PREFIX now and every
    published reference is `${IMAGE_NAME}-<component>`; a bare `${IMAGE_NAME}`
    in a destination would publish four different programs to one repository,
    where `:testing` cannot be resolved without also saying which program you
    meant -- and a manifest list has no field for that, its platform keys being
    already spent on os and architecture.
    """
    if not os.path.isfile(WORKFLOW):
        check("oci/ci4: .github/workflows/images.yml was found", WORKFLOW,
              "a file that exists")
        return
    text = open(WORKFLOW, encoding="utf-8").read()
    jobs = _wf_jobs(text)

    # ---- the name is a prefix, and every destination appends a component ---
    m = re.search(r"^  IMAGE_NAME:\s*(.+?)\s*$", text, re.M)
    check_true("oci/ci4: the workflow declares IMAGE_NAME", m is not None)
    if m:
        check("oci/ci4: ...and it is a prefix, not the retired whole-door "
              "repository -- `claudio-server` means ship AND read in one "
              "process, and no image is that any more",
              m.group(1).endswith("claudio-server"), False)

    # Every use of IMAGE_NAME outside its own declaration and outside a comment
    # must be immediately followed by a component: either the literal `-` and
    # one of the four, or `-${{ matrix.component }}`.
    bad = []
    for line in text.splitlines():
        if line.strip().startswith("#") or re.match(r"^\s*IMAGE_NAME:", line):
            continue
        for mm in re.finditer(r"\$\{\{\s*env\.IMAGE_NAME\s*\}\}|\$\{IMAGE_NAME\}",
                              line):
            rest = line[mm.end():]
            if re.match(r"^-\$\{\{\s*matrix\.component\s*\}\}", rest):
                continue
            if re.match(r"^-(%s)\b" % "|".join(COMPONENTS), rest):
                continue
            if re.match(r"^-\$\{comp\}", rest):
                continue
            bad.append(line.strip())
    check("oci/ci4: every published reference names a component's own "
          "repository", bad, [])

    # ---- no publishing step is scoped to one component --------------------
    #
    # `matrix.component == '<x>'` on a step that pushes, signs or records is
    # the exact shape of the regression: everything is built and one thing is
    # kept.
    publishing = ("buildah push", "docker push", "attest-build-provenance",
                  "upload-artifact", "steps.push.outputs.digest",
                  "buildah login", "docker login")
    for job in ("linux", "freebsd"):
        block = jobs.get(job)
        if block is None:
            check("oci/ci4: the %s job was found" % job, job, "a job")
            continue
        steps = _wf_steps(block)
        check_true("oci/ci4: the %s job's steps were found" % job,
                   len(steps) >= 5)
        offenders = []
        for step in steps:
            cond = re.search(r"^\s*if:\s*(.+?)\s*$", step, re.M)
            if not cond or "matrix.component ==" not in cond.group(1):
                continue
            # Comments explaining the rule are not the rule.
            live = "\n".join(l for l in step.splitlines()
                             if not l.strip().startswith("#"))
            for needle in publishing:
                if needle in live:
                    offenders.append((job, needle,
                                      cond.group(1).strip()[:60]))
        check("oci/ci4: no step in the %s job publishes for one component "
              "only -- building four and keeping one is a matrix that looks "
              "like %s cells and ships a quarter of them" % (job, "24"),
              offenders, [])

        # And the build itself is driven by the axis rather than by a name.
        live = "\n".join(l for l in block.splitlines()
                         if not l.strip().startswith("#"))
        check_true("oci/ci4: the %s job builds Containerfile.<os>.<component> "
                   "from the matrix, not one hardcoded component" % job,
                   "${{ matrix.component }}" in live)
        # SCOPED TO THE BUILD AND THE IGNORE FILE, and deliberately not to
        # every mention of a Containerfile: both jobs also READ
        # `Containerfile.<os>.api` to get the pinned DuckDB version, which is
        # correct -- the api is the only component with an engine to pin, and a
        # version read out of the file that installs it is one place rather
        # than two.  What must come from the axis is what gets BUILT.
        for line in live.splitlines():
            if not re.search(r"(-f|--ignorefile)\s", line):
                continue
            for comp in COMPONENTS:
                check("oci/ci4: the %s job's build takes its Containerfile "
                      "from the axis, never a hardcoded %s" % (job, comp),
                      ("Containerfile.linux.%s" % comp) in line
                      or ("Containerfile.freebsd.%s" % comp) in line, False)

    # ---- the markers carry the component ----------------------------------
    #
    # The manifest job builds one set of lists per component and discovers its
    # targets by globbing these names.  A marker without the component in it
    # would make two components' digests collide in one file name, and the
    # loser would simply not be published -- silently, since the glob would
    # still match.
    for job in ("linux", "freebsd"):
        block = jobs.get(job) or ""
        markers = re.findall(r'> "out/([^"]+)"', block)
        check_true("oci/ci4: the %s job writes a marker" % job, bool(markers))
        for name in markers:
            check_true("oci/ci4: ...and it names the component (%s)" % name,
                       "matrix.component" in name)

    # ---- every component is attested, in its own repository ---------------
    subjects = set()
    for step in _wf_steps(jobs.get("manifest") or ""):
        if "attest-build-provenance" not in step:
            continue
        mm = re.search(r"^\s*subject-name:\s*(.+?)\s*$", step, re.M)
        if mm:
            subjects.add(mm.group(1).strip())
    named = set()
    for s in subjects:
        for comp in COMPONENTS:
            if s.endswith("-" + comp):
                named.add(comp)
    check("oci/ci4: the manifest job attests a list for every component -- an "
          "attestation on three of four leaves the fourth indistinguishable, "
          "on the registry, from an image a leaked token pushed",
          sorted(named), sorted(COMPONENTS))

    # ---- the smoke job runs every component too ---------------------------
    #
    # It is the ONLY place a FreeBSD binary is executed anywhere in this
    # workflow.  Scoped to the api, a completely broken staged tree for the
    # other three passes every static check and publishes.
    smoke = jobs.get("smoke") or ""
    mm = re.search(r"^\s*component:\s*\[([^\]]*)\]", smoke, re.M)
    check_true("oci/ci4: the smoke job declares a component axis",
               mm is not None)
    if mm:
        got = sorted(x.strip() for x in mm.group(1).split(",") if x.strip())
        check("oci/ci4: ...and it is exactly the four, because nothing else "
              "in this workflow executes a FreeBSD instruction",
              got, sorted(COMPONENTS))


def test_oci_buildah_writes_the_credential_where_the_attestation_reads_it():
    """"No credentials found for registry ghcr.io", and neither side was wrong.

    `buildah login` writes `${XDG_RUNTIME_DIR}/containers/auth.json`.
    `actions/attest-build-provenance` with `push-to-registry: true` pushes the
    attestation itself, and its OCI client reads the DOCKER credential chain --
    `$DOCKER_CONFIG/config.json`, else `~/.docker/config.json` -- and has no
    option for the other path.  So a job that logs in with buildah and then
    attests has a valid credential in a file the action does not open.

    The file FORMAT is the same on both sides; only the default LOCATION
    differs.  So the fix is one flag plus two exported variables, and it is
    asserted PER JOB rather than as a string somewhere in the file: the Linux
    job needs none of it, because `docker login` already writes exactly the
    file the action reads, and asserting the fix globally would have passed on
    a file where only the job that did not need it had it.
    """
    if not os.path.isfile(WORKFLOW):
        check("oci/ci-auth: .github/workflows/images.yml was found", WORKFLOW,
              "a file that exists")
        return
    text = open(WORKFLOW, encoding="utf-8").read()
    checked = 0
    for name, block in _wf_jobs(text).items():
        live = "\n".join(l for l in block.splitlines()
                         if not l.strip().startswith("#"))
        if "buildah login" not in live:
            continue
        if "attest-build-provenance" not in live:
            continue          # nothing to hand a credential to
        checked += 1
        login = [l for l in live.splitlines() if "buildah login" in l]
        check("oci/ci-auth: %s logs in exactly once" % name, len(login), 1)
        if login:
            check_true("oci/ci-auth: %s names the Docker-style authfile on "
                       "the login itself -- $GITHUB_ENV does not reach the "
                       "step that sets it" % name,
                       "--authfile" in login[0] and ".docker/config.json" in login[0])
        for var in ("DOCKER_CONFIG", "REGISTRY_AUTH_FILE"):
            check_true("oci/ci-auth: %s exports %s, so buildah's later pushes "
                       "and the action read one file" % (name, var),
                       ('echo "%s=' % var) in live)
    # THE COUNT IS ASSERTED, because a guard whose loop body never runs is a
    # guard that reports success about nothing -- and this one selects its
    # jobs by two substrings, either of which a rename would take away.
    check("oci/ci-auth: the jobs this asserts about were found", checked, 2)

def test_oci_the_readme_says_what_about_the_freebsd_image_is_unverified():
    """An untested assertion in a README is an untested assertion.

    THIS GUARD ONCE ASSERTED THE OPPOSITE OF WHAT IT CLAIMED TO.  Its check was
    `"never been built" in flat`, labelled "it says no FreeBSD image has been
    built here" -- and when the README was corrected to say the image builds
    fine with Docker BuildKit and has never been built *by buildah*, the
    substring was still there, so the assertion went on passing while its own
    label became false.  That is the "prose about the thing rather than the
    thing" defect this file records twice already.

    So the three claims are now asserted separately, because they are three
    different facts and only one of them changed: `buildah` has never built it
    (the tool CI uses, and the one three properties in this repository are
    specifically about), it has never been RUN anywhere (no FreeBSD kernel
    here, so `import duckdb` on FreeBSD is unexecuted), and nothing has been
    pushed or pulled.
    """
    readme = os.path.join(os.path.dirname(HERE), "README.md")
    if not os.path.isfile(readme):
        check("oci/docs-bsd: server/README.md was found", readme,
              "a file that exists")
        return
    flat = " ".join(open(readme, encoding="utf-8").read().split())
    for needed in ("freebsd/freebsd-runtime", "buildah", "LD_LIBRARY_PATH",
                   "py312-duckdb", "STORAGE_DRIVER"):
        check_true("oci/docs-bsd: the README names %s" % needed, needed in flat)
    check_true("oci/docs-bsd: it says buildah has never built it -- which is "
               "the tool CI uses, and the one --format oci, --annotation and "
               "STORAGE_DRIVER: vfs are all specifically about",
               "never been built by `buildah`" in flat
               or "never been built **by `buildah`**" in flat)
    check_true("oci/docs-bsd: it says the image has never been RUN, which is "
               "the claim a successful build most invites people to skip over",
               "never been run" in flat.lower())
    check_true("oci/docs-bsd: and that nothing has been pushed or pulled",
               "pushed or pulled" in flat or "been pushed" in flat)
    check_true("oci/docs-bsd: it labels the unverified claims as such",
               "UNVERIFIED" in flat or "Unverified" in flat)
    check_true("oci/docs-bsd: it says the workstation half is still not "
               "containerised for FreeBSD either",
               "make install" in flat)

def test_mcp_tools_are_generated_from_the_served_spec_and_hold_no_copy():
    """The tool list is built from `/api/v1/openapi`, not written down here.

    A copy is a place to forget. This package already derives its route list,
    its refusal vocabulary and its coverage bases from source rather than
    restating them, and a tool list is the same hazard one layer out: a
    hardcoded one describes an endpoint the server may not have, which a model
    then calls and is refused by, with no way to tell a stale client from a
    broken server.
    """
    spec = api.openapi(["/api/v1/" + r for r in
                        ("search", "aggregate", "health", "diagnostics")])
    tools = mcp.tools_from_spec(spec)
    names = sorted(t["name"] for t in tools)
    check("mcp: a tool per interesting route", names, ["aggregate", "search"])
    check_true("mcp: operator routes are not handed to a model",
               "health" not in names and "diagnostics" not in names)

    search = [t for t in tools if t["name"] == "search"][0]
    props = search["inputSchema"]["properties"]
    check_true("mcp: parameters come from the spec, with their types",
               props["since"]["type"] == "number" and
               props["account"]["type"] == "string")

    # The warning is on EVERY tool, not once in a server description a model may
    # never read and certainly not in a README.
    for t in tools:
        check_true("mcp: %s warns that the empties differ" % t["name"],
                   "THREE DIFFERENT ANSWERS" in t["description"])


def test_mcp_a_refusal_is_an_error_not_an_empty_result():
    """`no-data`, `filtered-to-nothing` and `unanswerable` reach the model as
    three different things, and the last is flagged as an error.

    An agent that reads a refusal as an empty list concludes there is no data
    when there is, which is this project's cardinal sin with a model in the
    loop instead of a person. `isError` is what stops it: a successful empty
    answer and a refusal must not arrive looking alike.
    """
    ok = mcp._result({"outcome": "ok", "result": {"rows": []}})
    check("mcp: an ok answer is not an error", ok["isError"], False)

    nodata = mcp._result({"outcome": "no-data",
                          "empty": {"kind": "no-data"}})
    check("mcp: an empty corpus is an ANSWER, not an error",
          nodata["isError"], False)
    check_true("mcp: ...and says which empty it is",
               "no-data" in nodata["content"][0]["text"])

    ref = mcp._result({"outcome": "unanswerable",
                       "refusal": {"reason": "crosses-accounts",
                                   "remedy": "pass ?account=<uuid>"}})
    check("mcp: a refusal IS an error", ref["isError"], True)
    check_true("mcp: ...and carries the remedy through to the model",
               "pass ?account=" in ref["content"][0]["text"])


@contextlib.contextmanager
def _running_mcp(base):
    """`mcp.serve_http` in a thread, on a real port, shut down afterwards.

    Same capture as `_running_door` and for the same reason: the `ready`
    callback is handed the bound port, so there is nothing in it to shut down,
    and a subclass left installed would mean the suite tested its own wrapper
    from then on.
    """
    box = {}
    started = threading.Event()
    real = mcp.ThreadingHTTPServer

    class Captured(real):
        def __init__(self, *a, **k):
            real.__init__(self, *a, **k)
            box["httpd"] = self

    mcp.ThreadingHTTPServer = Captured

    def _run():
        try:
            mcp.serve_http(base, "127.0.0.1", 0,
                           ready=lambda port: (box.__setitem__("port", port),
                                               started.set()),
                           quiet=True)
        finally:
            started.set()

    err = io.StringIO()
    old = sys.stderr
    sys.stderr = err
    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    started.wait(20)
    sys.stderr = old
    try:
        yield box
    finally:
        if "httpd" in box:
            box["httpd"].shutdown()
        thread.join(20)
        mcp.ThreadingHTTPServer = real


MCP_MESSAGES = (
    {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
     "params": {"name": "accounts", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
     "params": {"name": "nonsense", "arguments": {}}},
    {"jsonrpc": "2.0", "id": 5, "method": "no/such/method"},
)


def test_mcp_both_transports_answer_identically():
    """One `handle()`, two front ends, and the point is that they cannot drift.

    Stdio is what a desktop client speaks and what composes with `docker run
    -i`; Streamable HTTP is what sits behind the proxy beside the api.  A tool
    that existed on one and not the other, or a refusal fixed on one only, is
    exactly the drift this project spends its comments avoiding -- and it is
    invisible, because whoever is using the other transport simply gets a
    worse answer.

    Driven against a REAL api over a real socket, and compared reply for reply:
    the same five messages, the same token, byte-identical JSON out.
    """
    root, _store, _a = ui_store()
    with open(os.path.join(root, "readers.json"), "w", encoding="utf-8") as fh:
        json.dump({"r-all": serve.ALL_ACCOUNTS}, fh)
    with open(os.path.join(root, "tokens.json"), "w", encoding="utf-8") as fh:
        json.dump({"t": fx.uuid_of("alpha")}, fh)

    with _running_door(root, ship_enabled=False) as (dbox, _out):
        base = "http://127.0.0.1:%d" % dbox["port"]

        # -- stdio ----------------------------------------------------------
        stdin = io.StringIO("".join(json.dumps(m) + "\n"
                                    for m in MCP_MESSAGES))
        stdout = io.StringIO()
        mcp.serve(mcp.Api(base, "r-all"), stdin=stdin, stdout=stdout)
        over_stdio = [json.loads(l) for l in stdout.getvalue().splitlines()
                      if l.strip()]

        # -- http -----------------------------------------------------------
        over_http = []
        with _running_mcp(base) as mbox:
            url = "http://127.0.0.1:%d%s" % (mbox["port"], mcp.PATH)
            for msg in MCP_MESSAGES:
                req = urllib.request.Request(
                    url, data=json.dumps(msg).encode("utf-8"), method="POST")
                req.add_header("Authorization", "Bearer r-all")
                req.add_header("Content-Type", "application/json")
                with urllib.request.urlopen(req, timeout=20) as fh:
                    over_http.append(json.loads(fh.read()))

    check("mcp-transports: stdio answered every message",
          len(over_stdio), len(MCP_MESSAGES))
    check("mcp-transports: http answered every message",
          len(over_http), len(MCP_MESSAGES))

    # TWO CALLS TO ONE API DIFFER IN THE CLOCK AND IN A COUNTER, AND THAT IS
    # NOT THE TRANSPORTS DISAGREEING.  Nothing is cached -- a window's state is
    # a function of `now`, so a report derived at T and served at T+2h reports
    # as open a window that closed an hour ago -- and the measured cost travels
    # in every payload.  So a second call legitimately carries a later `now`, a
    # different `derived_ms`, one more `derivations` and an older
    # `last_sample_age_s`.
    #
    # THE DIFFERING PATHS ARE ENUMERATED RATHER THAN THE VALUES BLANKED, which
    # is the whole strength of this assertion.  Blanking a list of keys and
    # then comparing hides any OTHER difference that happens to sit under one
    # of them; listing what actually differed and requiring the set to be
    # exactly the volatile four means a genuine divergence anywhere else -- a
    # tool missing, a refusal worded differently, a token not forwarded -- is a
    # named failure with its JSON path in the message.
    VOLATILE = ("now", "derived_ms", "derivations", "last_sample_age_s")

    def differing(x, y, path=""):
        if type(x) is not type(y):
            return [path or "/"]
        if isinstance(x, dict):
            out = []
            for k in sorted(set(x) | set(y)):
                if k not in x or k not in y:
                    out.append(path + "/" + str(k))
                else:
                    out.extend(differing(x[k], y[k], path + "/" + str(k)))
            return out
        if isinstance(x, list):
            if len(x) != len(y):
                return [path + "[len]"]
            out = []
            for i, (p_, q_) in enumerate(zip(x, y)):
                out.extend(differing(p_, q_, "%s[%d]" % (path, i)))
            return out
        return [] if x == y else [path]

    # The tool results are JSON documents inside a string, so they are parsed
    # before comparison -- otherwise every difference collapses into "the text
    # differs" and says nothing about where.
    def opened(reply):
        r = reply.get("result")
        if isinstance(r, dict) and isinstance(r.get("content"), list):
            out = json.loads(json.dumps(reply))
            for item in out["result"]["content"]:
                if item.get("type") == "text":
                    try:
                        item["text"] = json.loads(item["text"])
                    except ValueError:
                        pass
            return out
        return reply

    paths = differing([opened(r) for r in over_stdio],
                      [opened(r) for r in over_http])
    unexpected = [pp for pp in paths
                  if pp.rsplit("/", 1)[-1].split("[")[0] not in VOLATILE]
    check("mcp-transports: the two transports differ in NOTHING but the clock "
          "and the derivation counter", unexpected, [])
    # And the negative that keeps the assertion above from being vacuous: the
    # walker must be able to SEE a difference, or "no unexpected paths" is a
    # statement about a function that always returns [].
    check_true("mcp-transports: ...and the walker can see one when there is one",
               differing({"a": 1}, {"a": 2}) == ["/a"])
    # The comparison is only worth anything if the replies have content in
    # them: two empty lists agree perfectly.  So the shapes are named too.
    by_id = {r.get("id"): r for r in over_stdio}
    check_true("mcp-transports: ...over a real tool list",
               len(by_id.get(2, {}).get("result", {}).get("tools", [])) > 5)
    check_true("mcp-transports: ...a real tool call",
               "content" in by_id.get(3, {}).get("result", {}))
    check_true("mcp-transports: ...an unknown tool, refused",
               by_id.get(4, {}).get("result", {}).get("isError") is True)
    check_true("mcp-transports: ...and an unknown method",
               by_id.get(5, {}).get("error", {}).get("code") == -32601)


def test_mcp_a_spec_it_cannot_read_is_an_error_not_an_empty_tool_list():
    """THE SILENT FAILURE THE HTTP TRANSPORT WOULD HAVE INTRODUCED.

    The tool list is built from `/api/v1/openapi`, and that route sits behind
    the same reader auth as everything else -- so over HTTP, where the
    credential arrives on the request, the ordinary first failure is a 401,
    whose envelope has no `result` key at all.  The line this replaces was
    `(api.get(...) or {}).get("result") or {}`, which turns a 401, a 503, an
    unreachable host and a renamed `/api/` prefix alike into `{}` -- and `{}`
    into an EMPTY TOOL LIST, served with `"jsonrpc": "2.0"` and no error on it.
    A model reading that concludes the server has no tools.

    Three ways in, and each is named rather than flattened.  The third is the
    interesting one: a spec that parsed, carried paths, and matched none of
    `TOOLS` is PREFIX DRIFT -- somebody serving the API under a different
    external path -- which is precisely the failure the proxy configuration
    says breaks the MCP "first and silently".
    """
    class Refusing(object):
        def __init__(self, env):
            self.env = env

        def get(self, path, params=None):
            return self.env

    unauth = {"outcome": "unanswerable",
              "refusal": {"reason": "unauthorised",
                          "detail": "no usable reader token on this request",
                          "remedy": "send `Authorization: Bearer <token>`"}}
    tools, refusal = mcp.tools_for(Refusing(unauth))
    check("mcp-spec: a refused spec yields no tools", tools, [])
    check("mcp-spec: ...and says so by name", refusal.get("reason"), "no-spec")
    check_true("mcp-spec: ...carrying the api's own remedy",
               "Authorization" in refusal.get("remedy", ""))

    empty = mcp.tools_for(Refusing({"result": {"paths": {}}}))
    check("mcp-spec: a spec with no paths is no-spec's sibling, not silence",
          empty[1].get("reason"), "no-tools")
    drift = mcp.tools_for(Refusing({"result": {"paths": {
        "/read/v1/search": {"get": {}}, "/read/v1/accounts": {"get": {}}}}}))
    check("mcp-spec: a renamed prefix is named as drift, not answered with []",
          drift[1].get("reason"), "no-tools")
    check_true("mcp-spec: ...and the remedy says the prefix is the cause",
               "prefix" in drift[1].get("remedy", ""))

    # AND THE HANDLER TURNS IT INTO A JSON-RPC ERROR, on both the list and the
    # call -- not an empty array, and not a tool result with `isError` on it
    # either: the tool never ran, and saying it did would be a second wrong
    # answer on top of the first.
    for method, params in (("tools/list", None),
                           ("tools/call", {"name": "search", "arguments": {}})):
        msg = {"jsonrpc": "2.0", "id": 9, "method": method}
        if params:
            msg["params"] = params
        reply = mcp.handle(Refusing(unauth), msg)
        check("mcp-spec: %s over a refused spec is an ERROR" % method,
              "error" in reply, True)
        check("mcp-spec: ...and carries no result at all (%s)" % method,
              "result" in reply, False)
        check("mcp-spec: ...at the application-error code (%s)" % method,
              reply["error"]["code"], -32000)
        check("mcp-spec: ...with the refusal attached (%s)" % method,
              reply["error"]["data"]["reason"], "no-spec")

    # `initialize` IS ANSWERED WITHOUT TOUCHING THE API, and that is not an
    # oversight: it is a handshake about THIS process, and a client that cannot
    # complete it cannot be told anything else -- including why the api is
    # unreachable.
    class Exploding(object):
        def get(self, path, params=None):
            raise AssertionError("initialize must not call the api")

    reply = mcp.handle(Exploding(), {"jsonrpc": "2.0", "id": 1,
                                     "method": "initialize"})
    check("mcp-spec: initialize needs no api at all",
          reply["result"]["protocolVersion"], mcp.PROTOCOL)


def test_mcp_the_http_transport_holds_no_token_of_its_own():
    """Its authority is exactly the caller's, and that is the whole component.

    Over stdio the launcher IS the caller, so an ambient `CLAUDIO_API_TOKEN` is
    right: one process, one client, one identity.  Served over HTTP it is a
    privilege escalation in the one direction that matters -- a caller who
    sends NO credential inherits the process's, and the surface starts
    answering questions the caller was never entitled to ask.

    Refused rather than ignored, and refused IN THE PROCESS THAT WOULD LEAK IT.
    `entrypoint-mcp.sh` refuses it too, and that is not a duplicate for its own
    sake: the entrypoint has a documented escape hatch, so the shell check is
    the one that can be walked around and this one is not.
    """
    seen = []

    class Recorder(object):
        """A stand-in api that records which token reached it."""

        def __init__(self, base, token=None, timeout=None):
            seen.append(token)
            self.base, self.token = base, token

        def get(self, path, params=None):
            return {"result": {"paths": {}}}

    real_api = mcp.Api
    mcp.Api = Recorder
    try:
        with _running_mcp("http://127.0.0.1:1") as box:
            url = "http://127.0.0.1:%d%s" % (box["port"], mcp.PATH)

            def post(token):
                req = urllib.request.Request(
                    url, data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
                    method="POST")
                if token is not None:
                    req.add_header("Authorization", token)
                req.add_header("Content-Type", "application/json")
                try:
                    with urllib.request.urlopen(req, timeout=20) as fh:
                        return fh.status, json.loads(fh.read())
                except urllib.error.HTTPError as exc:
                    return exc.code, json.loads(exc.read())

            post("Bearer caller-one")
            check("mcp-http: the caller's token is what reaches the api",
                  seen[-1], "caller-one")
            post("Bearer caller-two")
            check("mcp-http: ...and a second caller gets their own",
                  seen[-1], "caller-two")
            post(None)
            check("mcp-http: a caller with no credential gets NONE, never an "
                  "ambient one", seen[-1], None)
            post("Basic dXNlcjpwdw==")
            check("mcp-http: a non-bearer scheme is not smuggled through",
                  seen[-1], None)

            # AND IT SPEAKS ONE ROUTE.  A GET, or any other path, is a named
            # refusal that says where the data actually lives -- a 404 with no
            # route out of it is how a client author concludes the server is
            # broken.
            req = urllib.request.Request(url, method="GET")
            try:
                urllib.request.urlopen(req, timeout=20)
                status, doc = 200, {}
            except urllib.error.HTTPError as exc:
                status, doc = exc.code, json.loads(exc.read())
            check("mcp-http: GET /mcp is a named refusal",
                  (status, doc.get("refusal", {}).get("reason")),
                  (404, "no-such-route"))
            check_true("mcp-http: ...naming the route that does exist",
                       mcp.PATH in doc["refusal"]["remedy"])
    finally:
        mcp.Api = real_api

    # THE AMBIENT-TOKEN REFUSAL, run rather than read.
    env = dict(os.environ)
    env["CLAUDIO_API_TOKEN"] = "ambient"
    proc = subprocess.run(
        [sys.executable, os.path.join(fx.SRV_DIR, "srv", "mcp.py"),
         "--http", "--host", "127.0.0.1", "--port", "0",
         "--api", "http://127.0.0.1:1"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    check("mcp-http: an ambient token in --http mode exits 7",
          proc.returncode, 7)
    err = proc.stderr.decode("utf-8", "replace")
    check_true("mcp-http: ...and the message names the transport it belongs to",
               "stdio" in err)
    check("mcp-http: ...and nothing bound", "listening" in err, False)


def test_mcp_holds_no_authority_of_its_own():
    """It is an HTTP client of the read API, and that is the whole of what it
    can do.

    Handing it the DuckDB handle would have been fewer moving parts and a
    privilege escalation: an agent with a database handle is bounded by nothing
    the operator configured, where a reader token's scope is exactly the
    accounts someone chose. A transport failure is reported as a refusal with a
    remedy rather than raised, because a model cannot act on a traceback.
    """
    src = open(os.path.join(fx.SRV_DIR, "srv", "mcp.py"),
               encoding="utf-8").read()
    for forbidden in ("import duckdb", "from . import store", "from . import duck",
                      "from . import query"):
        check_true("mcp: does not reach past the API (%s)" % forbidden,
                   forbidden not in src)
    check_true("mcp: the token is the only credential it holds",
               "Authorization" in src and "Bearer" in src)

    dead = mcp.Api("http://127.0.0.1:1", token="t", timeout=1)
    env = dead.get("/api/v1/search")
    check("mcp: an unreachable door is a refusal, not a traceback",
          env["outcome"], "unanswerable")
    check_true("mcp: ...naming what to check",
               "reachable" in env["refusal"]["remedy"])


def test_a_key_carries_its_own_account_so_nobody_hunts_a_uuid():
    """`<account-uuid>.<secret>` says which account it ships for.

    The step this deletes was the entire complaint about setting the server up.
    A token's value had to EQUAL the shipping account's account_uuid -- the door
    refuses the batch otherwise -- and that uuid lives in `.claude.json` on the
    workstation, which is not a machine the person configuring the server can
    see. So they copied a 36-character string between hosts and got a 409 with
    no clue in it when they got it wrong.
    """
    uuid = fx.uuid_of("alpha")
    key = uuid + ".k7Qm3vX9pR2wL8nT4bY6cH1sD5fG0jZa"

    acct, secret = serve.split_key(key)
    check("key: the left half is the account", acct, uuid)
    check("key: the right half is the secret", secret,
          "k7Qm3vX9pR2wL8nT4bY6cH1sD5fG0jZa")

    # A bare list is the shape to write now: one line each, nothing to look up.
    t = serve.Tenants(mapping=[key])
    check("key: a list of keys needs no account column", t.resolve(key), uuid)
    check("key: and the account is what it says", t.tenants(), [uuid])

    # The map still works, because it is what is deployed and because a plain
    # opaque token has nowhere to say which account it means.
    old = serve.Tenants(mapping={"plain-token": uuid})
    check("key: an old plain token still resolves", old.resolve("plain-token"),
          uuid)
    check("key: and an unknown one still does not", old.resolve("nope"), None)


def test_a_key_is_never_guessed_at():
    """Anything that is not exactly `<valid-uuid>.<secret>` is left alone.

    A token that merely CONTAINS a dot must not be reinterpreted as naming an
    account it does not name: that would file one tenant's records under
    another's, silently, which is worse than any refusal.
    """
    for bad in ("no-dots-at-all",
                "not-a-uuid.k7Qm3vX9pR2wL8nT4bY6cH1sD5fG0jZa",
                fx.uuid_of("alpha") + ".short",
                fx.uuid_of("alpha") + ".has spaces in it",
                "." + fx.uuid_of("alpha")):
        acct, secret = serve.split_key(bad)
        check("key: %r is not read as a key" % bad[:28], (acct, secret),
              (None, bad))

    # A list entry that is not self-describing is refused BY NAME, because the
    # shape carries the meaning and a silent skip would leave a machine
    # unable to ship with nothing saying why.
    t = serve.Tenants(mapping=["plain-token-in-a-list"])
    check("key: a plain token in a list is refused", len(t.refused), 1)
    check_true("key: ...and told which form it needs",
               "object form" in t.refused[0][1])
    blob = repr(t.refused)
    check("key: the refusal never echoes the token",
          "plain-token-in-a-list" in blob, False)


TESTS = ([v for k, v in sorted(globals().items()) if k.startswith("test_")]
         + _duck_tests())


# ---------------------------------------------------------------------------
# the summary, and why a skipping run announces itself twice
# ---------------------------------------------------------------------------

# Measured on this tree, same source, two interpreters:
#
#   no duckdb   1164 passed, 0 failed, 44 skipped
#   duckdb      2082 passed, 0 failed,  0 skipped
#
# 918 assertions -- 45% of the suite -- did not run in the first case.  They are
# ANNOUNCED, so this is not the silent failure this project keeps auditing for;
# but "1164 passed, 0 failed" is a sentence a reader acts on, and the 44 SKIP
# lines scroll off the top of a terminal while the summary does not.  So the
# summary carries the count AND a banner printed after it, which is the last
# thing on screen and names the interpreter that could not import the engine.
#
# The banner is prefixed `!!` rather than starting a line with `SKIP`, because
# `mutate.py` collects baseline skips with `startswith("SKIP")` and a banner
# that looked like one would inflate its refusal count.
BANNER_RULE = "!! " + "=" * 68


def _skip_banner(out=None):
    """The reason and the remedy, after the summary, or None if nothing skipped.

    Returned rather than printed so a test can read it without capturing
    stdout: this is the one line of output whose absence would be invisible.
    """
    if not SKIPPED:
        return None
    engine_skips = [n for n in SKIPPED if "duckdb" in n.lower()]
    lines = [
        "",
        BANNER_RULE,
        "!! %d of %d test functions SELF-SKIPPED (%d of them for duckdb)."
        % (len(SKIPPED), len(TESTS), len(engine_skips)),
        "!!",
        "!! duckdb is not importable by this interpreter:",
        "!!   %s" % sys.executable,
        "!!",
        "!! So srv/store.py, srv/duck.py and every stream-A route over them",
        "!! were NOT exercised.  THIS RUN IS NOT EVIDENCE ABOUT THE ENGINE.",
        "!! mutate.py refuses a tree in this state, for the sharper version of",
        "!! the same reason: over a skipping tree a SURVIVED row means",
        "!! 'never ran', not 'nothing asserts'.",
        "!!",
        "!! Run it properly -- either:",
        "!!   docker compose -f server/compose.yml run --rm suite",
        "!! or, with no container runtime:",
        "!!   python3 -m venv env && env/bin/pip install duckdb",
        "!!   env/bin/python3 server/tests/test_all.py",
        BANNER_RULE,
    ]
    return "\n".join(lines)


def main(argv=None):
    # A SELF-SKIP IS A FAILURE BY DEFAULT, and that is a correction.
    #
    # It used to be exit 0 with a banner, on the argument that a developer with
    # no engine is entitled to run the 1147 assertions that do not need it.
    # They still are -- `--allow-skips` is one word -- but the default was the
    # wrong way round, because `make check` runs this file and `make check` is
    # the release gate: it exited 0 while 918 of 2073 assertions had never run,
    # and a banner on stdout is not what CI, `&&` chains and Make read.  It is
    # the same ruling `claudio usage doctor` already follows, and for the same
    # stated reason: every WARN it prints sets a non-zero exit, because it used
    # to print `WARN ledger rows: 0` and exit 0.
    #
    # `--require-duckdb` is kept and is now redundant.  `compose.yml`'s `suite`
    # service passes it, and an environment built specifically to supply the
    # engine saying so out loud is documentation rather than ceremony; the
    # flag also predates this change and removing it would break anything that
    # already passes it.
    argv = sys.argv[1:] if argv is None else argv
    known = ("--require-duckdb", "--allow-skips")
    allow_skips = "--allow-skips" in argv
    unknown = [a for a in argv if a not in known]
    if unknown:
        # Refused by name rather than ignored, which is the rule the API half
        # of this suite spends several tests pinning.
        sys.stderr.write("unknown argument(s): %s\n"
                         "the options are %s\n"
                         % (", ".join(unknown), " and ".join(known)))
        return 2
    start = time.time()
    for t in TESTS:
        try:
            t()
        except Exception as exc:            # a crash is a failure, not a stop
            global FAIL
            FAIL += 1
            FAILURES.append("%s raised %s: %s"
                            % (t.__name__, type(exc).__name__, exc))
    for d in DOOR_TMP:
        shutil.rmtree(d, ignore_errors=True)
    td.cleanup()
    for name in SKIPPED:
        print("     SKIP  %s" % name)
    dur = time.time() - start
    if FAILURES:
        print("\n".join("FAIL " + f for f in FAILURES))
    # The skip count belongs on the SUMMARY line, not only in the list above
    # it.  `mutate.py` refuses to mutate a skipping tree, and a reader skimming
    # one line needs to see that this run asserted less than a full one --
    # "1073 passed, 0 failed" with 40 skips scrolled off the top is the shape
    # that let 42 controls be reported as unguarded when they were merely
    # unrun.
    print("\n%d passed, %d failed, %d skipped, %d test functions, %.2fs"
          % (PASS, FAIL, len(SKIPPED), len(TESTS), dur))
    banner = _skip_banner()
    if banner:
        # After the summary, so it is the last thing on screen.  A reason that
        # prints above 1147 green assertions is a reason nobody reads.
        print(banner)
        if not allow_skips:
            print("!! Exiting non-zero: this run skipped %d test function(s)."
                  % len(SKIPPED))
            print("!! Pass --allow-skips to accept a run that did not check "
                  "the engine.")
            return 1
        print("!! --allow-skips was given: exiting 0 over a run that skipped "
              "%d test function(s)." % len(SKIPPED))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
