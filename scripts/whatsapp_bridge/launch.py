"""Isolated systemd startup adapter; inert until explicitly installed/activated.

The existing reviewed fleet_secrets package is copied to release/lib at deploy.
Only a dedicated Universal Auth credential may be loaded, never shared auth.
"""

import json
import os
import sys
from pathlib import Path

POLL_NAMES = (
    "WHATSAPP_API_KEY",
    "WA_BRIDGE_HMAC_KEY_JET",
    "WA_WORKER_URL",
    "WA_STATE_DIRECTORY",
    "WA_AGENT_HASH_JET",
    "WA_SENDER_HASH_JET",
    "WHATSAPP_BRIDGE_ENABLED",
)
BIND_NAMES = ("WHATSAPP_API_KEY", "WA_BRIDGE_HMAC_KEY_JET", "WA_STATE_DIRECTORY")


def prepare(environment, credential):
    if environment.get("INFISICAL_TOKEN"):
        raise ValueError("static_token_refused")
    project = environment.get("WA_INFISICAL_PROJECT_ID")
    if (
        not isinstance(project, str)
        or not project.strip()
        or not isinstance(credential, dict)
        or set(credential) != {"client_id", "client_secret"}
        or not all(isinstance(v, str) and v for v in credential.values())
    ):
        raise ValueError("isolated_identity_missing")
    # Never inherit the host's shared identity, login profile or arbitrary env.
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "INFISICAL_FLEET_OPS_PROJECT_ID": project,
        "INFISICAL_AUTOMATION_CLIENT_ID": credential["client_id"],
        "INFISICAL_AUTOMATION_CLIENT_SECRET": credential["client_secret"],
    }


def arguments(mode, release):
    if mode not in ("poll", "bind"):
        raise ValueError("invalid_start_mode")
    module = "whatsapp_bridge.bridge" if mode == "poll" else "whatsapp_bridge.binding"
    names = POLL_NAMES if mode == "poll" else BIND_NAMES
    # -I ignores ambient PYTHONPATH; this fixed release path is added explicitly.
    boot = "import runpy,sys; sys.path.insert(0,sys.argv[1]); runpy.run_module(sys.argv[2],run_name='__main__')"
    return [
        "--path",
        "/jet",
        "--only",
        ",".join(names),
        "--",
        sys.executable,
        "-I",
        "-c",
        boot,
        str(release / "scripts"),
        module,
    ]


def main():
    release = Path(__file__).resolve().parents[2]
    directory = Path(os.environ["CREDENTIALS_DIRECTORY"])
    credential = json.loads((directory / "infisical").read_text())
    environment = prepare(os.environ, credential)
    sys.path.insert(0, str(release / "lib"))
    from fleet_secrets.cli import cmd_run

    return cmd_run(arguments(sys.argv[1], release), environment, sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyError as error:
        required = (
            error.args[0]
            if error.args[0] in ("CREDENTIALS_DIRECTORY", "WA_INFISICAL_PROJECT_ID")
            else "isolated_runtime"
        )
        print(json.dumps({"state": "isolated_start_failed", "required": required}), file=sys.stderr)
        sys.exit(1)
    except ValueError as error:
        code = (
            str(error)
            if str(error)
            in ("static_token_refused", "isolated_identity_missing", "invalid_start_mode")
            else "invalid_isolated_credential"
        )
        print(json.dumps({"state": "isolated_start_failed", "reason": code}), file=sys.stderr)
        sys.exit(1)
    except Exception:  # noqa: BLE001 - loader failures must not expose credential-bearing details
        print('{"state":"isolated_start_failed"}', file=sys.stderr)
        sys.exit(1)
