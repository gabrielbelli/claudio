#!/usr/bin/env python3
"""Independent implementation of the recorder's dedupe rule, in another
language, so that replay-expected.tsv is derived rather than blessed.

Not run by test.sh — test.sh replays the fixtures through claudio itself and
diffs against the goldens below. This is how the goldens were PRODUCED, kept
so that "where did this file come from" has an answer other than "the shell
printed it once".

    python3 fixtures/derive-expected.py < fixtures/replay-v1.jsonl \
      > fixtures/replay-expected.tsv

Regenerating this must never be the way a failing replay test is "fixed" — if
claudio and this disagree, one of them is wrong and the question is which.

Reads fixtures/replay-v1.jsonl on stdin, writes the golden TSV on stdout:
    <reason>\t<five_hour_pct>\t<seven_day_pct>
one line per record the recorder should emit. Percentages are printed as the
raw JSON literal from the fixture (empty for null), so the golden pins that
the recorder stores them unmodified.

This exists to cross-check claudio's rule with a second implementation rather
than freezing whatever the shell happened to do.
"""
import json
import math
import sys

PREC = 0
PLAUSIBLE = 1_000_000_000
# 30 days past the tick. Comfortably beyond the 7-day window and short of
# anything implausible: without a ceiling, one millisecond epoch is ~55000
# years out, sticks in the state as a mark nothing can beat, and silences its
# window for good.
HORIZON = 30 * 86400
# Shell arithmetic wraps silently past 2^63, so a value this long is not a
# usable quantised figure and _u_gt refuses to compare it.
MAX_Q_LEN = 18


def q(pct):
    """Quantise like jq's `round` — half away from zero.

    Not like C's printf '%.0f', which rounds half to EVEN: those two disagree
    at an exact .5, where printf gives 98 for 98.5 and jq gives 99. Neither is
    more correct — this is a trigger threshold, not a measurement — but the two
    implementations have to agree or this file stops being a cross-check and
    starts being a second opinion nobody can act on. Neither fixture contains
    a .5 value, so the choice does not move the goldens either way; it is
    written down because the next person to touch it will assume printf.

    None for anything that is not a number: the recorder's quantiser rejects
    it, so it neither triggers a record nor reaches the log, and a percentage
    too long to compare is rejected on the same grounds.
    """
    if pct is None or isinstance(pct, bool) or not isinstance(pct, (int, float, str)):
        return None
    try:
        f = float(pct)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    v = format(math.floor(abs(f) * (10 ** PREC) + 0.5) * (-1 if f < 0 else 1), "d")
    if v.startswith("-") or len(v) > MAX_Q_LEN:
        return None
    return v


def norm_reset(r, now):
    if r is None:
        return None
    try:
        f = float(r)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    v = format(math.floor(abs(f) + 0.5) * (-1 if f < 0 else 1), "d")
    if v.startswith("-") or len(v) > 11:
        return None
    return v if PLAUSIBLE <= int(v) <= now + HORIZON else None


def window(qv, rv, hw_q, hw_r):
    """Returns (fire, new_hw_q, new_hw_r). fire in {None,'change','reset'}."""
    if qv is None:
        return None, hw_q, hw_r
    if rv is not None and hw_r is not None:
        if int(rv) < int(hw_r):            # stale snapshot from another session
            return None, hw_q, hw_r
        if int(rv) > int(hw_r):            # rollover
            return "reset", qv, rv
    if hw_q is None or float(qv) > float(hw_q):
        return "change", qv, (rv if rv is not None else hw_r)
    if hw_r is None and rv is not None:
        return None, hw_q, rv
    return None, hw_q, hw_r


def main():
    st = None                              # None == no state file yet
    out = []
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        rec = json.loads(raw)
        q5, q7 = q(rec.get("five_hour_pct")), q(rec.get("seven_day_pct"))
        r5 = norm_reset(rec.get("five_hour_resets_at"), rec["ts"])
        r7 = norm_reset(rec.get("seven_day_resets_at"), rec["ts"])
        if q5 is None and q7 is None:
            continue                        # no rate-limit block: nothing at all

        reason = None
        if st is None:
            st = {"q5": None, "r5": None, "q7": None, "r7": None, "ts": None}
            reason = "first"

        f5, st["q5"], st["r5"] = window(q5, r5, st["q5"], st["r5"])
        f7, st["q7"], st["r7"] = window(q7, r7, st["q7"], st["r7"])

        if reason is None:
            if "reset" in (f5, f7):
                reason = "reset"
            elif "change" in (f5, f7):
                reason = "change"
        if reason is None:
            continue                        # heartbeat is off by default

        st["ts"] = rec["ts"]

        def lit(key, qv):
            # The raw JSON literal, so the golden pins that the recorder stores
            # the percentage unmodified — but only when it is a usable number.
            # A value the quantiser rejected is recorded as null: handing the
            # raw string to `jq --argjson` either fails to parse (losing the
            # whole record, the other window included) or lands an object in a
            # numeric field.
            v = rec.get(key)
            return "" if v is None or qv is None else json.dumps(v)

        out.append(f'{reason}\t{lit("five_hour_pct", q5)}\t{lit("seven_day_pct", q7)}')

    sys.stdout.write("\n".join(out) + "\n")
    hist = {}
    for line in out:
        hist[line.split("\t")[0]] = hist.get(line.split("\t")[0], 0) + 1
    sys.stderr.write(f"records={len(out)} {hist}\n")


main()
