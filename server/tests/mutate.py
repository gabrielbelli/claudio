#!/usr/bin/env python3
"""Mutation matrix: break each behaviour in turn and require a named failure.

Run: python3 server/tests/mutate.py

An unmutated test suite is decoration, and this project has proved it twice --
a lock test that a `LOCK_EX -> LOCK_SH` regression sailed through, and a
counters test that matched the *comment* explaining why a setting mattered
rather than the setting.  Both were green over the bug they existed to catch.

So every behaviour the core claims is broken here, on a copy, and the suite
must fail with a named assertion.  A mutation that SURVIVES is the finding: it
means that behaviour is asserted by nothing, and the report says so in the
first column.

The harness never touches `server/srv`.  It copies the package to a temporary
directory, applies one textual substitution, and runs the unmodified suite
against the copy via `SRV_DIR`.  Each `old` string must appear exactly once, so
a mutation that has silently stopped matching its target -- the way a mutation
list rots -- is reported as BAD PATCH rather than counted as a survivor.

**The copy has to be COMPLETE, and that is a scar rather than a detail.**  It
was `srv/` alone, so `test_api_the_readme_names_exactly_the_routes_that_exist`
raised `FileNotFoundError` in every mutated run -- one guaranteed failure per
row, which is indistinguishable from a caught mutation because an exception is
counted as a failure exactly as an assertion is.  Every verdict in the matrix
was therefore "caught" whether or not anything asserted the behaviour, and
three real survivors were sitting behind it (both halves of the account path
check, and the account clause `store.lookup` compiles).  A harness that
manufactures its own evidence is the decoration this whole file exists to
prevent, so the README is copied beside the package and the test that reads it
now fails BY NAME when it cannot find it.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.dirname(HERE)
SRC = os.path.join(SERVER, "srv")
SUITE = os.path.join(HERE, "test_all.py")


# (id, the behaviour being broken, file, old, new)
MUTATIONS = [

    # ------------------------------------------------ window reconstruction
    ("rollover-highwater", "a rollover is a new window, not a decrease to be "
     "ignored (the 'one record then permanent silence' bug)",
     "window.py",
     "        buckets.setdefault(int(raw_reset), []).append((rec, host, float(raw_pct)))",
     "        _hw = max([p for b in buckets.values() for _r, _h, p in b] or [-1.0])\n"
     "        if float(raw_pct) < _hw:\n"
     "            continue\n"
     "        buckets.setdefault(int(raw_reset), []).append((rec, host, float(raw_pct)))"),

    ("rollover-basis", "a window superseded by a later resets_at is closed BY "
     "that rollover",
     "window.py",
     '            state, basis = "closed", "rollover"',
     '            state, basis = "open", None'),

    ("membership-by-ts", "membership is by resets_at and never by ts (the "
     "staleness lemma)",
     "window.py",
     "        buckets.setdefault(int(raw_reset), []).append((rec, host, float(raw_pct)))",
     "        if _num(rec.get('ts')) and not (int(raw_reset) - length <= rec['ts']\n"
     "                                        < int(raw_reset)):\n"
     "            continue\n"
     "        buckets.setdefault(int(raw_reset), []).append((rec, host, float(raw_pct)))"),

    ("max-becomes-last", "the window's value is the max over every sample, "
     "not the last one by file order",
     "window.py",
     "        peak = max(p for _r, _h, p in rows)",
     "        peak = rows[-1][2]"),

    ("no-cross-machine-clamp", "re-clamping is max across ALL machines, not "
     "the first machine's own series",
     "window.py",
     "        peak = max(p for _r, _h, p in rows)",
     "        peak = max(p for _r, h, p in rows if (h or '?') == (rows[0][1] or '?'))"),

    ("skew-blind", "a machine whose clock is behind is named, with how far",
     "window.py",
     "            if reset - t > length:",
     "            if False:"),

    ("no-gaps", "the interval belonging to no window is reported",
     "window.py",
     "            if b.start > a.end:",
     "            if False:"),

    ("epoch-floor", "a percentage with an unusable window identity is refused "
     "(the real `resets_at: 1`)",
     "wire.py",
     "EPOCH_FLOOR = 1000000000",
     "EPOCH_FLOOR = 0"),

    ("pct-digits", "a quantised 1e30 must never become a high-water mark "
     "nothing can beat",
     "wire.py",
     "MAX_PCT_DIGITS = 18",
     "MAX_PCT_DIGITS = 600"),

    ("baseline-not-zero", "a closed 5-hour window's baseline is zero by "
     "construction, so movement == peak",
     "window.py",
     '            baseline, bbasis = 0.0, "zero-by-construction"',
     '            baseline, bbasis = trough, "zero-by-construction"'),

    ("mixed-accounts", "reconstruction refuses a sample belonging to another "
     "account",
     "window.py",
     "            if rec.get(\"account_uuid\") != account_uuid:",
     "            if False:"),

    ("no-timestamp-crash", "a sample with no usable ts costs its "
     "timestamp-derived fields, not the whole report",
     "window.py",
     "        peak_ts = min(peak_tss) if peak_tss else None",
     "        peak_ts = min(peak_tss)"),

    # ------------------------------------------------------ the wire format
    ("absent-is-null", "a field added later is distinguishable from a field "
     "that was absent",
     "wire.py",
     "    return rec[key] if key in rec else ABSENT",
     "    return rec.get(key)"),

    ("no-v1-schema", "a stream-B record with no schema key is schema 1 -- 65 "
     "real records are in that shape",
     "wire.py",
     "        return SCHEMA_SAMPLES_V1 if stream == STREAM_SAMPLES else None",
     "        return None"),

    ("tuple-sample-key", "the stream-B key is the whole record plus the host, "
     "not the proposed 4-tuple",
     "wire.py",
     "    return hashlib.sha256(\n"
     "        (canonical(rec) + \"\\x00\" + (host or \"\")).encode(\"utf-8\")).hexdigest()",
     "    return hashlib.sha256(canonical({\n"
     "        'a': rec.get('account_uuid'), 't': rec.get('ts'),\n"
     "        'f': rec.get('five_hour_pct'), 's': rec.get('seven_day_pct'),\n"
     "    }).encode('utf-8')).hexdigest()"),

    ("manifest-no-host", "the manifest must carry a host, because stream B's "
     "v1 shape has no host column of its own",
     "wire.py",
     '    if not isinstance(m.get("host"), str) or not m["host"]:\n'
     '        return False, "manifest-no-host"',
     "    if False:\n        pass"),

    # ------------------------------------------------------------- ingest
    ("no-ledger-dedupe", "re-ingesting a stream-A batch is a no-op",
     "ingest.py",
     "        if rid in self.ledger:",
     "        if False:"),

    ("no-sample-dedupe", "re-ingesting a stream-B batch is a no-op",
     "ingest.py",
     "        if key in self.samples:",
     "        if False:"),

    ("collision-as-duplicate", "two requests wearing one request_id are "
     "reported, not silently deduped",
     "ingest.py",
     "            if self.ledger_fp[rid] == fp:",
     "            if True:"),

    ("no-d7", "the token names the tenant: a disagreeing record fails the "
     "WHOLE batch",
     "ingest.py",
     "        for rec in batch.records:\n"
     "            if isinstance(rec, dict) and rec.get(\"account_uuid\") not in (\n"
     "                    batch.tenant, None):",
     "        for rec in batch.records:\n"
     "            if False:"),

    ("no-manifest-count", "a manifest that disagrees with its body is refused",
     "ingest.py",
     '        if batch.manifest["count"] != len(batch.records):',
     "        if False:"),

    # --------------------------------------------------------- attribution
    ("normalise-residual", "the residual is never made zero by normalising "
     "the split over the window's own weight",
     "attribute.py",
     "    attributed = rate * weight_total\n"
     "    residual = window.movement_lo - attributed",
     "    attributed = window.movement_lo\n"
     "    residual = 0.0"),

    ("clamp-residual", "a negative residual is reported, not clamped -- the "
     "movement is a lower bound, not a wrong request set",
     "attribute.py",
     "    residual = window.movement_lo - attributed",
     "    residual = max(0.0, window.movement_lo - attributed)"),

    ("attribute-open-windows", "attribution happens only after a window closes",
     "attribute.py",
     "    if not window.closed:",
     "    if False:"),

    ("flat-window-pins-rate", "a window that moved less than the quantiser's "
     "resolution says nothing about the rate",
     "attribute.py",
     "        if w.movement_lo < QUANT_RESOLUTION_PP:",
     "        if False:"),

    ("weight-by-input-tokens", "the weight is cost_usd_reported, which folds "
     "in all four token classes at their relative prices",
     "attribute.py",
     'WEIGHT_FIELD = "cost_usd_reported"',
     'WEIGHT_FIELD = "input_tokens"'),

    ("drop-provisional-note", "every attribution says the weighting is "
     "provisional",
     "attribute.py",
     "    notes = [PROVISIONAL_NOTE]",
     "    notes = []"),

    ("weightless-uncounted", "a request with no cost weighs 0 and is COUNTED",
     "attribute.py",
     "    return 0.0, True",
     "    return 0.0, False"),

    # ------------------------------------------------------------ coverage
    ("unknown-becomes-zero", "no attestation means coverage is unknown "
     "(None), never zero",
     "coverage.py",
     '        return Coverage(fraction=None, lower_bound=False, basis="unknown",',
     '        return Coverage(fraction=0.0, lower_bound=False, basis="unknown",'),

    ("sum-not-union", "two machines up at once is one second of coverage",
     "coverage.py",
     "    merged = union(spans)",
     "    merged = [tuple(s) for s in sorted(spans)]"),

    ("no-coverage-refusal", "a window nobody was listening to refuses to "
     "attribute rather than publishing a residual",
     "attribute.py",
     "    if cov is not None and cov.settled_zero:",
     "    if False:"),

    ("settled-ignores-pending", "settled_zero needs BOTH no coverage and "
     "nothing pending",
     "coverage.py",
     "        return (self.fraction == 0.0 and not self.hosts_pending\n"
     "                and not self.spans_unreadable)",
     "        return self.fraction == 0.0 and not self.spans_unreadable"),

    ("pending-is-dark", "a machine that has not spoken within the grace is "
     "pending, not dark",
     "coverage.py",
     "    pending = set(mute) if within_grace else set()",
     "    pending = set()"),

    # ------------------------------------------------------------ the sum
    ("cross-account-total", "there is no all-accounts total: different plans, "
     "different denominators",
     "reconcile.py",
     "        if not account_uuid:",
     "        if False:"),

    # ----------------------------------- the repaired adversarial findings
    ("rate-across-kinds", "the rate is fitted per (account, KIND): a 5-hour "
     "figure must not be scaled by the weekly plan's denominator",
     "reconcile.py",
     "                 if w.kind == kind and not covs[w.key].settled_zero])",
     "                 if not covs[w.key].settled_zero])"),

    ("rate-scalar-accepted", "a bare number rate is refused, not applied to "
     "both of an account's plans",
     "reconcile.py",
     "    if rate is None:\n        return None",
     "    if not isinstance(rate, dict):\n        return {'5h': rate, '7d': rate}\n"
     "    if rate is None:\n        return None"),

    ("blind-window-pins-rate", "a window nobody watched to the end does not "
     "pin the rate for every other window",
     "attribute.py",
     "    used, fell_back = (tight, False) if tight else (cands, True)",
     "    used, fell_back = cands, False"),

    ("no-pin-note", "every attributed row names the window that pinned its "
     "rate and how good that window's evidence was",
     "attribute.py",
     "    if pinned_stats:\n        notes.append(pin_note(pinned_stats))",
     "    if False:\n        notes.append(pin_note(pinned_stats))"),

    ("overlap-not-refused", "a resets_at closer than a window length to its "
     "neighbour is refused BY NAME, not attributed beside it",
     "window.py",
     "    resets = _refuse_overlapping(buckets, refusals, kind, length)",
     "    resets = sorted(buckets)"),

    ("no-start-clamp", "membership is a partition BY CONSTRUCTION, not as a "
     "consequence of the refusal rule firing",
     "window.py",
     "        if i and start < resets[i - 1]:",
     "        if False:"),

    ("sliver-pins-rate", "a clamped sliver cannot pin a rate for a full-length "
     "window",
     "attribute.py",
     "        if (w.end - w.start) < MIN_SPAN_FRACTION * w.length:",
     "        if False:"),

    ("attestation-last-arrived", "the attestation kept is the last EMITTED, "
     "not the last to arrive",
     "ingest.py",
     "                if new_at < old_at:",
     "                if False:"),

    ("no-emitted-at-check", "emitted_at is load-bearing, so it is validated "
     "like a field a decision rests on",
     "wire.py",
     '        ok, why = valid_epoch(get(rec, "emitted_at"), now)\n'
     '        if not ok:\n'
     '            return False, "attestation-" + why',
     "        if False:\n            pass"),

    ("unreadable-spans-accepted", "an attestation whose up_spans cannot be "
     "read is refused, not accepted and silently emptied",
     "wire.py",
     "    if not isinstance(v, (list, tuple)):\n"
     '        return False, "attestation-unreadable-spans"',
     "    if not isinstance(v, (list, tuple)):\n        return True, None\n"
     "    return True, None"),

    ("clip-drops-silently", "a span coverage cannot read is COUNTED, so a "
     "fraction of 0.0 can never be reported without saying how many",
     "coverage.py",
     "    out, bad = [], 0",
     "    out, bad = [], 0\n    spans = [s for s in (spans or [])\n"
     "             if isinstance(s, (list, tuple)) and len(s) >= 2\n"
     "             and isinstance(s[0], (int, float))\n"
     "             and isinstance(s[1], (int, float))]"),

    ("settled-ignores-unreadable", "an unreadable span means NOT settled: "
     "'nobody was listening' and 'we could not read it' are opposite claims",
     "coverage.py",
     "        return (self.fraction == 0.0 and not self.hosts_pending\n"
     "                and not self.spans_unreadable)",
     "        return self.fraction == 0.0 and not self.hosts_pending"),

    ("undatable-vanishes", "a ledger row with no usable ts is named, not lost "
     "between the window filter and the unclaimed filter",
     "reconcile.py",
     "        undatable = [r for r in acc_rows if not wire._is_number(r.get(\"ts\"))]",
     "        undatable = []"),

    ("rows-in-unguarded-ts", "an unusable ts costs the row, never the whole "
     "report: `window.start <= None` is a TypeError, not a filter",
     "attribute.py",
     "    return [r for r in rows\n"
     "            if _num(r.get(\"ts\"))\n"
     "            and window.start <= r[\"ts\"] < window.end]",
     "    return [r for r in rows\n"
     "            if window.start <= r[\"ts\"] < window.end]"),

    ("unclaimed-takes-a-boolean-ts", "`ts: true` is undatable, not the epoch "
     "1 sitting in the unclaimed list",
     "reconcile.py",
     "                               if wire._is_number(r.get(\"ts\"))",
     "                               if isinstance(r.get(\"ts\"), (int, float))"),

    ("refusals-thrown-away", "the top-level report carries the window "
     "refusals, not an empty list",
     "reconcile.py",
     "                  counts={\"accounts\": len(out)}, refusals=all_refusals)",
     "                  counts={\"accounts\": len(out)}, refusals=[])"),

    ("refusals-replaced", "reconcile_ingest EXTENDS the refusals; the door's "
     "and the window's are both refusals",
     "reconcile.py",
     "    rep.refusals = list(ing.refusals) + list(rep.refusals or [])",
     "    rep.refusals = list(ing.refusals)"),

    # ------------------------------------------------------------- the door
    ("door-identity-from-the-payload", "the token names the tenant; the door "
     "never reads an identity out of the body",
     "serve.py",
     "        tenant = self.tenants.resolve(token)",
     "        tenant = self.tenants.resolve(token) or (\n"
     "            json.loads(body.split(b'\\n')[1]).get('account_uuid')\n"
     "            if body.count(b'\\n') > 1 else None)"),

    ("door-partial-acceptance", "a record that disagrees with the token fails "
     "the WHOLE batch, not just itself",
     "serve.py",
     "            if got is not None and got != tenant:\n"
     "                return self._refuse(",
     "            if got is not None and got != tenant:\n"
     "                continue\n"
     "            if False:\n"
     "                return self._refuse("),

    ("door-reserialises", "the line's own bytes are appended, never the "
     "re-serialised parse",
     "serve.py",
     "                blob = b\"\".join(r + b\"\\n\" for r in records)",
     "                blob = b\"\".join(\n"
     "                    json.dumps(json.loads(r), sort_keys=True).encode()\n"
     "                    + b\"\\n\" for r in records)"),

    ("door-drops-the-manifest", "the envelope is stored beside the records, "
     "because stream B's v1 shape has no host column of its own",
     "serve.py",
     "                if man_raw is not None:",
     "                if False:"),

    ("door-offset-in-memory", "the offset is persisted BEFORE the ack, not "
     "held in a dictionary that dies with the process",
     "serve.py",
     "            self.save_offsets(tenant, offsets)",
     "            pass"),

    ("door-offset-ignores-identity", "a range is only skipped as a duplicate "
     "when the client's file proves it is the same file",
     "serve.py",
     "            same_file = bool(entry) and entry.get(\"ident\") == manifest.get(\"ident\")",
     "            same_file = bool(entry)"),

    ("door-rewrites-a-held-range", "a range this door already holds is not "
     "appended a second time",
     "serve.py",
     "            if same_file and to <= prev and not wire.manifest_reset(manifest):",
     "            if False:"),

    ("door-ignores-a-claimed-reset", "a client that says it restarted a file "
     "is not told the door already holds the range",
     "serve.py",
     "            if same_file and to <= prev and not wire.manifest_reset(manifest):",
     "            if same_file and to <= prev:"),

    ("door-glues-a-torn-line", "an interrupted write costs one line, never "
     "the good record that follows it",
     "serve.py",
     "                blob = b\"\\n\" + blob\n                torn = 1",
     "                torn = 1"),

    ("door-disk-unchecked", "a batch that would take the filesystem under the "
     "reserve is refused rather than written",
     "serve.py",
     "        ok, free = self.store.room_for(len(body))\n        if not ok:",
     "        ok, free = self.store.room_for(len(body))\n        if False:"),

    ("door-gzip-uncapped", "a compressed body is expanded with a cap",
     "serve.py",
     "        out = gzip.GzipFile(fileobj=io.BytesIO(body)).read(self.max_decompressed + 1)",
     "        out = gzip.GzipFile(fileobj=io.BytesIO(body)).read()"),

    ("door-count-unchecked", "a manifest that disagrees with its body is "
     "refused at the door too",
     "serve.py",
     "        if man[\"count\"] != len(raw_records):",
     "        if False:"),

    ("door-any-media-type", "the door accepts one content type and names the "
     "one it wants",
     "serve.py",
     "        if (content_type or \"\").split(\";\")[0].strip().lower() != CONTENT_TYPE:",
     "        if False:"),

    ("door-ack-without-the-offset", "the ack carries the offset this door "
     "durably holds for that client file",
     "serve.py",
     "                     \"offset\": res[\"offset\"],",
     "                     \"offset\": 0,"),

    ("door-ack-without-the-disk", "the ack carries the free bytes, on the way "
     "up as well as at the refusal",
     "serve.py",
     "                     \"disk_free_bytes\": self.store.free_bytes(),",
     "                     \"disk_free_bytes\": 0,"),

    ("door-tenant-traversal", "a tenant that would not be a usable directory "
     "name is refused by name, at load",
     "serve.py",
     "    return bool(name) and bool(TENANT_OK.match(name)) and \"..\" not in name",
     "    return bool(name)"),

    ("door-read-batches-skips-quietly", "an interrupted batch is named, never "
     "handed over as a shorter one",
     "serve.py",
     "            problems.append((\"interrupted-batch\", man))\n            continue",
     "            continue"),

    ("door-no-third-answer", "'nothing on record' is distinct from 'not the "
     "file I have': a first shipment is not told `false`",
     "serve.py",
     "            reported = same_file if entry else None",
     "            reported = same_file"),

    ("door-held-has-no-third-answer", "the same distinction where it is read "
     "back, not only where it is reported",
     "serve.py",
     "        if not isinstance(entry, dict):\n            return 0, None",
     "        if not isinstance(entry, dict):\n            return 0, False"),

    ("door-http-ignores-the-header", "the handler reads the bearer token off "
     "the request rather than assuming one",
     "serve.py",
     "        status, ack = self.door.ship(bearer(self.headers.get(\"Authorization\")),",
     "        status, ack = self.door.ship(next(iter(self.door.tenants.map), None),"),

    ("door-http-any-path", "the door is one path; anything else is 404",
     "serve.py",
     "        if self.path.split(\"?\")[0] != PATH_SHIP:",
     "        if False:"),

    # ------------------------------------------------------- the query layer
    ("query-unknown-column", "an unknown column is REFUSED, never grouped "
     "under (none) the way `show --by` does it",
     "query.py",
     "            ok, _kind = _known_column(col)",
     "            ok, _kind = True, None"),

    ("query-crosses-accounts", "an aggregate over more than one account is "
     "refused, not summed",
     "query.py",
     "    elif len(accounts) > 1:",
     "    elif False:"),

    ("query-label-is-an-identity", "grouping across accounts by the `account` "
     "label would merge two identities into one bucket",
     "query.py",
     "        if by in LABEL_COLUMNS:",
     "        if False:"),

    ("query-coverage-zero-not-unknown", "with nothing attested, coverage is "
     "None ('nobody said'), never 0.0 ('nobody was listening')",
     "query.py",
     "        fraction, basis = None, \"no-attestation\"",
     "        fraction, basis = 0.0, \"no-attestation\""),

    ("query-no-data-is-filtered", "'the corpus is empty' and 'every row was "
     "eliminated' are two different answers",
     "query.py",
     "    if scanned == 0:\n        empty = \"no-data\"",
     "    if False:\n        empty = \"no-data\""),

    ("query-blames-a-clause-for-an-empty-corpus", "with nothing scanned, no "
     "clause is to blame -- 0 of 0 is not 'this filter emptied it'",
     "query.py",
     "            if scanned and not kept else [])",
     "            if True else [])"),

    ("query-cursor-unbound", "a cursor is bound to the query that issued it; "
     "applying it to another skips whatever sorts before it",
     "query.py",
     "    if fp != q.fingerprint or order != q.order:",
     "    if False:"),

    ("query-undatable-dropped", "a row with no usable timestamp is counted, "
     "never quietly dropped from the selection",
     "query.py",
     "        if ok:\n            kept.append(row)",
     "        if ok:\n            if not _num(row.get(\"ts\")):\n"
     "                continue\n            kept.append(row)"),

    ("query-facets-from-a-list", "a facet list is derived from the rows; a "
     "column nothing has ever populated is not offered as an empty control",
     "query.py",
     "        if not counts:\n            continue",
     "        if not counts:\n            out[col] = []\n            continue"),

    ("query-bucket-or-chain", "the bucket key checks presence, not truth -- "
     "`show`'s `or` chain sends an empty-string value to the tag fallback",
     "query.py",
     "    if v is not ABSENT and v is not None:\n        return v",
     "    if v:\n        return v"),

    ("query-bool-is-one", "True == 1 in Python, so equality compares the type "
     "as well",
     "query.py",
     "        if isinstance(v, bool) != isinstance(w, bool):\n            continue",
     "        if False:\n            continue"),

    ("query-budget-ignored", "a stated interactive budget is enforced against "
     "the measured scan rate",
     "query.py",
     "    if budget_s is not None and est > budget_s:",
     "    if False:"),

    ("query-narrowing-claims-a-tag", "narrowing may only widen: a claim that "
     "the index can serve a column it cannot hands the predicate a candidate "
     "set with rows missing",
     "query.py",
     "        if col in INDEX_COLUMNS and None not in vals:",
     "        if True:"),

    # ------------------------------------------------------- the front end
    #
    # The page is a reader with no data of its own, so the mutations that
    # matter are the ones that give it some -- an external asset, a defaulted
    # zero, a list it holds itself -- and the ones that quietly drop a guard
    # on the way out of the endpoints.

    # `srv/ui.py` is gone -- omini is the front end -- so the six mutations
    # that broke the page went with it.  What replaced them is the layer that
    # now stands between the store and any front end: the pinned schema, the
    # torn-line containment, and the refusals a query engine would not make.

    ("duck-schema-inferred", "the column list is PINNED: inference DROPS a "
     "column absent from the sampled prefix -- measured at 29 columns where 30 "
     "were meant, with no error anywhere and GROUP BY still answering",
     "duck.py",
     "    return (\"read_json(%s, format='newline_delimited', columns=%s%s)\"\n"
     "            % (src, params.add(dict(columns)),\n"
     "               \", ignore_errors=true\" if ignore_errors else \"\"))",
     "    return (\"read_json_auto(%s, format='newline_delimited'%s)\"\n"
     "            % (src, \", ignore_errors=true\" if ignore_errors else \"\"))"),

    ("duck-never-observed-typed", "a column nothing has ever been observed in "
     "is pinned JSON, never guessed from its name: a pinned BIGINT COERCES, so "
     "1.5 arrives as 2, \"7\" as 7 and true as 1, and nobody is told",
     "duck.py",
     "for _c in Q.NEVER_OBSERVED:\n    _LEDGER_TYPES[_c] = \"JSON\"",
     "for _c in Q.NEVER_OBSERVED:\n    _LEDGER_TYPES[_c] = (\"BIGINT\" "
     "if _c == \"event_sequence\" else \"JSON\")"),

    ("duck-text-hits-a-number", "free text searches STRINGS: "
     "json_extract_string over a JSON 5 returns '5', so without the type guard "
     "the two engines disagree the first time anybody searches for a digit",
     "duck.py",
     "json_type(%s) = 'VARCHAR' ",
     "json_type(%s) <> 'NOPE' "),

    ("duck-phantom-row-counted", "ignore_errors emits a row of NULLs for a "
     "line it could not parse rather than skipping it, so an unfiltered store "
     "reports one request more than it can serve",
     "duck.py",
     "\"count(*) FILTER (WHERE (%s) AND %s) AS matched\" % (where, real),",
     "\"count(*) FILTER (WHERE (%s)) AS matched\" % (where,),"),

    ("duck-phantom-row-served", "and no page serves one: `ts IS NOT NULL` is "
     "there to make the ordering total, not to hide a tear, and the two stop "
     "coinciding the moment anybody asks for the undatable rows",
     "duck.py",
     "    where = \"(%s) AND NOT (%s)\" % (where, MALFORMED_PREDICATE)\n"
     "    if after is not None:",
     "    where = \"(%s)\" % (where,)\n"
     "    if after is not None:"),

    ("duck-value-written-into-the-sql", "no value reaches DuckDB as text: a "
     "filter value written into the string is an injection surface on a "
     "trusted network AND the live -inf defect, since repr(float('-inf')) is "
     "read as a column name",
     "duck.py",
     "            parts.append(\"(%s IS NOT NULL AND %s = %s)\"\n"
     "                         % (e, e, params.add(w)))",
     "            parts.append(\"(%s IS NOT NULL AND %s = '%s')\"\n"
     "                         % (e, e, w))"),

    ("duck-time-bound-written-into-the-sql", "a time bound is bound too: "
     "since=-inf is an ordinary \"no lower bound\", serve._num_param accepts "
     "it, and interpolated it raises Binder Error: Referenced column \"inf\" "
     "while the pure engine returns every row",
     "duck.py",
     "                    % (_ident(\"ts\"), _ident(\"ts\"), params.add(q.since))))",
     "                    % (_ident(\"ts\"), _ident(\"ts\"), repr(q.since))))"),

    ("duck-tag-key-written-into-the-json-path", "a tag key is arbitrary text "
     "from a user's --tag and becomes a JSON PATH: built by concatenation it "
     "is one quote away from selecting something else entirely",
     "duck.py",
     "        return \"json_extract(%s, %s)\" % (\n"
     "            _ident(\"tags\"), params.add(\"$.\" + json.dumps(col[len(Q.TAG_PREFIX):])))",
     "        return \"json_extract(%s, '$.\\\"%s\\\"')\" % (\n"
     "            _ident(\"tags\"), col[len(Q.TAG_PREFIX):])"),

    ("duck-column-map-not-bound-whole", "the bound column map is the WHOLE "
     "pinned schema: binding a narrower one is the silent column drop wearing "
     "the parameter's clothes, and it still reads as columns=$p2",
     "duck.py",
     "            % (src, params.add(dict(columns)),",
     "            % (src, params.add({k: v for k, v in columns.items()\n"
     "                                if k != \"cache_read_tokens\"}),"),

    ("duck-imports-the-driver", "DuckDB is permitted in ONE place: `duck.py` "
     "builds SQL and opens nothing, so the purity set stays {serve.py, "
     "store.py} and a second module cannot acquire the dependency quietly",
     "duck.py",
     "import json\n\nfrom . import query as Q",
     "import json\n\nimport duckdb\n\nfrom . import query as Q"),

    ("store-statement-without-its-parameters", "a statement executed without "
     "its parameter dict is one whose values were written into its text -- "
     "the form this layer started in and the one it would silently return to",
     "store.py",
     "        row = self.con().execute(acc_sql, params.values).fetchone()",
     "        row = self.con().execute(acc_sql).fetchone()"),

    ("query-partly-unknown-averaged", "the coverage fraction is None the "
     "moment ANY touched window is unattested: a length-weighted mean over "
     "the windows that spoke, presented as the coverage of a set that "
     "includes windows nobody described, is a confident number about an "
     "unknown -- the same error as 0.0 for \"nobody said\", one level up",
     "query.py",
     "        fraction, basis = None, \"partly-unknown\"",
     "        fraction, basis = ((covered / length) if length else None,\n"
     "                           \"attestations\")"),

    ("store-crosses-accounts", "no cross-account total exists, not even for "
     "tokens: two accounts are two plans and two denominators, and a query "
     "engine would have added them up without a word",
     "store.py",
     "                if by != \"account_uuid\":\n"
     "                    return self._crosses_accounts(present,",
     "                if False:\n"
     "                    return self._crosses_accounts(present,"),

    # The two routes the DuckDB swap ADDED.  The existing
    # `store-crosses-accounts` mutation cannot catch these: it breaks the
    # guard inside `breakdown`, and `facets`/`field_availability` never
    # reached that guard at all.  Nor can any `api-...-total` mutation -- the
    # value here is a COUNT and is not called a total, so the key-name walk
    # is structurally blind to it.
    ("store-facets-cross-account", "`/api/v1/values` served the pooled value "
     "distribution -- {claude-sonnet-5: 5, claude-haiku-4-5: 3} over three "
     "accounts, byte-identical to the per-account fan-out summed -- that "
     "`/aggregate?by=model` refuses by name",
     "store.py",
     "        cross = self._refuse_if_crosses(q, account_uuid, pooled=pooled)\n"
     "        if cross is not None:\n"
     "            return cross\n"
     "        paths = self.paths.ledgers(account_uuid or q.account_uuid)\n"
     "        if not paths:\n"
     "            return {}",
     "        paths = self.paths.ledgers(account_uuid or q.account_uuid)\n"
     "        if not paths:\n"
     "            return {}"),

    ("store-availability-cross-account", "`/api/v1/fields` reported one "
     "pooled `cardinality` per column over three plans -- `email` among them "
     "with distinct_n: 3 -- which is a cross-account count wearing the name "
     "of a schema",
     "store.py",
     "        cross = self._refuse_if_crosses(q, account_uuid, pooled=pooled)\n"
     "        if cross is not None:\n"
     "            return cross\n"
     "        paths = self.paths.ledgers(account_uuid or q.account_uuid)\n"
     "        cols = list(columns or Q.LEDGER_COLUMNS)",
     "        paths = self.paths.ledgers(account_uuid or q.account_uuid)\n"
     "        cols = list(columns or Q.LEDGER_COLUMNS)"),

    ("store-pooled-opt-out-is-a-door", "`pooled=True` is one earned exception "
     "for `breakdown(by=account_uuid)`; made unconditional it reopens both "
     "routes while every per-account test still passes",
     "store.py",
     "        if pooled:\n            return None",
     "        if True:\n            return None"),

    # The silent-drop family.  `spec_from` picked the keys it knew and never
    # looked at the remainder, so `?account_uuid=<alpha>` -- this module's own
    # keyword, and the spelling `_crosses_accounts` recommended until
    # `wire_remedy` landed -- returned 8 rows over 3 accounts as `ok` where
    # `?account=` returned 3 over 1.
    ("api-unknown-param-dropped", "an unrecognised URL parameter is refused, "
     "not dropped: dropping it returns the UNFILTERED answer as `ok`, and "
     "`?account_uuid=` silently unscopes a cross-account guard",
     "api.py",
     "        stray = unknown_params(slug, multi)\n        if stray:",
     "        stray = ()\n        if stray:"),

    ("api-filter-prefix-everywhere", "`where.<col>=` means something only "
     "where a spec is built; honoured everywhere, `/accounts?where.model=x` "
     "answers the unfiltered list as ok",
     "api.py",
     "    filters = route in SPEC_ROUTES",
     "    filters = True"),

    ("api-per-account-scoping-popped", "`?account=` on the fan-out asked to "
     "scope a route that cannot be scoped; popping it grants the request by "
     "ignoring it -- 200 ok over every account",
     "api.py",
     "        if spec.get(\"account_uuid\"):\n"
     "            return _refusal(",
     "        if False:\n"
     "            return _refusal("),

    ("api-endpoint-params-second-copy", "the published parameter list is "
     "DERIVED from the enforced one; a second copy had already drifted on "
     "four routes, and now that an undeclared key is refused, a drift decides "
     "whether a request works",
     "api.py",
     "    PATH_V1 + route: (sorted(params)\n"
     "                      + ([\"where.<field>\", \"not.<field>\"]\n"
     "                         if route in SPEC_ROUTES else []))",
     "    PATH_V1 + route: (sorted(params)[:1]\n"
     "                      + ([\"where.<field>\", \"not.<field>\"]\n"
     "                         if route in SPEC_ROUTES else []))"),

    ("api-nested-remedy-is-null", "a refusal's remedy is never null ON THE "
     "WIRE, including the ones nested per account in the fan-out -- the only "
     "route that reports a refusal per account, and the one seam that "
     "bypassed `_refusal` and its never-null fallback",
     "api.py",
     "                _st, doc = _refusal(b.reason, b.detail, "
     "wire_remedy(b.remedy))\n"
     "                out[uuid] = {\"outcome\": OUTCOME_UNANSWERABLE,\n"
     "                             \"refusal\": doc[\"refusal\"]}",
     "                out[uuid] = {\"outcome\": OUTCOME_UNANSWERABLE,\n"
     "                             \"refusal\": {\"reason\": b.reason,\n"
     "                                         \"detail\": b.detail,\n"
     "                                         \"remedy\": None,\n"
     "                                         \"catalogue\": CATALOGUE}}"),

    ("store-label-is-an-identity", "`account` and `email` are labels -- "
     "--tag account= overwrites one and the other survives an orphaned login -- "
     "so two accounts sharing one would silently become a single bucket",
     "store.py",
     "                if by in Q.LABEL_COLUMNS:\n"
     "                    return Unanswerable(\n"
     "                        \"label-is-not-an-identity\", by,",
     "                if False:\n"
     "                    return Unanswerable(\n"
     "                        \"label-is-not-an-identity\", by,"),

    ("store-empty-answers-merged", "no-data and filtered-to-nothing are two "
     "answers with two remedies; SQL returns zero rows for both",
     "store.py",
     "        elif not matched:\n            empty = \"filtered-to-nothing\"\n"
     "\n        notes = []\n        if undatable_n:\n"
     "            notes.append(\n                \"%d matching row(s) carry no "
     "usable timestamp: they are in \"\n                \"this selection, in "
     "no window of either kind, and in no page \"\n                \"-- an "
     "ordering by time cannot place them\" % undatable_n)\n"
     "        if empty == \"filtered-to-nothing\" and len(sole) == 1:\n"
     "            notes.append(\"every one of the %d scanned row(s) was "
     "eliminated \"\n                         \"by a single clause: %s\" % "
     "(scanned, sole[0]))\n        elif empty == \"filtered-to-nothing\":\n"
     "            notes.append(\"no single clause eliminated everything; the \"\n"
     "                         \"intersection of %d clause(s) is empty\" % "
     "len(labels))\n        return Q.Selection(",
     "        elif not matched:\n            empty = \"no-data\"\n"
     "\n        notes = []\n        if undatable_n:\n"
     "            notes.append(\n                \"%d matching row(s) carry no "
     "usable timestamp: they are in \"\n                \"this selection, in "
     "no window of either kind, and in no page \"\n                \"-- an "
     "ordering by time cannot place them\" % undatable_n)\n"
     "        if empty == \"filtered-to-nothing\" and len(sole) == 1:\n"
     "            notes.append(\"every one of the %d scanned row(s) was "
     "eliminated \"\n                         \"by a single clause: %s\" % "
     "(scanned, sole[0]))\n        elif empty == \"filtered-to-nothing\":\n"
     "            notes.append(\"no single clause eliminated everything; the \"\n"
     "                         \"intersection of %d clause(s) is empty\" % "
     "len(labels))\n        return Q.Selection("),

    ("store-missing-duckdb-is-silent", "a missing dependency must never look "
     "like an empty result: `0 requests` is a plausible answer for an account "
     "that has not shipped yet, so the two would be indistinguishable",
     "store.py",
     "    except ImportError as exc:\n"
     "        raise DuckDBMissing(INSTALL_HINT) from exc",
     "    except ImportError:\n        return None"),

    ("store-tear-not-reported", "a line the store could not parse is counted "
     "and NAMED, never quietly subtracted -- meta.store_problems is what stops "
     "an unreadable batch rendering like an account that never shipped",
     "store.py",
     "        if not n:\n            return\n        # ONE entry per file",
     "        if True:\n            return\n        # ONE entry per file"),

    ("store-unknown-keys-hidden", "pinning trades one silence for another, so "
     "a key the pinned list cannot see is counted and named: this is "
     "cu/otlp.py's allow-list one layer down, and that allow-list is how "
     "prompt.id, every user --tag and agent.name were each lost in silence",
     "store.py",
     "        rows = self.con().execute(duck.unknown_keys_sql(paths, params),\n"
     "                                  params.values).fetchall()",
     "        rows = []"),

    ("store-tag-facets-dropped", "facet values come from the rows, user tags "
     "included: iterating a fixed column list drops every one of them, which "
     "is an allow-list arriving through a tidier door",
     "store.py",
     "            cols += [Q.TAG_PREFIX + k[0] for k in keys]",
     "            cols += []"),

    ("store-undatable-not-counted", "a row with no usable `ts` matches a "
     "query and can take no position in a time ordering: it is in the "
     "selection, counted, and in no page -- reporting zero is the only thing "
     "that would tell a reader the page and the match count agree when they "
     "do not",
     "store.py",
     "            undatable_n=len(sel.undatable),",
     "            undatable_n=0,"),

    ("store-undatable-dropped", "and it is RETURNED: `ts IS NOT NULL` orders a "
     "page, it does not decide what matched, and collapsing the two drops a "
     "matching row from an unpaged answer with no symptom",
     "store.py",
     "                                        limit=q.limit, require_ts=False)",
     "                                        limit=q.limit, require_ts=True)"),

    ("store-reads-every-account", "an account-scoped query opens that "
     "account's file and no other: the per-account layout IS the narrowing, "
     "and the accounting it produces has to agree with the files it read",
     "store.py",
     "        paths = self.paths.ledgers(account_uuid or q.account_uuid)\n"
     "        if not paths:\n"
     "            # The ONE branch that can tell a missing ledger",
     "        paths = self.paths.ledgers(None)\n"
     "        if not paths:\n"
     "            # The ONE branch that can tell a missing ledger"),

    # ------------------------------------------------------- the read API
    ("api-coverage-stripped", "no attributed figure leaves the server without "
     "the coverage that separates an unwatched window from a browser session",
     "api.py",
     '        "spans_unreadable": d.get("spans_unreadable"),\n'
     '        "describes": describes,',
     '        "spans_unreadable": d.get("spans_unreadable"),\n'
     '        "describes": None,'),

    ("api-coverage-known-decoupled-from-the-fraction", "`known` is true if and "
     "only if a fraction exists -- the boolean and the null must not be "
     "reachable from one another",
     "api.py",
     '        "known": fraction is not None,',
     '        "known": True,'),

    ("api-unknown-coverage-rendered-as-zero", "an unknown coverage is null and "
     "never 0.0, which is the positive claim that nobody was listening",
     "api.py",
     '        "fraction": fraction,',
     '        "fraction": fraction if fraction is not None else 0.0,'),

    ("api-basis-claims-measurement", "`basis_is` is `measured` for exactly one "
     "basis: attestations. Anything else is unknown",
     "api.py",
     '        "basis_is": basis_is(basis),',
     '        "basis_is": "measured",'),

    ("api-report-pinned-to-a-stale-clock", "every request re-derives at the "
     "caller's `now`: a cached report reports as open a window that closed an "
     "hour ago",
     "serve.py",
     "        rep = reconcile.reconcile_ingest(ing, now)",
     "        rep = reconcile.reconcile_ingest(ing, 1786600000)"),

    ("api-refusal-becomes-an-empty-answer", "a question this data cannot "
     "answer is a refusal with a reason and a remedy, never a tidy empty list",
     "api.py",
     "def _as_refusal(u, status=400):\n"
     '    """A `query.Unanswerable` on the wire, reason, detail and remedy '
     'intact."""\n'
     "    return _refusal(u.reason, u.detail, wire_remedy(u.remedy), "
     "status=status)",
     "def _as_refusal(u, status=400):\n"
     "    return _ok({'windows': [], 'buckets': [], 'rows': []})"),

    # The remedy crosses a language boundary in `_as_refusal`.  Dropping the
    # respelling puts `pass account_uuid=` back on the wire, where it names a
    # parameter this API does not read: a client that follows it gets the same
    # refusal again.  Non-null was always asserted; USABLE was not, and this
    # mutation is what keeps the difference honest.
    ("api-remedy-keeps-the-module-spelling", "a remedy on the wire names the "
     "query parameter the API reads, not the Python keyword the module takes",
     "api.py",
     "    return _refusal(u.reason, u.detail, wire_remedy(u.remedy), "
     "status=status)",
     "    return _refusal(u.reason, u.detail, u.remedy, status=status)"),

    ("api-refusal-carries-a-result-key", "a refusal carries NO result key at "
     "all, so a client looping over result.rows raises instead of drawing an "
     "empty table",
     "api.py",
     '        "refusal": {\n            "reason": reason,',
     '        "result": {"rows": []},\n'
     '        "refusal": {\n            "reason": reason,'),

    # The generic fallback in `_refusal` means a nulled remedy is still a
    # STRING, so this is caught by the assertions that require the SPECIFIC
    # action -- the install command, the parameter to widen.  That is the right
    # bar: a refusal whose remedy is "this question has no answer" tells a
    # reader nothing they did not already know.
    ("api-refusal-remedy-nulled", "a refusal's remedy is the specific action a "
     "reader can take, never null and never the generic fallback",
     "api.py",
     '    return status, {\n        "outcome": OUTCOME_UNANSWERABLE,',
     '    remedy = None\n    return status, {\n'
     '        "outcome": OUTCOME_UNANSWERABLE,'),

    ("api-empty-blames-a-clause-for-an-empty-corpus", "0-of-0 is not `your "
     "filter emptied it`: on no-data no clause is named",
     "api.py",
     '    if kind == OUTCOME_NO_DATA:\n        eliminated, sole_cause = {}, None',
     '    if kind == OUTCOME_NO_DATA:\n        pass'),

    ("api-two-empties-become-one", "`no-data` and `filtered-to-nothing` are "
     "two different answers with two different messages",
     "api.py",
     '    kind = getattr(sel_like, "empty_because", None)',
     '    kind = (OUTCOME_FILTERED\n'
     '            if getattr(sel_like, "empty_because", None) else None)'),

    ("api-remedy-names-the-predicate-not-the-parameter", "the remedy names the "
     "URL parameter the caller can change, not the engine's clause label",
     "api.py",
     '        param = param_for_clause(sole)',
     '        param = None'),

    ("api-cross-account-total", "there is no total across accounts anywhere, "
     "not even for tokens: two accounts are two plans and two denominators",
     "api.py",
     '            "total": None,\n            "no_total_note": NO_TOTAL_NOTE,',
     '            "total": {"requests": sum(\n'
     '                b["stats"]["requests"] for v in out.values()\n'
     '                if isinstance(v, dict) and "buckets" in v\n'
     '                for b in v["buckets"])},\n'
     '            "no_total_note": NO_TOTAL_NOTE,'),

    ("api-legacy-route-still-answers", "the unversioned routes are gone and "
     "say where the API moved, rather than serving a second envelope for ever",
     "api.py",
     '        if path.startswith(PATH_LEGACY) and not path.startswith(PATH_V1):',
     '        if False:'),

    ("api-groupable-offers-a-measurement", "the field schema offers labels as "
     "buckets, never measurements: grouping a measurement is a histogram",
     "api.py",
     '    if col in query.OBSERVED_NUMERIC:\n        return False\n    return True',
     '    return True'),

    ("api-synthetic-rows-hidden", "a generated row is counted and named the "
     "moment it enters a result",
     "api.py",
     '            "synthetic_rows": len([r for r in kept\n'
     '                                   if r.get("source") == "synthetic"]),',
     '            "synthetic_rows": 0,'),

    ("api-never-observed-column-omitted", "a column that has never carried a "
     "value is LISTED with its zero, not omitted the way a facet list omits it",
     "api.py",
     '            if entry["never_observed"]:',
     '            if entry["never_observed"]:\n                continue\n'
     '            if False:'),

    ("api-tag-column-has-no-cardinality", "a `tags.<key>` field is a real "
     "column and arrives with its counts: stats taken from the pinned ledger "
     "list leave it unlabelled, which is the empty-control bug in a hat",
     "api.py",
     "        avail = d.field_availability(q, account_uuid=uuid, columns=wanted)",
     "        avail = d.field_availability(q, account_uuid=uuid)"),

    ("api-truncation-hidden", "a top-N that cut the tail says so: one that "
     "looks complete is a coverage claim",
     "api.py",
     '                "truncated": truncated,',
     '                "truncated": False,'),

    ("api-absent-reported-as-zero", "a pinned read cannot separate an absent "
     "key from an explicit null, and says so rather than claiming 0",
     "api.py",
     '                    "absent": st.get("absent"),',
     '                    "absent": 0,'),

    ("api-histogram-drops-the-empty-buckets", "a missing bucket and a zero "
     "bucket are different claims; a chart draws them identically only if the "
     "server withholds one",
     "api.py",
     '        out.append(have if have else\n'
     '                   {"bucket": b, "rows": 0, "value": 0, "metric": metric})',
     '        if have:\n            out.append(have)'),

    ("api-histogram-re-buckets-silently", "a range too wide for the interval is "
     "refused, never silently re-bucketed: re-bucketing changes the answer",
     "api.py",
     '            if n > MAX_BUCKETS:\n'
     '                # NEVER silently re-bucketed: that changes the answer, and a',
     '            if n > MAX_BUCKETS and False:\n'
     '                # NEVER silently re-bucketed: that changes the answer, and a'),

    ("api-histogram-cap-only-on-a-closed-range", "the cap is checked against "
     "the span that will be FILLED: checking only a fully-bounded range leaves "
     "`interval=1&since=0` to build a bucket per second and hang",
     "api.py",
     "        lo, hi = _bucket_span(hist[\"buckets\"], interval, since, until)\n"
     "        if lo is not None:",
     "        lo, hi = _bucket_span(hist[\"buckets\"], interval, since, until)\n"
     "        if lo is not None and since is not None and until is not None:"),

    ("api-duckdb-missing-answers-nothing", "a missing engine is a named "
     "refusal with the install command, never a plausible empty result",
     "api.py",
     '        if self.duck_store is None:',
     '        if False:'),

    ("api-window-request-list-reads-as-empty", "a request list that could not "
     "be read carries no `rows` key: an empty list reads as a window with no "
     "requests in it",
     "api.py",
     '                "unavailable": requests_unavailable,',
     '                "rows": [], "n": 0,\n'
     '                "unavailable": requests_unavailable,'),

    ("api-null-bucket-stringified", "a request that carried no value keeps its "
     "own null flag; stringifying it makes it a bucket called \"None\"",
     "api.py",
     '        out.append({"value": k, "value_is_null": k is None,',
     '        out.append({"value": str(k), "value_is_null": False,'),

    ("api-store-problems-dropped", "a batch the reader could not reassemble "
     "travels in EVERY payload: without it the account renders identically to "
     "one that never shipped",
     "api.py",
     '        problems = _merge_problems(\n            snap.problems,',
     '        problems = _merge_problems(\n            (),'),

    ("api-stream-b-loses-its-host", "a stream-B record without its shipping "
     "host is unattributable: the v1 shape has no host column of its own",
     "api.py",
     '            kept.append({"record": rec, "shipping_host": host})',
     '            kept.append({"record": rec, "shipping_host": "unknown"})'),

    ("api-verify-claims-agreement-it-never-checked", "verify= re-runs the pure "
     "predicate and refuses on disagreement; a bare True is a claim about a "
     "check that never ran",
     "api.py",
     "            verified = got == want",
     "            verified = True"),

    # ----------------------------------------- the repairs of 2026-08, pinned
    #
    # Every one of these was a CONFIRMED finding against a tree whose suite was
    # green, so each row here is a guard that did not exist before.

    ("api-histogram-crosses-accounts", "a bucket's `value` is a SUM, so a "
     "histogram with no account= is the cross-account total that exists "
     "nowhere here -- it answered 200 ok over every ledger in the store",
     "store.py",
     "        acct = account_uuid or q.account_uuid\n"
     "        if acct is None:\n"
     "            present = self.paths.accounts()\n"
     "            if len(present) > 1:\n"
     "                return self._crosses_accounts(\n"
     "                    present, \"ask for one account at a time\")",
     "        acct = account_uuid or q.account_uuid"),

    ("api-window-coverage-loses-its-boolean", "`known` travels wherever a "
     "fraction does: on a window row the JSON null was the only thing "
     "separating `nobody said` from `nobody was listening`",
     "api.py",
     '    d["known"] = d.get("fraction") is not None\n'
     '    d["basis_is"] = basis_is(d.get("basis"))',
     '    d.pop("known", None)'),

    ("api-coverage-payload-loses-its-boolean", "the same guarantee on the "
     "document-level coverage object",
     "api.py",
     '        "known": fraction is not None,',
     '        "known_": fraction is not None,'),

    ("api-undeclared-basis", "every basis a window can carry is in the served "
     "catalogue: `unknown` was emitted by every window in the store and "
     "declared nowhere",
     "api.py",
     '    "unknown": "unknown",\n}',
     '}'),

    ("api-basis-family-undeclared", "the parameterised basis is served as a "
     "prefix, so a client can still classify it from data alone",
     "api.py",
     '    "attestations-with-": "measured-with-unreadable-spans",',
     '    "never-emitted-prefix-": "unknown",'),

    ("store-problems-accumulate", "`meta.store_problems` describes the store, "
     "not how many queries somebody has run: unreset, it reached 3116 entries "
     "claiming 3118 torn lines over a store holding 3",
     "api.py",
     "        if self.duck_store is not None:\n"
     "            self.duck_store.reset_problems()",
     "        if False:\n"
     "            self.duck_store.reset_problems()"),

    ("store-problems-append-per-observation", "one entry per FILE with a "
     "count, never one per observation: `/search` calls select and page and "
     "each notices the same tear",
     "store.py",
     "        for p in self.problems:\n"
     "            if (p.get(\"reason\"), tuple(p.get(\"files\") or ())) \\\n"
     "                    == (reason, tuple(paths)):\n"
     "                p[\"count\"] = max(p.get(\"count\") or 0, int(count))\n"
     "                return",
     "        pass"),

    ("store-problems-lose-the-shared-key", "`reason` is `snap.problems`' key: "
     "with `problem` instead, a client rendering `p.reason` shows undefined "
     "for every problem this side produced",
     "store.py",
     '            "reason": reason,\n            "account_uuid"',
     '            "problem": reason,\n            "account_uuid"'),

    ("store-problems-blind-on-the-reader-half", "a torn ledger is named on the "
     "reconciler-only routes too, rather than waiting for somebody to run a "
     "stream-A query",
     "serve.py",
     "    out = []\n"
     "    for stream, fname in sorted(STREAM_FILES.items()):",
     "    out = []\n"
     "    for stream, fname in []:"),

    ("problems-not-deduped", "two readers seeing one tear is one tear: adding "
     "them up is how an envelope claimed 3118 unparseable lines over 3",
     "api.py",
     "            if key in seen:",
     "            if False:"),

    ("values-field-unchecked", "an identifier taken from a question is "
     "whitelisted before it reaches the SQL: unchecked, a typo was a 500 "
     "quoting the generated SQL back at the caller",
     "api.py",
     "        for col in wanted:\n"
     "            ok, _kind = query._known_column(col)\n"
     "            if not ok:",
     "        for col in wanted:\n"
     "            ok, _kind = query._known_column(col)\n"
     "            if False:"),

    ("ident-escapes-instead-of-refusing", "`_ident` enforces the precondition "
     "its docstring states; escaping an unchecked name is the half a future "
     "reader is invited to delete",
     "duck.py",
     "    if col not in LEDGER_SQL_TYPES and col not in SAMPLE_SQL_TYPES:",
     "    if False:"),

    ("unknown-account-answers-no-data", "an account nobody has ever seen is a "
     "named 404, not a 200 whose message asserts the account exists and its "
     "shipper is behind",
     "api.py",
     "        known = uuid in snap.report.accounts\n"
     "        if not known and self.duck_store is not None:\n"
     "            known = self.duck_store.paths.knows(uuid)",
     "        known = True"),

    ("account-joined-into-a-path", "`?account=` is refused by membership "
     "before any join: `os.path.join` discards its root, so an absolute one "
     "read any ledger.jsonl on the machine",
     "store.py",
     "        if uuid not in self.accounts():\n            return None",
     "        if False:\n            return None"),

    ("account-path-not-contained", "the realpath check behind the membership "
     "one, for a symlink planted inside the store",
     "store.py",
     "        if not os.path.realpath(p).startswith(root + os.sep):\n"
     "            return None",
     "        if False:\n            return None"),

    ("nan-is-a-bound", "NaN is not a position in time: the two engines order "
     "it oppositely, so `until=nan` answered `ok` over the whole corpus while "
     "the declared oracle returned nothing",
     "query.py",
     "        if v is not None and not _finite(v):",
     "        if False:"),

    ("nan-is-a-range-bound", "the same, one clause over, where it diverges in "
     "the other direction",
     "query.py",
     "            if b is not None and not _finite(b):",
     "            if False:"),

    ("nan-cursor", "a cursor position that is not finite was never issued: "
     "editing a real cursor's ts keeps its fingerprint and pages for ever",
     "query.py",
     "    if not math.isfinite(pos):",
     "    if False:"),

    ("nan-param", "`_num_param` refuses it at the door as well, because "
     "`String(NaN)` is `\"NaN\"` and a front end sends exactly that",
     "api.py",
     "    if math.isnan(v):",
     "    if False:"),

    ("wrong-typed-value-is-cast", "a string against a numeric column matches "
     "NOTHING, as the oracle does: cast, `529 = ' 529 '` is TRUE and "
     "`529 = 'abc'` raises a 500 over a typo",
     "duck.py",
     "        elif not comparable(col, w):",
     "        elif False:"),

    ("coerced-values-uncounted", "a value the pinned type could not hold is "
     "counted, not assumed: the row survives whole, so nothing else sees it",
     "store.py",
     "            if not duck.coercion_of(k, t, fits):\n                continue",
     "            if True:\n                continue"),

    ("coerced-values-note-lies", "the aggregate note must not assert the "
     "payload was silent about a value the payload stated precisely",
     "store.py",
     '                "token columns are null in some rows and are summed as "\n'
     '                "absent, not as 0: %s. Null here means the payload carried no "',
     '                "token columns are null where the payload did not state them "\n'
     '                "and are summed as absent, not as 0: %s. XX "'),

    ("no-data-explained-by-a-caveat", "the no-data sentence comes from its own "
     "field: mined from `notes[0]` it published the pagination note on "
     "/search and the list-rates note on /aggregate",
     "api.py",
     '        message = getattr(sel_like, "no_data_note", None) or (',
     '        message = (list(getattr(sel_like, "notes", None) or ()) or [None])[0] or ('),

    ("reader-fault-leaks-the-sql", "a 500 names the exception type; the "
     "message carries the generated SQL to a caller asked for no token",
     "serve.py",
     '                    "reader-failed", type(exc).__name__,',
     '                    "reader-failed", "%s: %s" % (type(exc).__name__, exc),'),

    ("lookup-has-no-account-clause", "the account is in the query as well as "
     "in the file list, so the clause survives a relaxed path check",
     "store.py",
     '        if account_uuid:\n            spec["account_uuid"] = account_uuid',
     '        if False:\n            spec["account_uuid"] = account_uuid'),

]



def run_suite(srv_dir):
    env = dict(os.environ, SRV_DIR=srv_dir, PYTHONDONTWRITEBYTECODE="1")
    p = subprocess.run([sys.executable, SUITE], env=env,
                       capture_output=True, text=True)
    out = p.stdout + p.stderr
    m = re.search(r"(\d+) passed, (\d+) failed", out)
    if not m:
        return None, None, out
    return int(m.group(1)), int(m.group(2)), out


def failing_names(out, limit=3):
    names = []
    for line in out.splitlines():
        if line.startswith("FAIL "):
            names.append(line[5:].strip())
    return names[:limit], len(names)


def main(argv=None):
    # An optional substring filter, because the full matrix takes minutes and
    # the thing you want after fixing one finding is that one row again.  It
    # narrows what is RUN and never what is reported: the summary says how many
    # of the whole matrix were selected, so a filtered run cannot be mistaken
    # for a clean one.
    argv = sys.argv[1:] if argv is None else argv
    only = argv[0] if argv else None
    selected = [m for m in MUTATIONS if only is None or only in m[0]]
    if not selected:
        print("no mutation id contains %r; there are %d"
              % (only, len(MUTATIONS)))
        return 2
    base_pass, base_fail, out = run_suite(SERVER)
    if base_fail is None:
        print("the suite does not run on an unmutated tree:\n" + out)
        return 2
    skips = [ln for ln in out.splitlines() if ln.strip().startswith("SKIP")]
    print("baseline: %d passed, %d failed, %d skipped\n"
          % (base_pass, base_fail, len(skips)))
    if base_fail:
        print("refusing to mutate a red tree")
        return 2
    # A SKIPPING tree is refused for the same reason a red one is, and the
    # reason is sharper: a red tree makes the matrix meaningless, a skipping
    # one makes it CONFIDENTLY WRONG.  Measured on this tree -- 118 caught, 42
    # SURVIVED, 1 BAD PATCH without duckdb against 161 caught, 0 survived, 0
    # bad patches with it, over the identical matrix and the identical source.
    # That pair was taken over the matrix AS IT THEN STOOD, 161 rows.  It is
    # 169 now and still 169 caught / 0 survived / 0 bad patches; the no-duckdb
    # half is deliberately not re-measurable, because the refusal below is the
    # thing those numbers bought.
    # All 42 were false.  They did not survive because nothing asserts the
    # behaviour; they survived because the assertion self-skipped, and this
    # file then printed "SURVIVED <id> -- nothing asserts: <behaviour>" for
    # each -- a positive, false sentence about the very guarantees the DuckDB
    # swap put at risk (no cross-account total, the three-way outcome
    # distinction, coverage on every aggregate, the remedy).  A reader acts on
    # that by "fixing" controls that already work.
    #
    # This is the cardinal sin located inside the one tool whose whole job is
    # to prove the suite is not decoration, so it refuses rather than warns.
    if skips:
        print("refusing to mutate a SKIPPING tree: %d baseline test(s) "
              "self-skipped, so SURVIVED below would mean 'never ran', not "
              "'nothing asserts'.\n%s\n\nInstall the missing dependency and "
              "re-run; `python3 -m venv env && env/bin/pip install duckdb` is "
              "enough." % (len(skips), "\n".join(skips)))
        return 2

    survived, badpatch, rows = [], [], []
    tmp = tempfile.mkdtemp(prefix="srv-mutate-")
    try:
        for mid, behaviour, fn, old, new in selected:
            work = os.path.join(tmp, mid)
            os.makedirs(work, exist_ok=True)
            dst = os.path.join(work, "srv")
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(SRC, dst,
                            ignore=shutil.ignore_patterns("__pycache__"))
            # The tree under test has to be COMPLETE, not just importable.
            # It was `srv/` alone, so `test_api_the_readme_names_exactly_the_
            # routes_that_exist` raised FileNotFoundError in EVERY mutated run
            # -- one guaranteed failure per row, which made every mutation
            # "caught" whether or not anything asserted it.  Three real
            # survivors were sitting behind it. A harness that manufactures
            # its own evidence is the decoration this file exists to prevent.
            shutil.copy2(os.path.join(SERVER, "README.md"),
                         os.path.join(work, "README.md"))
            path = os.path.join(dst, fn)
            src = open(path, encoding="utf-8").read()
            n = src.count(old)
            if n != 1:
                badpatch.append((mid, "%d matches in %s" % (n, fn)))
                rows.append(("BAD PATCH", mid, "-", "-", behaviour))
                continue
            open(path, "w", encoding="utf-8").write(src.replace(old, new, 1))

            p, f, out = run_suite(work)
            if f is None:
                # A mutation that stops the suite importing at all still
                # proves the behaviour is reachable, but it proves nothing
                # about the assertions, so it is not counted as caught.
                rows.append(("CRASHED", mid, "-", "-", behaviour))
                badpatch.append((mid, "suite did not run"))
                continue
            names, total = failing_names(out)
            if f == 0:
                survived.append((mid, behaviour))
                rows.append(("SURVIVED", mid, str(p), "0", behaviour))
            else:
                rows.append(("caught", mid, str(p), str(f), behaviour))
            rows[-1] = rows[-1] + (names,)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    w = max(len(r[1]) for r in rows)
    print("%-9s %-*s %6s %6s  %s" % ("verdict", w, "mutation", "pass", "fail",
                                     "behaviour"))
    print("-" * 100)
    for r in rows:
        print("%-9s %-*s %6s %6s  %s" % (r[0], w, r[1], r[2], r[3], r[4]))
        if len(r) > 5 and r[5]:
            for nm in r[5]:
                print("%-9s %-*s %13s %s" % ("", w, "", "", nm.split("\n")[0]))
    print("-" * 100)
    print("%d mutations%s, %d caught, %d survived, %d bad patches"
          % (len(selected),
             "" if only is None else " of %d (filtered on %r)"
             % (len(MUTATIONS), only),
             len(selected) - len(survived) - len(badpatch),
             len(survived), len(badpatch)))
    for mid, behaviour in survived:
        print("SURVIVED  %s -- nothing asserts: %s" % (mid, behaviour))
    for mid, why in badpatch:
        print("BAD PATCH %s -- %s" % (mid, why))
    return 1 if (survived or badpatch) else 0


if __name__ == "__main__":
    sys.exit(main())
