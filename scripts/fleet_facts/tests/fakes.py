"""A scripted stand-in for runner.run, and helpers for subprocess tests."""
from __future__ import annotations

from typing import Callable

from fleet_facts.runner import Result


class FakeRunner:
    def __init__(self, handler: Callable[[list[str]], Result]):
        self.handler = handler
        self.calls: list[dict] = []

    def __call__(self, argv, *, timeout, env=None, cwd=None):
        self.calls.append({"argv": list(argv), "timeout": timeout, "env": dict(env or {}), "cwd": cwd})
        return self.handler(list(argv))

    @property
    def argvs(self) -> list[list[str]]:
        return [c["argv"] for c in self.calls]


def out(text: str, rc: int = 0, err: str = "") -> Result:
    return Result(rc, text, err)
