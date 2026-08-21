"""Coverage: what fraction of a window had ANY machine reporting.

This is the guard on the residual.  The residual is sold as "usage from
somewhere you are not watching" -- browser, phone, an un-instrumented laptop.
A window during which none of your machines was listening also produces a
large residual, and the two are byte-identical in the output unless coverage
is reported beside it.  So a window with poor coverage says so, in the same
row, and a window with no coverage at all refuses to attribute rather than
publishing a residual that reads as evidence of a surface that may not exist.

Four states per host, and the difference between the last two is the whole
anti-silence mechanism:

  reporting  an attestation names this window and its spans cover part of it
  dark       an attestation names this window and its spans cover none of it,
             OR the host attests for other windows, the grace has elapsed and
             it has sent nothing naming this one.  A positive statement.
  pending    the host attests, this window is younger than the grace, and it
             has not spoken yet.  A machine that wakes within three days is
             not late, so this is not yet evidence of anything.
  silent     the host appears in the account's records but has never emitted
             an attestation at all.  It may have been up the whole time; an
             older shipper simply does not say.  Weaker than dark, so counted
             apart from it.

And one state per account: with no attestation anywhere, coverage is **None,
not 0.0**.  0.0 is a claim -- "nobody was listening" -- and None is the truth,
"nobody said".  That is today's normal state, because nothing in this
repository emits an attestation yet.

**Stream C is authored, not captured.**  There is no attestation anywhere in
this repository's fixtures and no code outside `server/` writes one.  Its
`up_spans` shape is `cu.collector.up_spans`' output -- `[start, stop, clean]`
with `clean` the *observed* graceful-stop flag -- which is real, but an
attestation carrying them has never crossed a wire.  Every attestation in the
suite is built by `tests/fixtures.attestation`, which records it in the
provenance ledger as AUTHORED, and a test asserts that the authored set is
exactly {manifest, attestation} -- so the day something else is invented, it
has to be admitted there first.
"""

from .window import GRACE


def _clip(spans, lo, hi):
    """(clipped spans, number of elements that could not be read).

    The count is the whole point.  These `continue`s used to drop a malformed
    span silently, and `wire.check` never looked at `up_spans` at all, so an
    unreadable attestation was accepted, emptied, and then reported as this
    module's `dark` state -- "a positive statement" that the machine was not
    listening, made about a machine that had said the opposite in a record the
    server was holding.

    `wire.check` now refuses such a record by name before it can get here, so
    on the ingest path this count is always 0.  It is kept, and carried on
    `Coverage`, because `reconcile.reconcile` takes attestations directly as
    well: a caller that has not been through the door must not be able to
    produce `fraction 0.0` without the report saying how many spans it failed
    to read.
    """
    out, bad = [], 0
    for s in spans or []:
        if not isinstance(s, (list, tuple)) or len(s) < 2:
            bad += 1
            continue
        a, b = s[0], s[1]
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            bad += 1
            continue
        if isinstance(a, bool) or isinstance(b, bool):
            bad += 1
            continue
        a, b = max(a, lo), min(b, hi)
        if b > a:
            out.append((a, b))
    return out, bad


def union(spans):
    """Merge overlapping intervals.  Two machines up at once is one second."""
    out = []
    for a, b in sorted(spans):
        if out and a <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], b))
        else:
            out.append((a, b))
    return out


def complement(spans, lo, hi):
    """The parts of [lo, hi) that no span covers."""
    out, cur = [], lo
    for a, b in union(spans):
        if a > cur:
            out.append((cur, a))
        cur = max(cur, b)
    if cur < hi:
        out.append((cur, hi))
    return out


class Coverage(object):
    __slots__ = ("fraction", "lower_bound", "basis", "hosts_reporting",
                 "hosts_dark", "hosts_pending", "hosts_silent", "uncovered",
                 "spans", "unclean_hosts", "spans_unreadable")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    @property
    def settled_zero(self):
        """Nobody was listening, and that is not going to change.

        The one hard branch in this module: attribution is refused here.
        Everywhere else coverage is a reported number and the reader judges,
        because a threshold would turn a number into a hidden verdict.

        A span this module could not read makes it NOT settled, however zero
        the fraction looks.  "Nobody was listening" and "we could not read what
        the machine said" are opposite claims, and only one of them is
        evidence.
        """
        return (self.fraction == 0.0 and not self.hosts_pending
                and not self.spans_unreadable)

    def as_dict(self):
        d = {s: getattr(self, s) for s in self.__slots__}
        for f in ("hosts_reporting", "hosts_dark", "hosts_pending",
                  "hosts_silent", "unclean_hosts"):
            if isinstance(d.get(f), set):
                d[f] = sorted(d[f])
        return d


def for_window(window, attestations, known_hosts, now):
    """Coverage of one window from the attestations naming it.

    `known_hosts` is every host that has appeared anywhere in this account's
    data.  `attestations` are this account's, all windows -- the ones naming
    other windows are what prove a host attests at all, which is what
    separates `dark` from `silent`.
    """
    if not attestations:
        return Coverage(fraction=None, lower_bound=False, basis="unknown",
                        hosts_reporting=set(), hosts_dark=set(),
                        hosts_pending=set(), hosts_silent=set(known_hosts),
                        uncovered=[], spans=[], unclean_hosts=set(),
                        spans_unreadable=0)

    def host_of(a):
        return a.get("host") or a.get("machine_id") or "?"

    attesting = {host_of(a) for a in attestations}
    naming = [a for a in attestations
              if a.get("window") == window.kind
              and a.get("resets_at") == window.resets_at]

    spans, reporting, unclean, spoke = [], set(), set(), set()
    unreadable = 0
    for a in naming:
        host = host_of(a)
        spoke.add(host)
        clipped, bad = _clip(a.get("up_spans"), window.start, window.end)
        unreadable += bad
        if clipped:
            reporting.add(host)
            spans.extend(clipped)
        for s in (a.get("up_spans") or []):
            if isinstance(s, (list, tuple)) and len(s) >= 3 and s[2] is False:
                unclean.add(host)

    within_grace = now < window.resets_at + GRACE
    mute = attesting - spoke
    pending = set(mute) if within_grace else set()
    dark = (spoke - reporting) | (set() if within_grace else mute)
    silent = set(known_hosts) - attesting

    merged = union(spans)
    covered = sum(b - a for a, b in merged)
    fraction = covered / float(window.length) if window.length else 0.0

    return Coverage(fraction=fraction,
                    # A pending host may yet report spans inside this window,
                    # so what is computed now can only rise.  Saying so is the
                    # difference between "21% covered" and "at least 21%".
                    lower_bound=bool(pending),
                    basis=("attestations-with-%d-unreadable-span(s)"
                           % unreadable if unreadable else
                           ("attestations" if naming
                            else "no-attestation-names-it")),
                    hosts_reporting=reporting, hosts_dark=dark,
                    hosts_pending=pending, hosts_silent=silent,
                    uncovered=complement(merged, window.start, window.end),
                    spans=merged, unclean_hosts=unclean,
                    spans_unreadable=unreadable)
