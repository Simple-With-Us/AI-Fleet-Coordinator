"""Review-completion and findings alerts for relay-owned PRs.

Reads review activity for PRs in the relay's ownership index and wakes the
exact chat session that opened the PR through the fleet wake-router.  The
origin session is a caller CLAIM recorded at creation (untrusted);  the
router validates it against the live registry before any delivery, and a
wake whose named session is not live is held, never re-routed.

Python 3.11+, standard library only.
"""
