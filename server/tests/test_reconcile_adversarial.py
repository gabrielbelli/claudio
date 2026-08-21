#!/usr/bin/env python3
"""Adversarial cases for the reconciliation core: concrete WRONG NUMBERS.

Run:  python3 server/tests/test_reconcile_adversarial.py

Every case in this file FAILS, and that is its contract.  Each constructs an
input and names the figure the reconciler reports that is not true.  The bar is
the one the design note sets: the residual is sold as "usage from somewhere you
are not watching", and an alert fires when it crosses a threshold, so a residual
manufactured out of an accounting artefact sends the user hunting for a machine
that does not exist.  A confident wrong number is worse than a refusal.

**Three of the four original cases have been repaired and have LEFT this file.**
A fixed defect pinned in a permanently-red suite is pinned by nothing -- nobody
reads a red file for a new red line, and `mutate.py` refuses to run against a
red tree, so it would never be mutated either.  They are now regression tests in
`test_all.py` (section 8) with mutations beside them in `mutate.py`:

  one rate fitted across BOTH window kinds, so a 5-hour figure was scaled by
  the weekly plan's denominator          -> test_all: rate is fitted per kind
  two `resets_at` less than a window apart overlapped and every request in
  the overlap was attributed twice       -> test_all: resets_at closer than a
                                            window
  the least-observed window pinned the rate for every other window, with no
  caveat on the victim                   -> test_all: a bound nobody watched

Provenance is the same rule as `test_all.py`.  Stream-B samples are REAL, read
byte for byte out of `usage/tests/fixtures/replay-real.jsonl`.  Ledger rows are
DERIVED from a REAL row (`real-api-request.jsonl`, the repl_main_thread request)
with `account_uuid`, `ts`, `request_id` and `cost_usd_reported` overridden and
nothing else -- there is no capture in which stream A and the replay account
coexist, and the alternative to deriving is not testing attribution at all.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import fixtures as fx                                    # noqa: E402
from srv import reconcile                                # noqa: E402

PASS = FAIL = 0
FAILURES = []

ACC = fx.REPLAY_ACCOUNT


def check(name, got, want, tol=None):
    global PASS, FAIL
    ok = (got is not None and abs(got - want) <= tol) if tol is not None \
        else got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append("%s\n     got:  %r\n     want: %r" % (name, got, want))


def check_true(name, cond):
    check(name, bool(cond), True)


# ------------------------------------------------------------- helpers -----

def rows_at(reset, n, cost=1.0, host="darwin", tag=""):
    """DERIVED ledger rows: one REAL request, re-stamped and re-timed."""
    base = fx.real_rows()[1]          # repl_main_thread, a real captured row
    out = []
    for k in range(n):
        out.append(fx.derive(
            "adversarial-request", "real-api-request.jsonl row 1", base,
            account_uuid=ACC, ts=reset - 9000 + k,
            request_id="%s%d-%d" % (tag, reset, k),
            cost_usd_reported=cost, host=host))
    return out


def samples_for(resets, cap=None, cap_window=None, drop_7d=False):
    """REAL replay samples, filtered to named windows.

    `cap` truncates one window's OBSERVATION (samples above the cap are simply
    never seen), which is what a machine that slept through the end of a window
    produces.  It changes nothing about the traffic.
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


def report(samples, rows, now=None):
    return reconcile.reconcile(samples, rows, [], now or fx.NOW_REPLAY)


def attribution(acc, kind, reset):
    for a in acc.attributions:
        if a.kind == kind and a.resets_at == reset:
            return a
    raise AssertionError("no %s attribution for %s" % (kind, reset))


# =========================================================== the cases =====

def test_the_quantisation_interval_is_collapsed_where_it_matters():
    """`movement_quant_lo/hi` exists and is never applied to the residual.

    `window.py` carries the interval "and never collapse[s] it to a point,
    because under floor the error is a systematic bias of ~1.8 pp/day ...
    which is 8-12% of consumption invented as 'usage from somewhere you are
    not watching'".  That sentence describes the residual exactly -- and
    `attribute` computes `residual = movement_lo - rate * weight`, a point,
    from a rate itself fitted on another window's `movement_lo`, another
    point.  `Attribution` has no `attributed_quant_*` or `residual_quant_*`.

    The exposure is largest where the fit is: `QUANT_RESOLUTION_PP` admits a
    window that moved 1 pp, whose true movement is [0.5, 1.5) under round and
    [1, 2) under floor, so the fitted rate -- and every attributed figure in
    the account -- is uncertain by a factor of two, unstated.
    """
    pin, victim = 1786406400, 1786471800
    rows = rows_at(pin, 19) + rows_at(victim, 23)
    acc = report(samples_for((pin, victim), cap=1, cap_window=pin,
                             drop_7d=True), rows).accounts[ACC]
    v = attribution(acc, "5h", victim)

    check_true("the residual carries the interval its own justification cites",
               hasattr(v, "residual_quant_lo") and hasattr(v, "residual_quant_hi"))
    basis = (acc.rate_basis or {}).get("5h") or ""
    check_true("a rate fitted on a 1 pp movement states its own factor-of-two "
               "uncertainty",
               "uncertain" in basis.lower() or "quantis" in basis.lower())


# =============================================================== runner =====

TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]


def main():
    for t in TESTS:
        try:
            t()
        except Exception as exc:
            global FAIL
            FAIL += 1
            FAILURES.append("%s raised %s: %s"
                            % (t.__name__, type(exc).__name__, exc))
    if FAILURES:
        print("\n".join("FAIL " + f for f in FAILURES))
    print("\n%d passed, %d failed, %d test functions" % (PASS, FAIL, len(TESTS)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
