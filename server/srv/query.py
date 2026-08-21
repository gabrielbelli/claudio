"""The query layer: filters, breakdowns, windows -- and what it refuses.

Pure, like everything beside it except the door.  Records in, answers out; no
file, no socket, no clock, `now` an argument.  That is what makes every
function here testable without a socket and re-derivable from byte zero, and it
is what keeps the storage decision open -- see `narrowing()` and `plan()` at
the foot of this file for the seam an index plugs into without any line above
it learning that one exists.

Four rules carry this module, and each of them is a defect this project has
already paid for.

**A question that cannot be answered gets a REFUSAL, never an empty list.**
`Unanswerable` is a value, not an exception, and it names a reason and a
remedy.  Three answers that look identical in a table are kept apart by
construction and by name:

    no-data              nothing was scanned; the corpus is empty
    filtered-to-nothing  rows were scanned and every one was eliminated -- and
                         the result names WHICH clause did it
    Unanswerable         the question is not one this data can answer

`show --by` today takes an unknown key and groups everything under `(none)`,
which is the third answer wearing the second one's face: a breakdown by a
column that does not exist prints a tidy single-row table.  Here an unknown
column is `unknown-column` with the list of real ones.

**No facet list is ever hardcoded.**  `facets()` derives its values from the
rows in front of it, and `field_availability()` names every column that is
null in every row scanned.  Six `query_source` names Claude Code has never sent
and one `agent_summary` that appears in no payload both survived in this
repository by *reading as coverage*: a list that matches nothing looks exactly
like a list that matches everything until someone counts.  A facet built on
`mcp_tool` renders an empty list that reads as "I use no MCP tools" rather than
"this attribute has never been observed", so the difference is a field on the
result rather than an inference for the reader.

**Never sum across accounts.**  `breakdown` takes rows for ONE account and
refuses otherwise, the same way `window.reconstruct` raises when handed a
sample belonging to somebody else.  There is no cross-account total anywhere in
this file, not even for tokens: a percentage of one plan and a percentage of
another have different denominators, and a rule with an exception in it
("percentages no, tokens yes") is a rule that gets misapplied.  Grouping by
`account` or `email` across accounts is refused separately and by its own name,
because those are labels -- `--tag account=` overwrites the first, and two
accounts sharing a label would silently become one bucket.  `account_uuid` is
the identity; the label is what the user called it.

**Every aggregate carries its coverage.**  An attributed figure without
coverage is the confident-wrong-number failure: a window nobody watched and a
window with a browser session behind it produce the same residual, and only
coverage separates them.  So every non-refusal result here has a
`CoverageStatement`, and it is `fraction=None` -- "nobody said" -- rather than
0.0 whenever nothing has attested.  That is today's normal state: nothing in
this repository emits an attestation, so every real coverage fraction in
existence right now is None, and this module says so in a note rather than
printing a confident 0.

Two smaller things worth knowing before reading on.

`weight_of` is imported from `attribute` rather than `r.get("cost_usd_reported")
or 0.0` being written again, so a bucket's cost column and the attributed
figure computed from it are the same number arrived at once.  `show` uses the
`or` form, which differs on a negative cost (never observed) and does not count
the rows that carried none; the count is what matters, so it travels with the
sum here as `weightless`.

And there is no combined token column, in any bucket, ever.  Cache reads were
~89% of this user's tokens, so a figure that folds the four classes together is
wrong by about an order of magnitude for anybody with a warm cache.  A test
asserts no bucket key called `tokens` exists.
"""

import hashlib
import math

from . import attribute, wire, window
from .wire import ABSENT, get
from .wire import _is_number as _num

# ---------------------------------------------------------------------------
# 1. the columns, classified by EVIDENCE
# ---------------------------------------------------------------------------
#
# Not by what the field name suggests.  `agent_name`, `skill_name`,
# `plugin_name`, `mcp_server`, `mcp_tool`, `event_sequence` and `profile` are
# read by `cu.otlp.rows_from_payload` and are null in every one of the 8 real
# rows -- a grep over every fixture in this repository returns zero hits for
# each.  So their *type* is unknown here: `event_sequence` is
# `a.get("event.sequence")` straight off the payload and nothing has ever
# stated what arrives in it.  Classifying it as numeric because the name ends
# in "sequence" is precisely the assumption-written-as-a-value this project
# keeps apologising for, so it is classified as NEVER OBSERVED and the
# consequences are: no facet, no type-dependent operator, and an explicit line
# in `field_availability`.
#
# A test derives NEVER_OBSERVED from the real fixture and compares it to this
# tuple, so the day a capture carries an `agent_name` the constant has to be
# edited in the same commit as the fixture, where a reviewer sees it.

OBSERVED_TEXT = (
    "client_version", "account", "host", "email", "model", "model_raw",
    "query_source", "terminal_type", "source",
)

OBSERVED_NUMERIC = (
    "schema", "ts", "ts_ns", "input_tokens", "output_tokens",
    "cache_read_tokens", "cache_creation_tokens", "duration_ms",
    "cost_usd_reported", "ingested_at",
)

# High cardinality: lookup keys, not filters and never facets.  `request_id` is
# a 16-hex content hash and `session_id`/`prompt_id` are UUIDs -- a checkbox
# list of them is a list of one row each.  `prompt_id` is the exception worth
# naming: it is the natural GROUPING unit (one user turn, including every
# subagent request it spawned, ~2 requests per turn in the capture), so it is
# a legal `by` for `breakdown` while staying out of `facets`.
ID_FIELDS = ("request_id", "session_id", "prompt_id", "account_uuid")

# A dict of user-defined labels.  Addressed as `tags.<key>`; see TAG_PREFIX.
# Stream B's `tags` is a raw comma-separated STRING, not a dict -- the two are
# asymmetric and must not be treated as one type.  This module only ever reads
# stream A's.
STRUCTURED_FIELDS = ("tags",)

NEVER_OBSERVED = (
    "profile", "agent_name", "skill_name", "plugin_name", "mcp_server",
    "mcp_tool", "event_sequence",
)

LEDGER_COLUMNS = tuple(wire.LEDGER_FIELDS) + (wire.LEDGER_STAMP,)

# Free text runs over the observed text columns and the never-observed ones,
# with an `isinstance(str)` guard so an unknown type simply does not match.
# The never-observed half is included deliberately: a search that finds nothing
# there is a real answer, and the result says how many of the columns it
# searched have never carried a value, which is the difference between "the
# string is not in your data" and "that column has never been populated".
# IDs are NOT searchable -- a substring of a content hash is not a question
# anybody has, and offering it invites a full scan for no recall.
TEXT_SEARCHABLE = OBSERVED_TEXT + NEVER_OBSERVED

# What a facet list may be built from at all.  The values still come from the
# rows; this only bounds which columns are eligible.
FACETABLE = OBSERVED_TEXT + NEVER_OBSERVED

TAG_PREFIX = "tags."

# The four token classes, separate and always.  Named here so the breakdown
# cannot grow a fifth combined one by accident; a test asserts no bucket ever
# carries a key called `tokens`.
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens",
                "cache_creation_tokens")

# Repeated from `cu.config.OVERHEAD_SOURCES` rather than imported, exactly as
# `wire.ledger_fingerprint` is repeated from `cu.ledger.fingerprint`: `server/`
# stands alone with no path games, and a test imports the reader's copy and
# compares the two so the drift is a failure rather than a discovery.  A second
# test asserts every name here appears in the captured payloads -- the six
# invented names, and `agent_summary` after them, all read as coverage while
# matching nothing, and a deny-list is the one place where an unverified string
# costs the user money silently.
OVERHEAD_SOURCES = frozenset({"generate_session_title", "prompt_suggestion"})


def _classify(col):
    if col in OBSERVED_NUMERIC:
        return "numeric"
    if col in OBSERVED_TEXT:
        return "text"
    if col in ID_FIELDS:
        return "id"
    if col in STRUCTURED_FIELDS:
        return "structured"
    if col in NEVER_OBSERVED:
        return "never-observed"
    return None


# ---------------------------------------------------------------------------
# 2. the measurement
# ---------------------------------------------------------------------------
#
# Measured on this machine (M2 Max, Python 3.14.6, warm cache) over corpora
# grown from the real fixtures by `bench/gen_corpus.py`, linear in size to
# within 1.8% across a 620x range, so the rates extrapolate:
#
#   json.loads every line + this module's predicate   7.706 s/GiB
#
# Two figures here were superseded by that run and the old ones are named
# rather than quietly replaced, because both were quoted in prose elsewhere.
# The mean stream-A row is **789.3 bytes**, not 737: 737 is the row the CLIENT
# writes, and the row in the STORE carries `account_uuid`, which the shipper
# stamps on and which is 46 bytes more.  Five years is therefore 62 M rows and
# 48.9 GB / 45.6 GiB, not 45.7 GB, and the whole-file scan is **351 s**, not
# 284.  The old numbers were measuring the wrong end of the wire.
#
# The interactive ceiling that falls out is about 27k rows parsed -- two days
# of one heavy user's traffic at the central projected rate.
#
# Two consequences, and neither of them is "add an index by reflex":
#
#   * Nothing in this module scans anything.  It is handed rows.  What bounds
#     the scan is `narrowing()`, which states -- declaratively, for a storage
#     layer that may or may not exist yet -- the account, the time range and
#     the equality columns that can narrow the candidate set.  `srv/store.py`
#     is now one such layer and honours exactly that contract; a test runs
#     every query through both and requires the identical answer.
#   * `plan()` will REFUSE a query as `scan-budget-exceeded` when the caller
#     has asked for an interactive answer and the candidate set is too big for
#     one, naming the narrowing that would fix it.  It refuses only when a
#     budget is stated: a 12-second whole-history aggregate is a legitimate
#     thing to ask for, and a module that decides for the user which questions
#     are worth waiting for has replaced a measurement with a policy.
#
# `plan()` still estimates the PYTHON path, deliberately.  It is the cost of
# the engine this module is, and `srv/store.py` reports its own measured
# `derived_ms` beside every answer rather than being estimated from here --
# one estimate covering two engines is a number that is wrong for at least one
# of them and says which for neither.  DuckDB over the same 45.6 GiB is 8.1 s
# for every one of these queries and 0.07-0.2 s over a Parquet projection; see
# `server/README.md`, "The storage question, measured".
#
# Stream B needs none of this and it is worth saying so, because the temptation
# is to index both: the whole five-year stream-B corpus is 339k rows / 202 MiB.

SCAN_PARSE_S_PER_GIB = 7.706
# Unchanged: the prefilter strategy was not re-measured against the
# stamped row, and a number carried forward is said to be carried
# forward rather than presented as this run's.
SCAN_PREFILTER_S_PER_GIB = 2.171
LEDGER_MEAN_ROW_BYTES = 789.3
INTERACTIVE_S = 0.2
_GIB = float(1024 ** 3)

INTERACTIVE_ROWS_PARSE = int(
    (INTERACTIVE_S / SCAN_PARSE_S_PER_GIB) * _GIB / LEDGER_MEAN_ROW_BYTES)
INTERACTIVE_ROWS_PREFILTER = int(
    (INTERACTIVE_S / SCAN_PREFILTER_S_PER_GIB) * _GIB / LEDGER_MEAN_ROW_BYTES)

# The columns the measured pointer table carries: (ts, acct, model, qsrc, sess)
# plus a byte offset, with one composite index on (acct, ts).  It holds no row
# content, so a corrupt index can fail to FIND a record and can never lose one
# -- delete the file, rebuild in one pass.  `request_id` and `prompt_id` were
# measured at ~10% of the JSONL each if lookup by them is to be first-class.
INDEX_COLUMNS = ("account_uuid", "ts", "model", "query_source", "session_id")


# ---------------------------------------------------------------------------
# 3. the values this module returns
# ---------------------------------------------------------------------------

class Unanswerable(object):
    """A question this data cannot answer, as a VALUE rather than an exception.

    Distinct from `ingest.Refusal`, which is one record the door would not
    place.  This is one question the reader would not answer, and the two are
    different axes: a report can be full of placed records and still be unable
    to answer "group these three accounts by profile".

    `remedy` is not decoration.  Every reason here has an action behind it --
    name the account, widen the range, use the identity instead of the label --
    and a refusal that does not say what to do next gets read as a fault.
    """

    __slots__ = ("reason", "detail", "remedy")

    def __init__(self, reason, detail=None, remedy=None):
        self.reason = reason
        self.detail = detail
        self.remedy = remedy

    def __repr__(self):
        return "Unanswerable(%s: %s)" % (self.reason, self.detail)

    def as_dict(self):
        return {"refused": self.reason, "detail": self.detail,
                "remedy": self.remedy}


def refused(x):
    """True if a call returned a refusal rather than an answer."""
    return isinstance(x, Unanswerable)


class CoverageStatement(object):
    """What fraction of the account's activity an aggregate could see.

    Two independent halves, because two different things can be unknown.

    `fraction` is attestation coverage -- how much of the windows these rows
    fall in had ANY machine reporting.  It is None whenever nothing attested,
    or whenever some windows attested and others did not: a fraction computed
    over the half that spoke is a confident number about a set that includes
    windows nobody described.  `basis` names which of those it is.

    The `rows_*` half is placement, and it is always computable: of the rows in
    this aggregate, how many sit inside a closed window (attributable), an open
    one (not yet), no window at all (unclaimed -- windows are activity-anchored
    and the gaps between them belong to nothing), or carry no usable timestamp
    (undatable, and therefore in neither).  That is the honest denominator for
    any breakdown of requests, and it is what makes a `project` breakdown --
    which can only reach stream A through `session_id`, and only for sessions
    that produced a sample -- say what fraction of rows it could place instead
    of implying it placed them all.
    """

    __slots__ = ("fraction", "basis", "windows_n", "windows_closed_n",
                 "windows_with_coverage_n", "attestations_n",
                 "rows_total", "rows_in_closed_windows", "rows_in_open_windows",
                 "rows_unclaimed", "rows_undatable",
                 "hosts_reporting", "hosts_dark", "hosts_pending",
                 "hosts_silent", "spans_unreadable", "notes")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    @property
    def placed_fraction(self):
        """Rows this aggregate could place in a closed window, as a fraction.

        None when there are no rows (0/0 is not 0) and None when nothing was
        reconciled -- a statement with no report behind it has not placed the
        rows at all, and answering 1.0 there would be the confident wrong
        number this class exists to prevent.
        """
        if not self.rows_total or self.rows_in_closed_windows is None:
            return None
        return self.rows_in_closed_windows / float(self.rows_total)

    def as_dict(self):
        d = {s: getattr(self, s) for s in self.__slots__}
        for f in ("hosts_reporting", "hosts_dark", "hosts_pending",
                  "hosts_silent"):
            if isinstance(d.get(f), set):
                d[f] = sorted(d[f])
        d["placed_fraction"] = self.placed_fraction
        return d


class Selection(object):
    """The rows a query matched, and the accounting of the ones it did not.

    `eliminated` is per clause and it is the whole reason this is a class
    rather than a list comprehension.  "Filtered to nothing" is only useful if
    it says which filter did it -- an intersection of six facets that returns
    nothing is otherwise six equally plausible suspects, and the user's next
    move is to remove them one at a time.
    """

    # `no_data_note` is the ENGINE's own sentence for `no-data`, and it is a
    # field rather than `notes[0]` because `notes` is a list of caveats, not an
    # explanation: `page()` unconditionally appends the keyset-pagination note,
    # so mining the first note published "pages are keyed on (ts, request_id)"
    # as the reason a search was empty.  Set only where the engine really can
    # tell a MISSING ledger from a present but empty one; None everywhere else,
    # which is what makes the generic fallback reachable.
    __slots__ = ("rows", "undatable", "scanned", "matched", "eliminated",
                 "sole_cause", "empty_because", "clauses", "notes", "source",
                 "no_data_note")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def as_dict(self):
        d = {s: getattr(self, s) for s in self.__slots__}
        d["rows"] = len(self.rows or ())
        d["undatable"] = len(self.undatable or ())
        return d


class Page(object):
    """One page of rows, and everything that page cannot show.

    `next_cursor` is a keyset cursor over `(ts, request_id)`; see `paginate`
    for why an offset would be wrong here.
    """

    __slots__ = ("items", "next_cursor", "has_more", "order", "limit",
                 "matched", "scanned", "undatable_n", "empty_because",
                 "eliminated", "sole_cause", "coverage", "notes", "source",
                 "no_data_note")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def as_dict(self):
        d = {s: getattr(self, s) for s in self.__slots__}
        d["items"] = list(self.items or ())
        if hasattr(self.coverage, "as_dict"):
            d["coverage"] = self.coverage.as_dict()
        return d


class Breakdown(object):
    """One grouping of one account's rows, with its coverage beside it."""

    __slots__ = ("account_uuid", "by", "buckets", "order", "matched", "scanned",
                 "undatable_n", "empty_because", "eliminated", "sole_cause",
                 "overhead", "coverage", "column_status", "notes",
                 "no_data_note")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def rows_sorted(self, key="cost"):
        """Buckets as a list, biggest first, ties broken by the bucket label.

        `show` sorts by cost descending and this keeps that, because the two
        are meant to agree: the per-column token and cost table already exists
        and is correct, and re-deriving its semantics is how two figures for
        one quantity end up in one product.
        """
        def sk(item):
            return (-(item[1].get(key) or 0), str(item[0]))
        return sorted(self.buckets.items(), key=sk)

    def as_dict(self):
        d = {s: getattr(self, s) for s in self.__slots__}
        if hasattr(self.coverage, "as_dict"):
            d["coverage"] = self.coverage.as_dict()
        d["buckets"] = {repr(k): v for k, v in (self.buckets or {}).items()}
        return d


class Narrowing(object):
    """What a storage layer may use to narrow the candidate set.

    Advisory in exactly one direction.  A storage layer may return MORE rows
    than this describes -- the predicate is re-applied to every candidate, so
    extra rows cost time and change no answer.  It must never return fewer: a
    stale or partial index that omits a matching row produces a smaller number
    with no symptom, which is this project's cardinal sin arriving through the
    fast path.  That is the whole contract, and it is why an index here has to
    be rebuildable from the JSONL rather than trusted as the record of truth.

    `unsupported` names the clauses the index cannot serve, so a caller can see
    what it is paying for in post-filtering rather than guessing.
    """

    __slots__ = ("account_uuid", "ts_lo", "ts_hi", "equalities", "ids",
                 "unsupported")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def as_dict(self):
        d = {s: getattr(self, s) for s in self.__slots__}
        d["equalities"] = {k: sorted(v, key=repr)
                           for k, v in (self.equalities or {}).items()}
        return d


class Plan(object):
    """A query, its narrowing, and the honest cost of answering it."""

    __slots__ = ("query", "narrowing", "candidate_rows", "estimate_s",
                 "interactive", "basis", "notes")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def as_dict(self):
        d = {s: getattr(self, s) for s in self.__slots__}
        d["query"] = self.query.spec if self.query else None
        d["narrowing"] = self.narrowing.as_dict() if self.narrowing else None
        return d


class Query(object):
    """A compiled, validated query.  Build it with `compile_query`."""

    __slots__ = ("spec", "account_uuid", "since", "until", "where", "not_where",
                 "ranges", "text", "text_in", "order", "limit", "fingerprint")

    def __init__(self, **kw):
        for s in self.__slots__:
            setattr(self, s, kw.get(s))

    def __repr__(self):
        return "Query(%s)" % wire.canonical(self.spec)


# ---------------------------------------------------------------------------
# 4. compiling a query
# ---------------------------------------------------------------------------

_ORDERS = ("asc", "desc")

_SPEC_KEYS = ("account_uuid", "since", "until", "where", "not_where", "ranges",
              "text", "text_in", "order", "limit")


def _as_values(v):
    """One value or a list of them, normalised to a tuple.

    A tuple rather than a set: `None` and unhashable values are legal here
    (`where={"profile": None}` is "rows with no profile", a real question), and
    membership over a handful of facet values costs nothing.
    """
    if isinstance(v, (list, tuple, set, frozenset)):
        return tuple(v)
    return (v,)


def _known_column(col):
    """(ok, kind).  `tags.<key>` is a column too, and its key is user-defined."""
    if col.startswith(TAG_PREFIX):
        return len(col) > len(TAG_PREFIX), "tag"
    return _classify(col) is not None, _classify(col)


def compile_query(spec):
    """A spec dict -> `Query`, or `Unanswerable`.

    Everything that can be wrong with the question is decided here, once, so
    that `select`, `paginate` and `breakdown` never have to answer half a
    question.  An unknown column is the important one: `show --by nonsense`
    groups every row under `(none)` and prints a table, which is a refusal
    wearing an answer's clothes.
    """
    if not isinstance(spec, dict):
        return Unanswerable("spec-not-an-object", type(spec).__name__,
                            "pass a dict of query keys")
    unknown = [k for k in spec if k not in _SPEC_KEYS]
    if unknown:
        return Unanswerable(
            "unknown-query-key", ", ".join(sorted(unknown)),
            "known keys: " + ", ".join(_SPEC_KEYS))

    acct = spec.get("account_uuid")
    if acct is not None and (not isinstance(acct, str) or not acct):
        return Unanswerable("account-uuid-not-a-string", repr(acct),
                            "pass the account's UUID, or omit it")

    since, until = spec.get("since"), spec.get("until")
    for name, v in (("since", since), ("until", until)):
        if v is not None and not _num(v):
            return Unanswerable("range-not-a-number", "%s=%r" % (name, v),
                                "pass epoch seconds, or omit it")
        if v is not None and not _finite(v):
            return Unanswerable(
                "range-not-a-number", "%s=%r" % (name, v),
                "NaN is not a position in time: pass epoch seconds, or omit "
                "the bound to mean no bound")
    if since is not None and until is not None and until < since:
        return Unanswerable("range-inverted", "since=%r until=%r"
                            % (since, until),
                            "the range is half-open [since, until)")

    where = dict(spec.get("where") or {})
    not_where = dict(spec.get("not_where") or {})
    for src in (where, not_where):
        for col in src:
            if not isinstance(col, str):
                return Unanswerable("column-not-a-string", repr(col),
                                    "columns are strings")
            ok, _kind = _known_column(col)
            if not ok:
                return Unanswerable(
                    "unknown-column", col,
                    "stream A columns: %s; user tags: %s<key>"
                    % (", ".join(sorted(LEDGER_COLUMNS)), TAG_PREFIX))
        for col in src:
            src[col] = _as_values(src[col])

    ranges = {}
    for col, bounds in (spec.get("ranges") or {}).items():
        if col not in OBSERVED_NUMERIC:
            # Deliberately the observed-numeric set and not "any column": a
            # range over `event_sequence`, whose type nothing has ever
            # observed, is a comparison whose meaning is unknown, and a
            # comparison with an unknown meaning that returns rows is worse
            # than one that refuses.
            return Unanswerable(
                "range-on-a-non-numeric-column", col,
                "numeric columns: " + ", ".join(sorted(OBSERVED_NUMERIC)))
        if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2):
            return Unanswerable("range-not-a-pair", "%s=%r" % (col, bounds),
                                "pass (lo, hi); either may be None")
        lo, hi = bounds
        for b in (lo, hi):
            if b is not None and not _num(b):
                return Unanswerable("range-not-a-number",
                                    "%s=%r" % (col, bounds), "pass numbers")
            if b is not None and not _finite(b):
                # The mirror image of the bound above, and it diverges the
                # other way: Python's `v < lo` is false for NaN so the pure
                # path KEEPS every row, and DuckDB's ordering eliminates
                # every one.
                return Unanswerable(
                    "range-not-a-number", "%s=%r" % (col, bounds),
                    "NaN is not a bound: pass numbers, or None for no bound")
        if lo is not None and hi is not None and hi < lo:
            return Unanswerable("range-inverted", "%s=%r" % (col, bounds),
                                "ranges are half-open [lo, hi)")
        ranges[col] = (lo, hi)

    text = spec.get("text")
    if text is not None and not isinstance(text, str):
        return Unanswerable("text-not-a-string", repr(text),
                            "free text is a string")
    if text is not None and not text.strip():
        return Unanswerable(
            "text-is-empty", repr(text),
            "an empty search matches every row, which is not a search; omit it")
    text_in = spec.get("text_in")
    if text_in is not None:
        text_in = tuple(text_in)
        for col in text_in:
            if col in ID_FIELDS:
                return Unanswerable(
                    "text-search-on-an-id-column", col,
                    "ids are content hashes and UUIDs; look one up with "
                    "where={'%s': '<value>'}" % col)
            if col in OBSERVED_NUMERIC or col in STRUCTURED_FIELDS:
                return Unanswerable(
                    "text-search-on-a-non-text-column", col,
                    "searchable columns: " + ", ".join(sorted(TEXT_SEARCHABLE)))
            if col not in TEXT_SEARCHABLE:
                return Unanswerable(
                    "unknown-column", col,
                    "searchable columns: " + ", ".join(sorted(TEXT_SEARCHABLE)))
    order = spec.get("order", "desc")
    if order not in _ORDERS:
        return Unanswerable("unknown-order", repr(order),
                            "order is 'asc' or 'desc'")
    limit = spec.get("limit")
    if limit is not None and (not isinstance(limit, int)
                              or isinstance(limit, bool) or limit <= 0):
        return Unanswerable("limit-not-a-positive-integer", repr(limit),
                            "pass a positive integer, or omit it")

    q = Query(spec=dict(spec), account_uuid=acct, since=since, until=until,
              where=where, not_where=not_where, ranges=ranges, text=text,
              text_in=text_in, order=order, limit=limit)
    q.fingerprint = _fingerprint(q)
    return q


def _finite(v):
    """A bound that has a position in an ordering.

    `float("nan")` parses, `wire._is_number` is true of it, and `until < since`
    is false for it, so NaN sailed through every check a bound had -- and then
    the two engines ordered it OPPOSITELY.  Python's `ts <= nan` is False for
    every row, DuckDB's total float ordering puts NaN above infinity so
    `ts < nan` is TRUE for every row: over the real fixtures `until=nan` gave
    the oracle 0 rows and the storage layer all of them, `outcome: ok`, no
    note.  `String(NaN)` in JavaScript is `"NaN"`, so a date field that fails
    to parse in a front end sends exactly that.

    +/-inf is NOT refused here: those are ordinary "no bound" spellings, they
    order identically in both engines, and the pair is pinned by
    `test_duck_an_infinite_time_bound_is_an_answer_not_a_crash`.  NaN is not a
    bound at all.
    """
    return not (isinstance(v, float) and math.isnan(v))


def _fingerprint(q):
    """A stable hash of everything that changes which rows match.

    `limit` is excluded on purpose -- changing the page size must not
    invalidate a cursor -- and `order` is included, because a cursor is a
    position in an ordering and reversing it makes the same position mean the
    opposite thing.
    """
    body = {
        "account_uuid": q.account_uuid, "since": q.since, "until": q.until,
        "where": {k: [repr(v) for v in vs] for k, vs in sorted(q.where.items())},
        "not_where": {k: [repr(v) for v in vs]
                      for k, vs in sorted(q.not_where.items())},
        "ranges": {k: list(v) for k, v in sorted(q.ranges.items())},
        "text": q.text, "text_in": list(q.text_in or ()), "order": q.order,
    }
    return hashlib.sha256(wire.canonical(body).encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# 5. matching
# ---------------------------------------------------------------------------

def cell(row, col):
    """The value of a column, or ABSENT.  `tags.<key>` reads the tag dict.

    ABSENT rather than None throughout, for `wire.get`'s reason one layer up: a
    column added after a row was written and a column the row explicitly wrote
    null into are different facts, and once a reader has written None for both
    no later pass can separate them.
    """
    if col.startswith(TAG_PREFIX):
        tags = row.get("tags")
        if not isinstance(tags, dict):
            return ABSENT
        return get(tags, col[len(TAG_PREFIX):])
    return get(row, col)


def _eq(row, col, wanted):
    """Equality against one of a set of values.

    `None` in `wanted` means "this row has no value here", and it matches both
    a null and an absent column -- for a query that is one question ("rows with
    no profile"), and the two are told apart by `field_availability` rather
    than by making every caller write both.

    `True == 1` in Python, so a boolean is compared by type as well; nothing in
    the capture carries one, and a silent `duration_ms == True` match is the
    kind of thing that is found years later.
    """
    v = cell(row, col)
    for w in wanted:
        if w is None:
            if v is ABSENT or v is None:
                return True
            continue
        if v is ABSENT or v is None:
            continue
        if isinstance(v, bool) != isinstance(w, bool):
            continue
        if v == w:
            return True
    return False


def _in_range(row, col, lo, hi):
    v = cell(row, col)
    if not _num(v):
        return False
    if lo is not None and v < lo:
        return False
    if hi is not None and v >= hi:
        return False
    return True


def _text_hit(row, needle, cols):
    for col in cols:
        v = cell(row, col)
        if isinstance(v, str) and needle in v.lower():
            return True
    return False


def clauses_of(q):
    """[(label, predicate)] -- one per thing the user asked for.

    Kept apart rather than folded into a single lambda so that
    `filtered-to-nothing` can name the clause that emptied the result.  The
    cost is that every clause is evaluated for every scanned row even after one
    has failed; that is deliberate, and it is the accounting, not an oversight.
    """
    out = []
    if q.account_uuid:
        out.append(("account_uuid=%s" % q.account_uuid,
                    lambda r, a=q.account_uuid: r.get("account_uuid") == a))
    if q.since is not None:
        out.append(("since=%r" % q.since,
                    lambda r, s=q.since: _num(r.get("ts")) and r["ts"] >= s))
    if q.until is not None:
        out.append(("until=%r" % q.until,
                    lambda r, u=q.until: _num(r.get("ts")) and r["ts"] < u))
    for col in sorted(q.where):
        vals = q.where[col]
        out.append(("%s in %s" % (col, list(vals)),
                    lambda r, c=col, v=vals: _eq(r, c, v)))
    for col in sorted(q.not_where):
        vals = q.not_where[col]
        out.append(("%s not in %s" % (col, list(vals)),
                    lambda r, c=col, v=vals: not _eq(r, c, v)))
    for col in sorted(q.ranges):
        lo, hi = q.ranges[col]
        out.append(("%s in [%r, %r)" % (col, lo, hi),
                    lambda r, c=col, a=lo, b=hi: _in_range(r, c, a, b)))
    if q.text:
        cols = tuple(q.text_in or TEXT_SEARCHABLE)
        out.append(("text=%r in %d column(s)" % (q.text, len(cols)),
                    lambda r, n=q.text.lower(), c=cols: _text_hit(r, n, c)))
    return out


def match(q, row):
    """Does one row satisfy the whole query?"""
    return all(fn(row) for _label, fn in clauses_of(q))


def select(q, rows, source="caller-supplied"):
    """Apply a query to rows.  Never raises, never returns a bare list.

    `source` is whatever the storage layer says it did -- "full-scan",
    "index:(account_uuid,ts)" -- and it is carried through to the result
    untouched.  A figure that came out of an index and one that came out of a
    scan are the same number only if the index was complete, so every answer
    says which it was and the audit is possible at all.
    """
    cl = clauses_of(q)
    kept, undatable = [], []
    scanned = 0
    eliminated = {label: 0 for label, _fn in cl}
    for row in rows:
        if not isinstance(row, dict):
            continue
        scanned += 1
        ok = True
        for label, fn in cl:
            if not fn(row):
                eliminated[label] += 1
                ok = False
        if ok:
            kept.append(row)
            if not _num(row.get("ts")):
                undatable.append(row)

    # Guarded on `scanned`, and the guard is not decoration: with an empty
    # corpus every clause has eliminated 0 of 0 rows, so an unguarded
    # `n == scanned` blames every clause for a corpus that was never there --
    # which is exactly the "no-data" and "filtered-to-nothing" confusion this
    # accounting exists to prevent, produced by the accounting itself.
    sole = ([label for label, n in eliminated.items() if n == scanned]
            if scanned and not kept else [])
    empty = None
    if scanned == 0:
        empty = "no-data"
    elif not kept:
        empty = "filtered-to-nothing"

    notes = []
    if undatable:
        # The same third bucket `reconcile` keeps: a row with no usable `ts` is
        # in no window and in no unclaimed list either, so its weight sits in
        # the residual.  It matched the query and is returned; what it cannot
        # do is take a position in a time ordering, so it never appears in a
        # page and is counted on every one.
        notes.append(
            "%d matching row(s) carry no usable timestamp: they are in this "
            "selection, in no window of either kind, and in no page -- an "
            "ordering by time cannot place them" % len(undatable))
    if empty == "filtered-to-nothing" and len(sole) == 1:
        notes.append("every one of the %d scanned row(s) was eliminated by a "
                     "single clause: %s" % (scanned, sole[0]))
    elif empty == "filtered-to-nothing":
        notes.append("no single clause eliminated everything; the intersection "
                     "of %d clause(s) is empty" % len(cl))

    return Selection(rows=kept, undatable=undatable, scanned=scanned,
                     matched=len(kept), eliminated=eliminated,
                     sole_cause=(sole[0] if len(sole) == 1 else None),
                     empty_because=empty, clauses=[c for c, _ in cl],
                     notes=notes, source=source)


def lookup(rows, field, value, source="caller-supplied"):
    """One id -> the rows carrying it.  `request_id`, `session_id`, `prompt_id`.

    Separate from `select` because an id lookup is a different question with a
    different failure: not finding a `request_id` means the id is wrong or the
    row was never shipped, and the answer must not read as "this request cost
    nothing".  `prompt_id` returns a turn -- one user message and every
    subagent request it spawned, ~2 requests per turn in the capture.
    """
    if field not in ID_FIELDS:
        return Unanswerable("not-an-id-column", field,
                            "id columns: " + ", ".join(ID_FIELDS))
    if not isinstance(value, str) or not value:
        return Unanswerable("id-not-a-string", repr(value),
                            "pass the id as a string")
    q = compile_query({"where": {field: value}, "order": "asc"})
    if refused(q):
        return q
    sel = select(q, rows, source=source)
    if sel.empty_because == "filtered-to-nothing":
        sel.notes.append(
            "no row carries %s=%s. That is not the same as a request with no "
            "cost: this store may never have received it, the shipper may not "
            "have reached it yet, or the id may be from another account."
            % (field, value))
    return sel


# ---------------------------------------------------------------------------
# 6. what the data actually contains
# ---------------------------------------------------------------------------

def field_availability(rows):
    """Per column: how many rows carried a value, and how many distinct ones.

    This is the antidote to the failure that has cost this project the most.  A
    facet list built from a column that has never been populated renders an
    empty control that reads as "I have never used an MCP tool", and a deny-list
    of names nothing ever sends reads as coverage.  Both are silent.  So the
    reader can always ask what is actually in front of it, and the answer
    distinguishes three states per column: values present, present-but-all-null,
    and absent from every row.
    """
    out = {}
    total = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        total += 1
        for col in LEDGER_COLUMNS:
            st = out.setdefault(col, {"present": 0, "null": 0, "absent": 0,
                                      "distinct": set()})
            v = get(row, col)
            if v is ABSENT:
                st["absent"] += 1
            elif v is None:
                st["null"] += 1
            else:
                st["present"] += 1
                try:
                    st["distinct"].add(v)
                except TypeError:
                    st["distinct"].add(repr(v))
        tags = row.get("tags")
        if isinstance(tags, dict):
            for k, v in tags.items():
                st = out.setdefault(TAG_PREFIX + k,
                                    {"present": 0, "null": 0, "absent": 0,
                                     "distinct": set()})
                st["present"] += 1
                st["distinct"].add(v)
    for col, st in out.items():
        st["distinct_n"] = len(st["distinct"])
        st["never_observed"] = st["present"] == 0
        del st["distinct"]
    return {"rows": total, "columns": out}


def facets(rows, columns=None, limit=None):
    """{column: [(value, count)]} -- derived from the rows, never from a list.

    Only columns that some row actually carried appear, and a column whose
    every value is null is omitted rather than rendered as an empty control.
    `column_status` on a `Breakdown` carries the same fact for the one column
    being grouped by.

    IDs are excluded structurally: `request_id` has one row per value, so a
    facet over it is a list as long as the corpus.
    """
    avail = field_availability(rows)["columns"]
    cols = columns
    if cols is None:
        # Every eligible column is considered and the DATA decides which
        # survive, one guard below.  Prefiltering on `avail` here as well would
        # make that guard redundant, and a redundant guard is one nobody can
        # tell is still working -- this is the property the mutation matrix
        # breaks, so it has to have exactly one place to break.
        cols = list(FACETABLE)
        cols += sorted(c for c in avail if c.startswith(TAG_PREFIX))
    out = {}
    for col in cols:
        if col in ID_FIELDS:
            continue
        counts = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            v = cell(row, col)
            if v is ABSENT or v is None:
                continue
            counts[v] = counts.get(v, 0) + 1
        if not counts:
            continue
        pairs = sorted(counts.items(), key=lambda kv: (-kv[1], repr(kv[0])))
        out[col] = pairs[:limit] if limit else pairs
    return out


# ---------------------------------------------------------------------------
# 7. pagination
# ---------------------------------------------------------------------------
#
# Keyset, over `(ts, request_id)`, and an offset would be wrong here for a
# reason particular to this system rather than the usual one.  The usual
# argument is that rows appended during paging shift every later offset; the
# sharper one is that this store accepts a WEEK-LATE SHIPMENT and re-derives
# every report from byte zero, so a row can be appended whose `ts` belongs in
# the middle of a page that was served yesterday.  Under an offset that shifts
# every subsequent row by one and the reader silently skips a real request.
#
# The keyset key is immutable: `ts` is the payload's own nanosecond clock as a
# float and `request_id` is a content hash of the row, so neither changes for a
# row that already exists, and a row is therefore never returned twice and
# never skipped -- with one honest exception, stated on every page rather than
# in a comment: a row that ARRIVES with a key behind the cursor was already
# passed and this pass will not show it.  That is a property of paging an
# out-of-order corpus, not a bug to be hidden, and the remedy is in the note.
#
# `matched` is recomputed on every page, so a caller that sees it change
# between pages knows the corpus moved underneath it.

_CURSOR_V = "c1"


def encode_cursor(q, row):
    """`c1|<query fingerprint>|<order>|<ts>|<request_id>`.

    The fingerprint is what stops a cursor from one query being applied to
    another.  Handing page 2's cursor to a differently-filtered query would
    otherwise silently skip whatever sorts before it -- an empty page that
    looks like the end of a result.
    """
    return "|".join((_CURSOR_V, q.fingerprint, q.order,
                     repr(float(row["ts"])), str(row.get("request_id") or "")))


def decode_cursor(q, cur):
    """(ts, request_id), or `Unanswerable`."""
    if not isinstance(cur, str):
        return Unanswerable("cursor-unreadable", repr(cur),
                            "pass the `next_cursor` from the previous page")
    parts = cur.split("|")
    if len(parts) != 5 or parts[0] != _CURSOR_V:
        return Unanswerable("cursor-unreadable", cur,
                            "pass the `next_cursor` from the previous page")
    _v, fp, order, ts, rid = parts
    if fp != q.fingerprint or order != q.order:
        return Unanswerable(
            "cursor-does-not-match-this-query",
            "cursor was issued for %s/%s, this query is %s/%s"
            % (fp, order, q.fingerprint, q.order),
            "start again without a cursor: a position in one ordering is not "
            "a position in another, and applying it would skip rows silently")
    try:
        pos = float(ts)
    except ValueError:
        return Unanswerable("cursor-unreadable", cur, "start again without one")
    if not math.isfinite(pos):
        # `encode_cursor` never issued one: a cursor position comes from a
        # row's own `ts`, which `paginate` has already required to be a real
        # number.  A hand-edited `nan` keeps the fingerprint intact and still
        # matches the query, and the two engines then compare it oppositely --
        # 3 items here, 0 there, on the same page, so a client paging with it
        # never terminates.
        return Unanswerable("cursor-unreadable", cur,
                            "start again without one: %r is not a position in "
                            "an ordering" % ts)
    return (pos, rid)


def _sort_key(row):
    return (row["ts"], str(row.get("request_id") or ""))


def paginate(q, rows, cursor=None, limit=None, coverage=None,
             source="caller-supplied"):
    """One page of matching rows, ordered and stable under appends.

    Pagination bounds the ANSWER, not the scan.  Every page here re-applies the
    predicate to everything it is handed, so a page is cheap in transfer and
    exactly as expensive in time as the candidate set is large -- which is what
    `narrowing()` and `plan()` exist to bound.  Saying so is the point: a
    `limit=50` that reads as "this only touched 50 rows" is how a query layer
    gets blamed for a storage decision.
    """
    if refused(q):
        return q
    lim = limit if limit is not None else (q.limit or 50)
    if not isinstance(lim, int) or isinstance(lim, bool) or lim <= 0:
        return Unanswerable("limit-not-a-positive-integer", repr(lim),
                            "pass a positive integer")
    after = None
    if cursor is not None:
        after = decode_cursor(q, cursor)
        if refused(after):
            return after

    sel = select(q, rows, source=source)
    datable = [r for r in sel.rows if _num(r.get("ts"))]
    datable.sort(key=_sort_key, reverse=(q.order == "desc"))
    if after is not None:
        if q.order == "desc":
            datable = [r for r in datable if _sort_key(r) < after]
        else:
            datable = [r for r in datable if _sort_key(r) > after]

    items = datable[:lim]
    has_more = len(datable) > lim
    notes = list(sel.notes)
    notes.append(
        "pages are keyed on (ts, request_id), which never change for a row, so "
        "a row is never served twice or skipped between pages. A row SHIPPED "
        "LATE whose ts sorts behind the cursor was already passed and will not "
        "appear in this pass -- re-run the query with an explicit since/until "
        "range to pick it up.")
    if cursor is not None and sel.empty_because is None and not datable:
        notes.append("this page is empty because the cursor is at the end of "
                     "the result, not because the query matched nothing")

    return Page(items=items,
                next_cursor=(encode_cursor(q, items[-1])
                             if items and has_more else None),
                has_more=has_more, order=q.order, limit=lim,
                matched=sel.matched, scanned=sel.scanned,
                undatable_n=len(sel.undatable),
                empty_because=sel.empty_because, eliminated=sel.eliminated,
                sole_cause=sel.sole_cause,
                coverage=coverage if coverage is not None
                else no_report_coverage(sel.rows),
                notes=notes, source=source)


# ---------------------------------------------------------------------------
# 8. coverage for an arbitrary set of rows
# ---------------------------------------------------------------------------

def no_report_coverage(rows):
    """The statement for an aggregate computed with no reconciliation behind it.

    Not a fabricated 1.0 and not an omission.  Counting requests and adding up
    a notional cost is a fact about the rows that were shipped; it becomes a
    claim about a plan only when a fraction of that plan is attached to it, and
    a caller that has not supplied a report has attached nothing.  So the
    statement says exactly that, and `placed_fraction` is None rather than 1.0.
    """
    n = len([r for r in rows if isinstance(r, dict)])
    return CoverageStatement(
        fraction=None, basis="no-report-supplied", windows_n=0,
        windows_closed_n=0, windows_with_coverage_n=0, attestations_n=0,
        rows_total=n, rows_in_closed_windows=None, rows_in_open_windows=None,
        rows_unclaimed=None, rows_undatable=len(
            [r for r in rows if isinstance(r, dict) and not _num(r.get("ts"))]),
        hosts_reporting=set(), hosts_dark=set(), hosts_pending=set(),
        hosts_silent=set(), spans_unreadable=0,
        notes=["no reconciled report was supplied, so these totals are counts "
               "of the rows this store holds and nothing is claimed about what "
               "fraction of the account's activity they are"])


def coverage_for(report, account_uuid, rows, kind="5h"):
    """The coverage statement for a set of one account's rows.

    Placement is by the same rule the reconciler uses -- a row is in a window
    if its own `ts` falls in `[start, end)` -- and the three ways a row can
    fail to be attributable are counted separately, because they mean different
    things: open (not yet), unclaimed (windows are activity-anchored and the
    gaps between them belong to no window at all), undatable (no usable clock).

    The attestation fraction is a length-weighted mean over the windows these
    rows touch, and it is None the moment ANY of them has no attestation.  A
    mean over the windows that spoke, presented as the coverage of a set that
    includes windows nobody described, is a confident number about an unknown.
    """
    rows = [r for r in rows if isinstance(r, dict)]
    if report is None:
        return no_report_coverage(rows)
    acc = report.accounts.get(account_uuid)
    if acc is None:
        return no_report_coverage(rows)

    wins = [w for w in acc.windows if w.kind == kind]
    covs = {}
    for a in acc.attributions:
        if a.kind == kind:
            covs[(a.kind, a.resets_at)] = a.coverage

    in_closed = in_open = unclaimed = undatable = 0
    touched = []
    for r in rows:
        ts = r.get("ts")
        if not _num(ts):
            undatable += 1
            continue
        home = None
        for w in wins:
            if window.contains(w, ts):
                home = w
                break
        if home is None:
            unclaimed += 1
        elif home.closed:
            in_closed += 1
            if home not in touched:
                touched.append(home)
        else:
            in_open += 1
            if home not in touched:
                touched.append(home)

    known, unknown, length, covered = 0, 0, 0.0, 0.0
    hosts_r, hosts_d, hosts_p, hosts_s = set(), set(), set(), set()
    unreadable = 0
    for w in touched:
        c = covs.get((w.kind, w.resets_at))
        if c is None or c.fraction is None:
            unknown += 1
            continue
        known += 1
        length += w.length
        covered += c.fraction * w.length
        hosts_r |= set(c.hosts_reporting or ())
        hosts_d |= set(c.hosts_dark or ())
        hosts_p |= set(c.hosts_pending or ())
        hosts_s |= set(c.hosts_silent or ())
        unreadable += c.spans_unreadable or 0

    if not touched:
        fraction, basis = None, "no-window-touched"
    elif unknown and known:
        # UNREACHABLE THROUGH `reconcile` TODAY, and said plainly rather than
        # left to look like coverage: `coverage.for_window` returns None only
        # when the account has sent no attestation at ALL, which is a
        # per-account state, so every window of an account is unknown together
        # or none is.  A window nobody named while another was named is
        # `fraction 0.0` with basis `no-attestation-names-it` -- dark, a
        # positive statement -- and it composes into the mean below, correctly.
        # This branch is kept because `coverage_for` is reached from
        # `reconcile.reconcile`, which takes attestations from any caller, and
        # because a per-window unknown is one change to `for_window` away; no
        # test covers it, and that is stated here rather than implied by its
        # existence.
        fraction, basis = None, "partly-unknown"
    elif unknown:
        fraction, basis = None, "no-attestation"
    else:
        fraction = (covered / length) if length else None
        basis = "attestations"

    notes = []
    if basis == "no-attestation":
        notes.append(
            "coverage is unknown, not zero: no machine on this account has "
            "sent an attestation, so nothing here separates a machine that was "
            "off from a browser or phone session")
    if basis == "partly-unknown":
        notes.append(
            "%d of the %d windows these rows touch have attestations and %d do "
            "not, so no single fraction describes the set; the covered ones "
            "are %.2f" % (known, known + unknown, unknown,
                          (covered / length) if length else 0.0))
    if unclaimed:
        notes.append(
            "%d row(s) fall in no %s window at all -- windows are "
            "activity-anchored, not a calendar tiling -- so they are counted "
            "here and attributed nowhere" % (unclaimed, kind))
    if undatable:
        notes.append(
            "%d row(s) carry no usable timestamp, so they are in no window and "
            "their weight sits in the residual" % undatable)
    if unreadable:
        notes.append(
            "%d uptime span(s) could not be read, so the fraction beside them "
            "is a floor built from the spans that parsed and is NOT evidence "
            "that a machine was off" % unreadable)

    return CoverageStatement(
        fraction=fraction, basis=basis, windows_n=len(touched),
        windows_closed_n=len([w for w in touched if w.closed]),
        windows_with_coverage_n=known, attestations_n=acc.attestations_n or 0,
        rows_total=len(rows), rows_in_closed_windows=in_closed,
        rows_in_open_windows=in_open, rows_unclaimed=unclaimed,
        rows_undatable=undatable, hosts_reporting=hosts_r, hosts_dark=hosts_d,
        hosts_pending=hosts_p, hosts_silent=hosts_s,
        spans_unreadable=unreadable, notes=notes)


# ---------------------------------------------------------------------------
# 9. breakdowns
# ---------------------------------------------------------------------------

# Grouping by a LABEL across accounts merges two identities into one bucket.
# `account` is what claudio's `--tag account=` put there and anybody can
# overwrite it; `email` survives in `.claude.json` after a login is orphaned.
# Within one account they are perfectly good group-by keys, which is why this
# is a cross-account refusal and not a ban.
LABEL_COLUMNS = ("account", "email")


def _bucket_key(row, by):
    """The bucket a row belongs to, and whether it named one.

    `show` computes this as `r.get(key) or tags.get(key) or "(none)"`, and the
    `or` chain has a real bug in it: a legitimately falsy value -- 0, or the
    empty string -- falls through to the tag lookup and then to `(none)`, so a
    row with `duration_ms: 0` is bucketed as unlabelled.  This checks presence
    instead.  The divergence is deliberate and named rather than silent, so
    that a figure differing from `show`'s has an explanation that is not "one
    of them is wrong".

    A row with no value lands in the `None` bucket and stays there.  It is
    never dropped and never spread across the other buckets, for
    `attribute._buckets`' reason: either would move weight the request did not
    have onto labels it does not carry.
    """
    v = cell(row, by)
    if v is not ABSENT and v is not None:
        return v
    if not by.startswith(TAG_PREFIX):
        tv = cell(row, TAG_PREFIX + by)
        if tv is not ABSENT and tv is not None:
            return tv
    return None


def breakdown(rows, by, report=None, account_uuid=None, kind="5h",
              selection=None):
    """One account's rows, grouped by one column.  Coverage travels with it.

    Refuses rather than answering when the row set spans more than one account.
    That is the same enforcement `window.reconstruct` applies to samples, one
    stream over: percentages belong to one plan, and a table whose rows come
    from two plans is a table with a percent sign and no denominator.  A
    breakdown BY `account_uuid` is not that -- each bucket is one account and
    there is no total row anywhere in this module -- so it is allowed.
    """
    rows = [r for r in rows if isinstance(r, dict)]
    ok, kind_of = _known_column(by) if isinstance(by, str) else (False, None)
    if not ok:
        return Unanswerable(
            "unknown-column", repr(by),
            "stream A columns: %s; user tags: %s<key>"
            % (", ".join(sorted(LEDGER_COLUMNS)), TAG_PREFIX))
    if kind_of == "structured":
        return Unanswerable(
            "cannot-group-by-a-structure", by,
            "a row carries many tags and belongs to as many buckets; group by "
            "%s<key> for one of them" % TAG_PREFIX)

    accounts = sorted({r.get("account_uuid") for r in rows} - {None})
    if account_uuid is not None:
        rows = [r for r in rows if r.get("account_uuid") == account_uuid]
        accounts = [account_uuid]
    elif len(accounts) > 1:
        if by in LABEL_COLUMNS:
            return Unanswerable(
                "label-is-not-an-identity", by,
                "`%s` is a label -- claudio's --tag account= overwrites it and "
                "two accounts can carry the same one, so grouping across "
                "accounts by it would merge two identities into one bucket. "
                "Group by account_uuid, or pass account_uuid=." % by)
        if by != "account_uuid":
            return Unanswerable(
                "crosses-accounts", ", ".join(accounts),
                "these rows belong to %d accounts. Percentages belong to one "
                "plan and this module computes no cross-account total: pass "
                "account_uuid=, or call breakdown_per_account()."
                % len(accounts))

    nulls_missing = {f: 0 for f in TOKEN_FIELDS}
    buckets = {}
    for r in rows:
        k = _bucket_key(r, by)
        b = buckets.setdefault(k, {
            "requests": 0, "cost": 0.0, "weightless": 0, "duration_ms": 0,
            "duration_ms_null": 0,
            "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "tokens_null_rows": 0,
            "overhead_requests": 0, "overhead_cost": 0.0,
        })
        b["requests"] += 1
        w, weightless = attribute.weight_of(r)
        b["cost"] += w
        b["weightless"] += 1 if weightless else 0
        missing = False
        for f in TOKEN_FIELDS:
            v = r.get(f)
            if _num(v):
                b[f] += v
            else:
                missing = True
                nulls_missing[f] += 1
        b["tokens_null_rows"] += 1 if missing else 0
        d = r.get("duration_ms")
        if _num(d):
            b["duration_ms"] += d
        else:
            b["duration_ms_null"] += 1
        if r.get("query_source") in OVERHEAD_SOURCES:
            b["overhead_requests"] += 1
            b["overhead_cost"] += w

    # `show`'s footer, kept because it is right: name the overhead sources
    # THESE rows contain, and name every source counted as the user's work, so
    # a new overhead source announces itself the first time it is seen instead
    # of waiting for somebody to guess its name.  Printing the whole deny-list
    # beside a count only one of its names produced reads as evidence of a
    # request that was never made.
    seen_over = sorted({r.get("query_source") for r in rows
                        if r.get("query_source") in OVERHEAD_SOURCES})
    seen_work = sorted({r.get("query_source") for r in rows
                        if r.get("query_source")
                        and r.get("query_source") not in OVERHEAD_SOURCES})
    overhead = {
        "requests": sum(1 for r in rows
                        if r.get("query_source") in OVERHEAD_SOURCES),
        "cost": sum(attribute.weight_of(r)[0] for r in rows
                    if r.get("query_source") in OVERHEAD_SOURCES),
        "sources_seen": seen_over, "counted_as_your_work": seen_work,
        "note": "these are Claude Code's own requests, counted and named, "
                "never filtered out from under a total",
    }

    # `avail` has an entry for every ledger column always, and for a TAG column
    # only when some row carried it -- so a missing entry is the strongest form
    # of never-observed, not an unknown to be defaulted to False.  Getting this
    # backwards would report a tag key nothing carries as a populated column
    # with one empty bucket, which is the exact reading this field exists to
    # prevent.
    info = field_availability(rows)["columns"].get(by)
    column_status = {
        "column": by,
        "rows_with_a_value": info.get("present", 0) if info else 0,
        "distinct_values": info.get("distinct_n", 0) if info else 0,
        "never_observed": True if info is None else bool(
            info.get("never_observed")),
    }

    notes = []
    if column_status["never_observed"] and rows:
        notes.append(
            "no row scanned carries a value for `%s`, so this is one bucket of "
            "everything rather than a breakdown. The column exists and has "
            "never been populated in this data -- which is not the same as "
            "every request having the same value." % by)
    if any(v > 0 for v in nulls_missing.values()):
        notes.append(
            "token columns are null where the payload did not state them and "
            "are summed as absent, not as 0: %s"
            % ", ".join("%s in %d row(s)" % (f, n)
                        for f, n in sorted(nulls_missing.items()) if n))
    notes.append(
        "`cost` is Claude Code's own cost_usd at API list rates, which a Pro "
        "or Max plan is never charged. It is a relative weight and it is the "
        "same number the attribution weights by; it is not spend.")
    if len(accounts) == 1 and accounts[0]:
        acct = accounts[0]
    else:
        acct = None
    cov = coverage_for(report, acct, rows, kind=kind) if acct \
        else no_report_coverage(rows)

    empty = None
    if selection is not None:
        empty = selection.empty_because
    elif not rows:
        empty = "no-data"

    return Breakdown(
        account_uuid=acct, by=by, buckets=buckets, order="cost desc",
        matched=len(rows),
        scanned=(selection.scanned if selection is not None else len(rows)),
        undatable_n=len([r for r in rows if not _num(r.get("ts"))]),
        empty_because=empty,
        eliminated=(selection.eliminated if selection is not None else {}),
        sole_cause=(selection.sole_cause if selection is not None else None),
        overhead=overhead, coverage=cov, column_status=column_status,
        notes=notes + list(selection.notes if selection is not None else []))


def breakdown_per_account(rows, by, report=None, kind="5h"):
    """{account_uuid: Breakdown}.  No total, deliberately, and not even for
    tokens.

    The rule could be written "percentages never, absolute quantities freely",
    and it is not, because a rule with an exception in it is applied by
    somebody who remembers only the exception.  Two accounts are two plans,
    two denominators and often two people; a single figure spanning them
    answers no question this tool is for.  A caller who genuinely wants one can
    add two numbers -- what it will not get is this module's name on it.
    """
    out = {}
    for uuid in sorted({r.get("account_uuid") for r in rows
                        if isinstance(r, dict)} - {None}):
        out[uuid] = breakdown([r for r in rows
                               if r.get("account_uuid") == uuid],
                              by, report=report, account_uuid=uuid, kind=kind)
    return out


# ---------------------------------------------------------------------------
# 10. the reconciled view
# ---------------------------------------------------------------------------

def windows_view(report, account_uuid, kind=None, closed_only=False,
                 include_by=True):
    """The reconciled windows for one account: movement, shares, residual,
    coverage.

    A pure re-shaping of what `reconcile` already computed.  Nothing is
    recomputed here and nothing is rounded away -- in particular the residual
    is passed through with its sign, because a negative residual means the
    movement bound is loose and clamping it at zero would delete the only
    evidence that the weighting is off.

    `refused` is carried on every row and never turned into an empty one.
    `window-open`, `no-coverage` and `no-rate` are three different states and
    each of them has a number the reader still needs: an open window has real
    movement, a window nobody watched has real movement, and a window with no
    rate has real shares of observed weight.
    """
    if report is None:
        return Unanswerable("no-report", None,
                            "call reconcile() or reconcile_ingest() first")
    if not account_uuid:
        return Unanswerable(
            "no-account", None,
            "windows belong to one account: percentages are a fraction of one "
            "plan and are never pooled")
    acc = report.accounts.get(account_uuid)
    if acc is None:
        return Unanswerable(
            "unknown-account", account_uuid,
            "accounts in this report: " + (", ".join(sorted(report.accounts))
                                           or "(none)"))
    if kind is not None and kind not in window.KINDS:
        return Unanswerable("unknown-window-kind", repr(kind),
                            "kinds: " + ", ".join(sorted(window.KINDS)))

    by_key = {(a.kind, a.resets_at): a for a in acc.attributions}
    out = []
    for w in acc.windows:
        if kind is not None and w.kind != kind:
            continue
        if closed_only and not w.closed:
            continue
        a = by_key.get((w.kind, w.resets_at))
        row = {
            "account_uuid": account_uuid,
            "kind": w.kind, "resets_at": w.resets_at,
            "start": w.start, "end": w.end, "length": w.length,
            "state": w.state, "close_basis": w.close_basis,
            "bounds_basis": w.bounds_basis,
            "samples_n": w.samples_n, "hosts": sorted(w.hosts or ()),
            "sessions_n": len(w.sessions or ()),
            "peak": w.peak, "trough": w.trough,
            "baseline": w.baseline, "baseline_basis": w.baseline_basis,
            "movement_pp": w.movement_lo,
            "movement_quant_lo": w.movement_quant_lo,
            "movement_quant_hi": w.movement_quant_hi,
            "observation_gap_s": w.observation_gap_s,
            "skew": dict(w.skew or {}), "stale": dict(w.stale or {}),
            "notes": list(w.notes or ()),
        }
        if a is None:
            row["refused"] = "no-attribution"
            row["coverage"] = None
            out.append(row)
            continue
        row.update({
            "refused": a.refused,
            "requests_n": a.requests_n, "weight_total": a.weight_total,
            "weightless_n": a.weightless_n,
            "attributed_pp": a.attributed_pp, "residual_pp": a.residual_pp,
            "residual_fraction": a.residual_fraction,
            "over_attributed": a.over_attributed,
            "rate": a.rate, "rate_basis": a.rate_basis,
            "rate_pinned_by": a.rate_pinned_by,
            "rate_is_from_this_window": a.rate_is_from_this_window,
            "rate_pinned_stats": a.rate_pinned_stats,
            "coverage": a.coverage.as_dict() if a.coverage else None,
            "notes": row["notes"] + list(a.notes or ()),
        })
        if include_by:
            row["by"] = a.by
            row["by_tag"] = a.by_tag
        out.append(row)

    return {
        "account_uuid": account_uuid,
        "windows": out,
        "rate": dict(acc.rate or {}), "rate_basis": dict(acc.rate_basis or {}),
        "gaps": list(acc.gaps or ()),
        "unclaimed_n": {k: len(v) for k, v in (acc.unclaimed or {}).items()},
        "undatable_n": len(acc.undatable or ()),
        "known_hosts": sorted(acc.known_hosts or ()),
        "requests_n": acc.requests_n, "samples_n": acc.samples_n,
        "attestations_n": acc.attestations_n,
        "refusals": [(r[0], r[1]) for r in (acc.refusals or ())],
        "notes": list(acc.notes or ()),
    }


def residual_view(report, account_uuid, kind="5h"):
    """The residual, per closed window, with the guard beside every figure.

    Refused windows are IN this list, with their reason and a null residual.
    Dropping them would leave a table of windows that all reconcile, which is
    the tidiest possible way to hide the ones that could not.
    """
    v = windows_view(report, account_uuid, kind=kind, closed_only=True,
                     include_by=False)
    if refused(v):
        return v
    rows = []
    for w in v["windows"]:
        rows.append({
            "kind": w["kind"], "resets_at": w["resets_at"],
            "state": w["state"],
            "movement_pp": w["movement_pp"],
            "movement_quant_lo": w["movement_quant_lo"],
            "movement_quant_hi": w["movement_quant_hi"],
            "attributed_pp": w.get("attributed_pp"),
            "residual_pp": w.get("residual_pp"),
            "residual_fraction": w.get("residual_fraction"),
            "refused": w.get("refused"),
            "coverage_fraction": (w.get("coverage") or {}).get("fraction"),
            "coverage_basis": (w.get("coverage") or {}).get("basis"),
            "observation_gap_s": w.get("observation_gap_s"),
            "notes": w.get("notes"),
        })
    return {"account_uuid": account_uuid, "kind": kind, "windows": rows,
            "note": attribute.PROVISIONAL_NOTE}


# ---------------------------------------------------------------------------
# 11. the seam a storage layer plugs into
# ---------------------------------------------------------------------------

def narrowing(q):
    """What an index may use to narrow the candidate set for this query.

    Read `Narrowing`'s docstring before using it: a storage layer may return a
    superset and must never return a subset.  Everything this describes is
    re-checked by the predicate, so an index can only cost time, never change
    an answer -- which is the property that lets it be deleted and rebuilt from
    the JSONL at any moment.
    """
    if refused(q):
        return q
    eq, unsupported = {}, []
    for col, vals in sorted(q.where.items()):
        if col in INDEX_COLUMNS and None not in vals:
            eq[col] = tuple(vals)
        else:
            unsupported.append("where %s" % col)
    for col in sorted(q.not_where):
        unsupported.append("not_where %s" % col)
    for col in sorted(q.ranges):
        if col != "ts":
            unsupported.append("range %s" % col)
    if q.text:
        unsupported.append("free text")
    ids = {c: tuple(q.where[c]) for c in ID_FIELDS if c in q.where}
    lo, hi = q.since, q.until
    if "ts" in q.ranges:
        rlo, rhi = q.ranges["ts"]
        lo = rlo if lo is None else (max(lo, rlo) if rlo is not None else lo)
        hi = rhi if hi is None else (min(hi, rhi) if rhi is not None else hi)
    return Narrowing(account_uuid=q.account_uuid, ts_lo=lo, ts_hi=hi,
                     equalities=eq, ids=ids, unsupported=unsupported)


def plan(q, candidate_rows=None, budget_s=None, strategy="parse"):
    """What answering this query will cost, and a refusal if that is too much.

    `candidate_rows` is what the storage layer says it will hand over -- the
    whole corpus for a scan, the index's count for a lookup.  With no number
    there is no estimate, and this returns a plan that says so rather than an
    invented one.

    The refusal only fires when `budget_s` is given.  The measurement says a
    parse-and-filter scan leaves interactive range at about 44k rows and a
    whole-history GROUP BY over 22 M rows takes 12.9 s even with the index:
    that is a fact about the corpus, not a verdict on the question, and a query
    layer that decides for the user which questions are worth waiting for has
    quietly replaced the measurement with a policy.
    """
    if refused(q):
        return q
    if strategy not in ("parse", "prefilter"):
        return Unanswerable("unknown-strategy", repr(strategy),
                            "strategy is 'parse' or 'prefilter'")
    nar = narrowing(q)
    rate = (SCAN_PARSE_S_PER_GIB if strategy == "parse"
            else SCAN_PREFILTER_S_PER_GIB)
    ceiling = (INTERACTIVE_ROWS_PARSE if strategy == "parse"
               else INTERACTIVE_ROWS_PREFILTER)
    if candidate_rows is None:
        return Plan(query=q, narrowing=nar, candidate_rows=None,
                    estimate_s=None, interactive=None,
                    basis="no candidate count was supplied",
                    notes=["the caller did not say how many rows it will hand "
                           "over, so no estimate is possible; an estimate "
                           "invented here would be a number with no "
                           "measurement behind it"])
    est = candidate_rows * LEDGER_MEAN_ROW_BYTES / _GIB * rate
    interactive = est <= INTERACTIVE_S
    basis = ("%d candidate row(s) at %.3f s/GiB (%s) over a %.0f-byte mean "
             "row, measured; %d rows is the %.0f ms ceiling"
             % (candidate_rows, rate, strategy, LEDGER_MEAN_ROW_BYTES,
                ceiling, INTERACTIVE_S * 1000))
    if budget_s is not None and est > budget_s:
        fix = []
        if nar.account_uuid is None:
            fix.append("name an account_uuid")
        if nar.ts_lo is None or nar.ts_hi is None:
            fix.append("bound the time range with since/until")
        if not nar.equalities and not nar.ids:
            fix.append("add an equality on one of: "
                       + ", ".join(INDEX_COLUMNS))
        return Unanswerable(
            "scan-budget-exceeded",
            "%s; budget %.3f s, estimate %.3f s" % (basis, budget_s, est),
            ("narrow it (%s), or raise the budget and wait -- the estimate is "
             "measured, not a guess" % "; ".join(fix)) if fix else
            "raise the budget and wait: this query is already narrowed as far "
            "as the index columns allow")
    return Plan(query=q, narrowing=nar, candidate_rows=candidate_rows,
                estimate_s=est, interactive=interactive, basis=basis,
                notes=([] if interactive else [
                    "this is above the measured interactive ceiling of %d rows "
                    "for a %s scan; it is not refused, because a slow answer "
                    "is still an answer and only the caller knows whether it "
                    "is waiting for a person" % (ceiling, strategy)]))


# Every reason string this module can return, derived by a test from the source
# rather than listed by hand -- a hand-written list is a fourth place to forget,
# which is the same guard claudio puts on its own usage text.  The tuple is here
# so a caller can enumerate them; the test is what keeps it honest.
REASONS = (
    "account-uuid-not-a-string", "cannot-group-by-a-structure",
    "column-not-a-string", "crosses-accounts",
    "cursor-does-not-match-this-query", "cursor-unreadable",
    "id-not-a-string", "label-is-not-an-identity",
    "limit-not-a-positive-integer", "no-account", "no-report",
    "not-an-id-column", "range-inverted", "range-not-a-number",
    "range-not-a-pair", "range-on-a-non-numeric-column",
    "scan-budget-exceeded", "spec-not-an-object", "text-is-empty",
    "text-not-a-string", "text-search-on-an-id-column",
    "text-search-on-a-non-text-column", "unknown-column", "unknown-order",
    "unknown-query-key", "unknown-strategy", "unknown-window-kind",
    "unknown-account",
)
