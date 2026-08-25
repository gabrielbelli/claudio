"""The DuckDB reader over `accounts/<uuid>/*.jsonl`, and what it refuses.

Impure by construction -- it opens a database and reads files -- so it is the
second and last member of the exception set the purity test asserts, beside
`serve.py`.  That set is an EQUALITY, not a floor: this file had to be added to
`IMPURE` in `server/tests/test_all.py` before it could exist, and the next
module that opens anything will too.  `srv/duck.py` holds every decision that
does not need a connection and is pure -- `duckdb` is a forbidden import
everywhere in `srv/` except here -- so the SQL, the pinned schema and the
empty-answer accounting are all testable by reading a string.

**Why DuckDB, in one line:** it queries the JSONL **in place**, so the files
stay the record of truth, there is no import step, no migration, nothing to
rebuild after a crash, and a real SQL join across the two streams -- which is
what ad-hoc correlation needs and is the one thing the hand-rolled engine
cannot do at all.  Measured on this corpus rather than assumed; the numbers and
the shapes that beat them are in `server/README.md`.

**What this module is NOT.**  It is not the record of truth and it is not the
reconciler.  `reconcile.py` and everything under it still read raw JSON through
`serve.read_batches`, from byte zero, every time -- that is what lets a
week-late shipment retroactively correct a window that closed days ago, and it
is a sequential algorithm over ordered samples with rollover detection, not an
aggregation.  Nothing here touches it.  Delete this whole file and
`claudio usage` still works offline with no DuckDB, no network and no server:
`usage/` reads JSONL with the standard library only, and a test asserts that
nothing under `usage/` imports duckdb.

**The five things SQL collapses, kept apart here by hand.**  A query engine
returns zero rows for "there is nothing", "your filter removed everything" and
"that question has no answer", and it will happily add two accounts together.
Those are guardrails that live in code:

  1. `no-data`, `filtered-to-nothing` and `Unanswerable` stay three different
     values with three different messages, from `duck.accounting_sql`'s
     per-clause elimination counts -- one extra aggregate per clause in the
     SAME scan, not a query per clause.
  2. No cross-account total exists here either, not even for tokens.  A
     breakdown spanning more than one account is REFUSED by name, and the
     refusal happens before any SQL runs.
  3. Every aggregate carries a `CoverageStatement`, from the reconciled report
     when there is one and `no_report_coverage` when there is not.
  4. `coverage.fraction` is None -- "nobody said" -- for every window that
     exists today, because nothing emits an attestation.  It is never 0.0,
     which would be the claim that nobody was listening.
  5. Plan percentages are lower bounds over quantised, possibly-stale
     snapshots.  `LOWER_BOUND_NOTE` travels with anything derived from them.

**And two the storage layer itself introduces**, both measured (see
`duck.py`): a malformed line becomes a row of NULLs rather than being skipped,
so it is excluded AND counted; and a pinned column list cannot see a key
Claude Code adds later, so the keys that arrive are counted and named.
"""

import os
import tempfile
import threading

from . import duck
from . import query as Q
from .query import Unanswerable, refused

# Named once.  Every path that could produce a plausible empty result instead
# of a missing dependency goes through `require()`.
# One string, naming every platform this server is published for, because the
# remedy is the whole point of the refusal and a remedy that does not work on
# the machine reading it is a refusal with no way out.
#
# The FreeBSD line is not decoration: there is a published FreeBSD image, and
# on FreeBSD `pip install duckdb` finds no wheel -- PyPI publishes macOS,
# manylinux and Windows and nothing else -- so it falls back to the sdist and
# compiles the C++ engine, for hours, if it succeeds at all.  `pkg` is the only
# viable route there.  `brew` does not exist on FreeBSD either.
INSTALL_HINT = (
    "the server's query layer needs DuckDB, which is not installed.\n"
    "    python3 -m pip install duckdb          (Linux, macOS, Windows)\n"
    "    pkg install py312-duckdb               (FreeBSD; PyPI has no FreeBSD "
    "wheel, so pip would compile the engine from source)\n"
    "  or:  brew install duckdb   (the CLI; the Python module is still pip)\n"
    "Nothing else in this project needs it: `claudio usage` and everything "
    "under usage/ read the same JSONL with the standard library, offline, "
    "with no server running."
)

LOWER_BOUND_NOTE = (
    "plan percentages are LOWER BOUNDS. They are quantised by the server and "
    "cached inside the claude process, so a sample can be arbitrarily stale "
    "and the only sound cross-machine operator is max: a figure derived from "
    "them is a floor, never a reading."
)


# WHERE DUCKDB SPILLS, NAMED RATHER THAN INHERITED FROM THE CWD.  See
# `DuckStore._settings` for the measurement; the short version is that
# DuckDB's own default is `.tmp` relative to the process's working directory,
# which in a container with no `WORKDIR` is `/`.
TEMP_DIR_ENV = "CLAUDIO_DUCKDB_TEMP_DIR"
MEMORY_LIMIT_ENV = "CLAUDIO_DUCKDB_MEMORY_LIMIT"
TEMP_DIR_MAX_ENV = "CLAUDIO_DUCKDB_TEMP_MAX"


def default_temp_dir():
    """An absolute path under the system temp directory, or whatever the
    operator named.  Absolute, always: a relative path is the bug."""
    named = os.environ.get(TEMP_DIR_ENV)
    if named:
        return os.path.abspath(named)
    return os.path.join(tempfile.gettempdir(), "claudio-duckdb")


class DuckDBMissing(RuntimeError):
    """Raised loudly, never swallowed into an empty answer."""


def require():
    """The duckdb module, or a specific, actionable failure.

    Deliberately an exception rather than a None the caller might forget to
    check: a storage layer that degrades into a plausible empty result is the
    silent failure this project has written up twice, and "0 requests" is a
    perfectly plausible answer for an account that has not shipped yet.
    """
    try:
        import duckdb
    except ImportError as exc:
        raise DuckDBMissing(INSTALL_HINT) from exc
    return duckdb


def available():
    """True if DuckDB can be imported.  For a diagnostics line, never a gate
    on an answer."""
    try:
        require()
    except DuckDBMissing:
        return False
    return True


# ---------------------------------------------------------------------------
# the files
# ---------------------------------------------------------------------------

LEDGER = "ledger.jsonl"
SAMPLES = "samples.jsonl"


class Paths(object):
    """Where the door put things.  Mirrors `serve.Store`'s layout exactly.

    A missing file is not an error and not an empty answer either: it is
    `no-data`, and the difference matters because an account directory that
    exists with no ledger in it means the shipper has not reached stream A yet,
    which is a different sentence from "this account made no requests".
    """

    def __init__(self, root):
        self.root = root
        self.accounts_dir = os.path.join(root, "accounts")

    def accounts(self):
        try:
            return sorted(d for d in os.listdir(self.accounts_dir)
                          if os.path.isdir(os.path.join(self.accounts_dir, d)))
        except OSError:
            return []

    def stream(self, uuid, name):
        """One account's stream file, or None -- and never a path outside the
        store.

        `uuid` arrives verbatim from `?account=` and `os.path.join` DISCARDS
        its first component when the second is absolute, so an unguarded join
        turned `?account=/private/tmp/x` into `/private/tmp/x/ledger.jsonl`
        and served the row -- email address, cost and all -- through an API
        that asks for no token.  Its stated contract is "every record in the
        store", not every `ledger.jsonl` the process can read.

        Refused by MEMBERSHIP, not by pattern: the account directories are
        listed anyway and a name that is not one of them names no account this
        store holds, which is a stronger statement than "contains no `..`".
        The realpath containment check below is the second half rather than
        the first, because a symlink planted inside the store would satisfy
        membership and still point out of it.
        """
        if not isinstance(uuid, str) or not uuid:
            return None
        if uuid not in self.accounts():
            return None
        p = os.path.join(self.accounts_dir, uuid, name)
        root = os.path.realpath(self.accounts_dir)
        if not os.path.realpath(p).startswith(root + os.sep):
            return None
        return p if os.path.exists(p) else None

    def knows(self, uuid):
        """True if this store holds a directory for `uuid`.

        The question `_account_or_refusal` asks of the reconciler, asked of
        the files: an account that has shipped stream A and nothing else is in
        no report yet and is still a real identity.
        """
        return isinstance(uuid, str) and bool(uuid) and uuid in self.accounts()

    def ledgers(self, uuid=None):
        ids = [uuid] if uuid else self.accounts()
        return [p for p in (self.stream(u, LEDGER) for u in ids) if p]

    def samples(self, uuid=None):
        ids = [uuid] if uuid else self.accounts()
        return [p for p in (self.stream(u, SAMPLES) for u in ids) if p]


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------

class DuckStore(object):
    """A reader.  It writes nothing, creates no database file and caches
    nothing.

    Not caching is the same decision `serve.py` already made and for the same
    reason: a window's state is a function of `now`, so a report derived at T
    and served at T+2h reports as open a window that closed an hour ago.
    Measured here as well -- two reads of a growing file two lines apart return
    two different counts, so DuckDB re-reads the JSONL every time and the
    property comes for free rather than needing a cache invalidation nobody
    would maintain.
    """

    def __init__(self, root, connection=None, threads=None, temp_dir=None,
                 memory_limit=None, temp_dir_max=None):
        self.paths = Paths(root)
        self.root = root
        self._con = connection
        self._threads = threads
        self.temp_dir = default_temp_dir() if temp_dir is None else temp_dir
        self.memory_limit = (os.environ.get(MEMORY_LIMIT_ENV) or None
                             if memory_limit is None else memory_limit)
        self.temp_dir_max = (os.environ.get(TEMP_DIR_MAX_ENV) or None
                             if temp_dir_max is None else temp_dir_max)
        self._tl = threading.local()
        # Guards the LAZY CONSTRUCTION only, never a query.  Two threads
        # arriving at an unopened store would otherwise each build a
        # connection and one of them would be dropped on the floor with its
        # settings; the queries themselves run on per-thread cursors and take
        # nothing.  See `con`.
        self._con_lock = threading.Lock()

    # -- problems: per DERIVATION, never per process ------------------------

    @property
    def problems(self):
        """What THIS derivation's queries could not read.

        It was a plain list on the instance, created once and only ever
        appended to, and one `DuckStore` lives for the process.  Measured
        against a live door over a store holding exactly 3 unparseable lines:
        after 1500 `/search` requests the envelope reported 3116 entries
        claiming 3118 unparseable lines, the payload had grown from 51 kB to
        1.34 MB on EVERY response including `/health`, and a query scoped to a
        healthy account reported another account's tear.  A front end
        rendering "3118 unparseable lines" over a store with 3 is worse than
        silence, and it contradicted this server's own claim that nothing is
        cached and every request re-derives from byte zero.

        Thread-local rather than one list with a lock: the door is a
        `ThreadingHTTPServer`, so two concurrent requests share this object,
        and a reset in one thread would empty the other's evidence mid-answer.
        """
        p = getattr(self._tl, "problems", None)
        if p is None:
            p = []
            self._tl.problems = p
        return p

    def reset_problems(self):
        """Called once per request, before dispatch.  See `problems`."""
        self._tl.problems = []

    # -- connection --------------------------------------------------------

    def con(self):
        """A handle for THIS THREAD.  One database, one cursor per caller.

        A `DuckDBPyConnection` holds the pending result of the last `execute`
        ON THE CONNECTION, so handing one handle to every request thread makes
        two concurrent readers consume each other's rows.  MEASURED against a
        completely static store -- nothing appending, no writer running, so
        this is not the read-while-append path and the store lock is innocent
        -- through a real door, 8 threads and 480 requests to
        `/api/v1/search`:

            71 answered HTTP 500 `reader-failed`
            30 answered HTTP 200 `ok` CARRYING ANOTHER QUESTION'S FIGURES

        The 500s are the harmless half.  The 200s are this project's cardinal
        sin with a status code on it: `matched: 303` served beside an EMPTY
        `rows` list -- the refusal-as-empty-list the whole envelope exists to
        prevent, delivered through the engine instead of through the
        vocabulary -- and one account's request answered with another
        account's `matched`.  A figure crossed accounts, which is the one rule
        this package refuses to bend.  The mechanism is not inferred: the
        server's own stderr caught a `request_id` hash arriving where the
        `scanned` count belongs, `int('f75165a92c6b44a9')`.

        `cursor()` returns a connection over the SAME in-memory database with
        a result slot of its own, so nothing is re-opened and no file is read
        twice.  Isolated: one connection, 6 threads, 1800 queries gives 6
        `None`s and a wrong value; `cursor()` gives 1800/1800.  With the fix
        the identical 480-request load gives 0 failures and 0 wrong answers.

        This is `problems` one level down -- the property directly above says
        "the door is a `ThreadingHTTPServer`, so two concurrent requests share
        this object", and that reasoning was applied to the list and not to the
        connection it describes.
        """
        with self._con_lock:
            if self._con is None:
                duckdb = require()
                con = duckdb.connect()
                if self._threads:
                    con.execute("SET threads=%d" % int(self._threads))
                for stmt, value in self._settings():
                    con.execute(stmt, [value])
                self._con = con
            base = self._con
        c = getattr(self._tl, "con", None)
        if c is None:
            c = base.cursor()
            self._tl.con = c
        return c

    def _settings(self):
        """`SET` statements every connection is opened with, as (sql, value).

        WHY THIS IS NOT LEFT TO DUCKDB'S DEFAULTS.  `temp_directory` defaults
        to `.tmp`, RELATIVE TO THE PROCESS'S CWD.  Measured inside the api
        container: no `WORKDIR`, so cwd is `/`, the image runs as 65534, and
        the store mount is `:ro` -- so the first query that spills answers

            IO Error: Failed to create directory ".tmp": Permission denied

        which reaches the caller as `reader-failed` with the detail reduced to
        the exception TYPE (deliberately -- the text goes to stderr), so an
        operator reads a fault with no cause, and the cause is a directory
        name.  Every measurement this design rests on points at the query that
        triggers it: 45.6 GiB, 62 M rows, `GROUP BY` over the whole corpus.

        The value binds; only the setting name is literal, and these three are
        literals in this file.  See `duck.py` on why an identifier is the one
        thing that cannot bind.
        """
        out = []
        if self.temp_dir:
            out.append(("SET temp_directory=?", self.temp_dir))
        if self.temp_dir_max:
            out.append(("SET max_temp_directory_size=?", self.temp_dir_max))
        if self.memory_limit:
            out.append(("SET memory_limit=?", self.memory_limit))
        return out

    def temp_dir_problem(self):
        """None, or why this process could not use its spill directory.

        SAID AT BOOT RATHER THAN DISCOVERED AS A 500 ON THE FIRST BIG
        QUESTION.  It writes and unlinks one byte, because `os.access` answers
        about the permission bits and not about a read-only mount, a full
        filesystem or a path that is really a file.

        It never imports duckdb: a missing DuckDB is a named 503 per request
        and must not become a process that refuses to start.

        And `serve()` WARNS on it rather than exiting, which is the honest
        wording for what this returns.  A door that cannot spill still answers
        every small question correctly, still serves the reconciler half and
        still takes shipments in the whole-door configuration, so refusing to
        start would turn a degraded reader into no reader at all.  Do not
        promote this to a refusal without changing that trade deliberately --
        and if you do, change this paragraph with it.
        """
        if not self.temp_dir:
            return None
        probe = os.path.join(self.temp_dir, ".probe-%d" % os.getpid())
        try:
            os.makedirs(self.temp_dir, mode=0o700, exist_ok=True)
            with open(probe, "wb") as fh:
                fh.write(b"\0")
            os.unlink(probe)
        except OSError as exc:
            return ("%s is not writable (%s). DuckDB spills large "
                    "aggregations there, so a big question would fail as an "
                    "unexplained `reader-failed`. Point %s at a writable "
                    "path, or give this process one."
                    % (self.temp_dir, exc, TEMP_DIR_ENV))
        return None

    def _rel(self, paths, stream="a"):
        """(params, relation) for ONE statement.  Never shared between two.

        The file paths and the pinned column map are bind parameters, so the
        relation string and the parameter dict are halves of one statement.
        DuckDB refuses a dict carrying a name the statement does not use, so
        reusing a `Params` across two statements fails the second one loudly
        rather than answering it -- which is why this returns the pair and no
        caller keeps either half around.
        """
        params = duck.Params()
        types = (duck.LEDGER_SQL_TYPES if stream == "a"
                 else duck.SAMPLE_SQL_TYPES)
        return params, duck.relation(paths, types, params)

    def _note_malformed(self, n, paths):
        """A torn line is counted and NAMED, never quietly subtracted.

        `ignore_errors=true` emits a row of NULLs for a line it could not
        parse (measured), so the count is exact and the rows are excluded by
        `duck.MALFORMED_PREDICATE` everywhere a row is returned.  It goes in
        `problems`, which travels in every payload for `meta.store_problems`'
        reason: a batch the reader could not put back together must not make an
        account render byte-identically to one that never shipped.
        """
        if not n:
            return
        # ONE entry per file, carrying the count -- not one entry per
        # observation.  `_v1_search` calls `select` and then `page`, and each
        # one notices the same tear, so appending would make the entry count
        # measure how many times somebody ran a query rather than how many
        # lines are torn.  The count is `max`, never a sum, for the same
        # reason: two calls in one request see the same lines.
        #
        # `reason` is `snap.problems`' key, deliberately: a client rendering
        # `p.reason` showed `undefined` for every problem this side produced,
        # so `meta.store_problems` was one array of two shapes.
        detail = ("%d line(s) in the store could not be parsed as JSON. "
                  "They are excluded from every figure here and counted. "
                  "A line with no terminating newline is a write in "
                  "progress and will parse on the next read; one that "
                  "stays is a tear -- `cu/ledger.py` counts the same "
                  "thing from the writing end." % int(n))
        self._note("unparseable-lines", paths, int(n), detail)

    def _note(self, reason, paths, count, detail):
        """One problem about one set of files, deduped on (reason, files).

        `file` and `account_uuid` are filled in only when the relation was ONE
        file, which is every account-scoped query: the count is over the whole
        relation, so naming an account beside a figure that spans three of
        them would be a confident wrong attribution of somebody else's tear.
        """
        paths = list(paths)
        one = paths[0] if len(paths) == 1 else None
        for p in self.problems:
            if (p.get("reason"), tuple(p.get("files") or ())) \
                    == (reason, tuple(paths)):
                p["count"] = max(p.get("count") or 0, int(count))
                return
        self.problems.append({
            "reason": reason,
            "account_uuid": self._account_of(one) if one else None,
            "file": one,
            "files": paths,
            "count": int(count),
            "detail": detail,
        })

    def _account_of(self, path):
        """The account a store path belongs to, so a client need not parse
        one."""
        d = os.path.dirname(os.path.abspath(path))
        if os.path.dirname(d) == os.path.abspath(self.paths.accounts_dir):
            return os.path.basename(d)
        return None

    # -- the three empty answers -------------------------------------------

    def select(self, q, account_uuid=None, columns=None):
        """`query.select`'s answer, computed in SQL over the files.

        Returns a `Selection` with the same fields the pure path returns, so a
        caller cannot tell which engine produced it -- which is the property
        the differential test asserts row for row over the real fixtures.
        """
        if refused(q):
            return q
        paths = self.paths.ledgers(account_uuid or q.account_uuid)
        if not paths:
            # The ONE branch that can tell a missing ledger from a present but
            # empty one, so it is the one branch that sets `no_data_note`.
            note = ("no ledger file exists for %s. That is not the same as "
                    "an account with no requests: the shipper may not have "
                    "reached stream A on any machine yet."
                    % (account_uuid or q.account_uuid or "any account"))
            return Q.Selection(
                rows=[], undatable=[], scanned=0, matched=0, eliminated={},
                sole_cause=None, empty_because="no-data",
                clauses=[l for l, _ in Q.clauses_of(q)],
                notes=[note], no_data_note=note,
                source="duckdb:no-files")
        params, rel = self._rel(paths)
        acc_sql, labels = duck.accounting_sql(rel, q, params)
        row = self.con().execute(acc_sql, params.values).fetchone()
        scanned, malformed, matched, undatable_n = row[0], row[1], row[2], row[3]
        elim = {labels[i]: row[4 + i] for i in range(len(labels))}
        self._note_malformed(malformed, paths)
        scanned = int(scanned) - int(malformed)

        # `require_ts=False`: a row with no usable `ts` matched the query and
        # belongs in the selection.  It is returned, counted in `undatable`,
        # and appears in no page -- the same three-way split `query.select`
        # makes, and the reason `undatable` is a list here rather than a
        # number nobody could reconcile against the rows in front of them.
        params, rel = self._rel(paths)
        sel_sql, cols = duck.select_sql(rel, q, params, columns=columns,
                                        limit=q.limit, require_ts=False)
        rows = [duck.materialise(cols, t)
                for t in self.con().execute(sel_sql, params.values).fetchall()]
        undated = [r for r in rows if not isinstance(r.get("ts"), float)]

        sole = ([l for l, n in elim.items() if n == scanned]
                if scanned and not matched else [])
        empty = None
        if scanned == 0:
            empty = "no-data"
        elif not matched:
            empty = "filtered-to-nothing"

        notes = []
        if undatable_n:
            notes.append(
                "%d matching row(s) carry no usable timestamp: they are in "
                "this selection, in no window of either kind, and in no page "
                "-- an ordering by time cannot place them" % undatable_n)
        if empty == "filtered-to-nothing" and len(sole) == 1:
            notes.append("every one of the %d scanned row(s) was eliminated "
                         "by a single clause: %s" % (scanned, sole[0]))
        elif empty == "filtered-to-nothing":
            notes.append("no single clause eliminated everything; the "
                         "intersection of %d clause(s) is empty" % len(labels))
        return Q.Selection(
            rows=rows, undatable=undated, scanned=scanned,
            matched=int(matched),
            eliminated=elim, sole_cause=(sole[0] if len(sole) == 1 else None),
            empty_because=empty, clauses=labels, notes=notes,
            source="duckdb:read_json(%d file(s))" % len(paths))

    def lookup(self, field, value, account_uuid=None):
        """One id -> its rows.  `query.lookup`'s refusals, unchanged."""
        if field not in Q.ID_FIELDS:
            return Unanswerable("not-an-id-column", field,
                                "id columns: " + ", ".join(Q.ID_FIELDS))
        if not isinstance(value, str) or not value:
            return Unanswerable("id-not-a-string", repr(value),
                                "pass the id as a string")
        # The account is in the QUERY, not only in the file list.  The
        # per-account layout is the narrowing today, so the clause eliminates
        # nothing and costs nothing -- but a lookup whose only account filter
        # was the path it opened is one relaxed path check away from returning
        # another tenant's row, and this is the clause that would still be
        # there.
        spec = {"where": {field: value}, "order": "asc"}
        if account_uuid:
            spec["account_uuid"] = account_uuid
        q = Q.compile_query(spec)
        if refused(q):
            return q
        sel = self.select(q, account_uuid=account_uuid)
        if not refused(sel) and sel.empty_because == "filtered-to-nothing":
            sel.notes.append(
                "no row carries %s=%s. That is not the same as a request with "
                "no cost: this store may never have received it, the shipper "
                "may not have reached it yet, or the id may be from another "
                "account." % (field, value))
        return sel

    # -- pages --------------------------------------------------------------

    def page(self, q, cursor=None, limit=None, coverage=None,
             account_uuid=None):
        """One keyset page.  Same cursor format, same refusals, same notes.

        The cursor is `query.encode_cursor`'s string and is decoded by
        `query.decode_cursor`, so a cursor issued by the pure path is valid
        here and vice versa: two engines that disagreed about a cursor would
        skip rows in silence, which is the one thing keyset paging exists to
        prevent.
        """
        if refused(q):
            return q
        lim = limit if limit is not None else (q.limit or 50)
        if not isinstance(lim, int) or isinstance(lim, bool) or lim <= 0:
            return Unanswerable("limit-not-a-positive-integer", repr(lim),
                                "pass a positive integer")
        after = None
        if cursor is not None:
            after = Q.decode_cursor(q, cursor)
            if refused(after):
                return after

        paths = self.paths.ledgers(account_uuid or q.account_uuid)
        if not paths:
            empty = self.select(q, account_uuid=account_uuid)
            return Q.Page(items=[], next_cursor=None, has_more=False,
                          order=q.order, limit=lim, matched=0, scanned=0,
                          undatable_n=0, empty_because="no-data",
                          eliminated={}, sole_cause=None,
                          coverage=coverage if coverage is not None
                          else Q.no_report_coverage([]),
                          notes=list(empty.notes),
                          no_data_note=empty.no_data_note,
                          source="duckdb:no-files")
        params, rel = self._rel(paths)
        acc_sql, labels = duck.accounting_sql(rel, q, params)
        arow = self.con().execute(acc_sql, params.values).fetchone()
        scanned, malformed, matched, undatable_n = arow[:4]
        elim = {labels[i]: arow[4 + i] for i in range(len(labels))}
        self._note_malformed(malformed, paths)
        scanned = int(scanned) - int(malformed)

        # One row more than asked for, which is how `has_more` is known
        # without a second count -- and `matched` above is the honest total,
        # so a caller never reads `limit` as "this touched 50 rows".
        params, rel = self._rel(paths)
        sql, cols = duck.select_sql(rel, q, params, order=q.order,
                                    limit=lim + 1, after=after)
        got = [duck.materialise(cols, t)
               for t in self.con().execute(sql, params.values).fetchall()]
        has_more = len(got) > lim
        items = got[:lim]

        sole = ([l for l, n in elim.items() if n == scanned]
                if scanned and not matched else [])
        empty = None
        if scanned == 0:
            empty = "no-data"
        elif not matched:
            empty = "filtered-to-nothing"

        notes = []
        if undatable_n:
            notes.append(
                "%d matching row(s) carry no usable timestamp: they are in "
                "this selection, in no window of either kind, and in no page "
                "-- an ordering by time cannot place them" % undatable_n)
        if empty == "filtered-to-nothing" and len(sole) == 1:
            notes.append("every one of the %d scanned row(s) was eliminated "
                         "by a single clause: %s" % (scanned, sole[0]))
        elif empty == "filtered-to-nothing":
            notes.append("no single clause eliminated everything; the "
                         "intersection of %d clause(s) is empty" % len(labels))
        notes.append(
            "pages are keyed on (ts, request_id), which never change for a "
            "row, so a row is never served twice or skipped between pages. A "
            "row SHIPPED LATE whose ts sorts behind the cursor was already "
            "passed and will not appear in this pass -- re-run the query with "
            "an explicit since/until range to pick it up.")
        if cursor is not None and empty is None and not items:
            notes.append("this page is empty because the cursor is at the end "
                         "of the result, not because the query matched "
                         "nothing")
        return Q.Page(
            items=items,
            next_cursor=(Q.encode_cursor(q, items[-1])
                         if items and has_more else None),
            has_more=has_more, order=q.order, limit=lim, matched=int(matched),
            scanned=scanned, undatable_n=int(undatable_n),
            empty_because=empty, eliminated=elim,
            sole_cause=(sole[0] if len(sole) == 1 else None),
            coverage=coverage if coverage is not None
            else Q.no_report_coverage(items),
            notes=notes, source="duckdb:read_json(%d file(s))" % len(paths))

    # -- breakdowns ---------------------------------------------------------

    def breakdown(self, q, by, report=None, account_uuid=None, kind="5h"):
        """One account's rows, grouped by one column, coverage beside it.

        Every refusal `query.breakdown` makes is made HERE, before any SQL
        runs, and for the same reasons: percentages belong to one plan, and
        `account`/`email` are labels that `--tag account=` can overwrite, so
        two accounts sharing one would silently become one bucket.  A query
        engine would have added them up without a word.
        """
        if refused(q):
            return q
        ok, kind_of = (Q._known_column(by) if isinstance(by, str)
                       else (False, None))
        if not ok:
            return Unanswerable(
                "unknown-column", repr(by),
                "stream A columns: %s; user tags: %s<key>"
                % (", ".join(sorted(Q.LEDGER_COLUMNS)), Q.TAG_PREFIX))
        if kind_of == "structured":
            return Unanswerable(
                "cannot-group-by-a-structure", by,
                "a row carries many tags and belongs to as many buckets; "
                "group by %s<key> for one of them" % Q.TAG_PREFIX)

        acct = account_uuid or q.account_uuid
        if acct is None:
            present = self.paths.accounts()
            if len(present) > 1:
                if by in Q.LABEL_COLUMNS:
                    return Unanswerable(
                        "label-is-not-an-identity", by,
                        "`%s` is a label -- claudio's --tag account= "
                        "overwrites it and two accounts can carry the same "
                        "one, so grouping across accounts by it would merge "
                        "two identities into one bucket. Group by "
                        "account_uuid, or pass account_uuid=." % by)
                if by != "account_uuid":
                    return self._crosses_accounts(present,
                                                  "call breakdown_per_account()")

        sel_paths = self.paths.ledgers(acct)
        if not sel_paths:
            sel = self.select(q, account_uuid=acct)
            return Q.Breakdown(
                account_uuid=acct, by=by, buckets={}, order="cost desc",
                matched=0, scanned=0, undatable_n=0, empty_because="no-data",
                eliminated={}, sole_cause=None,
                overhead={"requests": 0, "cost": 0.0, "sources_seen": [],
                          "counted_as_your_work": [],
                          "note": "these are Claude Code's own requests, "
                                  "counted and named, never filtered out from "
                                  "under a total"},
                coverage=Q.no_report_coverage([]),
                column_status={"column": by, "rows_with_a_value": 0,
                               "distinct_values": 0, "never_observed": True},
                notes=list(sel.notes), no_data_note=sel.no_data_note)

        params, rel = self._rel(sel_paths)
        where = "(%s) AND NOT (%s)" % (duck.where_sql(q, params),
                                       duck.MALFORMED_PREDICATE)
        key = self._bucket_key_sql(by, params)
        over = " OR ".join("query_source = %s" % params.add(s)
                           for s in sorted(Q.OVERHEAD_SOURCES))
        w = ("CASE WHEN cost_usd_reported IS NOT NULL "
             "AND cost_usd_reported >= 0 THEN cost_usd_reported ELSE 0 END")
        wl = ("cost_usd_reported IS NULL OR cost_usd_reported < 0")
        agg = [
            "%s AS k" % key,
            "count(*) AS requests",
            "sum(%s) AS cost" % w,
            "count(*) FILTER (WHERE %s) AS weightless" % wl,
            "coalesce(sum(duration_ms), 0) AS duration_ms",
            "count(*) FILTER (WHERE duration_ms IS NULL) AS duration_ms_null",
        ]
        for f in Q.TOKEN_FIELDS:
            agg.append("coalesce(sum(%s), 0) AS %s" % (f, f))
        agg.append("count(*) FILTER (WHERE %s) AS tokens_null_rows"
                   % " OR ".join("%s IS NULL" % f for f in Q.TOKEN_FIELDS))
        agg.append("count(*) FILTER (WHERE %s) AS overhead_requests" % over)
        agg.append("sum(CASE WHEN %s THEN %s ELSE 0 END) AS overhead_cost"
                   % (over, w))
        sql = ("SELECT %s FROM %s WHERE %s GROUP BY k"
               % (", ".join(agg), rel, where))
        rows = self.con().execute(sql, params.values).fetchall()

        buckets = {}
        for r in rows:
            k = r[0]
            if isinstance(k, str) and duck.is_json_column(by):
                k = duck.materialise([by], (k,))[by]
            buckets[k] = {
                "requests": int(r[1]), "cost": float(r[2] or 0.0),
                "weightless": int(r[3]), "duration_ms": int(r[4] or 0),
                "duration_ms_null": int(r[5]),
                "input_tokens": int(r[6]), "output_tokens": int(r[7]),
                "cache_read_tokens": int(r[8]),
                "cache_creation_tokens": int(r[9]),
                "tokens_null_rows": int(r[10]),
                "overhead_requests": int(r[11]),
                "overhead_cost": float(r[12] or 0.0),
            }

        sel = self.select(q, account_uuid=acct, columns=("ts", "request_id"))
        # `pooled=True`: everything that crosses accounts has ALREADY been
        # refused above, so reaching here with `acct is None` and more than
        # one account in the store means `by == "account_uuid"` exactly -- the
        # permitted grouping.  This asks only for that column's cardinality.
        avail = self.field_availability(q, account_uuid=acct, columns=(by,),
                                        pooled=True)
        info = avail["columns"].get(by)
        column_status = {
            "column": by,
            "rows_with_a_value": info.get("present", 0) if info else 0,
            "distinct_values": info.get("distinct_n", 0) if info else 0,
            "never_observed": True if info is None
            else bool(info.get("never_observed")),
        }

        nulls_missing = self._token_nulls(sel_paths, q)
        p2, rel2 = self._rel(sel_paths)
        where2 = "(%s) AND NOT (%s)" % (duck.where_sql(q, p2),
                                        duck.MALFORMED_PREDICATE)
        seen = self.con().execute(
            "SELECT DISTINCT query_source FROM %s WHERE %s "
            "AND query_source IS NOT NULL" % (rel2, where2),
            p2.values).fetchall()
        seen = sorted(s[0] for s in seen)
        overhead = {
            "requests": sum(b["overhead_requests"] for b in buckets.values()),
            "cost": sum(b["overhead_cost"] for b in buckets.values()),
            "sources_seen": [s for s in seen if s in Q.OVERHEAD_SOURCES],
            "counted_as_your_work": [s for s in seen
                                     if s not in Q.OVERHEAD_SOURCES],
            "note": "these are Claude Code's own requests, counted and named, "
                    "never filtered out from under a total",
        }

        notes = []
        if column_status["never_observed"] and sel.matched:
            notes.append(
                "no row scanned carries a value for `%s`, so this is one "
                "bucket of everything rather than a breakdown. The column "
                "exists and has never been populated in this data -- which is "
                "not the same as every request having the same value." % by)
        if any(v > 0 for v in nulls_missing.values()):
            # NOT "the payload did not state them".  A pinned read nulls a
            # value it cannot hold and keeps the row, so this count mixes a
            # silent payload with one that stated `"abc"` or `1e30` -- and
            # asserting the first about the second is a false sentence printed
            # directly above a figure derived from it.  The route that
            # separates them is named rather than implied.
            notes.append(
                "token columns are null in some rows and are summed as "
                "absent, not as 0: %s. Null here means the payload carried no "
                "value OR carried one this column's pinned type could not "
                "hold -- a pinned read cannot tell those apart, and "
                "/api/v1/diagnostics counts the second kind."
                % ", ".join("%s in %d row(s)" % (f, n)
                            for f, n in sorted(nulls_missing.items()) if n))
        notes.append(
            "`cost` is Claude Code's own cost_usd at API list rates, which a "
            "Pro or Max plan is never charged. It is a relative weight and it "
            "is the same number the attribution weights by; it is not spend.")
        cov = (Q.coverage_for(report, acct, sel.rows, kind=kind) if acct
               else Q.no_report_coverage(sel.rows))
        return Q.Breakdown(
            account_uuid=acct, by=by, buckets=buckets, order="cost desc",
            matched=sel.matched, scanned=sel.scanned,
            undatable_n=len(sel.undatable),
            empty_because=sel.empty_because, eliminated=sel.eliminated,
            sole_cause=sel.sole_cause, overhead=overhead, coverage=cov,
            column_status=column_status, notes=notes + list(sel.notes),
            no_data_note=sel.no_data_note)

    def breakdown_per_account(self, q, by, report=None, kind="5h"):
        """{account_uuid: Breakdown}.  No total, deliberately, not even for
        tokens -- `query.breakdown_per_account`'s rule, unchanged: a rule with
        an exception in it is applied by whoever remembers only the
        exception."""
        out = {}
        for uuid in self.paths.accounts():
            out[uuid] = self.breakdown(q, by, report=report,
                                       account_uuid=uuid, kind=kind)
        return out

    # -- histograms ---------------------------------------------------------

    # A bucketed metric is a SUM over a column or a COUNT of rows, and nothing
    # else is offered.  Naming them here rather than formatting a caller's
    # string into the SQL is the same closed-set rule `serve.STREAM_FILES`
    # applies to a stream name: a validated string and a closed set are one
    # refactor apart, and an identifier is the one thing DuckDB cannot bind.
    HISTOGRAM_METRICS = {
        "requests": None,
        "cost": "cost_usd_reported",
        "input_tokens": "input_tokens",
        "output_tokens": "output_tokens",
        "cache_read_tokens": "cache_read_tokens",
        "cache_creation_tokens": "cache_creation_tokens",
        "duration_ms": "duration_ms",
    }

    def histogram(self, q, interval, metric, account_uuid=None):
        """Rows bucketed on epoch multiples of `interval`.

        **Epoch arithmetic only.**  `to_timestamp()` returns a TIMESTAMPTZ and
        converting one to Python requires `pytz` -- a second non-stdlib
        dependency acquired by writing an ordinary date histogram -- and it
        would assert a time zone the data does not carry.  So the bucket is
        `floor(ts / interval) * interval`, an integer, and the client
        localises.

        A row with no usable `ts` is in NO bucket and is counted, never
        dropped: it matched the query and its weight is real, so a histogram
        that silently omitted it would draw a shorter total than the search
        beside it returned.
        """
        if refused(q):
            return q
        if metric not in self.HISTOGRAM_METRICS:
            return Unanswerable("unknown-column", repr(metric),
                                "metric is one of: "
                                + ", ".join(sorted(self.HISTOGRAM_METRICS)))
        try:
            interval = int(interval)
        except (TypeError, ValueError):
            interval = 0
        if interval <= 0:
            return Unanswerable("range-not-a-number", repr(interval),
                                "pass a positive number of seconds")
        # Before any SQL runs, exactly as `breakdown` does it: a bucket's
        # `value` is a sum, and a sum over two accounts is the cross-account
        # total this module does not have.  It is refused HERE rather than in
        # the API so both callers inherit it and a third cannot forget it.
        acct = account_uuid or q.account_uuid
        if acct is None:
            present = self.paths.accounts()
            if len(present) > 1:
                return self._crosses_accounts(
                    present, "ask for one account at a time")
        paths = self.paths.ledgers(account_uuid or q.account_uuid)
        if not paths:
            return {"buckets": [], "undatable_n": 0, "source": "duckdb:no-files"}

        params, rel = self._rel(paths)
        where = "(%s) AND NOT (%s)" % (duck.where_sql(q, params),
                                       duck.MALFORMED_PREDICATE)
        iv = params.add(int(interval))
        col = self.HISTOGRAM_METRICS[metric]
        value = "count(*)" if col is None else "coalesce(sum(%s), 0)" % col
        bucket = "CAST(floor(ts / %s) AS BIGINT) * %s" % (iv, iv)
        sql = ("SELECT %s AS bucket, count(*) AS rows, %s AS value "
               "FROM %s WHERE %s AND ts IS NOT NULL "
               "GROUP BY bucket ORDER BY bucket" % (bucket, value, rel, where))
        rows = self.con().execute(sql, params.values).fetchall()

        params, rel = self._rel(paths)
        where = "(%s) AND NOT (%s)" % (duck.where_sql(q, params),
                                       duck.MALFORMED_PREDICATE)
        undated = self.con().execute(
            "SELECT count(*) FROM %s WHERE %s AND ts IS NULL" % (rel, where),
            params.values).fetchone()[0]

        return {
            "buckets": [{"bucket": int(b), "rows": int(n),
                         "value": (float(v) if v is not None else 0),
                         "metric": metric} for b, n, v in rows],
            "undatable_n": int(undated),
            "source": "duckdb:read_json(%d file(s))" % len(paths),
            "note": ("%d matching row(s) carry no usable timestamp and are in "
                     "no bucket. They are counted here rather than dropped: a "
                     "histogram that omits them draws a smaller total than the "
                     "search beside it returns." % int(undated))
                    if undated else None,
        }

    def _refuse_if_crosses(self, q, account_uuid, alternative=None,
                           pooled=False):
        """`None` to carry on, or the refusal, for a caller that pools rows.

        `breakdown` and `histogram` each inline this check; `facets` and
        `field_availability` had neither, so the two routes built on them --
        `/api/v1/values` and `/api/v1/fields` -- answered the account-less
        question that `/aggregate` and `/histogram` refuse by name.  Factored
        out rather than copied a third and fourth time, because the count of
        places to forget is the whole problem: `_crosses_accounts` was already
        extracted for that reason and the extraction stopped one level short.

        `pooled=True` is the ONE earned exception, and it is a statement by
        the caller that it has already settled the crossing question for
        itself.  It exists because `breakdown(by="account_uuid")` is the one
        cross-account grouping this module ALLOWS -- one bucket is one account
        and there is no total row -- and it asks this layer for the grouping
        column's cardinality on the way past.  Refusing that call would have
        broken the permitted case in the name of the forbidden one.  A static
        test asserts `srv/api.py` never passes it, so the exception cannot
        migrate from the internal caller that earned it out onto a route.
        """
        if pooled:
            return None
        if (account_uuid or q.account_uuid) is not None:
            return None
        present = self.paths.accounts()
        if len(present) <= 1:
            return None
        return self._crosses_accounts(
            present, alternative or "ask for one account at a time")

    def _crosses_accounts(self, present, alternative):
        """The refusal, in one place, so a third caller cannot miss it.

        It lived inside `breakdown` and `histogram` did not have it, so
        `/api/v1/histogram` with no `account=` opened EVERY account's ledger
        and emitted `sum(cost_usd_reported)` over the lot -- 200 ok, no
        refusal, one number spanning three plans with three denominators,
        while `/api/v1/aggregate` refused the identical question by name.
        Percentages belong to one plan; so does a token count, which is why
        the rule has no exception in it.
        """
        return Unanswerable(
            "crosses-accounts", ", ".join(present),
            "these rows belong to %d accounts. Percentages belong to one plan "
            "and this module computes no cross-account total, not even for "
            "tokens: pass account_uuid=, or %s." % (len(present), alternative))

    def _bucket_key_sql(self, by, params):
        """`query._bucket_key`: the column, else the same-named tag, else NULL.

        Presence, not truthiness.  `show`'s `r.get(k) or tags.get(k) or
        "(none)"` buckets a row with `duration_ms: 0` as unlabelled; this does
        not, and the divergence is named on both sides rather than being one
        of them quietly being wrong.
        """
        e = duck._col_expr(by, params)
        if by.startswith(Q.TAG_PREFIX):
            return e
        t = duck._col_expr(Q.TAG_PREFIX + by, params)
        if by in duck.JSON_COLUMNS:
            return "coalesce(%s, %s)" % (e, t)
        return "coalesce(CAST(%s AS VARCHAR), json_extract_string(%s, '$'))" \
            % (e, t)

    def _token_nulls(self, paths, q):
        """Per token column, how many matching rows carried no value.

        Takes the paths and the query rather than a ready-made relation and
        predicate: it is its own statement, so it needs its own parameters,
        and handing it another statement's would bind names this one never
        uses.
        """
        params, rel = self._rel(paths)
        where = "(%s) AND NOT (%s)" % (duck.where_sql(q, params),
                                       duck.MALFORMED_PREDICATE)
        sel = ", ".join("count(*) FILTER (WHERE %s IS NULL) AS %s" % (f, f)
                        for f in Q.TOKEN_FIELDS)
        row = self.con().execute("SELECT %s FROM %s WHERE %s"
                                 % (sel, rel, where), params.values).fetchone()
        return {f: int(row[i]) for i, f in enumerate(Q.TOKEN_FIELDS)}

    # -- what the data actually contains -----------------------------------

    def field_availability(self, q, account_uuid=None, columns=None,
                           pooled=False):
        """Per column: rows that carried a value, and how many distinct ones.

        The antidote this project paid the most for.  A pinned read cannot
        separate an absent key from an explicit null -- both are SQL NULL --
        so `absent` is reported as None rather than as 0, which would be the
        confident claim that every null was written deliberately.  The pure
        path can tell them apart and does; this one says it cannot.

        Cross-account is refused HERE, the third caller `_crosses_accounts`
        exists for.  `present` and `distinct_n` are counts over pooled rows,
        and a count over two plans is the cross-account total this module does
        not have -- it merely is not spelled "total", which is exactly why the
        key-name walk in the suite could not see it.
        """
        cross = self._refuse_if_crosses(q, account_uuid, pooled=pooled)
        if cross is not None:
            return cross
        paths = self.paths.ledgers(account_uuid or q.account_uuid)
        cols = list(columns or Q.LEDGER_COLUMNS)
        if not paths:
            return {"rows": 0, "columns": {}, "notes": ["no ledger file"]}
        params, rel = self._rel(paths)
        where = "(%s) AND NOT (%s)" % (duck.where_sql(q, params),
                                       duck.MALFORMED_PREDICATE)
        sel = ["count(*) AS n"]
        for i, c in enumerate(cols):
            e = duck._col_expr(c, params)
            sel.append("count(%s) AS p_%d" % (e, i))
            sel.append("count(DISTINCT %s) AS d_%d" % (e, i))
        row = self.con().execute("SELECT %s FROM %s WHERE %s"
                                 % (", ".join(sel), rel, where),
                                 params.values).fetchone()
        out = {}
        for i, c in enumerate(cols):
            present = int(row[1 + 2 * i])
            out[c] = {"present": present, "null": int(row[0]) - present,
                      "absent": None, "distinct_n": int(row[2 + 2 * i]),
                      "never_observed": present == 0}
        return {"rows": int(row[0]), "columns": out,
                "notes": ["`absent` is None, not 0: a pinned column list reads "
                          "a key that is missing and a key written null "
                          "identically, and reporting 0 absences would be a "
                          "claim this engine cannot support"]}

    def facets(self, q, account_uuid=None, columns=None, limit=None,
               pooled=False):
        """{column: [(value, count)]} -- from the rows, never from a list.

        A column no row carried is OMITTED rather than rendered as an empty
        control, which is `query.facets`' rule and the reason six
        `query_source` names Claude Code has never sent survived here reading
        as coverage.

        Cross-account is refused HERE, for the reason `histogram` refuses it:
        every `n` beside a value is `count(*)` over whatever files were
        opened.  Measured on the real fixtures, `/api/v1/values?fields=model`
        with no account returned `{claude-sonnet-5: 5, claude-haiku-4-5: 3}`
        over three accounts -- byte-identical to the per-account fan-out
        summed by hand -- while `/api/v1/aggregate?by=model` refused the
        identical question by name.
        """
        cross = self._refuse_if_crosses(q, account_uuid, pooled=pooled)
        if cross is not None:
            return cross
        paths = self.paths.ledgers(account_uuid or q.account_uuid)
        if not paths:
            return {}
        out = {}
        cols = list(columns or Q.FACETABLE)
        if columns is None:
            # Tag columns are DISCOVERED from the rows, exactly as
            # `query.facets` discovers them through `field_availability`.
            # Iterating only `FACETABLE` would drop every user tag -- which is
            # the same defect as an allow-list, arriving through the tidier
            # door of "these are the columns we support".
            kp, krel = self._rel(paths)
            kwhere = "(%s) AND NOT (%s)" % (duck.where_sql(q, kp),
                                            duck.MALFORMED_PREDICATE)
            keys = self.con().execute(
                "SELECT DISTINCT unnest(json_keys(tags)) AS k FROM %s "
                "WHERE %s AND tags IS NOT NULL ORDER BY k" % (krel, kwhere),
                kp.values).fetchall()
            cols += [Q.TAG_PREFIX + k[0] for k in keys]
        for c in cols:
            if c in Q.ID_FIELDS:
                continue
            # One statement per column, so one `Params` per column: the tag
            # key in `_col_expr` is itself a bound value.
            params, rel = self._rel(paths)
            where = "(%s) AND NOT (%s)" % (duck.where_sql(q, params),
                                           duck.MALFORMED_PREDICATE)
            e = duck._col_expr(c, params)
            sql = ("SELECT %s AS v, count(*) AS n FROM %s WHERE %s "
                   "AND %s IS NOT NULL GROUP BY v ORDER BY n DESC, "
                   "CAST(v AS VARCHAR)" % (e, rel, where, e))
            if limit:
                sql += " LIMIT %s" % params.add(int(limit))
            pairs = self.con().execute(sql, params.values).fetchall()
            if not pairs:
                continue
            out[c] = [((duck.materialise([c], (v,))[c]
                        if duck.is_json_column(c) else v), int(n))
                      for v, n in pairs]
        return out

    # -- diagnostics --------------------------------------------------------

    def unknown_keys(self, account_uuid=None, sample_bytes=None):
        """Keys in the files that the pinned column list does not carry.

        The other half of pinning, and the reason pinning is defensible at
        all: a pinned read is blind to a field Claude Code adds, exactly as
        `cu/otlp.py`'s allow-list was, and that allow-list is how `prompt.id`,
        every user `--tag` and `agent.name` were each lost in silence.  This is
        a full JSON parse, so it is a diagnostics call and never on the query
        path, and what it sampled is reported with the answer.
        """
        paths = self.paths.ledgers(account_uuid)
        if not paths:
            return {"keys": [], "files": [], "note": "no ledger file"}
        params = duck.Params()
        rows = self.con().execute(duck.unknown_keys_sql(paths, params),
                                  params.values).fetchall()
        return {"keys": [(k, int(n)) for k, n in rows], "files": paths,
                "note": "every key here arrived in the store and is invisible "
                        "to every query, because the column list is pinned. "
                        "Add it to `duck.LEDGER_SQL_TYPES` and "
                        "`wire.LEDGER_FIELDS` in the same commit."}

    def coerced_values(self, account_uuid=None):
        """Values the pinned read did not keep as the payload wrote them.

        The counterpart to `unknown_keys`, and it exists for the same reason:
        pinning buys a stable shape and pays for it in silence, so the silence
        is counted.  A value the pinned type cannot hold becomes NULL and the
        row survives -- `request_id` is intact, so `MALFORMED_PREDICATE` never
        fires and nothing anywhere moves -- and a value of a different type
        that CAN be converted is converted, so `"7"` and `true` add to sums the
        Python oracle excludes.

        A full JSON parse, so it is a diagnostics call and never on the query
        path; `duck.py`'s measurement 3 has the numbers.  It records what it
        found in `problems`, so the finding travels in the payload that asked
        for it rather than only in a return value somebody has to read.
        """
        paths = self.paths.ledgers(account_uuid)
        if not paths:
            return {"columns": [], "files": [], "rows": 0,
                    "note": "no ledger file"}
        params = duck.Params()
        rows = self.con().execute(duck.coerced_values_sql(paths, params),
                                  params.values).fetchall()
        found = {}
        for k, t, fits, n in rows:
            if not duck.coercion_of(k, t, fits):
                continue
            found.setdefault(k, []).append(
                {"json_type": t, "fits_pinned_type": bool(fits), "rows": int(n)})
        out = [{"column": c,
                "pinned_as": duck.LEDGER_SQL_TYPES.get(c),
                "rows": sum(x["rows"] for x in v),
                "json_types": sorted(v, key=lambda x: str(x["json_type"]))}
               for c, v in sorted(found.items())]
        total = sum(c["rows"] for c in out)
        if total:
            self._note(
                "coerced-values", paths, total,
                "%d value(s) in %d column(s) are not of the type that column "
                "is pinned to. A pinned read either CONVERTS such a value "
                "(measured: \"7\" -> 7, true -> 1, 1.5 -> 2, all of which add "
                "to a sum the Python oracle excludes) or replaces it with "
                "NULL (\"abc\", 1e30, an integer past BIGINT) and keeps the "
                "row -- so it is indistinguishable from a payload that said "
                "nothing. Columns: %s"
                % (total, len(out), ", ".join(c["column"] for c in out)))
        return {
            "columns": out, "files": paths, "rows": total,
            "note": "a value counted here reached the store and is NOT the "
                    "value any query returns. The pinned type is in "
                    "`duck.LEDGER_SQL_TYPES`; change it and `wire.LEDGER_"
                    "FIELDS` in the same commit, or fix the writer.",
        }

    def counts(self, account_uuid=None):
        """Rows and malformed lines per account.  Cheap; no predicate."""
        out = {}
        for uuid in ([account_uuid] if account_uuid else self.paths.accounts()):
            paths = self.paths.ledgers(uuid)
            if not paths:
                out[uuid] = {"rows": 0, "malformed": 0, "bytes": 0}
                continue
            params, rel = self._rel(paths)
            n, bad = self.con().execute(
                "SELECT count(*), count(*) FILTER (WHERE %s) FROM %s"
                % (duck.MALFORMED_PREDICATE, rel), params.values).fetchone()
            out[uuid] = {"rows": int(n) - int(bad), "malformed": int(bad),
                         "bytes": sum(os.path.getsize(p) for p in paths)}
        return out
