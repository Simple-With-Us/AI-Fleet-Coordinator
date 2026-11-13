#!/usr/bin/env python3
"""Put one seat's CLI login where the hosts that seat runs on can read it.

A CLI login lives in the store the fleet already uses:  Infisical, Fleet-OPS, prod.
A seat that also runs somewhere else  (a bot on the shared VPS,  a helper on the local
VM)  needs the same login there,  and copying a credential file between hosts is the
thing the fleet does not do.  So the login is written under ``/cli-logins/<seat>``,
which is where the consumers read it from:

    python3 scripts/fleet_infisical_cli.py run --path /cli-logins/OPENCODE --only GH_TOKEN -- gh api user

The value never appears in an argument list,  a message or a log.  This script reads it
from an approved source and delegates the write plus exact named verification to the
shared client.  Output contains the name, path and safe recovery UUID/time/purpose;
no value, value digest, token or provider payload is printed.

Where the value comes from,  in the order the fleet reads credentials:

  --from env          the environment variable NAME.  This is what ``fleet-secrets run
                      --only`` injects,  and it is what the Mac's own shell already
                      exports for the GitHub CLI.
  --from fleet-secrets  compatibility spelling for one direct shared-client named read
                      through the explicitly bound reader, not an app subprocess.
  --from file PATH    a 0600 file the owner handed over,  which is how the owner
                      hands a secret to a seat at all.

The GitHub CLI's own keychain entry and a generic keychain lookup are deliberately not
sources here.  This helper reads only the explicitly selected environment, fleet-secrets
folder, or owner-provided file.

An automated retry supplies the same --request-id and --request-created-at values.
An explicit --reconcile with that identity reads no source and never retries a write.
The shared CLI caller declares the same protected state root for both entry points;
an uncertain original operation blocks a new invocation and reports its original
recovery identity.  This is not a cross-host or external-writer exactly-once promise.

    python3 scripts/fleet_secrets/sync_cli_login.py --name GH_TOKEN --path /cli-logins/OPENCODE
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fleet_infisical as core
import fleet_infisical_cli as caller

# The names a CLI reads out of its environment.  The write allowlist is the gate,  and
# this is the same list,  so the two cannot drift.
CLI_LOGIN_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "COPILOT_GITHUB_TOKEN")

EXIT_USAGE = 2
EXIT_FAILED = 4


def _read_from_env(name: str, env: dict) -> str:
    """The value the environment already carries."""
    return (env.get(name) or "").strip()


def _read_from_fleet_secrets(name: str, path: str, env: dict) -> str:
    """Compatibility source spelling; one named direct read through its own scoped reader."""
    project = core.infisical_project_id(env, core.home_dir(env))
    reader = core.resolve_token(env)
    return (
        core.fetch_project(
            project, path, env, token=reader, names=(name,), timeout=core.EXPORT_TIMEOUT
        )
        .get(name, "")
        .strip()
    )


def _read_from_file(path: str) -> str:
    """The value in a file the owner handed over."""
    target = Path(path).expanduser()
    try:
        info = target.stat()
    except OSError as exc:
        raise core.SecretsError(f"the file cannot be read: {exc}", EXIT_FAILED)
    if not os.path.isfile(target) or info.st_mode & 0o077:
        raise core.SecretsError("the file must be a regular file with mode 0600", EXIT_FAILED)
    try:
        return target.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise core.SecretsError(f"the file cannot be read: {exc}", EXIT_FAILED)


def read_value(name: str, source: str, from_path: str, file_path: str, env: dict) -> str:
    """The value,  from the approved source.  The caller has already checked the policy."""
    if source == "env":
        value = _read_from_env(name, env)
    elif source == "fleet-secrets":
        value = _read_from_fleet_secrets(name, from_path, env)
    elif source == "file":
        value = _read_from_file(file_path)
    else:
        raise core.SecretsError("the source must be env, fleet-secrets or file", EXIT_USAGE)
    if not value:
        raise core.SecretsError(f"the {source} source gave no value", EXIT_FAILED)
    return value


def sync(name: str, path: str, value: str, env: dict, *, intent=None) -> core.WriteReceipt:
    """The shared writer proves its causal ACK and exact named readback using the scoped writer."""
    core.check_name(name)
    core.check_path(path)
    core.check_writable(name, path)
    if intent is None:
        intent = caller.write_intent_for_cli(env, "cli-login-sync")
    return core.put_secret(name, path, value, env, intent=intent)


def main(argv: list[str] | None = None, env: dict | None = None, out=None, err=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True, help="the environment variable the CLI reads")
    parser.add_argument(
        "--path", required=True, help="where the login goes, such as /cli-logins/OPENCODE"
    )
    parser.add_argument(
        "--from",
        dest="source",
        default="env",
        choices=("env", "fleet-secrets", "file"),
        help="where the value comes from",
    )
    parser.add_argument(
        "--from-path", default="/", help="the folder the fleet-secrets read looks in"
    )
    parser.add_argument("--file", default="", help="the file the owner handed over")
    parser.add_argument(
        "--state-dir", default=None, help="explicit shared protected writer state directory"
    )
    parser.add_argument(
        "--request-id", default=None, help="reuse this original UUID for an automated retry"
    )
    parser.add_argument(
        "--request-created-at", type=int, default=None, help="original epoch milliseconds"
    )
    parser.add_argument(
        "--reconcile",
        action="store_true",
        help="read-only recovery of an original intent; no source is read",
    )
    args = parser.parse_args(argv)
    env = dict(os.environ) if env is None else dict(env)
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    value = ""
    try:
        if args.name not in CLI_LOGIN_NAMES:
            raise core.SecretsError(
                "the name must be one of {}".format(", ".join(CLI_LOGIN_NAMES)), EXIT_USAGE
            )
        # The policy is checked before the value is read,  so a name or a path outside
        # the allowlist never reaches a child process and never touches the network.
        core.check_name(args.name)
        core.check_path(args.path)
        core.check_writable(args.name, args.path)
        if args.reconcile and (args.request_id is None or args.request_created_at is None):
            raise core.UsageError(
                "reconcile requires the original request-id and request-created-at"
            )
        intent = caller.write_intent_for_cli(
            env,
            "cli-login-sync",
            request_id=args.request_id,
            created_at_ms=args.request_created_at,
            state_dir=args.state_dir,
        )
        if args.reconcile:
            result = core.reconcile_secret(args.name, args.path, env, intent=intent)
        else:
            value = read_value(args.name, args.source, args.from_path, args.file, env)
            result = sync(args.name, args.path, value, env, intent=intent)
        if (
            not isinstance(result, core.WriteReceipt)
            or result.outcome != "ok"
            or result.request_id != intent.request_id
            or result.created_at_ms != intent.created_at_ms
        ):
            raise core.SecretsError("write-receipt-invalid")
    except core.WriteError as exc:
        err.write(
            f"sync-cli-login: {exc.message}; outcome={exc.outcome} requestID={exc.request_id} "
            f"requestCreatedAt={exc.created_at_ms} purpose={exc.purpose} name={exc.name or args.name}\n"
        )
        return exc.code
    except core.SecretsError as exc:
        err.write(f"sync-cli-login: {exc.message}\n")
        return exc.code
    finally:
        value = ""
    out.write(
        f"requestID={result.request_id} requestCreatedAt={result.created_at_ms} purpose={intent.purpose}\n"
    )
    out.write(f"synced {args.name} at {args.path}\n")
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
