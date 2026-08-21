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
import io
import json
import os
import re
import shutil
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
from srv import api, duck, serve                               # noqa: E402
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
IMPURE = ("serve.py", "store.py")


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
    for key in ("by", "by_tag"):
        check("api: %s carries exactly the core's buckets, in wire shape" % key,
              canon_json(got["result"]["window"].get(key)),
              canon_json({dim: api.bucketise(vals)
                          for dim, vals in (want.get(key) or {}).items()}))
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
    shapes = [(V1 + "capabilities", ""), (V1 + "accounts", ""),
              (V1 + "diagnostics", ""), (V1 + "health", ""),
              (V1 + "windows", "account=" + u),
              (V1 + "window", "account=%s&kind=5h&resets_at=1786598400" % u),
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
        def handle(self, path, params, multi, now=None):
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

        # The front end's case, first because it is the ordinary one.
        for acct in (alpha, beta):
            status, doc = get(V1 + "search?account=" + acct, token="r-all")
            check("reader-auth: a universal token reads every account (%s)"
                  % acct[:8], (status, doc.get("outcome")), (200, "ok"))
        status, doc = get(V1 + "accounts", token="r-all")
        check("reader-auth: and may ask a store-wide question",
              (status, doc.get("outcome")), (200, "ok"))

        status, doc = get(V1 + "search?account=" + alpha, token="r-alpha")
        check("reader-auth: a scoped token reads the account it covers",
              (status, doc.get("outcome")), (200, "ok"))

        status, doc = get(V1 + "search?account=" + beta, token="r-alpha")
        check("reader-auth: another account is REFUSED BY NAME, not emptied",
              (status, doc.get("refusal", {}).get("reason")),
              (403, "account-not-permitted"))
        check("reader-auth: a refusal carries no result key to render as empty",
              "result" in doc, False)

        status, doc = get(V1 + "search?account=" + beta, token="r-all")
        check("reader-auth: a '*' token reads any account",
              (status, doc.get("outcome")), (200, "ok"))

        # Operators and health checks are deliberately outside the gate.
        req = urllib.request.Request("http://127.0.0.1:%d/healthz" % port)
        with urllib.request.urlopen(req, timeout=10) as fh:
            check("reader-auth: /healthz stays open for operators",
                  fh.status, 200)
    finally:
        httpd.shutdown()
        serve.ShipHandler.door = None
        serve.ShipHandler.api = None


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
