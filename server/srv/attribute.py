"""Attribution for a closed window: attributed, and residual.

**The residual is the product.**  It is the measure of usage from somewhere
you are not watching -- claude.ai in a browser, the desktop app, a phone, an
un-instrumented laptop -- and it is the only thing here that no single machine
could ever compute.  It must never be redistributed into the attributed
buckets to make the numbers tidy, which rules out the obvious implementation:

    attributed_i = movement * w_i / sum(w)          # WRONG

That normalisation makes the residual identically zero by construction, for
every window, for ever.  The numbers look complete and say nothing.  So the
split needs a conversion from weight to percentage points that does not come
from the window it is being applied to.

  attributed = rate * sum(weights of the requests we saw)
  residual   = movement - attributed

`rate` is percentage points per unit of weight, for one account **and one
window kind**.  A percentage point of the 5-hour limit and a percentage point
of the weekly limit are fractions of different plans with different
denominators -- the same rule that forbids summing across accounts, one level
down, inside a single account.  Fitting one rate over both kinds was not even
a tie: a 7-day window's weight is a SUPERSET of every 5-hour window's inside
it (the same requests, counted again) and its movement is `peak - trough`
rather than `peak - 0` because its baseline cannot be zero, so its
movement/weight ratio is structurally the smallest and `min` selected it
essentially always.  Measured on the replay with 16 instrumented requests: the
7-day window explained itself exactly (residual 0.0) while the SAME rows read
through the 5-hour windows reported residual fractions of 50%, 84%, 80% and
87% -- identical evidence, two reports, 0% and 87%, with the second one
telling the user to go hunting for a machine that does not exist.

The fit is the tightest rate at which no window of that kind is
over-attributed:

    rate = min over eligible windows of (movement_lo / weight_total)

and its assumption is stated in the output rather than buried: **at least one
observed window had no unwatched usage.**  The window that sets the rate has a
residual of exactly zero by construction, so the report names it -- a zero
residual that is an artefact of the fit must not read as a measurement.  It
names it on *every* attribution that used the rate, with that window's own
weaknesses, because `rate_is_from_this_window=False` on a row whose own
evidence is perfect is not a caveat anybody reads as one.

Eligibility has three criteria, and each excludes a window that cannot say
anything about the rate:

  * **Movement below the quantiser's resolution.**  The plan percentage is
    served as an integer, so a window that moved 0 pp carries no information
    except that the traffic was under a percentage point: 43 requests and
    $2.92 have been observed producing no visible change at all.  Including
    such a window drives the fitted rate to zero and attributes nothing.

  * **A weak lower bound.**  `movement_lo` is `max` over the samples that
    happened to be SEEN, so a machine that slept through the end of a window
    reports a peak far below the terminal value; that window's ratio collapses
    and `min` selects precisely it.  Measured: capping one window's
    observation at 8% -- changing nothing about the traffic -- dropped the
    fitted rate from 1.0 to 0.1579 and gave a neighbouring window, observed to
    within 39 s of its close with every one of its 23 requests in the ledger,
    a residual of 19.37 pp (84%) and not one word about a bound.

  * **A clamped sliver.**  A window whose `start` was clamped by an
    overlapping neighbour spans less time than its own kind's length while its
    weight still covers the requests inside it; see `window.py`.

**The weighting is provisional and the output says so on every row.**
`cost_usd_reported` is Claude Code's own figure at API list rates; on a Pro or
Max plan it is never billed, so it is a relative weight and nothing more.  It
is used because it folds in all four token classes at their correct relative
prices, and because which token class the plan percentage actually tracks is
an open question that only a server with many closed windows can answer.
"""

from .wire import _is_number as _num

WEIGHT_FIELD = "cost_usd_reported"

# Printed on every attribution.  Requirement, not decoration: a weight nobody
# has validated, reported without that sentence, becomes a fact by repetition.
PROVISIONAL_NOTE = (
    "weighting is PROVISIONAL: requests are weighted by cost_usd_reported, "
    "Claude Code's own figure at API list rates, which is notional on a "
    "subscription and is used only as a relative weight. Which token class "
    "the plan percentage actually tracks is unanswered; until it is, every "
    "attributed figure below inherits that assumption."
)

# A window must have moved at least this much to say anything about the rate.
# One percentage point is the resolution of the published figure: every
# distinct value in the 65 real samples is an integer-valued double.
QUANT_RESOLUTION_PP = 1.0

# How much of a window may go unobserved before its movement stops being usable
# evidence about the rate.  0.25 is read off the capture rather than chosen for
# roundness: the five real 5-hour observation gaps are 39, 395, 2469, 4989 and
# 17184 s against an 18000 s window -- 0.2%, 2.2%, 13.7%, 27.7% and 95% -- and
# the only wide separation anywhere in that series is between 13.7% and 27.7%.
#
# It is a preference and NOT a hard floor, and that distinction is the whole of
# its safety.  Every 5-hour window in the joint capture has a gap of 78% (the
# session simply stopped), so a hard floor would refuse to fit a rate on the
# only real multi-account fixture there is, and the tool would report nothing
# at all on the data it was built for.  So: a window observed close to its
# close outranks one that is not, and when EVERY candidate is blind the
# tightest available still pins the rate and `basis` says outright that it did.
MAX_GAP_FRACTION = 0.25

# A clamped window (see `window._refuse_overlapping`) spans less than its
# kind's length.  Its movement covers only that span while its weight covers
# every request inside it, so the ratio is understated by construction and
# `min` would select it.  0.9 rather than 1.0 because floating-point equality
# on `end - start` is not a thing to depend on.
MIN_SPAN_FRACTION = 0.9

# Dimensions that partition the requests: each request lands in exactly one
# bucket, so these sum to `attributed_pp` exactly.  `by_tag` does NOT -- a
# request carries a dict of user tags and belongs to as many buckets as it has
# tags -- so it is kept out of the invariant rather than being made to fit by
# splitting a request's weight between its own labels.
PARTITION_DIMENSIONS = ("host", "profile", "query_source", "model",
                        "session_id")


class Attribution(object):
    __slots__ = (
        "account_uuid", "kind", "resets_at", "state",
        "movement_pp", "movement_quant_lo", "movement_quant_hi",
        "rate", "rate_basis", "rate_pinned_by", "rate_is_from_this_window",
        "rate_pinned_stats",
        "requests_n", "weight_total", "weightless_n",
        "attributed_pp", "residual_pp", "residual_fraction",
        "over_attributed", "by", "by_tag", "coverage", "refused", "notes",
    )

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def as_dict(self):
        d = {s: getattr(self, s) for s in self.__slots__}
        if hasattr(d.get("coverage"), "as_dict"):
            d["coverage"] = d["coverage"].as_dict()
        return d

    def __repr__(self):
        return "Attribution(%s %s move=%s attr=%s resid=%s)" % (
            self.kind, self.resets_at, self.movement_pp, self.attributed_pp,
            self.residual_pp)


def weight_of(row):
    """(weight, weightless).  An absent cost is 0 weight and is COUNTED.

    Treating it as 0 is the safe direction -- it can only inflate the residual,
    never shrink it -- but it is exactly the direction that makes an unwatched
    surface look bigger than it is, so the count travels with the number.
    """
    w = row.get(WEIGHT_FIELD)
    if isinstance(w, (int, float)) and not isinstance(w, bool) and w >= 0:
        return float(w), False
    return 0.0, True


def rows_in(window, rows):
    """Requests inside the window, by their own timestamp.

    Stream A's `ts` is the machine's clock, so this is the one place a clock
    genuinely matters: a machine two hours behind files its requests into the
    previous window.  It is bounded and visible -- the window's own `skew`
    field names any host whose stream-B samples proved a skewed clock -- and
    there is nothing better available, because a request carries no window
    identity of its own.  That asymmetry between the streams is real and is
    reported, not smoothed over.

    `_num`, not a bare isinstance, and `window.contains` uses the same guard:
    `ts: true` is an int in Python and would otherwise be placed as the integer
    1.  A row this rejects is not silently gone -- `reconcile` collects exactly
    the rows both filters reject into `AccountReport.undatable` and says so in
    a note, because a row that is in no window AND in no unclaimed list is
    weight subtracted from `attributed_pp` and handed to the residual, which is
    the one figure here sold as evidence of an unwatched surface.
    """
    return [r for r in rows
            if _num(r.get("ts"))
            and window.start <= r["ts"] < window.end]


def fit_rate(pairs):
    """[(window, weight_total)] -> (rate, pinned_key, basis, pinned_window).

    ONE KIND, one account.  `reconcile` passes only same-kind windows and keys
    the result by kind; handing this both kinds is what made a 5-hour report a
    fraction of the weekly plan's denominator.

    Returns (None, None, reason, None) when nothing is eligible, and the caller
    then refuses to attribute rather than inventing a rate -- a made-up rate
    produces a residual that is a made-up number with a percentage sign on it.
    """
    cands = []
    for w, weight in pairs:
        if not w.closed:
            continue
        if weight <= 0:
            continue
        if w.movement_lo < QUANT_RESOLUTION_PP:
            continue
        if (w.end - w.start) < MIN_SPAN_FRACTION * w.length:
            continue
        cands.append((w, weight))
    if not cands:
        return None, None, "no-eligible-window", None

    # A window observed close to its close outranks one that is not; see
    # MAX_GAP_FRACTION for why this is a preference and not a floor.  A window
    # with no usable timestamp at all says nothing about tightness either way,
    # so it is never in the tight set.
    tight = [(w, weight) for w, weight in cands
             if w.observation_gap_s is not None
             and w.observation_gap_s <= MAX_GAP_FRACTION * w.length]
    used, fell_back = (tight, False) if tight else (cands, True)

    best, pinned, pinned_w = None, None, None
    for w, weight in used:
        r = w.movement_lo / weight
        if best is None or r < best:
            best, pinned, pinned_w = r, w.key, w
    basis = (
        "min movement/weight over this account's closed %s windows that moved "
        "at least %.0f pp and were observed within %.0f%% of their close; "
        "assumes at least one of them had no unwatched usage, so the window "
        "that pinned it has a residual of zero by construction"
        % (pinned_w.kind, QUANT_RESOLUTION_PP, 100.0 * MAX_GAP_FRACTION))
    if fell_back:
        basis += (
            ". NO candidate met that observation bound -- the tightest of the "
            "blind ones was used, so the rate rests on a peak that may be far "
            "below its window's terminal value and every attributed figure in "
            "this account inherits that")
    return best, pinned, basis, pinned_w


def pin_note(stats):
    """What the window that set the rate was, and how good its evidence was.

    On EVERY row that used the rate, including rows whose own evidence is
    perfect.  A window observed to within 39 s of its close, with every one of
    its requests in the ledger, inherited another window's blindness wholly
    into its residual and carried no caveat at all, because the only existing
    hint was `rate_is_from_this_window=False` -- a boolean nobody reads as "the
    figure beside it came from somewhere weaker".
    """
    gap = stats.get("observation_gap_s")
    cov = stats.get("coverage_fraction")
    return (
        "the rate used here was pinned by the %s window ending %s, which moved "
        "%.1f pp over %.6g of weight, was last observed %s before it closed "
        "and had coverage %s. Every attributed figure on this row inherits "
        "that window's weaknesses, whether or not this window has any of its "
        "own."
        % (stats.get("kind"), stats.get("resets_at"),
           stats.get("movement_pp") or 0.0, stats.get("weight_total") or 0.0,
           ("%d s" % gap) if gap is not None else "an unknown time",
           ("%.2f" % cov) if cov is not None else "unknown"))


def attribute(window, rows, rate, rate_basis, rate_pinned_by, cov,
              pinned_stats=None):
    """One closed window -> one Attribution.  Nothing is clamped or rebalanced."""
    notes = [PROVISIONAL_NOTE]

    if not window.closed:
        # Attribution only after a window closes.  An open window's movement
        # is a lower bound that is still rising, and every request in it is
        # still arriving.
        return Attribution(
            account_uuid=window.account_uuid, kind=window.kind,
            resets_at=window.resets_at, state=window.state,
            movement_pp=window.movement_lo,
            movement_quant_lo=window.movement_quant_lo,
            movement_quant_hi=window.movement_quant_hi,
            rate=None, rate_basis=None, rate_pinned_by=None,
            rate_is_from_this_window=False, rate_pinned_stats=None,
            requests_n=len(rows_in(window, rows)), weight_total=None,
            weightless_n=None, attributed_pp=None, residual_pp=None,
            residual_fraction=None, over_attributed=False, by={}, by_tag={},
            coverage=cov, refused="window-open", notes=notes)

    inside = rows_in(window, rows)
    weight_total, weightless = 0.0, 0
    for r in inside:
        w, missing = weight_of(r)
        weight_total += w
        weightless += 1 if missing else 0

    if cov is not None and cov.settled_zero:
        # No machine was observed listening for a single second of this
        # window, and the grace has elapsed on every host that attests.  The
        # movement is real and is reported; the residual would be 100% of it
        # and would be indistinguishable from browser usage, so it is refused
        # by name instead of published.
        return Attribution(
            account_uuid=window.account_uuid, kind=window.kind,
            resets_at=window.resets_at, state=window.state,
            movement_pp=window.movement_lo,
            movement_quant_lo=window.movement_quant_lo,
            movement_quant_hi=window.movement_quant_hi,
            rate=rate, rate_basis=rate_basis, rate_pinned_by=rate_pinned_by,
            rate_is_from_this_window=(rate_pinned_by == window.key),
            rate_pinned_stats=pinned_stats,
            requests_n=len(inside), weight_total=weight_total,
            weightless_n=weightless, attributed_pp=None, residual_pp=None,
            residual_fraction=None, over_attributed=False, by={}, by_tag={},
            coverage=cov, refused="no-coverage", notes=notes + [
                "no machine was observed listening during this window, so a "
                "residual here would be indistinguishable from usage on a "
                "surface that may not exist"])

    if rate is None:
        return Attribution(
            account_uuid=window.account_uuid, kind=window.kind,
            resets_at=window.resets_at, state=window.state,
            movement_pp=window.movement_lo,
            movement_quant_lo=window.movement_quant_lo,
            movement_quant_hi=window.movement_quant_hi,
            rate=None, rate_basis=rate_basis, rate_pinned_by=None,
            rate_is_from_this_window=False, rate_pinned_stats=None,
            requests_n=len(inside), weight_total=weight_total,
            weightless_n=weightless, attributed_pp=None, residual_pp=None,
            residual_fraction=None, over_attributed=False,
            by=_buckets(inside, None), by_tag=_tag_buckets(inside, None),
            coverage=cov, refused="no-rate", notes=notes + [
                "no closed window of this account moved at least %.0f pp with "
                "any observed traffic, so there is nothing to fit a rate on; "
                "shares of observed weight are reported, percentages are not"
                % QUANT_RESOLUTION_PP])

    attributed = rate * weight_total
    residual = window.movement_lo - attributed

    if pinned_stats:
        notes.append(pin_note(pinned_stats))
    for n in (window.notes or ()):
        notes.append(n)
    if residual < 0:
        notes.append(
            "attributed exceeds the observed movement: the movement is a "
            "LOWER bound (the published percentage is quantised to whole "
            "points and the snapshot behind it can be hours old), so this "
            "means the bound is loose, not that the requests are wrong. "
            "Nothing is clamped and the residual is reported negative.")
    if cov is not None and cov.fraction is not None and cov.fraction < 1.0:
        notes.append(
            "coverage is %.2f: %.0f%% of this window had no machine "
            "reporting, so part of the residual is time nobody was watching "
            "rather than a surface you do not control"
            % (cov.fraction, 100.0 * (1.0 - cov.fraction)))
    if cov is not None and cov.fraction is None:
        notes.append(
            "coverage is unknown: this account has sent no attestation, so "
            "nothing here distinguishes a machine that was off from a "
            "surface you do not control")
    if cov is not None and cov.spans_unreadable:
        notes.append(
            "%d uptime span(s) in this window's attestations could not be "
            "read, so the coverage figure beside them is a floor built from "
            "the spans that parsed and is NOT evidence that a machine was off"
            % cov.spans_unreadable)
    if weightless:
        notes.append(
            "%d of %d requests carried no cost_usd_reported and were weighted "
            "0, which inflates the residual by however much they used"
            % (weightless, len(inside)))
    if window.observation_gap_s is not None and window.observation_gap_s > 3600:
        notes.append(
            "the last observation of this window was %d s before it closed, "
            "so the peak is a weak lower bound on the terminal value"
            % window.observation_gap_s)
    if window.kind == "7d":
        notes.append(
            "7-day window: its length is assumed (no 7-day rollover appears "
            "in any capture) and its baseline is the smallest observation "
            "rather than zero, so this movement is weaker than a 5-hour one")

    return Attribution(
        account_uuid=window.account_uuid, kind=window.kind,
        resets_at=window.resets_at, state=window.state,
        movement_pp=window.movement_lo,
        movement_quant_lo=window.movement_quant_lo,
        movement_quant_hi=window.movement_quant_hi,
        rate=rate, rate_basis=rate_basis, rate_pinned_by=rate_pinned_by,
        rate_is_from_this_window=(rate_pinned_by == window.key),
        rate_pinned_stats=pinned_stats,
        requests_n=len(inside), weight_total=weight_total,
        weightless_n=weightless,
        attributed_pp=attributed, residual_pp=residual,
        residual_fraction=(residual / window.movement_lo
                           if window.movement_lo else None),
        over_attributed=residual < 0,
        by=_buckets(inside, rate), by_tag=_tag_buckets(inside, rate),
        coverage=cov, refused=None, notes=notes)


def _buckets(rows, rate):
    """{dimension: {value: {weight, attributed_pp, requests}}}.

    A request with no value for a dimension goes into the `None` bucket.  It
    is never dropped and never spread over the other buckets, because either
    would move weight the request did not have onto labels it does not carry.
    """
    out = {d: {} for d in PARTITION_DIMENSIONS}
    for r in rows:
        w, _ = weight_of(r)
        for d in PARTITION_DIMENSIONS:
            k = r.get(d)
            b = out[d].setdefault(k, {"weight": 0.0, "attributed_pp": None,
                                      "requests": 0})
            b["weight"] += w
            b["requests"] += 1
    if rate is not None:
        for d in out:
            for b in out[d].values():
                b["attributed_pp"] = rate * b["weight"]
    return out


def _tag_buckets(rows, rate):
    """`k=v` -> weight.  Deliberately NOT a partition; see PARTITION_DIMENSIONS."""
    out = {}
    for r in rows:
        w, _ = weight_of(r)
        tags = r.get("tags")
        keys = (["%s=%s" % (k, v) for k, v in sorted(tags.items())]
                if isinstance(tags, dict) and tags else [None])
        for k in keys:
            b = out.setdefault(k, {"weight": 0.0, "attributed_pp": None,
                                   "requests": 0})
            b["weight"] += w
            b["requests"] += 1
    if rate is not None:
        for b in out.values():
            b["attributed_pp"] = rate * b["weight"]
    return out
