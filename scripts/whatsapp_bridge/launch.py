"""Isolated service-token startup adapter; inert until approved installation.

The reviewed fleet_secrets package is copied to release/lib at deploy.  Each
mode reads its own dedicated, server-scoped, read-only AFC service token from
systemd LoadCredential.  Ambient/shared credentials are never a fallback.
"""

import json
import os
import sys
import uuid
from pathlib import Path

POLL_NAMES = (
    "WHATSAPP_API_KEY",
    "WA_BRIDGE_HMAC_KEY_JET",
    "WA_WORKER_URL",
    "WA_STATE_DIRECTORY",
    "WA_AGENT_HASH_JET",
    "WA_SENDER_HASH_JET",
    "WHATSAPP_BRIDGE_ENABLED",
    # AFC-545: the SEND switch, off by default.  A missing or stale value
    # counts as off, so the bridge fails closed.
    "WHATSAPP_BRIDGE_SEND_ENABLED",
)
BIND_NAMES = ("WHATSAPP_API_KEY", "WA_BRIDGE_HMAC_KEY_JET", "WA_STATE_DIRECTORY")
OPERATOR_NAMES = ("WA_OPERATOR_HMAC_KEY_JET", "WA_WORKER_URL")
OPERATOR_FOLDER = "/operator-jet-wake"
CREDENTIAL_NAMES = {
    "poll": "infisical-jet",
    "bind": "infisical-jet",
    "operator": "infisical-operator",
}
AMBIENT_AUTH = (
    "INFISICAL_TOKEN",
    "INFISICAL_AUTOMATION_CLIENT_ID",
    "INFISICAL_AUTOMATION_CLIENT_SECRET",
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET",
)


def _service_token(value):
    if not isinstance(value, str):
        raise TypeError("isolated_service_token_missing")
    value = value.strip()
    # Service tokens are opaque; only reject empty/malformed input or a different
    # credential kind.  Actual project, folder, read-only and expiry restrictions
    # belong to Infisical's server-side token scope, verified before activation.
    if (
        not value.startswith("st.")
        or value.count(".") < 2
        or any(c.isspace() or c == "\0" for c in value)
        or any(not part for part in value.split("."))
    ):
        raise ValueError("isolated_service_token_missing")
    return value


def prepare(environment, service_token):
    if any(environment.get(name) for name in AMBIENT_AUTH):
        raise ValueError("ambient_credentials_refused")
    project = environment.get("WA_INFISICAL_PROJECT_ID")
    if not isinstance(project, str) or not project.strip():
        raise ValueError("WA_INFISICAL_PROJECT_ID_missing")
    # Never inherit the host's identity, profile, API override or arbitrary env.
    # The legacy selector is process-local and explicitly names the AFC project.
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "INFISICAL_FLEET_OPS_PROJECT_ID": project.strip(),
        "INFISICAL_TOKEN": _service_token(service_token),
    }


def arguments(mode, release, request_id=None):
    if mode not in CREDENTIAL_NAMES:
        raise ValueError("invalid_start_mode")
    if mode == "operator":
        try:
            canonical_id = str(uuid.UUID(request_id)) if isinstance(request_id, str) else None
        except ValueError:
            raise ValueError("invalid_operator_request") from None
        if canonical_id is None or canonical_id != request_id:
            raise ValueError("invalid_operator_request")
        module, names, folder = "whatsapp_bridge.operator_wake", OPERATOR_NAMES, OPERATOR_FOLDER
    else:
        if request_id is not None:
            raise ValueError("invalid_start_mode")
        module = "whatsapp_bridge.bridge" if mode == "poll" else "whatsapp_bridge.binding"
        names, folder = (POLL_NAMES if mode == "poll" else BIND_NAMES), "/jet"
    boot = "import runpy,sys; root,module=sys.argv[1:3]; sys.path.insert(0,root); sys.argv=[module,*sys.argv[3:]]; runpy.run_module(module,run_name='__main__')"
    return [
        "--path",
        folder,
        "--only",
        ",".join(names),
        "--",
        sys.executable,
        "-I",
        "-c",
        boot,
        str(release / "scripts"),
        module,
        *([request_id] if request_id else []),
    ]


def run_poll_with_optional_send(command, prepared):
    # Same isolated /jet service scope and fixed allowlist as cmd_run. SEND is
    # the only optional entry, and absent means off; required secrets stay strict.
    from fleet_secrets import core
    from fleet_secrets.cli import SecretsError

    secrets = core.fetch_all(core.check_path("/jet"), prepared)
    chosen = {}
    for name in POLL_NAMES:
        if name == "WHATSAPP_BRIDGE_SEND_ENABLED":
            chosen[name] = secrets.get(name, "off")
        elif name not in secrets:
            raise SecretsError(f"key {name} is not in Infisical at /jet", 3)
        else:
            chosen[name] = secrets[name]
    core.reject_unresolved(chosen)
    child = {k: v for k, v in prepared.items() if k not in core.AUTH_ENV_VARS}
    child.update(chosen)
    executable = command[command.index("--") + 1 :]
    try:
        os.execvpe(executable[0], executable, child)
    except FileNotFoundError:
        raise SecretsError("command not found", 127) from None
    except OSError:
        raise SecretsError("could not start poll bridge", 126) from None
    return 0


class CredentialSourceError(ValueError):
    pass


def load_service_token(directory, name):
    if name not in CREDENTIAL_NAMES.values():
        raise CredentialSourceError("invalid_systemd_credential_name")
    try:
        value = (directory / name).read_text()
    except UnicodeDecodeError:
        raise CredentialSourceError("invalid_systemd_service_token") from None
    except OSError:
        raise CredentialSourceError("unavailable_systemd_service_token") from None
    try:
        return _service_token(value)
    except ValueError:
        raise CredentialSourceError("invalid_systemd_service_token") from None


def main(argv=None, environment=None):
    argv = sys.argv[1:] if argv is None else argv
    environment = os.environ if environment is None else environment
    mode = argv[0] if argv else ""
    if len(argv) != (2 if mode == "operator" else 1):
        raise ValueError("invalid_start_mode")
    release = Path(__file__).resolve().parents[2]
    command = arguments(mode, release, argv[1] if mode == "operator" else None)
    directory = Path(environment["CREDENTIALS_DIRECTORY"])
    service_token = load_service_token(directory, CREDENTIAL_NAMES[mode])
    prepared = prepare(environment, service_token)
    service_token = ""
    sys.path.insert(0, str(release / "lib"))
    from fleet_secrets.cli import cmd_run

    if mode == "poll":
        return run_poll_with_optional_send(command, prepared)
    return cmd_run(command, prepared, sys.stderr)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except CredentialSourceError as error:
        print(
            json.dumps(
                {
                    "state": "isolated_start_failed",
                    "source": "systemd_service_token",
                    "reason": str(error),
                }
            ),
            file=sys.stderr,
        )
        sys.exit(1)
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
            in (
                "ambient_credentials_refused",
                "isolated_service_token_missing",
                "WA_INFISICAL_PROJECT_ID_missing",
                "invalid_start_mode",
                "invalid_operator_request",
            )
            else "invalid_isolated_configuration"
        )
        print(json.dumps({"state": "isolated_start_failed", "reason": code}), file=sys.stderr)
        sys.exit(1)
    except ImportError:
        print(
            '{"state":"isolated_start_failed","source":"fleet_secrets_dependency"}', file=sys.stderr
        )
        sys.exit(1)
    except Exception:  # noqa: BLE001 - never expose credential-bearing loader exceptions
        print(
            '{"state":"isolated_start_failed","source":"fleet_secrets/Infisical"}', file=sys.stderr
        )
        sys.exit(1)
