"""Build a benchmark corpus at projected volume, out of the REAL records.

Nothing here invents a record shape.  Both streams are grown from the captured
fixtures in `usage/tests/fixtures/` by the DERIVED rule `server/tests/
fixtures.py` already uses: a real record, serialised in its own key order, with
NAMED fields overridden and nothing else touched.  Identity and clock fields
vary (that is what makes a corpus rather than one row repeated); every other
byte -- key names, key order, null placement, the float formatting of
`cost_usd_reported`, the `tags: {}` empty dict -- is the machine's own output.

That rule is the whole point.  A generator written to match what the reader
expects proves the reader agrees with the generator.  This one is a
transcription of what `cu.otlp.rows_from_payload` and the status-line shim
actually wrote, so a benchmark over it is a benchmark over the real shape,
including the parts a hand-written generator gets wrong: nulls in seven columns
that have never carried a value, `input_tokens` occasionally absent entirely,
and floats that round-trip to 17 significant digits.

    python3 server/bench/gen_corpus.py --stream a --rows 1000000 --out /tmp/a.jsonl
    python3 server/bench/gen_corpus.py --stream b --rows 339000  --out /tmp/b.jsonl

Volumes come from `server/README.md`: stream A five years is 62 M rows /
45.7 GB; stream B is 339k rows / 205 MiB.
"""

import argparse
import json
import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
ROOT = os.path.dirname(SERVER)
REAL = os.path.join(ROOT, "usage", "tests", "fixtures")

sys.path.insert(0, os.path.join(SERVER, "tests"))
sys.path.insert(0, os.path.join(ROOT, "usage"))

# Five years ending at the fixture capture, which is what the projection means.
YEARS = 5
SPAN_S = YEARS * 365 * 24 * 3600
T_END = 1786584384          # the newest real stream-A ts, to the second
T_START = T_END - SPAN_S

# The three real accounts in the capture, with the UUIDs the shipper stamps.
# Read from the real sample file rather than typed here, so a rescrub moves
# them in one place.


def _real_accounts():
    seen = []
    with open(os.path.join(REAL, "real-samples.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            r = json.loads(line)
            pair = (r["account"], r["account_uuid"], r["account_email"])
            if pair not in seen:
                seen.append(pair)
    return seen


def _real_ledger_rows():
    from cu import otlp
    rows = []
    with open(os.path.join(REAL, "real-api-request.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                rows.extend(otlp.rows_from_payload(json.loads(line)))
    return rows


def _real_samples():
    out = []
    with open(os.path.join(REAL, "real-samples.jsonl"), encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def _uuid(rnd):
    h = "%032x" % rnd.getrandbits(128)
    return "%s-%s-4%s-8%s-%s" % (h[0:8], h[8:12], h[13:16], h[17:20], h[20:32])


def gen_a(rows, out, seed=1):
    """Stream A, from the 8 real ledger rows.

    Overridden: request_id, ts, ts_ns, session_id, prompt_id, account_uuid,
    account, email, ingested_at.  Everything else is the real row -- model,
    query_source, the four token counts, cost, duration, the seven nulls.
    """
    rnd = random.Random(seed)
    base = _real_ledger_rows()
    accts = _real_accounts()
    # One session per ~12 requests, which is the capture's own shape (8 rows
    # over 4 sessions, and a session runs on).
    n = 0
    step = float(SPAN_S) / max(rows, 1)
    sess = [_uuid(rnd) for _ in accts]
    prompt = [_uuid(rnd) for _ in accts]
    written = 0
    with open(out, "w", encoding="utf-8") as fh:
        buf = []
        while written < rows:
            src = base[n % len(base)]
            ai = n % len(accts)
            if n % 12 == 0:
                sess[ai] = _uuid(rnd)
            if n % 2 == 0:
                prompt[ai] = _uuid(rnd)
            ts = T_START + step * written
            r = dict(src)
            r["request_id"] = "%016x" % rnd.getrandbits(64)
            r["ts"] = round(ts, 3)
            r["ts_ns"] = int(ts * 1e9)
            r["session_id"] = sess[ai]
            r["prompt_id"] = prompt[ai]
            r["account"] = accts[ai][0]
            r["account_uuid"] = accts[ai][1]
            r["email"] = accts[ai][2]
            r["ingested_at"] = round(ts + 1.7, 6)
            buf.append(json.dumps(r, separators=(",", ":")))
            written += 1
            n += 1
            if len(buf) >= 20000:
                fh.write("\n".join(buf) + "\n")
                buf = []
        if buf:
            fh.write("\n".join(buf) + "\n")
    return written


def gen_b(rows, out, seed=2):
    """Stream B, from the 14 real schema-2 samples.

    Overridden: ts, session_id, prompt_id, the two percentages and the two
    `resets_at`, session_cost_usd.  Windows advance the way the real capture's
    do -- a 5-hour boundary on the hour, a 7-day one -- so the join in the
    benchmark is a real join and not a cross product.
    """
    rnd = random.Random(seed)
    base = _real_samples()
    n = 0
    step = float(SPAN_S) / max(rows, 1)
    written = 0
    with open(out, "w", encoding="utf-8") as fh:
        buf = []
        while written < rows:
            src = base[n % len(base)]
            ts = T_START + step * written
            r = dict(src)
            r["ts"] = int(ts)
            five_start = int(ts) - (int(ts) % 18000)
            r["five_hour_resets_at"] = five_start + 18000
            r["five_hour_pct"] = int((int(ts) - five_start) * 100 // 18000)
            seven_start = int(ts) - (int(ts) % 604800)
            r["seven_day_resets_at"] = seven_start + 604800
            r["seven_day_pct"] = int((int(ts) - seven_start) * 100 // 604800)
            r["session_id"] = _uuid(rnd)
            r["prompt_id"] = _uuid(rnd)
            r["session_cost_usd"] = round(rnd.random() * 20, 6)
            buf.append(json.dumps(r, separators=(",", ":")))
            written += 1
            n += 1
            if len(buf) >= 20000:
                fh.write("\n".join(buf) + "\n")
                buf = []
        if buf:
            fh.write("\n".join(buf) + "\n")
    return written


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--stream", choices=("a", "b"), required=True)
    ap.add_argument("--rows", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=0)
    a = ap.parse_args(argv)
    fn = gen_a if a.stream == "a" else gen_b
    n = fn(a.rows, a.out, seed=a.seed or (1 if a.stream == "a" else 2))
    size = os.path.getsize(a.out)
    sys.stdout.write("%s rows=%d bytes=%d mean=%.1f\n"
                     % (a.out, n, size, size / float(n)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
