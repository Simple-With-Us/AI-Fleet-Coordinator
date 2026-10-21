#!/usr/bin/env python3
"""Put one seat's CLI login where the hosts that seat runs on can read it.

A CLI login lives in the store the fleet already uses:  Infisical, Fleet-OPS, prod.
A seat that also runs somewhere else  (a bot on the shared VPS,  a helper on the local
VM)  needs the same login there,  and copying a credential file between hosts is the
thing the fleet does not do.  So the login is written under ``/cli-logins/<seat>``,
which is where the consumers read it from:

    fleet-secrets run --path /cli-logins/OPENCODE --only GH_TOKEN -- gh api user

The value never appears in an argument list,  a message or a log.  This script reads it
from an approved source,  writes it,  reads it back,  compares the digests,  and prints
the name and the path and nothing else.

Where the value comes from,  in the order the fleet reads credentials:

  --from env          the environment variable NAME.  This is what ``fleet-secrets run
                      --only`` injects,  and it is what the Mac's own shell already
                      exports for the GitHub CLI.
  --from fleet-secrets  read it with ``fleet-secrets get NAME --path <from-path>``.
  --from file PATH    a 0600 file the owner handed over,  which is how the owner
                      hands a secret to a seat at all.

The GitHub CLI's own keychain entry and a generic keychain lookup are deliberately not
sources here:  the keychain is reserved for the machine identity,  and a path-less
lookup can reach an item in another account or in the System keychain.

    python3 scripts/fleet_secrets/sync_cli_login.py --name GH_TOKEN --path /cli-logins/OPENCODE
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fleet_secrets import core  # noqa: E402

# The names a CLI reads out of its environment.  The write allowlist is the gate,  and
# this is the same list,  so the two cannot drift.
CLI_LOGIN_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "COPILOT_GITHUB_TOKEN")

EXIT_USAGE = 2
EXIT_FAILED = 4
EXIT_MISSING_BINARY = 127
EXIT_TIMEOUT = 124


def _digest(value: str) -> str:
    """A digest of a value,  for the read-back comparison.  The value is never stored."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _read_from_env(name: str, env: dict) -> str:
    """The value the environment already carries."""
    return (env.get(name) or "").strip()


def _read_from_fleet_secrets(name: str, path: str, env: dict) -> str:
    """The value,  read through the fleet's own read path."""
    argv = [sys.executable, "-m", "fleet_secrets", "get", name, "--path", path]
    try:
        run = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=core.EXPORT_TIMEOUT,
            env=env,
            check=False,
        )
    except FileNotFoundError:
        raise core.SecretsError("cli-missing:  fleet-secrets is not installed", EXIT_MISSING_BINARY)
    except subprocess.TimeoutExpired:
        raise core.SecretsError("the read timed out", EXIT_TIMEOUT)
    except OSError as exc:
        raise core.SecretsError("the read failed: {}".format(exc), EXIT_FAILED)
    if run.returncode:
        raise core.SecretsError(
            "the read failed: {}".format((run.stderr or "").strip()[:200]), EXIT_FAILED
        )
    return (run.stdout or "").strip()


def _read_from_file(path: str) -> str:
    """The value in a file the owner handed over."""
    target = Path(path).expanduser()
    try:
        info = target.stat()
    except OSError as exc:
        raise core.SecretsError("the file cannot be read: {}".format(exc), EXIT_FAILED)
    if not os.path.isfile(target) or info.st_mode & 0o077:
        raise core.SecretsError("the file must be a regular file with mode 0600", EXIT_FAILED)
    try:
        return target.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise core.SecretsError("the file cannot be read: {}".format(exc), EXIT_FAILED)


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
        raise core.SecretsError("the {} source gave no value".format(source), EXIT_FAILED)
    return value


def sync(name: str, path: str, value: str, env: dict) -> str:
    """Write the value,  prove it landed,  and say what happened.

    The policy is checked before the value is read and before any request,  so a name or
    a path outside the allowlist never reaches a child process or the network.  The
    token is resolved once and used for the folder,  the write and the read-back.
    """
    core.check_name(name)
    core.check_path(path)
    core.check_writable(name, path)
    token = core.resolve_token(env)
    core.ensure_folder(path, env, token)
    result = core.put_secret(name, path, value, env, token=token)
    stored = core.fetch_all(path, env, token=token).get(name, "")
    if not stored:
        raise core.SecretsError("the write reported {} but the read back found nothing".format(name), EXIT_FAILED)
    if _digest(stored) != _digest(value):
        raise core.SecretsError("the read back does not match what was written", EXIT_FAILED)
    return result


def main(
    argv: list[str] | None = None, env: dict | None = None, out=None, err=None
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--name", required=True, help="the environment variable the CLI reads")
    parser.add_argument("--path", required=True, help="where the login goes, such as /cli-logins/OPENCODE")
    parser.add_argument(
        "--from",
        dest="source",
        default="env",
        choices=("env", "fleet-secrets", "file"),
        help="where the value comes from",
    )
    parser.add_argument("--from-path", default="/", help="the folder the fleet-secrets read looks in")
    parser.add_argument("--file", default="", help="the file the owner handed over")
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
        value = read_value(args.name, args.source, args.from_path, args.file, env)
        result = sync(args.name, args.path, value, env)
    except core.SecretsError as exc:
        err.write("sync-cli-login: {}\n".format(exc.message))
        return exc.code
    finally:
        value = ""
    out.write("synced {} at {}\n".format(args.name, args.path))
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
