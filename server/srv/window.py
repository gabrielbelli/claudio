"""Window reconstruction, per account, from stream B.

A window is identified by its `resets_at` and by nothing else.  That is the
single most consequential rule in this file and every other rule follows from
it.

**Membership is by `resets_at`, never by `ts`.**  A stream-B sample is
`(t, P̂, ê)` where `t` is the observing machine's clock but `P̂` and `ê` come
from a snapshot cached inside the claude process, and that snapshot is
arbitrarily old.  `replay-real.jsonl` proves it five times over: row 49 is
`ts=1786459730` naming `resets_at=1786424400`, a snapshot at least 35 330 s
(9.81 h) stale, reported *after* the window it names had closed.  Placing that
sample by its timestamp would file a real 14% reading against the wrong
window, and pooling all sessions and ordering by `ts` shows six apparent
in-window decreases (rows 1, 6, 12, 24, 31 and 49 -- row 1 shares its
timestamp with row 0 and differs only in the percentage) -- every one a stale
snapshot, not a decrease.

**The only sound cross-machine operator is `max`.**  A cached snapshot can
only be old, never from the future, and utilisation is non-decreasing within a
window, so every sample is a lower bound on that window's terminal value and
nothing more.  Differencing two samples, ordering them by `t`, interpolating
between them: all unsound, because the timestamp on a sample is not the
timestamp of the reading.  Re-clamping across machines is therefore exactly
`max` over every sample naming the same `resets_at`, whoever sent it and
whenever their clock says.

**A forward `resets_at` is a rollover: close the window, open the next.**
Most of the feature lives in that branch.  Locally, deleting it drops the
replay of 65 real samples from 49 records to 22 and turns a 90%->2% roll into
one record followed by permanent silence.  Here it is what makes the *set* of
windows an observable: five 5-hour windows come out of the replay, and a
reconstruction that required the percentage to rise would see one.

**Windows are activity-anchored, not a calendar tiling.**  Consecutive
`five_hour_resets_at` values in the replay step by exactly 18000 twice and
then by 47400, leaving 29 400 s -- 8 h 10 m -- belonging to no window at all.
A window opens on the first request after the previous one closed.  Requests
landing in such a gap are unclaimed, and saying so is the whole difference
between "we attributed everything" and the truth.

**Two `resets_at` of one kind can never be closer together than the window
length, and membership is a partition BY CONSTRUCTION rather than by that
rule holding.**  The structural argument is short: a window opens no earlier
than its predecessor's close and then runs a full `length`, so the next
`resets_at` is at least `length` after this one.  A closer pair is corruption,
a hand edit, a shared `usage_dir` or two clients disagreeing about the second
a window ends -- and reconciling clients that disagree is the entire
justification for this server, so it cannot be met with silence.  Left alone
it is the double counting the server exists to prevent, occurring inside one
account on one machine: `_reconstruct_kind` derived every bucket's `start` as
`reset - length` independently, so two windows overlapped, every request in
the overlap was a member of both, and `gaps()` said nothing because it only
tests `b.start > a.end`.  One stray record with `five_hour_resets_at` 100 s
past a real one counted 19 requests as 38, pinned the rate 19x too low off the
sliver, and moved the real window's residual from 0% to 95%.

That is not an exotic input: `replay-real.jsonl` row 16 carries
`five_hour_resets_at: 1`, so the real capture ALREADY contains a corrupt
`resets_at` and is caught only because `EPOCH_FLOOR` happens to reject that
particular value.  Three things answer it, and they are deliberately
independent:

  * the closer bucket is REFUSED BY NAME (`resets-at-overlaps-neighbour`),
    choosing by sample count because one stray record against eighteen is the
    stray -- evidence, not a coin flip;
  * on a tie there is no evidence either way, so both are kept and `start` is
    CLAMPED to the previous `resets_at`, which is what makes membership a
    partition whatever the refusal rule does;
  * `attribute.fit_rate` refuses to let a clamped sliver pin a rate.

Belt and braces on purpose.  Any one of the three can be weakened without the
double count returning, and the tie branch is what keeps the clamp reachable.
"""

from . import wire

# Window lengths.
#
# L5 is CONFIRMED by the data: consecutive `five_hour_resets_at` values in
# `replay-real.jsonl` differ by exactly 18000 twice (1786388400 -> 1786406400
# -> 1786424400).
#
# L7 is NOT confirmed by anything in this repository.  No capture contains a
# 7-day rollover: every 7-day sample in `replay-real.jsonl` names the same
# `resets_at`, and the three in `real-samples.jsonl` are one per account and
# never move.  7*86400 is the name of the window, not a measurement, and the
# reports say so -- a 7-day window's *start* is assumed, so its coverage
# fraction and its request set are assumed with it.
L5 = 18000
L7 = 7 * 86400
L7_CONFIRMED = False
L5_CONFIRMED = True

# 72 h.  The worst snapshot lateness observed is 35 330 s (9.81 h) --
# `replay-real.jsonl` row 49 -- and 72 h is 7.3x that.  The asymmetry is the
# reason for the size: the cost of too large a grace is report latency, and
# the cost of too small a one is a permanently understated window, which is
# one-sided and unrecoverable.
GRACE = 259200

KINDS = {
    "5h": ("five_hour_pct", "five_hour_resets_at", L5, True),
    "7d": ("seven_day_pct", "seven_day_resets_at", L7, False),
}


def _num(v):
    """A real number.  `True` is an int in Python and is not a timestamp."""
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class Window(object):
    """One reconstructed window for one account.  A value, not a record."""

    __slots__ = (
        "account_uuid", "kind", "resets_at", "start", "end", "length",
        "peak", "peak_ts", "peak_hosts", "trough",
        "baseline", "baseline_basis",
        "movement_lo", "movement_quant_lo", "movement_quant_hi",
        "samples_n", "hosts", "sessions",
        "first_ts", "last_ts", "last_ts_before_close", "observation_gap_s",
        "state", "close_basis", "superseded_by",
        "skew", "stale", "bounds_basis", "notes",
    )

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    @property
    def key(self):
        return (self.account_uuid, self.kind, self.resets_at)

    @property
    def closed(self):
        return self.state in ("closed", "settled")

    def as_dict(self):
        d = {s: getattr(self, s) for s in self.__slots__}
        for f in ("hosts", "sessions", "peak_hosts"):
            if isinstance(d.get(f), set):
                d[f] = sorted(d[f])
        return d

    def __repr__(self):
        return "Window(%s %s resets_at=%s peak=%s %s)" % (
            self.account_uuid[:8], self.kind, self.resets_at, self.peak,
            self.state)


def reconstruct(samples, now, account_uuid=None):
    """[(record, shipping host)] for ONE account -> its windows, both kinds.

    Sorted by kind then `resets_at`.  Records for another account are a
    programming error, not an input to be tolerated: `never sum across
    accounts` has to be enforced where the summing happens.
    """
    if account_uuid is not None:
        for rec, _h in samples:
            if rec.get("account_uuid") != account_uuid:
                raise ValueError(
                    "reconstruct() was given a sample for %r while building "
                    "windows for %r; accounts have different plans and "
                    "different denominators and must never be pooled"
                    % (rec.get("account_uuid"), account_uuid))

    windows, refusals = [], []
    for kind in ("5h", "7d"):
        w, r = _reconstruct_kind(samples, kind, now)
        windows.extend(w)
        refusals.extend(r)
    return windows, refusals


def _refuse_overlapping(buckets, refusals, kind, length):
    """Drop `resets_at` values that cannot both be genuine.  Sorted keeps.

    Mutates `buckets` and appends to `refusals`, one refusal per REFUSED
    RECORD rather than one per bucket, so the count of refusals is the count of
    records that were not placed -- the same accounting `epoch-below-floor`
    already gets.  A tie is kept whole and left to the clamp; see this module's
    docstring for why the two are separate.
    """
    kept = []
    for reset in sorted(buckets):
        if kept and reset - kept[-1] < length:
            prev = kept[-1]
            n_new, n_prev = len(buckets[reset]), len(buckets[prev])
            if n_new == n_prev:
                kept.append(reset)
                continue
            # The candidate is always LATER than `prev`, and `prev` is at least
            # `length` after the reset before it, so replacing `prev` cannot
            # produce a new overlap further back.
            loser = prev if n_new > n_prev else reset
            if loser == prev:
                kept[-1] = reset
            for rec, _h, _p in buckets[loser]:
                refusals.append((kind, "resets-at-overlaps-neighbour", rec))
            del buckets[loser]
            continue
        kept.append(reset)
    return kept


def _reconstruct_kind(samples, kind, now):
    pct_key, reset_key, length, zero_baseline = KINDS[kind]
    buckets, refusals = {}, []

    for rec, host in samples:
        raw_reset = wire.get(rec, reset_key)
        raw_pct = wire.get(rec, pct_key)
        if raw_reset is wire.ABSENT or raw_pct is wire.ABSENT:
            # Not a refusal: a schema that never carried this window's columns
            # is silent about them, which is different from having sent null.
            continue
        if raw_reset is None or raw_pct is None:
            # A genuine null.  `replay-real.jsonl` row 16 is null across every
            # 7-day field while carrying `five_hour_resets_at: 1`; one
            # unusable window must not cost the other.
            continue
        ok, why = wire.valid_epoch(raw_reset, now)
        if not ok:
            # Row 16 again, from the other side: `resets_at: 1` reads as an
            # ancient window, and a percentage without a valid window identity
            # is unusable -- the baseline argument only holds relative to a
            # known window start.  claudio refuses it locally for the same
            # reason; a record that reached here anyway came from a shared
            # usage_dir, a hand edit or a machine with a different clock.
            refusals.append((kind, why, rec))
            continue
        ok, why = wire.valid_pct(raw_pct)
        if not ok:
            refusals.append((kind, why, rec))
            continue
        buckets.setdefault(int(raw_reset), []).append((rec, host, float(raw_pct)))

    out = []
    resets = _refuse_overlapping(buckets, refusals, kind, length)
    for i, reset in enumerate(resets):
        rows = buckets[reset]
        peak = max(p for _r, _h, p in rows)
        trough = min(p for _r, _h, p in rows)
        # Guarded, and the guard is not decoration: `wire.check` deliberately
        # does not validate `ts` -- a record whose window identity is sound is
        # placeable whatever its clock says, because `resets_at` is what
        # membership is by.  So a sample with no usable `ts` reaches here, and
        # an unguarded `min()` over an empty sequence raised ValueError and
        # took the whole report down: one malformed line from one machine and
        # every account's reconciliation stops.  A missing timestamp costs the
        # fields derived from timestamps and nothing else.
        peak_tss = [r.get("ts") for r, _h, p in rows
                    if p == peak and _num(r.get("ts"))]
        peak_ts = min(peak_tss) if peak_tss else None
        peak_hosts = {(r.get("host") or h) for r, h, p in rows if p == peak}
        tss = [r.get("ts") for r, _h, _p in rows if _num(r.get("ts"))]
        before = [t for t in tss if t <= reset]
        start = reset - length
        if i and start < resets[i - 1]:
            # The tie branch of `_refuse_overlapping`: two buckets with equal
            # evidence were both kept, so nothing has separated them and the
            # partition has to be made here instead.  Clamping to the previous
            # `resets_at` costs the overlap its membership in the earlier
            # window and gives it to the later one, which is the direction that
            # cannot count a request twice.  `end` is untouched -- a window is
            # identified by its `resets_at` and by nothing else.
            start = resets[i - 1]

        # Clock skew is one-sided and that is what makes it detectable: a
        # snapshot cannot predate the window it names, so `resets_at - ts > L`
        # is impossible without the observing machine's clock being behind.
        # There are zero such rows in the replay, so this machine's clock is
        # not measurably wrong -- the check exists for the second machine.
        skew, stale = {}, {}
        for rec, host, _p in rows:
            t = rec.get("ts")
            if not _num(t):
                continue
            h = rec.get("host") or host or "?"
            if reset - t > length:
                skew[h] = max(skew.get(h, 0), int(reset - t - length))
            if t > reset:
                stale[h] = max(stale.get(h, 0), int(t - reset))

        superseded = resets[i + 1] if i + 1 < len(resets) else None
        if superseded is not None:
            state, basis = "closed", "rollover"
        elif now >= reset:
            state, basis = "closed", "elapsed"
        else:
            state, basis = "open", None
        if state == "closed" and now >= reset + GRACE:
            state = "settled"

        if zero_baseline:
            # W2: a 5-hour window opens on the first request after the
            # previous one closed, so its utilisation at `start` is 0 by
            # construction.  Four real rows land on it within seconds of a
            # window start and report exactly 0 -- rows 4, 21, 43 and 62, at
            # 268 s, 120 s, 221 s and 27 s after their window opened.
            # Movement and peak are therefore the same quantity for a closed
            # 5-hour window.
            baseline, bbasis = 0.0, "zero-by-construction"
        else:
            # No 7-day window is ever observed from 0 in any capture (the
            # lowest is 41).  What is sound is that utilisation at the start
            # is at most the smallest reading in the window, because every
            # reading is a lower bound taken at some time inside it.  So the
            # movement below is a lower bound built from a lower bound, and
            # it is weaker than the 5-hour one by exactly `trough`.
            baseline, bbasis = trough, "at-most-smallest-observation"

        out.append(Window(
            account_uuid=rows[0][0]["account_uuid"], kind=kind,
            resets_at=reset, start=start, end=reset, length=length,
            peak=peak, peak_ts=peak_ts, peak_hosts=peak_hosts, trough=trough,
            baseline=baseline, baseline_basis=bbasis,
            movement_lo=peak - baseline,
            # The quantiser is unknown -- round, floor and ceil are all
            # consistent with every value observed, since every distinct value
            # in the 65 samples is an integer-valued double.  Under round the
            # true value is within +-0.5 pp; under floor it is up to +1 pp.
            # Carried as an interval and never collapsed to a point, because
            # under floor the error is a systematic bias of ~1.8 pp/day at the
            # observed window rate, which is 8-12% of consumption invented as
            # "usage from somewhere you are not watching".
            movement_quant_lo=(peak - baseline) - 0.5,
            movement_quant_hi=(peak - baseline) + 1.0,
            samples_n=len(rows),
            hosts={(r.get("host") or h or "?") for r, h, _p in rows},
            sessions={r.get("session_id") for r, _h, _p in rows
                      if r.get("session_id")},
            first_ts=min(tss) if tss else None,
            last_ts=max(tss) if tss else None,
            last_ts_before_close=max(before) if before else None,
            # How close the last observation was to the close, which is the
            # only measure of how tight the `max` lower bound is.  Replay:
            # 2469, 395, 4989, 39, 17184 s.  39 s is strong evidence the peak
            # is the terminal value; 17 184 s is none at all.
            observation_gap_s=(reset - max(before)) if before else None,
            state=state, close_basis=basis, superseded_by=superseded,
            skew=skew, stale=stale,
            bounds_basis="confirmed" if kind == "5h" else "assumed-length",
            notes=([] if start == reset - length else [
                "this window's start was clamped to the previous resets_at: "
                "two resets_at of one kind were less than %d s apart with "
                "equal evidence behind each, so it spans %d s rather than %d "
                "and its movement covers less time than its weight does"
                % (length, reset - start, length)]),
        ))
    return out, refusals


def gaps(windows):
    """Intervals belonging to no window, per kind.

    W1 made visible.  In the replay this returns exactly one 5-hour gap, of
    29 400 s between 1786424400 and 1786453800 -- 8 h 10 m in which the
    account had no open 5-hour window at all.  A request landing there is
    unclaimed and must be reported as such rather than folded into a
    neighbour, which is the difference between a residual that means something
    and one that has absorbed an accounting error.
    """
    out = []
    for kind in ("5h", "7d"):
        ws = sorted((w for w in windows if w.kind == kind),
                    key=lambda w: w.resets_at)
        for a, b in zip(ws, ws[1:]):
            if b.start > a.end:
                out.append((kind, a.end, b.start, b.start - a.end))
    return out


def contains(window, ts):
    """Is this request inside the window?  Half-open, [start, resets_at).

    `_num`, not a bare isinstance: `ts: true` is an int in Python and would be
    placed as the integer 1.  `attribute.rows_in` asks the same question with
    the same guard, and the two must agree or a row is a member of a window
    for one of them and not the other.
    """
    return _num(ts) and window.start <= ts < window.end
