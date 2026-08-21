"""Compiling a `query.Query` into SQL DuckDB can run over the JSONL in place.

Pure.  This module opens nothing: it builds strings and maps values, so every
decision below is testable without a database and without a socket, and the
purity test's exception set stays `{serve.py, store.py}`.  `srv/store.py` is
the half that connects.

That is enforced, not intended, and the enforcement reaches this file
specifically: `duckdb` is in the purity test's FORBIDDEN_IMPORTS beside `os`
and `socket`, and the exception set is asserted as an EQUALITY rather than
subtracted from -- so `import duckdb` here fails the suite, and a third impure
module has to be admitted in `server/tests/test_all.py` before it can exist.
The split is what lets every rule below be tested by reading a string.  A
stranger reaching for `duckdb.typing` to tidy the pinned map up would be
deleting that.

DuckDB is here for exactly one reason: it reads `accounts/<uuid>/*.jsonl` **in
place**, so the JSONL stays the record of truth, nothing is migrated, and a
derived index can be deleted at any moment.  Everything else it brings --
real joins across the two streams above all -- is a consequence of that, not
the argument for it.

Five measurements shaped this file, and each of them is a refusal.

**1.  Inference DROPS a column, and the drop is silent.**  Measured on 1.5.5:
30 000 rows of the real ledger with `cache_read_tokens` absent from the first
25 000 -- inside the default `sample_size` -- and `read_json_auto` returns a
relation of **29 columns instead of 30**.  `SELECT *` is a column narrower,
`GROUP BY model` answers cheerfully, and only a query that names the column by
hand errors.  Cache reads were ~89% of this user's tokens.  A reader that
derives its column list from the relation -- which is what `srv/ui.py`'s "hold
no list" rule demanded, and what omini will do -- would render the table with
that column simply gone, indistinguishable from a column that never existed.

So every column is PINNED, from `wire.LEDGER_FIELDS`, and `columns=` is never
omitted.  The same measurement with a sampled-but-all-null column gives `JSON`
where `BIGINT` was meant, and `sum()` over it refuses by name -- loud, so not
the dangerous half, but wrong all the same.

**2.  A pinned list trades one silence for another, so it is counted.**  A key
that arrives and is not in the list is invisible to a pinned read (measured:
`SELECT new_field_2027` is a binder error, and `read_json_auto` sees it fine).
That is `cu/otlp.py`'s allow-list one layer down, and the fix is the one the
receiver already uses: `UNKNOWN_KEYS_SQL` counts and NAMES every key in the
file that this list does not carry, so a field Claude Code adds announces
itself instead of being dropped in silence.  A list that matches nothing looks
exactly like a list that matches everything until somebody counts.

**3.  `ignore_errors=true` does not skip a malformed line.  It emits a row of
NULLs.**  Measured: 1000 good records plus one torn fragment gives
`count(*) = 1001`, and the extra row is `(None, None, None, ...)`.  A torn
final line is not an exotic input here -- `serve.Store._append` carries a guard
against gluing a fragment onto a good record precisely because a process killed
mid-append leaves one, and `cu/ledger.py` counts the same tear from the other
end.  Without `ignore_errors` the whole query dies on it instead, which turns
one torn byte range into a store that answers nothing.

So the read is `ignore_errors=true` **and** every count excludes and reports
the phantom: `request_id` is a content hash present in every real row, so
`request_id IS NULL` is exactly the malformed set.  `MALFORMED_PREDICATE` is
that test in one place, and `store.py` reports the count as a store problem
rather than subtracting it quietly.

The flag does the same thing one level DOWN, and that half is not visible in
any count: with `columns=` pinned, a perfectly valid JSON line whose value
does not fit the pinned type has THAT VALUE nulled and the row KEPT.  Measured
against the real fixtures: `input_tokens: 9223372036854775808` -> NULL,
`1e30` -> NULL, `"abc"` -> NULL, `ts: "2026-08-12T10:00:00Z"` -> NULL, and in
every case `scanned=2, matched=2`, `request_id` intact, so
`MALFORMED_PREDICATE` never fires.  Without `ignore_errors` the same file
raises `Invalid Input Error: JSON transform error`, so the flag is doing
double duty and the silence is its doing.  It is the cardinal sin in its
purest form: `store._token_nulls` counts such a row and the aggregate note
used to say "the payload did not state them" about a figure the payload stated
precisely.

There is no free way to count that -- the coercion has already happened by the
time the pinned relation exists -- so it is counted by a SECOND read of the
same bytes, `coerced_values_sql`, which is a diagnostics call for
`unknown_keys_sql`'s reason and reported at `/api/v1/diagnostics`.  What is on
the query path instead is the truthful wording: `_token_nulls`' note names
both possibilities and points at the route that separates them.

**4.  A pinned BIGINT COERCES rather than refusing.**  `1.5` arrives as `2`,
`"7"` as `7`, `true` as `1` -- measured, all three, no warning, and the first
two ADD to a sum the oracle excludes.  `model: {"a":1}` arrives as the string
`'{"a":1}'` and `ts: "1786584663"` as a datable `1786584663.0` where the
oracle counts the row as undatable.  The token
columns, `duration_ms`, `ts_ns` and `schema` are integers in every one of the 8
real rows, so BIGINT is what they are; the coercion is named here rather than
discovered later.  The seven NEVER-OBSERVED columns get `JSON` instead, and
that is the whole point of them being a separate list: `event_sequence` is
`a.get("event.sequence")` off the payload and nothing has ever stated what
arrives in it, so pinning it `BIGINT` because the name ends in "sequence" is
the assumption-written-as-a-value this project keeps apologising for.  `JSON`
preserves the value AND its type, so `5` and `"5"` are still different when
somebody finally looks.

**5.  Everything binds except the one thing that matters, so that one thing
gets a whitelist instead.**  An earlier draft of this module quoted its values
into the SQL and justified it by saying binding "is not available for a table
function's path argument".  Measured on 1.5.5, that is FALSE: the path binds,
a LIST of paths binds, the pinned `columns=` map binds and keeps its types
exactly, a value inside `FILTER (WHERE ...)` binds, a row comparison binds,
`LIKE ... ESCAPE` binds, a JSON path binds, and so does `LIMIT`.  So every
value is bound and the quoting helpers are deleted rather than left lying
about as a second way to do it.

That is not a tidy-up.  Interpolation had a live defect with no injection in
it at all: `repr(float("-inf"))` is `-inf`, which DuckDB reads as a COLUMN
NAME, so `since=-inf` -- an ordinary way to say "no lower bound", and one
`serve._num_param` accepts today -- matched all 8 real rows through
`query.py` and raised `Binder Error: Referenced column "inf" not found`
through this one.  Bound, the two engines agree.

What does NOT bind is an identifier, and that measurement is the opposite of
reassuring: `SELECT $c` with `c="model"` returns the constant STRING 'model'
once per row -- a plausible answer to a question nobody asked, with no error
anywhere.  A column name therefore has exactly one possible defence, a
whitelist, and `query.compile_query` applies it before this module is
reached.
"""

import json

from . import query as Q
from . import wire

# ---------------------------------------------------------------------------
# 1. the pinned schema
# ---------------------------------------------------------------------------
#
# Types by EVIDENCE, exactly as `query.py` classifies the same columns, and a
# test asserts the two agree: every `query.OBSERVED_NUMERIC` column is numeric
# here, every `query.NEVER_OBSERVED` column is `JSON` here, and the key set is
# `wire.LEDGER_FIELDS + (wire.LEDGER_STAMP,)` with nothing added or missing.
# Deriving the list is the point -- a hand-typed copy is the fourth place to
# forget, and the column it forgets renders as an absence.

_LEDGER_TYPES = {
    "request_id": "VARCHAR",
    "schema": "BIGINT",
    "ts": "DOUBLE",
    "ts_ns": "BIGINT",
    "client_version": "VARCHAR",
    "account": "VARCHAR",
    "host": "VARCHAR",
    "email": "VARCHAR",
    "tags": "JSON",
    "session_id": "VARCHAR",
    "prompt_id": "VARCHAR",
    "model": "VARCHAR",
    "model_raw": "VARCHAR",
    "query_source": "VARCHAR",
    "terminal_type": "VARCHAR",
    "input_tokens": "BIGINT",
    "output_tokens": "BIGINT",
    "cache_read_tokens": "BIGINT",
    "cache_creation_tokens": "BIGINT",
    "duration_ms": "BIGINT",
    "cost_usd_reported": "DOUBLE",
    "source": "VARCHAR",
    "ingested_at": "DOUBLE",
    "account_uuid": "VARCHAR",
}

# The never-observed seven, typed `JSON` on purpose.  Built from `query.py`'s
# list rather than repeated, so the day a capture carries an `agent_name` the
# constant moves in one place and this map follows it.
for _c in Q.NEVER_OBSERVED:
    _LEDGER_TYPES[_c] = "JSON"

LEDGER_SQL_TYPES = {c: _LEDGER_TYPES[c] for c in Q.LEDGER_COLUMNS}

# Stream B.  `tags` here is a comma-separated STRING, not a dict -- the two
# streams are asymmetric and `query.py` says so; typing it `JSON` would make a
# `k=v,k=v` string unparseable and the column null for every real row.  The
# two percentages are DOUBLE because the shim stores the server's raw float
# byte for byte, which is where `7.000000000000001` comes from.
_SAMPLE_TYPES = {
    "ts": "BIGINT",
    "account_uuid": "VARCHAR",
    "account_email": "VARCHAR",
    "organization_uuid": "VARCHAR",
    "rate_limit_tier": "VARCHAR",
    "five_hour_pct": "DOUBLE",
    "five_hour_resets_at": "BIGINT",
    "seven_day_pct": "DOUBLE",
    "seven_day_resets_at": "BIGINT",
    "session_id": "VARCHAR",
    "model": "VARCHAR",
    "session_cost_usd": "DOUBLE",
    "profile": "VARCHAR",
    "client": "VARCHAR",
    "project": "VARCHAR",
    "schema": "BIGINT",
    "reason": "VARCHAR",
    "host": "VARCHAR",
    "account": "VARCHAR",
    "prompt_id": "VARCHAR",
    "agent": "VARCHAR",
    "tags": "VARCHAR",
    "truncated": "BIGINT",
}

SAMPLE_COLUMNS = tuple(wire.SAMPLE_FIELDS_V2) + tuple(
    wire.SAMPLE_FIELDS_CONDITIONAL)
SAMPLE_SQL_TYPES = {c: _SAMPLE_TYPES[c] for c in SAMPLE_COLUMNS}

# The columns whose SQL value is JSON text and must be decoded on the way out.
JSON_COLUMNS = frozenset(c for c, t in LEDGER_SQL_TYPES.items() if t == "JSON")

# A content hash, present in every row `cu/otlp.py` ever wrote.  Its absence is
# therefore the malformed-line signature and nothing else -- see the module
# docstring, measurement 3.
MALFORMED_PREDICATE = "request_id IS NULL"


# ---------------------------------------------------------------------------
# 2. values are BOUND; only checked NAMES are written into the SQL
# ---------------------------------------------------------------------------
#
# Measurement 5 in the module docstring.  Every value that reaches DuckDB from
# a question -- a filter value, a time bound, a cursor position, a text
# needle, a tag key, a LIMIT, the pinned column map, and the FILE PATHS
# themselves -- is a bind parameter.  No function below concatenates one into
# SQL, and a test builds SQL from a query full of hostile values and asserts
# none of them appears anywhere in the string.
#
# What IS written into the SQL is names, and only names checked against a list
# first: `query.compile_query` refuses any column outside `LEDGER_COLUMNS` by
# name before this module is reached.  That division of labour is forced
# rather than chosen, and the measurement is worth stating because it is the
# opposite of reassuring: an identifier CANNOT be bound -- `SELECT $c` with
# `c="model"` returns the constant STRING 'model' once per row, a plausible
# answer to a question nobody asked, with no error anywhere.  So a whitelist
# is the only defence an identifier can have; `_ident` quotes what it is
# given, and a test pins both halves.

_INT128_MAX = (1 << 127) - 1


def _bindable(v):
    """A value DuckDB can bind, or the nearest one that compares identically.

    Exactly one conversion is needed, and it exists to keep the two engines
    from disagreeing rather than for DuckDB's comfort: an int beyond 128 bits
    is refused outright (`Python integer too large for 128-bit integer type`)
    while `query.py` compares it perfectly well, so an oracle that answers
    would face an engine that raises.  Every numeric column here is DOUBLE or
    BIGINT, so the comparison happens in floating point either way; the
    differential test asserts the agreement rather than this comment claiming
    it.
    """
    if isinstance(v, int) and not isinstance(v, bool) and abs(v) > _INT128_MAX:
        return float(v)
    return v


class Params(object):
    """The bind parameters of ONE statement, accumulated as its SQL is built.

    NAMED (`$p1`), not positional (`?`), and that is structural rather than a
    matter of taste.  `accounting_sql` embeds the whole predicate THREE times
    -- once for `matched`, once for `undatable`, once more per clause -- so
    positional placeholders would require every value repeated in textual
    order, and a repeat in the wrong order is a WRONG ANSWER rather than an
    error.  A named parameter is written once and reused; measured on 1.5.5,
    including three uses of one name in one statement.

    One `Params` per statement, never shared across two: DuckDB refuses a
    dict carrying a name the statement does not use (`Parameter
    argument/count mismatch, identifiers of the excess parameters: ...`).
    That refusal is the reason this pairing cannot drift -- a fragment built
    and then dropped fails loudly at `execute` instead of quietly widening an
    answer -- and it is pinned by its own test.
    """

    def __init__(self):
        self.values = {}

    def add(self, v):
        """Bind one value; return the placeholder that stands for it."""
        name = "p%d" % (len(self.values) + 1)
        self.values[name] = _bindable(v)
        return "$" + name


class UncheckedIdentifier(ValueError):
    """A column name reached the SQL builder without passing a whitelist."""


def _ident(col):
    """A quoted identifier -- and it now ENFORCES the precondition it states.

    An identifier cannot be bound (measurement 5: `SELECT $c` returns the
    constant string), so a whitelist is the only defence it can have.  This
    used to say "only ever reached with a checked column name" and merely
    escape whatever it got, which made the stated precondition an assertion
    rather than a guard -- and `/api/v1/values?fields=<anything>` walked past
    `query.compile_query` entirely and reached here, where only the `"`
    doubling stood between a caller and the rest of the statement.  Doubling
    held; the answer was still a 500 quoting the generated SQL back at a
    caller who had made an ordinary typo.

    So an unchecked name RAISES.  Callers that take a name from a question
    check it first and refuse `unknown-column` by name; the raise is the
    backstop for the next caller, not the diagnostic.
    """
    if col not in LEDGER_SQL_TYPES and col not in SAMPLE_SQL_TYPES:
        raise UncheckedIdentifier(
            "%r is not a pinned column; check it with query._known_column "
            "before building SQL from it" % (col,))
    return _quote_ident(col)


def _quote_ident(col):
    """Doubling, which is the SQL escape for a quoted identifier.

    Kept as its own function and still belt and braces: the whitelist above is
    the defence, this is what stands if a pinned column name ever contains a
    quote.  Separated so both halves can be asserted -- the escape used to be
    the ONLY half, and a test that pins an escape reads as a test that pins a
    defence.
    """
    return '"' + col.replace('"', '""') + '"'


# ---------------------------------------------------------------------------
# 3. the relation
# ---------------------------------------------------------------------------

def relation(paths, columns, params, ignore_errors=True):
    """`read_json(...)` over one path or many, with the schema pinned.

    `columns=` is never optional and `union_by_name` is never needed: a pinned
    list makes every file the same shape by construction, which is what the
    per-account layout (`accounts/<uuid>/ledger.jsonl`, one file per tenant)
    otherwise leaves to inference.

    Both the paths and the column map are BOUND.  A path is a value like any
    other -- it is derived from an `account_uuid` that arrives from a client --
    and binding the map was measured to preserve the pinned types exactly
    (`typeof(tags)` is still `JSON`, `typeof(input_tokens)` still `BIGINT`,
    and a torn line still yields its one phantom row), so there was no reason
    left to write it out.
    """
    if isinstance(paths, str):
        paths = [paths]
    if not paths:
        return None
    src = ("[" + ", ".join(params.add(p) for p in paths) + "]"
           if len(paths) > 1 else params.add(paths[0]))
    return ("read_json(%s, format='newline_delimited', columns=%s%s)"
            % (src, params.add(dict(columns)),
               ", ignore_errors=true" if ignore_errors else ""))


def unknown_keys_sql(paths, params):
    """Every key in the files that the pinned list does not carry, with counts.

    Measurement 2 in the module docstring: pinning cannot be allowed to make a
    new field invisible.  This is a full JSON parse and is deliberately NOT on
    the query path -- `store.py` runs it for diagnostics and over a bounded
    sample, and reports what it sampled.

    The known-column names are bound as VALUES here, not written out: in
    `k NOT IN (...)` they are strings compared against the file's keys, and
    the fact that they happen to be column names elsewhere does not make them
    identifiers here.
    """
    if isinstance(paths, str):
        paths = [paths]
    src = ("[" + ", ".join(params.add(p) for p in paths) + "]"
           if len(paths) > 1 else params.add(paths[0]))
    known = ", ".join(params.add(c) for c in Q.LEDGER_COLUMNS)
    return ("SELECT k, count(*) AS n FROM ("
            "  SELECT unnest(json_keys(json)) AS k"
            "  FROM read_ndjson_objects(%s, ignore_errors=true)"
            ") WHERE k NOT IN (%s) GROUP BY k ORDER BY n DESC, k"
            % (src, known))


# The JSON types `json_type` reports that a pinned SQL type can hold WITHOUT
# converting anything.  Measured on 1.5.5 against a file carrying one value of
# every shape; see `coerced_values_sql`.
_PIN_ACCEPTS = {
    "VARCHAR": ("VARCHAR",),
    "BIGINT": ("BIGINT", "UBIGINT"),
    "DOUBLE": ("BIGINT", "UBIGINT", "DOUBLE"),
}


def coerced_values_sql(paths, params):
    """Per (column, JSON type), how many rows carry a value of that type.

    The other half of measurement 3, and the half nothing counted.  A pinned
    read turns a value the column cannot hold into NULL and KEEPS the row, so
    `request_id IS NULL` never fires and no counter moves: `input_tokens:
    "abc"` and `input_tokens: 1e30` are indistinguishable from a payload that
    said nothing.  In the other direction it silently CONVERTS -- `"7"` -> 7,
    `true` -> 1, `1.5` -> 2 -- which ADDS to a sum the Python oracle excludes.

    This is a full JSON parse, so it is a diagnostics call and never on the
    query path, exactly like `unknown_keys_sql` beside it.  The classification
    is deliberately NOT in the SQL: the pinned type map is Python data and
    encoding it into a CASE expression would be a second copy of it.

    `fits` answers the one question `json_type` cannot: `5` and
    `9223372036854775808` are both `UBIGINT`, and only the second is nulled by
    a BIGINT pin.

    A key whose name contains a backslash escapes wrongly into the JSON path
    and extracts NULL, which this counts as absence rather than misreporting
    its type; `unknown_keys_sql` still names the key itself.
    """
    if isinstance(paths, str):
        paths = [paths]
    src = ("[" + ", ".join(params.add(p) for p in paths) + "]"
           if len(paths) > 1 else params.add(paths[0]))
    # The JSON path is built at RUN TIME out of a key in the data, which is a
    # value in an expression and not SQL text: `"` is escaped for the path.
    path = r"""'$."' || replace(k, '"', '\"') || '"'"""
    return (
        "SELECT k, json_type(v) AS t, "
        "(try_cast(json_extract_string(v, '$') AS BIGINT) IS NOT NULL) "
        "AS fits, count(*) AS n FROM ("
        "  SELECT k, json_extract(json, %s) AS v"
        "  FROM (SELECT json, unnest(json_keys(json)) AS k"
        "        FROM read_ndjson_objects(%s, ignore_errors=true))"
        ") GROUP BY k, t, fits ORDER BY k, t" % (path, src)
    )


def coercion_of(col, json_type, fits):
    """True if a value of `json_type` in `col` did NOT survive the pin intact.

    `NULL` is absence, not coercion: a key written null and a key that is not
    there are the same SQL NULL and neither is a value the reader lost.
    """
    pin = LEDGER_SQL_TYPES.get(col)
    if pin is None or pin == "JSON" or json_type in (None, "NULL"):
        return False
    accepts = _PIN_ACCEPTS.get(pin)
    if accepts is None:
        return False
    if json_type not in accepts:
        return True
    return pin == "BIGINT" and not fits


# ---------------------------------------------------------------------------
# 4. a clause, as SQL
# ---------------------------------------------------------------------------
#
# Every expression built here is NULL-FREE by construction: each returns TRUE
# or FALSE and never NULL, because the accounting below counts a row as
# eliminated by a clause when the clause is not TRUE, and a three-valued clause
# would make "eliminated" and "unknown" the same number.  `query.clauses_of`
# has the same property on the Python side -- its predicates return bools --
# and the differential test is what proves the two agree row for row.

def _col_expr(col, params):
    """The SQL expression for a column, including `tags.<key>`.

    A tag key is arbitrary text from a user's `--tag`, so it is bound as a
    JSON path rather than quoted into one: `$."k"` built by concatenation is
    one `"` away from selecting something else entirely.
    """
    if col.startswith(Q.TAG_PREFIX):
        return "json_extract(%s, %s)" % (
            _ident("tags"), params.add("$." + json.dumps(col[len(Q.TAG_PREFIX):])))
    return _ident(col)


def is_json_column(col):
    """True where the SQL value is JSON TEXT and must be decoded on the way out.

    Tag columns count: `json_extract(tags, '$."k"')` returns `"4"`, not `4`,
    so a facet built on one without decoding would report the tag value
    `'"4"'` -- a different string from the one the pure path reports, and a
    difference no reader would ever suspect.  Caught by the differential test,
    which is what it is for.
    """
    return (col.startswith(Q.TAG_PREFIX)
            or LEDGER_SQL_TYPES.get(col) == "JSON")


_is_json = is_json_column


NUMERIC_SQL_TYPES = ("BIGINT", "DOUBLE")


def comparable(col, v):
    """True if `v` can be compared to `col` WITHOUT a cast.

    `query._eq` compares Python values: `529 == "abc"` is False and `529 ==
    " 529 "` is False, with no error and no coercion.  SQL does not work that
    way.  Binding a VARCHAR against a BIGINT column makes DuckDB CAST, and the
    cast is wrong in both directions:

      * it RAISES on an ordinary typo -- `where.input_tokens=abc` is
        `ConversionException: Could not convert string 'abc' to INT64`, which
        escaped as HTTP 500 `reader-failed` ("this is a fault in the reader")
        over a user's spelling mistake, collapsing the three empty answers
        into a crash;
      * and where it succeeds it is LENIENT in ways equality is not.  Measured
        on 1.5.5: `529 = ' 529 '`, `529 = '+529'`, `529 = '529.0'`,
        `16 = '0x10'` and `16 = '1_6'` are all TRUE.  `where.input_tokens=%20529%20`
        returned the row with `outcome: ok` while the oracle matched none of
        the five -- a filter silently matching rows it did not name.

    The reverse is worse in principle: a non-string bound against VARCHAR casts
    the COLUMN, so whether the query raises depends on the DATA rather than on
    the question.  So a value of the wrong type for a column's pinned type
    matches nothing, spelled `FALSE`, which is exactly what the oracle does.
    """
    t = LEDGER_SQL_TYPES.get(col)
    if t in NUMERIC_SQL_TYPES:
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    if t == "VARCHAR":
        return isinstance(v, str)
    return True


def _eq_sql(col, wanted, params):
    """`query._eq` in SQL: one of a set of values, with `None` meaning absent.

    A pinned read cannot tell absent from null -- both arrive as SQL NULL --
    and neither can `_eq`, which matches `v is ABSENT or v is None` in one
    branch.  `field_availability` is what separates them, on both sides.
    """
    e = _col_expr(col, params)
    parts = []
    for w in wanted:
        if w is None:
            parts.append("(%s IS NULL)" % e)
        elif _is_json(col):
            parts.append("(%s IS NOT NULL AND %s = %s::JSON)"
                         % (e, e, params.add(json.dumps(w))))
        elif isinstance(w, bool):
            # `True == 1` in Python and `query._eq` compares by type as well,
            # so a boolean must not match an integer column.  Nothing in the
            # capture carries one; the guard is kept on both sides so they
            # cannot disagree.
            parts.append("(%s IS NOT NULL AND typeof(%s) = 'BOOLEAN' "
                         "AND %s = %s)" % (e, e, e, params.add(w)))
        elif not comparable(col, w):
            # NOT bound, and no `CAST` anywhere: see `comparable`.  The value
            # is still named in the clause LABEL, so `filtered-to-nothing`
            # points at the clause that really emptied the result.
            parts.append("FALSE")
        else:
            parts.append("(%s IS NOT NULL AND %s = %s)"
                         % (e, e, params.add(w)))
    return "(" + " OR ".join(parts) + ")" if parts else "FALSE"


def _range_sql(col, lo, hi, params):
    e = _ident(col)
    parts = ["%s IS NOT NULL" % e]
    if lo is not None:
        parts.append("%s >= %s" % (e, params.add(lo)))
    if hi is not None:
        parts.append("%s < %s" % (e, params.add(hi)))
    return "(" + " AND ".join(parts) + ")"


def _text_sql(needle, cols, params):
    """`query._text_hit`: a case-insensitive substring, over STRINGS only.

    The `json_type(...) = 'VARCHAR'` guard is load-bearing and was measured:
    `json_extract_string(x, '$')` over a JSON `5` returns the string `'5'`, so
    without it a search for "5" would match a number that `isinstance(v, str)`
    rejects on the Python side, and the two engines would disagree on real
    data the first time somebody searched for a digit.
    """
    pat = params.add("%" + needle.lower().replace("\\", "\\\\")
                     .replace("%", "\\%").replace("_", "\\_") + "%")
    parts = []
    for col in cols:
        e = _col_expr(col, params)
        if _is_json(col):
            parts.append(
                "(%s IS NOT NULL AND json_type(%s) = 'VARCHAR' "
                "AND lower(json_extract_string(%s, '$')) LIKE %s ESCAPE '\\')"
                % (e, e, e, pat))
        else:
            parts.append("(%s IS NOT NULL AND lower(%s) LIKE %s ESCAPE '\\')"
                         % (e, e, pat))
    return "(" + " OR ".join(parts) + ")" if parts else "FALSE"


def clauses_sql(q, params):
    """[(label, sql)] in `query.clauses_of`'s order, with its labels.

    The labels are the same strings, deliberately: `filtered-to-nothing` names
    the clause that emptied the result, and a reader comparing a DuckDB answer
    with the Python oracle's must see one vocabulary, not two.  A label is
    prose for a human and never reaches SQL.
    """
    out = []
    if q.account_uuid:
        out.append(("account_uuid=%s" % q.account_uuid,
                    "(%s IS NOT NULL AND %s = %s)"
                    % (_ident("account_uuid"), _ident("account_uuid"),
                       params.add(q.account_uuid))))
    if q.since is not None:
        out.append(("since=%r" % q.since,
                    "(%s IS NOT NULL AND %s >= %s)"
                    % (_ident("ts"), _ident("ts"), params.add(q.since))))
    if q.until is not None:
        out.append(("until=%r" % q.until,
                    "(%s IS NOT NULL AND %s < %s)"
                    % (_ident("ts"), _ident("ts"), params.add(q.until))))
    for col in sorted(q.where):
        vals = q.where[col]
        out.append(("%s in %s" % (col, list(vals)), _eq_sql(col, vals, params)))
    for col in sorted(q.not_where):
        vals = q.not_where[col]
        out.append(("%s not in %s" % (col, list(vals)),
                    "(NOT %s)" % _eq_sql(col, vals, params)))
    for col in sorted(q.ranges):
        lo, hi = q.ranges[col]
        out.append(("%s in [%r, %r)" % (col, lo, hi),
                    _range_sql(col, lo, hi, params)))
    if q.text:
        cols = tuple(q.text_in or Q.TEXT_SEARCHABLE)
        out.append(("text=%r in %d column(s)" % (q.text, len(cols)),
                    _text_sql(q.text, cols, params)))
    return out


def where_sql(q, params):
    """The whole predicate.  `TRUE` when the query asks for everything."""
    cl = clauses_sql(q, params)
    if not cl:
        return "TRUE"
    return " AND ".join(sql for _label, sql in cl)


# ---------------------------------------------------------------------------
# 5. the accounting -- one pass, not one pass per clause
# ---------------------------------------------------------------------------
#
# This is the part that has to survive the move to SQL, and it is the reason a
# thin layer exists at all.  `no-data`, `filtered-to-nothing` and
# `Unanswerable` are three different answers with three different messages, and
# SQL returns zero rows for all three.  `scanned`, `matched` and the per-clause
# elimination count are what tell the first two apart, and they cost one
# aggregate each in the SAME scan -- conditional aggregates, not a query per
# clause.
#
# `IS NOT TRUE` rather than `NOT`: `NOT NULL` is NULL, and a row a clause could
# not decide would then be counted as neither eliminated nor kept.
#
# Say plainly what the mutation matrix says: the expressions above are NULL-free
# by construction, so `NOT (x)` and `(x) IS NOT TRUE` agree on every input this
# module can produce, and NO TEST CATCHES the substitution.  It is kept because
# the NULL-freedom is a property of six functions that a seventh clause type
# would have to remember, and because it costs nothing -- the same reasoning,
# and the same honesty, as claudio's unreachable `${#1} -le 11` length check.

def accounting_sql(rel, q, params):
    """(sql, clause_labels) -> one row: scanned, matched, malformed, elim_*.

    The predicate is built ONCE and its text reused, which is the whole reason
    the parameters are named: the same `$p3` stands for one value in four
    places, where a positional `?` would need the value repeated four times in
    the order the placeholders happen to appear.
    """
    cl = clauses_sql(q, params)
    where = " AND ".join(sql for _l, sql in cl) if cl else "TRUE"
    # `matched` and `undatable` exclude the phantom row as well, and that is
    # not belt and braces: with no clauses at all `where` is `TRUE`, so an
    # unfiltered count over a store with one torn line reported one request
    # more than it served -- a number nobody could reconcile against the rows
    # in front of them, produced by the containment itself.
    real = "NOT (%s)" % MALFORMED_PREDICATE
    sel = ["count(*) AS scanned",
           "count(*) FILTER (WHERE %s) AS malformed" % MALFORMED_PREDICATE,
           "count(*) FILTER (WHERE (%s) AND %s) AS matched" % (where, real),
           "count(*) FILTER (WHERE (%s) AND %s AND %s IS NULL) AS undatable"
           % (where, real, _ident("ts"))]
    for i, (_label, sql) in enumerate(cl):
        sel.append("count(*) FILTER (WHERE (%s) IS NOT TRUE AND %s) "
                   "AS elim_%d" % (sql, real, i))
    return ("SELECT %s FROM %s" % (", ".join(sel), rel),
            [label for label, _sql in cl])


# ---------------------------------------------------------------------------
# 6. the row shapes
# ---------------------------------------------------------------------------

def select_sql(rel, q, params, columns=None, order=None, limit=None,
               after=None, require_ts=True):
    """Matching rows, ordered by the keyset key, optionally after a cursor.

    `require_ts=False` is what `select` uses and `page` must not: a row with no
    usable `ts` MATCHES a query and belongs in a selection -- `query.select`
    returns it and counts it -- but it can take no position in a time ordering,
    so it is in no page.  Collapsing the two would drop a matching row from an
    unpaged answer with no symptom, which is the same third-bucket mistake
    `reconcile` already made once with `undatable` weight landing silently in
    the residual.

    Keyset, not offset, for `query.paginate`'s reason: this store accepts a
    week-late shipment, so a row can be appended whose `ts` belongs in the
    middle of a page served yesterday, and an offset would skip a real request
    in silence.  The tuple comparison is what SQL gives us for free and what
    the Python side spells out by sorting.
    """
    cols = list(columns or Q.LEDGER_COLUMNS)
    where = where_sql(q, params)
    # A phantom NULL row from a torn line matches nothing a user asked for, but
    # `where` can be `TRUE`, so it is excluded here as well as counted above.
    #
    # It is TODAY redundant with the `ts IS NOT NULL` further down -- a
    # malformed line is all-NULL, so it has no `ts` either -- and a behavioural
    # test therefore cannot see this line removed.  That is exactly why it is
    # pinned by a test on the SQL TEXT instead: `ts IS NOT NULL` is there to
    # make the ordering total, not to exclude a tear, and the day somebody adds
    # the obvious "show me the undatable rows" query the two stop coinciding
    # and this is the only thing standing between a reader and a row of nulls
    # presented as a request.
    where = "(%s) AND NOT (%s)" % (where, MALFORMED_PREDICATE)
    if after is not None:
        ts, rid = after
        cmp_ = "<" if (order or q.order) == "desc" else ">"
        where += (" AND (%s, coalesce(%s, '')) %s (%s, %s)"
                  % (_ident("ts"), _ident("request_id"), cmp_,
                     params.add(float(ts)), params.add(rid)))
    direction = "DESC" if (order or q.order) == "desc" else "ASC"
    sql = ("SELECT %s FROM %s WHERE %s%s "
           "ORDER BY %s %s, coalesce(%s, '') %s"
           % (", ".join(_ident(c) for c in cols), rel, where,
              (" AND %s IS NOT NULL" % _ident("ts")) if require_ts else "",
              _ident("ts"), direction, _ident("request_id"), direction))
    if limit:
        sql += " LIMIT %s" % params.add(int(limit))
    return sql, cols


def materialise(cols, tup):
    """One SQL row -> the dict shape `query.py` and the core already read.

    JSON columns come back as TEXT and are decoded here, so `tags` is a dict
    again and a never-observed column keeps its own type rather than being
    flattened to a string.  That is the whole reason those seven are `JSON`
    and not `VARCHAR`.
    """
    row = {}
    for col, v in zip(cols, tup):
        if is_json_column(col) and isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        row[col] = v
    return row
