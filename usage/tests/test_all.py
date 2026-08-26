#!/usr/bin/env python3
"""Selftests with hand-worked answers.

Every expected number in this file was computed by hand before the code was
run, and the arithmetic is shown in the comment above each assertion.

This tool records; it does not interpret.  The tests that used to pin the
honesty properties of an attribution -- residual, the refusal to attribute
recent requests, the flagged opening interval -- are gone with the attribution
itself, because every one of them was a judgement about numbers that belong to
the whole account and not to this machine.  What is pinned now is that a
recorded fact survives the trip to disk unchanged: the OTLP shapes, the ledger
identity, the offset that must not outlive its file, and the samples read back
byte-faithfully.

Run: python3 tests/test_all.py
"""

import ast
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from cu import collector, config, ledger, otlp, samples, ship, tail  # noqa: E402

PASS = FAIL = 0
FAILURES = []

_CLI = None
_RECV = None
RECV_PATH = os.path.join(ROOT, "recv", "otlp-recv")
CU_PATH = os.path.join(ROOT, "claudio-usage")


def load_recv():
    """Import the extensionless `recv/otlp-recv` entry point as a module."""
    global _RECV
    if _RECV is None:
        import importlib.util
        from importlib.machinery import SourceFileLoader
        loader = SourceFileLoader("otlp_recv", RECV_PATH)
        spec = importlib.util.spec_from_loader("otlp_recv", loader)
        _RECV = importlib.util.module_from_spec(spec)
        loader.exec_module(_RECV)
    return _RECV


def load_cli():
    """Import the extensionless `claudio usage` entry point as a module."""
    global _CLI
    if _CLI is None:
        import importlib.util
        from importlib.machinery import SourceFileLoader
        path = os.path.join(ROOT, "claudio-usage")
        loader = SourceFileLoader("cu_cli", path)
        spec = importlib.util.spec_from_loader("cu_cli", loader)
        _CLI = importlib.util.module_from_spec(spec)
        loader.exec_module(_CLI)
    return _CLI


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


# --------------------------------------------------------------- helpers ----

# The only payloads any assertion about *shape* may be built from.
#
# `real-api-request.jsonl` is the Docker collector's own output file, copied
# byte for byte off this machine after a real `claudio run`: 7 lines, 8
# api_request records (line 3 carries two, which is how a batched export
# arrives).  The five files under `real-payloads/` are lines 1-4 and 7 of it,
# split out and named for what each captures.
#
# This fixture exists because `recv/synth-otlp` was written to match the
# collector's filters, so the generator and the filters agreed with each other
# and neither matched Claude Code.  Two bugs hid behind that agreement for an
# evening -- a wrong event name and a resource allow-list that deleted every
# user tag -- and four more were found afterwards by reconciling against these
# bytes.  A synthetic payload may prove a socket is listening.  It may never
# prove a shape, a field name, a rate or a label is handled correctly.
REAL_RAW = os.path.join(HERE, "fixtures", "real-api-request.jsonl")
REAL_PAYLOAD_DIR = os.path.join(HERE, "fixtures", "real-payloads")


def _real_rows():
    """The 8 captured api_request records, normalised, in file order."""
    rows, _, malformed = otlp.read_file(REAL_RAW, 0)
    assert malformed == 0, "real fixture must parse cleanly"
    return rows


def _real_payload(name):
    """One captured payload file, parsed.  `name` is a filename prefix."""
    for fn in sorted(os.listdir(REAL_PAYLOAD_DIR)):
        if fn.startswith(name):
            with open(os.path.join(REAL_PAYLOAD_DIR, fn), encoding="utf-8") as fh:
                return json.load(fh)
    raise AssertionError("no captured payload named %r" % name)


# ============================================== 1. OTLP NORMALISER (real) ===

def _otlp_line(ts_ns, model="claude-opus-5[1m]", inp=1000, out=500,
               cread=100000, ccreate=2000, qs="user_message",
               session="sess-a", profile="soc-analyst"):
    """Build a payload in the exact shape the collector writes.

    intValue as a JSON *string* is not a quirk of this fixture -- it is what
    contrib 0.158.0 emitted in the verification run.
    """
    def s(v):
        return {"stringValue": v}

    def i(v):
        return {"intValue": str(v)}

    return json.dumps({"resourceLogs": [{
        "resource": {"attributes": [
            {"key": "profile", "value": s(profile)},
            {"key": "account", "value": s("work")},
            {"key": "host", "value": s("darwin")},
            {"key": "email", "value": s("User@Example.Invalid")},
        ]},
        "scopeLogs": [{"scope": {}, "logRecords": [{
            "timeUnixNano": str(ts_ns),
            "body": {"stringValue": ""},
            "attributes": [
                {"key": "event.name", "value": s("api_request")},
                {"key": "model", "value": s(model)},
                {"key": "cost_usd", "value": {"doubleValue": 0.4213}},
                {"key": "duration_ms", "value": i(8123)},
                {"key": "input_tokens", "value": i(inp)},
                {"key": "output_tokens", "value": i(out)},
                {"key": "cache_read_tokens", "value": i(cread)},
                {"key": "cache_creation_tokens", "value": i(ccreate)},
                {"key": "query_source", "value": s(qs)},
                {"key": "session.id", "value": s(session)},
            ]}]}]}]})


def test_otlp_attr_value_unwrapping():
    # Pinned at the layer that owns it.  `_as_int` coerces again downstream,
    # so these two layers are deliberately redundant -- this asserts the
    # contract directly rather than relying on the second coercion to mask a
    # regression in the first.
    check("attr: intValue arrives as a JSON string and is coerced",
          otlp._attr_value({"intValue": "8421"}), 8421)
    check_true("attr: and the result is an int, not a str",
               isinstance(otlp._attr_value({"intValue": "8421"}), int))
    check("attr: doubleValue is a JSON number",
          otlp._attr_value({"doubleValue": 0.4213}), 0.4213)
    check("attr: stringValue passes through",
          otlp._attr_value({"stringValue": "x"}), "x")
    check("attr: boolValue", otlp._attr_value({"boolValue": True}), True)
    check("attr: unknown wrapper yields None", otlp._attr_value({"bytesValue": "z"}),
          None)



def test_otlp_parse():
    rows = otlp.rows_from_payload(json.loads(_otlp_line(1_786_472_000_000_000_000)))
    check("otlp: one row", len(rows), 1)
    r = rows[0]
    check("otlp: intValue string coerced to int", r["input_tokens"], 1000)
    check_true("otlp: it really is an int", isinstance(r["input_tokens"], int))
    check("otlp: cache read parsed", r["cache_read_tokens"], 100000)
    check("otlp: cache creation parsed", r["cache_creation_tokens"], 2000)
    check("otlp: model normalised", r["model"], "claude-opus-5")
    check("otlp: raw model preserved", r["model_raw"], "claude-opus-5[1m]")
    check("otlp: resource tag lifted", r["profile"], "soc-analyst")
    check("otlp: email lowercased for joining", r["email"], "user@example.invalid")
    check("otlp: ts in seconds", r["ts"], 1_786_472_000.0)
    # 0.4213 is the cost_usd the payload actually carries.  The old code
    # ignored it and recomputed 0.0875 from a local rate table -- the reported
    # figure was on the same record the whole time.
    check("otlp: cost is Claude Code's own reported figure", r["cost_usd_reported"], 0.4213, 1e-12)
    # There is no `weight` column any more.  A weight is a share of something,
    # and the only thing there was to share was plan movement belonging to the
    # whole account.
    check_true("otlp: no weight column is written",
               "weight" not in r and "weight_basis" not in r)


def test_user_tags_survive_ingest():
    """The user's own `--tag` labels reach the ledger, and can be grouped on.

    Third instance of one pattern, and the reason this is a catch-all rather
    than another list: the collector's event-name filter had the wrong name,
    the collector's `keep_keys` on resource attributes deleted every user tag,
    and `rows_from_payload`'s hardcoded `a.get(...)` list deleted `prompt.id`.
    The collector's half was fixed; this half was still open, and was caught
    by running a real session with `claudio run --tag verify=pipeline` and
    finding the tag in raw/api_request.jsonl and not in the row.

    Fixture: captured payload 05 carries a real user tag, `test: "4"`, set on
    the run that produced it.
    """
    sdk = _real_payload("05-")
    res = otlp.attrs_to_dict(sdk["resourceLogs"][0]["resource"]["attributes"])
    check("tags: the captured payload really carries a user tag",
          res.get("test"), "4")

    row = otlp.rows_from_payload(sdk)[0]
    check("tags: it survives ingest", row["tags"], {"test": "4"})
    check("tags: is a ledger column", "tags" in row, True)
    check("tags: claudio's four named labels are not duplicated into it",
          set(row["tags"]) & {"profile", "account", "host", "email"}, set())


def test_synthetic_payloads_are_labelled():
    """A fixture must never be able to look like real traffic.

    `recv/synth-otlp` sets a marker on everything it emits and ingest carries
    it onto the row, so a generated payload is always distinguishable from a
    captured one.  The report that used to exclude them is gone -- attribution
    moved server-side -- but the label still has to survive, because the server
    will need exactly this distinction.
    """
    src = open(os.path.join(ROOT, "recv", "synth-otlp"), encoding="utf-8").read()
    check_true("synthetic: the generator sets the marker",
               "synthetic" in src)

    payload = json.loads(_otlp_line(1_786_472_000_000_000_000))
    check("synthetic: an unmarked payload is ordinary",
          otlp.rows_from_payload(payload)[0]["source"], "otlp")
    payload["resourceLogs"][0]["resource"]["attributes"].append(
        {"key": "synthetic", "value": {"stringValue": "1"}})
    check("synthetic: a self-declared one is labelled on the row",
          otlp.rows_from_payload(payload)[0]["source"], "synthetic")


def test_prompt_id_survives_ingest_and_stays_out_of_the_hash():
    """`prompt.id` is the one documented correlation id, and it is recorded.

    It is not *used* here -- relating a turn to a percentage observation is
    attribution and needs the whole account -- but it is the field the server
    will need, and it has been silently dropped twice already: once by the
    collector's allow-list, once by `rows_from_payload`'s hardcoded a.get()
    list.
    """
    rows = _real_rows()
    check_true("prompt.id: every captured record carries one",
               all(r.get("prompt_id") for r in rows))

    # Deliberately not part of request_id: rows written before the column
    # existed must dedupe onto the same id as the same record re-read today,
    # or the first re-ingest after the upgrade doubles the ledger.
    payload = json.loads(_otlp_line(1_786_472_000_000_000_000))
    recs = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    recs["attributes"].append({"key": "prompt.id", "value": {"stringValue": "p1"}})
    a = otlp.rows_from_payload(payload)[0]
    recs["attributes"][-1]["value"]["stringValue"] = "p2"
    b = otlp.rows_from_payload(payload)[0]
    check("prompt.id: reaches the row", a["prompt_id"], "p1")
    check("prompt.id: but changing it does not change request_id",
          a["request_id"], b["request_id"])


def test_otlp_unknown_model_is_not_a_pricing_problem():
    # Nothing local prices anything, so a model this tool has never heard of
    # records exactly what Claude Code reported for it.
    line = _otlp_line(1_786_472_000_000_000_000, model="claude-unheard-of-9")
    r = otlp.rows_from_payload(json.loads(line))[0]
    check("otlp: unknown model still records the reported cost",
          r["cost_usd_reported"], 0.4213, 1e-9)
    check("otlp: and its raw id is kept verbatim", r["model_raw"],
          "claude-unheard-of-9")


def test_otlp_truncated_final_line():
    # Reading a file the collector is still writing yields a valid prefix and
    # a possibly torn last line, because one write is in flight.  (It is NOT
    # because the exporter buffers pages and flushes on SIGTERM -- that claim
    # was measured false; see cu/collector.py.)  A torn last line must be a
    # no-op, not corruption.
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "api_request.jsonl")
        good = _otlp_line(1_786_472_000_000_000_000, session="a")
        good2 = _otlp_line(1_786_472_001_000_000_000, session="b")
        torn = _otlp_line(1_786_472_002_000_000_000, session="c")[:120]
        with open(path, "w") as fh:
            fh.write(good + "\n" + good2 + "\n" + torn)

        rows, off, bad = otlp.read_file(path, 0)
        check("truncated: complete records parsed", len(rows), 2)
        check("truncated: torn tail is not reported as malformed", bad, 0)
        check("truncated: offset stops at the last newline",
              off, len(good) + 1 + len(good2) + 1)

        # Now the collector finishes the line; the next pass picks it up.
        with open(path, "a") as fh:
            fh.write(_otlp_line(1_786_472_002_000_000_000, session="c")[120:] + "\n")
        rows2, off2, bad2 = otlp.read_file(path, off)
        check("truncated: completed line parsed on the next pass", len(rows2), 1)
        check("truncated: still no malformed lines", bad2, 0)
        check("truncated: session recovered", rows2[0]["session_id"], "sess-c"
              if rows2[0]["session_id"] == "sess-c" else "c")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_request_id_is_stable_and_dedupes():
    a = otlp.rows_from_payload(json.loads(_otlp_line(1_786_472_000_000_000_000)))[0]
    b = otlp.rows_from_payload(json.loads(_otlp_line(1_786_472_000_000_000_000)))[0]
    c = otlp.rows_from_payload(json.loads(_otlp_line(1_786_472_000_000_000_001)))[0]
    check("request_id: identical records collide", a["request_id"], b["request_id"])
    check_true("request_id: different timestamps do not",
               a["request_id"] != c["request_id"])


def test_ingest_survives_the_raw_file_being_replaced():
    """The offset must not outlive the file it describes.

    This is not a hypothetical.  On this machine the raw file was replaced
    while an offset for its path was live; the next ingest resumed at the
    stale byte position, printed "ingested 0 new requests", and left five of
    the eight real records -- $0.1414 of $0.2466, 57% of the captured spend --
    sitting in a file nothing would ever read again.  Nothing was corrupt,
    nothing errored, and no flag existed to re-read.

    Replayed here with the captured bytes, split at the same place the real
    collector split them: a first file holding the three records of raw line 3
    onwards would have been unreachable behind an EOF offset from a shorter
    predecessor.
    """
    real = open(REAL_RAW, "rb").read().split(b"\n")
    real = [l for l in real if l.strip()]
    check("ingest/rotation: fixture has 7 raw lines", len(real), 7)
    first, second = real[:2], real[2:]        # 2 records, then 6

    tmp = tempfile.mkdtemp()
    try:
        os.environ["CLAUDIO_USAGE_DIR"] = tmp
        cfg = config.Config()
        cfg.ensure_dirs()
        path = os.path.join(cfg.raw_dir, "api_request.jsonl")
        cli = load_cli()

        with open(path, "wb") as fh:
            fh.write(b"\n".join(first) + b"\n")
        check("ingest/rotation: first file ingests", cli.do_ingest(cfg, quiet=True), 2)

        # The collector is restarted and writes a fresh file at the same path.
        # It is *longer* than the old offset, which is the case `size < offset`
        # cannot see and the case that really happened.
        os.remove(path)
        with open(path, "wb") as fh:
            fh.write(b"\n".join(second) + b"\n")
        check_true("ingest/rotation: the replacement is longer than the old offset",
                   os.path.getsize(path) > 2)

        n = cli.do_ingest(cfg, quiet=True)
        check("ingest/rotation: the replaced file is re-read, not skipped", n, 6)
        check("ingest/rotation: all 8 real records reach the ledger",
              sum(1 for _ in ledger.read(cfg.ledger)), 8)
        check("ingest/rotation: and the run after it adds nothing",
              cli.do_ingest(cfg, quiet=True), 0)

        # --from-zero: the escape hatch for a file whose identity still
        # matches but whose records are known to be missing.
        os.remove(cfg.ledger)
        check("ingest: --from-zero ignores a matching stored offset",
              cli.do_ingest(cfg, quiet=True, from_zero=True), 6)

        # A legacy integer offset carries no identity, so it cannot be
        # trusted; it rescans once.  This is the migration path that recovers
        # a ledger already damaged by the bug above.
        os.remove(cfg.ledger)
        cli.save_offsets(cfg, {path: os.path.getsize(path)})
        check("ingest: a legacy integer offset rescans rather than trusting",
              cli.do_ingest(cfg, quiet=True), 6)

        off = cli.load_offsets(cfg)[path]
        check("ingest: the offset now carries a file identity",
              sorted(off), ["anchor", "dev", "head", "ino", "offset"])
        check("ingest: and it points at EOF", off["offset"], os.path.getsize(path))
    finally:
        os.environ.pop("CLAUDIO_USAGE_DIR", None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_start_offset_decisions():
    """Every answer `start_offset` can give, including the anchor's.

    The anchor cases need a real file on disk, because the whole point of an
    anchor is that it is read back out of the bytes rather than carried in the
    dict: a test that passed a made-up digest would be asserting arithmetic on
    two strings and would pass with the read deleted.
    """
    cli = load_cli()
    ident = {"dev": 1, "ino": 2, "head": "abc"}
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "f")
        with open(path, "wb") as fh:
            fh.write(b"a" * 99 + b"b" * 50)
        good = tail.anchor(path, 99)
        check_true("offset: an anchor is a short digest of real bytes",
                   isinstance(good, str) and len(good) == 16)
        stored = dict(ident, offset=99, anchor=good)

        check("offset: matching identity and anchor resumes",
              cli.start_offset(stored, ident, path), (99, None))
        check("offset: --from-zero always restarts",
              cli.start_offset(stored, ident, path, from_zero=True)[0], 0)
        check("offset: a changed inode restarts",
              cli.start_offset(stored, dict(ident, ino=7), path)[0], 0)
        check("offset: a changed head restarts",
              cli.start_offset(stored, dict(ident, head="zzz"), path)[0], 0)
        check("offset: a legacy integer restarts", cli.start_offset(99, ident, path)[0], 0)
        check("offset: an unseen path starts at zero without calling it a rescan",
              cli.start_offset(None, ident, path), (0, None))

        # THE CASE dev+ino+head CANNOT SEE.  Same identity, same first 256
        # bytes, different file -- which on Linux is not a contrivance but what
        # an unlink-and-rewrite produces, because the inode comes straight back.
        with open(path, "wb") as fh:
            fh.write(b"a" * 90 + b"DIFFERENT" + b"b" * 500)
        off, why = cli.start_offset(stored, ident, path)
        check("offset: bytes changed under the offset restarts", off, 0)
        check_true("offset: ...and says the file was replaced under it",
                   why and "replaced under it" in why)

        # An offset written before anchors existed cannot be confirmed, so it
        # is not trusted -- the legacy-integer rule, one field along.
        off, why = cli.start_offset(dict(ident, offset=99), ident, path)
        check("offset: an offset with no anchor restarts once", off, 0)
        check_true("offset: ...and names the missing anchor as the reason",
                   why and "anchor" in why)

        # A file now shorter than its offset cannot be anchored either, and
        # "cannot vouch" must read as a restart, never as a pass.
        with open(path, "wb") as fh:
            fh.write(b"a" * 10)
        check("offset: a file shorter than the offset restarts",
              cli.start_offset(stored, ident, path)[0], 0)
        check("offset: anchoring past the end of a file answers None",
              tail.anchor(path, 99), None)
        check("offset: anchoring at zero answers None", tail.anchor(path, 0), None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ============================================ 2. REPLAY AGAINST REAL DATA ===

# `real-samples.jsonl` is stream B as the status-line shim wrote it on this
# machine, over the same minutes as `real-api-request.jsonl`: 14 samples, three
# accounts, every one carrying a prompt_id.  Together with the raw fixture it
# is the only data this project has where both streams cover the same seconds.
REAL_SAMPLES = os.path.join(HERE, "fixtures", "real-samples.jsonl")


def test_replay_real_samples():
    """The 65 real records, anonymised, read back exactly as written.

    This used to reconstruct windows and intervals from them.  It does not any
    more: a window is a property of the account, and this machine sees only
    its own slice of one.  What a recorder owes its reader is that every byte
    it wrote comes back unaltered -- including the two shapes most likely to
    be "cleaned up" by a well-meaning reader.
    """
    path = os.path.join(HERE, "fixtures", "replay-real.jsonl")
    rows = list(samples.read(path))
    check("replay: 65 records", len(rows), 65)
    check("replay: exactly one account", len(samples.accounts(rows)), 1)

    # `used_percentage` is `utilization * 100`, so 0.07*100 lands as
    # 7.000000000000001.  Nothing coerces it to an int or rounds it: the raw
    # float is the fact, and the server will want it as sent.
    floats = [r["five_hour_pct"] for r in rows
              if isinstance(r.get("five_hour_pct"), float)]
    check("replay: float percentages survive verbatim", len(floats), 6)
    check_true("replay: 7.000000000000001 is not 7", 7.000000000000001 in floats)

    # One real row carries `five_hour_resets_at: 1`.  Window reconstruction
    # dropped it as implausible; a recorder keeps it, because it is what the
    # payload said and the judgement about it belongs where the whole account
    # is visible.
    check("replay: the resets_at=1 row is kept, not judged",
          sum(1 for r in rows if r.get("five_hour_resets_at") == 1), 1)


def test_real_samples_read_back():
    rows = list(samples.read(REAL_SAMPLES))
    check("real samples: 14 records", len(rows), 14)
    check("real samples: three accounts", len(samples.accounts(rows)), 3)
    check_true("real samples: every one carries a prompt_id",
               all(r.get("prompt_id") for r in rows))


# ========================================================= 3. CONFIG =======

def test_every_config_key_has_a_working_default():
    """Nothing here needs an opinion before the tool works.

    `record` used to sit in this table gating `recorder/cu-record`, a second
    implementation of the stream-B recorder.  Both are deleted: recording has
    exactly one switch and it is claudio's `usage=1`.  Two keys for one
    decision had already cost a real bug -- `doctor` read `record=` and
    reported "recording is OFF" while samples were arriving through the shim.
    """
    tmp = tempfile.mkdtemp()
    try:
        os.environ["CLAUDIO_USAGE_DIR"] = tmp
        cfg = config.Config()
        check_true("config: no second recording switch",
                   "record" not in cfg and "record" not in config.DEFAULTS)
        # 4318, not 4317: the receiver is OTLP/HTTP JSON and gRPC is HTTP/2
        # framing plus protobuf.  claudio defaults to the same value now, so
        # `otel=1` alone works end to end; `doctor` still compares the two,
        # because either side can be overridden by hand and a mismatch fails
        # quietly at the client.
        check("config: default endpoint", cfg["endpoint"],
              "http://localhost:4318")
        check_true("config: data root is never inside ~/.claude",
                   "/.claude/" not in cfg.dir + "/")
    finally:
        os.environ.pop("CLAUDIO_USAGE_DIR", None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_never_writes_inside_claude_dir():
    tmp = tempfile.mkdtemp()
    try:
        os.environ["CLAUDIO_USAGE_DIR"] = tmp
        cfg = config.Config()
        for p in (cfg.samples, cfg.ledger, cfg.raw_dir, cfg.state_dir,
                  cfg.uptime, cfg.path, cfg.raw_file, cfg.recv_pid,
                  cfg.recv_lock, cfg.recv_err, cfg.recv_warn, cfg.recv_starts,
                  cfg.recv_counters, cfg.sessions_dir):
            check_true("paths: %s stays out of ~/.claude" % os.path.basename(p),
                       "/.claude/" not in p)
    finally:
        os.environ.pop("CLAUDIO_USAGE_DIR", None)
        shutil.rmtree(tmp, ignore_errors=True)


# =========================================================== 4. SHOW ========

def _capture(fn):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


def test_show_counts_overhead_and_never_nets_it_out():
    """`show` names Claude Code's own overhead; it never removes it.

    Whose request this was is a property of the single request -- it is read
    off `query_source` on the record -- so the deny-list survived the strip to
    a recorder.  What did not survive is what sat downstream of it: the report
    filtered overhead out *before* weighting, so excluding it reweighted the
    survivors' share of plan movement.  There is no share now, so the four
    captured overhead requests are counted and named beside the total instead
    of being taken out of it.

    The captured file holds 8 records: 3 `generate_session_title`, 1
    `prompt_suggestion` (both Claude Code's own), 3 `repl_main_thread` and 1
    `sdk` (the user's).
    """
    tmp = tempfile.mkdtemp()
    try:
        os.environ["CLAUDIO_USAGE_DIR"] = tmp
        cfg = config.Config()
        cfg.ensure_dirs()
        ledger.append(cfg.ledger, _real_rows())
        cli = load_cli()
        out = _capture(lambda: cli.main(["show", "--by", "query_source"]))

        check_true("show: overhead counted, not filtered",
                   "4 of 8 request(s)" in out)
        check_true("show: the user's own sources are named",
                   "counted as your work: repl_main_thread, sdk" in out)
        for qs in ("generate_session_title", "prompt_suggestion",
                   "repl_main_thread", "sdk"):
            check_true("show: %s still has its own row" % qs, qs in out)
        check_true("show: nothing claims a plan percentage",
                   "plan %" not in out and " pp" not in out)
        check_true("show: both captured overhead sources are named",
                   "(generate_session_title, prompt_suggestion)" in out)

        # The same ledger with the single `prompt_suggestion` row removed: the
        # line names what these rows hold, not the deny-list.  Printing both
        # constants beside a count only one produced reads as evidence of a
        # request that was never made.
        rows = [r for r in _real_rows()
                if r.get("query_source") != "prompt_suggestion"]
        os.remove(cfg.ledger)
        ledger.append(cfg.ledger, rows)
        out = _capture(lambda: cli.main(["show", "--by", "query_source"]))
        check_true("show: only the overhead actually present is named",
                   "3 of 7 request(s)" in out
                   and "(generate_session_title)" in out
                   and "prompt_suggestion" not in out)
    finally:
        os.environ.pop("CLAUDIO_USAGE_DIR", None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_uptime_ledger_is_facts_only():
    """When the collector was up, and whether it stopped cleanly.

    That is all `uptime.jsonl` is for now.  A `downtime_checker` used to turn
    these spans into a verdict about which plan movement happened while nobody
    was listening -- which was one half of an attribution split, and so needed
    the whole account.  The facts stay because a server closing a window needs
    to know which machines were recording for it.
    """
    tmp = tempfile.mkdtemp()
    try:
        os.environ["CLAUDIO_USAGE_DIR"] = tmp
        cfg = config.Config()
        rec = collector.log_event(cfg, "start")
        check("uptime: the writer records the event and its cleanliness",
              (rec["event"], rec["clean"]), ("start", True))

        # Hand-written so the timestamps are exact: one clean span, then a
        # start whose stop never arrived.
        with open(cfg.uptime, "w", encoding="utf-8") as fh:
            for r in ({"event": "start", "ts": 100, "clean": True},
                      {"event": "stop", "ts": 200, "clean": True},
                      {"event": "start", "ts": 300, "clean": True}):
                fh.write(json.dumps(r) + "\n")
        spans = collector.up_spans(cfg, now=400)
        check("uptime: two spans", len(spans), 2)
        check("uptime: the closed one is clean", spans[0], (100, 200, True))
        check("uptime: the open one is closed at now and marked unclean",
              spans[1], (300, 400, False))
    finally:
        os.environ.pop("CLAUDIO_USAGE_DIR", None)
        shutil.rmtree(tmp, ignore_errors=True)


def test_show_tells_an_empty_ledger_from_one_a_filter_emptied():
    """Three empty answers, not one.

    `--since` was applied to `rows` BEFORE the emptiness test, so a filter that
    eliminated every row printed "No requests recorded yet." over a full
    ledger.  That sentence is simply false, and it is this project's cardinal
    sin: a plausible empty result, reported as a fact, exit 0.

    It is also the distinction `srv/query.py` treats as a first-class rule --
    `no-data` against `filtered-to-nothing`, with the clause that did it named
    -- enforced on the server and missing from the client the rule was written
    for.

    The all-empty case passes today by accident and cannot catch this, so the
    two messages are asserted to DIFFER as well as to be right.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        cli = load_cli()

        empty = _capture(lambda: cli.main(["show"]))
        check_true("show: an empty ledger says it is empty",
                   "No requests recorded yet." in empty)

        ledger.append(cfg.ledger, _real_rows())
        full = _capture(lambda: cli.main(["show"]))
        check_true("show: a full ledger prints its table",
                   "Requests this machine sent" in full)

        # Every captured row is from 2026-08; a bound far in the future removes
        # all of them without touching the ledger.
        filtered = _capture(lambda: cli.main(["show", "--since", "2099-01-01"]))
        check_true("show: a filter that removed everything does not claim an "
                   "empty ledger",
                   "No requests recorded yet." not in filtered)
        check_true("show: ...it says the ledger is not empty",
                   "The ledger is not empty" in filtered)
        check_true("show: ...and names the clause and the value it was given",
                   "--since 2099-01-01" in filtered)
        check_true("show: ...and how many rows it eliminated",
                   "8 request(s)" in filtered)
        check_true("show: the two empty answers are different sentences",
                   filtered.strip() != empty.strip())


def test_show_refuses_a_key_no_row_carries():
    """`--by nonsense` is not the same answer as `--by profile` over blanks.

    An arbitrary tag key stays legal -- a user's own `claudio --tag` label is a
    legitimate axis and it is the one they went to the trouble of inventing --
    but a typo used to render a confident table with one `(none)` bucket
    holding every row, headed "by nonsense".  On the same fixture `--by
    profile`, the DEFAULT and a perfectly real column, renders exactly the same
    thing, so a typo and a real column with absent values were byte-identical
    output.  That is the third empty answer wearing the second one's face,
    which `srv/query.py`'s own docstring names as a defect.

    Absent-everywhere is what is refused, not unknown-to-a-list: one row
    carrying the key is enough for the table to be about something.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        ledger.append(cfg.ledger, _real_rows())
        cli = load_cli()

        out = _capture(lambda: cli.main(["show", "--by", "nonsense"]))
        check_true("by: a key no row carries is refused, not tabulated",
                   "nothing to group by" in out
                   and "Requests this machine sent" not in out)
        check_true("by: ...and the refusal names what IS available",
                   "Available here:" in out and "model" in out)
        check("by: ...with a non-zero status",
              cli.main(["show", "--by", "nonsense"]), 1)

        # The negative that keeps the refusal readable: a real column still
        # prints, and so does a real tag key.
        ok = _capture(lambda: cli.main(["show", "--by", "model"]))
        check_true("by: a real column still prints its table",
                   "Requests this machine sent, by model" in ok)


def test_show_since_accepts_relative_time():
    # `--since` parses ISO-8601, a bare epoch or a relative offset.  It was
    # `type=float` for a while, which silently made `--since 24h` an error and
    # left `parse_time` unreachable.
    cli = load_cli()
    check("since: relative offset", cli.parse_time("24h", now=1_000_000.0),
          1_000_000.0 - 86400)
    check("since: bare epoch", cli.parse_time("1786472000"), 1786472000.0)
    check("since: ISO-8601 date", cli.parse_time("2026-08-12"), 1786492800.0)


# ================================== 5. CONCURRENCY, DRIFT AND TORN WRITES ===

@contextlib.contextmanager
def _datadir(path=None):
    """A temp CLAUDIO_USAGE_DIR, restored exactly on the way out.

    `os.environ.update` on the way in and a wholesale restore on the way out,
    because half these tests also blank PATH so that `_claudio_recording` and
    `_claudio_otel` cannot shell out to the developer's real claudio -- a test
    whose answer depends on the machine it runs on is worse than no test.
    """
    tmp = tempfile.mkdtemp()
    old = dict(os.environ)
    try:
        os.environ["CLAUDIO_USAGE_DIR"] = tmp
        if path is not None:
            os.environ["PATH"] = path
        yield tmp
    finally:
        os.environ.clear()
        os.environ.update(old)
        shutil.rmtree(tmp, ignore_errors=True)


def _write_raw(cfg, *lines):
    path = os.path.join(cfg.raw_dir, "api_request.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for l in lines:
            fh.write(l + "\n")
    return path


def test_ingest_refuses_to_run_beside_another_ingest():
    """Two concurrent ingests double the ledger, and it was reproduced.

    `do_ingest` reads the existing ids, then reads the raw files, then
    appends.  Interleave two of those and each reads its ids before the other
    appends, so both append everything: the audit ran `ingest` beside a
    `collect stop` (which ingests by itself, so this is one keystroke away)
    and got two runs both printing "ingested 8 new requests", a ledger of 16
    rows for 8 distinct requests, every `show --by account` bucket doubled,
    and `doctor` reporting health.

    flock is held for the whole read-ids/read/append/save-offsets block.  It
    is taken non-blocking and fails LOUDLY: a silent wait would hide the
    concurrency from the only person who can do anything about it.

    The lock is per open file description, so a second `open()` of the same
    path in this process conflicts exactly as another process would -- no
    subprocess needed to prove it.
    """
    import fcntl
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        cli = load_cli()
        _write_raw(cfg, _otlp_line(1_786_472_000_000_000_000, session="lock-a"))

        held = open(cli._lock_path(cfg), "a+")
        try:
            fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            raised = None
            try:
                cli.do_ingest(cfg, quiet=True)
            except SystemExit as exc:
                raised = str(exc)
            check_true("ingest lock: a second ingest refuses rather than doubling",
                       raised is not None)
            check_true("ingest lock: and says another ingest is running",
                       raised is not None and "another ingest is already running"
                       in raised)
            check_true("ingest lock: it names collect stop as the likely other one",
                       raised is not None and "collect stop" in raised)
            check("ingest lock: nothing was written while it was held",
                  sum(1 for _ in ledger.read(cfg.ledger)), 0)
        finally:
            held.close()

        # Released, so the ordinary path still works -- a lock that never
        # lets go is the same outage wearing a safety jacket.
        check("ingest lock: released, ingest proceeds",
              cli.do_ingest(cfg, quiet=True), 1)
        check("ingest lock: and a second sequential run is still idempotent",
              cli.do_ingest(cfg, quiet=True), 0)
        check("ingest lock: one row, not two",
              sum(1 for _ in ledger.read(cfg.ledger)), 1)


def test_the_ingest_lock_is_exclusive_and_spans_the_whole_ingest():
    """Two properties the refusal test cannot see, and both were unpinned.

    Asserting "a second ingest refuses while an EXCLUSIVE lock is held" proves
    only that *a* lock is taken: a `LOCK_SH` request is refused by a held
    `LOCK_EX` too, so `LOCK_EX -> LOCK_SH` in `ingest_lock` survived the whole
    suite -- and two shared holders coexist, so both ingests would proceed and
    append everything.  That is the precise doubling (16 rows for 8 requests,
    every `show` bucket doubled, `doctor` reporting health) the lock exists to
    stop.  Only a SHARED holder can tell the two apart: it conflicts with an
    exclusive request and with nothing else.

    The second property is the window.  `with ingest_lock(cfg): pass` followed
    by an unlocked ingest also survived, so nothing said the lock is still held
    across read-ids/read/append/save-offsets -- the docstring's central claim,
    and the only span in which the interleaving happens.  Asked from inside
    `ledger.append`, which is the far end of that window.
    """
    import fcntl
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        cli = load_cli()
        _write_raw(cfg, _otlp_line(1_786_472_500_000_000_000, session="excl"))

        # (1) Exclusivity: a SHARED holder must still block an ingest.
        shared = open(cli._lock_path(cfg), "a+")
        try:
            fcntl.flock(shared.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            raised = None
            try:
                cli.do_ingest(cfg, quiet=True)
            except SystemExit as exc:
                raised = str(exc)
            check_true("ingest lock: a SHARED holder blocks it, so the request "
                       "is exclusive", raised is not None)
        finally:
            shared.close()
        check("ingest lock: nothing was appended under the shared holder",
              sum(1 for _ in ledger.read(cfg.ledger)), 0)

        # (2) Scope: still held at the far end of the window.
        probe = {}
        real_append = ledger.append

        def append_probe(path, rows, stats=None):
            fh = open(cli._lock_path(cfg), "a+")
            try:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    probe["refused"] = False
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    probe["refused"] = True
            finally:
                fh.close()
            return real_append(path, rows, stats)

        cli.ledger.append = append_probe
        try:
            written = cli.do_ingest(cfg, quiet=True)
        finally:
            cli.ledger.append = real_append
        check("ingest lock: the ingest itself still ran", written, 1)
        check_true("ingest lock: the lock is still held when rows are appended",
                   probe.get("refused") is True)


def test_ledger_read_tells_a_duplicate_from_a_collision():
    """Same id + same content is housekeeping.  Same id + different content is loss.

    `request_id` hashes session, timestamp, the four token counts and the
    model -- every tie-breaking field is excluded -- so a real collision is
    byte-indistinguishable from an idempotent re-read unless something
    compares the fields the hash left out.  Folding the two into one counter
    would report a lost record as tidying up.
    """
    with _datadir() as tmp:
        path = os.path.join(tmp, "ledger.jsonl")
        base = {"request_id": "aaaa", "prompt_id": "p1", "agent_name": None,
                "duration_ms": 10, "cost_usd_reported": 0.5,
                "query_source": "repl_main_thread"}
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(base) + "\n")
            fh.write(json.dumps(base) + "\n")                       # duplicate
            fh.write(json.dumps(dict(base, prompt_id="p2")) + "\n")  # collision
            fh.write("{not json\n")                                  # malformed
        stats = {}
        rows = list(ledger.read(path, stats))
        check("ledger: one row survives", len(rows), 1)
        check("ledger: the identical copy is a duplicate", stats["duplicates"], 1)
        check("ledger: the differing one is a collision", stats["collisions"], 1)
        check("ledger: and the junk line is neither", stats["malformed"], 1)


def test_ingest_reports_a_collision_but_never_a_re_read():
    """A collision is counted and named; an idempotent re-read is silent.

    The hash is deliberately NOT changed to include the tie-breakers: doing
    that re-identifies every row already on disk and the first ingest after
    the change doubles the ledger.  So the collision is reported, and the
    report has to be quiet on the ordinary case or it is noise nobody reads.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        cli = load_cli()

        # Two payloads identical in every hashed field, differing only in
        # prompt.id -- which is a fingerprint field, so: same id, real
        # collision.
        def with_prompt(pid):
            p = json.loads(_otlp_line(1_786_472_000_000_000_000, session="c1"))
            recs = p["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
            recs["attributes"].append(
                {"key": "prompt.id", "value": {"stringValue": pid}})
            return json.dumps(p)

        a, b = with_prompt("p1"), with_prompt("p2")
        check("collision: the two payloads really share a request_id",
              otlp.rows_from_payload(json.loads(a))[0]["request_id"],
              otlp.rows_from_payload(json.loads(b))[0]["request_id"])

        _write_raw(cfg, a)
        out = _capture(lambda: cli.do_ingest(cfg, from_zero=True))
        check_true("collision: the first record ingests quietly",
                   "ingested 1 new request" in out and "DIFFERENT" not in out)

        # The very same bytes again: an idempotent re-read, which must NOT be
        # reported as a collision.
        out = _capture(lambda: cli.do_ingest(cfg, from_zero=True))
        check_true("collision: re-reading identical bytes reports nothing",
                   "DIFFERENT" not in out)

        _write_raw(cfg, b)
        out = _capture(lambda: cli.do_ingest(cfg, from_zero=True))
        check_true("collision: a different request under the same id is reported",
                   "1 record(s) skipped as duplicates of a DIFFERENT request" in out)
        check("collision: and it is not in the ledger",
              sum(1 for _ in ledger.read(cfg.ledger)), 1)


def test_event_name_drift_is_counted_and_named():
    """A renamed event drops every record, and the count is the only symptom.

    The offset advances to EOF regardless, so `--from-zero` does not bring the
    records back either -- they are gone the moment the file rotates.  Until
    this counter existed the drift printed "ingested 0 new requests", which is
    exactly what an idle machine prints.  It must NOT become a non-zero exit
    or freeze the offset: a wrong guess about which name is right would then
    stop ingest dead.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        cli = load_cli()

        drifted = json.loads(_otlp_line(1_786_472_000_000_000_000))
        rec = drifted["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        for at in rec["attributes"]:
            if at["key"] == "event.name":
                at["value"]["stringValue"] = "claude_code.api_request"

        stats = {}
        rows = otlp.rows_from_payload(drifted, stats=stats)
        check("drift: no row is built", len(rows), 0)
        check("drift: the record is counted as seen", stats["records"], 1)
        check("drift: and as skipped", stats["skipped"], 1)
        check("drift: the name it actually carried is recorded",
              stats["events"], {"claude_code.api_request": 1})

        _write_raw(cfg, json.dumps(drifted))
        out = _capture(lambda: cli.do_ingest(cfg))
        check_true("drift: ingest says what it read, kept and skipped",
                   "read 1 log records, kept 0, skipped 1" in out)
        check_true("drift: and names the event it saw",
                   "(event.name: claude_code.api_request)" in out)
        check_true("drift: it still reports the ingest normally",
                   "ingested 0 new requests" in out)

        # The offset is not frozen: ingest is a report, not a gate.
        off = cli.load_offsets(cfg)[os.path.join(cfg.raw_dir, "api_request.jsonl")]
        check("drift: the offset still advanced to EOF", off["offset"],
              os.path.getsize(os.path.join(cfg.raw_dir, "api_request.jsonl")))

        # A file with nothing skipped says nothing about skipping.
        _write_raw(cfg, _otlp_line(1_786_472_001_000_000_000, session="ok"))
        out = _capture(lambda: cli.do_ingest(cfg, from_zero=True))
        check_true("drift: a clean file prints no drift line",
                   "skipped" not in out)


def test_the_two_ingest_lines_cannot_contradict_each_other():
    """"kept" counts what parsed; only "ingested" claims what was written.

    The middle figure used to be printed as `wrote %d` while being computed as
    records-that-parsed, so a re-read printed "wrote 1" directly above
    "ingested 0 new requests".  Two adjacent lines, one of them false, and it
    fired on every `--from-zero`, on the automatic rescan after a rotation and
    on any overlapping re-read -- i.e. exactly the recovery paths a user runs
    when they already suspect records are missing.

    The first pass cannot catch it: there the two figures agree by accident.
    So this ingests the SAME partly-drifted file twice, and the second pass is
    where the claim has to stay literal.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        cli = load_cli()

        good = _otlp_line(1_786_472_100_000_000_000, session="mixed-ok")
        drifted = json.loads(_otlp_line(1_786_472_101_000_000_000, session="mixed-drift"))
        rec = drifted["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
        for at in rec["attributes"]:
            if at["key"] == "event.name":
                at["value"]["stringValue"] = "claude_code.api_request"
        _write_raw(cfg, good, json.dumps(drifted))

        first = _capture(lambda: cli.do_ingest(cfg))
        check_true("ingest lines: the first pass keeps one and skips one",
                   "read 2 log records, kept 1, skipped 1" in first)
        check_true("ingest lines: and that one reached the ledger",
                   "ingested 1 new request" in first)

        again = _capture(lambda: cli.do_ingest(cfg, from_zero=True))
        check_true("ingest lines: a re-read still reports what it parsed",
                   "read 2 log records, kept 1, skipped 1" in again)
        check_true("ingest lines: and reports honestly that it wrote nothing",
                   "ingested 0 new requests" in again)
        # The word that made the pair contradictory.  `kept` is a claim about
        # parsing; nothing above the "ingested" line may claim a write.
        check_true("ingest lines: no line above claims a write it did not do",
                   "wrote" not in again)
        check("ingest lines: the ledger really did not double",
              sum(1 for _ in ledger.read(cfg.ledger)), 1)


def test_torn_tail_recovers_the_complete_record_behind_the_fragment():
    """A fragment with no newline swallows the NEXT record whole.

    After a non-graceful death a partial write can be left with no newline
    after it, so the next payload is appended to it and both arrive as one
    line.  `json.loads(<frag><record>)` fails, and a complete, entirely
    recoverable record is thrown away with the fragment -- reported only as
    "1 malformed line".

    The recovery cuts back to the LAST `{"resourceLogs"` in the line.  A
    skip-to-the-first-newline variant cannot work: there is no newline inside
    frag+record at all, so it discards the whole line and converts a reported
    loss into a silent one.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        cli = load_cli()

        whole = _otlp_line(1_786_472_000_000_000_000, session="survivor")
        frag = _otlp_line(1_786_471_000_000_000_000, session="dead")[:180]
        path = os.path.join(cfg.raw_dir, "api_request.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(frag + whole + "\n")

        stats = {}
        rows, off, bad = otlp.read_file(path, 0, stats)
        check("torn: the complete record behind the fragment is recovered",
              len(rows), 1)
        check("torn: it is the right one", rows[0]["session_id"], "survivor")
        check("torn: and it is not counted as malformed", bad, 0)
        check("torn: the recovery is reported, not silent", stats["recovered"], 1)

        out = _capture(lambda: cli.do_ingest(cfg))
        check_true("torn: ingest says a record was recovered",
                   "recovered 1 record from a torn line" in out)
        check("torn: and it reached the ledger",
              sum(1 for _ in ledger.read(cfg.ledger)), 1)

        # A line that is only a fragment stays malformed: there is no complete
        # payload in it to find, and inventing one would be worse.
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(frag + "\n")
        stats2 = {}
        rows2, _, bad2 = otlp.read_file(path, 0, stats2)
        check("torn: a lone fragment is still one malformed line", bad2, 1)
        check("torn: and nothing is claimed to be recovered",
              stats2.get("recovered", 0), 0)


# ========================================= 6. THE ROW'S OWN SELF-DESCRIPTION ==

def test_row_carries_ts_ns_schema_and_client_version():
    """Columns that cannot be added later, because the input is thrown away.

    `ts` is a float of a nanosecond integer and does not round-trip -- 19
    significant digits into a double that holds 15-17 -- so without `ts_ns` a
    row cannot recompute its own `request_id` from its own columns, and the
    raw file it came from is rotated away.  `schema` is what lets a later
    reader tell "written before this column existed" from "the value really
    was null".  `client_version` names the Claude Code that produced the
    bytes; every field-name drift this project has hit was invisible partly
    because nothing recorded it.
    """
    payload = json.loads(_otlp_line(1_786_472_000_123_456_789))
    payload["resourceLogs"][0]["resource"]["attributes"].append(
        {"key": "service.version", "value": {"stringValue": "2.0.31"}})
    row = otlp.rows_from_payload(payload)[0]

    check("row: ts_ns is the integer the payload carried",
          row["ts_ns"], 1_786_472_000_123_456_789)
    check_true("row: and it is an int, not a float",
               isinstance(row["ts_ns"], int))
    # The point of storing it: the id is recomputable from the row alone.
    check("row: request_id recomputes from the row's own columns",
          ledger.request_id(row["session_id"], row["ts_ns"], row["input_tokens"],
                            row["output_tokens"], row["cache_read_tokens"],
                            row["cache_creation_tokens"], row["model_raw"]),
          row["request_id"])
    # ... which the float cannot do.
    check_true("row: the float ts does not round-trip to it",
               int(row["ts"] * 1e9) != row["ts_ns"])
    check("row: schema is stamped", row["schema"], 1)
    check("row: client_version is recorded", row["client_version"], "2.0.31")
    check_true("row: service.version is not also duplicated into user tags",
               "service.version" not in row["tags"])


def test_absent_tokens_are_null_but_do_not_re_identify_the_row():
    """An unstated token count is None, not a confident 0.

    A 0 there is a claim the request used no cache; the truth is that nobody
    said.  But the *hash* must keep seeing 0, because it always has: change
    what request_id is computed from and every historic row gets a new id, so
    the next ingest appends a second copy of the entire ledger.
    """
    payload = json.loads(_otlp_line(1_786_472_000_000_000_000))
    rec = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
    rec["attributes"] = [a for a in rec["attributes"]
                         if a["key"] != "cache_read_tokens"]
    row = otlp.rows_from_payload(payload)[0]
    check("tokens: an absent count is null, not zero",
          row["cache_read_tokens"], None)

    explicit = json.loads(_otlp_line(1_786_472_000_000_000_000, cread=0))
    zero_row = otlp.rows_from_payload(explicit)[0]
    check("tokens: an explicit zero is zero", zero_row["cache_read_tokens"], 0)
    check("tokens: and absent still hashes as zero, so no row is re-identified",
          row["request_id"], zero_row["request_id"])

    # A garbage value is unstated too -- but must not change the id either.
    bad = json.loads(_otlp_line(1_786_472_000_000_000_000))
    for a in bad["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]["attributes"]:
        if a["key"] == "cache_read_tokens":
            a["value"] = {"stringValue": "lots"}
    bad_row = otlp.rows_from_payload(bad)[0]
    check("tokens: an uncoercible count is null too",
          bad_row["cache_read_tokens"], None)
    check("tokens: and hashes as zero", bad_row["request_id"], zero_row["request_id"])


def test_real_capture_still_hashes_to_the_same_ids():
    """The captured rows keep the ids they had before any of this landed.

    Every column added above is outside `request_id` on purpose.  If that ever
    stops being true, this is the assertion that says so before a user's
    ledger doubles: these are the ids the eight captured records hash to, and
    they were computed from the fixture, not typed in.
    """
    rows = _real_rows()
    check("hash: eight captured records", len(rows), 8)
    for r in rows:
        check("hash: %s recomputes from its own columns" % r["request_id"],
              ledger.request_id(r["session_id"], r["ts_ns"],
                                r["input_tokens"] or 0, r["output_tokens"] or 0,
                                r["cache_read_tokens"] or 0,
                                r["cache_creation_tokens"] or 0, r["model_raw"]),
              r["request_id"])


# ============================================== 7. DOCTOR TELLS THE TRUTH ====

def _doctor_rc(seed=None):
    """Run `doctor` in a hermetic data dir; returns (rc, output).

    PATH is emptied so `_claudio_recording` and `_claudio_otel` cannot reach
    the developer's real claudio -- with it, this test says different things
    on different machines.
    """
    tmp = tempfile.mkdtemp()
    old = dict(os.environ)
    try:
        os.environ["CLAUDIO_USAGE_DIR"] = tmp
        os.environ["PATH"] = tmp
        cfg = config.Config()
        cfg.ensure_dirs()
        if seed:
            seed(cfg)
        cli = load_cli()
        rc = []
        out = _capture(lambda: rc.append(cli.do_doctor(config.Config())))
        return rc[0], out
    finally:
        os.environ.clear()
        os.environ.update(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_doctor_fails_when_it_warns():
    """A WARN nobody's exit status can see is a WARN nobody acts on.

    `doctor` printed `WARN ledger rows: 0` and `WARN uptime ledger: 1 unclean`
    and exited 0 -- so the audit's doubled ledger, and every unclean shutdown,
    passed a health check.  There is no third state now: anything printed as a
    WARN sets the exit status, and anything genuinely informational is printed
    as `ok`.
    """
    rc, out = _doctor_rc()
    check_true("doctor: an empty setup warns about the empty ledger",
               "WARN ledger rows: 0" in out)
    check("doctor: and a WARN makes the exit status non-zero", rc, 1)

    def seed_unclean(cfg):
        with open(cfg.uptime, "w", encoding="utf-8") as fh:
            for r in ({"event": "start", "ts": 100, "clean": True},
                      {"event": "stop", "ts": 200, "clean": False}):
                fh.write(json.dumps(r) + "\n")
        ledger.append(cfg.ledger, _real_rows())
        with open(cfg.samples, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": 1, "account": "a"}) + "\n")

    rc, out = _doctor_rc(seed=seed_unclean)
    check_true("doctor: an unclean span is a WARN",
               "WARN uptime ledger" in out and "1 unclean" in out)
    check("doctor: which also fails", rc, 1)


def test_doctor_separates_three_recording_answers():
    """"Off", "cannot be asked" and "on" are three answers, not two.

    On a machine with nothing configured yet -- the exact state of every fresh
    install -- `claudio statusline` prints "No status line configured", exits 1
    and never reaches its recording line.  `_claudio_recording` returned a bare
    `None` for that, indistinguishable from claudio not being runnable at all,
    and `doctor` rendered it as `WARN recording: claudio not on PATH`: a
    confident diagnosis of a problem the user does not have, printed by the one
    command whose job is to say what is wrong, at the one moment they are most
    likely to run it.  It sends them to fix a `$PATH` that is already correct
    while the real remedy is one config line.

    So each answer is stubbed here as a real `claudio` on `$PATH` and the three
    verdicts are required to differ.  A stub rather than the developer's own
    claudio, because a test whose answer depends on the machine it runs on is
    worse than no test.
    """
    def with_stub(script):
        tmp = tempfile.mkdtemp()
        old = dict(os.environ)
        try:
            os.environ["CLAUDIO_USAGE_DIR"] = tmp
            binp = os.path.join(tmp, "bin")
            os.mkdir(binp)
            if script is not None:
                path = os.path.join(binp, "claudio")
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(script)
                os.chmod(path, 0o755)
            os.environ["PATH"] = binp
            config.Config().ensure_dirs()
            cli = load_cli()
            return _capture(lambda: cli.do_doctor(config.Config()))
        finally:
            os.environ.clear()
            os.environ.update(old)
            shutil.rmtree(tmp, ignore_errors=True)

    # 1. Nothing configured at all: claudio's own words, exit 1, no recording
    #    line.  This is the case that used to be misreported.
    out = with_stub("#!/bin/sh\necho 'No status line configured.' >&2\nexit 1\n")
    check_true("doctor: nothing configured reads as recording OFF",
               "recording is OFF" in out)
    check_true("doctor: ...and names the config line, not the user's $PATH",
               "logging=local" in out and "not on PATH" not in out)

    # 2. claudio cannot be run at all. This is the only case that may blame
    #    $PATH, and it has to still be reachable or the fix above would have
    #    deleted a true diagnosis along with a false one.
    out = with_stub(None)
    check_true("doctor: an absent claudio is still reported as an absent claudio",
               "not on PATH" in out)

    # 3. Recording on: claudio's sentence is quoted rather than re-derived,
    #    because the layering behind it has a per-account inversion this side
    #    would get subtly wrong.
    out = with_stub("#!/bin/sh\necho 'Recording plan usage: on (logging=local)'\n")
    check_true("doctor: an on verdict is claudio's own sentence",
               "recording is on (logging=local)" in out)

    # 4. claudio ran and said something this reader does not understand. Not
    #    "off" -- a guess in either direction is what the three-way split
    #    exists to refuse.
    out = with_stub("#!/bin/sh\necho 'something else entirely'\n")
    check_true("doctor: an unrecognised answer is neither on nor off",
               "reported no recording state" in out)

    # 5. Found, and broken. A claudio whose shebang cannot resolve, or one too
    #    old to have the verb, exits non-zero with its complaint on stderr --
    #    and "reported no recording state" would imply it ran successfully,
    #    which is the same class of invented cause as "not on PATH", one step
    #    milder. Quote what it said.
    out = with_stub("#!/bin/sh\necho 'boom' >&2\nexit 3\n")
    check_true("doctor: a claudio that failed to run quotes its own error",
               "exited 3" in out and "boom" in out)
    check_true("doctor: ...and does not claim it ran successfully",
               "reported no recording state" not in out)


def test_doctor_asks_the_claudio_that_ran_it_not_the_one_on_path():
    """`CLAUDIO_SELF`, and why a bare name was the wrong question to ask.

    `claudio usage` resolves this program through `_tool_path`'s three layouts
    -- checkout, package, `$PATH` -- and then handed nothing back, so the
    Python resolved back to claudio through the third alone.  Reproduced on a
    developer machine: `./claudio statusline` printed `Recording plan usage: on
    (logging=local)` while `./usage/claudio-usage doctor`, run from the same
    checkout in the same environment, printed `WARN recording is OFF -- claudio
    reports nothing configured at all; add logging=local to <conf>` and named
    the very file that already contained that line.  It had asked
    /usr/local/bin/claudio: a different build reading a different config.

    Unhelpful became CONFIDENTLY WRONG, from the one command whose job is to
    say what is wrong.

    Pinned with two stubs that CONTRADICT each other -- one on `$PATH`, one
    named by `CLAUDIO_SELF` -- because a single stub cannot tell "asked the
    right one" from "there was only one to ask".
    """
    tmp = tempfile.mkdtemp()
    old = dict(os.environ)
    try:
        os.environ["CLAUDIO_USAGE_DIR"] = tmp
        binp = os.path.join(tmp, "bin")
        os.mkdir(binp)
        on_path = os.path.join(binp, "claudio")
        with open(on_path, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho 'No status line configured.' >&2\nexit 1\n")
        os.chmod(on_path, 0o755)
        elsewhere = os.path.join(tmp, "the-real-one")
        with open(elsewhere, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\necho 'Recording plan usage: on (logging=local)'\n")
        os.chmod(elsewhere, 0o755)
        os.environ["PATH"] = binp
        config.Config().ensure_dirs()
        cli = load_cli()

        # Negative control first: with nothing exported, the one on $PATH is
        # what answers -- so the assertion below is about the export and not
        # about which stub happens to be reachable.
        os.environ.pop("CLAUDIO_SELF", None)
        out = _capture(lambda: cli.do_doctor(config.Config()))
        check_true("doctor: with no CLAUDIO_SELF it falls back to $PATH",
                   "recording is OFF" in out)
        check_true("doctor: ...and says so, so the answer is attributable",
                   "asking claudio: claudio (first on $PATH)" in out)

        os.environ["CLAUDIO_SELF"] = elsewhere
        out2 = _capture(lambda: cli.do_doctor(config.Config()))
        check_true("doctor: CLAUDIO_SELF is the claudio that gets asked",
                   "recording is on (logging=local)" in out2)
        check_true("doctor: ...and the header names which one it asked",
                   "asking claudio: %s" % elsewhere in out2)
        check_true("doctor: ...and the one on $PATH did not answer",
                   "recording is OFF" not in out2)
    finally:
        os.environ.clear()
        os.environ.update(old)
        shutil.rmtree(tmp, ignore_errors=True)


def test_doctor_names_the_directory_the_data_was_renamed_out_of():
    """Two zeroes, or the reason for them.

    `~/.claude-usage` became `~/.claudio-usage` in the same pass that folded
    these tools into claudio as subcommands, and nothing looked at the old
    path.  A machine that had been recording for months and had not pinned
    `usage_dir=` got a fresh empty directory: `show` printed "No requests
    recorded yet.", `doctor` printed `WARN plan samples: 0` and `WARN ledger
    rows: 0` -- byte-indistinguishable from a machine that never recorded
    anything.  Two states, one message, and the message asserts the false one.

    BOTH conditions are asserted, because either alone makes the line noise: a
    machine already migrated has data in the new place, and a machine that
    never used the old name has no such directory at all.
    """
    home = tempfile.mkdtemp()
    old = dict(os.environ)
    try:
        os.environ["HOME"] = home
        os.environ.pop("CLAUDIO_USAGE_DIR", None)
        newdir = os.path.join(home, ".claudio-usage")
        olddir = os.path.join(home, ".claude-usage")
        os.makedirs(newdir)
        cli = load_cli()
        cfg = config.Config()
        cfg.dir = newdir            # HOME is read at import time in some paths
        cfg.path = os.path.join(newdir, "config")

        check("predecessor: with no old directory there is nothing to say",
              cli._predecessor_dir(cfg), None)

        os.makedirs(olddir)
        with open(os.path.join(olddir, "ledger.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(_real_rows()[0]) + "\n")
        check("predecessor: an old directory with records is named",
              cli._predecessor_dir(cfg), olddir)

        out = _capture(lambda: cli.do_doctor(cfg))
        check_true("predecessor: doctor names it, with the move as the remedy",
                   "was renamed" in out and "mv %s %s" % (olddir, newdir) in out)
        check_true("predecessor: ...and offers keeping the old path instead",
                   "usage_dir=%s" % olddir in out)

        # Already migrated: the new directory has records too, so the line
        # would be noise and is not printed.
        with open(os.path.join(newdir, "ledger.jsonl"), "w",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(_real_rows()[0]) + "\n")
        check("predecessor: a machine already migrated is not warned",
              cli._predecessor_dir(cfg), None)
    finally:
        os.environ.clear()
        os.environ.update(old)
        shutil.rmtree(home, ignore_errors=True)


def test_doctor_reports_a_doubled_ledger():
    """The read-side dedupe repairs the totals; doctor still says the file is wrong.

    A ledger doubled by two concurrent ingests reads correctly now, which is
    the point of deduping on read -- and is exactly why doctor has to say so
    out loud, because nothing else will and the file on disk is still wrong
    for every other reader of it.
    """
    def seed(cfg):
        rows = _real_rows()
        ledger.append(cfg.ledger, rows)
        ledger.append(cfg.ledger, rows)             # the reproduced doubling

    rc, out = _doctor_rc(seed=seed)
    check_true("doctor: the doubled rows are counted",
               "WARN ledger duplicates: 8 row(s)" in out)
    check_true("doctor: the count itself is already repaired on read",
               "ledger rows: 8" in out)
    check("doctor: a doubled ledger is not a healthy one", rc, 1)


# ================================= 8. STOPPING: LABELS, NOT LOST RECORDS ====

def test_stop_message_states_a_labelling_loss_not_a_data_one():
    """The distinction the measurement forced, pinned as text.

    Three documents claimed the file exporter buffers ~4KB pages and that a
    `docker kill` silently discards the tail, so a hard stop lost data.
    Reproduced twice on 0.158.0: 5 records POSTed, host-side `wc -c` on the
    bind-mounted file read 0 bytes -- then SIGKILL and `docker rm -f`, and all
    5 records, 3355 bytes, were there.  The 0 was macOS VirtioFS bind-mount
    coherence lag.  With `rotation:` the exporter writes through lumberjack,
    unbuffered per write.

    So what an unobserved stop costs is the *label*, and saying otherwise
    sends the reader hunting for records that were never lost.
    """
    src = open(os.path.join(ROOT, "cu", "collector.py"), encoding="utf-8").read()
    check_true("stop docs: the module records the bind-mount lesson",
               "bind mount" in src and "VirtioFS" in src)

    # EVERY doc under usage/, not one module.  Three files were corrected and a
    # fourth was missed: `recv/README.md` went on asserting the buffering claim
    # verbatim, next to the very `collect start`/`collect stop` commands it
    # describes, so the repo said both things at once and the copy most likely
    # to be believed was the measured-false one.  A scan of a single file could
    # not see it.  Scanning by directory means the next copy cannot survive
    # either -- which is the only reason this is a test and not a commit.
    docs = []
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in ("tests", "__pycache__", ".git", "fixtures")]
        for f in sorted(files):
            # The two extensionless entry points are named, because both carry
            # architecture prose and neither ends in `.py`.  `recv/otlp-recv`
            # is the one that inherited the collector's docstring wholesale.
            if f.endswith(".md") or f.endswith(".py") \
                    or f in ("claudio-usage", "otlp-recv"):
                docs.append(os.path.join(base, f))
    check_true("stop docs: the scan reaches recv/README.md",
               os.path.join(ROOT, "recv", "README.md") in docs)
    check_true("stop docs: and the receiver itself",
               os.path.join(ROOT, "recv", "otlp-recv") in docs)
    check_true("stop docs: and the top-level README", os.path.join(ROOT, "README.md") in docs)

    # The old claim may only appear as something being refuted, and three
    # things about *how* that is checked are the whole strength of the check:
    #
    #   - the text is flattened first.  `usage/README.md` carries "does not\n>
    #     flush on idle" -- the claim, spelled exactly, and invisible to a
    #     literal `in` because prose wraps and quote markers and comment
    #     hashes sit at the start of the next line.
    #   - EVERY occurrence is checked, not the first.  A file that refutes the
    #     claim in one paragraph and asserts it in the next is precisely the
    #     state the repo was in at file scale, and `find` sees only the good
    #     one.
    #   - the window is tight.  The furthest any real refutation sits from its
    #     claim is 114 characters, so 200 is slack rather than licence; at 500
    #     an uncorrected paragraph passes by standing next to a corrected one.
    #     If a future rewording trips this, move the refutation nearer the
    #     claim -- a reader scanning the page needs it there anyway.
    flat = lambda s: re.sub(r"[\s>#]+", " ", s)
    for path in docs:
        body = flat(open(path, encoding="utf-8", errors="replace").read())
        rel = os.path.relpath(path, ROOT)
        marks = [m.start() for m in re.finditer("false", body.lower())]
        for phrase in ("buffers ~4KB pages", "does not flush on idle",
                       "flushes the tail", "discards the tail"):
            for m in re.finditer(re.escape(phrase), body):
                check_true("stop docs: %s says %r only as a refutation"
                           % (rel, phrase),
                           any(abs(x - m.start()) <= 200 for x in marks))


# ================================ 9. THE RECEIVER: STREAM A'S ONLY DOOR ====
#
# These run the real `recv/otlp-recv` as a subprocess on an ephemeral port.
# Nothing here is stubbed, because the properties under test are the ones a
# stub cannot have: a socket that binds, a file that is on disk before a
# response, and a process that exits by itself.


def _recv_env(tmp, **extra):
    env = dict(os.environ)
    env["CLAUDIO_USAGE_DIR"] = tmp
    env.update({k: str(v) for k, v in extra.items()})
    return env


def _spawn_recv(tmp, args=(), env=None, wait=True, timeout=8.0):
    """Start a receiver in `tmp`.  Returns (proc, pidfile-dict-or-None)."""
    proc = subprocess.Popen(
        [sys.executable, RECV_PATH, "serve", "--port", "0", "--quiet"] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env or _recv_env(tmp), text=True)
    if not wait:
        return proc, None
    old = os.environ.get("CLAUDIO_USAGE_DIR")
    os.environ["CLAUDIO_USAGE_DIR"] = tmp
    try:
        cfg = config.Config()
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = collector.read_pidfile(cfg)
            if got and got.get("port"):
                return proc, got
            if proc.poll() is not None:
                return proc, None
            time.sleep(0.02)
    finally:
        if old is None:
            os.environ.pop("CLAUDIO_USAGE_DIR", None)
        else:
            os.environ["CLAUDIO_USAGE_DIR"] = old
    return proc, None


def _kill_recv(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _post(port, path, body, ctype="application/json"):
    """POST and return (status, body-bytes).  Raises nothing for 4xx/5xx."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                 data=body, method="POST",
                                 headers={"Content-Type": ctype})
    try:
        with urllib.request.urlopen(req, timeout=5) as fh:
            return fh.status, fh.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _healthz(port):
    with urllib.request.urlopen("http://127.0.0.1:%d/healthz" % port,
                                timeout=5) as fh:
        return json.loads(fh.read().decode())


def _metrics_payload():
    """One `claude_code.token.usage` point, in the shape recv/README documents."""
    return json.dumps({"resourceMetrics": [{
        "resource": {"attributes": [
            {"key": "account", "value": {"stringValue": "agent"}}]},
        "scopeMetrics": [{"metrics": [{
            "name": "claude_code.token.usage", "unit": "tokens",
            "sum": {"dataPoints": [{"timeUnixNano": "1786584384665000000",
                                    "asInt": "893",
                                    "attributes": [{"key": "type", "value": {
                                        "stringValue": "output"}}]}]}}]}]}]})


def _log_payload(event="api_request", extra_attrs=(), ts_ns=1_786_584_384_665_000_000):
    attrs = [{"key": "event.name", "value": {"stringValue": event}},
             {"key": "session.id", "value": {"stringValue": "sess-recv"}},
             {"key": "model", "value": {"stringValue": "claude-opus-5[1m]"}},
             {"key": "input_tokens", "value": {"intValue": "11"}},
             {"key": "output_tokens", "value": {"intValue": "22"}},
             {"key": "cost_usd", "value": {"doubleValue": 0.5}}]
    attrs += [{"key": k, "value": {"stringValue": v}} for k, v in extra_attrs]
    return json.dumps({"resourceLogs": [{
        "resource": {"attributes": [
            {"key": "account", "value": {"stringValue": "agent"}},
            {"key": "verify", "value": {"stringValue": "pipeline"}}]},
        "scopeLogs": [{"logRecords": [
            {"timeUnixNano": str(ts_ns), "body": {"stringValue": ""},
             "attributes": attrs}]}]}]})


def test_a_crash_loop_refuses_itself():
    """Written before the supervisor, because the supervisor is the hazard.

    A receiver that cannot bind dies instantly.  A supervisor that checks
    `kill -0` on every status-line render -- several times a second -- and
    respawns on failure is then a fork bomb, and an unkillable respawn loop is
    strictly worse than having no supervisor at all: it burns the machine while
    recording nothing, and the user's only symptom is a slow laptop.

    The cap is enforced in two places on purpose.  The supervisor will carry
    the cheap half (it must, because refusing *here* still costs a Python
    fork).  This half is the backstop that holds when the supervisor is wrong,
    and it is the half that can leave a `recv.err` saying why nothing is
    recording -- the supervisor cannot, because it never gets to run anything
    that knows.

    Only starts that never reached a bind count: a successful bind truncates
    the file, so a busy day of legitimate sessions can never trip it.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        for _ in range(collector.MAX_STARTS):
            collector.note_start_attempt(cfg)

        proc, got = _spawn_recv(tmp, wait=False)
        rc = proc.wait(timeout=10)
        check("crash loop: the receiver refuses to start past the cap", rc, 3)
        check("crash loop: and it never binds", collector.read_pidfile(cfg), None)
        err = collector.read_error(cfg) or ""
        check_true("crash loop: recv.err says why, for the next `claudio run`",
                   "refusing to start" in err and "never reached a successful "
                   "bind" in err)

        # The window is what makes this recoverable rather than a wall: old
        # attempts age out, so a machine that was broken an hour ago starts.
        old = time.time() - collector.START_WINDOW - 1
        with open(cfg.recv_starts, "w", encoding="utf-8") as fh:
            fh.write("".join("%.3f\n" % old for _ in range(20)))
        proc, got = _spawn_recv(tmp)
        try:
            check_true("crash loop: attempts outside the window do not count",
                       got is not None)
            check_true("crash loop: a successful bind clears the accounting",
                       not os.path.exists(cfg.recv_starts))
            check("crash loop: and clears the stale recv.err with it",
                  collector.read_error(cfg), None)
        finally:
            _kill_recv(proc)


def test_the_receiver_binds_loopback_and_refuses_anything_else():
    """0.0.0.0 is refused, not clamped.

    This process holds an unauthenticated firehose of one person's request
    metadata -- model, token counts, cost, session ids and their email address
    -- with no authentication of any kind, because on loopback there is nobody
    to authenticate.  Binding it to a network interface would publish all of
    that, and a caller who asked for `0.0.0.0` and got `127.0.0.1` silently
    would be left believing something false about their own machine.  So it
    refuses, says so in `recv.err`, and exits non-zero.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        proc, _ = _spawn_recv(tmp, args=["--host", "0.0.0.0"], wait=False)
        rc = proc.wait(timeout=10)
        check("loopback: a non-loopback bind is refused", rc, 2)
        check("loopback: nothing was bound", collector.read_pidfile(cfg), None)
        check_true("loopback: and recv.err names the address it refused",
                   "0.0.0.0" in (collector.read_error(cfg) or ""))

        # The default really is loopback, not merely documented as such.
        proc, got = _spawn_recv(tmp)
        try:
            check_true("loopback: the default bind answers on 127.0.0.1",
                       _healthz(got["port"])["status"] == "ok")
        finally:
            _kill_recv(proc)


def test_a_payload_is_on_disk_before_the_200():
    """The property the whole on-demand design rests on.

    If a record can be in memory when the response goes back, then every
    shutdown path becomes load-bearing for data integrity, and an on-demand
    process that exits by itself is a bad idea.  It is not: the payload is one
    `write(2)` on an `O_APPEND` file *before* the status line is written, so a
    receiver killed a microsecond after the 200 has already lost nothing.

    The check is made from the client, with no sleep between the response and
    the read.  A second, negative probe is what makes it an ordering assertion
    rather than a "did anything ever get written" one: before the POST the file
    does not exist at all, so `existed_after` cannot pass by accident.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        proc, got = _spawn_recv(tmp)
        try:
            check_true("durability: the capture does not exist before the POST",
                       not os.path.exists(cfg.raw_file))
            status, body = _post(got["port"], "/v1/logs", _log_payload())
            size = os.path.getsize(cfg.raw_file) if os.path.exists(cfg.raw_file) else 0
            check("durability: the payload is accepted", status, 200)
            check("durability: with the OTLP empty-success body", body, b"{}")
            check_true("durability: and it is on disk by the time the 200 "
                       "arrives", size > 0)

            # One payload, one line, and the line starts with the key
            # `cu/otlp.py` cuts back to when it recovers a torn write.
            lines = open(cfg.raw_file, encoding="utf-8").read().splitlines()
            check("durability: one payload is one line", len(lines), 1)
            check_true("durability: the line starts with {\"resourceLogs\"",
                       lines[0].startswith('{"resourceLogs"'))
        finally:
            _kill_recv(proc)


def test_metrics_are_accepted_and_discarded():
    """Accepted, counted, dropped -- and never written.

    Claude Code's metrics are pre-aggregated: no per-request granularity, so
    there is nothing in them a ledger row could be made of, and `session.id` on
    them would make unbounded-cardinality series.  Answering 404 would be
    honest and useless, because an OTLP client treats 4xx and 5xx as retryable
    and would send the same body for ever.  So the drop is deliberate, the
    success shape is exact, and the count is the only evidence it happened.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        proc, got = _spawn_recv(tmp)
        try:
            status, body = _post(got["port"], "/v1/metrics", _metrics_payload())
            check("metrics: accepted", status, 200)
            check("metrics: with the empty-success body that stops the retry",
                  body, b"{}")
            check_true("metrics: and nothing is written to the capture",
                       not os.path.exists(cfg.raw_file)
                       or os.path.getsize(cfg.raw_file) == 0)
            h = _healthz(got["port"])
            check("metrics: the payload is counted as arrived",
                  h["payloads"]["metrics"], 1)
            check("metrics: and counted as dropped", h["dropped"]["metrics"], 1)
            check("metrics: no log record was invented", h["accepted"], 0)

            # A protobuf body cannot be decoded here, and a protobuf client
            # must still get a valid empty message: zero bytes IS
            # ExportLogsServiceResponse{}.  A JSON `{}` in a protobuf response
            # is a decode error at the client, which is a retry.
            status, body = _post(got["port"], "/v1/logs", b"\x0a\x00",
                                 ctype="application/x-protobuf")
            check("protobuf: answered 200 so the client does not spin", status, 200)
            check("protobuf: with an empty protobuf message, not JSON", body, b"")
            # In recv.WARN, and provably not in recv.err: this receiver is up
            # and serving, and claudio's supervisor stands down for as long as
            # recv.err exists.  One protobuf body used to disarm the respawn
            # for that session and every later one -- the receiver would be
            # killed an hour later and nothing would ever bring it back.
            check_true("protobuf: and the misconfiguration is in recv.warn",
                       "otel_protocol=http/json"
                       in (collector.read_warning(cfg) or ""))
            check("protobuf: a running receiver never writes the file the "
                  "supervisor gates on", collector.read_error(cfg), None)
            check("protobuf: counted as undecodable",
                  _healthz(got["port"])["undecodable"], 1)
        finally:
            _kill_recv(proc)


def test_counters_name_the_drift_at_the_door():
    """The blind spot the audit named, closed structurally.

    `raw/api_request.jsonl` used to be the *collector's own output*, so nothing
    downstream could see what never arrived: a field deleted by `keep_keys` was
    invisible in the only file anyone read, which is how `prompt.id`, every
    user `--tag` and `agent.name` were each lost in silence, and how a filter
    naming an event Claude Code does not send dropped every record for an
    evening while looking exactly like an idle machine.

    The receiver keeps everything, so the file itself is now the evidence -- and
    the counters name the two things a reader would otherwise have to diff for:
    every distinct `event.name` seen, and every attribute key that arrived and
    that the ledger has no column for.
    """
    with _datadir() as tmp:
        proc, got = _spawn_recv(tmp)
        try:
            # The historical bug, replayed: the collector's filter named
            # `claude_code.api_request` and Claude Code sends `api_request`.
            _post(got["port"], "/v1/logs",
                  _log_payload(event="claude_code.api_request"))
            # And a key that really is sent and really is not stored:
            # `cost_usd_micros` was in the collector's allow-list and has never
            # had a ledger column.
            _post(got["port"], "/v1/logs",
                  _log_payload(event="user_prompt",
                               extra_attrs=[("cost_usd_micros", "500000")]))
            h = _healthz(got["port"])
            check("counters: every event name seen is named",
                  sorted(h["events"]), ["claude_code.api_request", "user_prompt"])
            check("counters: an arriving key nothing stores is counted",
                  h["unknown_attributes"].get("cost_usd_micros"), 1)

            txt = collector.reconcile_text(h)
            check_true("counters: drift is called out, not implied",
                       "no api_request records arrived" in txt)
            check_true("counters: and the events are named beside it",
                       "user_prompt 1" in txt)
            check_true("counters: the unstored key is named too",
                       "cost_usd_micros" in txt)

            # Two records arrived and two are on disk: the receiver drops
            # nothing, so `accepted` and `written` may only differ on a write
            # failure -- which has its own alarm.
            check("counters: nothing was dropped on the way to disk",
                  (h["accepted"], h["written"]), (2, 2))
        finally:
            _kill_recv(proc)

    # The healthy shape, for contrast: no alarm clause at all.
    ok = {"accepted": 5, "written": 5, "events": {"api_request": 5}}
    check_true("counters: a healthy reconciliation says nothing alarming",
               "<--" not in collector.reconcile_text(ok))
    check_true("counters: an accepted record that never landed is an alarm",
               "nothing reached disk" in
               collector.reconcile_text({"accepted": 5, "written": 0,
                                         "events": {"api_request": 5}}))
    check_true("counters: no counters at all is not a confident zero",
               "no counters" in collector.reconcile_text(None))


def test_idle_exit_needs_both_conditions():
    """No records for `idle_minutes` AND no live session.  Both, always.

    Either alone is wrong in a way that costs data or costs a machine.  A timer
    alone orphans a user who spends forty minutes reading with claude still
    open -- their next request exports into nothing, silently, which is the
    cardinal sin here.  A session check alone never exits at all once a session
    file is stranded by a crash, and an on-demand process that never leaves is
    just a daemon nobody chose to install.

    The sweep is what keeps the second condition honest, and it is tested with
    a pid that is genuinely dead rather than one assumed to be free.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        # A session that is certainly alive: this test process.
        live_file = collector.record_session(cfg, os.getpid())
        # And one that is certainly dead: a process we have already reaped.
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        dead_file = collector.record_session(cfg, dead.pid)

        proc, got = _spawn_recv(tmp, args=["--idle-seconds", "0.4",
                                           "--check-seconds", "0.1"])
        try:
            check_true("idle exit: the receiver came up", got is not None)
            time.sleep(1.5)             # comfortably past the idle threshold
            check_true("idle exit: a live session holds it up despite the timer",
                       proc.poll() is None)
            check_true("idle exit: and the dead session entry was swept",
                       not os.path.exists(dead_file))
            check_true("idle exit: while the live one is left alone",
                       os.path.exists(live_file))

            os.unlink(live_file)
            proc.wait(timeout=10)
            check("idle exit: with no session and no records, it leaves",
                  proc.returncode, 0)
            check("idle exit: and takes its pidfile with it",
                  collector.read_pidfile(cfg), None)

            recs = collector.read_uptime(cfg)
            check("idle exit: the span was closed by the receiver itself",
                  recs[-1]["event"], "stop")
            check("idle exit: and closed CLEAN, because it was observed",
                  recs[-1]["clean"], True)
            check_true("idle exit: the reason says which condition fired",
                       "idle" in (recs[-1].get("reason") or ""))
        finally:
            _kill_recv(proc)

    # And the other half of the `and`: no session at all, but a record arriving
    # keeps it up -- the timer measures silence, not uptime.
    with _datadir() as tmp:
        proc, got = _spawn_recv(tmp, args=["--idle-seconds", "1.0",
                                           "--check-seconds", "0.1"])
        try:
            for _ in range(4):
                time.sleep(0.4)
                _post(got["port"], "/v1/logs", _log_payload())
            check_true("idle exit: traffic resets the idle clock",
                       proc.poll() is None)
            proc.wait(timeout=10)
            check("idle exit: and once it stops, so does the receiver",
                  proc.returncode, 0)
        finally:
            _kill_recv(proc)


def test_the_idle_decision_is_re_asked_before_it_is_acted_on():
    """A receiver may not exit into a session that has just registered.

    `claudio run` asks whether the receiver is alive and registers its session
    afterwards.  The watchdog decided from the sweep at the top of its tick and
    never looked again, so a tick landing in that gap ended a receiver a
    launching session had just been told it could export to.  Reproduced
    against the real receiver: `alive: True` at t=5.98, session registered at
    t=6.08, `[Errno 61] Connection refused` at t=8.08, and the receiver's own
    uptime record reading "idle: no records for 6s and no live session".

    claudio now registers before it checks, which removes the tick-sized half
    of the window.  This is the other half: the count is asked again
    immediately before the shutdown, so a session that arrived during the tick
    is seen.

    Driven in-process, with `sweep_sessions` alternating 0 then 1, because
    that IS the race and a subprocess cannot be made to hit it on demand.
    `cmd_serve` installs signal handlers, which only the main thread may do,
    so the module's `signal` reference is stubbed for the duration.
    """
    recv = load_recv()
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()

        calls = []

        def racing(_cfg):
            # Odd call: the tick's own snapshot, nothing live.  Even call: the
            # re-check, which is where claudio's registration has landed.
            calls.append(1)
            return 0 if len(calls) % 2 else 1

        real_sweep = collector.sweep_sessions
        real_signal = recv.signal
        collector.sweep_sessions = racing
        recv.signal = types.SimpleNamespace(
            signal=lambda *a: None, SIGTERM=signal.SIGTERM,
            SIGINT=signal.SIGINT, SIGHUP=getattr(signal, "SIGHUP", None))
        thread = None
        try:
            ns = types.SimpleNamespace(host="127.0.0.1", port=0, quiet=True,
                                       idle_seconds=0.0, check_seconds=0.05,
                                       out=None)
            thread = threading.Thread(target=recv.cmd_serve, args=(ns,),
                                      daemon=True)
            thread.start()
            deadline = time.time() + 5
            while len(calls) < 6 and time.time() < deadline:
                time.sleep(0.02)
            check_true("idle race: the decision is re-asked, not taken from "
                       "the tick's snapshot", len(calls) >= 6)
            check_true("idle race: and a session that arrived in the window "
                       "keeps the receiver up", thread.is_alive())
        finally:
            collector.sweep_sessions = real_sweep
            recv.signal = real_signal
            if thread is not None:
                thread.join(timeout=15)

        # With the race over and nothing live, it still leaves by itself: the
        # re-check is a correction, not a way of never exiting.
        check_true("idle race: with no session at all it still exits",
                   thread is not None and not thread.is_alive())
        recs = collector.read_uptime(cfg)
        check("idle race: and the stop record says how many sessions were "
              "live when it went", recs[-1].get("sessions_at_exit"), 0)


def test_the_pidfile_carries_a_token_not_just_a_pid():
    """A recycled pid answers `kill -0`, and that answer is a lie.

    The supervisor's per-render check has to be a shell builtin, so it is
    `kill -0` and it cannot be exact.  The token is what makes *ownership*
    decisions exact anyway: a receiver shutting down while its replacement is
    already binding must not delete the new one's pidfile on the way out, or
    the supervisor respawns over a live process, the spawn cannot bind, and the
    user reads a `recv.err` about a fault that never happened.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        collector.write_pidfile(cfg, 4242, "aaaa", 4318, 1000.0)
        raw = open(cfg.recv_pid, encoding="utf-8").read().splitlines()
        # One field per line, because the other reader is POSIX sh: a tab is
        # IFS whitespace, so a tab-separated record loses an empty field and
        # shifts every later one.  `{ read pid; read token; } < recv.pid` is
        # two builtins and no fork, which is what a status line can afford.
        check("pidfile: four lines, one field each", len(raw), 4)
        check("pidfile: the pid is first, so `read pid` alone is enough",
              raw[0], "4242")
        got = collector.read_pidfile(cfg)
        check("pidfile: it reads back", (got["pid"], got["token"], got["port"]),
              (4242, "aaaa", 4318))

        collector.clear_pidfile(cfg, "bbbb")
        check_true("pidfile: another receiver's token cannot remove it",
                   os.path.exists(cfg.recv_pid))
        collector.clear_pidfile(cfg, "aaaa")
        check_true("pidfile: its own token can", not os.path.exists(cfg.recv_pid))

        # A truncated pidfile -- caught mid-write by a reader that is not
        # atomic -- must read as "no receiver", never as pid 0.
        with open(cfg.recv_pid, "w", encoding="utf-8") as fh:
            fh.write("4242\n")
        check("pidfile: a half-written file is not a receiver",
              collector.read_pidfile(cfg), None)


def test_a_failed_bind_is_written_where_the_next_run_prints_it():
    """The one failure that must never be silent.

    A receiver that cannot bind exits immediately, and the export it was meant
    to catch then fails quietly at the client -- Claude Code's exporter is
    silent by design.  Without `recv.err` the only symptom of a permanently
    occupied port is an empty ledger, which is indistinguishable from a quiet
    week.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        held = socket.socket()
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        try:
            proc = subprocess.Popen(
                [sys.executable, RECV_PATH, "serve", "--port", str(port),
                 "--quiet"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=_recv_env(tmp), text=True)
            rc = proc.wait(timeout=10)
            check_true("bind failure: it exits non-zero", rc != 0)
            err = collector.read_error(cfg) or ""
            check_true("bind failure: recv.err says it could not bind",
                       "cannot bind" in err)
            check_true("bind failure: and names the port", str(port) in err)
            check("bind failure: no pidfile is left behind",
                  collector.read_pidfile(cfg), None)
            check_true("bind failure: and the spawn lock is not left held",
                       not os.path.exists(cfg.recv_lock))
        finally:
            held.close()


def test_the_spawn_lock_is_a_handoff_and_never_a_hold():
    """`mkdir` is the atom, and a stranded lock must expire.

    A directory rather than a file because the other caller of this contract is
    a shell script: `mkdir` is atomic in POSIX and needs no helper, and the
    loser of the race walks away instead of waiting -- whoever won is about to
    bind the only port there is.

    It expires because a spawner killed between the `mkdir` and the exec would
    otherwise stop every future spawn for ever.  A permanent refusal to record
    is far worse than a rare double spawn, of which the loser simply fails to
    bind and exits.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        check_true("lock: the first claim wins", collector.claim_lock(cfg))
        check_true("lock: the second is refused", not collector.claim_lock(cfg))

        old = time.time() - collector.LOCK_STALE - 5
        os.utime(cfg.recv_lock, (old, old))
        check_true("lock: an abandoned lock is broken rather than obeyed",
                   collector.claim_lock(cfg))

        # The receiver drops it as soon as it has bound, whoever created it:
        # a live receiver is proof that no spawn is in progress.
        proc, got = _spawn_recv(tmp)
        try:
            check_true("lock: a bound receiver has released the lock",
                       not os.path.exists(cfg.recv_lock))
        finally:
            _kill_recv(proc)


def test_start_and_stop_observe_rather_than_assume():
    """`clean` is earned by the receiver living long enough to write it.

    The old bug is inverted rather than re-fixed: `docker stop -t 10` returns 0
    *after* escalating to SIGKILL (measured: rc 0, exit code 137) and the code
    wrote clean:true over it.  Here nothing in `stop()` can write a clean span
    at all -- only the receiver can, on its way out -- so the label cannot be
    optimistic.

    And a start must be able to say the receiver is still there a moment later.
    `docker run -d` returned on CREATE, so a config the collector could not
    parse exited immediately and `start` reported success over the corpse;
    every session after that exported into nothing, silently.  A spawn has
    exactly the same shape.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        # The library start binds the configured port, so give it a free one.
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        free = s.getsockname()[1]
        s.close()
        os.environ["CLAUDIO_USAGE_HTTP_PORT"] = str(free)
        cfg = config.Config()

        ok, msg = collector.start(cfg)
        try:
            check_true("start: it comes up: %s" % msg, ok)
            check_true("start: and says where it is listening", str(free) in msg)
            again, msg2 = collector.start(cfg)
            check_true("start: a second start is refused, not doubled", not again)
            check_true("start: and says why", "already running" in msg2)
            check_true("start: /healthz answers with the pidfile's token",
                       collector.verify_running(cfg) is True)

            ok, msg = collector.stop(cfg)
            check_true("stop: it stops", ok)
            recs = collector.read_uptime(cfg)
            check("stop: the receiver closed its own span", recs[-1]["event"], "stop")
            check("stop: clean, because it was observed", recs[-1]["clean"], True)
            check_true("stop: the message claims no flush that never happened",
                       "flushed" not in msg and "buffered" not in msg)
            check("stop: and the pidfile is gone",
                  collector.read_pidfile(cfg), None)
        finally:
            if collector.running(cfg):
                collector.stop(cfg)

        # A receiver killed outright: nobody observed a stop, so the span is
        # unclean -- and the message must say what that costs, which is the
        # label and never the records.
        proc, got = _spawn_recv(tmp)
        proc.kill()
        proc.wait(timeout=5)
        ok, msg = collector.stop(cfg)
        recs = collector.read_uptime(cfg)
        check_true("stop: a killed receiver still lets ingest run", ok)
        check("stop: its span is unclean", recs[-1]["clean"], False)
        check("stop: and the reason names what was seen",
              recs[-1]["reason"], "already_gone")
        check_true("stop: the records are reported as being on disk",
                   "on disk" in msg)
        check_true("stop: and no data loss is implied",
                   "may be missing" not in msg and "buffered tail" not in msg)


def test_the_capture_is_exactly_what_the_ledger_ingests():
    """End to end, with the keeping decision in one place.

    The receiver keeps every log record; `cu/otlp.py` decides which of them is
    a ledger row and *names* what it skipped.  This test exists because the
    alternative -- filtering at the door as the collector did -- puts the same
    decision in two implementations, which is how three fields were dropped in
    silence while each half looked correct beside the other.

    So: three records in, one row out, and the two that were not stored are
    named on the ingest report rather than vanishing.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        proc, got = _spawn_recv(tmp)
        try:
            _post(got["port"], "/v1/logs", _log_payload(event="user_prompt"))
            _post(got["port"], "/v1/logs", _log_payload(event="api_request"))
            _post(got["port"], "/v1/logs", _log_payload(event="assistant_response"))
        finally:
            _kill_recv(proc)

        cli = load_cli()
        # `--from-zero`, because the receiver now ingests on its own tick and
        # once more before it exits, so by the time anyone types `ingest` the
        # offset is already past these records.  Re-reading is safe at any time
        # -- rows dedupe on content -- and the drift report is what is under
        # test here, not the write.  That the automatic pass happened at all is
        # pinned separately, in
        # `test_the_receiver_ingests_on_its_own_tick_so_stream_a_can_ship`.
        out = _capture(lambda: cli.do_ingest(config.Config(), from_zero=True))
        rows = list(ledger.read(cfg.ledger))
        check("ingest: one api_request became one row", len(rows), 1)
        check("ingest: and it carries the resource tag the user invented",
              (rows[0].get("tags") or {}).get("verify"), "pipeline")
        check("ingest: the account resource attribute survived",
              rows[0]["account"], "agent")
        check_true("ingest: the two records it did not store are NAMED",
                   "assistant_response" in out and "user_prompt" in out)
        check_true("ingest: and counted", "skipped 2" in out)


def test_stored_attrs_cannot_drift_from_the_code_that_reads_them():
    """The list the door counts against is derived, not remembered.

    `otlp.STORED_ATTRS` is what the receiver uses to decide that an arriving
    attribute is one nothing keeps.  A hand-maintained copy of the reader's
    `a.get(...)` calls would recreate the blind spot one level up: a new column
    would be added to the ledger, the list would be forgotten, and the field
    would be reported for ever as "arrived, nothing keeps it" -- a false alarm
    is the same failure as a missing one, in the direction that trains people
    to ignore the counter.
    """
    src = open(os.path.join(ROOT, "cu", "otlp.py"), encoding="utf-8").read()
    body = src[src.index("def rows_from_payload"):]
    read = set(re.findall(r'a\.get\("([^"]+)"\)', body))
    check("stored attrs: the derivation found the reader's calls",
          len(read) > 10, True)
    check("stored attrs: every attribute the ledger reads is in the set",
          read - set(otlp.STORED_ATTRS), set())
    check("stored attrs: and the set claims nothing the ledger does not read",
          set(otlp.STORED_ATTRS) - read, set())


def test_the_capture_rotates_and_ingest_follows_it():
    """Verbatim capture grows faster than a filtered one did.

    The Docker collector rotated through lumberjack at 32MB/16 backups; nothing
    rotates for free once it is gone, and the file now holds `user_prompt` and
    `assistant_response` as well.  Rotation is safe on the ingest side by
    construction -- `raw_files` picks up every `api_request*.jsonl`, and
    `request_id` dedupes on content, so a re-read costs a parse and never a
    duplicate row -- but only if the rotated name is one `raw_files` matches.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        recv = load_recv()
        # Sized so that a payload fits and two do not: the rotation lands
        # between appends, which is the only state in which both a rotated part
        # and a live file exist -- and `raw_files` has to order those two.
        cap = recv.Capture(cfg.raw_file, max_bytes=800, max_backups=2)
        payload = json.loads(_log_payload())
        for _ in range(3):
            cap.append(payload)
        rotated = [f for f in os.listdir(cfg.raw_dir) if f != "api_request.jsonl"]
        check_true("rotation: the live file was rolled aside", len(rotated) >= 1)
        found = otlp.raw_files(cfg.raw_dir)
        check("rotation: every part is still ingestable", len(found),
              len(os.listdir(cfg.raw_dir)))
        check_true("rotation: the live file is read last, so history is in order",
                   found[-1] == cfg.raw_file)

        # The backup cap really caps.
        for _ in range(8):
            cap.append(payload)
        parts = [f for f in os.listdir(cfg.raw_dir) if f != "api_request.jsonl"]
        check_true("rotation: old parts are pruned to max_backups",
                   len(parts) <= 2)


def test_rotation_never_deletes_what_ingest_has_not_read():
    """The unlink had no interlock at all with the ingest offset.

    Reproduced with real processes at `raw_max_megabytes=1`,
    `raw_max_backups=2`: 6000 real payloads POSTed, then one `claudio-usage
    ingest` -> `ingested 1900 new requests`, 4100 records gone for good, with
    `doctor` printing `records: 6000 accepted -> 6000 written` directly above
    `ledger rows: 1900` and exiting 0.  A tight ingest loop still lost 830 of
    9000, because rotation outran the reader.

    A raw directory that grows past `raw_max_backups` is visible in a byte
    count and is emptied by ingesting; a deleted record is neither.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        recv = load_recv()
        payload = json.loads(_log_payload())
        cap = recv.Capture(cfg.raw_file, max_bytes=800, max_backups=1,
                           unread=recv._unread_by_ingest(cfg))
        for _ in range(6):
            cap.append(payload)
        parts = sorted(f for f in os.listdir(cfg.raw_dir)
                       if f != "api_request.jsonl")
        unread = recv._unread_by_ingest(cfg)
        check("rotation: nothing ingested, so no part was deleted despite "
              "max_backups=1", len(parts), 3)
        check("rotation: ...because every one of them is still unread",
              [f for f in parts if not unread(os.path.join(cfg.raw_dir, f))],
              [])

        # Ingested: the backlog is in the ledger, and the surplus may go.
        cli = load_cli()
        cli.do_ingest(cfg, quiet=True)
        for _ in range(2):
            cap.append(payload)
        left = sorted(f for f in os.listdir(cfg.raw_dir)
                      if f != "api_request.jsonl")
        check_true("rotation: once the records are rows, pruning resumes",
                   len(left) < len(parts))
        check("rotation: and what survives is what ingest has not read yet",
              [f for f in left if not unread(os.path.join(cfg.raw_dir, f))
               and f not in parts], [])

        # The keep is not silent: `recv.warn` is printed by `claudio run` and
        # by `doctor`, which is where a growing raw directory gets explained.
        check_true("rotation: and a kept part is said out loud when it happens",
                   True)

    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        recv = load_recv()
        notes = []
        cap = recv.Capture(cfg.raw_file, max_bytes=800, max_backups=1,
                           unread=recv._unread_by_ingest(cfg),
                           note=notes.append)
        for _ in range(6):
            cap.append(json.loads(_log_payload()))
        check_true("rotation: the keep is reported, never silent",
                   any("ingest has not read them" in n for n in notes))


def test_the_receiver_ingests_on_its_own_tick_so_stream_a_can_ship():
    """Nothing on any automatic path ingested, so stream A never left.

    `do_ingest` was reached from exactly two places, both in `claudio-usage
    main()`: the `ingest` verb and `collect stop`.  Not `claudio run`, not the
    receiver's watchdog, not `ship.tick`.  The receiver wrote `raw/`, the
    shipper read `ledger.jsonl`, and with no ingester between them the ledger
    never existed -- so `_ship_stream` answered "no such file yet; nothing to
    say", `run_pass` recorded no failure and cleared `ship.err`.

    Reproduced with real processes: 7 real OTLP payloads captured durably, the
    real door, several shipping ticks, ZERO stream-A batches accepted, and both
    ship.err and ship.warn absent.  Dropping `real-samples.jsonl` into place
    made stream B ship immediately -- so the machine delivered plan percentages
    and no request rows, which on the server is indistinguishable from a
    machine that made no requests.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        proc, got = _spawn_recv(tmp, args=["--check-seconds", "0.2"])
        try:
            check_true("recv/ingest: the receiver came up", bool(got))
            _post(got["port"], "/v1/logs", _log_payload())
            deadline = time.time() + 8
            rows = []
            while time.time() < deadline:
                rows = list(ledger.read(cfg.ledger))
                if rows:
                    break
                time.sleep(0.05)
            check("recv/ingest: the captured record became a ledger row with "
                  "nobody typing `ingest`", len(rows), 1)
        finally:
            _kill_recv(proc)

        # And the final pass ingests too, so a record captured between the last
        # tick and the exit is not left for the next session.
        proc, got = _spawn_recv(tmp, args=["--check-seconds", "600"])
        try:
            check_true("recv/ingest: a second receiver came up", bool(got))
            _post(got["port"], "/v1/logs",
                  _log_payload(ts_ns=1_786_584_999_000_000_000))
        finally:
            _kill_recv(proc)
        check("recv/ingest: the record captured after the last tick is a row "
              "too", len(list(ledger.read(cfg.ledger))), 2)


def test_the_socket_and_the_pidfile_are_released_before_the_final_ship():
    """The half-dead window: nothing served, everything still claiming to be.

    In `cmd_serve`'s `finally`, `ship.tick` ran first and `clear_pidfile` /
    `server_close` ran last.  `serve_forever` had returned, so nothing was
    being served -- but the socket was still bound and `recv.pid` still named a
    live pid, which is exactly what claudio's supervisor (`kill -0` on the
    render path) and `claudio run` read as "the receiver is up, export away".
    Measured: 0.7 s after SIGTERM the pidfile was present, `kill -0` said
    alive, and a POST died with ConnectionResetError after 3.3 s -- records
    Claude Code's exporter drops in silence.

    Before the shipper existed that window was two small file writes.  It is
    now a network call bounded only by TIMEOUT x MAX_BATCHES_PER_PASS per
    destination per stream, so a hung door holds the receiver there for hours.
    A blackhole destination -- a listening socket that never answers -- is what
    that looks like.
    """
    blackhole = socket.socket()
    blackhole.bind(("127.0.0.1", 0))
    blackhole.listen(1)                      # accepted by the kernel, never read
    dead_port = blackhole.getsockname()[1]
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        acct = os.path.join(tmp, "accounts", "alpha")
        os.makedirs(acct, exist_ok=True)
        with open(os.path.join(acct, ".claude.json"), "w", encoding="utf-8") as fh:
            json.dump({"oauthAccount": {"accountUuid": SHIP_UUID["alpha"],
                                        "emailAddress": SHIP_EMAIL["alpha"]}}, fh)
        conf = os.path.join(tmp, "claudio.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write("logging=remote\n"
                     "account.alpha.ship_url=http://127.0.0.1:%d/v1/ship\n"
                     % dead_port)
        ledger.append(cfg.ledger, _real_rows())
        env = _recv_env(tmp)
        env["CLAUDIO_CONF"] = conf
        env["CLAUDIO_ACCOUNTS"] = os.path.join(tmp, "accounts")

        proc, got = _spawn_recv(tmp, args=["--check-seconds", "600"], env=env)
        try:
            check_true("recv/exit: the receiver came up", bool(got))
            port = got["port"]
            proc.terminate()
            # The final pass is now hanging on the blackhole.  Everything the
            # supervisor reads must already say "gone".
            deadline = time.time() + 5
            released = None
            while time.time() < deadline:
                if collector.read_pidfile(cfg) is None:
                    released = time.time()
                    break
                time.sleep(0.02)
            check_true("recv/exit: the pidfile is cleared before the shipping "
                       "pass finishes", released is not None)
            check_true("recv/exit: ...while the process is still shipping",
                       proc.poll() is None)
            probe = socket.socket()
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                bound = True
            except OSError:
                bound = False
            finally:
                probe.close()
            check_true("recv/exit: ...and the port is free for a replacement",
                       bound)
        finally:
            proc.kill()
            proc.wait(timeout=5)
            blackhole.close()


def test_doctor_names_records_that_arrived_and_never_became_rows():
    """Two numbers this function already printed, with nothing comparing them.

    `ok  counters: records: 6000 accepted -> 6000 written; events: api_request
    6000` sat three lines above `ok  ledger rows: 1900`, and `doctor` exited 0.
    The door's count is per receiver LIFETIME and the ledger is cumulative, so
    the ledger is normally the larger of the two -- which is exactly what makes
    the other direction worth a WARN.
    """
    with _datadir(path="") as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        with open(cfg.recv_counters, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "accepted": 5, "written": 5,
                       "events": {otlp.EVENT_NAME: 5}, "payloads": {},
                       "dropped": {}, "bytes": 0, "undecodable": 0,
                       "write_failed": 0, "rejected": 0,
                       "unknown_attributes": {}}, fh)
        ledger.append(cfg.ledger, _real_rows()[:2])
        out = _capture(lambda: load_cli().do_doctor(cfg))
        check_true("doctor: the gap between the door and the ledger is named",
                   "have not become rows" in out)
        check_true("doctor: ...with both numbers in the line",
                   "5 api_request record(s)" in out and "holds 2 row(s)" in out)
        rc = load_cli().do_doctor(cfg)
        check("doctor: and it fails the exit status", rc, 1)

        # The ordinary direction -- a cumulative ledger ahead of one lifetime's
        # counters -- is not an alarm.
        ledger.append(cfg.ledger, _real_rows()[2:])
        out2 = _capture(lambda: load_cli().do_doctor(cfg))
        check_true("doctor: a ledger ahead of the counters says nothing",
                   "have not become rows" not in out2)


def test_a_torn_ledger_line_costs_itself_and_never_the_record_behind_it():
    """`append` used to write a good row onto the back of a fragment.

    A process killed mid-append -- or an ENOSPC -- leaves a line with no
    trailing newline.  The next append wrote straight onto it, so one torn
    fragment plus one perfectly good row became one unparseable line: `read`
    reported `malformed: 1` and the good row was gone.  The raw offset had
    already advanced, so a normal `ingest` never regenerated it, and
    `ship._select` consumed the same bytes and advanced its offset past it too.
    `serve.Store._append` has had this guard from the start, with a comment
    saying it exists "instead of gluing a fragment onto the front of a good
    record and costing two"; this is the same fix on the other end of the wire.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        rows = _real_rows()
        ledger.append(cfg.ledger, rows[:2])
        with open(cfg.ledger, "a", encoding="utf-8") as fh:
            fh.write('{"request_id":"aaa3","ts":3.0,"mo')      # killed here

        stats = {}
        ledger.append(cfg.ledger, [dict(rows[2], request_id="bbb9")], stats)
        check("ledger: the torn line is terminated, and counted",
              stats["torn_lines_terminated"], 1)

        got = {r.get("request_id") for r in ledger.read(cfg.ledger)}
        check_true("ledger: the good row behind the fragment survives",
                   "bbb9" in got)
        check("ledger: and the two rows before it are untouched",
              len(got & {r["request_id"] for r in rows[:2]}), 2)

        rstats = {}
        list(ledger.read(cfg.ledger, rstats))
        check("ledger: the fragment is still reported as damage",
              rstats["malformed"], 1)

        # The shipper reads the same file, so the same row has to survive to
        # the wire rather than being consumed as one malformed line.
        acct = os.path.join(tmp, "accounts", "alpha")
        os.makedirs(acct, exist_ok=True)
        with open(os.path.join(acct, ".claude.json"), "w", encoding="utf-8") as fh:
            json.dump({"oauthAccount": {"accountUuid": SHIP_UUID["alpha"],
                                        "emailAddress": SHIP_EMAIL["alpha"]}}, fh)
        conf = os.path.join(tmp, "claudio.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write("logging=remote\n"
                     "account.alpha.ship_url=http://sink.example/v1\n")
        sink = _Sink()
        ship.run_pass(cfg, now=1786600000.0, post=sink,
                      conf=ship.read_conf(conf),
                      accounts=os.path.join(tmp, "accounts"))
        check_true("ledger: and it reaches the wire",
                   "bbb9" in {r["request_id"]
                              for r in sink.parsed(ship.STREAM_LEDGER)})

    # A pre-existing glued line -- written before the guard existed -- yields
    # its good half rather than costing two records.
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        good = json.dumps(dict(_real_rows()[0], request_id="ccc4"),
                          separators=(",", ":"))
        with open(cfg.ledger, "w", encoding="utf-8") as fh:
            fh.write('{"request_id":"aaa3","ts":3.0,"mo' + good + "\n")
        st = {}
        got = {r.get("request_id") for r in ledger.read(cfg.ledger, st)}
        check_true("ledger: the complete record behind an old glued line is "
                   "recovered", "ccc4" in got)
        check("ledger: ...and the tear is still counted, not painted over",
              (st["malformed"], st["recovered"]), (1, 1))


def test_the_ledger_and_its_offsets_are_fsynced_before_they_are_trusted():
    """The order in the code was right and the order on the disk was not.

    `_ingest_locked` appends and then saves the offsets.  A rename is a
    journalled metadata operation while appended data is unjournalled page
    cache, so after a power loss the offsets file can be durable while the rows
    it accounts for are not -- and ingest then resumes past raw bytes it never
    turned into rows.  Because the raw file is rotated and deleted, those
    records are unrecoverable: `--from-zero` cannot help once raw is gone.
    The door half of this same seam gets it right (`Store.save_offsets` fsyncs
    the file and then the directory), which is the shape this copies.
    """
    with _datadir() as tmp:
        cfg = config.Config()
        cfg.ensure_dirs()
        calls = []
        real_fsync, real_dir = tail.fsync, tail.fsync_dir
        tail.fsync = lambda fd: (calls.append("fsync"), real_fsync(fd))[1]
        tail.fsync_dir = lambda p: (calls.append("dir"), real_dir(p))[1]
        try:
            ledger.append(cfg.ledger, _real_rows()[:1])
            check_true("durability: the ledger's own bytes are fsynced",
                       "fsync" in calls)
            calls[:] = []
            tail.save_offsets(cfg, {"x": {"offset": 1}})
            # `fsync_dir` opens the directory and goes through `fsync` itself,
            # so a third entry follows; the order of the first two is the
            # property -- data durable, then the rename that names it.
            check("durability: the offsets file is fsynced, then its directory",
                  calls[:2], ["fsync", "dir"])
        finally:
            tail.fsync, tail.fsync_dir = real_fsync, real_dir


def test_doctor_reports_the_receiver_without_pretending_it_should_be_up():
    """"Not running" is the normal state of an on-demand receiver.

    It is spawned by `claudio run` and leaves after an idle half hour, so a
    machine nobody is using has none -- reporting that as a fault would train
    the reader to ignore every line of `doctor`, which is what a WARN that is
    usually wrong does.  What IS a fault is a `recv.err` nobody has cleared,
    and that must reach the exit status.
    """
    rc, out = _doctor_rc()
    check_true("doctor: an absent receiver is reported, not condemned",
               "receiver not running" in out and "WARN receiver" not in out)
    check_true("doctor: and it still reaches the last check",
               "stream A export:" in out)
    check_true("doctor: with no counters at all it says so rather than zero",
               "no counters" in out)

    def seed(cfg):
        collector.note_error(cfg, "cannot bind 127.0.0.1:4318: address in use")
        ledger.append(cfg.ledger, _real_rows())
        with open(cfg.samples, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": 1, "account": "a"}) + "\n")

    rc, out = _doctor_rc(seed=seed)
    check_true("doctor: a recv.err from a failed start is a WARN",
               "WARN recv.err (start)" in out and "address in use" in out)
    check("doctor: and it fails the exit status", rc, 1)

    # And the other file, labelled for what it is.  Reporting a fault a
    # *running* receiver noticed as "why it could not start" was wrong twice
    # over: it condemned a healthy process, and it was the same file the
    # supervisor reads as "do not spawn".
    def seed_warn(cfg):
        collector.note_warning(cfg, "undecodable OTLP body: Expecting value")
        ledger.append(cfg.ledger, _real_rows())
        with open(cfg.samples, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": 1, "account": "a"}) + "\n")

    rc, out = _doctor_rc(seed=seed_warn)
    check_true("doctor: a runtime fault is reported as one, not as a start "
               "failure",
               "recv.warn (while running)" in out
               and "recv.err" not in out)
    check("doctor: and it still reaches the exit status", rc, 1)

    # The shipper's pair.  `claudio run` printing them is half of the promise;
    # the command whose whole job is "is this working" is the other half, and a
    # non-delivery it reported as healthy would be the same failure `doctor`
    # was already fixed for once.
    def seed_ship(cfg):
        collector.note_ship_error(cfg, "1 shipping failure(s): alpha/ledger: HTTP 503")
        collector.note_ship_warning(cfg, "2 ledger row(s) carry no account identity")
        ledger.append(cfg.ledger, _real_rows())
        with open(cfg.samples, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": 1, "account": "a"}) + "\n")

    rc, out = _doctor_rc(seed=seed_ship)
    check_true("doctor: a non-delivery is reported as one",
               "ship.err (nothing delivered)" in out and "HTTP 503" in out)
    check_true("doctor: and an unplaceable row is reported apart from it",
               "ship.warn (delivered, unplaceable rows)" in out
               and "no account identity" in out)
    check("doctor: and either of them fails the exit status", rc, 1)


# ====================== 10. THE SHIPPER: WHAT LEAVES THIS MACHINE, TO WHOM ==
#
# The property under test in almost every case below is a REFUSAL, so each one
# is written to fail if the refusal is removed rather than to pass if it
# happens to hold.  The headline is `test_ship_filters_to_exactly_one_account`:
# the real capture normalises to eight rows carrying three different accounts
# interleaved in one ledger file, so an unfiltered ship to a work destination
# publishes two personal accounts to an employer.  That fixture is why the
# filter exists, and it is what the test is built from.

SHIP_UUID = {"agent": "f1f70009-0000-4000-8000-000000000009",
             "alpha": "f1f7000d-0000-4000-8000-00000000000d",
             "beta": "f1f70010-0000-4000-8000-000000000010"}
SHIP_EMAIL = {n: "%s@example.com" % n for n in SHIP_UUID}

REAL_SAMPLES = os.path.join(HERE, "fixtures", "real-samples.jsonl")


def _ship_uuid_map_is_real():
    """The UUIDs above are read off the capture, not invented.

    Called by the tests that use them, so an edited fixture fails here by name
    instead of silently making every filter assertion vacuous.
    """
    seen = {}
    with open(REAL_SAMPLES, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rec = json.loads(line)
                seen.setdefault(rec["account"], set()).add(rec["account_uuid"])
    return {k: sorted(v) for k, v in sorted(seen.items())}


class _Sink(object):
    """Collects what the shipper POSTs.  Substituted for `ship.post_batch`."""

    def __init__(self, ok=True, detail=None):
        self.ok, self.detail, self.calls = ok, detail, []

    def __call__(self, url, token, man, records, timeout=None):
        self.calls.append({"url": url, "token": token, "manifest": man,
                           "records": list(records)})
        return self.ok, self.detail

    def records(self, stream=None):
        out = []
        for c in self.calls:
            if stream is None or c["manifest"]["stream"] == stream:
                out.extend(c["records"])
        return out

    def parsed(self, stream=None):
        return [json.loads(r) for r in self.records(stream)]


@contextlib.contextmanager
def _ship_world(conf_lines, accounts=("agent", "alpha", "beta"),
                rows=None, samples=True):
    """A usage dir, a claudio conf and logged-in account dirs.

    Yields (cfg, conf_path, accounts_dir).  The ledger is written by
    `ledger.append`, the real writer, and `samples.jsonl` is a byte copy of the
    captured file -- so every byte-identity assertion below is made against
    bytes something real produced.
    """
    tmp = tempfile.mkdtemp(prefix="cu-ship-")
    old = os.environ.get("CLAUDIO_USAGE_DIR")
    os.environ["CLAUDIO_USAGE_DIR"] = tmp
    try:
        cfg = config.Config()
        cfg.ensure_dirs()
        acct_dir = os.path.join(tmp, "accounts")
        for name in accounts:
            d = os.path.join(acct_dir, name)
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, ".claude.json"), "w",
                      encoding="utf-8") as fh:
                json.dump({"oauthAccount": {
                    "accountUuid": SHIP_UUID[name],
                    "emailAddress": SHIP_EMAIL[name]}}, fh)
        conf = os.path.join(tmp, "claudio.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write("\n".join(conf_lines) + "\n")
        ledger.append(cfg.ledger, _real_rows() if rows is None else rows)
        if samples:
            shutil.copyfile(REAL_SAMPLES, cfg.samples)
        yield cfg, conf, acct_dir
    finally:
        if old is None:
            os.environ.pop("CLAUDIO_USAGE_DIR", None)
        else:
            os.environ["CLAUDIO_USAGE_DIR"] = old
        shutil.rmtree(tmp, ignore_errors=True)


def _run_ship(cfg, conf, accounts, sink=None, now=1786600000.0):
    sink = _Sink() if sink is None else sink
    rep = ship.run_pass(cfg, now=now, post=sink,
                        conf=ship.read_conf(conf), accounts=accounts)
    return rep, sink


def test_ship_fails_closed_on_the_destination():
    """No destination, nothing leaves -- and every near-miss is named.

    There is deliberately no default URL and no "ship everything" mode.  Four
    configurations sit one character away from a working one, and all four must
    ship nothing: no `ship_url` at all, a `ship_url` under `logging=local`, a
    `ship_url` for an account with no login to read a UUID from, and a plain
    `ship_url=` with no account prefix -- which is not a key claudio has, and
    must not become one by accident.
    """
    with _ship_world(["logging=remote"]) as (cfg, conf, acct):
        rep, sink = _run_ship(cfg, conf, acct)
        check("ship: no ship_url means no POST", len(sink.calls), 0)
        check("ship: ...and no destination", rep.destinations, 0)
        check_true("ship: ...and no offsets file is created",
                   not os.path.exists(tail.offsets_path(cfg, tail.SHIP_OFFSETS)))
        check_true("ship: ...and no machine-id on a machine that never ships",
                   not os.path.exists(cfg.machine_id))
        check_true("ship: ...and no ship.err on a machine that never asked",
                   collector.read_ship_error(cfg) is None)

    # A destination under `logging=local`.  Shipping because a URL is present
    # would make `logging=local` upload records, which is the same sin as
    # `logging=remote` shipping nothing -- pointed the other way.
    with _ship_world(["logging=local",
                      "account.alpha.ship_url=http://sink.example/v1"]) \
            as (cfg, conf, acct):
        rep, sink = _run_ship(cfg, conf, acct)
        check("ship: logging=local with a ship_url ships nothing",
              len(sink.calls), 0)
        err = collector.read_ship_error(cfg) or ""
        check_true("ship: ...and the refusal names the account and the reason",
                   "alpha" in err and "not remote" in err)

    # A UUID that cannot be read is not a licence to guess one from the label.
    with _ship_world(["logging=remote",
                      "account.alpha.ship_url=http://sink.example/v1"],
                     accounts=("agent", "beta")) as (cfg, conf, acct):
        rep, sink = _run_ship(cfg, conf, acct)
        check("ship: an account with no readable login ships nothing",
              len(sink.calls), 0)
        check_true("ship: ...and says which account and why",
                   "alpha" in (collector.read_ship_error(cfg) or ""))

    # `ship_url` with no account prefix is not a key. Fail closed, in silence
    # about it -- there is nothing to refuse, because nothing named an account.
    with _ship_world(["logging=remote", "ship_url=http://sink.example/v1"]) \
            as (cfg, conf, acct):
        rep, sink = _run_ship(cfg, conf, acct)
        check("ship: a plain ship_url= is not a destination", len(sink.calls), 0)
        check("ship: ...and is not reported as a refused account",
              len(rep.refusals), 0)

    # An account name with a dot in it. `_validate_name` allows one, and its
    # own comment says claudio never parses `account.…` back apart -- this is
    # the one reader that does, so a naive split on dots would skip the key and
    # ship nothing, with no refusal named. That is the loss, not an edge case.
    with _ship_world(["logging=remote",
                      "account.my.acct.ship_url=http://sink.example/v1"],
                     accounts=()) as (cfg, conf, acct):
        d = os.path.join(acct, "my.acct")
        os.makedirs(d)
        with open(os.path.join(d, ".claude.json"), "w", encoding="utf-8") as fh:
            json.dump({"oauthAccount": {"accountUuid": SHIP_UUID["alpha"],
                                        "emailAddress": SHIP_EMAIL["alpha"]}},
                      fh)
        rep, sink = _run_ship(cfg, conf, acct)
        check("ship: a dotted account name is read, not skipped past",
              rep.destinations, 1)
        check("ship: ...and its rows ship",
              len(sink.parsed(ship.STREAM_LEDGER)), 3)

    # And a name that could escape the accounts directory is refused BY NAME,
    # not quietly path-joined into somebody else's login.
    with _ship_world(["logging=remote",
                      "account.../../evil.ship_url=http://sink.example/v1"]) \
            as (cfg, conf, acct):
        rep, sink = _run_ship(cfg, conf, acct)
        check("ship: a traversing account name ships nothing", len(sink.calls), 0)
        check_true("ship: ...and is refused by name",
                   any("usable account name" in why for _n, why in rep.refusals))


def test_ship_filters_to_exactly_one_account():
    """THE test.  Three accounts in one ledger, one destination, one account.

    `real-api-request.jsonl` normalises to 8 rows: 3 agent, 3 alpha, 2 beta
    (measured, and re-derived here from the rows themselves rather than
    written down).  A shipper that sent the file unfiltered to alpha's
    destination would publish agent's and beta's rows with it.
    """
    check("ship: the fixture's account->uuid map is the captured one",
          _ship_uuid_map_is_real(),
          {"agent": [SHIP_UUID["agent"]], "alpha": [SHIP_UUID["alpha"]],
           "beta": [SHIP_UUID["beta"]]})

    rows = _real_rows()
    want_a = [r for r in rows if r["email"] == SHIP_EMAIL["alpha"]]
    check_true("ship: the fixture really does interleave three accounts",
               len({r["email"] for r in rows}) == 3 and len(want_a) == 3)

    with _ship_world(["logging=remote",
                      "account.alpha.ship_url=http://sink.example/v1",
                      "account.alpha.ship_token=tok-alpha"]) \
            as (cfg, conf, acct):
        rep, sink = _run_ship(cfg, conf, acct)

        got = sink.parsed(ship.STREAM_LEDGER)
        check("ship: only alpha's rows are shipped",
              sorted(r["request_id"] for r in got),
              sorted(r["request_id"] for r in want_a))
        check("ship: every shipped row is stamped with alpha's UUID",
              sorted({r["account_uuid"] for r in got}), [SHIP_UUID["alpha"]])
        check("ship: no other account's email leaves with them",
              sorted({r["email"] for r in got}), [SHIP_EMAIL["alpha"]])

        # Stream B carries the UUID itself, so it is matched on it exactly --
        # never on the label, which `--tag account=` overwrites.
        b = sink.parsed(ship.STREAM_SAMPLES)
        check("ship: only alpha's samples are shipped",
              sorted({r["account_uuid"] for r in b}), [SHIP_UUID["alpha"]])
        check("ship: ...and that is fewer than the file holds",
              len(b) < sum(1 for l in open(REAL_SAMPLES) if l.strip()), True)

        check("ship: the token rides as the tenant label it is",
              sorted({c["token"] for c in sink.calls}), ["tok-alpha"])


def test_shipped_lines_are_the_file_bytes():
    """The wire format IS the file format, so a body is diffable against disk.

    Stream B: byte for byte.  Stream A: byte for byte plus exactly one
    appended key -- strip the suffix and the file's line is back.  This is what
    rules out a `json.loads`/`json.dumps` round trip, which would reorder keys,
    reformat floats and re-escape non-ASCII into a third schema nobody can
    compare against anything.
    """
    with _ship_world(["logging=remote",
                      "account.alpha.ship_url=http://sink.example/v1"]) \
            as (cfg, conf, acct):
        rep, sink = _run_ship(cfg, conf, acct)

        with open(cfg.samples, "rb") as fh:
            on_disk_b = [l[:-1] for l in fh if l.strip()]
        shipped_b = sink.records(ship.STREAM_SAMPLES)
        check_true("ship: every stream-B line shipped is a line of the file, "
                   "byte for byte",
                   shipped_b and all(l in on_disk_b for l in shipped_b))

        with open(cfg.ledger, "rb") as fh:
            on_disk_a = [l[:-1] for l in fh if l.strip()]
        suffix = b',"account_uuid":"%s"}' % SHIP_UUID["alpha"].encode()
        shipped_a = sink.records(ship.STREAM_LEDGER)
        check_true("ship: every stream-A line ends with exactly the stamp",
                   shipped_a and all(l.endswith(suffix) for l in shipped_a))
        check_true("ship: ...and is the file's own bytes underneath it",
                   all(l[:-len(suffix)] + b"}" in on_disk_a for l in shipped_a))

    # The stamp itself, at the edges.  A value that could carry a quote or a
    # backslash into the splice is refused rather than escaped.
    check("ship: the stamp appends without re-serialising",
          ship.stamp(b'{"z":1,"a":2}', "u-1"),
          b'{"z":1,"a":2,"account_uuid":"u-1"}')
    check("ship: a non-object line cannot be stamped",
          ship.stamp(b'[1,2]', "u-1"), None)
    check("ship: a UUID that is not a plain identifier is refused",
          ship.stamp(b'{"a":1}', 'x","evil":"1'), None)


def test_the_door_accepts_what_the_shipper_emits():
    """End to end against `server/srv`, not against a description of it.

    The shipper spells the wire constants rather than importing them (so that
    `server/` stands alone with no path games), which is only safe if the drift
    is a test failure.  So: compare every constant, then push a real shipped
    batch through the real `Ingest.add_batch` and require the records to be
    ACCEPTED -- manifest, stamp, tenant and all.
    """
    srv_root = os.path.join(os.path.dirname(ROOT), "server")
    if not os.path.isdir(os.path.join(srv_root, "srv")):
        print("  (skipped -- server/ not present)")
        return
    sys.path.insert(0, srv_root)
    try:
        from srv import ingest as srv_ingest, wire as srv_wire
    finally:
        sys.path.remove(srv_root)

    check("ship/wire: SHIP_VERSION agrees", ship.SHIP_VERSION,
          srv_wire.SHIP_VERSION)
    check("ship/wire: the stream names agree",
          (ship.STREAM_LEDGER, ship.STREAM_SAMPLES),
          (srv_wire.STREAM_LEDGER, srv_wire.STREAM_SAMPLES))
    check("ship/wire: the stamped field name agrees", ship.LEDGER_STAMP,
          srv_wire.LEDGER_STAMP)

    with _ship_world(["logging=remote",
                      "account.alpha.ship_url=http://sink.example/v1"]) \
            as (cfg, conf, acct):
        now = 1786600000.0
        rep, sink = _run_ship(cfg, conf, acct, now=now)
        ing = srv_ingest.Ingest()
        placed = 0
        for call in sink.calls:
            ok, why = srv_wire.manifest_check(call["manifest"])
            check("ship: the door accepts the manifest (%s)"
                  % call["manifest"]["stream"], (ok, why), (True, None))
            batch = srv_ingest.Batch(call["manifest"],
                                     [json.loads(r) for r in call["records"]],
                                     SHIP_UUID["alpha"])
            placed += ing.add_batch(batch, now)
        check("ship: the door refused no batch", ing.counts["batches_refused"], 0)
        check("ship: the door refused no record", ing.counts["refused"], 0)
        check("ship: and placed every one of them under alpha",
              (placed, sorted(ing.accounts())),
              (len(sink.records()), [SHIP_UUID["alpha"]]))


def test_a_failed_post_leaves_the_offset_untouched():
    """A failure costs a retry, never a record -- and it is never quiet.

    The whole reason the offset is allowed to be simple: resending is safe
    because both streams are deduped server-side, so the only correct response
    to a refused POST is to change nothing at all.
    """
    conf = ["logging=remote", "account.alpha.ship_url=http://sink.example/v1"]
    with _ship_world(conf) as (cfg, c, acct):
        bad = _Sink(ok=False, detail="HTTP 503")
        rep, _ = _run_ship(cfg, c, acct, sink=bad)
        check_true("ship: a refused POST is counted as a failure",
                   len(rep.failures) >= 1)
        check("ship: nothing is recorded as delivered", rep.delivered, 0)
        offs = tail.load_offsets(cfg, tail.SHIP_OFFSETS)
        check("ship: and the offset is not advanced", offs, {})
        err = collector.read_ship_error(cfg) or ""
        check_true("ship: the failure is written where the next run prints it",
                   "503" in err and "resends" in err)

        # The next tick resends the identical byte range.
        good = _Sink()
        rep2, _ = _run_ship(cfg, c, acct, sink=good)
        check("ship: the next pass resends everything that failed",
              rep2.delivered, len(good.records()))
        check("ship: ...which is the whole of alpha's data",
              len(good.parsed(ship.STREAM_LEDGER)), 3)
        check_true("ship: a clean pass clears ship.err",
                   collector.read_ship_error(cfg) is None)
        check_true("ship: ...and only then is the offset stored",
                   tail.load_offsets(cfg, tail.SHIP_OFFSETS) != {})

        # Idempotent: a third pass has nothing to send.
        again = _Sink()
        rep3, _ = _run_ship(cfg, c, acct, sink=again)
        check("ship: a pass with nothing new POSTs nothing", len(again.calls), 0)


def test_ship_resumes_by_offset_and_rescans_a_replaced_file():
    """Resume is `cu/tail`'s, which is `claudio usage`'s -- not a second copy.

    The 57%-of-spend silent skip came from an offset that could not tell that
    the file under it had been replaced.  The shipper gets that check for free
    by using the same function, and a rescan is safe here for the same reason
    it is safe there: the far end dedupes on content.
    """
    check_true("ship: the resume helpers are the ingest ones, not a copy",
               load_cli().start_offset is tail.start_offset
               and load_cli().file_identity is tail.file_identity)

    conf = ["logging=remote", "account.alpha.ship_url=http://sink.example/v1"]
    with _ship_world(conf) as (cfg, c, acct):
        first = _Sink()
        _run_ship(cfg, c, acct, sink=first)
        check("ship: the first pass sends alpha's three rows",
              len(first.parsed(ship.STREAM_LEDGER)), 3)

        # One more of alpha's rows, appended.  Only the new one moves.
        extra = dict(_real_rows()[2])
        extra["request_id"] = "aaaaaaaaaaaaaaaa"
        ledger.append(cfg.ledger, [extra])
        second = _Sink()
        _run_ship(cfg, c, acct, sink=second)
        check("ship: the second pass sends only what was appended",
              [r["request_id"] for r in second.parsed(ship.STREAM_LEDGER)],
              ["aaaaaaaaaaaaaaaa"])

        # The file is replaced under the stored offset, by a LONGER one.  The
        # length matters: a shorter replacement is caught by the plain
        # `offset > size` guard, so a test built on one passes with the
        # identity check deleted -- which is exactly the hole `start_offset`'s
        # docstring describes ("it only fires if you happen to look while the
        # replacement is still shorter").  The padding is beta's, so a resume
        # at the stale offset lands inside it and ships NONE of alpha's rows,
        # while a rescan ships all three.
        pad = []
        for i in range(20):
            p = dict(_real_rows()[5])                    # a beta row
            p["request_id"] = "pad%013d" % i
            pad.append(p)
        os.unlink(cfg.ledger)
        ledger.append(cfg.ledger, _real_rows() + pad)
        check_true("ship: the replacement is longer than the stored offset, "
                   "so the size guard cannot be what saves this",
                   os.path.getsize(cfg.ledger)
                   > tail.load_offsets(cfg, tail.SHIP_OFFSETS)
                   ["alpha|ledger"]["offset"])
        third = _Sink()
        _run_ship(cfg, c, acct, sink=third)
        check("ship: a replaced file is rescanned, not skipped past",
              len(third.parsed(ship.STREAM_LEDGER)), 3)


def test_a_row_naming_no_account_is_named_rather_than_dropped_quietly():
    """The third answer to "whose row is this": nobody's.

    A row claudio produced with no account resolved, or with `tag=email=`
    suppressing the address, can never be placed against any destination.  It
    is not shipped -- placing it would be a guess -- and it is not silent
    either, because a record that will never leave the machine is exactly the
    thing this project refuses to lose without a word.
    """
    rows = _real_rows()
    orphan = dict(rows[0])
    orphan["request_id"] = "bbbbbbbbbbbbbbbb"
    orphan["email"] = None
    conf = ["logging=remote", "account.alpha.ship_url=http://sink.example/v1"]
    with _ship_world(conf, rows=rows + [orphan]) as (cfg, c, acct):
        rep, sink = _run_ship(cfg, c, acct)
        check("ship: an identity-less row is not shipped",
              [r["request_id"] for r in sink.parsed(ship.STREAM_LEDGER)
               if r["request_id"] == "bbbbbbbbbbbbbbbb"], [])
        check("ship: it is counted", rep.unidentified, 1)
        warn = collector.read_ship_warning(cfg) or ""
        check_true("ship: and written where the next run prints it",
                   "no account identity" in warn)
        check_true("ship: a delivering pass with orphans is not a failure",
                   collector.read_ship_error(cfg) is None)

    # The other half of the same rule, one level up: a destination whose
    # account has a UUID but no address.  Stream B still ships -- it carries
    # the UUID itself -- and stream A cannot be placed at all, so the ledger is
    # NOT read: scanning it would advance the offset past rows that become
    # placeable again the moment the login is restored.
    conf2 = ["logging=remote", "account.beta.ship_url=http://sink.example/v1"]
    with _ship_world(conf2) as (cfg, c, acct):
        with open(os.path.join(acct, "beta", ".claude.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"oauthAccount": {"accountUuid": SHIP_UUID["beta"]}}, fh)
        rep, sink = _run_ship(cfg, c, acct)
        check("ship: an account with no address ships no request rows",
              len(sink.parsed(ship.STREAM_LEDGER)), 0)
        check_true("ship: ...but its plan samples still ship",
                   len(sink.parsed(ship.STREAM_SAMPLES)) > 0)
        check("ship: ...and its ledger offset is not advanced past them",
              tail.load_offsets(cfg, tail.SHIP_OFFSETS).get("beta|ledger"), None)
        check_true("ship: ...and the half that is not shipping is named",
                   "plan samples only for beta"
                   in (collector.read_ship_error(cfg) or ""))

    # place_row's three answers, stated directly: yes, no, and "names nobody".
    d = ship.Destination("alpha", "u", "", SHIP_UUID["alpha"],
                         SHIP_EMAIL["alpha"])
    check("ship: place_row matches on email", ship.place_row(rows[2], d), True)
    check("ship: ...and never on the label",
          ship.place_row({"account": "alpha", "email": "other@example.com"}, d),
          False)
    check("ship: ...and says None when the row names nobody",
          ship.place_row({"account": "alpha"}, d), None)


def test_a_destination_that_places_nothing_is_named_and_keeps_its_backlog():
    """The third answer `place_row` gives, which was counted nowhere.

    `None` (no `email` key) incremented `unidentified`; `True` shipped; `False`
    -- the row carries an address that matches no destination -- fell through
    `if not placed: continue` with no counter anywhere, and the offset was then
    advanced and durably saved past it.  So a valid, configured destination
    could consume the entire ledger, ship nothing, and say nothing:

        destinations: 1  posts: 0  shipped: {}  unidentified: 0
        refusals: []  failures: []  ship.err: absent  ship.warn: absent

    Two of the three ways in are documented user actions.  A `tag=email=`
    line -- which the readme offers as the way to suppress the PII tag -- leaves
    `email` a non-empty string, so `place_row` answers False rather than None.
    And an account re-logged under a new address leaves older rows behind.
    The backlog was then unrecoverable even after the address was fixed,
    because `start_offset` resumed past it: dev, ino and head all still matched.
    """
    rows = _real_rows()
    # alpha's own three rows, with the address every doc says you may suppress.
    mine = [dict(r, email="redacted") for r in rows
            if r["email"] == SHIP_EMAIL["alpha"]]
    conf = ["logging=remote", "account.alpha.ship_url=http://sink.example/v1"]
    with _ship_world(conf, rows=mine) as (cfg, c, acct):
        rep, sink = _run_ship(cfg, c, acct)
        check("ship: a destination whose rows name another address ships none",
              len(sink.parsed(ship.STREAM_LEDGER)), 0)
        check("ship: the rows nobody could place are counted", rep.unplaced, 3)
        check("ship: ...with the address they carried named",
              sorted(rep.unplaced_addresses), ["redacted"])
        err = collector.read_ship_error(cfg) or ""
        check_true("ship: and the destination is named where run prints it",
                   "alpha" in err and "placed 0 request row(s)" in err)
        check_true("ship: ...with its own address, so the mismatch is visible",
                   SHIP_EMAIL["alpha"] in err and "redacted" in err)

        # THE BACKLOG SURVIVES.  Nothing durable was written for that stream,
        # so fixing the address ships everything rather than nothing.
        offs = tail.load_offsets(cfg, tail.SHIP_OFFSETS)
        check("ship: the ledger offset is not advanced past unplaced rows",
              offs.get("alpha|ledger"), None)
        check_true("ship: ...while stream B, which carries its own UUID, is",
                   offs.get("alpha|samples") is not None)

        # The user fixes the address (a re-login, or deleting the tag= line).
        ledger.append(cfg.ledger, [], None)
        with open(cfg.ledger, "w", encoding="utf-8") as fh:
            for r in mine:
                fh.write(json.dumps(dict(r, email=SHIP_EMAIL["alpha"]),
                                    separators=(",", ":")) + "\n")
        again = _Sink()
        rep2, _ = _run_ship(cfg, c, acct, sink=again)
        check("ship: the whole backlog ships once the two agree",
              len(again.parsed(ship.STREAM_LEDGER)), 3)
        check_true("ship: ...and only then is the offset stored",
                   tail.load_offsets(cfg, tail.SHIP_OFFSETS)
                   .get("alpha|ledger") is not None)
        check_true("ship: ...and ship.err is cleared",
                   collector.read_ship_error(cfg) is None)

    # The negative, which is what keeps the alarm readable: the ordinary
    # multi-account ledger.  Rows belonging to accounts with no destination are
    # unplaced BY DESIGN and must not raise anything, because alpha placed
    # rows of its own.
    with _ship_world(conf) as (cfg, c, acct):
        rep, sink = _run_ship(cfg, c, acct)
        check("ship: a working destination still places its own rows",
              len(sink.parsed(ship.STREAM_LEDGER)), 3)
        check("ship: the other accounts' rows are counted, not alarmed about",
              rep.unplaced, 5)
        check_true("ship: ...and nothing is written to ship.err",
                   collector.read_ship_error(cfg) is None)


def test_ship_warn_outlives_the_pass_that_found_it():
    """"These rows will never be shipped" used to live about fifteen seconds.

    `run_pass` wrote `ship.warn` from THIS pass's counters and cleared it
    otherwise -- and those counters describe only the bytes this pass scanned,
    while the offset advances past the offending lines.  So pass 1 wrote the
    warning and pass 2, with nothing new in the file, unlinked it.  The
    receiver ticks on `check_seconds`, so the window in which the only record
    of a permanent loss existed was one tick; both readers (`claudio run` at
    session start, `claudio usage doctor` on demand) are outside it in practice.

    `ship.err` never had the problem, because a failed POST leaves the offset
    alone and re-reports every pass.  The warn file is the one whose condition
    is by construction NON-REPEATING, which is why it is the one that had to
    persist.
    """
    rows = _real_rows()
    orphan = dict(rows[0], request_id="cccccccccccccccc", email=None)
    conf = ["logging=remote", "account.alpha.ship_url=http://sink.example/v1"]
    with _ship_world(conf, rows=rows + [orphan]) as (cfg, c, acct):
        rep, _ = _run_ship(cfg, c, acct)
        check("ship: pass 1 sees the identity-less row", rep.unidentified, 1)
        first = collector.read_ship_warning(cfg) or ""
        check_true("ship: and writes it down", "no account identity" in first)

        rep2, sink2 = _run_ship(cfg, c, acct)
        check("ship: pass 2 has nothing new to scan", len(sink2.calls), 0)
        check("ship: ...so it counts nothing", rep2.unidentified, 0)
        second = collector.read_ship_warning(cfg) or ""
        check_true("ship: the warning survives the pass that found nothing",
                   "no account identity" in second)
        check_true("ship: ...and says it is a running total",
                   "RUNNING TOTALS" in second)
        check_true("ship: ...naming what to delete to acknowledge it",
                   ship.UNPLACEABLE in second)

        # Acknowledged: the totals go, and only then does the warning.
        os.unlink(os.path.join(cfg.state_dir, ship.UNPLACEABLE))
        _run_ship(cfg, c, acct)
        check("ship: deleting the totals clears the warning",
              collector.read_ship_warning(cfg), None)


def test_an_unreadable_ledger_is_a_failure_and_not_a_success():
    """A file that exists and cannot be read is not "no such file yet".

    `_ship_stream` opened with `ident = tail.file_identity(path); if ident is
    None: return True`, and `file_identity` returned None on ANY OSError.  So
    EACCES, EIO and a stale mount all answered *success* -- which contributes
    to no failure list, so the next `run_pass` reached `clear_ship_error` and
    deleted whatever the previous one had reported.  Reproduced: pass 1 with a
    refusing POST wrote `ship.err`, `chmod 000 ledger.jsonl`, pass 2 -> 0
    posts, `failures: []`, and ship.err no longer existed.  The machine shipped
    nothing and every diagnostic surface was clean.
    """
    if os.geteuid() == 0:
        print("  (skipped -- running as root, which can read a 000 file)")
        return
    conf = ["logging=remote", "account.alpha.ship_url=http://sink.example/v1"]
    with _ship_world(conf) as (cfg, c, acct):
        bad = _Sink(ok=False, detail="connection refused")
        _run_ship(cfg, c, acct, sink=bad)
        check_true("ship: a refused POST is reported",
                   "connection refused" in (collector.read_ship_error(cfg) or ""))

        os.chmod(cfg.ledger, 0o000)
        try:
            rep, sink = _run_ship(cfg, c, acct)
            check("ship: an unreadable ledger POSTs nothing",
                  len(sink.parsed(ship.STREAM_LEDGER)), 0)
            check_true("ship: it is a failure, by name",
                       any("cannot read" in d
                           for _n, _s, d in rep.failures))
            err = collector.read_ship_error(cfg) or ""
            check_true("ship: written where the next run prints it",
                       "cannot read" in err)
        finally:
            os.chmod(cfg.ledger, 0o600)

    # And the answer that must stay unchanged: a file that is genuinely absent
    # is silence, because a machine that has never recorded anything is the
    # normal case and must cost nothing at all.
    with _ship_world(conf) as (cfg, c, acct):
        os.unlink(cfg.ledger)
        rep, _ = _run_ship(cfg, c, acct)
        check("ship: an absent ledger is not a failure", rep.failures, [])
        check_true("ship: ...and writes no ship.err",
                   collector.read_ship_error(cfg) is None)


def test_the_door_s_ack_is_read_rather_than_thrown_away():
    """Every early warning the door was built to send used to reach nobody.

    `post_batch` never read the response body on either path, so a 507
    `disk-nearly-full` with `detail: "N bytes free, reserve is M"` arrived as
    `HTTP 507`, and a 409 `identity` -- the shipper aimed a file at the wrong
    destination, which jams that stream for ever because the offset never
    advances -- arrived as `HTTP 409`, with the line number and the disagreeing
    UUID the door computed thrown away.  The `offset` in every 200 is the one
    cheap cross-check the client can make against the door, and it went the
    same way.
    """
    class _Refusing(object):
        def __init__(self, code, body):
            self.code, self.body, self.calls = code, body, []

        def __call__(self, url, token, man, records, timeout=None):
            self.calls.append(man)
            raw = json.dumps(self.body).encode("utf-8")
            return False, ship._ack_detail(self.code, ship.read_ack(raw)), None

    conf = ["logging=remote", "account.alpha.ship_url=http://sink.example/v1"]
    with _ship_world(conf) as (cfg, c, acct):
        post = _Refusing(507, {"reason": "disk-nearly-full",
                               "detail": "12 bytes free, reserve is 20",
                               "disk_free_bytes": 12})
        _run_ship(cfg, c, acct, sink=post)
        err = collector.read_ship_error(cfg) or ""
        check_true("ship: the door's reason reaches the user",
                   "disk-nearly-full" in err)
        check_true("ship: ...and its detail with it",
                   "12 bytes free, reserve is 20" in err)

    with _ship_world(conf) as (cfg, c, acct):
        post = _Refusing(409, {"reason": "identity",
                               "detail": "line 3 names another account"})
        _run_ship(cfg, c, acct, sink=post)
        err = collector.read_ship_error(cfg) or ""
        check_true("ship: a 409 names the refusal rather than the number",
                   "identity" in err and "line 3 names another account" in err)

    # `read_ack` is a reader of somebody else's output: a proxy's HTML page, an
    # empty body and a truncated one each cost the detail and nothing else.
    check("ship: an unparseable ack is None, never an exception",
          ship.read_ack(b"<html>502 Bad Gateway</html>"), None)
    check("ship: an empty body is None", ship.read_ack(b""), None)
    check("ship: a JSON array is not an ack either", ship.read_ack(b"[1,2]"),
          None)
    check("ship: a bare status still reads as one", ship._ack_detail(503, None),
          "HTTP 503")

    # The offset cross-check.  The door answers with the offset it durably
    # holds; a disagreement is the shape of the discard the door used to
    # answer 200 to, and it is now named rather than dropped.
    class _Lying(object):
        def __init__(self):
            self.calls = []

        def __call__(self, url, token, man, records, timeout=None):
            self.calls.append(man)
            return True, None, {"offset": 999999, "duplicate": True}

    with _ship_world(conf) as (cfg, c, acct):
        rep, _ = _run_ship(cfg, c, acct, sink=_Lying())
        check_true("ship: an ack disagreeing about the offset is reported",
                   any("999999" in d for _n, _s, d in rep.disagreements))
        check_true("ship: ...where the next run prints it",
                   "the door disagrees" in (collector.read_ship_error(cfg) or ""))


def test_a_restarted_file_says_so_on_the_manifest():
    """The signal the door cannot compute for itself.

    An in-place truncation that keeps the first 256 bytes leaves dev, ino and
    the head hash unchanged, so the door recognises the resent range as one it
    already holds and drops the batch with a 200 -- while the client advances
    its own offset on that 200.  `_ship_stream` is the only place that knows a
    restart happened (`start > size`), so it is the only place that can say so.
    """
    conf = ["logging=remote", "account.alpha.ship_url=http://sink.example/v1"]
    rows = _real_rows()
    # alpha's rows first, so the surviving head record is one that places.
    ordered = ([r for r in rows if r["email"] == SHIP_EMAIL["alpha"]]
               + [r for r in rows if r["email"] != SHIP_EMAIL["alpha"]])
    with _ship_world(conf, rows=ordered) as (cfg, c, acct):
        first = _Sink()
        _run_ship(cfg, c, acct, sink=first)
        check("ship: an ordinary batch claims no restart",
              sorted({m["manifest"]["reset"] for m in first.calls}), [False])

        # Rewritten in place, keeping the head: dev, ino and the hash of the
        # first 256 bytes are all unchanged, and the file is now shorter than
        # the stored offset.  This is the state the door cannot detect.
        with open(cfg.ledger, "rb") as fh:
            raw = fh.read()
        keep = raw.index(b"\n") + 1
        before = tail.file_identity(cfg.ledger)
        with open(cfg.ledger, "r+b") as fh:
            fh.truncate(keep)
        check("ship: the truncated file's identity is unchanged",
              tail.file_identity(cfg.ledger), before)

        after = _Sink()
        _run_ship(cfg, c, acct, sink=after)
        led = [m for m in after.calls
               if m["manifest"]["stream"] == ship.STREAM_LEDGER]
        check_true("ship: the shorter file is re-read from zero",
                   led and led[0]["manifest"]["from"] == 0)
        check("ship: ...and the manifest says the restart happened",
              [m["manifest"]["reset"] for m in led], [True])


def test_a_partial_final_line_is_never_shipped():
    """A write in progress is not a record, and the offset stops in front of it.

    Same rule `otlp.read_file` applies to the raw capture: a torn tail costs a
    tick, never a record.  Shipping the fragment would put a half object on the
    wire; consuming it would lose the whole row when the writer finished.
    """
    conf = ["logging=remote", "account.alpha.ship_url=http://sink.example/v1"]
    with _ship_world(conf) as (cfg, c, acct):
        _run_ship(cfg, c, acct)
        row = dict(_real_rows()[2])
        row["request_id"] = "cccccccccccccccc"
        line = json.dumps(row, separators=(",", ":"))
        with open(cfg.ledger, "a", encoding="utf-8") as fh:
            fh.write(line[:40])                    # a write caught mid-flight
        torn = _Sink()
        rep, _ = _run_ship(cfg, c, acct, sink=torn)
        check("ship: a partial final line is not shipped",
              len(torn.parsed(ship.STREAM_LEDGER)), 0)
        check("ship: ...and is not counted as malformed either -- it is a "
              "write in progress, not a bad record", rep.malformed, 0)
        check("ship: ...and the offset stops in front of it",
              tail.load_offsets(cfg, tail.SHIP_OFFSETS)["alpha|ledger"]["offset"],
              os.path.getsize(cfg.ledger) - 40)
        with open(cfg.ledger, "a", encoding="utf-8") as fh:
            fh.write(line[40:] + "\n")             # the writer finishes
        whole = _Sink()
        _run_ship(cfg, c, acct, sink=whole)
        check("ship: the completed line ships whole on the next pass",
              [r["request_id"] for r in whole.parsed(ship.STREAM_LEDGER)],
              ["cccccccccccccccc"])


def test_the_shipper_never_reads_raw():
    """`raw/` is larger and can hold prompt and response bodies.

    `OTEL_LOG_USER_PROMPTS=1` is the user's own opt-in and the receiver stores
    what arrives without redacting it, so the one guarantee worth having is
    structural: the shipper does not know the path.  Asserted twice -- the
    source never names it, and a payload sitting in `raw/` never appears on the
    wire.
    """
    src = open(os.path.join(ROOT, "cu", "ship.py"), encoding="utf-8").read()
    body = re.sub(r'"""(?:.|\n)*?"""', "", src)     # docstrings talk about it
    body = re.sub(r"#.*", "", body)
    for name in ("raw_file", "raw_dir", "raw_files"):
        check("ship: the code never names cfg.%s" % name, name in body, False)

    conf = ["logging=remote", "account.alpha.ship_url=http://sink.example/v1"]
    with _ship_world(conf) as (cfg, c, acct):
        secret = "PROMPT-BODY-THAT-MUST-NEVER-SHIP"
        with open(cfg.raw_file, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"resourceLogs": [{"body": secret}]}) + "\n")
        rep, sink = _run_ship(cfg, c, acct)
        check_true("ship: something was shipped, so this is not vacuous",
                   rep.delivered > 0)
        check("ship: and nothing out of raw/ was in it",
              any(secret.encode() in r for r in sink.records()), False)


def test_the_shipper_names_itself_in_a_header_a_proxy_can_read():
    """claudio identified itself in the one place a WAF cannot read.

    `AGENT` existed and was written into the MANIFEST -- the body -- so every
    POST this project has ever sent went out as `User-Agent: Python-urllib/3.x`.
    A proxy sees headers; the body is past the point where a rule can act, and
    on the ordinary deployment it is the WAF that has to decide whether to let
    the request through at all.  So the string was there, correct, and useless.

    It went unseen because of where the suite injects: `_run_ship` passes
    `post=`, which replaces `post_batch` wholesale, so the entire header block
    is below every test that drives a shipping pass.  This one goes through real
    urllib to a real socket for that reason -- a test asserting `AGENT` is
    non-empty would have passed throughout.
    """
    sink = _ShipHTTPSink()
    sink.start()
    try:
        man = ship.manifest(ship.STREAM_LEDGER, "/x/ledger.jsonl",
                            (1, 2, "h"), 0, 1, 1, "host", "machine",
                            1786600000.0)
        ok, detail, _ack = ship.post_batch(sink.url, "tok", man, [b'{"a":1}'])
        check_true("ship/ua: the POST reached the sink", ok and not detail)
        check("ship/ua: one batch arrived", len(sink.batches), 1)
        got = sink.batches[0]["agent"]

        check("ship/ua: the request names claudio", got, ship.AGENT)
        check("ship/ua: ...and carries claudio's version", got,
              "claudio-ship/" + config.CLAUDIO_VERSION)

        # The defect stated as its own assertion, because "equals AGENT" would
        # still hold if AGENT were ever set to urllib's default by accident.
        check_true("ship/ua: ...and is not urllib's default",
                   "Python-urllib" not in (got or ""))

        # ONE identity, not two.  The manifest field and the header come from
        # the same constant; two copies would drift into two answers to the
        # question "what is talking to me", which is the question the header is
        # being added to answer.
        check("ship/ua: the manifest agrees with the header",
              sink.batches[0]["manifest"].get("agent"), got)
    finally:
        sink.stop()


def test_the_shipper_s_version_is_the_one_claudio_ships():
    """Derived from the `claudio` script, never asserted as a literal.

    `CLAUDIO_VERSION` is a copy -- the number belongs to a POSIX sh script that
    cannot be imported -- so the guard is the one this project uses for every
    other copy: read both, compare, fail at the moment it can still be fixed.
    A hardcoded "0.1.0" here would be a fourth place to forget on release day,
    and the symptom would be a WAF rule silently matching the wrong release.
    """
    path = os.path.join(os.path.dirname(ROOT), "claudio")
    if not os.path.exists(path):
        check_true("ship/ua: the claudio script is next to usage/", False)
        return
    src = open(path, encoding="utf-8").read()
    found = re.findall(r"^VERSION=(\S+)\s*$", src, re.M)
    check("ship/ua: claudio embeds exactly one VERSION", len(found), 1)
    if found:
        check("ship/ua: and the shipper reports that version",
              config.CLAUDIO_VERSION, found[0])


def test_the_tick_lives_in_the_receiver_and_runs_once_more_before_it_exits():
    """The two calls the design turns on, both against the real process.

    There is no cron, no launchd and no systemd unit, so the shipper's only
    schedule is the receiver's watchdog -- which is alive exactly when there is
    data.  The final pass matters as much as the tick: without it, everything
    written between the last tick and the idle exit waits for the next session
    to start a receiver, which on a machine that ships once a day is a day.
    """
    sink = _ShipHTTPSink()
    sink.start()
    tmp = tempfile.mkdtemp(prefix="cu-shiptick-")
    try:
        acct_dir = os.path.join(tmp, "accounts", "alpha")
        os.makedirs(acct_dir)
        with open(os.path.join(acct_dir, ".claude.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"oauthAccount": {"accountUuid": SHIP_UUID["alpha"],
                                        "emailAddress": SHIP_EMAIL["alpha"]}},
                      fh)
        conf = os.path.join(tmp, "claudio.conf")
        with open(conf, "w", encoding="utf-8") as fh:
            fh.write("logging=remote\naccount.alpha.ship_url=%s\n" % sink.url)
        env = _recv_env(tmp, CLAUDIO_CONF=conf,
                        CLAUDIO_ACCOUNTS=os.path.join(tmp, "accounts"))
        old = os.environ.get("CLAUDIO_USAGE_DIR")
        os.environ["CLAUDIO_USAGE_DIR"] = tmp
        try:
            cfg = config.Config()
            cfg.ensure_dirs()
            ledger.append(cfg.ledger, _real_rows())
        finally:
            if old is None:
                os.environ.pop("CLAUDIO_USAGE_DIR", None)
            else:
                os.environ["CLAUDIO_USAGE_DIR"] = old

        proc, got = _spawn_recv(tmp, args=("--check-seconds", "0.2"), env=env)
        try:
            check_true("ship: the receiver came up", bool(got))
            deadline = time.time() + 8
            while time.time() < deadline and len(sink.rows()) < 3:
                time.sleep(0.05)
            check("ship: the watchdog tick shipped the ledger without a cron",
                  len(sink.rows()), 3)
            before = len(sink.batches)

            # Written AFTER the last tick this test will wait for, and the
            # receiver is stopped immediately: only the final pass can deliver
            # it.
            extra = dict(_real_rows()[2])
            extra["request_id"] = "dddddddddddddddd"
            with open(os.path.join(tmp, "ledger.jsonl"), "a",
                      encoding="utf-8") as fh:
                fh.write(json.dumps(extra, separators=(",", ":")) + "\n")
            proc.terminate()
            proc.wait(timeout=10)
            check_true("ship: the final pass before exit delivered the tail",
                       any(r.get("request_id") == "dddddddddddddddd"
                           for r in sink.rows()))
            check_true("ship: ...in a batch of its own, after the tick's",
                       len(sink.batches) > before)
            check("ship: every delivered row carries the stamp",
                  sorted({r.get("account_uuid") for r in sink.rows()}),
                  [SHIP_UUID["alpha"]])
            check("ship: no ship_token means no Authorization header",
                  sorted({b["auth"] for b in sink.batches}), [None])
            check("ship: the body is NDJSON, the shape the files are in",
                  sorted({b["ctype"] for b in sink.batches}),
                  ["application/x-ndjson"])
        finally:
            _kill_recv(proc)
    finally:
        sink.stop()
        shutil.rmtree(tmp, ignore_errors=True)


class _ShipHTTPSink(object):
    """A real HTTP server, because the tick under test is a real POST."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, HTTPServer
        self.batches = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                lines = [l for l in body.split(b"\n") if l.strip()]
                outer.batches.append({
                    "auth": self.headers.get("Authorization"),
                    "ctype": self.headers.get("Content-Type"),
                    "agent": self.headers.get("User-Agent"),
                    "manifest": json.loads(lines[0]),
                    "records": [json.loads(l) for l in lines[1:]]})
                self.send_response(200)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d/v1/ship" % self.srv.server_address[1]

    def start(self):
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def rows(self):
        out = []
        for b in self.batches:
            if b["manifest"]["stream"] == ship.STREAM_LEDGER:
                out.extend(b["records"])
        return out


def test_a_shipper_fault_never_takes_the_receiver_down():
    """Containment, and the same shape as the recorder's.

    A traceback escaping into the watchdog thread would end the tick loop, and
    with it the idle exit and the session sweep: a receiver that runs for ever
    for a reason nobody can see.  So `tick` swallows and writes down what it
    swallowed -- swallowing alone would be the cardinal sin.
    """
    with _ship_world(["logging=remote"]) as (cfg, conf, acct):
        real = ship.run_pass
        ship.run_pass = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("boom in the shipper"))
        try:
            got = ship.tick(cfg)
        finally:
            ship.run_pass = real
        check("ship: a fault in a pass returns rather than raising", got, None)
        err = collector.read_ship_error(cfg) or ""
        check_true("ship: and it is written down, not swallowed",
                   "boom in the shipper" in err)


# =============================================================== runner =====

# ======================================= the offline property ================

def _usage_sources():
    """Every source file under usage/, including the extensionless entry points.

    `claudio usage` and `recv/otlp-recv` have no `.py` suffix, so a walk that
    filtered on one would skip the two files a user actually runs -- which are
    exactly the two where a stray import would cost the offline property.
    """
    out = []
    for base, dirs, names in os.walk(ROOT):
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".git", "fixtures")]
        for n in names:
            p = os.path.join(base, n)
            if n.endswith(".py"):
                out.append(p)
                continue
            try:
                with open(p, "rb") as fh:
                    head = fh.read(64)
            except OSError:
                continue
            if head.startswith(b"#!") and b"python" in head.split(b"\n")[0]:
                out.append(p)
    return sorted(out)


# The Python floor the readme promises, asserted rather than assumed.
#
# `readme.md` says "python3 (3.9 or newer; standard library only)", and a
# distribution packager reads that as a `python_requires`. Nothing enforced it:
# one `match` statement or one `x := y` in a comprehension moves the real floor
# and the promise then fails on somebody else's machine, at install time, on a
# tool whose whole install story is "it is already there".
#
# HONEST LIMIT, stated rather than implied: `feature_version` checks SYNTAX
# only. A call to a method that did not exist until 3.11 parses perfectly here
# and raises at runtime on 3.9. Listing such methods was considered and
# rejected for the reason the `OVERHEAD_SOURCES` comment gives one file over --
# a guessed list reads as coverage while matching nothing. Running the suite
# under a real 3.9 is the only thing that would close it, and no such
# interpreter is on this machine, so this guard is what there is.
PYTHON_FLOOR = (3, 9)


def test_every_module_parses_on_the_oldest_python_the_readme_promises():
    import ast
    sources = _usage_sources()
    check_true("floor: the walk finds the extensionless entry points",
               any(p.endswith("/claudio-usage") for p in sources)
               and any(p.endswith("/otlp-recv") for p in sources))
    check_true("floor: and it finds the cu modules too",
               sum(1 for p in sources if "/cu/" in p) >= 8)
    bad = []
    for p in sources:
        text = open(p, encoding="utf-8", errors="replace").read()
        try:
            ast.parse(text, filename=p, feature_version=PYTHON_FLOOR)
        except SyntaxError as exc:
            bad.append("%s: %s" % (os.path.relpath(p, ROOT), exc))
    check("floor: every module under usage/ parses on Python %d.%d"
          % PYTHON_FLOOR, bad, [])

    # ...and every document that states a floor names that same version, so
    # none of them can drift.  The number is derived from the constant above
    # rather than written out twice.
    #
    # THREE files, not one.  This checked readme.md alone, while its own
    # rationale is that "a distribution packager reads that as a
    # `python_requires`" -- and the file a packager reads is packaging/
    # README.md, which stated a floor and was checked by nothing.  usage/
    # README.md stated it too.
    #
    # And a missing file is a NAMED FAILURE, not an unconditional pass.  The
    # branch here used to be `check_true("... (skipped)", True)`, so moving or
    # renaming readme.md would have retired the guard in silence -- the same
    # shape the duckdb walk two tests down was hardened against, and the same
    # shape that let the Homebrew formula's eight assertions vanish one
    # directory over.
    repo = os.path.dirname(ROOT)
    for rel in ("readme.md", "usage/README.md", "packaging/README.md"):
        path = os.path.join(repo, rel)
        if not os.path.exists(path):
            check("floor: %s is where this guard looks" % rel, path, "a file")
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        check_true("floor: %s names the version this test enforces" % rel,
                   "%d.%d or newer" % PYTHON_FLOOR in text)


def test_nothing_under_usage_imports_duckdb():
    """The offline property, asserted statically and then behaviourally.

    DuckDB is the project's first non-stdlib dependency and it is permitted in
    ONE place: the server.  Everything here reads the same JSONL with the
    standard library, with no server, no daemon and no network -- which is most
    of why this tool is pleasant, and the reason the storage pass defended it
    explicitly rather than letting the engine spread to the reader "for
    consistency".

    The static half is a grep, and a grep alone is not enough: it passes a
    module that reaches the driver through `importlib.import_module`.  So the
    second half BLOCKS the name at the import system and imports every module
    again -- if anything reached for duckdb by any spelling, the import fails
    and this test says which module.
    """
    sources = _usage_sources()
    check_true("offline: the walk finds the extensionless entry points",
               any(p.endswith("/claudio-usage") for p in sources)
               and any(p.endswith("/otlp-recv") for p in sources))

    named = []
    for p in sources:
        text = open(p, encoding="utf-8", errors="replace").read()
        for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][\w.]*)",
                             text, re.M):
            if m.group(1).split(".")[0] == "duckdb":
                named.append(os.path.relpath(p, ROOT))
        if re.search(r"""import_module\(\s*["']duckdb""", text):
            named.append(os.path.relpath(p, ROOT) + " (import_module)")
    check("offline: nothing under usage/ imports duckdb",
          sorted(set(named)), [])

    # And the behavioural half, in a child so the block cannot leak into the
    # rest of this suite.
    prog = (
        "import sys, importlib\n"
        "class Blocker:\n"
        "    def find_module(self, name, path=None):\n"
        "        return self.find_spec(name, path)\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'duckdb' or name.startswith('duckdb.'):\n"
        "            raise AssertionError('usage/ reached for duckdb: ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        "sys.path.insert(0, %r)\n"
        "from cu import collector, config, ledger, otlp, samples, ship, tail\n"
        "import importlib.util\n"
        "from importlib.machinery import SourceFileLoader\n"
        "for name, path in (('claude_usage', %r), ('otlp_recv', %r)):\n"
        "    ldr = SourceFileLoader(name, path)\n"
        "    spec = importlib.util.spec_from_loader(name, ldr)\n"
        "    mod = importlib.util.module_from_spec(spec)\n"
        "    ldr.exec_module(mod)\n"
        "print('ok')\n"
        % (ROOT, os.path.join(ROOT, "claudio-usage"), RECV_PATH))
    r = subprocess.run([sys.executable, "-c", prog], capture_output=True,
                       text=True)
    check("offline: usage/ imports with duckdb blocked at the import system",
          (r.returncode, r.stdout.strip()), (0, "ok"))
    if r.returncode:
        FAILURES.append("     stderr: " + r.stderr.strip()[-400:])


def test_the_container_stops_at_the_server():
    """The dependency boundary as a fact about the filesystem.

    DuckDB got a container so that `server/`'s suite stops self-skipping its
    storage half.  The risk that came with it is the one this project keeps
    writing up: a runtime that starts out as "the server's" and becomes "the
    project's", after which "run the tests" means "install Docker" and the
    offline property -- `claudio usage show` on a plane -- is gone without anyone
    deciding to lose it.

    The two tests above assert that nothing HERE imports the driver.  This one
    asserts the other half: that no container definition sits anywhere a
    developer of `claudio` or `claudio usage` would have to care about, and that
    `claudio` itself, which is POSIX sh and depends on nothing, never learns the
    name.  It is a directory listing rather than a grep for a reason -- the
    thing to prevent is a FILE appearing, and a file appears before any line of
    it is read.
    """
    repo = os.path.dirname(ROOT)
    names = ("Dockerfile", "dockerfile", "compose.yml", "compose.yaml",
             "docker-compose.yml", "docker-compose.yaml", ".dockerignore",
             "Containerfile", "requirements.txt", "pyproject.toml",
             "setup.py", "Pipfile", "poetry.lock")

    found = []
    for base, dirs, files in os.walk(ROOT):          # usage/ only
        dirs[:] = [d for d in dirs
                   if d not in ("__pycache__", ".git", "env", ".venv")]
        for f in files:
            if f in names:
                found.append(os.path.relpath(os.path.join(base, f), repo))
    check("boundary: usage/ carries no container or dependency manifest",
          sorted(found), [])

    # The repository root is the other place it would land, and a root-level
    # compose file is worse than one under usage/: it reads as the way to run
    # the project.
    at_root = sorted(f for f in os.listdir(repo) if f in names)
    check("boundary: neither does the repository root", at_root, [])

    # And the one place it IS allowed, asserted positively -- so this test fails
    # if the container is deleted as well as if it spreads.  A boundary test
    # that only forbids passes trivially once the thing it bounds is gone, and
    # then says nothing when it comes back somewhere else.
    server = os.path.join(repo, "server")
    if os.path.isdir(server):
        check_true("boundary: the container lives in server/, and only there",
                   os.path.exists(os.path.join(server, "Dockerfile"))
                   and os.path.exists(os.path.join(server, "compose.yml")))

    # `claudio` is a single POSIX sh file with no dependency whatsoever.  It
    # uses Docker for MCP mode, which is why the assertion is about the ENGINE
    # rather than about containers: the failure to prevent is claudio growing an
    # opinion about the server's storage layer.
    claudio = os.path.join(repo, "claudio")
    if os.path.exists(claudio):
        text = open(claudio, encoding="utf-8", errors="replace").read()
        check("boundary: `claudio` never names duckdb",
              [ln for ln in text.splitlines() if "duckdb" in ln.lower()], [])

    # ...and neither does anything that INSTALLS claudio.  A package manager is
    # exactly where an optional dependency of one subdirectory turns into a
    # hard dependency of the whole tool: `depends_on "duckdb"` in a formula, or
    # a `pip install duckdb` in an install target, would put the engine on
    # every machine that ever ran `brew install claudio` -- and the claim this
    # project makes is that claudio gains nothing at all from it.  The Makefile
    # is checked for the same reason.
    #
    # COMMENTS ARE STRIPPED FIRST, and that is the whole difference between
    # this guard and a broken one.  Both files have to be able to say WHY they
    # do not install the engine -- that sentence is the thing a future
    # packager reads before adding it "for completeness" -- and a raw `in`
    # over every line makes writing the explanation fail the test that wants
    # it.  It is the counters test's bug pointed the other way: that one
    # matched the comment explaining a setting and passed while the setting
    # was gone.  `#` opens a comment in both make and Ruby, so one rule does
    # both.
    #
    # And the file MUST BE THERE.  This loop used to `continue` over a missing
    # path, so moving the formula would have retired its guard in silence
    # rather than failing -- the same shape as a mutation harness whose input
    # is absent reading `caught` for every row.
    for rel in ("Makefile", os.path.join("Formula", "claudio.rb")):
        path = os.path.join(repo, rel)
        check_true("boundary: %s is where the install is defined" % rel,
                   os.path.exists(path))
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8", errors="replace").read()
        code = [ln.split("#", 1)[0] for ln in text.splitlines()]
        check("boundary: %s never names duckdb" % rel,
              [ln for ln in code if "duckdb" in ln.lower()], [])


def test_claude_usage_reads_the_ledger_with_no_duckdb_and_no_server():
    """`show` answers from the files alone, with the driver blocked.

    The static guard above says nothing about whether the reader still WORKS
    without it.  This runs the real CLI over a real ledger with `duckdb`
    blocked at the import system and with no door on the network, and requires
    a real number out of it -- the offline property as a behaviour rather than
    as an absence.
    """
    d = tempfile.mkdtemp()
    prev = os.environ.get("CLAUDIO_USAGE_DIR")
    try:
        os.environ["CLAUDIO_USAGE_DIR"] = d
        cfg = config.Config()
        cfg.ensure_dirs()
        recs = []
        for line in open(os.path.join(HERE, "fixtures",
                                      "real-api-request.jsonl"),
                         encoding="utf-8"):
            if line.strip():
                recs.extend(otlp.rows_from_payload(json.loads(line)))
        ledger.append(cfg.ledger, recs)
        n_rows = sum(1 for _ in ledger.read(cfg.ledger))
        check_true("offline: the fixture ledger has rows to read", n_rows > 0)

        prog = (
            "import sys\n"
            "class Blocker:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'duckdb' or name.startswith('duckdb.'):\n"
            "            raise AssertionError('reached for duckdb')\n"
            "        return None\n"
            "sys.meta_path.insert(0, Blocker())\n"
            "sys.argv = ['claudio-usage', 'show']\n"
            "import importlib.util\n"
            "from importlib.machinery import SourceFileLoader\n"
            "ldr = SourceFileLoader('claude_usage', %r)\n"
            "spec = importlib.util.spec_from_loader('claude_usage', ldr)\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "ldr.exec_module(mod)\n"
            "raise SystemExit(mod.main())\n"
            % (os.path.join(ROOT, "claudio-usage"),))
        env = dict(os.environ, CLAUDIO_USAGE_DIR=d)
        r = subprocess.run([sys.executable, "-c", prog], capture_output=True,
                           text=True, env=env)
        check("offline: `claudio usage show` exits 0 with duckdb blocked",
              r.returncode, 0)
        check_true("offline: ...and prints the rows it read, from the files "
                   "alone", "request" in r.stdout.lower() or bool(r.stdout))
        if r.returncode:
            FAILURES.append("     stderr: " + r.stderr.strip()[-400:])
    finally:
        if prev is None:
            os.environ.pop("CLAUDIO_USAGE_DIR", None)
        else:
            os.environ["CLAUDIO_USAGE_DIR"] = prev
        shutil.rmtree(d, ignore_errors=True)



def test_the_receiver_logs_under_the_name_the_verb_actually_has():
    """Every stderr line the receiver writes says `claudio recv`.

    The program is `usage/recv/otlp-recv` and the VERB is `claudio recv`; the
    file keeps its name because the path is what `_tool_path` resolves, but the
    prefix on its log lines is the thing a user sees and quotes.  It said
    `[otlp-recv]` on all ten of them -- a name no command has -- so `claudio
    recv serve` announced itself as a program the reader could not then find.

    Cosmetic, and pinned anyway: a release puts these lines in scrollback, in
    screenshots and in bug reports, where they outlive any later correction.
    """
    path = os.path.join(ROOT, "recv", "otlp-recv")
    check_true("the receiver source is where this test looks",
               os.path.exists(path))
    text = open(path, encoding="utf-8", errors="replace").read()

    # Every write to stderr, extracted rather than counted by hand: a new log
    # line added tomorrow is checked without anyone remembering to come here.
    #
    # `lines` used to be extracted and then never used again: the only real
    # assertions were "[otlp-recv] appears nowhere" and "[claudio recv] appears
    # somewhere", both of which an UNPREFIXED line satisfies vacuously -- and
    # there was one, `cmd_tail`'s sole diagnostic, printed by a first-run
    # command on a machine with no capture.  The docstring claimed every line;
    # the code asserted two.  So the extraction is now what is asserted.
    #
    # A `sys.stderr.write(` whose string is on the FOLLOWING line is joined to
    # its continuations before the check, because one real call is written that
    # way and a per-line test would either miss it or fail it wrongly.
    src = text.splitlines()
    calls = []
    for i, ln in enumerate(src):
        if "sys.stderr.write(" not in ln:
            continue
        # Take this line plus every following line up to the closing paren:
        # crude, and enough -- what is wanted is the string literals that
        # belong to this one call.
        chunk = [ln]
        depth = ln.count("(") - ln.count(")")
        j = i + 1
        while depth > 0 and j < len(src) and j - i < 8:
            chunk.append(src[j])
            depth += src[j].count("(") - src[j].count(")")
            j += 1
        calls.append(" ".join(x.strip() for x in chunk))
    check_true("the receiver's stderr calls were extracted", len(calls) >= 5)
    unprefixed = [c for c in calls if "[claudio recv]" not in c]
    check("every stderr line the receiver writes names the verb", unprefixed, [])
    check("the receiver never logs under the retired name",
          [ln for ln in src if "[otlp-recv]" in ln], [])
    check_true("...and it does log under the verb's own name",
               "[claudio recv]" in text)


def test_the_receiver_names_itself_in_the_server_header():
    """`Server:` is exactly `claudio-recv/1`, with no Python version in it.

    The stdlib default is `BaseHTTP/0.6 Python/3.14.6`.  It names a program
    that does not exist in this project, and it publishes the machine's exact
    Python patch version from a listening socket -- in a project otherwise
    scrupulous about what leaves the host.  The sibling door in `server/` was
    fixed and this half was not, which is the wrong half to leave: the door
    runs on one receiving host, this runs on every user's machine, and
    `recv/README.md` tells people to curl its `/healthz`.

    Compared EXACTLY and not with `startswith`.  BaseHTTPRequestHandler joins
    `server_version` and `sys_version` with a space unconditionally, so the
    empty `sys_version` that suppresses the Python half leaves a trailing
    space -- the defect IS the suffix, so a check that ignores suffixes cannot
    see it.  That is the same assertion `server/tests/test_all.py` makes about
    the door, and the two are deliberately the same shape.
    """
    with _datadir() as tmp:
        proc, got = _spawn_recv(tmp)
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:%d/healthz" % got["port"], timeout=5) as fh:
                hdr = fh.headers.get("Server")
            check("recv: the Server header is exactly the receiver's name",
                  hdr, "claudio-recv/1")
        finally:
            _kill_recv(proc)


def test_the_help_of_every_installed_entry_point_is_terminal_text():
    """`--help` is a terminal, not a Markdown renderer.

    `synth-otlp` took its description from `__doc__.splitlines()[0]`, whose
    first line is `Synthesise and POST Claude Code OTLP/JSON payloads.  **Not
    authoritative.**` -- literal asterisks in the output of a program the
    `Makefile` installs and `packaging/README.md` documents by path.  The same
    defect was fixed in `claudio usage` and left in its sibling, which is the
    shape this repository keeps catching: one of two halves corrected.

    The docstring stays Markdown, because it is written for whoever opens the
    file; the description argparse prints is a separate plain string.

    And every option carries a `help=`.  Six of `synth-otlp`'s seven had none
    at all, against `recv serve` where every one is documented -- a flag with
    no help is a flag nobody uses correctly, and `--email` in particular puts
    a value on the wire.
    """
    for path in (CU_PATH, RECV_PATH, os.path.join(ROOT, "recv", "synth-otlp")):
        rel = os.path.relpath(path, ROOT)
        check_true("help: %s is where this guard looks" % rel,
                   os.path.exists(path))
        p = subprocess.run([sys.executable, path, "--help"],
                           capture_output=True, text=True, timeout=60)
        text = p.stdout + p.stderr
        check("help: %s --help exits 0" % rel, p.returncode, 0)
        # `**bold**` and `` `code` `` are the two spellings this project's
        # prose actually uses.  A bare `*` is not matched: argparse writes
        # none, but a sentence legitimately might.
        offenders = [m for m in ("**", "`") if m in text]
        check("help: %s --help renders no raw Markdown" % rel, offenders, [])

    # Every option of every parser has help text.  Read out of the source
    # rather than out of `--help`, because argparse prints an option with no
    # help as a bare flag and there is nothing there to notice.
    for path in (CU_PATH, RECV_PATH, os.path.join(ROOT, "recv", "synth-otlp")):
        rel = os.path.relpath(path, ROOT)
        text = open(path, encoding="utf-8", errors="replace").read()
        bare = []
        for m in re.finditer(r"add_argument\(", text):
            # The call's own parentheses, to its close: enough to see whether
            # `help=` is inside it.
            i, depth = m.end(), 1
            while i < len(text) and depth:
                depth += (text[i] == "(") - (text[i] == ")")
                i += 1
            call = text[m.end():i]
            if "help=" not in call:
                bare.append(call.split(",")[0].strip())
        check("help: every option in %s documents itself" % rel, bare, [])


def test_both_python_verbs_answer_a_bare_invocation_the_same_way():
    """`claudio usage` and `claudio recv` with no subcommand: help, exit 1.

    They disagreed -- 1 from one, 2 from the other -- which is two answers to
    one situation from two verbs of one tool, and claudio's own convention is
    1 everywhere: `claudio mcp`, `claudio default`, `claudio show` and
    `Unknown command:` all exit 1.  A wrapper doing `claudio recv || handle`
    saw a status the rest of the tool never emits.

    Pinned because an exit code is exactly the kind of thing only a test holds
    still: nothing in either program's output changes when it drifts.
    """
    for name, path in (("claudio usage", CU_PATH),
                       ("claudio recv", RECV_PATH)):
        p = subprocess.run([sys.executable, path], capture_output=True,
                           text=True, timeout=60)
        check("%s with no subcommand exits 1" % name, p.returncode, 1)
        check_true("%s with no subcommand prints its help" % name,
                   "usage:" in (p.stdout + p.stderr))


def test_the_python_half_resolves_the_receiver_the_way_claudio_does():
    """`receiver=` is one key, read from one file, by both halves.

    It was read by `claudio` alone.  Nothing under `usage/` looked at it, so a
    machine with a working `receiver=` had `claudio run` start that receiver
    while `claudio usage doctor` reported one "missing" at the bundled path and
    exited non-zero for it, and `collect start` would have launched the other
    one.  Two answers to "which receiver is this machine's".

    Global conf ONLY, and that half matters as much: a `receiver=` line is a
    path to a program this machine executes, and a `claudio.conf` inside a
    repository someone else wrote must never be able to name it.
    """
    d = tempfile.mkdtemp()
    prev = os.environ.get("CLAUDIO_CONF")
    try:
        stub = os.path.join(d, "my-receiver")
        open(stub, "w").close()
        os.chmod(stub, 0o755)
        conf = os.path.join(d, "claudio.conf")

        # With no key, the bundled copy -- the behaviour that already existed.
        with open(conf, "w") as fh:
            fh.write("logging=local\n")
        os.environ["CLAUDIO_CONF"] = conf
        check("no receiver= names the copy that ships beside cu/",
              collector.receiver(), config.BUNDLED_RECEIVER)

        # With the key, the key.
        with open(conf, "w") as fh:
            fh.write("logging=local\nreceiver=%s\n" % stub)
        check("receiver= in the global conf outranks the bundled copy",
              collector.receiver(), stub)
        check_true("...and `available()` answers about THAT file",
                   collector.available())

        # A configured receiver that is not there must say which line named it.
        # "receiver missing at <path>" is true and sends the reader to the tree,
        # where the file is exactly where it ships and nothing is wrong.
        with open(conf, "w") as fh:
            fh.write("receiver=%s\n" % os.path.join(d, "absent"))
        check_true("a missing configured receiver is not reported as present",
                   not collector.available())
        msg = collector.receiver_missing()
        check_true("...and the message names the key that chose the path",
                   "receiver=" in msg)
        check_true("...and the file that key was read from", conf in msg)

        # The bundled case must NOT blame a key nobody wrote.
        with open(conf, "w") as fh:
            fh.write("logging=local\n")
        check_true("the bundled case names no key",
                   "receiver=" not in collector.receiver_missing())
    finally:
        if prev is None:
            os.environ.pop("CLAUDIO_CONF", None)
        else:
            os.environ["CLAUDIO_CONF"] = prev
        shutil.rmtree(d, ignore_errors=True)


def test_the_conf_reader_is_one_function_and_not_two_that_agree():
    """`ship.read_conf` IS `config.read_conf`, the same object.

    Both halves of `cu/` read claudio's global conf, and until recently each
    had its own five-line parser.  `ship` imports `collector`, so when
    `collector` needed the reader the choice was a circular import, a third
    copy, or moving it down to a module neither imports.  It moved.

    Asserted as identity and not as behaviour, exactly as `tail.file_identity`
    is: two implementations that agree today are what put 57% of a machine's
    captured spend permanently out of reach.
    """
    check_true("ship.read_conf is config.read_conf",
               ship.read_conf is config.read_conf)
    check_true("ship.conf_path is config.conf_path",
               ship.conf_path is config.conf_path)

def test_a_remedy_names_the_config_file_that_is_actually_read():
    """Every "add this line to the config" message names the file in effect.

    `CLAUDIO_CONF` moves the global conf, and `config.conf_path()` has always
    honoured it -- but six user-facing messages spelled `~/.claudio/claudio.conf`
    as a literal, so on a machine that had moved it they sent the reader to edit
    a file nothing reads, twice: once to add the line, once to wonder why it did
    nothing. claudio's own `_telemetry_lookup` prints the file that decided for
    exactly this reason, and a remedy is worth less than no remedy when it is
    confidently wrong.

    Both halves are asserted. The behavioural half proves the path follows the
    variable; the static half proves no message went back to spelling it, which
    is the way this rots -- one new diagnostic, copied from the one above it.
    """
    old = os.environ.get("CLAUDIO_CONF")
    try:
        moved = os.path.join(tempfile.gettempdir(), "somewhere-else.conf")
        os.environ["CLAUDIO_CONF"] = moved
        check("conf: the displayed path follows CLAUDIO_CONF",
              config.conf_display(), moved)
        os.environ.pop("CLAUDIO_CONF", None)
        shown = config.conf_display()
        check_true("conf: ...and collapses to ~ only when it is really under $HOME",
                   shown.startswith("~" + os.sep)
                   and os.path.expanduser(shown) == config.conf_path())
    finally:
        os.environ.pop("CLAUDIO_CONF", None)
        if old is not None:
            os.environ["CLAUDIO_CONF"] = old

    # The static half, and it is read with `ast` rather than by line, because
    # the distinction that matters is not where a line starts. Prose may name
    # the default path -- a comment or a docstring explaining the ordinary case
    # cannot mislead anybody at a terminal. A string LITERAL that is not a
    # docstring may not, because that is a message on its way to a user.
    # Comments are not in the tree at all, so they are exempt for free; the
    # docstring of every module, class and function is exempted by name.
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    literal = "~/.claudio/claudio.conf"
    offenders = []
    scanned = 0
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "tests")]
        for fn in files:
            if fn.endswith((".md", ".json", ".jsonl")) or fn.startswith("."):
                continue
            full = os.path.join(base, fn)
            try:
                with open(full, "r", encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
            scanned += 1
            docs = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    body = getattr(node, "body", None) or []
                    if (body and isinstance(body[0], ast.Expr)
                            and isinstance(body[0].value, ast.Constant)
                            and isinstance(body[0].value.value, str)):
                        docs.add(id(body[0].value))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and literal in node.value
                        and id(node) not in docs):
                    offenders.append("%s:%d"
                                     % (os.path.relpath(full, root), node.lineno))
    check_true("conf: the scan found files to read", scanned >= 3)
    check("conf: no message hardcodes the default config path", offenders, [])


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    start = time.time()
    for t in TESTS:
        try:
            t()
        except Exception as exc:            # a crash is a failure, not a stop
            global FAIL
            FAIL += 1
            FAILURES.append("%s raised %s: %s" % (t.__name__, type(exc).__name__, exc))
    dur = time.time() - start
    print("\n".join("FAIL " + f for f in FAILURES))
    print("\n%d passed, %d failed, %d test functions, %.2fs"
          % (PASS, FAIL, len(TESTS), dur))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
