#!/usr/bin/env python3
"""Run wrangler for the agent-sync MCP Worker with credentials from Infisical, never from ~/.secrets (AFC-347).

    python3 -I wrangler_infisical.py deploy                                   # wrangler deploy
    python3 -I wrangler_infisical.py put RECALL_API_TOKEN shared:/:RECALL_API_TOKEN
    python3 -I wrangler_infisical.py put LINEAR_WISE_CLIENT_ID afc:/linear:LINEAR_WISE_CLIENT_ID

wrangler's Cloudflare pair (CLOUDFLARE_JAY_ACCOUNT_EMAIL, CLOUDFLARE_JAY_API_KEY) comes from Infisical's
Fleet-OPS project, prod, /global-api-keys, through the INFISICAL_AUTOMATION identity in the environment,
exactly as install_seat_key.py reads it.  `put NAME PROJECT:PATH:KEY` reads one Infisical secret and
hands it to `wrangler secret put NAME` on stdin;  PROJECT is afc, fleet-ops or shared (or another project's slug).
No value is printed, logged or written to a file, and any wrangler output line that contains one is
redacted.  Run from scripts/agent-sync-mcp/.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("install_seat_key", HERE / "install_seat_key.py")
seatkey = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seatkey)

# Projects by short name.  Only afc's id was already in this repo (install_seat_key.py);  the others are
# looked up by slug at run time, so no other Infisical project id lives in public source (Kody Rule 26).
PROJECT_SLUGS = {"fleet-ops": "fleet-ops", "shared": "shared-at-ct-tg-v7"}


def project_id(name: str) -> str:
    if name == "afc":
        return seatkey.INFISICAL_PROJECT
    return seatkey.infisical_project_id(PROJECT_SLUGS.get(name, name))


def infisical_value(ref: str) -> str:
    try:
        project, path, key = ref.split(":", 2)
    except ValueError:
        seatkey.fail(
            "the source must be PROJECT:PATH:KEY, for example afc:/linear:LINEAR_WISE_CLIENT_ID"
        )
    value = seatkey.infisical_secrets(project_id(project), path).get(key, "")
    if not value:
        seatkey.fail(
            f"{key} is missing or empty in Infisical {project} {seatkey.INFISICAL_ENV}{path}"
        )
    return value


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] == "put":
        if len(argv) != 3:
            seatkey.fail("usage:  put WORKER_SECRET_NAME PROJECT:PATH:KEY")
        value = infisical_value(argv[2])
        code = seatkey.wrangler(["secret", "put", argv[1]], value, value)
        if code == 0:
            print(f"set {argv[1]}")
        return code
    return seatkey.wrangler(argv, None, "")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
