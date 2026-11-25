#!/usr/bin/env python3
"""Scoped Infisical reads for Muster, through the shared client.

  infisical-scoped.py get NAME --folder F [--project fleet-ops|ai-fleet-coordinator]
  infisical-scoped.py names --folder F [--project fleet-ops|ai-fleet-coordinator]
  infisical-scoped.py doctor [--project fleet-ops|ai-fleet-coordinator]

The optional --consumer-binding ABS --consumer EXPECTED selection uses the strict
generic declaration contract in docs/protocols/declared-consumer-reader.md.

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
import re
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
    if named is not None:
        return named
    account = ACCOUNTS.get((project, folder))
    if not account:
        raise ReaderError("no scoped token covers that project and folder", EXIT_REFUSED)
    return account


def _store_token(account: str, env: dict[str, str], home: Path) -> tuple[str, str]:
    """Read one existing store item and keep timeout/refusal reasons safe."""
    try:
        return FI.token_from_store(account, env, home)
    except FI.SecretsError as exc:
        if exc.code == 124:
            raise ReaderError("the scoped token store timed out", 124) from None
        raise ReaderError("the scoped token store refused the read", EXIT_REFUSED) from None


def _token_for(account: str, env: dict[str, str], home: Path) -> str:
    """The consumer's own token from the host's approved store."""
    token, status = _store_token(account, env, home)
    if not token:
        raise ReaderError(f"the reader token is not available ({status})", EXIT_REFUSED)
    return token


def _project_id(env: dict[str, str], home: Path, project: str) -> str:
    """The project id, from the environment or the host's own file."""
    try:
        return FI.infisical_project_id(env, home, project)
    except FI.SecretsError as exc:
        raise ReaderError(_reason_token(exc.message), EXIT_REFUSED) from None


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


def _unavailable(exc) -> bool:
    return (isinstance(exc, TimeoutError) or getattr(exc, "code", None) == 124
            or getattr(exc, "failure_kind", None) == "unavailable")


def _declared_context(args, folder: str):
    """Select one protected declaration and its exact existing Keychain account."""
    env, home = _environment()
    conflicts = FI.AUTH_ENV_VARS | {
        FI.ENV_API,
        "INFISICAL_API_URL",
        FI.ENV_READ_PATHS,
        FI.ENV_WRITE_PATHS,
    }
    if getattr(args, "account", None) is not None or any(key in env for key in conflicts):
        raise ReaderError("consumer-binding-conflict")
    if FI.environment_refusal(env):
        raise ReaderError("consumer-binding-environment-refused")
    try:
        declaration = FI._decode_json(
            FI._protected_read(args.consumer_binding, 16 * 1024, private=True, strict_custody=True)
        )
        keys = {
            "schemaVersion",
            "consumer",
            "project",
            "projectId",
            "environment",
            "secretPath",
            "permissions",
            "expiresAt",
            "tokenName",
            "tokenId",
            "storage",
        }
        if not isinstance(declaration, dict) or set(declaration) != keys:
            raise ValueError
        if (
            type(declaration["schemaVersion"]) is not int
            or declaration["schemaVersion"] != 1
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.consumer)
            or args.consumer.startswith("st.")
            or declaration["consumer"] != args.consumer
            or declaration["project"] != args.project
            or not isinstance(declaration["projectId"], str)
            or not FI._UUID_RE.fullmatch(declaration["projectId"])
            or declaration["environment"] != "prod"
            or declaration["secretPath"] != folder
            or declaration["permissions"] != ["read"]
        ):
            raise ValueError
        expected = FI._expected_token_identity(
            {
                "id": declaration["tokenId"],
                "name": declaration["tokenName"],
                "expiresAt": declaration["expiresAt"],
            }
        )
        required_account = getattr(args, "consumer_account", None)
        if required_account is not None and declaration["tokenName"] != required_account:
            raise ValueError
        if declaration["storage"] != {
            "kind": "mac-login-keychain",
            "service": FI.SCOPED_KEYCHAIN_SERVICE,
            "account": declaration["tokenName"],
        }:
            raise ValueError
        # Resolve the installed project selection first; a declaration cannot override it.
        project_id = FI.infisical_project_id(env, home, args.project)
        if project_id != declaration["projectId"]:
            raise ValueError
    except (
        ValueError,
        TypeError,
        KeyError,
        RecursionError,
        UnicodeError,
        FI.SecretsError,
        FI.UsageError,
    ):
        raise ReaderError("consumer-binding-refused") from None
    try:
        token, _status = FI.token_from_store(
            declaration["tokenName"], env, home, bounded_keychain=True
        )
    except (FI.SecretsError, TimeoutError) as exc:
        raise ReaderError("consumer-binding-store-unavailable" if _unavailable(exc) else "consumer-binding-store-refused",
                          124 if _unavailable(exc) else 4) from None
    if not token:
        raise ReaderError("consumer-binding-store-unavailable")
    # Only nonsecret project and exact scope metadata reaches the canonical reader.
    private = {"INFISICAL_ENVIRONMENT": "prod", FI.ENV_READ_PATHS: folder}
    return project_id, token, private, expected


def _declared_read(args, folder: str) -> int:
    project_id, token, env, expected = _declared_context(args, folder)
    try:
        if args.verb == "get":
            values = FI.bounded_fetch_project(
                project_id,
                folder,
                env,
                token=token,
                names=(args.name,),
                timeout=30.0,
                max_output_bytes=8192,
                strict_records=True,
                require_finite_expiry=True,
                expected_identity=expected,
            )
            if args.name not in values:
                raise ReaderError("that name is absent", EXIT_ABSENT)
            sys.stdout.write(values[args.name] + "\n")
        else:
            found = FI.list_project_names(
                project_id,
                folder,
                env,
                token=token,
                timeout=30.0,
                require_finite_expiry=True,
                expected_identity=expected,
                strict_records=True,
            )
            if not found:
                raise ReaderError("that folder holds no name", EXIT_ABSENT)
            sys.stdout.write("".join(name + "\n" for name in found))
    except (FI.SecretsError, TimeoutError) as exc:
        raise ReaderError(
            "consumer-binding-read-unavailable" if _unavailable(exc) else "consumer-binding-read-refused",
            124 if _unavailable(exc) else EXIT_ABSENT if exc.code == 3 else 4
        ) from None
    except FI.UsageError:
        raise ReaderError("consumer-binding-read-refused") from None
    return EXIT_OK


def _declared_doctor(args) -> int:
    """Mask diagnostics while applying the same identity and metadata-only policy."""
    report = {
        "ok": False,
        "environment_ok": False,
        "project_id_resolvable": False,
        "reader_configured": False,
        "fetch_names_ok": False,
        "names_count": 0,
        "failure_reason": "",
    }
    try:
        FI.check_path(args.folder)
        project_id, token, env, expected = _declared_context(args, args.folder)
        report.update(environment_ok=True, project_id_resolvable=True, reader_configured=True)
        found = FI.list_project_names(
            project_id,
            args.folder,
            env,
            token=token,
            timeout=30.0,
            require_finite_expiry=True,
            expected_identity=expected,
            strict_records=True,
        )
        report.update(ok=True, fetch_names_ok=True, names_count=len(found))
    except (ReaderError, FI.SecretsError, FI.UsageError, TimeoutError) as exc:
        if _unavailable(exc):
            raise ReaderError("consumer-binding-read-unavailable", 124) from None
        report["failure_reason"] = "consumer-binding-refused"
    sys.stdout.write(json.dumps(report) + "\n")
    return EXIT_OK


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
    if args.consumer_binding is not None:
        return _declared_read(args, folder)
    account = _account_for(project, folder, getattr(args, "account", None))
    env, home = _environment()
    token = _token_for(account, env, home)
    project_id = _project_id(env, home, project)
    try:
        value = FI.fetch_verified(name, folder, _scoped_env(env, project_id, folder), token=token)
    except FI.UsageError as exc:
        raise ReaderError(exc.message, EXIT_USAGE) from None
    except FI.SecretsError as exc:
        # The reason is the failure's own code, never its message:  a message from the
        # provider can carry a response body,  and that never reaches a log.
        raise ReaderError(
            f"the scoped read was refused (code {exc.code})",
            EXIT_ABSENT if exc.code == 3 else EXIT_REFUSED,
        ) from None
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
    if args.consumer_binding is not None:
        return _declared_read(args, folder)
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
        # The reason is the failure's own code, never its message.
        raise ReaderError(f"the scoped read was refused (code {exc.code})", EXIT_REFUSED) from None
    if not found:
        raise ReaderError("that folder holds no name", EXIT_ABSENT)
    for name in found:
        sys.stdout.write(name + "\n")
    return EXIT_OK


def _doctor(args: argparse.Namespace) -> int:
    """One JSON report about this Mac's access.  No value and no token in it."""
    if args.consumer_binding is not None:
        return _declared_doctor(args)
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
    try:
        account = _account_for(project, folder, getattr(args, "account", None))
    except ReaderError:
        # The doctor reports the uncovered token instead of raising out of it:  the
        # report is what names the missing grant.
        account = None
    if account is None:
        reason = reason or "no-scoped-token"
    else:
        try:
            project_id = FI.infisical_project_id(env, home, project)
            report["project_id_resolvable"] = True
        except FI.SecretsError as exc:
            reason = reason or _reason_token(exc.message)
        try:
            token, status = _store_token(account, env, home)
        except ReaderError as exc:
            # Keep the doctor's JSON contract on store refusal, including timeout.
            token, status = "", "keychain-timeout" if exc.code == 124 else "store-refused"
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
    doctor.add_argument("--folder", default=ROOT_FOLDER, help="declared doctor's exact folder")
    doctor.add_argument(
        "--account", help="the consumer's own token account, when it is not in the table"
    )
    for verb in (get, names, doctor):
        verb.add_argument("--consumer-binding", help="absolute protected nonsecret declaration")
        verb.add_argument("--consumer", help="the expected consumer identifier")
        verb.add_argument("--consumer-account", help="the exact expected declared account")
    return parser


def main(argv: list[str]) -> int:
    args = _parser().parse_args(argv[1:])
    try:
        if (args.consumer_binding is None) != (args.consumer is None):
            raise ReaderError("consumer-binding and consumer must be supplied together", EXIT_USAGE)
        if args.consumer_account is not None and args.consumer_binding is None:
            raise ReaderError("consumer-account requires a consumer binding", EXIT_USAGE)
        if args.verb == "doctor" and args.consumer_binding is None and args.folder != ROOT_FOLDER:
            raise ReaderError("doctor folder selection requires a consumer binding", EXIT_USAGE)
        if args.verb == "get":
            return _get(args)
        if args.verb == "names":
            return _names(args)
        return _doctor(args)
    except ReaderError as exc:
        return _fail(exc.message, exc.code)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
