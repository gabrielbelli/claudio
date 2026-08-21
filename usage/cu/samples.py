"""Stream B: the plan percentages the status line saw, read back as facts.

Nothing here interprets them.  Windows, intervals and horizons used to be
reconstructed in this file so that plan movement could be split across this
machine's requests; that reconstruction is gone, because the plan percentage
belongs to the *account* and no local process can see the whole account.  Two
machines each observe the same 5% -> 7% move and each claim the full 2pp;
browser, desktop and phone sessions are invisible from here; and the first
observation after a gap has no baseline at all.  Reconciling every machine's
requests against a closed window is the central server's job.

So this module reads samples and says which accounts appear in them.  The file
it reads is written by claudio's status-line shim, one JSON object per line,
append-only.  A malformed line is skipped, never fatal.
"""

import json


def read(path):
    """Yield sample dicts.  A malformed line is skipped, never fatal."""
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except ValueError:
                continue


def accounts(samples):
    """Distinct (account_uuid, account_email) pairs present in the stream."""
    seen = {}
    for s in samples:
        uuid = s.get("account_uuid")
        if uuid and uuid not in seen:
            seen[uuid] = s.get("account_email")
    return seen
