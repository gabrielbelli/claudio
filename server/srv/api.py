"""The read API: `/api/v1/`, and the guardrails a query engine cannot hold.

This module is the contract omini is written against.  It is the whole of what
replaced `srv/ui.py`: a renderer shipped inside the server that produced its
numbers was one opinion about them, and every honesty property it carried was
expressed as HOW IT DREW THINGS, which is unenforceable the moment somebody
else draws them.  So each is a property of the PAYLOAD here, asserted by tests
that read the payload.

**It holds no clock and opens nothing.**  `now` is an argument, exactly as it
is everywhere else in `srv/`, and the two handles it is given -- a reconciler
`view` and a DuckDB `store` -- are the only things that touch a file.  That is
why this file imports nothing from the forbidden list and why the purity test's
impure set is still exactly `{serve.py, store.py}`: the I/O this module causes
is entirely through objects handed to it, which is what makes every endpoint
testable without a socket.

**The engine is split, and the split is forced rather than preferred.**
Stream A -- the ~30-column request rows -- is answered by DuckDB over
`accounts/<uuid>/ledger.jsonl` in place.  Stream B, the windows, the coverage
and the residual are answered by the reconciler, because DuckDB reading
`samples.jsonl` in place reads neither of the two things that make a sample
mean anything: the SHIPPING HOST, which lives on the manifest and not in the
v1 record (65 real records on disk are in that shape, and `wire.sample_key`
hashes the record together with it), and the dedupe, which is the core's.  A
`SELECT` over those files would produce a confident answer computed from
records it had double-counted and could not attribute to a machine.

**Five guardrails live here because SQL cannot express them.**  A query engine
returns zero rows for "there is nothing", "your filter removed everything" and
"that question has no answer", and it will add two accounts together without a
word:

  * **Three empty answers are three different values**, discriminated by KEY
    PRESENCE rather than by a status code or an empty array.  `unanswerable`
    carries no `result` AT ALL, so a client looping over `payload.result.rows`
    raises rather than rendering a tidy empty table -- which is the exact
    failure that shape exists to prevent.
  * **No cross-account total exists**, not even for tokens.  Different plans,
    different denominators.  The refusal happens before any SQL runs.
  * **Coverage accompanies every aggregate**, computed over the SELECTED rows
    in the same call that produced them.
  * **Unknown is not zero.**  `coverage.fraction` is `None` for every window in
    this store today, and the tri-state is carried by a BOOLEAN as well,
    because `x || 0`, `Number(null)` and every charting library turn a JSON
    null into zero without a word.
  * **Plan percentages are lower bounds** over quantised, possibly-stale
    snapshots -- a floor, never a reading.

**Nothing here is a copy a client also holds.**  The refusal reasons, the
coverage bases and the meanings of every `null` are served as DATA at
`/api/v1/capabilities`, so adding one is a one-line edit to a dict rather than
a change in two repositories that can disagree.
"""

import math

from . import attribute, duck, query, store, window, wire
from .query import refused


API_VERSION = "1"

PATH_V1 = "/api/v1/"

# The unversioned routes `srv/ui.py` used.  They are gone rather than
# forwarded: their only consumer was the page in this repository, and keeping a
# second envelope alive for a client that does not exist is ceremony that has
# to be tested for ever.
PATH_LEGACY = "/api/"

# Each names the v1 route that replaced it, because a 404 that only says "gone"
# leaves a client author to guess -- and this is the one thing the server knows
# and the client does not.
LEGACY_ROUTES = {
    "overview": "accounts",
    "windows": "windows",
    "window": "window",
    "requests": "search",
    "breakdown": "aggregate",
    "diagnostics": "diagnostics",
}


# ---------------------------------------------------------------------------
# 1. the envelope
# ---------------------------------------------------------------------------

OUTCOME_OK = "ok"
OUTCOME_NO_DATA = "no-data"
OUTCOME_FILTERED = "filtered-to-nothing"
OUTCOME_UNANSWERABLE = "unanswerable"

OUTCOMES = (OUTCOME_OK, OUTCOME_NO_DATA, OUTCOME_FILTERED,
            OUTCOME_UNANSWERABLE)

# The empty kinds are exactly the two `query.Selection.empty_because` values,
# spelled the same, so the wire word and the engine's word cannot drift.
EMPTY_KINDS = (OUTCOME_NO_DATA, OUTCOME_FILTERED)

# Reasons this layer can emit that `query.REASONS` does not know about, because
# they are about the transport or the engine rather than about the question.
# The union is served at `/api/v1/capabilities` and a test derives it from the
# source, exactly as `query.REASONS` is already derived.
TRANSPORT_REASONS = (
    # Reader auth. Both are refusals by NAME rather than empty results: an
    # account this token may not read must not look like an account that has
    # shipped nothing, which is the distinction the whole vocabulary exists for.
    "account-not-permitted",
    "unauthorised",
    "duckdb-missing",
    "engine-disagreement",
    "no-group-by",
    "no-view",
    "no-window-named",
    "param-not-a-number",
    "param-not-an-integer",
    "reader-failed",
    "too-many-buckets",
    "unknown-endpoint",
    "unknown-query-key",
    "unknown-window",
)

REFUSAL_REASONS = tuple(sorted(set(query.REASONS) | set(TRANSPORT_REASONS)))

CATALOGUE = PATH_V1 + "capabilities"


def _refusal(reason, detail, remedy, status=400):
    """A refusal, whole.  `remedy` is required and is never null.

    A refusal with no action in it reads as a fault, and a reader who cannot
    tell a broken server from an unanswerable question stops believing either.
    Every call site passes one; the fallback exists so that a `query`
    `Unanswerable` carrying none still arrives with something to do rather than
    with a `null` the client has to render as blank.
    """
    return status, {
        "outcome": OUTCOME_UNANSWERABLE,
        "api_version": API_VERSION,
        "refusal": {
            "reason": reason,
            "detail": detail,
            "remedy": remedy or ("this question has no answer over this "
                                 "store; the reason names which. The full "
                                 "catalogue is at " + CATALOGUE),
            "catalogue": CATALOGUE,
        },
    }


# A remedy crosses a LANGUAGE BOUNDARY here, and it used to cross it unchanged.
# `srv/store.py` and `srv/query.py` are the module API: their remedies name
# Python spellings -- `account_uuid=`, `breakdown_per_account()` -- which are
# exactly right for a caller holding a `DuckStore` and unusable for a client
# holding a URL.  Measured against this server: `crosses-accounts` told an HTTP
# client to "pass account_uuid=", the client passed `?account_uuid=<uuid>`,
# this API reads `?account=` and ignored it, and the identical refusal came
# back -- a remedy that is a complaint, which is the one thing `_refusal`'s
# docstring promises never to return.  `_account_or_refusal` already writes the
# wire spelling ("pass ?account=<uuid>"), so the vocabulary existed and this
# path simply did not use it.
#
# Explicit pairs rather than a regex, and applied at the two seams where a
# module refusal becomes wire bytes: an untranslated spelling stays visible as
# itself instead of being half-rewritten into something plausible.
WIRE_SPELLINGS = (
    ("pass account_uuid=", "pass ?account=<uuid>"),
    ("call breakdown_per_account()",
     "call GET " + PATH_V1 + "aggregate/per-account"),
)


def wire_remedy(remedy):
    """A module-API remedy respelled for a client that holds a URL."""
    if not remedy:
        return remedy
    for module_spelling, wire in WIRE_SPELLINGS:
        remedy = remedy.replace(module_spelling, wire)
    return remedy


def _as_refusal(u, status=400):
    """A `query.Unanswerable` on the wire, reason, detail and remedy intact."""
    return _refusal(u.reason, u.detail, wire_remedy(u.remedy), status=status)


def _ok(result, meta=None):
    doc = {"outcome": OUTCOME_OK, "api_version": API_VERSION,
           "result": result}
    if meta is not None:
        doc["meta"] = meta
    return 200, doc


def _empty(kind, result, empty, meta=None):
    """A 200 that says outright which of the two empties this is.

    `result` is PRESENT and empty here, unlike on a refusal: the question was
    answerable and the answer is nothing, so a client that renders a table
    renders an empty one correctly.
    """
    doc = {"outcome": kind, "api_version": API_VERSION, "result": result,
           "empty": empty}
    if meta is not None:
        doc["meta"] = meta
    return 200, doc


def _answer(result, empty, meta=None):
    """`_ok` or `_empty`, decided by whether an empty statement was built."""
    if empty is None:
        return _ok(result, meta)
    return _empty(empty["kind"], result, empty, meta)


def empty_statement(kind, scanned, matched, eliminated, sole_cause,
                    message, remedy):
    """The `empty` object, and the one conjunction it exists to preserve.

    On `no-data`, `eliminated` is `{}` and `sole_cause` is `None`: 0-of-0 is
    not "this filter emptied it", and blaming a clause for an empty corpus
    sends somebody to widen a filter that was never the problem.  That is
    `query.select`'s `if scanned and not kept` conjunction, expressed on the
    wire.
    """
    if kind == OUTCOME_NO_DATA:
        eliminated, sole_cause = {}, None
    return {
        "kind": kind,
        "scanned": int(scanned or 0),
        "matched": int(matched or 0),
        "eliminated": {k: int(v) for k, v in (eliminated or {}).items()},
        "sole_cause": sole_cause,
        "message": message,
        "remedy": remedy,
    }


def param_for_clause(label):
    """The URL parameter that produced a `query.clauses_of` label.

    The engine's clause labels are precise and unusable as instructions:
    `model in ['sonnet-4']` names the predicate, not the thing the caller can
    change.  So the message quotes the engine's label -- which is the truth
    about what eliminated the rows -- and the remedy names the PARAMETER, which
    is what the caller can act on.  The mapping is by construction from
    `query.clauses_of`'s own formats and a test walks every shape it can emit,
    so a new clause kind fails here rather than silently producing a remedy
    that names nothing.
    """
    if not isinstance(label, str):
        return None
    for prefix, param in (("account_uuid=", "account"), ("since=", "since"),
                          ("until=", "until"), ("text=", "text")):
        if label.startswith(prefix):
            return param
    # `<col> not in [...]` before `<col> in [...]`, because the second is a
    # substring of the first and matching it first would call every exclusion
    # an inclusion.
    if " not in " in label:
        return "not." + label.split(" not in ", 1)[0]
    if " in " in label:
        return "where." + label.split(" in ", 1)[0]
    return None


def _empty_from(sel_like, what="row"):
    """The `empty` object for anything carrying `empty_because`, or None."""
    kind = getattr(sel_like, "empty_because", None)
    if kind not in EMPTY_KINDS:
        return None
    scanned = getattr(sel_like, "scanned", 0) or 0
    sole = getattr(sel_like, "sole_cause", None)
    if kind == OUTCOME_NO_DATA:
        # The engine's own sentence wins where it has one -- it can tell a
        # MISSING ledger from a present but empty one and this layer cannot.
        # The fallback has to be true of both, which is why it does not say
        # "no file exists": a 0-byte ledger is also `no-data`.
        #
        # Read from a DEDICATED field, never from `notes[0]`.  `notes` is a
        # list of caveats and `page()`/`paginate()` unconditionally append the
        # keyset one, so mining the first note told a user whose search was
        # empty about cursors and late arrivals, and a user whose breakdown was
        # empty about API list rates -- while the fallback below, whose own
        # comment explains why it is worded to be true of both cases, was
        # UNREACHABLE on `/search` and `/aggregate`.
        message = getattr(sel_like, "no_data_note", None) or (
            "no request row has reached this store for this account. That is "
            "not the same as an account with no requests: the shipper may not "
            "have reached stream A on any machine yet.")
        remedy = "GET " + PATH_V1 + "health"
    elif sole:
        message = ("every one of the %d scanned %s(s) was eliminated by a "
                   "single clause: %s" % (scanned, what, sole))
        param = param_for_clause(sole)
        remedy = ("remove or widen `%s`" % param if param
                  else "remove or widen the clause named in `sole_cause`")
    else:
        message = ("no single clause eliminated everything; the intersection "
                   "of %d clause(s) is empty"
                   % len(getattr(sel_like, "eliminated", {}) or {}))
        params = sorted({param_for_clause(l) or l for l in
                         (getattr(sel_like, "eliminated", {}) or {})})
        remedy = "widen or remove one of: " + ", ".join(params)
    return empty_statement(kind, scanned,
                           getattr(sel_like, "matched", 0),
                           getattr(sel_like, "eliminated", {}), sole,
                           message, remedy)


# ---------------------------------------------------------------------------
# 2. coverage, and why a null alone is not enough
# ---------------------------------------------------------------------------

# `basis` -> whether the number beside it was MEASURED or is UNKNOWN.  Served
# at `/api/v1/capabilities` so omini holds no copy of this mapping: a client
# that hardcodes "no-attestation means unknown" is a client that will render a
# basis this server adds later as if it were measured.
COVERAGE_BASIS = {
    "attestations": "measured",
    "no-attestation": "unknown",
    "no-attestation-names-it": "unknown",
    "partly-unknown": "unknown",
    "no-window-touched": "unknown",
    "no-report-supplied": "unknown",
    "unknown": "unknown",
}

# The one basis the core PARAMETERISES, so it cannot be an exact key:
# `coverage.for_window` spells it `attestations-with-3-unreadable-span(s)`.
# Served as data beside the exact map so a client can still classify it
# without holding a copy of this rule -- and it is its own value rather than
# `measured`, because a fraction computed from spans some of which could not
# be read is a floor even by the standards of a measured one.  `basis_is`
# travels on the wire regardless, so no client has to do this lookup at all.
COVERAGE_BASIS_PREFIXES = {
    "attestations-with-": "measured-with-unreadable-spans",
}


def basis_is(basis):
    """`measured`, `measured-with-unreadable-spans` or `unknown`.

    Defaults to `unknown`, and that direction is deliberate: a basis this
    server adds later and forgets to declare reads as unmeasured rather than
    as measured.
    """
    if basis in COVERAGE_BASIS:
        return COVERAGE_BASIS[basis]
    if isinstance(basis, str):
        for prefix, kind in COVERAGE_BASIS_PREFIXES.items():
            if basis.startswith(prefix):
                return kind
    return "unknown"

# What a `null` means, per JSON path.  DATA, so a generic renderer can consult
# it and adding a key is a one-line edit here rather than a release of the
# front end.
NULL_MEANS = {
    "coverage.fraction":
        "unknown -- nobody said. NOT zero, which would be the positive claim "
        "that no machine was listening.",
    "coverage.placement.placed_fraction":
        "unknown -- no rows, or no reconciled report behind this answer",
    "windows[].rate":
        "unknown -- no rate could be fitted for this (account, kind)",
    "windows[].attributed_pp":
        "unknown -- the window is refused; see .refused",
    "windows[].residual_pp":
        "unknown -- not a zero residual",
    "accounts[].last_sample_age_s":
        "no sample has ever arrived for this account",
    "fields[].cardinality.absent":
        "this engine cannot separate an absent key from an explicit null",
    "empty.sole_cause":
        "no single clause is to blame -- and on `no-data` there is no clause "
        "to blame at all",
}


def coverage_payload(cov, describes):
    """A `query.CoverageStatement` on the wire, with the tri-state doubled.

    `known` is the load-bearing field and `fraction` corroborates it.  JSON
    `null` alone is not enough: `x || 0`, `Number(null)`, `d3.sum` and every
    charting library in existence turn it into zero without a word, and a
    coverage of 0.0 is not "nobody said" -- it is the positive claim that
    NOBODY WAS LISTENING.  The two must not be reachable from one another, so
    the boolean says which one this is and a test asserts both directions.
    """
    d = cov.as_dict() if hasattr(cov, "as_dict") else dict(cov)
    fraction = d.get("fraction")
    basis = d.get("basis")
    return {
        "known": fraction is not None,
        "fraction": fraction,
        "basis": basis,
        "basis_is": basis_is(basis),
        # The server's own sentences, verbatim and in order.  omini renders
        # these and writes none of its own: a fixed sentence in a client goes
        # stale against a store that contradicts it, which has already happened
        # here -- a page asserting that nothing emits an attestation, printed
        # beside a strip reading `basis: attestations`.
        "notes": list(d.get("notes") or ()),
        "placement": {
            "rows_total": d.get("rows_total"),
            "in_closed_windows": d.get("rows_in_closed_windows"),
            "in_open_windows": d.get("rows_in_open_windows"),
            "unclaimed": d.get("rows_unclaimed"),
            "undatable": d.get("rows_undatable"),
            "placed_fraction": d.get("placed_fraction"),
        },
        "windows": {
            "touched": d.get("windows_n"),
            "closed": d.get("windows_closed_n"),
            "with_coverage": d.get("windows_with_coverage_n"),
            "attestations_n": d.get("attestations_n"),
        },
        "hosts": {
            "reporting": list(d.get("hosts_reporting") or ()),
            "dark": list(d.get("hosts_dark") or ()),
            "pending": list(d.get("hosts_pending") or ()),
            "silent": list(d.get("hosts_silent") or ()),
        },
        "spans_unreadable": d.get("spans_unreadable"),
        "describes": describes,
    }


def window_coverage_payload(cov):
    """A per-window `coverage.Coverage` on the wire, with the same tri-state.

    There were two coverage shapes on the wire and only one of them honoured
    the contract.  `windows[].coverage`, `window.coverage` and
    `accounts[].last_closed.<kind>.coverage` are `coverage.Coverage.as_dict()`
    put on the wire raw -- no `known`, no `basis_is`, and a `basis` (`unknown`,
    `no-attestation-names-it`) that is in no served catalogue.  Those are
    precisely the rows carrying `attributed_pp` and `residual_pp`, so the
    object declared insufficient was sitting beside every attributed figure in
    the store.

    And BOTH halves of the tri-state are live in that slot: `fraction: 0.0`
    with basis `no-attestation-names-it` is the positive claim that an
    attesting host did not name this window, `fraction: null` is "nobody
    said", and on the wire only a JSON null separated them -- which `x || 0`,
    `Number(null)` and every charting library turn into the same zero.

    Everything the core computed is passed through unchanged; two keys are
    ADDED.  A reader that reshapes a number is a second opinion about it, so
    `spans`, `uncovered`, `lower_bound` and the host lists stay exactly as
    `windows_view` produced them.
    """
    if cov is None:
        return None
    d = dict(cov)
    d["known"] = d.get("fraction") is not None
    d["basis_is"] = basis_is(d.get("basis"))
    return d


# ---------------------------------------------------------------------------
# 3. constraints, stated once and served
# ---------------------------------------------------------------------------

MAX_LIMIT = 1000
MAX_BUCKETS = 2000
WINDOW_ROWS_CAP = 200

# A DISPLAY THRESHOLD, and it says so on the wire.  Nothing in any capture says
# how long a machine may be quiet before its last reading stops describing the
# present; the age travels beside it either way.
STALE_AFTER_S = 900

HISTOGRAM_METRICS = ("requests", "cost", "input_tokens", "output_tokens",
                     "cache_read_tokens", "cache_creation_tokens",
                     "duration_ms")

STREAMS = ("a", "b")

LOWER_BOUND_NOTE = store.LOWER_BOUND_NOTE

NO_TOTAL_NOTE = (
    "nothing here is summed across accounts. Different plans, different "
    "denominators: there is deliberately no total, not even for tokens.")

WEIGHT_NOTE = (
    "these are shares of OBSERVED WEIGHT. They become percentage points of a "
    "plan only inside a closed window, through a fitted rate, and only with "
    "coverage beside them.")

SYNTHETIC_NOTE = (
    "rows whose `source` is `synthetic` were made by a generator, not "
    "captured. Two of them once became the top consumer in a real account's "
    "report.")


# ---------------------------------------------------------------------------
# 4. parameters
# ---------------------------------------------------------------------------

def _num_param(params, key):
    """(value, refusal).  A number or nothing; never a silent zero.

    `float("NaN")`, `float("nan")` and `float("Infinity")` all parse, and NaN
    is the one that is not a bound at all: the two engines order it
    oppositely, so `until=NaN` answered `outcome: ok` over the WHOLE corpus
    while the declared oracle returned nothing.  `String(NaN)` is `"NaN"`, so
    a front-end date field that fails to parse sends it.  `wire.valid_pct`
    already had this rule one file over; it was simply not applied here.

    +/-inf is kept: it is an ordinary "no bound" spelling, it orders
    identically in both engines and it is pinned by its own test.
    """
    raw = params.get(key)
    if raw is None or raw == "":
        return None, None
    try:
        v = float(raw)
    except ValueError:
        return None, _refusal("param-not-a-number", "%s=%r" % (key, raw),
                              "pass epoch seconds as a number")
    if math.isnan(v):
        return None, _refusal(
            "param-not-a-number", "%s=%r" % (key, raw),
            "NaN is not a position in time: pass epoch seconds, or omit the "
            "bound to mean no bound")
    return v, None


def _int_param(params, key):
    raw = params.get(key)
    if raw is None or raw == "":
        return None, None
    try:
        return int(raw), None
    except ValueError:
        return None, _refusal("param-not-an-integer", "%s=%r" % (key, raw),
                              "pass a whole number")


def _resolve_value(rows, col, text):
    """A query-string value matched against the values the corpus really holds.

    Every parameter arrives as a string and half the columns are numeric, so
    something has to decide that `schema=1` means the integer.  Guessing with
    `int()` first is how `duration_ms=0` starts matching `False`; guessing from
    the column's name is the assumption-written-as-a-value failure this
    repository has paid for six times.  So it is resolved against what is
    actually in the rows, and a value nothing matches is passed through as the
    string -- which `select` then reports as `filtered-to-nothing` naming this
    clause, rather than as an error about a value that may simply be absent.
    """
    for r in rows:
        v = query.cell(r, col)
        if v is query.ABSENT or v is None:
            continue
        if str(v) == text:
            return v
    return text


# The URL parameters each route reads, declared once and checked at the single
# door in `Api.handle`.
#
# `spec_from` picked the keys it knew and never looked at what was left, so any
# misspelling was dropped without a word and the UNFILTERED answer came back as
# `outcome: ok`.  Measured against this server, alpha's 3 rows against the
# store's 8: `?where.model=claude-sonnet-5` matched 2, and `?wher.model=`,
# `?wheres.model=`, `?sinc=`, `?untill=`, `?tex=`, `?limitt=`, `?ordre=` and
# `?bogus=` each matched 3 -- the filter silently gone, 200 ok every time.
#
# The sharpest case is the scoping parameter, because it is now the guard that
# stops a cross-account total:
#
#     ?stream=a&account=<alpha>       -> 3 rows, 1 account,  1 email
#     ?stream=a&account_uuid=<alpha>  -> 8 rows, 3 accounts, 3 emails, ok
#
# `account_uuid=` is this module's own keyword, `query.py`'s spec key, and the
# spelling `store._crosses_accounts` put in its remedy until `wire_remedy()`
# landed -- so a client that followed the remedy verbatim had its scoping
# silently discarded and was handed the pooled answer.
#
# `query.compile_query` -- the declared oracle -- refuses an unknown spec key
# BY NAME, and `unknown-query-key` is published in
# `/api/v1/capabilities.refusal_reasons`, so the contract promised a refusal
# the wire could never produce.  Checked in `handle` rather than in
# `spec_from` because six routes never call `spec_from` at all and were
# equally silent.
#
# `where.<col>` and `not.<col>` are open-ended by design -- the column is part
# of the key -- so they are matched by PREFIX; an unknown column inside one is
# still refused, one layer down, by `compile_query`.
WHERE_PREFIXES = ("where.", "not.")

# The routes that build a `query` spec, and therefore the only ones for which
# `where.<col>=` and `not.<col>=` mean anything.  Allowed everywhere, they are
# the same silent drop one door along: `/accounts?where.model=x` would answer
# the unfiltered list as `ok`.  Derived from the source by a test, because a
# route that starts calling `spec_from` and is not added here has its filters
# refused.
SPEC_ROUTES = ("aggregate", "aggregate/per-account", "histogram", "search",
               "values")

# What `spec_from` itself consumes, on every route that builds a spec.
SPEC_PARAMS = ("account", "since", "until", "text", "order", "limit")

ROUTE_PARAMS = {
    "accounts": (),
    "aggregate": SPEC_PARAMS + ("by",),
    "aggregate/per-account": SPEC_PARAMS + ("by",),
    "capabilities": (),
    "diagnostics": ("account",),
    "fields": ("account",),
    "health": (),
    "histogram": SPEC_PARAMS + ("interval", "metric"),
    "lookup": ("account", "field", "value"),
    "search": SPEC_PARAMS + ("stream", "cursor", "verify"),
    "values": SPEC_PARAMS + ("fields",),
    "window": ("account", "kind", "resets_at"),
    "windows": ("account", "kind"),
}


def unknown_params(route, multi):
    """The URL keys this route does not read, or `()`.

    A route absent from the table declares nothing, which would silently
    accept everything -- so it is not defaulted to "anything goes": `handle`
    treats a missing entry as an empty set, and a test asserts the table names
    exactly the routes that exist.
    """
    known = set(ROUTE_PARAMS.get(route) or ())
    filters = route in SPEC_ROUTES
    return tuple(sorted(
        k for k in (multi or {})
        if k not in known
        and not (filters and k.startswith(WHERE_PREFIXES))))


def spec_from(params, multi, sample_rows):
    """A `query.compile_query` spec out of the query string, or a refusal.

    Nothing is defaulted into existence: a key that is not in the URL is not in
    the spec, so a client cannot be given a filter it did not ask for.
    """
    spec = {}
    if params.get("account"):
        spec["account_uuid"] = params["account"]
    # Named one at a time rather than looped.  `ROUTE_PARAMS` is kept honest
    # by a test that DERIVES, from this file, every parameter each route
    # reads -- and a derivation reads constants, so a loop variable is
    # invisible to it.  Two lines of repetition buy an exact derivation, and
    # an inexact one is how the table rots into refusing a real parameter.
    since, err = _num_param(params, "since")
    if err:
        return err
    if since is not None:
        spec["since"] = since
    until, err = _num_param(params, "until")
    if err:
        return err
    if until is not None:
        spec["until"] = until
    where, not_where = {}, {}
    for key, vals in (multi or {}).items():
        if key.startswith("where."):
            col = key[len("where."):]
            where[col] = [_resolve_value(sample_rows, col, v) for v in vals]
        elif key.startswith("not."):
            col = key[len("not."):]
            not_where[col] = [_resolve_value(sample_rows, col, v)
                              for v in vals]
    if where:
        spec["where"] = where
    if not_where:
        spec["not_where"] = not_where
    if params.get("text"):
        spec["text"] = params["text"]
    if params.get("order"):
        spec["order"] = params["order"]
    if params.get("limit"):
        lim, err = _int_param(params, "limit")
        if err:
            return err
        if lim is not None and lim > MAX_LIMIT:
            return _refusal(
                "limit-not-a-positive-integer", "limit=%d" % lim,
                "the cap is %d; page with `cursor` rather than asking for one "
                "large page" % MAX_LIMIT)
        spec["limit"] = lim
    return spec


# ---------------------------------------------------------------------------
# 5. the API
# ---------------------------------------------------------------------------

class Api(object):
    """Every route is one function.  Every answer carries its own outcome.

    `view` is the reconciler's (impure, injected).  `duck_store` is the DuckDB
    reader (impure, injected, and allowed to be absent -- in which case every
    stream-A route refuses BY NAME with the install command rather than
    answering zero rows, because "0 requests" is a perfectly plausible answer
    for an account that has not shipped yet and a layer degrading into one is
    indistinguishable from a working one over a quiet store).
    """

    def __init__(self, view, duck_store=None, counters=None):
        self.view = view
        self.duck_store = duck_store
        self.counters = counters

    # -- dispatch ----------------------------------------------------------

    # A method name and a URL path are one string under a bijection, spelled
    # once in each direction and round-tripped by a test: `__` is a slash and a
    # single `_` is a hyphen.  Deriving it beats a second table listing the
    # routes -- a hand-maintained one is a place to forget an endpoint, which
    # is how a route ends up reachable and undocumented, or documented and 404.
    @staticmethod
    def _route_name(method_suffix):
        return method_suffix.replace("__", "/").replace("_", "-")

    @staticmethod
    def _method_suffix(route):
        return route.replace("-", "_").replace("/", "__")

    def routes(self):
        return sorted(PATH_V1 + self._route_name(n[len("_v1_"):])
                      for n in dir(self) if n.startswith("_v1_"))

    def handle(self, path, params, multi, now=None):
        if path.startswith(PATH_LEGACY) and not path.startswith(PATH_V1):
            # Named, with the version AND the replacement in the remedy.  A 404
            # with no route out of it is how a client author concludes the
            # server is broken; one that names the successor costs a dict.
            old = path[len(PATH_LEGACY):].strip("/")
            moved = LEGACY_ROUTES.get(old)
            return _refusal(
                "unknown-endpoint", path,
                ("the API is %s; %s moved to %s%s"
                 % (PATH_V1, path, PATH_V1, moved)) if moved else
                ("the API is " + PATH_V1 + "; see " + CATALOGUE),
                status=404)
        if not path.startswith(PATH_V1):
            return _refusal("unknown-endpoint", path,
                            "the API is " + PATH_V1 + "; see " + CATALOGUE,
                            status=404)
        slug = path[len(PATH_V1):].strip("/")
        name = self._method_suffix(slug)
        route = getattr(self, "_v1_" + name, None) if name else None
        if route is None:
            return _refusal(
                "unknown-endpoint", path,
                "endpoints: " + ", ".join(self.routes()), status=404)
        # Per DERIVATION, before anything is read.  `meta.store_problems`
        # describes THIS store as of THIS answer; a list accumulated on a
        # process-lifetime store handle described how many queries somebody
        # had run, kept reporting a fault after the file was repaired, and
        # named another account's file in an account-scoped answer.
        if self.duck_store is not None:
            self.duck_store.reset_problems()
        # Before the route runs, so an unread parameter can never become a
        # silently unfiltered answer.  The refusal names the stray keys AND
        # what this route does read, because "unknown-query-key: wher.model"
        # with no list is a complaint rather than an action.
        stray = unknown_params(slug, multi)
        if stray:
            known = ROUTE_PARAMS.get(slug) or ()
            # The filter prefixes are mentioned only where they are accepted.
            # Offered on `/accounts`, the sentence would be exactly the kind
            # of confident wrong instruction this refusal exists to replace.
            filters = (" (plus where.<column>= and not.<column>=)"
                       if slug in SPEC_ROUTES else "")
            # Likewise the scoping hint: `/accounts` reads no `account=` and
            # telling its caller to pass one is the same wrong instruction in
            # a different sentence.  It is the single most useful thing to say
            # where it IS true, because `account_uuid=` is the module spelling
            # a client following an older remedy would have sent.
            scope = (" Scope with ?account=<uuid> -- account_uuid= is the "
                     "module spelling and is not read here."
                     if "account" in known else "")
            return _refusal(
                "unknown-query-key", ", ".join(stray),
                "%s reads: %s%s.%s"
                % (path, ", ".join(sorted(known)) or "no parameters",
                   filters, scope))
        return route(params, multi or {}, now)

    # -- shared ------------------------------------------------------------

    def _snap(self, now):
        return self.view.load(now)

    def _duck(self):
        """The DuckDB reader, or a refusal naming the install command.

        Never a None the caller might forget to check, and never an empty
        result: those are the two shapes in which a missing dependency becomes
        a plausible answer.
        """
        if self.duck_store is None:
            return _refusal(
                "duckdb-missing", "no store handle was configured",
                store.INSTALL_HINT, status=503)
        try:
            self.duck_store.con()
        except store.DuckDBMissing as exc:
            return _refusal("duckdb-missing", str(exc), store.INSTALL_HINT,
                            status=503)
        return self.duck_store

    def _meta(self, snap, extra=None):
        """Travels in EVERY payload, refusals included where one is available.

        `store_problems` is here rather than on a diagnostics route because a
        batch the reader could not reassemble otherwise makes an account render
        byte-identically to one that never shipped.
        """
        problems = _merge_problems(
            snap.problems,
            getattr(self.duck_store, "problems", ())
            if self.duck_store is not None else ())
        meta = {
            "accounts": [dict(identity_of(snap, u), account_uuid=u)
                         for u in snap.accounts],
            "now": snap.now,
            "derived_ms": snap.derived_ms,
            "derivations": self.view.derivations,
            "batches": snap.batches_n,
            "root": self.view.root,
            "store_problems": problems,
            "counts": dict(snap.report.counts or {}),
            "api_version": API_VERSION,
            "stale_after_s": STALE_AFTER_S,
            "no_total_note": NO_TOTAL_NOTE,
            "lower_bound_note": LOWER_BOUND_NOTE,
            "note": "re-derived from every record in the store, just now. "
                    "Nothing here is cached: a window's state depends on the "
                    "clock, so a stored report would announce as open a window "
                    "that has since closed.",
        }
        if extra:
            meta.update(extra)
        return meta

    def _account_or_refusal(self, snap, params):
        uuid = params.get("account")
        if not uuid:
            return _refusal(
                "no-account", None,
                "windows and percentages belong to one account: pass "
                "?account=<uuid>. Accounts here: "
                + (", ".join(snap.accounts) or "(none)"))
        if uuid not in snap.report.accounts:
            return _refusal("unknown-account", uuid,
                            "accounts in this store: "
                            + (", ".join(snap.accounts) or "(none)"),
                            status=404)
        return None

    def _known_account_or_refusal(self, snap, params):
        """The same question on the stream-A half, where it was not asked.

        `?account=<never-seen>` answered 200 `no-data` with "the shipper may
        not have reached stream A on any machine yet" -- byte-identical to the
        answer for an account that really does exist and really has shipped
        stream B and not stream A.  Two states, one message, and the message
        positively asserts the one that is false.  `unknown-account` is
        already in the published vocabulary and already reachable on
        `/windows`; it was reachable on the reconciler half and unreachable on
        this one.

        An account is real if EITHER reader has seen it: the reconciler knows
        the ones with batches, and the store knows the ones with a directory,
        and an account that has shipped stream A and nothing else is only in
        the second.
        """
        uuid = params.get("account") or None
        if uuid is None:
            return None, None
        known = uuid in snap.report.accounts
        if not known and self.duck_store is not None:
            known = self.duck_store.paths.knows(uuid)
        if known:
            return uuid, None
        listed = sorted(set(snap.accounts)
                        | set(self.duck_store.paths.accounts()
                              if self.duck_store is not None else ()))
        return None, _refusal("unknown-account", uuid,
                              "accounts in this store: "
                              + (", ".join(listed) or "(none)"), status=404)

    # -- 3.1 capabilities --------------------------------------------------

    def _v1_capabilities(self, params, multi, now):
        """The self-description.  omini hardcodes nothing.

        Everything a client would otherwise keep its own copy of is here:
        the refusal vocabulary, what each `basis` means, what every `null`
        means, and the constraints that decide whether a question is even
        worth asking.
        """
        engine_available = store.available()
        result = {
            "server": {"name": "claudio-door",
                       "door_version": DOOR_VERSION_HINT,
                       "api_version": API_VERSION},
            "endpoints": [{"path": p, "params": ENDPOINT_PARAMS.get(p, [])}
                          for p in self.routes()],
            "streams": {
                "a": {"name": "requests", "engine": "duckdb",
                      "record": "one row per API request",
                      "columns": list(query.LEDGER_COLUMNS),
                      "deduped_on": "request_id"},
                "b": {"name": "plan-usage observations",
                      "engine": "reconciler",
                      "record": "a 5-hour and 7-day plan percentage as the "
                                "status line saw it",
                      "why_not_sql": "the v1 record carries no host column -- "
                                     "the shipping host is on the manifest and "
                                     "`wire.sample_key` hashes the two "
                                     "together -- so a SELECT over the file in "
                                     "place reads neither the host nor the "
                                     "dedupe",
                      "columns": list(duck.SAMPLE_COLUMNS)},
            },
            "refusal_reasons": list(REFUSAL_REASONS),
            "coverage_basis": dict(COVERAGE_BASIS),
            "coverage_basis_prefixes": dict(COVERAGE_BASIS_PREFIXES),
            "null_means": dict(NULL_MEANS),
            "outcomes": list(OUTCOMES),
            "constraints": {
                "no_cross_account_total": True,
                "pagination": "keyset",
                "offset_paging": False,
                "max_limit": MAX_LIMIT,
                "max_buckets": MAX_BUCKETS,
                "window_rows_cap": WINDOW_ROWS_CAP,
                "stale_after_s": STALE_AFTER_S,
            },
            "auth": {"read": "none", "write": "bearer-routing-label",
                     "transport": "plaintext", "posture": "trusted-network"},
            "engine": {
                "name": "duckdb",
                "available": engine_available,
                "version": _duck_version(),
                "install_hint": None if engine_available
                                else store.INSTALL_HINT,
            },
            "exposure": "loopback",
            "histogram_metrics": list(HISTOGRAM_METRICS),
            "notes": [NO_TOTAL_NOTE, LOWER_BOUND_NOTE],
        }
        return _ok(result)

    # -- 3.2 fields --------------------------------------------------------

    def _v1_fields(self, params, multi, now):
        """The field schema.  Data, not code.

        A client that builds its controls from this cannot render a control for
        a column that has never been populated -- which reads as coverage --
        and cannot miss one this store has started carrying.
        """
        snap = self._snap(now)
        d = self._duck()
        if isinstance(d, tuple):
            return d
        uuid, bad = self._known_account_or_refusal(snap, params)
        if bad:
            return bad
        q = query.compile_query({"account_uuid": uuid} if uuid else {})
        if refused(q):
            return _as_refusal(q)
        avail = d.field_availability(q, account_uuid=uuid)
        # Through `_as_refusal`, which applies `wire_remedy`, so the store's
        # module spelling "pass account_uuid=" reaches the client as
        # "pass ?account=<uuid>" -- the parameter this route actually reads.
        if refused(avail):
            return _as_refusal(avail)
        cols = avail.get("columns") or {}
        fields = []
        for col in query.LEDGER_COLUMNS:
            st = cols.get(col) or {}
            fields.append({
                "name": col,
                "role": _role_of(col),
                "sql_type": duck.LEDGER_SQL_TYPES.get(col),
                "facetable": col in query.FACETABLE,
                "text_searchable": col in query.TEXT_SEARCHABLE,
                "groupable": _is_groupable(col),
                "is_id": col in query.ID_FIELDS,
                "never_observed_in_capture": col in query.NEVER_OBSERVED,
                "cardinality": {
                    "rows_with_a_value": st.get("present"),
                    "distinct_n": st.get("distinct_n"),
                    "null": st.get("null"),
                    # None, never 0: a pinned read cannot separate an absent
                    # key from an explicit null -- both are SQL NULL -- and a
                    # 0 here would be the confident claim that every null was
                    # written deliberately.
                    "absent": st.get("absent"),
                },
                "never_observed_here": bool(st.get("never_observed")),
            })
        result = {
            "fields": fields,
            "rows": avail.get("rows"),
            "tag_prefix": query.TAG_PREFIX,
            "id_fields": list(query.ID_FIELDS),
            "token_fields": list(query.TOKEN_FIELDS),
            "notes": list(avail.get("notes") or ()) + [
                "`never_observed_in_capture` is a statement about the whole "
                "capture this project was built from; "
                "`never_observed_here` is a statement about THIS store. A "
                "column can be the second without being the first."],
        }
        return _ok(result, self._meta(snap))

    # -- 3.3 values --------------------------------------------------------

    def _v1_values(self, params, multi, now):
        """Value distributions -- the sidebar -- with the third state spelled.

        `query.facets` OMITS a column no row carried.  This endpoint LISTS it,
        with `values: []` and `never_observed: true`, because omitting it from
        the sidebar and reporting it in a separate array is precisely how a
        client re-creates the empty-control bug on its own side.  `values: []`
        + `never_observed` and `values: []` + `all_null` are two different
        sentences and both are emitted.
        """
        snap = self._snap(now)
        d = self._duck()
        if isinstance(d, tuple):
            return d
        uuid, bad = self._known_account_or_refusal(snap, params)
        if bad:
            return bad
        rows_for_values = snap.rows(uuid) if uuid in snap.report.accounts else [
            r for u in snap.accounts for r in snap.rows(u)]
        spec = spec_from(params, multi, rows_for_values)
        if isinstance(spec, tuple):
            return spec
        q = query.compile_query(spec)
        if refused(q):
            return _as_refusal(q)
        limit, err = _int_param(params, "limit")
        if err:
            return err
        limit = limit or 20

        wanted = [c for c in (params.get("fields") or "").split(",") if c]
        # CHECKED, before it reaches the store.  This list bypasses
        # `query.compile_query` entirely and lands in `duck._col_expr` ->
        # `duck._ident`, which is the one place an identifier can be defended
        # at all -- and an unchecked name there became HTTP 500 `reader-failed`
        # ("this is a fault in the reader") with the generated SQL and
        # DuckDB's candidate-binding list in `detail`, over an ordinary typo
        # that `/aggregate?by=` answers correctly as `unknown-column`.
        for col in wanted:
            ok, _kind = query._known_column(col)
            if not ok:
                return _refusal(
                    "unknown-column", "fields=%r" % col,
                    "stream A columns: %s; user tags: %s<key>"
                    % (", ".join(sorted(query.LEDGER_COLUMNS)),
                       query.TAG_PREFIX))
        if not wanted:
            wanted = list(query.FACETABLE)
        # Stats for the columns ACTUALLY asked for, not the pinned ledger list:
        # a `tags.<key>` field is a real column here and asking about the
        # default set would leave it with no cardinality at all -- which would
        # render as an unlabelled control, the empty-control bug wearing a
        # different hat.
        avail = d.field_availability(q, account_uuid=uuid, columns=wanted)
        if refused(avail):
            return _as_refusal(avail)
        cols = avail.get("columns") or {}
        # One extra row is asked for per column so `truncated` is known
        # without a second count: a top-20 that looks complete is a coverage
        # claim.
        facets = d.facets(q, account_uuid=uuid, columns=wanted,
                          limit=limit + 1)
        if refused(facets):
            return _as_refusal(facets)
        sel = d.select(q, account_uuid=uuid)
        if refused(sel):
            return _as_refusal(sel)

        out = []
        for col in wanted:
            pairs = list(facets.get(col) or ())
            truncated = len(pairs) > limit
            shown = pairs[:limit]
            st = cols.get(col) or {}
            present = st.get("present")
            entry = {
                "name": col,
                "values": [[v, int(n)] for v, n in shown],
                "distinct_n": st.get("distinct_n") if st else len(pairs),
                "rows_with_a_value": present,
                "never_observed": bool(present == 0),
                "all_null": bool(present == 0 and (st.get("null") or 0) > 0),
                "truncated": truncated,
                "other_values_n": (max(0, (st.get("distinct_n") or 0) - limit)
                                   if st else 0),
            }
            if entry["never_observed"]:
                entry["note"] = (
                    "this column has never carried a value in this result. "
                    "Zero is written out rather than left as an empty control: "
                    "an empty facet list reads as 'I use no MCP tools' rather "
                    "than 'this has never been observed'.")
            out.append(entry)

        result = {
            "applies_to": {"spec": spec, "matched": sel.matched},
            "fields": out,
            "note": "counts are over the FILTERED result, not the account. A "
                    "distribution computed over the whole account beside a "
                    "filtered table is coverage-before-filter one screen over.",
        }
        empty = _empty_from(sel)
        return _answer(result, empty, self._meta(snap))

    # -- 3.4 search --------------------------------------------------------

    def _v1_search(self, params, multi, now):
        stream = (params.get("stream") or "a").lower()
        if stream not in STREAMS:
            return _refusal("unknown-query-key", "stream=%r" % stream,
                            "stream is one of: " + ", ".join(STREAMS))
        if stream == "b":
            return self._search_b(params, multi, now)
        return self._search_a(params, multi, now)

    def _search_a(self, params, multi, now):
        snap = self._snap(now)
        d = self._duck()
        if isinstance(d, tuple):
            return d
        uuid, bad = self._known_account_or_refusal(snap, params)
        if bad:
            return bad
        pure_rows = (snap.rows(uuid) if uuid in snap.report.accounts
                     else [r for u in snap.accounts for r in snap.rows(u)])
        spec = spec_from(params, multi, pure_rows)
        if isinstance(spec, tuple):
            return spec
        q = query.compile_query(spec)
        if refused(q):
            return _as_refusal(q)

        # Coverage over the SELECTED rows, from the same call that produced
        # them.  A coverage figure computed before the filter ran is a
        # confident number sitting next to a result it does not describe --
        # measured once as `3% of this window had a machine reporting` above a
        # two-row result and `nobody said` a few centimetres below.
        sel = d.select(q, account_uuid=uuid)
        if refused(sel):
            return _as_refusal(sel)
        kept = list(sel.rows)
        cov = (query.coverage_for(snap.report, uuid, kept)
               if uuid in snap.report.accounts
               else query.no_report_coverage(kept))

        page = d.page(q, cursor=params.get("cursor") or None,
                      coverage=cov, account_uuid=uuid)
        if refused(page):
            return _as_refusal(page)

        verify = (params.get("verify") or "").lower() in ("1", "true", "yes")
        verified = None
        if verify:
            # The narrowing contract, checked on demand: a storage layer may
            # return a superset and must NEVER return a subset.  Re-running the
            # pure predicate over the same rows is only affordable per request
            # because the caller asked for it.
            oracle = query.select(q, pure_rows, source="oracle")
            got = sorted(r.get("request_id") for r in kept)
            want = sorted(r.get("request_id") for r in oracle.rows)
            verified = got == want
            if not verified:
                return _refusal(
                    "engine-disagreement",
                    "duckdb matched %d, the python predicate matched %d"
                    % (len(got), len(want)),
                    "the JSONL is the record of truth and is unchanged; this "
                    "is a fault in the query layer. Re-run without verify= to "
                    "get the storage layer's answer, and report the spec.")

        pd = page.as_dict()
        result = {
            "rows": pd["items"],
            "page": {
                "limit": pd["limit"], "order": pd["order"],
                "matched": pd["matched"], "scanned": pd["scanned"],
                "next_cursor": pd["next_cursor"], "has_more": pd["has_more"],
                "undatable_n": pd["undatable_n"],
                "late_arrival_possible": True,
                "remedy": "re-run with an explicit since/until range to pick "
                          "up a row shipped late",
                "notes": list(pd["notes"] or ()),
            },
            "coverage": coverage_payload(
                cov, "the %d rows this result matched, not the account"
                     % (cov.rows_total or 0)),
            "spec": spec,
            "engine": {"name": "duckdb", "source": pd["source"],
                       "predicate": "sql", "verified": verified},
            "synthetic_rows": len([r for r in kept
                                   if r.get("source") == "synthetic"]),
            "synthetic_note": SYNTHETIC_NOTE,
        }
        return _answer(result, _empty_from(page), self._meta(snap))

    def _search_b(self, params, multi, now):
        """Stream B, from the RECONCILER rather than from DuckDB.

        A stream-B record without its shipping host is unattributable -- the v1
        shape has no host column and `wire.sample_key` hashes the record
        together with the manifest's host -- so every row here is the pair, and
        the dedupe is the core's.
        """
        snap = self._snap(now)
        bad = self._account_or_refusal(snap, params)
        if bad:
            return bad
        uuid = params["account"]
        since, err = _num_param(params, "since")
        if err:
            return err
        until, err = _num_param(params, "until")
        if err:
            return err
        limit, err = _int_param(params, "limit")
        if err:
            return err
        limit = min(limit or 50, MAX_LIMIT)
        order = (params.get("order") or "desc").lower()
        if order not in ("asc", "desc"):
            return _refusal("unknown-order", repr(order), "asc or desc")

        pairs = list(snap.samples(uuid))
        scanned = len(pairs)
        kept, undatable = [], 0
        for rec, host in pairs:
            ts = rec.get("ts")
            if not wire._is_number(ts):
                undatable += 1
                continue
            if since is not None and ts < since:
                continue
            if until is not None and ts >= until:
                continue
            kept.append({"record": rec, "shipping_host": host})
        kept.sort(key=lambda s: s["record"].get("ts") or 0,
                  reverse=(order == "desc"))
        shown = kept[:limit]

        empty = None
        if scanned == 0:
            empty = empty_statement(
                OUTCOME_NO_DATA, 0, 0, {}, None,
                "no sample has ever arrived for %s. That is not the same as "
                "an account whose plan never moved: a sample is only written "
                "when a percentage moves or a window rolls." % uuid,
                "GET " + PATH_V1 + "health")
        elif not kept:
            elim = {"since/until": scanned}
            empty = empty_statement(
                OUTCOME_FILTERED, scanned, 0, elim, "since/until",
                "every one of the %d scanned sample(s) was eliminated by a "
                "single clause: since/until" % scanned,
                "remove or widen `since`/`until`")

        # Deliberately NOT computed over the samples.  `coverage_for` places
        # rows by their own `ts`, and a sample is placed by the `resets_at` it
        # NAMES -- it carries a snapshot cached inside the claude process and
        # one real record names a window that had closed 9 h 49 m earlier.
        # Feeding samples to a ts-placer would file them into the wrong windows
        # and produce a confident placement that is simply wrong, so this is
        # the account's attestation story with the placement left empty and
        # `describes` saying so.
        cov = query.coverage_for(snap.report, uuid, [])
        result = {
            "rows": shown,
            "page": {"limit": limit, "order": order, "matched": len(kept),
                     "scanned": scanned, "next_cursor": None,
                     "has_more": len(kept) > limit,
                     "undatable_n": undatable,
                     "late_arrival_possible": True,
                     "remedy": "re-run with an explicit since/until range to "
                               "pick up a sample shipped late",
                     "notes": ["stream B is paged by neither cursor nor "
                               "offset: the whole five-year corpus is ~339k "
                               "rows and is deliberately unindexed."]},
            "coverage": coverage_payload(
                cov, "this account's attestation state. It does NOT describe "
                     "these rows: a sample is placed by the resets_at it "
                     "names, never by its ts, so the row placement that "
                     "accompanies a stream-A result has no meaning here and "
                     "is left empty rather than computed wrongly"),
            "spec": {"account_uuid": uuid, "since": since, "until": until,
                     "order": order, "limit": limit},
            "engine": {"name": "reconciler", "source": "srv.ingest",
                       "predicate": "python", "verified": None},
            "row_shape": {
                "record": "the sample as it was shipped, byte for byte",
                "shipping_host": "read off the batch manifest, because the v1 "
                                 "sample shape carries no host of its own",
            },
            "lower_bound_note": LOWER_BOUND_NOTE,
        }
        return _answer(result, empty, self._meta(snap))

    # -- 3.5 aggregate -----------------------------------------------------

    def _v1_aggregate(self, params, multi, now):
        by = params.get("by")
        if not by:
            return _refusal("no-group-by", None,
                            "pass ?by=<column>, or a tags.<key>. Groupable "
                            "columns are at " + PATH_V1 + "fields")
        snap = self._snap(now)
        d = self._duck()
        if isinstance(d, tuple):
            return d
        uuid, bad = self._known_account_or_refusal(snap, params)
        if bad:
            return bad
        pure_rows = (snap.rows(uuid) if uuid in snap.report.accounts
                     else [r for u in snap.accounts for r in snap.rows(u)])
        spec = spec_from(params, multi, pure_rows)
        if isinstance(spec, tuple):
            return spec
        q = query.compile_query(spec)
        if refused(q):
            return _as_refusal(q)
        b = d.breakdown(q, by, report=snap.report,
                        account_uuid=uuid if uuid in snap.report.accounts
                        else None)
        if refused(b):
            return _as_refusal(b)
        return _answer(self._breakdown_payload(b), _empty_from(b),
                       self._meta(snap))

    def _v1_aggregate__per_account(self, params, multi, now):
        """Fans out per account and produces NO TOTAL, not even for tokens.

        A rule with an exception in it gets applied by whoever remembers only
        the exception.
        """
        by = params.get("by")
        if not by:
            return _refusal("no-group-by", None, "pass ?by=<column>")
        snap = self._snap(now)
        d = self._duck()
        if isinstance(d, tuple):
            return d
        spec = spec_from(params, multi, [])
        if isinstance(spec, tuple):
            return spec
        # Refused BY NAME, not popped in silence.  `?account=` here asked to
        # scope a route whose entire purpose is to fan out over every account,
        # and the pop granted the request by ignoring it: 200 ok, three
        # accounts in the result, and a `spec` echo the client would have had
        # to diff to notice.  That is the same defect as an unknown parameter
        # being dropped, one door further in -- and this one is worse, because
        # the key is spelled correctly.
        if spec.get("account_uuid"):
            return _refusal(
                "unknown-query-key", "account=%s" % spec["account_uuid"],
                "this route fans out over every account by design and cannot "
                "be scoped to one. For a single account use GET "
                + PATH_V1 + "aggregate?account=<uuid>&by=<column>")
        spec.pop("account_uuid", None)
        out = {}
        for uuid in snap.accounts:
            s = dict(spec, account_uuid=uuid)
            q = query.compile_query(s)
            if refused(q):
                return _as_refusal(q)
            b = d.breakdown(q, by, report=snap.report, account_uuid=uuid)
            if refused(b):
                # Through `_refusal`, not by hand.  `wire_remedy` is applied
                # for the same reason `_as_refusal` applies it -- this is the
                # second and only other place a module remedy becomes wire
                # bytes -- but the dict itself is no longer spelled out here.
                # Hand-building it duplicated `_refusal`'s shape while
                # dropping the one thing its docstring promises, the
                # never-null `remedy` fallback, so a nested refusal could
                # carry `null` where a top-level one could not.  Nesting is
                # not a reason for a weaker contract: this fan-out is the
                # ONLY route that reports a refusal per account, so it is
                # exactly where a client meets one.
                _st, doc = _refusal(b.reason, b.detail, wire_remedy(b.remedy))
                out[uuid] = {"outcome": OUTCOME_UNANSWERABLE,
                             "refusal": doc["refusal"]}
                continue
            out[uuid] = self._breakdown_payload(b)
        result = {
            "by": by,
            "accounts": out,
            "total": None,
            "no_total_note": NO_TOTAL_NOTE,
        }
        return _ok(result, self._meta(snap))

    def _breakdown_payload(self, b):
        d = b.as_dict()
        cov = b.coverage
        return {
            "account_uuid": d["account_uuid"],
            "by": d["by"],
            "order": "cost desc",
            "buckets": [{"value": k, "value_is_null": k is None, "stats": v}
                        for k, v in b.rows_sorted()],
            "column_status": d["column_status"],
            "overhead": d["overhead"],
            "matched": d["matched"],
            "scanned": d["scanned"],
            "undatable_n": d["undatable_n"],
            "coverage": coverage_payload(
                cov, "the %d rows this aggregate grouped, not the account"
                     % ((cov.rows_total if cov is not None else 0) or 0)),
            "notes": list(d["notes"] or ()),
            "weight_note": WEIGHT_NOTE,
        }

    # -- 3.6 histogram -----------------------------------------------------

    def _v1_histogram(self, params, multi, now):
        """Buckets on epoch multiples.  Zero-filled, and never re-bucketed.

        Epoch arithmetic ONLY.  `to_timestamp()` returns a TIMESTAMPTZ and
        converting one to Python requires `pytz` -- a second non-stdlib
        dependency acquired by writing an ordinary date histogram -- and it
        would assert a timezone the data does not carry.  The client
        localises.
        """
        snap = self._snap(now)
        d = self._duck()
        if isinstance(d, tuple):
            return d
        interval, err = _num_param(params, "interval")
        if err:
            return err
        interval = int(interval or 3600)
        if interval <= 0:
            return _refusal("param-not-a-number", "interval=%r" % interval,
                            "pass a positive number of seconds")
        metric = params.get("metric") or "requests"
        if metric not in HISTOGRAM_METRICS:
            return _refusal("unknown-column", "metric=%r" % metric,
                            "metric is one of: "
                            + ", ".join(HISTOGRAM_METRICS))
        uuid, bad = self._known_account_or_refusal(snap, params)
        if bad:
            return bad
        pure_rows = (snap.rows(uuid) if uuid in snap.report.accounts
                     else [r for u in snap.accounts for r in snap.rows(u)])
        spec = spec_from(params, multi, pure_rows)
        if isinstance(spec, tuple):
            return spec
        q = query.compile_query(spec)
        if refused(q):
            return _as_refusal(q)

        since = spec.get("since")
        until = spec.get("until")

        hist = d.histogram(q, interval, metric, account_uuid=uuid)
        if refused(hist):
            return _as_refusal(hist)

        # The cap is checked against the span that will actually be FILLED,
        # not against the requested range, and it is the only place the
        # decision is made.  Two reasons it lives here rather than above:
        # a range given as only `since`, or as neither, is bounded by the data
        # and was previously not checked at all; and the check is what stops
        # `_zero_fill` looping, so a cap that could be bypassed is not a cap
        # but a hint -- `interval=1` over an unbounded range built a billion
        # buckets and hung the request rather than refusing it.
        lo, hi = _bucket_span(hist["buckets"], interval, since, until)
        if lo is not None:
            n = (hi - lo) / float(interval) + 1
            if n > MAX_BUCKETS:
                # NEVER silently re-bucketed: that changes the answer, and a
                # chart drawn at an interval nobody asked for is a chart whose
                # peaks are an artefact of the server's opinion.
                return _refusal(
                    "too-many-buckets",
                    "%.0f buckets at interval=%d; the cap is %d"
                    % (n, interval, MAX_BUCKETS),
                    "ask for a wider interval: at least %d seconds covers "
                    "this range" % int((hi - lo) / MAX_BUCKETS + 1))
        sel = d.select(q, account_uuid=uuid)
        if refused(sel):
            return _as_refusal(sel)
        cov = (query.coverage_for(snap.report, uuid, list(sel.rows))
               if uuid in snap.report.accounts
               else query.no_report_coverage(list(sel.rows)))

        buckets = _zero_fill(hist["buckets"], interval, lo, hi, metric)
        result = {
            "interval": interval,
            "metric": metric,
            "buckets": buckets,
            "aligned_to": "epoch multiples of interval, UTC-free: no timezone "
                          "is asserted anywhere. The client localises.",
            "zero_filled": True,
            "zero_fill_note": "empty buckets are emitted with rows: 0. A "
                              "missing bucket and a zero bucket are different "
                              "claims, and a chart draws them identically only "
                              "if the server withholds one.",
            "coverage": coverage_payload(
                cov, "the %d rows these buckets were built from"
                     % (cov.rows_total or 0)),
            "spec": spec,
            "engine": {"name": "duckdb", "source": hist.get("source"),
                       "predicate": "sql", "verified": None},
        }
        return _answer(result, _empty_from(sel), self._meta(snap))

    # -- 3.7 lookup --------------------------------------------------------

    def _v1_lookup(self, params, multi, now):
        snap = self._snap(now)
        d = self._duck()
        if isinstance(d, tuple):
            return d
        field = params.get("field") or "request_id"
        value = params.get("value")
        if value is None:
            return _refusal("id-not-a-string", "value=None",
                            "pass ?value=<the id>")
        uuid, bad = self._known_account_or_refusal(snap, params)
        if bad:
            return bad
        sel = d.lookup(field, value, account_uuid=uuid)
        if refused(sel):
            return _as_refusal(sel)
        rows = list(sel.rows)
        cov = (query.coverage_for(snap.report, uuid, rows)
               if uuid in snap.report.accounts
               else query.no_report_coverage(rows))
        result = {
            "field": field, "value": value, "rows": rows, "n": len(rows),
            "coverage": coverage_payload(cov, "the row(s) this id matched"),
            "notes": list(sel.notes or ()),
            "engine": {"name": "duckdb", "source": sel.source,
                       "predicate": "sql", "verified": None},
        }
        empty = _empty_from(sel)
        if empty is not None and empty["kind"] == OUTCOME_FILTERED:
            empty["message"] = (
                "no row carries %s=%s. That is not the same as a request with "
                "no cost: this store may never have received it, the shipper "
                "may not have reached it yet, or the id may be from another "
                "account." % (field, value))
            empty["remedy"] = ("check the id, or GET " + PATH_V1
                               + "health to see whether this store is current")
        return _answer(result, empty, self._meta(snap))

    # -- 3.8 accounts ------------------------------------------------------

    def _v1_accounts(self, params, multi, now):
        snap = self._snap(now)
        accounts = []
        for uuid in snap.accounts:
            acc = snap.report.accounts[uuid]
            # `include_by=False`: the accounts list is a summary, and a
            # per-tag breakdown of every window on the landing payload is
            # a different question asked at `/api/v1/window`.
            v = query.windows_view(snap.report, uuid, include_by=False)
            rows = v["windows"] if not refused(v) else []
            last = _last_sample(snap, uuid)
            accounts.append({
                "account_uuid": uuid,
                "identity": identity_of(snap, uuid),
                "requests_n": acc.requests_n,
                "samples_n": acc.samples_n,
                "attestations_n": acc.attestations_n,
                "known_hosts": sorted(acc.known_hosts or ()),
                "last_sample_ts": last,
                "last_sample_age_s": (snap.now - last) if last else None,
                # SEPARATE KEYS, so a client cannot silently fall back from one
                # to the other and print a window that closed hours ago as the
                # live one.
                "open": {k: _pick(rows, k, False) for k in window.KINDS},
                "last_closed": {k: _pick(rows, k, True) for k in window.KINDS},
                "windows_n": {k: len([w for w in rows if w["kind"] == k])
                              for k in window.KINDS},
                "gaps_n": len(acc.gaps or ()),
                "unclaimed_n": {k: len(x)
                                for k, x in (acc.unclaimed or {}).items()},
                "undatable_n": len(acc.undatable or ()),
                "rate": dict(acc.rate or {}),
                "rate_basis": dict(acc.rate_basis or {}),
                "rate_pinned_by": {k: list(x) if x else None for k, x
                                   in (acc.rate_pinned_by or {}).items()},
                "notes": list(acc.notes or ()),
            })
        result = {
            "accounts": accounts,
            "stale_after_s": STALE_AFTER_S,
            "stale_note": "a display threshold, not a measurement: nothing in "
                          "any capture says how long a machine may be quiet "
                          "before its last reading stops describing the "
                          "present. The age is printed either way.",
            "no_total_note": NO_TOTAL_NOTE,
            "peak_note": "every plan figure is an OBSERVED PEAK -- the max "
                         "over the samples that happened to be seen, from "
                         "snapshots that can be hours old. It is a lower "
                         "bound on what the window reached, never a reading.",
        }
        empty = None
        if not accounts:
            empty = empty_statement(
                OUTCOME_NO_DATA, 0, 0, {}, None,
                "this store holds no account directory. Nothing has ever been "
                "shipped to this door.",
                "check the shipper's ship_url and ship_token, then GET "
                + PATH_V1 + "health")
        return _answer(result, empty, self._meta(snap))

    # -- windows -----------------------------------------------------------

    def _v1_windows(self, params, multi, now):
        snap = self._snap(now)
        bad = self._account_or_refusal(snap, params)
        if bad:
            return bad
        uuid = params["account"]
        kind = params.get("kind") or None
        v = query.windows_view(snap.report, uuid, kind=kind,
                               include_by=False)
        if refused(v):
            return _as_refusal(v)
        acc = snap.report.accounts[uuid]
        rows = [r for r in snap.rows(uuid)]
        cov = query.coverage_for(snap.report, uuid, rows)
        result = dict(v)
        # Every window row on this route goes through the same wire shape
        # `/window` uses, so `windows[].coverage` and `window.coverage` are one
        # object with one contract rather than two that happen to agree.
        result["windows"] = [_wire_window(w) for w in v["windows"]]
        result["identity"] = identity_of(snap, uuid)
        result["rate_pinned_by"] = {k: list(x) if x else None for k, x
                                    in (acc.rate_pinned_by or {}).items()}
        result["provisional_note"] = attribute.PROVISIONAL_NOTE
        result["kinds"] = sorted(window.KINDS)
        result["coverage"] = coverage_payload(
            cov, "every row this account holds, not one window")
        result["bounds_note"] = {
            "5h": "confirmed: consecutive five_hour_resets_at values differ by "
                  "exactly 18000 in the capture",
            "7d": "ASSUMED: no 7-day rollover appears in any capture, so a "
                  "7-day window's length -- and therefore its start, its "
                  "coverage fraction and its request set -- is assumed, not "
                  "measured",
        }
        result["lower_bound_note"] = LOWER_BOUND_NOTE
        empty = None
        if not v.get("windows"):
            empty = empty_statement(
                OUTCOME_NO_DATA, 0, 0, {}, None,
                "no window of this kind exists for %s: windows are anchored "
                "to observed activity, so an account with no samples has "
                "none." % uuid,
                "ship a sample, or widen `kind`")
        return _answer(result, empty, self._meta(snap))

    def _v1_window(self, params, multi, now):
        snap = self._snap(now)
        bad = self._account_or_refusal(snap, params)
        if bad:
            return bad
        uuid = params["account"]
        kind = params.get("kind")
        reset, err = _num_param(params, "resets_at")
        if err:
            return err
        if kind not in window.KINDS or reset is None:
            return _refusal("no-window-named",
                            "kind=%r resets_at=%r"
                            % (kind, params.get("resets_at")),
                            "a window is identified by its kind and its "
                            "resets_at and by nothing else")
        v = query.windows_view(snap.report, uuid, kind=kind, include_by=True)
        if refused(v):
            return _as_refusal(v)
        row = None
        for w in v["windows"]:
            if w["resets_at"] == int(reset):
                row = _wire_window(w)
        if row is None:
            return _refusal(
                "unknown-window", "%s %s" % (kind, int(reset)),
                "windows of this kind here: "
                + (", ".join(str(w["resets_at"]) for w in v["windows"])
                   or "(none)"), status=404)

        # The window's own figures come from the reconciler and need no
        # engine.  Only the REQUEST LIST inside it is stream A, so a missing
        # DuckDB costs that list and not the whole window -- and it is reported
        # as a refusal object with NO `rows` key, exactly as the top-level
        # envelope does it, so a client iterating `requests.rows` raises rather
        # than rendering "no requests in this window" over a window full of
        # them.
        d = self._duck()
        requests_unavailable = None
        inside, sel = [], None
        if isinstance(d, tuple):
            requests_unavailable = d[1]["refusal"]
        else:
            q = query.compile_query({"account_uuid": uuid,
                                     "since": row["start"],
                                     "until": row["end"], "order": "asc"})
            if refused(q):
                return _as_refusal(q)
            sel = d.select(q, account_uuid=uuid)
            if refused(sel):
                return _as_refusal(sel)
            inside = sorted(sel.rows, key=lambda r: r.get("ts") or 0)
        cov = query.coverage_for(snap.report, uuid, inside)

        reset_key = window.KINDS[kind][1]
        samples = [{"record": rec, "shipping_host": host}
                   for rec, host in snap.samples(uuid)
                   if rec.get(reset_key) == int(reset)]
        samples.sort(key=lambda s: s["record"].get("ts") or 0)

        result = {
            "account_uuid": uuid,
            "identity": identity_of(snap, uuid),
            "window": row,
            "rate_pinned_by": {
                k: list(x) if x else None for k, x
                in (snap.report.accounts[uuid].rate_pinned_by or {}).items()},
            "requests": {
                "unavailable": requests_unavailable,
                "note": "the request list inside this window could not be "
                        "read; the window's own figures above come from the "
                        "reconciler and are unaffected. There is deliberately "
                        "no `rows` key: an empty list here would read as a "
                        "window with no requests in it.",
            } if requests_unavailable else {
                "rows": inside[:WINDOW_ROWS_CAP], "n": len(inside),
                "shown": len(inside[:WINDOW_ROWS_CAP]), "cap": WINDOW_ROWS_CAP,
                "note": "requests are placed by their own ts, half-open on "
                        "[start, resets_at). A machine whose clock is behind "
                        "files its requests into a neighbouring window; this "
                        "window's skew field names any host whose samples "
                        "proved one.",
            },
            "samples": {
                "rows": samples[:WINDOW_ROWS_CAP], "n": len(samples),
                "shown": len(samples[:WINDOW_ROWS_CAP]), "cap": WINDOW_ROWS_CAP,
                "note": "samples are placed by the resets_at they NAME, never "
                        "by their ts: a sample carries a snapshot cached "
                        "inside the claude process and it can be hours old -- "
                        "one real record names a window that had already "
                        "closed 9 h 49 m earlier.",
            },
            "coverage": coverage_payload(cov, "the rows inside this window"),
            "provisional_note": attribute.PROVISIONAL_NOTE,
            "lower_bound_note": LOWER_BOUND_NOTE,
        }
        return _answer(result,
                       _empty_from(sel) if sel is not None else None,
                       self._meta(snap))

    # -- diagnostics -------------------------------------------------------

    def _v1_diagnostics(self, params, multi, now):
        snap = self._snap(now)
        by_reason = {}
        for r in (snap.report.refusals or []):
            k = "%s/%s" % (r.stream, r.reason)
            by_reason[k] = by_reason.get(k, 0) + 1
        unknown = None
        counts = None
        acct, bad = self._known_account_or_refusal(snap, params)
        if bad:
            return bad
        coerced = None
        if self.duck_store is not None and store.available():
            try:
                unknown = self.duck_store.unknown_keys(acct)
                counts = self.duck_store.counts(acct)
                # The other half of pinning's silence, counted here for
                # `unknown_keys`' reason and never on the query path.
                coerced = self.duck_store.coerced_values(acct)
            except store.DuckDBMissing:
                unknown = None
        result = {
            "schemas": snap.report.schemas,
            "absent_fields": snap.report.absent_fields,
            "counts": dict(snap.report.counts or {}),
            "refusals_by_reason": by_reason,
            "refusals": [{"stream": r.stream, "reason": r.reason,
                          "detail": r.detail, "record": r.record}
                         for r in (snap.report.refusals or [])],
            "hosts": {u: sorted(h) for u, h in (snap.ing.hosts or {}).items()},
            "accounts": [{
                "account_uuid": u,
                "requests_n": snap.report.accounts[u].requests_n,
                "samples_n": snap.report.accounts[u].samples_n,
                "attestations_n": snap.report.accounts[u].attestations_n,
                "unclaimed_n": {k: len(x) for k, x in
                                (snap.report.accounts[u].unclaimed
                                 or {}).items()},
                "undatable_n": len(snap.report.accounts[u].undatable or ()),
                "notes": list(snap.report.accounts[u].notes or ()),
                "window_refusals": [[k, why] for k, why, _rec in
                                    (snap.report.accounts[u].refusals or ())],
            } for u in snap.accounts],
            "door": self.counters.snapshot() if self.counters else None,
            "engine": {"name": "duckdb", "available": store.available(),
                       "version": _duck_version(),
                       "unknown_keys": unknown, "row_counts": counts,
                       "coerced_values": coerced},
            # Conditional on the store, never a standing claim about the
            # world: the unconditional form once said "nothing emits an
            # attestation" on a payload whose own `attestations_accepted` read
            # 3 -- a sentence about the system contradicting the record it was
            # printed beside.
            "attestation_note": (
                "nothing in this repository emits an attestation, so every "
                "coverage fraction in this store is `None` -- nobody said. "
                "That is not 0.0, which would be the claim that nobody was "
                "listening."
                if not (snap.report.counts or {}).get("attestations_accepted")
                else None),
        }
        return _ok(result, self._meta(snap))

    # -- health ------------------------------------------------------------

    def _v1_health(self, params, multi, now):
        """The enveloped health view.  `/healthz` keeps its own bare shape."""
        snap = self._snap(now)
        result = {
            "accounts": len(snap.accounts),
            "batches": snap.batches_n,
            "store_problems": list(snap.problems or ()),
            "engine": {"name": "duckdb", "available": store.available(),
                       "version": _duck_version(),
                       "install_hint": None if store.available()
                                       else store.INSTALL_HINT},
            "derived_ms": snap.derived_ms,
            "note": "`/healthz` is the unenveloped, unversioned shape "
                    "operators and scripts read; this is the same question "
                    "asked through the API envelope.",
        }
        return _ok(result, self._meta(snap))


# ---------------------------------------------------------------------------
# 6. helpers
# ---------------------------------------------------------------------------

DOOR_VERSION_HINT = 1

# DERIVED from `ROUTE_PARAMS`, never listed a second time.  It was listed
# twice, and the two copies had already drifted apart on four routes: the
# published list omitted `limit` and `order` on `/aggregate`, and `account`,
# `limit`, `order` and `text` on `/aggregate/per-account` -- so a client
# building itself from `/capabilities` was told less than the server accepts,
# while the server enforced more than it published.  Now that an undeclared
# parameter is REFUSED, the two disagreeing is no longer cosmetic: one of them
# decides whether a request works.
ENDPOINT_PARAMS = {
    PATH_V1 + route: (sorted(params)
                      + (["where.<field>", "not.<field>"]
                         if route in SPEC_ROUTES else []))
    for route, params in ROUTE_PARAMS.items()
}


def _merge_problems(*lists):
    """One array, one shape, one entry per distinct problem.

    The two readers see the same file from different ends -- the reconciler
    reads the byte ranges the door acknowledged, the storage layer reads the
    whole file -- so one tear can be reported twice.  A problem ABOUT A FILE
    is deduped on `(reason, account_uuid, file)` with the LARGEST count kept,
    because two observations of one tear are one tear and adding them up is
    how an envelope came to claim 3118 unparseable lines over a store holding
    3.

    A problem about anything else -- an interrupted batch, an unparseable
    manifest -- carries no `file` and passes through untouched.  Two of those
    for one account are two events, and collapsing them would be this same
    defect pointed the other way.
    """
    out, seen = [], {}
    for lst in lists:
        for p in (lst or ()):
            if not isinstance(p, dict) or not p.get("file"):
                out.append(p)
                continue
            key = (p.get("reason"), p.get("account_uuid"), p.get("file"))
            if key in seen:
                held = seen[key]
                if (p.get("count") or 0) > (held.get("count") or 0):
                    held["count"] = p.get("count")
                continue
            copy = dict(p)
            seen[key] = copy
            out.append(copy)
    return out


def _duck_version():
    """The engine's version, or None.  Never raises: this is a description."""
    try:
        return store.require().__version__
    except Exception:                                        # noqa: BLE001
        return None


def bucketise(mapping):
    """`{value: stats}` -> `[{value, value_is_null, stats}]`.

    Two reasons, and the second is a bug this shape removes by construction.

    A request that carried no value for a dimension lands in the `None`
    bucket -- deliberately, because dropping it or spreading it over the other
    buckets both move weight onto a label the request does not have.  But a
    dict keyed by `str` and `None` together CANNOT be serialised by the door's
    own `json.dumps(..., sort_keys=True)`: ordering a `str` against a `None`
    raises, so the response became a 500 with the store perfectly healthy.  It
    was latent on every payload carrying `by_tag` and no test could see it,
    because the tests called the API in-process and never serialised what came
    back.

    So the wire shape is a LIST, exactly as `aggregate`'s buckets are, and the
    null bucket keeps its own flag instead of being stringified into a label
    indistinguishable from a real value called "None".
    """
    if not isinstance(mapping, dict):
        return mapping
    out = []
    for k, v in mapping.items():
        out.append({"value": k, "value_is_null": k is None,
                    "stats": bucketise(v) if _is_bucket_map(v) else v})
    out.sort(key=lambda d: (d["value_is_null"], str(d["value"])))
    return out


def _is_bucket_map(v):
    """A nested `{value: stats}`, which is what `by[dimension]` holds."""
    return isinstance(v, dict) and any(k is None for k in v)


def _wire_window(row):
    """One `windows_view` row, with its by-maps put into wire shape.

    Nothing else is touched: the endpoint serves the query layer's own row and
    a test compares the two field for field, because a reader that quietly
    reshapes a number is a second opinion about it.
    """
    if not isinstance(row, dict):
        return row
    out = dict(row)
    for key in ("by", "by_tag"):
        if isinstance(out.get(key), dict):
            out[key] = {dim: bucketise(vals)
                        for dim, vals in out[key].items()}
    # The one number on this row that must not be readable as zero when it is
    # unknown -- and it sits beside `attributed_pp` and `residual_pp`.
    out["coverage"] = window_coverage_payload(out.get("coverage"))
    return out


def _role_of(col):
    if col in query.ID_FIELDS:
        return "id"
    if col in query.STRUCTURED_FIELDS:
        return "structure"
    if col in query.OBSERVED_NUMERIC:
        return "measurement"
    return "label"


def _is_groupable(col):
    if col == "request_id":
        return False
    if col in query.OBSERVED_NUMERIC:
        return False
    return True


def _bucket_span(buckets, interval, since, until):
    """(lo, hi) aligned to epoch multiples, or (None, None) for nothing to fill.

    The requested range wins where it was given, and the DATA bounds it where
    it was not -- there is nothing to fill outside the data, and inventing a
    range would be the server asserting a period nobody asked about.  Split out
    from the fill so the bucket count can be checked BEFORE any list is built.
    """
    got = [int(b["bucket"]) for b in buckets]
    if since is None and not got:
        return None, None
    lo = (int(since // interval * interval) if since is not None
          else min(got))
    if until is not None:
        hi = int((until - 1) // interval * interval)
    elif got:
        hi = max(got)
    else:
        hi = lo
    if hi < lo:
        return None, None
    return lo, hi


def _zero_fill(buckets, interval, lo, hi, metric):
    """Emit every bucket in the span, including the empty ones.

    A missing bucket and a zero bucket are different claims about the same
    minute, and a chart draws them identically only if the server withholds
    one.  The span is decided and CAPPED by the caller; this only fills it.
    """
    if lo is None:
        return []
    # A structural bound, not the policy cap.  The caller has already refused
    # anything over `MAX_BUCKETS`; if this fires, that check has been bypassed
    # and the honest outcome is a named fault rather than either a hung request
    # or a silently shortened chart.  A truncated series is the worse of the
    # three: it draws a complete-looking picture of part of the range.
    n = (hi - lo) // interval + 1
    if n > MAX_BUCKETS:
        raise ValueError(
            "refusing to fill %d buckets; the cap is %d and it should have "
            "been enforced before this call" % (n, MAX_BUCKETS))
    got = {int(b["bucket"]): b for b in buckets}
    out = []
    b = lo
    while b <= hi:
        have = got.get(b)
        out.append(have if have else
                   {"bucket": b, "rows": 0, "value": 0, "metric": metric})
        b += interval
    return out


def identity_of(snap, uuid):
    """The names an account goes by, and the warning that they are names."""
    latest, out = None, {"account": None, "email": None,
                         "rate_limit_tier": None, "organization_uuid": None}
    for rec, _host in snap.samples(uuid):
        ts = rec.get("ts")
        if not wire._is_number(ts):
            continue
        if latest is None or ts > latest:
            latest = ts
            for k, src in (("account", "account"), ("email", "account_email"),
                           ("rate_limit_tier", "rate_limit_tier"),
                           ("organization_uuid", "organization_uuid")):
                v = rec.get(src)
                if v is not None:
                    out[k] = v
    if out["account"] is None:
        for r in snap.rows(uuid):
            if r.get("account"):
                out["account"] = r["account"]
                break
    if out["email"] is None:
        for r in snap.rows(uuid):
            if r.get("email"):
                out["email"] = r["email"]
                break
    out["from_sample_ts"] = latest
    out["note"] = ("`account` is a label claudio's --tag account= can "
                   "overwrite and `email` survives an orphaned login; the "
                   "account_uuid is the identity. rate_limit_tier names the "
                   "denominator -- two accounts on different tiers have "
                   "percentages that are not comparable.")
    return out


def _last_sample(snap, uuid):
    tss = [rec.get("ts") for rec, _h in snap.samples(uuid)
           if wire._is_number(rec.get("ts"))]
    return max(tss) if tss else None


def _pick(rows, kind, closed):
    """The newest window of one kind, open or closed.  None if there is none."""
    cands = [w for w in rows if w["kind"] == kind
             and (w["state"] in ("closed", "settled")) == closed]
    if not cands:
        return None
    return _wire_window(sorted(cands, key=lambda w: w["resets_at"])[-1])
