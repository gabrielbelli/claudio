"""The reconciler: a pure function over records, re-derived from byte zero.

No file, no socket, no database, no clock.  `now` is an argument.  A static
test walks every module in this package and asserts that none of them imports
anything that touches a file, a socket or a clock, because purity here is not
tidiness -- it is what keeps the storage decision open, what makes the
algorithm free to change, and what makes a week-late shipment retroactively
correct a window that closed days ago.  Every report is computed from every
record, every time.

The output is per account and there is no operation that spans accounts.
Percentage points are a fraction of one plan; two accounts have different
plans and different denominators, and a figure summed across them is a number
with a percentage sign and no meaning.  `Report.total_movement` therefore
takes an account and raises without one, and `window.reconstruct` raises if it
is handed a sample belonging to somebody else.
"""

from . import attribute, coverage, window, wire
from .ingest import Batch, Ingest, Refusal, ingest  # noqa: F401  (re-export)
from .window import GRACE, L5, L7                  # noqa: F401  (re-export)

KINDS = ("5h", "7d")


class AccountReport(object):
    """`rate`, `rate_basis` and `rate_pinned_by` are keyed BY WINDOW KIND.

    They were scalars, one per account, and that is the same error as summing
    across accounts one level down: a percentage point of the 5-hour limit and
    one of the weekly limit are fractions of different plans with different
    denominators.  See `attribute`'s docstring for the measurement.
    """

    __slots__ = ("account_uuid", "windows", "attributions", "gaps",
                 "unclaimed", "undatable", "known_hosts", "rate", "rate_basis",
                 "rate_pinned_by", "refusals", "requests_n", "samples_n",
                 "attestations_n", "notes")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def as_dict(self):
        d = {s: getattr(self, s) for s in self.__slots__}
        d["windows"] = [w.as_dict() for w in self.windows]
        d["attributions"] = [a.as_dict() for a in self.attributions]
        d["known_hosts"] = sorted(self.known_hosts or ())
        return d


class Report(object):
    __slots__ = ("accounts", "now", "schemas", "absent_fields", "counts",
                 "refusals")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def total_movement(self, account_uuid, kind="5h"):
        """Summed movement for ONE account.

        The account argument is mandatory and there is deliberately no
        all-accounts form.  Different plans, different denominators: summing
        5 pp of one account's 5-hour window with 5 pp of another's produces
        10 pp of nothing.
        """
        if not account_uuid:
            raise ValueError(
                "total_movement() requires an account_uuid: percentages "
                "belong to one plan and are never summed across accounts")
        acc = self.accounts.get(account_uuid)
        if acc is None:
            return None
        return sum(w.movement_lo for w in acc.windows
                   if w.kind == kind and w.closed)

    def as_dict(self):
        return {
            "now": self.now,
            "counts": self.counts,
            "schemas": self.schemas,
            "absent_fields": self.absent_fields,
            "refusals": [(r.stream, r.reason, r.detail) for r in
                         (self.refusals or [])],
            "accounts": {k: v.as_dict() for k, v in self.accounts.items()},
        }


def _as_pairs(samples):
    """Accept `[record]` or `[(record, shipping host)]`.

    The shipping host matters because stream B's v1 shape has no host column
    of its own -- 65 real records on disk carry 15 keys and `host` is not one
    of them -- so for those the envelope is the only machine identity there
    is.
    """
    out = []
    for s in samples:
        if isinstance(s, tuple):
            out.append((s[0], s[1]))
        else:
            out.append((s, s.get("host") if isinstance(s, dict) else None))
    return out


def _rate_override(rate, account_uuid):
    """The caller's `rate` argument, normalised to `{kind: rate}` for ONE
    account -- or None when the caller supplied nothing for it.

    A bare number RAISES.  It used to be accepted and applied to every window
    of the account, which is exactly the defect this module was repaired for:
    one rate cannot serve two plans with two denominators.  Refusing it is the
    only answer that cannot silently produce a 5-hour figure scaled by the
    weekly limit, and a caller who really has one calibration per kind can say
    so in four more characters.
    """
    if rate is None:
        return None
    if isinstance(rate, dict):
        if not rate or set(rate) <= set(KINDS):
            return dict(rate)
        per = rate.get(account_uuid)
        if per is None:
            return None
        if isinstance(per, dict) and (not per or set(per) <= set(KINDS)):
            return dict(per)
    raise ValueError(
        "rate must be given per window kind: a percentage point of the 5-hour "
        "limit and one of the weekly limit are fractions of different plans "
        "with different denominators, so no single number can serve both. "
        "Pass {'5h': r} (and/or {'7d': r}), or {account_uuid: {'5h': r}}.")


def reconcile(samples, rows, attestations, now, rate=None):
    """samples + ledger rows + attestations -> a per-account Report.

    `rate` overrides the fit when the caller has a real calibration (pp per
    unit of weight).  It is given PER WINDOW KIND -- `{'5h': r}` for every
    account, or `{account_uuid: {'5h': r}}` -- and a bare number raises; see
    `_rate_override`.  Unset is the normal case and the rate is fitted per
    kind; see `attribute.fit_rate` for what it assumes and how it says so.
    """
    pairs = _as_pairs(samples)
    accounts = set()
    for rec, _h in pairs:
        if rec.get("account_uuid"):
            accounts.add(rec["account_uuid"])
    for r in rows:
        if r.get("account_uuid"):
            accounts.add(r["account_uuid"])
    for a in attestations:
        if a.get("account_uuid"):
            accounts.add(a["account_uuid"])

    out, all_refusals = {}, []
    for uuid in sorted(accounts):
        acc_samples = [(r, h) for (r, h) in pairs if r.get("account_uuid") == uuid]
        acc_rows = [r for r in rows if r.get("account_uuid") == uuid]
        acc_att = [a for a in attestations if a.get("account_uuid") == uuid]
        acc_rate = _rate_override(rate, uuid)

        wins, refusals = window.reconstruct(acc_samples, now, account_uuid=uuid)
        for kind, why, rec in refusals:
            all_refusals.append(Refusal(wire.STREAM_SAMPLES, why, detail=kind,
                                        record=rec))

        known_hosts = set()
        for rec, h in acc_samples:
            known_hosts.add(rec.get("host") or h or "?")
        for r in acc_rows:
            if r.get("host"):
                known_hosts.add(r["host"])
        for a in acc_att:
            if a.get("host"):
                known_hosts.add(a["host"])
        known_hosts.discard("?")

        covs = {w.key: coverage.for_window(w, acc_att, known_hosts, now)
                for w in wins}

        # ONE FIT PER KIND.  Handing `fit_rate` both kinds at once fitted a
        # single rate over two plans with two denominators, and it was not a
        # tie -- the 7-day window's weight is a superset of every 5-hour
        # window's inside it and its movement is `peak - trough`, so its ratio
        # is structurally the smallest and `min` chose it every time one weekly
        # window had closed.  See `attribute`'s docstring.
        weights = {w.key: sum(attribute.weight_of(r)[0]
                              for r in attribute.rows_in(w, acc_rows))
                   for w in wins}
        fitted, basis, pinned, pin_stats = {}, {}, {}, {}
        for kind in KINDS:
            supplied = acc_rate.get(kind) if acc_rate else None
            if supplied is not None:
                fitted[kind] = supplied
                basis[kind] = "supplied by the caller, for %s windows" % kind
                pinned[kind], pin_stats[kind] = None, None
                continue
            r, p, b, pw = attribute.fit_rate(
                [(w, weights[w.key]) for w in wins
                 if w.kind == kind and not covs[w.key].settled_zero])
            fitted[kind], pinned[kind], basis[kind] = r, p, b
            pin_stats[kind] = None if pw is None else {
                "key": pw.key, "kind": pw.kind, "resets_at": pw.resets_at,
                "movement_pp": pw.movement_lo,
                "weight_total": weights[pw.key],
                "observation_gap_s": pw.observation_gap_s,
                "coverage_fraction": covs[pw.key].fraction,
            }

        attribs = [attribute.attribute(w, acc_rows, fitted[w.kind],
                                       basis[w.kind], pinned[w.kind],
                                       covs[w.key], pin_stats[w.kind])
                   for w in wins]

        # A row whose `ts` is unusable is in no window AND in no unclaimed
        # list, so it used to leave no trace anywhere: `rows_in` and the
        # unclaimed filter reject it identically, its weight is subtracted from
        # `attributed_pp`, and the difference lands in the residual -- the one
        # figure here sold as evidence of a surface you are not watching.
        # `wire.check` deliberately does not validate `ts` (membership is by
        # `resets_at` for stream B and a bad clock must not cost a whole
        # record), so the answer is a third bucket rather than a refusal.
        undatable = [r for r in acc_rows if not wire._is_number(r.get("ts"))]
        unclaimed = {}
        for kind in KINDS:
            kws = [w for w in wins if w.kind == kind]
            unclaimed[kind] = [r for r in acc_rows
                               if wire._is_number(r.get("ts"))
                               and not any(window.contains(w, r["ts"])
                                           for w in kws)]

        notes = []
        if undatable:
            notes.append(
                "%d request(s) carry no usable timestamp, so they are in no "
                "window of either kind and in no unclaimed list either. They "
                "are reported here and attributed nowhere; their weight is "
                "not in any attributed figure, so it is sitting in the "
                "residual." % len(undatable))
        if not acc_att:
            notes.append(
                "this account has sent no attestation, so coverage is unknown "
                "rather than zero and a dark machine cannot be told from a "
                "browser session")
        for kind in KINDS:
            if unclaimed[kind]:
                notes.append(
                    "%d request(s) fall in no %s window at all -- windows are "
                    "activity-anchored, not a calendar tiling, so the gaps "
                    "between them belong to nothing. They are reported here "
                    "and attributed nowhere."
                    % (len(unclaimed[kind]), kind))

        out[uuid] = AccountReport(
            account_uuid=uuid, windows=wins, attributions=attribs,
            gaps=window.gaps(wins), unclaimed=unclaimed, undatable=undatable,
            known_hosts=known_hosts, rate=fitted, rate_basis=basis,
            rate_pinned_by=pinned, refusals=[r for r in refusals],
            requests_n=len(acc_rows), samples_n=len(acc_samples),
            attestations_n=len(acc_att), notes=notes)

    # `all_refusals` was accumulated here and then thrown away -- the return
    # said `refusals=[]` -- so the top-level report, which is what `as_dict()`
    # exists to serialise, told every caller that nothing had been refused
    # while `AccountReport.refusals` named the real `resets_at: 1` record from
    # the capture.  The anti-silence surface reporting a silence.
    return Report(accounts=out, now=now, schemas={}, absent_fields={},
                  counts={"accounts": len(out)}, refusals=all_refusals)


def reconcile_ingest(ing, now, rate=None):
    """The same, from an `Ingest`: what the door produced, interpreted.

    This is the only seam between ingest and reconciliation, and it is a
    function call over in-memory lists.  Whatever storage is chosen appends
    bytes and reads them back into `ingest`; nothing below this line learns
    about it.
    """
    samples, rows, atts = [], [], []
    for uuid in sorted(ing.accounts()):
        samples.extend(ing.samples_for(uuid))
        rows.extend(ing.rows_for(uuid))
        atts.extend(ing.attestations_for(uuid))
    rep = reconcile(samples, rows, atts, now, rate=rate)
    rep.schemas = ing.schemas
    rep.absent_fields = ing.absent_fields
    rep.counts = dict(ing.counts)
    rep.counts["accounts"] = len(rep.accounts)
    # EXTEND, never replace: the door's refusals and the window-level ones are
    # both refusals, and assigning here is what hid every `epoch-below-floor`
    # from the only report a caller reads.
    rep.refusals = list(ing.refusals) + list(rep.refusals or [])
    return rep
