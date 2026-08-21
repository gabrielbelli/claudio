"""Model id normalisation.  Nothing here prices anything.

This file used to be `pricing.py`, which recomputed each request's cost from a
hand-written rate table and used that as an attribution weight.  It was wrong
twice -- an intro rate reality did not apply, and a cache-write multiplier
arrived at by solving for whatever reproduced Claude Code's figure rather than
from any documentation.

Claude Code already exports `cost_usd` on every request.  Recomputing it meant
maintaining a rate card forever, getting it wrong, and drifting silently the
next time Anthropic changed a price -- to produce a number we already had.  So
the rate table is gone: the ledger records Claude Code's own figure, and a
request that carries none records none.

On a subscription that figure is notional anyway -- Claude Code computes it
from token counts at list rates, and a Max or Pro plan is never charged it.  It
is recorded as a relative size, and nothing here divides anything by it.
"""

import re

_CONTEXT_VARIANT = re.compile(r"\[[^\]]*\]$")     # claude-opus-5[1m]
_DATE_SNAPSHOT = re.compile(r"-\d{8}$")           # claude-haiku-4-5-20251001


def normalise(model):
    """Strip the context-variant marker and the dated snapshot suffix.

    `claude-opus-5[1m]` -> `claude-opus-5`
    `claude-haiku-4-5-20251001` -> `claude-haiku-4-5`

    The recorded value in this user's samples is `claude-opus-5[1m]`, an id
    with a context-variant marker, so this is exercised on real data.
    """
    if not model:
        return ""
    m = _CONTEXT_VARIANT.sub("", model.strip())
    m = _DATE_SNAPSHOT.sub("", m)
    return m
