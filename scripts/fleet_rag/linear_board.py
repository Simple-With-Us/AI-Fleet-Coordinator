"""In-process simple-board GraphQL reads for fleet-rag linear ingest.

Fleet ingest issues many GraphQL queries in one Python process.  Spawning
``simple-board api query`` per call mints a fresh OAuth client-credentials token
each time when only ``LINEAR_CLIENT_ID`` / ``LINEAR_CLIENT_SECRET`` are in the
environment.  This module loads the board CLI once and keeps one ``Auth`` +
``Linear`` client per seat for the lifetime of the ingest run, so tokens are
minted at most once per app (with the board's own expiry and 401 refresh rules).
Tokens live in memory only; they are never logged or written to disk as plain text.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import sys
import threading
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any

_BOARD_DIR = Path(__file__).resolve().parents[1] / "linear-board"
_BOARD_PATH = _BOARD_DIR / "board"

_lock = threading.Lock()
_board_mod: Any | None = None
# seat label -> Linear client (Auth is owned by Linear)
_sessions: dict[str, Any] = {}


class BoardCliFailure(Exception):
    """The board CLI stopped with a non-zero exit (``die`` or ``SystemExit``)."""

    def __init__(self, code: int, stderr: str):
        self.code = code
        self.stderr = stderr
        super().__init__(f"board exited {code}")


def load_board():
    """Import the board CLI module once (same pattern as relabel_claims.py)."""
    global _board_mod
    if _board_mod is not None:
        return _board_mod
    if not _BOARD_PATH.is_file():
        raise BoardCliFailure(
            4, f"the board CLI is missing at {_BOARD_DIR.name}/board next to fleet_rag"
        )
    with _lock:
        if _board_mod is not None:
            return _board_mod
        mod = sys.modules.get("board_cli_fleet_rag")
        if mod is not None:
            _board_mod = mod
            return mod
        loader = importlib.machinery.SourceFileLoader("board_cli_fleet_rag", str(_BOARD_PATH))
        spec = importlib.util.spec_from_loader("board_cli_fleet_rag", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        sys.modules["board_cli_fleet_rag"] = mod
        _board_mod = mod
        return mod


def reset_sessions() -> None:
    """Drop cached clients so the next ingest run starts clean."""
    _sessions.clear()


def _identity(B: Any, seat: str, client_id: str, client_secret: str):
    """An env-pair identity matching what fleet_rag passes to simple-board subprocesses."""
    app_names = B.seat_app_names(seat) if seat and seat != "LINEAR_CLIENT_ID" else ()
    return B.Identity(
        "env",
        "" if seat == "LINEAR_CLIENT_ID" else seat,
        client_id,
        client_secret,
        B.ENV_PAIR,
        "",
        tuple(n for n in app_names if n),
        "LINEAR_CLIENT_ID app",
    )


def _client_for(seat: str, client_id: str, client_secret: str):
    B = load_board()
    if seat not in _sessions:
        token_url = B.endpoint("LINEAR_BOARD_TOKEN_URL", B.TOKEN_URL)
        api_url = B.endpoint("LINEAR_BOARD_API_URL", B.GRAPHQL_URL)
        ident = _identity(B, seat, client_id, client_secret)
        auth = B.Auth(token_url, ident)
        _sessions[seat] = B.Linear(auth, api_url)
    return B, _sessions[seat]


def query(
    seat: str,
    client_id: str,
    client_secret: str,
    graphql: str,
    *,
    max_chars: int,
) -> dict:
    """One read-only GraphQL query through the board stack for this seat's app."""
    err_buf = io.StringIO()
    # Outside the try:  a board that cannot be loaded raises its own error, instead of the
    # `except B.GqlError` clause below hiding it behind an UnboundLocalError.
    B, linear = _client_for(seat, client_id, client_secret)
    try:
        with redirect_stderr(err_buf):
            data = B.clean_data(linear.call(graphql))
            B.output_limited(data, max_chars)
            return data
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        raise BoardCliFailure(code, err_buf.getvalue()) from None
    except B.GqlError as exc:
        raise BoardCliFailure(B.EXIT_ERROR, f"Linear error: {B.gql_summary(exc)}") from None


def clear_sessions_for_tests() -> None:
    """Test helper: forget clients without reloading the board module."""
    _sessions.clear()
