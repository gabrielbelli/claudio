"""The DuckDB query layer: what it pins, what it counts, and what it refuses.

Imported by `test_all.py`, which owns the runner and the counters, so these
run in the same pass and under the same mutation harness.

Two rules govern everything here.

**The differential test is the load-bearing one.**  `srv/query.py`'s Python
predicate is the ORACLE: every behaviour DuckDB now serves is asserted equal to
what the pure path returns over the same real rows -- row for row, count for
count, clause label for clause label.  That is the one-directional contract
`Narrowing` states, enforced rather than described: a storage layer may cost
time and must never change an answer.  A benchmark can prove a query is fast.
Only this can prove it is right.

**A skipped test says so, loudly, and skips nothing that does not need the
dependency.**  DuckDB is optional on a developer's machine and mandatory on the
server, so the tests that need a connection self-skip and the tests that do not
-- the pinned column list, the SQL text, the refusals, the loud-failure
contract -- always run.  A suite that silently drops half its assertions when a
module is missing is the decoration this project keeps writing tests against.
"""

import json
import re
import os
import shutil
import tempfile

import fixtures as fx

from srv import duck, query as Q, store, wire

TMPDIRS = []


def _have_duckdb():
    return store.available()


def _store_root(rows=None, samples=None, extra_lines=None):
    """A store laid out exactly as `serve.Store` lays one out.

    Written from `fx.stamped_rows()` -- the 8 REAL ledger rows with the
    shipper's own `account_uuid` stamp -- through `json.dumps` in the compact
    form `cu/ledger.py` writes, so the bytes on disk are the bytes the door
    would have appended.
    """
    root = tempfile.mkdtemp(prefix="duckstore-")
    TMPDIRS.append(root)
    rows = fx.stamped_rows() if rows is None else rows
    by = {}
    for r in rows:
        by.setdefault(r.get("account_uuid"), []).append(r)
    for uuid, rs in by.items():
        d = os.path.join(root, "accounts", uuid or "unplaced")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "ledger.jsonl"), "w", encoding="utf-8") as fh:
            for r in rs:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
            for extra in (extra_lines or []):
                fh.write(extra)
    for uuid, lines in (samples or {}).items():
        d = os.path.join(root, "accounts", uuid)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "samples.jsonl"), "wb") as fh:
            for ln in lines:
                fh.write(ln + b"\n")
    return root


def cleanup():
    for d in TMPDIRS:
        shutil.rmtree(d, ignore_errors=True)


# ===================================================== 1. the pinned schema ==

def test_duck_columns_are_derived_from_wire_not_typed_here(check, check_true):
    """The pinned list IS `wire.LEDGER_FIELDS`, not a copy of it.

    Measured on 1.5.5: a column absent from `read_json_auto`'s sampled prefix
    is dropped from the relation entirely -- 29 columns where 30 were meant,
    `SELECT *` a column narrower, no error anywhere.  Pinning is the fix and a
    hand-typed pin is the same defect one level up, so the key set is asserted
    against `wire` and the day a column is added the test fails before the
    reader silently stops carrying it.
    """
    check("duck: the pinned ledger columns are exactly wire's",
          tuple(sorted(duck.LEDGER_SQL_TYPES)),
          tuple(sorted(tuple(wire.LEDGER_FIELDS) + (wire.LEDGER_STAMP,))))
    check("duck: the pinned column ORDER is query.LEDGER_COLUMNS'",
          tuple(duck.LEDGER_SQL_TYPES), tuple(Q.LEDGER_COLUMNS))
    check("duck: the pinned sample columns are exactly wire's v2 + conditional",
          tuple(sorted(duck.SAMPLE_SQL_TYPES)),
          tuple(sorted(tuple(wire.SAMPLE_FIELDS_V2)
                       + tuple(wire.SAMPLE_FIELDS_CONDITIONAL))))


def test_duck_types_follow_query_pys_classification_by_evidence(check):
    """Every never-observed column is `JSON`; every observed numeric is numeric.

    `event_sequence` is `a.get("event.sequence")` straight off the payload and
    nothing has ever stated what arrives in it.  Pinning it BIGINT because the
    name ends in "sequence" is the assumption-written-as-a-value this project
    keeps apologising for -- and it would not even fail loudly: a pinned BIGINT
    COERCES, measured, so `1.5` arrives as `2`, `"7"` as `7` and `true` as `1`.
    `JSON` keeps the value and its type.
    """
    wrong = [c for c in Q.NEVER_OBSERVED
             if duck.LEDGER_SQL_TYPES.get(c) != "JSON"]
    check("duck: every NEVER_OBSERVED column is pinned JSON", wrong, [])
    num = [c for c in Q.OBSERVED_NUMERIC
           if duck.LEDGER_SQL_TYPES.get(c) not in ("BIGINT", "DOUBLE")]
    check("duck: every OBSERVED_NUMERIC column is pinned numeric", num, [])
    txt = [c for c in Q.OBSERVED_TEXT
           if duck.LEDGER_SQL_TYPES.get(c) != "VARCHAR"]
    check("duck: every OBSERVED_TEXT column is pinned VARCHAR", txt, [])
    check("duck: stream A's tags is a structure", duck.LEDGER_SQL_TYPES["tags"],
          "JSON")
    check("duck: stream B's tags is a comma-separated STRING, not a structure",
          duck.SAMPLE_SQL_TYPES["tags"], "VARCHAR")


def test_duck_never_omits_the_columns_argument(check, check_true):
    """`relation()` cannot be talked into inference.

    There is no code path that emits `read_json_auto`, because that is the one
    that drops a column in silence.  A grep, deliberately, and not a
    behavioural test: the defect is the ABSENCE of `columns=`, and the only way
    to assert an absence is to look.
    """
    src = open(os.path.join(fx.SRV_DIR, "srv", "duck.py"),
               encoding="utf-8").read()
    body = src.split('"""', 2)[-1]          # past the module docstring
    check("duck: no read_json_auto on any code path", "read_json_auto" in body,
          False)
    params = duck.Params()
    rel = duck.relation("/tmp/x.jsonl", duck.LEDGER_SQL_TYPES, params)
    check_true("duck: relation() pins columns", "columns=$" in rel)
    check_true("duck: relation() tolerates a torn line", "ignore_errors" in rel)
    # The map is a BOUND value now, so pinning is asserted on what was bound
    # rather than on the text: a narrower map is the silent column drop this
    # whole test exists for, and it would still read as `columns=$p2`.
    bound = [v for v in params.values.values() if isinstance(v, dict)]
    check("duck: exactly one column map is bound", len(bound), 1)
    check("duck: and it is the whole pinned schema", bound[0],
          dict(duck.LEDGER_SQL_TYPES))


def test_duck_every_row_query_excludes_the_phantom_row(check, check_true):
    """Asserted on the SQL text, because behaviour cannot see this one.

    A malformed line is ALL-NULL, so it has no `ts` either, and `select_sql`'s
    `ts IS NOT NULL` -- which is there to make the ordering total -- excludes
    it as a side effect.  Removing the malformed guard therefore changes no
    answer today and the mutation survives every behavioural test, which is
    precisely the shape of a guard that rots.  So it is pinned where it lives:
    the day somebody adds "show me the rows with no usable timestamp", the two
    stop coinciding and this line is the only thing between a reader and a row
    of nulls rendered as a request.
    """
    q = Q.compile_query({})
    sql, _cols = duck.select_sql("REL", q, duck.Params())
    check_true("duck: the row query excludes the malformed set by name",
               "NOT (%s)" % duck.MALFORMED_PREDICATE in sql)
    check_true("duck: ...separately from the ordering guard",
               "\"ts\" IS NOT NULL" in sql)
    acc, _labels = duck.accounting_sql("REL", q, duck.Params())
    check_true("duck: and so does every count in the accounting",
               acc.count("NOT (%s)" % duck.MALFORMED_PREDICATE) >= 2)


def test_duck_text_search_never_matches_a_number_in_a_json_column(check,
                                                                  check_true,
                                                                  skip):
    """`json_extract_string(x, '$')` over a JSON `5` returns the STRING '5'.

    Measured.  So without the `json_type(...) = 'VARCHAR'` guard a free-text
    search for a digit matches a numeric value that the pure path's
    `isinstance(v, str)` rejects -- the two engines disagreeing on real data
    the first time anybody searches for a version number.  The seven
    never-observed columns are the ones at risk, because they are the JSON ones.
    """
    if not _have_duckdb():
        return skip("duck: json text search (duckdb not installed)")
    rows = NEVER_OBSERVED_ROWS
    st = store.DuckStore(_store_root(rows=rows))
    q = Q.compile_query({"text": "5", "text_in": ("event_sequence",)})
    got = st.select(q)
    pure = Q.select(q, rows)
    check("duck/pure: a digit search hits the string and not the number",
          sorted(r["request_id"] for r in got.rows),
          sorted(r["request_id"] for r in pure.rows))
    check("duck: and that is exactly one row -- the string one",
          [r["request_id"] for r in got.rows], ["e5e50000000000a2"])


def test_duck_malformed_predicate_is_the_content_hash(check):
    """`request_id IS NULL` is the malformed set, and that is not a guess.

    `cu/otlp.py` computes `request_id` for every row it writes, so a NULL there
    can only be the all-NULL row `ignore_errors=true` emits for a line it could
    not parse -- measured: 1000 good records plus one torn fragment gives
    `count(*) = 1001` and the extra row is all NULLs.
    """
    check("duck: the malformed predicate", duck.MALFORMED_PREDICATE,
          "request_id IS NULL")
    check_true = lambda n, c: check(n, bool(c), True)              # noqa: E731
    check_true("duck: request_id is a ledger column",
               "request_id" in Q.LEDGER_COLUMNS)


# ================================================== 2. the SQL, as text ======

HOSTILE = "x' OR 1=1 --"


def test_duck_binds_every_value_and_writes_none_into_the_sql(check, check_true):
    """Not one value reaches DuckDB as text.  Asserted by looking.

    Escaping is a promise that a function was called on every value; binding
    is a promise that no value was in the string to begin with, and only the
    second can be checked by reading the string.  So this builds SQL from a
    query carrying a hostile value in EVERY value position there is -- account
    uuid, both time bounds, a `where`, a tag key, a `not_where`, a range, free
    text, the cursor and the LIMIT, plus the file path itself -- and asserts
    that none of them appears anywhere in the SQL.

    The placeholder count is asserted against the parameter count for a second
    reason: DuckDB refuses a dict carrying a name the statement does not use,
    so a fragment built and then dropped is a loud failure rather than a
    quietly wider answer, and this is that property pinned where it is cheap.
    """
    spec = {"account_uuid": HOSTILE + "-acct", "since": 1.5, "until": 2.5,
            "where": {"model": HOSTILE, 'tags.a"b': HOSTILE + "-tag"},
            "not_where": {"host": HOSTILE + "-host"},
            "ranges": {"duration_ms": (3, 4)}, "text": HOSTILE + "-text"}
    q = Q.compile_query(spec)
    check_true("duck: the hostile spec compiles to a query", not Q.refused(q))

    params = duck.Params()
    rel = duck.relation("/tmp/" + HOSTILE + ".jsonl", duck.LEDGER_SQL_TYPES,
                        params)
    sql, _cols = duck.select_sql(rel, q, params, limit=7,
                                 after=(1.0, HOSTILE + "-cursor"))

    for v in params.values.values():
        if isinstance(v, str):
            check("duck: %r is bound, never written into the SQL" % v[:20],
                  v in sql, False)
    # A value is bound in the form its COMPARISON needs, which is not always
    # the form it arrived in -- and that is worth asserting rather than
    # working around, because each transformation is a place a value could
    # have been mangled instead: a tag value is compared against JSON text, a
    # text needle is compared as a lowercased LIKE pattern, and a tag KEY
    # becomes a JSON path.  All of them are still parameters.
    bound = list(params.values.values())
    for what, expected in (
            ("the model value", HOSTILE),
            ("the account uuid", HOSTILE + "-acct"),
            ("the not_where host", HOSTILE + "-host"),
            ("the cursor id", HOSTILE + "-cursor"),
            ("the file path", "/tmp/" + HOSTILE + ".jsonl"),
            ("the tag value, as JSON text", json.dumps(HOSTILE + "-tag")),
            ("the tag key, as a JSON path", '$."a\\"b"'),
            ("the text needle, as a LIKE pattern",
             "%" + (HOSTILE + "-text").lower() + "%")):
        check_true("duck: %s reached the parameters" % what, expected in bound)
    check("duck: the LIMIT is bound too", 7 in params.values.values(), True)
    check("duck: and so are the time bounds",
          1.5 in params.values.values() and 2.5 in params.values.values(),
          True)
    names = set(re.findall(r"\$p\d+", sql))
    check("duck: every placeholder in the SQL has a parameter",
          names, set("$" + k for k in params.values))


def test_duck_a_bound_value_is_matched_as_data(check, check_true, skip):
    """And the hostile value comes back as a row, which is the other half.

    A layer that refused the value, or dropped it, or escaped it into
    something else would also produce "no injection".  The only proof that
    binding is correct rather than merely safe is that the value still
    selects its own row, and that the pure oracle selects the same one.
    """
    if not _have_duckdb():
        return skip("duck: hostile values round-trip (duckdb not installed)")
    rows = ROWS + [fx.derive("duck-hostile-model",
                             "a real row whose model is a SQL fragment",
                             ROWS[0], request_id="badbad0000000001",
                             model=HOSTILE)]
    root = _store_root(rows=rows)
    st = store.DuckStore(root)
    q = Q.compile_query({"where": {"model": HOSTILE}})
    got = st.select(q)
    pure = Q.select(q, rows)
    check("duck: the hostile value selects its own row", got.matched, 1)
    check("duck: and the oracle agrees", got.matched, pure.matched)
    check("duck: the row that came back is that row",
          got.rows[0]["model"], HOSTILE)
    check("duck: the store still answers afterwards",
          st.select(Q.compile_query({})).matched, len(rows))


def test_duck_an_identifier_cannot_be_bound_so_the_list_is_its_only_defence(
        check, check_true, skip):
    """The measurement here is the opposite of reassuring, so it is pinned.

    Binding a column NAME does not fail: `SELECT $c` with `c="model"` returns
    the constant string 'model', once per row -- a plausible column of data
    answering a question nobody asked.  So an identifier cannot be defended by
    binding at any price, and a whitelist is the only line -- which is why
    `_ident` now ENFORCES it rather than asserting it in a docstring.  It used
    to escape whatever it was handed and say "only ever reached with a checked
    column name", and `/api/v1/values?fields=` reached it with an unchecked
    one: the doubling held, and the answer was still a 500 quoting the
    generated SQL back at a caller who had made a typo.
    """
    for name in ("model; DROP TABLE x", 'model" , "', "nonsense"):
        bad = Q.compile_query({"where": {name: 1}})
        check_true("duck: %r is refused before any SQL" % name[:18],
                   Q.refused(bad))
        check("duck: ...by name", bad.reason, "unknown-column")
        raised = None
        try:
            duck._ident(name)
        except duck.UncheckedIdentifier as exc:
            raised = exc
        check_true("duck: ...and _ident refuses it too rather than escaping "
                   "it: %r" % name[:18], raised is not None)
    check("duck: the escape is still there behind the whitelist",
          duck._quote_ident('a"b'), '"a""b"')
    check_true("duck: and a pinned column passes both",
               duck._ident("model") == '"model"')
    if not _have_duckdb():
        return skip("duck: a bound identifier is a string (duckdb not "
                    "installed)")
    con = store.require().connect()
    check("duck: a bound identifier is a CONSTANT, not a column",
          con.execute("SELECT $c", {"c": "model"}).fetchall(), [("model",)])


def test_duck_an_infinite_time_bound_is_an_answer_not_a_crash(
        check, check_true, skip):
    """The defect binding removed, kept as its regression test.

    `since=-inf` is an ordinary way to say "no lower bound" and `serve.py`'s
    `_num_param` accepts it today, because `float("-inf")` parses and
    `wire._is_number` is true of it.  Interpolated, `repr(float("-inf"))` is
    `-inf`, which DuckDB reads as a COLUMN NAME: the query matched all 8 real
    rows through `query.py` and raised `Binder Error: Referenced column "inf"
    not found` through this one.

    The `-inf` case is the one that matters -- it is the shape that RETURNS
    rows, so a reader would have had an answer and a traceback for the same
    question -- and the int case is here because DuckDB refuses an integer
    beyond 128 bits outright while Python compares it perfectly well.

    NaN is NOT one of these and is refused instead; see the test below.  It
    used to be in this list, in the `since` spelling only -- the one where the
    two engines happen to agree by both eliminating everything -- while
    `until=nan` returned the whole corpus through this engine and nothing
    through the oracle.
    """
    if not _have_duckdb():
        return skip("duck: infinite bounds (duckdb not installed)")
    st = store.DuckStore(_store_root())
    for label, spec in (("since=-inf", {"since": float("-inf")}),
                        ("since=inf", {"since": float("inf")}),
                        ("until=inf", {"until": float("inf")}),
                        ("until=-inf", {"until": float("-inf")}),
                        ("duration_ms >= 10**40",
                         {"ranges": {"duration_ms": (10 ** 40, None)}})):
        q = Q.compile_query(spec)
        check_true("duck: %s compiles" % label, not Q.refused(q))
        pure = Q.select(q, ROWS)
        got = st.select(q)
        check("duck: %s answers what the oracle answers" % label,
              got.matched, pure.matched)
    check("duck: and the unbounded one really does return the rows",
          st.select(Q.compile_query({"since": float("-inf")})).matched,
          len(ROWS))


def test_duck_every_statement_is_executed_with_its_parameters(check,
                                                              check_true):
    """A static guard, because no behavioural one can see this.

    A statement executed with no parameter dict is a statement whose values
    were written into its text -- which is how this layer started, and the
    form it would silently return to when somebody adds the next query.  The
    single exception is `SET threads=`, which carries a number from this
    process's own constructor and no value from any question; it is named here
    rather than excluded quietly.
    """
    import ast
    src = open(os.path.join(fx.SRV_DIR, "srv", "store.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    bare = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and len(node.args) < 2):
            seg = ast.get_source_segment(src, node) or ""
            bare.append(" ".join(seg.split())[:60])
    check("duck: every query statement carries its parameters",
          [b for b in bare if "threads" not in b], [])
    check_true("duck: the one exception is the thread setting, by name",
               any("threads" in b for b in bare))


def test_duck_clause_labels_match_the_oracles_exactly(check):
    """One vocabulary, not two.

    `filtered-to-nothing` names the clause that emptied the result, and a
    reader comparing an answer from this engine with one from the pure path
    must see the same string -- otherwise the same refusal reads as two
    different faults.
    """
    spec = {"account_uuid": "a", "since": 1.0, "until": 2.0,
            "where": {"model": "m"}, "not_where": {"host": "h"},
            "ranges": {"duration_ms": (0, 10)}, "text": "z"}
    q = Q.compile_query(spec)
    check("duck: clause labels are query.clauses_of's, in order",
          [l for l, _ in duck.clauses_sql(q, duck.Params())],
          [l for l, _ in Q.clauses_of(q)])


def test_duck_null_is_never_a_clause_value(check):
    """Every clause expression is TRUE or FALSE, never NULL.

    The elimination accounting counts a row as eliminated when the clause is
    not TRUE.  A three-valued clause would make "eliminated" and "could not
    decide" one number, which is the accounting producing the very confusion
    it exists to prevent.
    """
    q = Q.compile_query({"where": {"profile": None, "model": "m"}})
    for label, sql in duck.clauses_sql(q, duck.Params()):
        check_true_ = "IS NULL" in sql or "IS NOT NULL" in sql
        check("duck: clause %r is null-guarded" % label, check_true_, True)


# ============================================== 3. the loud-failure contract ==

def test_duckdb_absent_fails_loudly_and_names_the_install(check, check_true):
    """A missing dependency must never look like an empty result.

    "0 requests" is a perfectly plausible answer for an account that has not
    shipped yet, so a store that degraded into one would be indistinguishable
    from a working store over a quiet account -- the silent failure this
    project has audited twice.
    """
    check_true("store: the hint names pip install duckdb",
               "pip install duckdb" in store.INSTALL_HINT)
    check_true("store: the hint says usage/ still works without it",
               "usage/" in store.INSTALL_HINT
               and "offline" in store.INSTALL_HINT)

    class Boom(object):
        pass
    import builtins
    real_import = builtins.__import__

    def fake(name, *a, **k):
        if name == "duckdb":
            raise ImportError("no duckdb")
        return real_import(name, *a, **k)
    builtins.__import__ = fake
    try:
        raised = None
        try:
            store.require()
        except store.DuckDBMissing as exc:
            raised = exc
        check_true("store: require() raises DuckDBMissing", raised is not None)
        check_true("store: the exception carries the install command",
                   "pip install duckdb" in str(raised))
        check("store: available() is False without it", store.available(), False)
    finally:
        builtins.__import__ = real_import
    check("store: available() is True again once it can import",
          store.available(), _have_duckdb())


def test_nothing_under_usage_imports_duckdb(check):
    """The offline property, asserted rather than promised.

    `claudio usage` and everything under `usage/` read the same JSONL with the
    standard library: no DuckDB, no network, no server, no daemon.  That is
    most of why the tool is pleasant, and it is the property the storage pass
    defended explicitly -- so it is a test, not a paragraph.  DuckDB is
    permitted in exactly one place, and this is what keeps "one place" true.
    """
    usage = os.path.join(os.path.dirname(fx.SERVER), "usage")
    offenders = []
    for base, dirs, files in os.walk(usage):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", ".git")]
        for fn in files:
            p = os.path.join(base, fn)
            if fn.endswith(".pyc"):
                continue
            try:
                src = open(p, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if fn.endswith(".py") or src.startswith("#!"):
                for i, ln in enumerate(src.splitlines(), 1):
                    s = ln.strip()
                    if s.startswith("#"):
                        continue
                    if (s.startswith("import duckdb")
                            or s.startswith("from duckdb")
                            or "import duckdb" in s):
                        offenders.append("%s:%d" % (os.path.relpath(p, usage), i))
    check("usage/ imports duckdb nowhere -- the offline property", offenders, [])


# ==================================== 4. the differential test (needs duckdb) =

ROWS = fx.stamped_rows()        # one list, reused: `ingested_at` is a clock

# Declared at IMPORT time, not inside the test that uses them: the provenance
# ledger is asserted by a test whose name sorts before these, and a derivation
# registered later would be invisible to it -- a fixture quietly becoming "real
# data" through nothing worse than test ordering.
UNDATABLE_ROW = None            # filled in below, after the derivations


NEVER_OBSERVED_ROWS = [
    fx.derive("duck-numeric-never-observed",
              "a real stream-A row with a NUMBER in the never-observed "
              "event_sequence column", ROWS[0],
              request_id="e5e50000000000a1", event_sequence=5),
    fx.derive("duck-string-never-observed",
              "the same row with a STRING there instead", ROWS[0],
              request_id="e5e50000000000a2", event_sequence="5"),
]

# A row whose `ts` is unusable.  `reconcile` already made this mistake once --
# a ledger row with no usable `ts` was in no window's request set and in no
# `unclaimed` list either, so its weight landed silently in the residual -- and
# it is a third bucket on this side too: such a row MATCHES a query, belongs in
# a selection, and can take no position in a time ordering, so it is in no page.
UNDATABLE_ROWS = ROWS + [
    fx.derive("duck-undatable-request",
              "a real stream-A row whose ts is not a number", ROWS[0],
              request_id="d8d80000000000a1", ts=None),
]


def _pure_and_duck(spec, root, **kw):
    q = Q.compile_query(spec)
    pure = Q.select(q, ROWS)
    st = store.DuckStore(root)
    got = st.select(q, **kw)
    return q, pure, got, st


def test_duck_select_agrees_with_the_pure_oracle(check, check_true, skip):
    """Every filter shape, both engines, the same answer.

    Over the 8 REAL rows: three accounts, two models, two query sources, seven
    columns null in every row, and a `tags` dict that is empty in all but one.
    """
    if not _have_duckdb():
        return skip("duck: differential select (duckdb not installed)")
    root = _store_root()
    specs = [
        {},
        {"account_uuid": fx.uuid_of("agent")},
        {"where": {"model": "claude-sonnet-5"}},
        {"where": {"model": ["claude-sonnet-5", "claude-haiku-4-5"]}},
        {"where": {"agent_name": None}},
        {"not_where": {"query_source": "generate_session_title"}},
        {"ranges": {"input_tokens": (0, 100)}},
        {"text": "repl"},
        {"text": "REPL"},
        {"text": "iterm", "text_in": ("terminal_type",)},
        {"where": {"model": "nothing-like-this"}},
        {"since": 1786584400.0},
        {"until": 1786584400.0},
        {"account_uuid": fx.uuid_of("alpha"),
         "where": {"query_source": "repl_main_thread"}},
        # Every URL value is a STRING, and `api._resolve_value` passes one
        # through unchanged when the corpus holds no match -- its docstring
        # promises `select` will then report `filtered-to-nothing` naming the
        # clause.  Bound against a numeric column, DuckDB CAST it instead:
        # measured on 1.5.5, `529 = ' 529 '`, `529 = '+529'`,
        # `529 = '529.0'`, `18 = '0x12'` and `18 = '1_8'` are all TRUE, so the
        # filter matched rows it did not name, and `529 = 'abc'` RAISES, which
        # escaped as HTTP 500 over a typo.  The oracle matches none of the six.
        {"where": {"input_tokens": "abc"}},
        {"where": {"input_tokens": " 529 "}},
        {"where": {"input_tokens": "+529"}},
        {"where": {"input_tokens": "529.0"}},
        {"where": {"output_tokens": "0x12"}},
        {"where": {"output_tokens": "1_8"}},
        {"not_where": {"input_tokens": "abc"}},
        {"where": {"ts": "abc"}},
        # The reverse direction casts the COLUMN, so whether the query raises
        # depends on the data rather than on the question.
        {"where": {"model": 1}},
        {"not_where": {"model": 1}},
        # ...and a value that really is of the column's type still matches.
        {"where": {"input_tokens": 529}},
        {"where": {"model": "claude-haiku-4-5"}},
    ]
    for spec in specs:
        q, pure, got, st = _pure_and_duck(spec, root)
        name = json.dumps(spec, sort_keys=True, default=repr)
        # The ANSWER is identical, always.  That is the contract.
        check("duck/pure matched: %s" % name, got.matched, pure.matched)
        check("duck/pure row ids: %s" % name,
              sorted(r["request_id"] for r in got.rows),
              sorted(r["request_id"] for r in pure.rows))
        check("duck/pure empty_because: %s" % name,
              got.empty_because, pure.empty_because)
        if not spec.get("account_uuid"):
            # With no account named, both engines see every row, so the
            # ACCOUNTING is comparable too -- and it is compared, because the
            # per-clause elimination count is what tells `filtered-to-nothing`
            # from `no-data` and names the clause that did it.
            check("duck/pure scanned: %s" % name, got.scanned, pure.scanned)
            check("duck/pure eliminated: %s" % name,
                  dict(got.eliminated), dict(pure.eliminated))
            check("duck/pure sole_cause: %s" % name, got.sole_cause,
                  pure.sole_cause)
        else:
            # With an account named, the store opens ONE file -- the layout is
            # the narrowing -- so it scans fewer rows than the oracle and its
            # account clause eliminates none of them.  This is the
            # one-directional contract's good direction: the answer is the
            # same and the candidate set is smaller.  It is asserted rather
            # than excused, because a `scanned` that quietly meant something
            # different in one engine is exactly how two figures for one
            # quantity end up in one product.
            mine = [r for r in ROWS
                    if r["account_uuid"] == spec["account_uuid"]]
            check("duck: an account query scans only that account's file: %s"
                  % name, got.scanned, len(mine))
            check("duck: ...and its account clause therefore eliminates "
                  "nothing: %s" % name,
                  got.eliminated["account_uuid=%s" % spec["account_uuid"]], 0)
            check_true("duck: ...so a filtered-to-nothing still names the "
                       "clause that really did it: %s" % name,
                       got.empty_because != "filtered-to-nothing"
                       or got.sole_cause != "account_uuid=%s"
                       % spec["account_uuid"])


def test_duck_returns_the_row_the_core_reads(check, check_true, skip):
    """A materialised row is byte-equal to the row on disk, `tags` included.

    The seven never-observed columns are pinned JSON, so they come back as
    JSON TEXT and are decoded -- which is the whole reason they are not
    VARCHAR: `5` and `"5"` must still be different when somebody finally
    looks.  If this drifts, every downstream consumer sees a different shape
    from the one the reconciler reads, silently.
    """
    if not _have_duckdb():
        return skip("duck: row shape (duckdb not installed)")
    st = store.DuckStore(_store_root(rows=ROWS))
    q = Q.compile_query({"account_uuid": fx.uuid_of("agent")})
    got = st.select(q)
    want = {r["request_id"]: r for r in ROWS
            if r["account_uuid"] == fx.uuid_of("agent")}
    for row in got.rows:
        check("duck: row %s is the row on disk" % row["request_id"][:8],
              row, want[row["request_id"]])


def test_duck_an_undatable_row_is_in_the_selection_and_in_no_page(
        check, check_true, skip):
    """Three places a row can be, and a row with no `ts` is in two of them.

    `select` returns it and counts it; no page can order it.  Reporting
    `undatable_n: 0` while holding one is the confident-wrong-number failure
    at its smallest and least visible scale -- the count is the only thing
    telling a reader that the rows in a page do not add up to the rows that
    matched.
    """
    if not _have_duckdb():
        return skip("duck: undatable rows (duckdb not installed)")
    st = store.DuckStore(_store_root(rows=UNDATABLE_ROWS))
    q = Q.compile_query({"account_uuid": fx.uuid_of("agent")})
    got = st.select(q)
    pure = Q.select(q, [r for r in UNDATABLE_ROWS
                        if r["account_uuid"] == fx.uuid_of("agent")])
    check("duck/pure: the undatable row is IN the selection",
          sorted(r["request_id"] for r in got.rows),
          sorted(r["request_id"] for r in pure.rows))
    check("duck/pure: and is counted as undatable",
          len(got.undatable), len(pure.undatable))
    check("duck: which is one of them", len(got.undatable), 1)
    check_true("duck: and the selection says so in a note",
               any("no usable timestamp" in n for n in got.notes))
    pg = st.page(q, limit=50)
    check("duck: no page can order it, so it is in none",
          [r["request_id"] for r in pg.items
           if r["request_id"] == "d8d80000000000a1"], [])
    check("duck: and every page counts it", pg.undatable_n, 1)
    bd = st.breakdown(q, "model")
    check("duck: a breakdown counts it too, rather than reporting zero",
          bd.undatable_n, 1)


def test_duck_page_is_the_same_page(check, check_true, skip):
    """Keyset paging, cursor for cursor, against the pure path.

    The cursor format is shared on purpose: two engines that disagreed about a
    cursor would skip rows between pages in silence, which is the one failure
    keyset paging exists to prevent.
    """
    if not _have_duckdb():
        return skip("duck: differential paging (duckdb not installed)")
    root = _store_root()
    st = store.DuckStore(root)
    rows = fx.stamped_rows()
    for order in ("asc", "desc"):
        q = Q.compile_query({"order": order})
        cur = None
        seen_d, seen_p = [], []
        for _ in range(6):
            pd = st.page(q, cursor=cur, limit=3)
            pp = Q.paginate(q, rows, cursor=cur, limit=3)
            check("duck/pure page items (%s)" % order,
                  [r["request_id"] for r in pd.items],
                  [r["request_id"] for r in pp.items])
            check("duck/pure has_more (%s)" % order, pd.has_more, pp.has_more)
            check("duck/pure next_cursor (%s)" % order,
                  pd.next_cursor, pp.next_cursor)
            check("duck/pure matched on every page (%s)" % order,
                  pd.matched, pp.matched)
            seen_d += [r["request_id"] for r in pd.items]
            seen_p += [r["request_id"] for r in pp.items]
            cur = pd.next_cursor
            if cur is None:
                break
        check("duck/pure whole walk (%s)" % order, seen_d, seen_p)
        check("duck: the walk visits every row once (%s)" % order,
              sorted(seen_d), sorted(r["request_id"] for r in rows))


def test_duck_breakdown_agrees_bucket_for_bucket(check, check_true, skip):
    if not _have_duckdb():
        return skip("duck: differential breakdown (duckdb not installed)")
    root = _store_root()
    st = store.DuckStore(root)
    rows = fx.stamped_rows()
    for label in ("agent", "alpha"):
        uuid = fx.uuid_of(label)
        mine = [r for r in rows if r["account_uuid"] == uuid]
        for by in ("model", "query_source", "prompt_id", "agent_name",
                   "session_id", "host"):
            q = Q.compile_query({"account_uuid": uuid})
            bd = st.breakdown(q, by)
            bp = Q.breakdown(mine, by, account_uuid=uuid)
            check("duck/pure buckets %s/%s" % (label, by),
                  {repr(k): v for k, v in bd.buckets.items()},
                  {repr(k): v for k, v in bp.buckets.items()})
            check("duck/pure column_status %s/%s" % (label, by),
                  bd.column_status, bp.column_status)
            check("duck/pure overhead %s/%s" % (label, by),
                  bd.overhead, bp.overhead)


def test_duck_facets_and_availability_agree(check, check_true, skip):
    if not _have_duckdb():
        return skip("duck: differential facets (duckdb not installed)")
    st = store.DuckStore(_store_root())
    rows = fx.stamped_rows()
    q = Q.compile_query({})
    # `pooled=True` because that is exactly what this comparison is: the pure
    # side is `Q.facets(rows)` over a row list the test chose itself, and the
    # pure layer has no notion of a file or an account.  The differential is
    # about SQL and Python agreeing on how a facet is COMPUTED.  Whether the
    # question may be ASKED across accounts is a different property, asserted
    # immediately below -- and by `test_duck_refuses_every_cross_account_total`
    # and the API's own route tests.
    fd = st.facets(q, pooled=True)
    fp = Q.facets(rows)
    check("duck/pure facet columns", sorted(fd), sorted(fp))
    for col in sorted(fp):
        check("duck/pure facet values: %s" % col,
              sorted(map(repr, fd.get(col, []))),
              sorted(map(repr, fp[col])))
    ad = st.field_availability(q, pooled=True)["columns"]
    ap = Q.field_availability(rows)["columns"]
    for col in Q.LEDGER_COLUMNS:
        check("duck/pure present: %s" % col,
              ad[col]["present"], ap[col]["present"])
        check("duck/pure distinct_n: %s" % col,
              ad[col]["distinct_n"], ap[col]["distinct_n"])
        check("duck/pure never_observed: %s" % col,
              ad[col]["never_observed"], ap[col]["never_observed"])
    check_true("duck: `absent` is None, not 0 -- a pinned read cannot tell an "
               "absent key from an explicit null",
               all(ad[c]["absent"] is None for c in Q.LEDGER_COLUMNS))

    # And WITHOUT the opt-out both refuse, because pooling three accounts is
    # the cross-account count `/aggregate` refuses by name.  It is a count and
    # not a "total", which is why the key-name walk in `test_all.py` could not
    # see it and why `/api/v1/values` served it for as long as it existed.
    for label, got in (("facets", st.facets(q)),
                       ("field_availability", st.field_availability(q))):
        check("duck: %s refuses to pool accounts unasked" % label,
              getattr(got, "reason", None), "crosses-accounts")
    # The opt-out is not a way to ask the forbidden question: naming an
    # account answers it, so the refusal has a real alternative.
    one = sorted(st.paths.accounts())[0]
    check_true("duck: ...and naming one account answers it",
               not Q.refused(st.facets(q, account_uuid=one)))


# ================================== 5. the guardrails SQL would have collapsed

def test_duck_keeps_the_three_empty_answers_apart(check, check_true, skip):
    """SQL returns zero rows for all three.  These are three different values.

    `no-data` is a store with nothing in it.  `filtered-to-nothing` is a store
    with rows and a clause that removed them, and it NAMES the clause.
    `Unanswerable` is a question with no answer at all.  A reader that showed
    one message for all three would send somebody looking for a shipping fault
    when their filter was wrong, and vice versa.
    """
    if not _have_duckdb():
        return skip("duck: three empty answers (duckdb not installed)")
    empty_root = tempfile.mkdtemp(prefix="duckstore-empty-")
    TMPDIRS.append(empty_root)
    os.makedirs(os.path.join(empty_root, "accounts"), exist_ok=True)
    st_empty = store.DuckStore(empty_root)
    q = Q.compile_query({})
    sel = st_empty.select(q)
    check("duck: an empty store is no-data", sel.empty_because, "no-data")
    check("duck: no-data blames no clause", sel.sole_cause, None)

    st = store.DuckStore(_store_root())
    q2 = Q.compile_query({"where": {"model": "no-such-model"}})
    s2 = st.select(q2)
    check("duck: a filter that removes everything is filtered-to-nothing",
          s2.empty_because, "filtered-to-nothing")
    check("duck: and it names the clause that did it", s2.sole_cause,
          "model in ['no-such-model']")
    check_true("duck: the note names the clause", any(
        "no-such-model" in n for n in s2.notes))

    bad = Q.compile_query({"where": {"nonsense": 1}})
    check_true("duck: an unknown column is Unanswerable, not zero rows",
               Q.refused(bad))
    check("duck: and it is named", bad.reason, "unknown-column")
    check("duck: a refused query passes straight through the store",
          st.select(bad).reason, "unknown-column")


def test_duck_refuses_every_cross_account_total(check, check_true, skip):
    """No cross-account total, not even for tokens, and the refusal is by name.

    Three accounts in the real capture and one `usage_dir` holding all of them
    interleaved.  A query engine would have summed them without a word; the
    percentages have different denominators and often different people behind
    them.
    """
    if not _have_duckdb():
        return skip("duck: cross-account refusal (duckdb not installed)")
    st = store.DuckStore(_store_root())
    q = Q.compile_query({})
    r = st.breakdown(q, "model")
    check("duck: grouping across accounts is refused", getattr(r, "reason", None),
          "crosses-accounts")
    for by in Q.LABEL_COLUMNS:
        r2 = st.breakdown(q, by)
        check("duck: grouping across accounts by the label `%s`" % by,
              getattr(r2, "reason", None), "label-is-not-an-identity")
    r3 = st.breakdown(q, "account_uuid")
    check_true("duck: grouping BY account_uuid is allowed -- one bucket is one "
               "account and there is no total row",
               not Q.refused(r3))
    per = st.breakdown_per_account(q, "model")
    check_true("duck: breakdown_per_account gives one Breakdown per account",
               all(not Q.refused(v) for v in per.values()))
    check("duck: breakdown_per_account returns one per account, and the "
          "return value is a mapping with no total row in it",
          sorted(per), sorted(st.paths.accounts()))
    # The only keys that may say `total` are totals WITHIN one bucket or one
    # window -- the same whitelist the payload test one section up applies.
    stray = []
    for uuid, bd in per.items():
        for k in bd.as_dict():
            if "total" in k and k not in ("rows_total",):
                stray.append("%s.%s" % (uuid, k))
    check("duck: no key spanning accounts is called a total", stray, [])
    # And behaviourally, which is what a grep cannot show: with more than one
    # account in the store, EVERY aggregate either names an account or
    # refuses.  A query engine would have added them up without a word.
    unguarded = []
    for by in ("model", "query_source", "host", "account", "email",
               "prompt_id", "session_id", "tags.test"):
        r = st.breakdown(Q.compile_query({}), by)
        if not Q.refused(r) and r.account_uuid is None:
            unguarded.append(by)
    check("duck: no aggregate spans accounts without naming one", unguarded, [])


def test_duck_coverage_travels_with_every_aggregate(check, check_true, skip):
    """An attributed figure without its coverage is the confident wrong number.

    And today the honest coverage is `None` -- "nobody said" -- because nothing
    in this repository emits an attestation.  `0.0` would be the claim that
    nobody was listening, which is a positive statement about a machine.
    """
    if not _have_duckdb():
        return skip("duck: coverage on aggregates (duckdb not installed)")
    st = store.DuckStore(_store_root())
    uuid = fx.uuid_of("agent")
    q = Q.compile_query({"account_uuid": uuid})
    bd = st.breakdown(q, "model")
    check_true("duck: a breakdown carries a CoverageStatement",
               isinstance(bd.coverage, Q.CoverageStatement))
    check("duck: with no report, coverage.fraction is None, never 0.0",
          bd.coverage.fraction, None)
    check("duck: and the basis says why -- nobody supplied a reconciliation, "
          "which is not the same as nobody attesting",
          bd.coverage.basis, Q.no_report_coverage([]).basis)
    pg = st.page(q, limit=2)
    check_true("duck: a page carries one too",
               isinstance(pg.coverage, Q.CoverageStatement))
    check("duck: unknown is not zero on a page either",
          pg.coverage.fraction, None)
    check_true("duck: the lower-bound note exists and says `lower bound`",
               "LOWER BOUND" in store.LOWER_BOUND_NOTE.upper()
               and "max" in store.LOWER_BOUND_NOTE)


def test_duck_counts_a_torn_line_and_never_serves_it(check, check_true, skip):
    """`ignore_errors=true` emits a row of NULLs, it does not skip the line.

    Measured: 1000 good records plus one torn fragment gives `count(*) = 1001`
    and the extra row is all NULLs.  A torn final line is a write in progress
    and `serve.Store._append` guards against gluing a fragment onto a good
    record precisely because a process killed mid-append leaves one -- so this
    is the ordinary state of a live store, not a corner case.  Without the
    exclusion the store over-reports its own row count for ever.
    """
    if not _have_duckdb():
        return skip("duck: torn line (duckdb not installed)")
    root = _store_root(extra_lines=['{"request_id":"deadbeef","ts":178658'])
    st = store.DuckStore(root)
    q = Q.compile_query({})
    sel = st.select(q, account_uuid=fx.uuid_of("agent"))
    mine = [r for r in fx.stamped_rows()
            if r["account_uuid"] == fx.uuid_of("agent")]
    check("duck: the phantom row is not scanned", sel.scanned, len(mine))
    check("duck: the phantom row is not matched", sel.matched, len(mine))
    check("duck: the phantom row is not served",
          [r for r in sel.rows if r.get("request_id") is None], [])
    probs = [p for p in st.problems if p["reason"] == "unparseable-lines"]
    check("duck: and it is reported as a store problem", len(probs), 1)
    check("duck: with a count", probs[0]["count"], 1)


def test_duck_a_nan_bound_is_refused_rather_than_answered_oppositely(
        check, check_true, skip):
    """NaN parsed everywhere and ordered oppositely in the two engines.

    `float("nan")` parses, `wire._is_number` is true of it, and `until < since`
    is false for NaN, so nothing a bound had ever checked caught it.  Then
    Python's `ts <= nan` is False for every row while DuckDB's total float
    ordering puts NaN above infinity, so `ts < $nan` is TRUE for every row:
    over the 8 real rows `until=nan` gave the oracle 0 and this engine 8,
    `outcome: ok`, no note.  The mirror image lives in `ranges`, where
    `(nan, None)` keeps every row on the Python side and none here.

    `since=nan` used to be in the infinite-bounds list above, agreeing by
    coincidence -- both engines eliminate everything -- which is why the
    diverging spelling was never tried.  Both are refused now, and the
    refusal is `compile_query`'s so every caller inherits it.
    """
    for label, spec in (("until=nan", {"until": float("nan")}),
                        ("since=nan", {"since": float("nan")}),
                        ("both", {"since": float("nan"),
                                  "until": float("nan")}),
                        ("ranges ts (nan, None)",
                         {"ranges": {"ts": (float("nan"), None)}}),
                        ("ranges ts (None, nan)",
                         {"ranges": {"ts": (None, float("nan"))}})):
        q = Q.compile_query(spec)
        check_true("duck: %s is refused before any SQL" % label, Q.refused(q))
        check("duck: ...by name (%s)" % label, q.reason, "range-not-a-number")
        check_true("duck: ...with a remedy that says what a bound is (%s)"
                   % label, "bound" in (q.remedy or ""))
    check_true("duck: and the infinities still compile, because they are "
               "ordinary no-bound spellings",
               not Q.refused(Q.compile_query({"until": float("-inf")})))

    # A cursor is the third way in, and the only one that survives a
    # fingerprint check: editing the ts field of a REAL cursor keeps the
    # fingerprint and the order, so it still matches the query.
    q = Q.compile_query({"order": "desc"})
    cur = Q.encode_cursor(q, {"ts": 1786584663.0, "request_id": "x"})
    parts = cur.split("|")
    parts[3] = "nan"
    bad = Q.decode_cursor(q, "|".join(parts))
    check_true("duck: a NaN cursor position is refused", Q.refused(bad))
    check("duck: ...by name", bad.reason, "cursor-unreadable")
    check_true("duck: while the cursor it was made from still decodes",
               not Q.refused(Q.decode_cursor(q, cur)))
    if not _have_duckdb():
        return skip("duck: nan bounds (duckdb not installed)")
    st = store.DuckStore(_store_root())
    got = st.select(Q.compile_query({"until": float("nan")}))
    check_true("duck: and the store refuses it rather than answering",
               Q.refused(got))


def test_duck_a_wrong_typed_value_matches_nothing_rather_than_being_cast(
        check, check_true, skip):
    """`FALSE`, not a CAST -- which is what the oracle does.

    The differential list above already runs every shape end to end; this
    pins the mechanism, because the shapes agree today for a reason a
    refactor can remove.  `comparable` is the whole of it: a string against a
    numeric column and a non-string against a VARCHAR column are values the
    oracle's `isinstance` comparison can never match, so the SQL says so
    outright instead of asking DuckDB to convert one side.
    """
    check("duck: a string is not comparable to a BIGINT column",
          duck.comparable("input_tokens", " 529 "), False)
    check("duck: a number is not comparable to a VARCHAR column",
          duck.comparable("model", 1), False)
    check_true("duck: a number IS comparable to a numeric column",
               duck.comparable("input_tokens", 529))
    check_true("duck: a string IS comparable to a VARCHAR column",
               duck.comparable("model", "claude-sonnet-5"))
    check_true("duck: and a JSON column takes anything, because it compares "
               "as JSON", duck.comparable("event_sequence", "5")
               and duck.comparable("tags", 5))
    p = duck.Params()
    sql = duck._eq_sql("input_tokens", ["abc"], p)
    check("duck: the wrong-typed value is not bound at all", p.values, {})
    check_true("duck: and the clause is a constant FALSE", "FALSE" in sql)
    check("duck: no CAST is written anywhere", "CAST" in sql.upper(), False)
    if not _have_duckdb():
        return skip("duck: wrong-typed values (duckdb not installed)")
    st = store.DuckStore(_store_root())
    q = Q.compile_query({"where": {"input_tokens": "abc"}})
    sel = st.select(q)
    check("duck: a typo is `filtered-to-nothing`, not an exception",
          sel.empty_because, "filtered-to-nothing")
    check("duck: ...naming the clause a caller can change",
          sel.sole_cause, "input_tokens in ['abc']")


def test_duck_counts_a_value_the_pinned_type_could_not_hold(check, check_true,
                                                            skip):
    """The half of `ignore_errors=true` that no counter saw.

    A malformed LINE becomes a row of NULLs and is caught by
    `request_id IS NULL`.  A well-formed line whose VALUE does not fit the pin
    has that value nulled and the row kept, so `request_id` is intact, the
    malformed predicate never fires and nothing moves.  Measured against the
    real fixtures: `9223372036854775808`, `1e30` and `"abc"` in `input_tokens`
    each gave `scanned=2, matched=2, problems=[]`.
    """
    if not _have_duckdb():
        return skip("duck: coerced values (duckdb not installed)")
    rows = list(fx.stamped_rows())
    uuid = rows[0]["account_uuid"]
    base = {"request_id": "c0e0000000000000", "ts": 1786584663.0,
            "account_uuid": uuid, "model": "claude-sonnet-5"}
    shapes = [
        ("nulled: past BIGINT", {"input_tokens": 9223372036854775808}),
        ("nulled: 1e30", {"input_tokens": 1e30}),
        ("nulled: a word", {"input_tokens": "abc"}),
        ("converted: a numeric string", {"input_tokens": "7"}),
        ("converted: a boolean", {"input_tokens": True}),
        ("converted: a fraction", {"input_tokens": 1.5}),
        ("stringified: a structure", {"model": {"a": 1}}),
        ("datable: a timestamp as text", {"ts": "2026-08-12T10:00:00Z"}),
    ]
    for i, (_n, over) in enumerate(shapes):
        rows.append(dict(base, request_id="c0e0%012d" % i, **over))
    st = store.DuckStore(_store_root(rows=rows))

    sel = st.select(Q.compile_query({"account_uuid": uuid}))
    check("duck: not one of them is malformed -- the row survives whole",
          [p for p in st.problems if p["reason"] == "unparseable-lines"], [])

    got = st.coerced_values(uuid)
    by = {c["column"]: c["rows"] for c in got["columns"]}
    check("duck: every shape is counted, per column",
          by, {"input_tokens": 6, "model": 1, "ts": 1})
    check_true("duck: and it says the pin", 
               [c for c in got["columns"]
                if c["column"] == "input_tokens"][0]["pinned_as"] == "BIGINT")
    check_true("duck: it is reported as a store problem, so it travels",
               any(p["reason"] == "coerced-values" for p in st.problems))
    check("duck: with the total", 
          [p for p in st.problems if p["reason"] == "coerced-values"][0]
          ["count"], 8)

    clean = store.DuckStore(_store_root())
    check("duck: and a store whose values all fit reports none",
          clean.coerced_values()["columns"], [])
    check("duck: with no problem raised either",
          [p for p in clean.problems if p["reason"] == "coerced-values"], [])


def test_duck_names_a_key_the_pinned_list_cannot_see(check, check_true, skip):
    """Pinning trades one silence for another, so the other one is counted.

    A pinned read is blind to a field Claude Code adds -- measured: `SELECT
    new_field_2027` is a binder error and `read_json_auto` sees it fine.  That
    is `cu/otlp.py`'s allow-list one layer down, and that allow-list is how
    `prompt.id`, every user `--tag` and `agent.name` were each lost in silence.
    """
    if not _have_duckdb():
        return skip("duck: unknown keys (duckdb not installed)")
    rows = [dict(r, new_field_2027="hello") for r in fx.stamped_rows()]
    st = store.DuckStore(_store_root(rows=rows))
    got = st.unknown_keys()
    check("duck: the unknown key is named", [k for k, _n in got["keys"]],
          ["new_field_2027"])
    check("duck: with the number of rows carrying it",
          dict(got["keys"])["new_field_2027"], len(rows))
    st2 = store.DuckStore(_store_root())
    check("duck: and a store with no unknown key says so",
          st2.unknown_keys()["keys"], [])


def test_duck_a_query_never_sees_a_row_from_another_account(check, skip):
    """The per-account layout is the narrowing, and it is checked.

    `accounts/<uuid>/ledger.jsonl` means an account-scoped query opens one
    file, so the isolation is structural rather than a WHERE clause somebody
    could drop.  Asserted anyway: the file layout and the predicate must agree,
    and the day a store grows a shared file the predicate is what is left.
    """
    if not _have_duckdb():
        return skip("duck: account isolation (duckdb not installed)")
    st = store.DuckStore(_store_root())
    for label in ("agent", "alpha", "beta"):
        try:
            uuid = fx.uuid_of(label)
        except KeyError:
            continue
        q = Q.compile_query({"account_uuid": uuid})
        sel = st.select(q)
        others = [r["account_uuid"] for r in sel.rows
                  if r["account_uuid"] != uuid]
        check("duck: %s sees only its own rows" % label, others, [])
        check("duck: and it opened only its own file", sel.source,
              "duckdb:read_json(1 file(s))")
