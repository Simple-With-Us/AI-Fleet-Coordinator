#!/usr/bin/env python3
"""Copy the NOTES_RELAY_CONSUMER fleet knob from Infisical to the Worker binding of the same name.

    python3 -I sync_notes_consumer.py            # checks only, changes nothing
    python3 -I sync_notes_consumer.py --apply    # wrangler secret put NOTES_RELAY_CONSUMER

NOTES_RELAY_CONSUMER names the seat whose bot is the Apple Notes relay consumer (the Mac listener's
`[notes_relay] seat`).  `notes_status` counts a check mark, a cross mark or a refusal reply only from that bot.
It is a registered fleet knob (scripts/fleet_knobs/registry.py, INFISICAL.md "Fleet knobs"):  its source of truth is
Infisical, project "AI Fleet Coordinator", environment prod, root path `/`, key `NOTES_RELAY_CONSUMER`.  The Mac
reads it through fleet_knobs.  The Worker cannot, and it is never committed to wrangler.jsonc, so this script
copies it to an encrypted Worker binding (a secret, because that is the one kind of binding `wrangler` can set
without a redeploy of the config).  Run it after the owner creates or changes the key;  the Worker reads the
binding on its next request.

It writes nothing to Infisical.  The value is a seat name, not a credential, so the check mode prints it.  It must
be a live seat of fleet-apps.json.  Read through the INFISICAL_AUTOMATION identity, like install_seat_key.py.

Standard library only.  Python 3.11+.  Run from scripts/agent-sync-mcp/.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pathlib
import re
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent
KEY = "NOTES_RELAY_CONSUMER"
INFISICAL_PATH = "/"  # fleet knobs live at the project root
SEAT_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,31}$")
FLEET_APPS = HERE.parent.parent / "fleet-apps.json"


def _install():
    """install_seat_key.py as a module:  its Infisical login, HTTP and wrangler helpers."""
    spec = importlib.util.spec_from_file_location("install_seat_key", HERE / "install_seat_key.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def live_seats() -> set[str]:
    data = json.loads(FLEET_APPS.read_text(encoding="utf-8"))
    return {
        str(row.get("tag", "")).upper() for row in data.get("seats", []) if not row.get("retired")
    }


def check_value(value: str, seats: set[str]) -> str:
    """The value, or SystemExit saying why it is not a seat the Worker may trust."""
    value = value.strip().upper()
    if not SEAT_RE.match(value):
        raise SystemExit(
            f"sync_notes_consumer: {KEY} is not a seat tag (letters, digits and hyphens)"
        )
    if value not in seats and value != "OPERATOR":
        raise SystemExit(
            f"sync_notes_consumer: {KEY} names {value}, which is not a live seat in fleet-apps.json"
        )
    return value


def read_knob(install=None) -> str:
    """NOTES_RELAY_CONSUMER from Infisical prod `/`.  Raises SystemExit when it is missing or empty."""
    install = install or _install()
    login = install.http_json(
        "POST",
        f"{install.INFISICAL_API}/api/v1/auth/universal-auth/login",
        body={
            "clientId": install.handoff("INFISICAL_AUTOMATION_CLIENT_ID"),
            "clientSecret": install.handoff("INFISICAL_AUTOMATION_CLIENT_SECRET"),
        },
    )
    token = login.get("accessToken") or raise_exit("Infisical login returned no access token")
    query = urllib.parse.urlencode(
        {
            "workspaceId": install.INFISICAL_PROJECT,
            "environment": install.INFISICAL_ENV,
            "secretPath": INFISICAL_PATH,
        }
    )
    data = install.http_json(
        "GET",
        f"{install.INFISICAL_API}/api/v3/secrets/raw?{query}",
        headers={"Authorization": "Bearer " + token},
    )
    found = {s.get("secretKey"): s.get("secretValue") or "" for s in data.get("secrets", [])}
    if not found.get(KEY, "").strip():
        raise SystemExit(
            f"sync_notes_consumer: {KEY} is not set in Infisical {install.INFISICAL_ENV}{INFISICAL_PATH};  "
            'the owner creates it (DEPLOY.md, "Notes, recall and the guide")'
        )
    return found[KEY]


def raise_exit(message: str):
    raise SystemExit("sync_notes_consumer: " + message)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--apply", action="store_true", help="write the Worker binding (default: check only)"
    )
    args = parser.parse_args(argv)
    install = _install()
    value = check_value(read_knob(install), live_seats())
    print(f"{KEY}={value}  (Infisical prod {INFISICAL_PATH}, a seat name, not a credential)")
    if not args.apply:
        print("check only:  rerun with --apply to run `wrangler secret put " + KEY + "`")
        return 0
    code = install.wrangler(["secret", "put", KEY], value, "")
    print("installed" if code == 0 else f"wrangler exited {code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
