"""Measure DuckDB against this corpus.  Real milliseconds, no estimates.

Five queries, chosen because they are the five shapes the reader actually
serves and the five a storage decision turns on:

    filter      a filtered search  (account + time range + equality + text)
    terms       a terms aggregation (GROUP BY model, ORDER BY cost)
    histogram   a date histogram   (per-day cost and request count)
    lookup      one row by request_id
    join        stream A x stream B on account and time

Each is run against every store shape given on the command line -- JSONL read
in place, a persisted DuckDB database, Parquet -- and against the hand-rolled
scan in `srv/query.py` where that is tractable, so the comparison is measured
on one machine at one moment rather than quoted from two different notes.

    python3 server/bench/bench_duckdb.py --a /tmp/ddb/a-1m.jsonl \
        --b /tmp/ddb/b-339k.jsonl --shapes jsonl,parquet,db --repeats 3

Every timing is the MEDIAN of `--repeats` runs after one warm-up, and the
warm-up is reported separately: the first read of a JSONL file is a cold page
cache and that is a different measurement, not noise to be averaged away.
"""

import argparse
import json
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
sys.path.insert(0, SERVER)


def _duckdb():
    try:
        import duckdb
    except ImportError:
        sys.stderr.write(
            "duckdb is not installed.  This benchmark needs it:\n"
            "    python3 -m pip install duckdb\n")
        raise SystemExit(2)
    return duckdb


def timed(fn, repeats=3):
    """(median_s, first_s, all_s).  First run reported, never averaged in."""
    runs = []
    t0 = time.perf_counter()
    fn()
    first = time.perf_counter() - t0
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        runs.append(time.perf_counter() - t0)
    return statistics.median(runs), first, runs


# --------------------------------------------------------------------------
# the five queries, as SQL over whatever relation name is handed in
# --------------------------------------------------------------------------

Q_FILTER = """
SELECT request_id, ts, model, query_source, cost_usd_reported
FROM {a}
WHERE account_uuid = '{acct}'
  AND ts >= {lo} AND ts < {hi}
  AND model = 'claude-sonnet-5'
  AND query_source LIKE '%repl%'
ORDER BY ts DESC, request_id DESC
LIMIT 50
"""

Q_TERMS = """
SELECT model,
       count(*)                    AS requests,
       sum(cost_usd_reported)      AS cost,
       sum(input_tokens)           AS input_tokens,
       sum(cache_read_tokens)      AS cache_read_tokens
FROM {a}
WHERE account_uuid = '{acct}'
GROUP BY model
ORDER BY cost DESC
"""

# Epoch arithmetic, not `date_trunc('day', to_timestamp(ts))`: that returns
# TIMESTAMPTZ, and converting one to Python REQUIRES `pytz` -- a second
# non-stdlib dependency acquired by writing an ordinary date histogram.  It
# also avoids asserting a time zone the data does not carry.
Q_HISTOGRAM = """
SELECT CAST(ts / 86400 AS BIGINT) * 86400 AS day,
       count(*)               AS requests,
       sum(cost_usd_reported) AS cost
FROM {a}
WHERE account_uuid = '{acct}'
GROUP BY day
ORDER BY day
"""

Q_LOOKUP = """
SELECT request_id, ts, model, cost_usd_reported
FROM {a}
WHERE request_id = '{rid}'
"""

# The join the hand-rolled engine cannot do at all: every request placed into
# the 5-hour window that was open when it was made.  `resets_at` bounds the
# window above and the previous one bounds it below, which is exactly
# `srv/window.py`'s rule -- expressed here as a range join.
Q_JOIN = """
WITH w AS (
  SELECT account_uuid,
         five_hour_resets_at                       AS resets_at,
         max(five_hour_pct)                        AS pct_hi,
         min(five_hour_pct)                        AS pct_lo,
         count(*)                                  AS samples
  FROM {b}
  WHERE account_uuid = '{acct}'
  GROUP BY account_uuid, five_hour_resets_at
)
SELECT w.resets_at,
       w.pct_hi - w.pct_lo        AS movement,
       count(a.request_id)        AS requests,
       sum(a.cost_usd_reported)   AS weight
FROM w
LEFT JOIN {a} a
       ON a.account_uuid = w.account_uuid
      AND a.ts >= w.resets_at - 18000
      AND a.ts <  w.resets_at
GROUP BY w.resets_at, movement
ORDER BY w.resets_at DESC
LIMIT 200
"""

QUERIES = (("filter", Q_FILTER), ("terms", Q_TERMS), ("histogram", Q_HISTOGRAM),
           ("lookup", Q_LOOKUP), ("join", Q_JOIN))


# --------------------------------------------------------------------------
# the store shapes
# --------------------------------------------------------------------------

def rel_jsonl(path, columns=None, ignore_errors=True):
    """A relation reading the JSONL in place.

    `columns=` is the pinned-schema form and it is the only safe one: with
    inference, a column absent from the sampled prefix is DROPPED FROM THE
    RELATION -- measured, and the reason `srv/duck.py` pins.
    """
    ie = ", ignore_errors=true" if ignore_errors else ""
    if columns:
        cols = ", ".join("'%s': '%s'" % (k, v) for k, v in columns.items())
        return ("read_json('%s', format='newline_delimited', columns={%s}%s)"
                % (path, cols, ie))
    return "read_json_auto('%s', format='newline_delimited'%s)" % (path, ie)


def pinned_a():
    """The stream-A column list, DERIVED from `wire`, never typed here."""
    from srv import duck
    return dict(duck.LEDGER_SQL_TYPES)


def pinned_b():
    from srv import duck
    return dict(duck.SAMPLE_SQL_TYPES)


def build_parquet(con, src_rel, dst):
    if os.path.exists(dst):
        return dst
    if os.path.dirname(dst):
        os.makedirs(os.path.dirname(dst), exist_ok=True)
    con.execute("COPY (SELECT * FROM %s) TO '%s' (FORMAT parquet, "
                "COMPRESSION zstd)" % (src_rel, dst))
    return dst


def build_db(con_path, src_a, src_b, name_a="a", name_b="b"):
    duckdb = _duckdb()
    if os.path.exists(con_path):
        return con_path
    if os.path.dirname(con_path):
        os.makedirs(os.path.dirname(con_path), exist_ok=True)
    con = duckdb.connect(con_path)
    con.execute("CREATE TABLE %s AS SELECT * FROM %s" % (name_a, src_a))
    con.execute("CREATE TABLE %s AS SELECT * FROM %s" % (name_b, src_b))
    con.close()
    return con_path


# --------------------------------------------------------------------------
# the hand-rolled baseline
# --------------------------------------------------------------------------

def handrolled_scan(path, spec, limit_bytes=None):
    """`srv/query.py` over lines read with `json.loads`, exactly as today.

    Returns (seconds, matched, scanned, bytes_read).  `limit_bytes` stops
    early so the 45.7 GB case can be measured on a sample and scaled, with the
    sample size reported rather than hidden.
    """
    from srv import query as Q
    q = Q.compile_query(spec)
    if Q.refused(q):
        raise SystemExit("bench spec refused: %r" % (q.as_dict(),))
    cl = Q.clauses_of(q)
    matched = scanned = 0
    read = 0
    t0 = time.perf_counter()
    with open(path, "rb") as fh:
        for raw in fh:
            read += len(raw)
            row = json.loads(raw)
            scanned += 1
            if all(fn(row) for _l, fn in cl):
                matched += 1
            if limit_bytes and read >= limit_bytes:
                break
    return time.perf_counter() - t0, matched, scanned, read


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="stream-A JSONL")
    ap.add_argument("--b", required=True, help="stream-B JSONL")
    ap.add_argument("--shapes", default="jsonl")
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--acct", default=None)
    ap.add_argument("--tmp", default="/tmp/ddb")
    ap.add_argument("--threads", type=int, default=None)
    ap.add_argument("--scan-sample-bytes", type=int, default=2 * 1024 ** 3,
                    help="stop the Python baseline after this many bytes and "
                         "scale, reporting that it did (0 = whole file)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    duckdb = _duckdb()
    con = duckdb.connect()
    if args.threads:
        con.execute("SET threads=%d" % args.threads)

    # PINNED, like the server: an inferred schema drops a column absent from
    # the sampled prefix, so a benchmark over an inferred relation would be
    # timing a query the server cannot run.
    a_json = rel_jsonl(args.a, pinned_a())
    b_json = rel_jsonl(args.b, pinned_b())

    acct = args.acct or con.execute(
        "SELECT account_uuid FROM %s LIMIT 1" % a_json).fetchone()[0]
    rid = con.execute(
        "SELECT request_id FROM %s WHERE account_uuid='%s' "
        "ORDER BY ts DESC LIMIT 1 OFFSET 100000" % (a_json, acct)).fetchone()
    rid = rid[0] if rid else con.execute(
        "SELECT request_id FROM %s LIMIT 1" % a_json).fetchone()[0]
    lo, hi = con.execute(
        "SELECT min(ts), max(ts) FROM %s" % a_json).fetchone()
    span = hi - lo
    q_lo, q_hi = hi - span * 0.02, hi          # the newest 2% of history

    out = {"a": args.a, "b": args.b,
           "a_bytes": os.path.getsize(args.a),
           "b_bytes": os.path.getsize(args.b),
           "acct": acct, "shapes": {}}

    shapes = {}
    for shape in args.shapes.split(","):
        shape = shape.strip()
        if shape == "jsonl":
            shapes["jsonl"] = (a_json, b_json, None)
        elif shape == "parquet":
            pa = build_parquet(con, a_json, os.path.join(args.tmp, "a.parquet"))
            pb = build_parquet(con, b_json, os.path.join(args.tmp, "b.parquet"))
            shapes["parquet"] = ("'%s'" % pa, "'%s'" % pb,
                                 os.path.getsize(pa) + os.path.getsize(pb))
        elif shape == "db":
            dbp = os.path.join(args.tmp, "bench.duckdb")
            build_db(dbp, a_json, b_json)
            shapes["db"] = ("adb.a", "adb.b", os.path.getsize(dbp))
            con.execute("ATTACH '%s' AS adb (READ_ONLY)" % dbp)
        else:
            raise SystemExit("unknown shape: %s" % shape)

    for shape, (ra, rb, size) in shapes.items():
        res = {"store_bytes": size, "queries": {}}
        for name, sql in QUERIES:
            text = sql.format(a=ra, b=rb, acct=acct, rid=rid,
                              lo=q_lo, hi=q_hi)
            def run(t=text):
                con.execute(t).fetchall()
            med, first, runs = timed(run, args.repeats)
            res["queries"][name] = {"median_s": med, "first_s": first,
                                    "runs": runs}
            sys.stderr.write("%-8s %-10s median %8.3f s  first %8.3f s\n"
                             % (shape, name, med, first))
        out["shapes"][shape] = res

    # The comparison the whole exercise is for, measured in the same process
    # on the same machine at the same moment rather than quoted from two notes.
    sys.stderr.write("\n--- hand-rolled: srv/query.py over json.loads ---\n")
    el, matched, scanned, read = handrolled_scan(
        args.a, {"account_uuid": acct, "where": {"model": "claude-sonnet-5"}},
        limit_bytes=args.scan_sample_bytes or None)
    gib = read / float(1024 ** 3)
    whole = el * (out["a_bytes"] / float(read))
    out["handrolled"] = {"s": el, "gib": gib, "s_per_gib": el / gib,
                         "scanned": scanned, "matched": matched,
                         "whole_file_s": whole,
                         "sampled": read < out["a_bytes"]}
    sys.stderr.write(
        "scanned %d rows / %.2f GiB in %.3f s = %.3f s/GiB; whole file %.1f s%s\n"
        % (scanned, gib, el, el / gib, whole,
           "  (extrapolated from the sample above)"
           if read < out["a_bytes"] else ""))

    if args.json:
        sys.stdout.write(json.dumps(out, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
