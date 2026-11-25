"""Read Muster's registered knobs through its existing per-folder declarations.

This path is selected only for the interactive app's status command. It does not
read an ambient token or grant write authority, and has no fallback credentials.
"""

from __future__ import annotations

import runpy
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from . import source

_PROJECT = "ai-fleet-coordinator"
_CONSUMER = "muster-data-reader"
_READ_TIMEOUT = 8.0


@lru_cache(maxsize=1)
def _reader():
    # Use the reader beside this package in the app's trusted engine checkout,
    # not a PATH command or an independently updated executable.
    path = Path(__file__).resolve().parents[1] / "infisical-scoped.py"
    return runpy.run_path(str(path))


def fetch_named(names, *, project_id: str, path: str) -> dict[str, str]:
    if project_id != source.PROJECT_ID:
        raise source.FetchFailed("muster-binding-refused")
    try:
        reader = _reader()
        account = reader["ACCOUNTS"][(_PROJECT, path)]
    except KeyError:
        raise source.FetchFailed("muster-binding-refused") from None
    except (OSError, ImportError):
        raise source.FetchFailed("scoped-reader-unavailable") from None

    fi = reader["FI"]
    home = fi.home_dir({})
    declaration = home / ".config" / "infisical" / "muster" / (account + ".json")
    args = SimpleNamespace(
        project=_PROJECT,
        consumer_binding=str(declaration),
        consumer=_CONSUMER,
        consumer_account=account,
    )
    try:
        project, token, env, expected = reader["_declared_context"](args, path)
        if project != project_id:
            raise source.FetchFailed("muster-binding-refused")
        return fi.bounded_fetch_project(
            project, path, env, token=token, names=names,
            timeout=_READ_TIMEOUT, strict_records=True,
            require_finite_expiry=True, expected_identity=expected,
        )
    except reader["ReaderError"] as exc:
        reason = {
            "consumer-binding-store-unavailable": "muster-binding-store-unavailable",
            "consumer-binding-store-refused": "muster-binding-store-unavailable",
        }.get(exc.message, "muster-binding-refused")
        raise source.FetchFailed(reason) from None
    except TimeoutError:
        raise source.FetchFailed("timeout") from None
    except (fi.SecretsError, fi.UsageError) as exc:
        reason = getattr(exc, "reason", None)
        raise source.FetchFailed(
            "token-expired-or-invalid" if reason == "token-expired-or-invalid" else "scoped-read-failed"
        ) from None
