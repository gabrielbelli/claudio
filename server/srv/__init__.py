"""The reconciliation core.

Pure logic: records in, reconciled windows out.  Nothing in this package opens
a file, a socket or a database, and a static test asserts it -- the storage
decision is deliberately still open, and this is the half of the server that
must not depend on it.

  wire       what a shipper may send; schema versions, guards, idempotency keys
  ingest     dedupe and partition by account, in memory
  window     window reconstruction per account, from stream B
  coverage   what fraction of a window had any machine reporting
  attribute  attributed and RESIDUAL for a closed window
  reconcile  the one entry point, `reconcile()` / `reconcile_ingest()`
"""

from . import attribute, coverage, ingest, reconcile, window, wire  # noqa: F401

__all__ = ["wire", "ingest", "window", "coverage", "attribute", "reconcile"]
