"""Fixture-backed GitHub client for unit tests."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import github

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FixtureGitHub(github.GitHubClient):
    def __init__(self) -> None:
        super().__init__("ghs_fixture_token", request_fn=self._fixture_request)
        self.calls: list[str] = []

    def _fixture_request(
        self, method: str, url: str, headers: dict[str, str]
    ) -> tuple[bytes, dict[str, str]]:
        assert method == "GET"
        path = url.split("api.github.com", 1)[-1].split("?", 1)[0]
        self.calls.append(path)
        body = _load_fixture(path)
        return json.dumps(body).encode(), {"X-RateLimit-Remaining": "99"}


def _load_fixture(path: str) -> Any:
    # Exact file first: path with slashes -> underscores
    safe = path.strip("/").replace("/", "__")
    exact = FIXTURES / f"{safe}.json"
    if exact.is_file():
        return json.loads(exact.read_text(encoding="utf-8"))
    # Pattern files for numeric ids
    for pattern_file in sorted(FIXTURES.glob("*.json")):
        name = pattern_file.stem
        regex = "^" + re.escape(name).replace("__", "/").replace(r"\{id\}", r"[0-9]+") + "$"
        if re.fullmatch(regex, path.strip("/")):
            return json.loads(pattern_file.read_text(encoding="utf-8"))
    raise AssertionError(f"no fixture for path {path}")
