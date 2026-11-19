"""Bind the shared letter client's own scoped Infisical bootstrap token.

Only the bootstrap token lives in the host's protected consumer store.  The registry
address and signing key come from the registry-only Infisical folder through the shared reader.
No seat credentials, retired registry helper, CLI login or signing-key cache is read.
"""

from __future__ import annotations

import os
import sys
from collections.abc import MutableMapping

from fleet_infisical_bootstrap import bind_private_token

TOKEN_ENV = "SIMPLE_TOOLS_OPERATOR_REGISTRY_INFISICAL_TOKEN"
STORE_DIRECTORY = ".config/platform-letter"
STORE_NAME = "infisical-registry.token"


def bind(environ: MutableMapping[str, str] | None = None) -> str:
    """Bind only this shared letter consumer's declared private token store."""
    env = os.environ if environ is None else environ
    home = env.get("HOME") or os.path.expanduser("~")
    return bind_private_token(
        env,
        token_env=TOKEN_ENV,
        store_directory=os.path.join(home, STORE_DIRECTORY),
        store_name=STORE_NAME,
    )


def report(reason: str) -> None:
    """Report a refused binding by name and reason, never its value; keep chats usable."""
    if reason not in {"bootstrap-bound", "explicit-binding"}:
        sys.stderr.write(
            "platform-letter: %s unavailable (%s); registry access disabled\n" % (TOKEN_ENV, reason)
        )
