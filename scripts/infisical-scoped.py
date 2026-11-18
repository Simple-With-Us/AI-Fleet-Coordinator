#!/usr/bin/env python3
"""Scoped Infisical reads for Muster, through the shared client.

  infisical-scoped.py get NAME --folder F [--project fleet-ops|ai-fleet-coordinator]
  infisical-scoped.py names --folder F [--project fleet-ops|ai-fleet-coordinator]
  infisical-scoped.py doctor [--project fleet-ops|ai-fleet-coordinator]

The reader replaces the retired `fleet-secrets` command for Muster.  Every read goes
through the shared client (`scripts/fleet_infisical.py`), never through HTTP code of
its own and never through a second reader, and every read uses the consumer's own
scoped service token:  the account is picked by (project, folder) from the one table
at the top of this file.

Exit codes:
  0  the read worked:  a value, a list of names, or a doctor report
  2  the command line is wrong
  3  the name is not in that folder (`get`), or the folder holds no name at all (`names`)
  4  the read was refused or failed

Only the value (`get`), the names (`names`) and the report (`doctor`) ever reach
standard output.  A value is never printed in an error, never logged and never put in
argv.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fleet_infisical as FI

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ABSENT = 3
EXIT_REFUSED = 4

# The projects the reader names.  The first is the default,  as it was for the
# retired command.
PROJECTS = ("fleet-ops", "ai-fleet-coordinator")
DEFAULT_PROJECT = "fleet-ops"

# The folder the doctor reports on:  the project's own root.
ROOT_FOLDER = "/"

# One table:  (project, folder) -> the consumer's own service-token account.
#
# A service token covers the one folder it was minted for,  so a token scoped to "/"
# does NOT reach "/code-mirrors" or "/wake-router":  the account is chosen by the
# project AND the folder,  and a folder with no account here is refused rather than
# read with somebody else's token.
ACCOUNTS: dict[tuple[str, str], str] = {
    ("fleet-ops", "/"): "muster-fleetops-ro",
    ("ai-fleet-coordinator", "/"): "muster-afc-ro",
    ("ai-fleet-coordinator", "/code-mirrors"): "muster-afc-code-mirrors-ro",
    ("ai-fleet-coordinator", "/wake-router"): "muster-afc-wake-router-ro",
}


class ReaderError(Exception):
    """A failure with a short reason that never holds a value.  `code` is the exit code."""

    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


def _fail(message: str, code: int) -> int:
    """Say what went wrong on standard error,  and nothing else."""
    print(f"infisical-scoped:  {message}", file=sys.stderr)
    return code


# --------------------------------------------------------------------------- helpers


def _environment() -> tuple[dict[str, str], Path]:
    """The environment the shared client reads,  and the home it looks under."""
    env = dict(os.environ)
    return env, FI.home_dir(env)


def _scoped_env(env: dict[str, str], project_id: str, folder: str) -> dict[str, str]:
    """The environment one read runs in.

    `INFISICAL_READ_PATHS` declares exactly the folder the token's grant covers:  the
    shared client refuses a read whose declared folders are not the grant's folders.

    The project id goes in the client's default-project slot,  because `fetch_verified`
    resolves the project from the environment and checks the answer's own provenance
    against it.  The grant check, the query and the provenance check then all use one
    workspace,  and a token from another project fails closed on the grant.
    """
    scoped = dict(env)
    scoped[FI.ENV_READ_PATHS] = folder
    scoped[FI.ENV_PROJECT] = project_id
    return scoped


def _account_for(project: str, folder: str, named: str | None = None) -> str:
    """The account whose token covers that project and folder.

    A named account wins:  a consumer outside this table holds its own token for a
    folder the table does not name, and it says which one.  Without a name the table
    answers, and a folder it does not carry is refused rather than read with somebody
    else's token.
    """
    if named:
        return named
    account = ACCOUNTS.get((project, folder))
    if not account:
        raise ReaderError("no scoped token covers that project and folder", EXIT_REFUSED)
    return account


def _token_for(account: str, env: dict[str, str], home: Path) -> str:
    """The consumer's own token from the host's approved store."""
    token, status = FI.token_from_store(account, env, home)
    if not token:
        raise ReaderError(f"the reader token is not available ({status})", EXIT_REFUSED)
    return token


def _project_id(env: dict[str, str], home: Path, project: str) -> str:
    """The project id, from the environment or the host's own file."""
    try:
        return FI.infisical_project_id(env, home, project)
    except FI.SecretsError as exc:
        raise ReaderError(exc.message, EXIT_REFUSED) from None


def _reason_token(message: str) -> str:
    """A short reason word for the doctor report:  no value, no token, no sentence."""
    text = (message or "").strip().lower()
    for needle, token in (
        ("token-scope-mismatch", "token-scope-mismatch"),
        ("token-expired-or-invalid", "token-expired-or-invalid"),
        ("token-verification-failed", "token-verification-failed"),
        ("unresolved-reference", "unresolved-reference"),
        ("unverified-source", "unverified-source"),
        ("project id", "project-id-unavailable"),
        ("refusing infisical environment", "environment-refused"),
        ("fetch-failed", "fetch-failed"),
    ):
        if needle in text:
            return token
    return "read-failed"


# --------------------------------------------------------------------------- verbs


def _get(args: argparse.Namespace) -> int:
    """One secret's value, on standard output, and nothing else."""
    name = args.name
    folder = args.folder
    project = args.project
    try:
        FI.check_name(name)
        FI.check_path(folder)
    except FI.UsageError as exc:
        raise ReaderError(exc.message, EXIT_USAGE) from None
    account = _account_for(project, folder, getattr(args, "account", None))
    env, home = _environment()
    token = _token_for(account, env, home)
    project_id = _project_id(env, home, project)
    try:
        value = FI.fetch_verified(name, folder, _scoped_env(env, project_id, folder), token=token)
    except FI.UsageError as exc:
        raise ReaderError(exc.message, EXIT_USAGE) from None
    except FI.SecretsError as exc:
        raise ReaderError(exc.message, EXIT_ABSENT if exc.code == 3 else EXIT_REFUSED) from None
    sys.stdout.write(value + "\n")
    return EXIT_OK


def _names(args: argparse.Namespace) -> int:
    """The names in one folder, one per line, and never a value."""
    folder = args.folder
    project = args.project
    try:
        FI.check_path(folder)
    except FI.UsageError as exc:
        raise ReaderError(exc.message, EXIT_USAGE) from None
    account = _account_for(project, folder, getattr(args, "account", None))
    env, home = _environment()
    token = _token_for(account, env, home)
    project_id = _project_id(env, home, project)
    try:
        found = FI.list_project_names(
            project_id, folder, _scoped_env(env, project_id, folder), token=token
        )
    except FI.UsageError as exc:
        raise ReaderError(exc.message, EXIT_USAGE) from None
    except FI.SecretsError as exc:
        raise ReaderError(exc.message, EXIT_REFUSED) from None
    if not found:
        raise ReaderError("that folder holds no name", EXIT_ABSENT)
    for name in found:
        sys.stdout.write(name + "\n")
    return EXIT_OK


def _doctor(args: argparse.Namespace) -> int:
    """One JSON report about this Mac's access.  No value and no token in it."""
    project = args.project
    folder = ROOT_FOLDER
    report = {
        "ok": False,
        "environment_ok": False,
        "project_id_resolvable": False,
        "reader_configured": False,
        "fetch_names_ok": False,
        "names_count": 0,
        "failure_reason": "",
    }
    env, home = _environment()
    refusal = FI.environment_refusal(env)
    reason = "environment-refused" if refusal else ""
    report["environment_ok"] = refusal is None
    project_id = ""
    token = ""
    account = _account_for(project, folder, getattr(args, "account", None))
    if account is None:
        reason = reason or "no-scoped-token"
    else:
        try:
            project_id = FI.infisical_project_id(env, home, project)
            report["project_id_resolvable"] = True
        except FI.SecretsError as exc:
            reason = reason or _reason_token(exc.message)
        token, status = FI.token_from_store(account, env, home)
        if not token:
            reason = reason or status
        else:
            report["reader_configured"] = True
    if token and project_id:
        try:
            found = FI.list_project_names(
                project_id, folder, _scoped_env(env, project_id, folder), token=token
            )
        except FI.SecretsError as exc:
            reason = reason or _reason_token(exc.message)
        else:
            report["fetch_names_ok"] = True
            report["names_count"] = len(found)
    report["ok"] = bool(
        report["environment_ok"]
        and report["project_id_resolvable"]
        and report["reader_configured"]
        and report["fetch_names_ok"]
    )
    report["failure_reason"] = reason
    sys.stdout.write(json.dumps(report) + "\n")
    return EXIT_OK


# --------------------------------------------------------------------------- entry point


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="infisical-scoped.py",
        description="Scoped Infisical reads through the shared client.",
    )
    verbs = parser.add_subparsers(dest="verb", required=True)

    get = verbs.add_parser("get", help="one secret's value, on standard output")
    get.add_argument("name", help="the secret's name")
    get.add_argument("--folder", required=True, help="the Infisical folder to read")
    get.add_argument("--project", choices=PROJECTS, default=DEFAULT_PROJECT)
    get.add_argument(
        "--account", help="the consumer's own token account, when it is not in the table"
    )

    names = verbs.add_parser("names", help="the names in one folder, one per line")
    names.add_argument("--folder", required=True, help="the Infisical folder to list")
    names.add_argument("--project", choices=PROJECTS, default=DEFAULT_PROJECT)
    names.add_argument(
        "--account", help="the consumer's own token account, when it is not in the table"
    )

    doctor = verbs.add_parser("doctor", help="one JSON report about this Mac's access")
    doctor.add_argument("--project", choices=PROJECTS, default=DEFAULT_PROJECT)
    doctor.add_argument(
        "--account", help="the consumer's own token account, when it is not in the table"
    )
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv[1:])
    try:
        if args.verb == "get":
            return _get(args)
        if args.verb == "names":
            return _names(args)
        return _doctor(args)
    except ReaderError as exc:
        return _fail(exc.message, exc.code)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
