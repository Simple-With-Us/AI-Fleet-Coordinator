"""The seat a command acts as.

The seat is the launcher's (`AGENT_LAUNCH_SEAT`, when `AGENT_LAUNCHER` is set) or, in an ordinary
session, `AGENT_SEAT`;  `--by` is only a check that the caller means the same seat (AGENT-SYNC § Identity
Rules).  The same F3 `identity.resolve_seat` that `simple-chat` uses does the choosing, so a launched bot
whose `AGENT_SEAT` names another seat is refused here exactly as it would be there.  `board` reads only
`AGENT_SEAT`, never `AGENT_LAUNCH_SEAT`, so every child process gets the verified seat as `AGENT_SEAT`
and keeps the launcher variables as they were.
"""
from __future__ import annotations

import os
from typing import Mapping

from . import f3
from .errors import Refusal


def verified_seat(by: str, environ: Mapping[str, str] | None = None) -> str:
    """The seat of this session, which must equal `by`.  Raises Refusal."""
    env = os.environ if environ is None else environ
    resolution = f3.identity.resolve_seat(env, use_tag=False)
    if resolution.problem:
        raise Refusal(resolution.problem)
    if not resolution.seat:
        raise Refusal("no seat:  AGENT_SEAT is not set and no launcher assigned one.  Export AGENT_SEAT=<your seat tag>")
    seat = f3.identity.valid_seat(resolution.seat)
    if not seat:
        raise Refusal("%r is not a seat tag" % resolution.seat[:40])
    wanted = f3.identity.canonical_seat(by)
    if wanted != seat:
        raise Refusal("--by %s does not match this session's seat %s.  --by is only a check, never the source of identity" % (
            wanted[:40], seat))
    return seat


def child_environment(seat: str, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """A copy of the environment with AGENT_SEAT set to the verified seat."""
    env = dict(os.environ if environ is None else environ)
    env["AGENT_SEAT"] = seat
    return env
