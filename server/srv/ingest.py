"""Ingest and dedupe, in memory, over lists of records.

No file, no socket, no database.  This module takes batches as the door hands
them over and produces the three deduped collections `reconcile` consumes,
partitioned by account.  Whatever the storage decision turns out to be, it
appends bytes and then calls this over what it read back.

Three properties, each of which has cost this project data before:

  Re-ingesting the same batch is a no-op.  Not "nearly" -- the second pass
  must produce the identical collections and count the whole batch as
  duplicates.  A doubled stream-A row doubles attributed weight and therefore
  *shrinks* the residual, which is the one number here nobody would question.

  Accounts never mix.  A record is placed by `account_uuid` and by nothing
  else.  There is no fallback to the `account` label (a user-chosen string
  `--tag account=` overwrites) and none to `email` -- the join exists, but
  performing it here would be a guess written into a value, and the whole
  point of D9 is that the stamp is applied on the machine where the answer is
  actually readable.

  A refusal is counted and named.  Nothing is dropped quietly, and the report
  carries the refusals so a zero can be told apart from a silence.
"""

from . import wire


class Refusal(object):
    """One record the server would not place, and why."""

    __slots__ = ("stream", "reason", "detail", "record")

    def __init__(self, stream, reason, detail=None, record=None):
        self.stream = stream
        self.reason = reason
        self.detail = detail
        self.record = record

    def __repr__(self):
        return "Refusal(%s, %s, %s)" % (self.stream, self.reason, self.detail)


class Batch(object):
    """What one POST carried: a manifest, its records, and the token's tenant.

    `tenant` is the account UUID the bearer token maps to.  The server never
    reads an identity out of the payload (D7) -- it compares.  A record whose
    `account_uuid` disagrees with the token's fails the WHOLE batch, and
    nothing from it is placed: a batch is one file range from one machine, so
    a disagreeing record means the shipper's `accounts=` filter is wrong, and
    accepting the rest would publish the good half of a misdirected file.
    """

    __slots__ = ("manifest", "records", "tenant")

    def __init__(self, manifest, records, tenant):
        self.manifest = manifest
        self.records = list(records)
        self.tenant = tenant


class Ingest(object):
    """The deduped collections, and what was refused getting there."""

    def __init__(self):
        self.ledger = {}              # request_id -> row
        self.ledger_fp = {}           # request_id -> fingerprint
        self.samples = {}             # sample_key -> (record, host)
        self.attestations = {}        # attest_key -> record
        self.refusals = []
        self.batches_refused = []
        self.counts = {
            "ledger_accepted": 0, "ledger_duplicates": 0,
            "ledger_collisions": 0,
            "samples_accepted": 0, "samples_duplicates": 0,
            "attestations_accepted": 0, "attestations_replaced": 0,
            "attestations_superseded_by_older": 0,
            "attestations_merged": 0,
            "refused": 0, "batches_refused": 0,
        }
        # Fields the current schema defines that some accepted record did not
        # carry.  Absent, not null -- this is what tells a reader that a
        # column was added after the record was written.
        self.absent_fields = {}       # stream -> {field: count}
        self.schemas = {}             # stream -> {schema: count}
        self.hosts = {}               # account_uuid -> set(host)

    # -- accounting ------------------------------------------------------

    def _refuse(self, stream, reason, detail=None, record=None):
        self.refusals.append(Refusal(stream, reason, detail, record))
        self.counts["refused"] += 1

    def _note_schema(self, stream, schema, rec):
        per = self.schemas.setdefault(stream, {})
        per[schema] = per.get(schema, 0) + 1
        missing = wire.absent_fields(rec, stream, schema)
        if missing:
            per_f = self.absent_fields.setdefault(stream, {})
            for f in missing:
                per_f[f] = per_f.get(f, 0) + 1

    def _note_host(self, account_uuid, host):
        if host:
            self.hosts.setdefault(account_uuid, set()).add(host)

    # -- the door --------------------------------------------------------

    def add_batch(self, batch, now):
        """Place one batch.  Returns the number of records newly accepted."""
        ok, why = wire.manifest_check(batch.manifest)
        if not ok:
            self.batches_refused.append((why, batch.manifest))
            self.counts["batches_refused"] += 1
            return 0
        stream = batch.manifest["stream"]
        host = batch.manifest["host"]

        if batch.manifest["count"] != len(batch.records):
            self.batches_refused.append(("manifest-count-mismatch",
                                         batch.manifest))
            self.counts["batches_refused"] += 1
            return 0

        # D7, and it is a batch-level verdict on purpose: nothing is written
        # before every record has been checked.
        for rec in batch.records:
            if isinstance(rec, dict) and rec.get("account_uuid") not in (
                    batch.tenant, None):
                self.batches_refused.append(("identity", batch.manifest))
                self.counts["batches_refused"] += 1
                return 0

        added = 0
        for rec in batch.records:
            ok, why = wire.check(rec, stream, now)
            if not ok:
                self._refuse(stream, why, record=rec)
                continue
            if rec["account_uuid"] != batch.tenant:
                self._refuse(stream, "identity", record=rec)
                continue
            if stream == wire.STREAM_LEDGER:
                n = self._add_ledger(rec, host)
            elif stream == wire.STREAM_SAMPLES:
                n = self._add_sample(rec, host)
            else:
                n = self._add_attestation(rec)
            # Counted for a record that was STORED, never for one that arrived.
            # `schemas` and `absent_fields` describe the collection a report is
            # built from -- "this window was reconstructed from records written
            # before `agent` existed" -- so a re-ingest must not double them.
            # What arrived is already counted, separately and by name, in
            # `counts`; a re-ingest is a duplicate there and nothing else.
            if n:
                self._note_schema(stream, wire.schema_of(rec, stream), rec)
            added += n
        return added

    # -- stream A --------------------------------------------------------

    def _add_ledger(self, row, host):
        rid = row["request_id"]
        fp = wire.ledger_fingerprint(row)
        if rid in self.ledger:
            # Two events wearing one face, kept apart exactly as
            # `cu.ledger.read` keeps them apart: the same request written
            # twice is fully repaired by dropping the copy, while two
            # different requests that hashed alike lose a real record and can
            # only be reported.  The hash is never widened to fix it -- that
            # re-identifies every row already stored and doubles the ledger on
            # the next pass.
            if self.ledger_fp[rid] == fp:
                self.counts["ledger_duplicates"] += 1
            else:
                self.counts["ledger_collisions"] += 1
                self._refuse(wire.STREAM_LEDGER, "collision", detail=rid,
                             record=row)
            return 0
        self.ledger[rid] = row
        self.ledger_fp[rid] = fp
        self.counts["ledger_accepted"] += 1
        self._note_host(row["account_uuid"],
                        row.get("host") or host)
        return 1

    # -- stream B --------------------------------------------------------

    def _add_sample(self, rec, host):
        key = wire.sample_key(rec, host)
        if key in self.samples:
            self.counts["samples_duplicates"] += 1
            return 0
        self.samples[key] = (rec, host)
        self.counts["samples_accepted"] += 1
        # v1 records carry no host column at all, so the shipping host is the
        # only machine identity there is for 65 of the real records.
        self._note_host(rec["account_uuid"], rec.get("host") or host)
        return 1

    # -- stream C --------------------------------------------------------

    def _add_attestation(self, rec):
        """Last EMITTED wins, not last arrived.

        This was `self.attestations[key] = rec` with no comparison, and "a
        later one carries later counts" was true of emission and false of
        arrival: a machine catching up on a backlog ships old ranges after new
        ones as a matter of routine, and out-of-order delivery is the normal
        state of a queue.  `emitted_at` was declared in `wire.ATTEST_FIELDS`
        and read by no line of code in the package.

        Reproduced against the real alpha capture: two attestations from one
        host for one window, one covering the whole window and one covering
        none of it.  Stale last -> coverage 0.0 and `refused: no-coverage`;
        fresh last -> coverage 1.0 and nothing refused.  Identical records,
        identical counts, opposite verdicts -- and the refusal prints "no
        machine was observed listening during this window", a claim that
        machine explicitly contradicted in a record the server is holding.
        Coverage is the guard on the residual, so this is the one number the
        design is built to protect.

        Equal `emitted_at` is not a tie to be broken: the two are the same
        emission described twice, so their spans are CONCATENATED and
        `coverage.union` merges them at read time -- which keeps each span's
        `clean` flag instead of picking one record's word over the other's.
        """
        key = wire.attest_key(rec)
        cur = self.attestations.get(key)
        if cur is not None:
            new_at, old_at = rec.get("emitted_at"), cur.get("emitted_at")
            if wire._is_number(new_at) and wire._is_number(old_at):
                if new_at < old_at:
                    self.counts["attestations_superseded_by_older"] += 1
                    self._refuse(wire.STREAM_ATTEST,
                                 "attestation-older-than-held",
                                 detail=repr(key), record=rec)
                    return 0
                if new_at == old_at:
                    have = [list(s) for s in (cur.get("up_spans") or [])]
                    extra = [list(s) for s in (rec.get("up_spans") or [])
                             if list(s) not in have]
                    if not extra:
                        # Byte-for-byte the same emission: idempotent, and the
                        # collection must come out identical or a re-ingest is
                        # no longer a no-op.
                        self.counts["attestations_replaced"] += 1
                        return 0
                    merged = dict(cur)
                    merged["up_spans"] = have + extra
                    self.attestations[key] = merged
                    self.counts["attestations_merged"] += 1
                    return 0
            self.attestations[key] = rec
            self.counts["attestations_replaced"] += 1
            return 0
        self.attestations[key] = rec
        self.counts["attestations_accepted"] += 1
        self._note_host(rec["account_uuid"], rec.get("host"))
        return 1

    # -- reading back ----------------------------------------------------

    def accounts(self):
        """Every account UUID that appears in any placed record."""
        seen = set()
        for row in self.ledger.values():
            seen.add(row["account_uuid"])
        for rec, _host in self.samples.values():
            seen.add(rec["account_uuid"])
        for rec in self.attestations.values():
            seen.add(rec["account_uuid"])
        return seen

    def rows_for(self, account_uuid):
        return [r for r in self.ledger.values()
                if r["account_uuid"] == account_uuid]

    def samples_for(self, account_uuid):
        """[(record, shipping host)] for one account, and one account only."""
        return [(r, h) for (r, h) in self.samples.values()
                if r["account_uuid"] == account_uuid]

    def attestations_for(self, account_uuid):
        return [r for r in self.attestations.values()
                if r["account_uuid"] == account_uuid]


def ingest(batches, now):
    """Place every batch.  Pure: lists in, one `Ingest` out."""
    ing = Ingest()
    for b in batches:
        ing.add_batch(b, now)
    return ing
