"""Errors, each with the exit code the CLI returns for it.

0  done (an item filed with its notice pending is still done:  the decision landed)
1  something failed (Linear, the network, a missing tool)
2  refused:  bad flags, a seat that does not match, a secret or personal data in the text, a change
   to an item that was already announced, a parent the filer does not hold
3  `status` or `adopt` named something that is not a needs-owner item
4 to 6  the `board` CLI's own exit code, passed through when a write failed (4 no credentials,
   5 rate limit, 6 no bearer-verified seat or a refused authentication)
"""

from __future__ import annotations


class NeedsOwnerError(Exception):
    exit_code = 1


class Refusal(NeedsOwnerError):
    """The request is not allowed or not valid.  Nothing was sent."""

    exit_code = 2


class NotFound(NeedsOwnerError):
    exit_code = 3


class BoardFailed(NeedsOwnerError):
    """A `board` write failed.  `exit_code` is board's own code when it is one of 4 to 6."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = code if code in (4, 5, 6) else 1
