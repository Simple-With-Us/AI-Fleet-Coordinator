"""The ``fleet-secrets`` command.

  run [--path /] [--only NAME,NAME] -- CMD [ARG...]
        exec CMD with the secrets as environment variables.  Existing variables of the same
        name are replaced.  A few names (PATH, HOME, LD_*, DYLD_* ...) are never injected.
  get NAME [--path /]
        print one value, exactly, with no trailing newline.  Exit 3 when NAME is absent.
  names [--path /] [--project fleet-ops|ai-fleet-coordinator]
        key names only, one per line.  --project picks the Infisical project (read-only;  default fleet-ops).
  set NAME [--path /]
        create or update one secret in Fleet-OPS prod.  The value is read from standard input (one
        trailing newline removed), never from an argument.  Needs a machine identity.  Only names on the
        built-in allowlist in core.WRITE_ALLOWLIST (now CACHE_* under /linear) are accepted;  everything
        else exits 2.
  doctor [--json]
        booleans and counts only, never a value.

Exit codes:  0 ok, 2 usage or refused configuration, 3 key absent, 4 fetch or auth failure,
5 a value came back as an unresolved Infisical reference, 126/127 the command in `run` could
not be started.  Error text names keys and reason tokens, never values.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from typing import TextIO

from . import core
from .core import SecretsError, UsageError

USAGE = (
    "usage: fleet-secrets run [--path /] [--only NAME,NAME] -- CMD [ARG...]\n"
    "       fleet-secrets get NAME [--path /]\n"
    "       fleet-secrets names [--path /] [--project fleet-ops|ai-fleet-coordinator]\n"
    "       fleet-secrets set NAME --path /linear   (value on standard input;  CACHE_* names only)\n"
    "       fleet-secrets doctor [--json]\n"
)


def _parse(args: Sequence[str], allow_only: bool, positional: int) -> dict[str, object]:
    """Tiny parser:  --path P, --only A,B, and `positional` bare arguments.  Raises SecretsError(code 2)."""
    opts: dict[str, object] = {"path": "/", "only": None, "args": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--path", "--only"):
            if a == "--only" and not allow_only:
                raise UsageError("unknown option --only for this command")
            if i + 1 >= len(args):
                raise UsageError("%s needs a value" % a)
            opts["path" if a == "--path" else "only"] = args[i + 1]
            i += 2
        elif a.startswith("--path=") or (allow_only and a.startswith("--only=")):
            key, _, val = a[2:].partition("=")
            opts[key] = val
            i += 1
        elif a.startswith("-") and a != "-":
            raise UsageError("unknown option %s" % a)
        else:
            opts["args"].append(a)  # type: ignore[union-attr]
            i += 1
    if len(opts["args"]) != positional:  # type: ignore[arg-type]
        raise UsageError("wrong number of arguments")
    return opts


def _only_names(raw: object | None) -> list[str] | None:
    if raw is None:
        return None
    names = [core.check_name(n.strip()) for n in str(raw).split(",") if n.strip()]
    if not names:
        raise UsageError("--only needs at least one key name")
    return names


def cmd_get(args: Sequence[str], env: Mapping[str, str], out: TextIO) -> int:
    opts = _parse(args, False, 1)
    name = core.check_name(str(opts["args"][0]))  # type: ignore[index]
    secrets = core.fetch_all(core.check_path(str(opts["path"])), env)
    if name not in secrets:
        raise SecretsError("key %s is not in Infisical at %s" % (name, opts["path"]), 3)
    core.reject_unresolved(secrets, [name])
    out.write(secrets[name])
    out.flush()
    return 0


def cmd_set(args: Sequence[str], env: Mapping[str, str], out: TextIO, stdin: TextIO) -> int:
    opts = _parse(args, False, 1)
    name = core.check_name(str(opts["args"][0]))  # type: ignore[index]
    path = core.check_path(str(opts["path"]))
    core.check_writable(name, path)  # refuse before reading the value or touching auth
    value = stdin.read(core.MAX_VALUE_BYTES + 1)
    value = value.removesuffix("\n")
    if not value:
        raise UsageError("set reads the value from standard input and it was empty")
    try:
        result = core.put_secret(name, path, value, env)
    finally:
        value = ""
    out.write("%s %s at %s\n" % (result, name, path))
    out.flush()
    return 0


def cmd_names(args: Sequence[str], env: Mapping[str, str], out: TextIO) -> int:
    rest = list(args)
    project = core.DEFAULT_PROJECT
    for i, a in enumerate(rest):
        if a == "--project" or a.startswith("--project="):
            if a == "--project":
                if i + 1 >= len(rest):
                    raise UsageError("--project needs a value")
                project = rest[i + 1]
                del rest[i : i + 2]
            else:
                project = a.partition("=")[2]
                del rest[i]
            break
    if project not in core.PROJECT_SOURCES:
        raise UsageError("unknown --project:  use %s" % " or ".join(core.PROJECT_SOURCES))
    opts = _parse(rest, False, 0)
    secrets = core.fetch_all(core.check_path(str(opts["path"])), env, project)
    core.reject_unresolved(secrets)
    out.write("".join(n + "\n" for n in sorted(secrets)))
    out.flush()
    return 0


def cmd_run(args: Sequence[str], env: Mapping[str, str], err: TextIO) -> int:
    if "--" not in args:
        raise UsageError("run needs `--` before the command:  fleet-secrets run -- CMD [ARG...]")
    split = list(args).index("--")
    opts = _parse(args[:split], True, 0)
    command = list(args[split + 1 :])
    if not command or not command[0]:
        raise UsageError("run needs a command after `--`")
    only = _only_names(opts["only"])
    if only is not None:
        reserved = [n for n in only if not core.injectable(n)]
        if reserved:
            raise UsageError(
                "--only cannot inject reserved name %s (PATH, HOME, SHELL, interpreter, LD_* and DYLD_* "
                "variables are never injected);  read it with `get` if you really need the value"
                % reserved[0]
            )
    secrets = core.fetch_all(core.check_path(str(opts["path"])), env)
    if only is not None:
        for name in only:
            if name not in secrets:
                raise SecretsError("key %s is not in Infisical at %s" % (name, opts["path"]), 3)
        chosen = {n: secrets[n] for n in only}
    else:
        chosen = {n: v for n, v in secrets.items() if core.injectable(n)}
    core.reject_unresolved(chosen)
    child_env = {k: v for k, v in env.items() if k not in core.AUTH_ENV_VARS}
    child_env.update(chosen)
    chosen = secrets = {}
    try:
        os.execvpe(command[0], command, child_env)
    except FileNotFoundError:
        raise SecretsError("command not found: %s" % command[0], 127) from None
    except PermissionError:
        raise SecretsError("command not executable: %s" % command[0], 126) from None
    except OSError:
        raise SecretsError("could not start: %s" % command[0], 126) from None
    return 0  # unreachable after a real exec


def doctor_report(env: Mapping[str, str]) -> dict[str, object]:
    """Booleans, a version and counts.  Never a value, a client id or a token."""
    rep: dict[str, object] = {}
    rep["environment_ok"] = core.environment_refusal(env) is None
    cli = core.find_cli(env)
    rep["cli_present"] = cli is not None
    rep["cli_version"] = core.cli_version(cli, env) if cli else None
    try:
        core.infisical_project_id(env, core.home_dir(env))
        rep["project_id_resolvable"] = True
    except SecretsError:
        rep["project_id_resolvable"] = False
    (
        rep["keychain_client_id_present"],
        rep["keychain_client_secret_present"],
    ) = core.keychain_presence(core.login_keychain())
    (
        rep["system_keychain_client_id_present"],
        rep["system_keychain_client_secret_present"],
    ) = core.keychain_presence(core.SYSTEM_KEYCHAIN)
    (
        rep["automation_env_client_id_present"],
        rep["automation_env_client_secret_present"],
    ) = core.automation_env_presence(env)
    try:
        rep["auth_mode"] = core.auth_mode(env)
    except SecretsError as exc:
        rep["auth_mode"] = exc.message.split(":")[0][:40]
    rep["session_login_valid"] = core.session_valid(cli, env) if cli else False
    rep["fetch_names_ok"] = False
    rep["names_count"] = 0
    rep["unresolved_reference_count"] = 0
    rep["failure_reason"] = None
    try:
        secrets = core.fetch_all("/", env)
        rep["fetch_names_ok"] = True
        rep["names_count"] = len(secrets)
        rep["unresolved_reference_count"] = sum(1 for v in secrets.values() if core.REF_RE.match(v))
    except SecretsError as exc:
        rep["failure_reason"] = exc.message.split(":")[0][:60]
    ok = (
        rep["environment_ok"]
        and rep["cli_present"]
        and rep["project_id_resolvable"]
        and rep["fetch_names_ok"]
        and rep["unresolved_reference_count"] == 0
    )
    rep["ok"] = bool(ok)
    return rep


def cmd_doctor(args: Sequence[str], env: Mapping[str, str], out: TextIO) -> int:
    as_json = False
    rest = list(args)
    if "--json" in rest:
        as_json = True
        rest.remove("--json")
    _parse(rest, False, 0)
    rep = doctor_report(env)
    if as_json:
        out.write(json.dumps(rep, sort_keys=True) + "\n")
    else:
        for key in rep:
            val = rep[key]
            shown = (
                ("true" if val else "false")
                if isinstance(val, bool)
                else ("none" if val is None else str(val))
            )
            out.write("%s: %s\n" % (key, shown))
    out.flush()
    return 0 if rep["ok"] else 1


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if not argv or argv[0] in ("-h", "--help", "help"):
        (out if argv else err).write(USAGE)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "get":
            return cmd_get(rest, env, out)
        if cmd == "names":
            return cmd_names(rest, env, out)
        if cmd == "set":
            return cmd_set(rest, env, out, sys.stdin)
        if cmd == "run":
            return cmd_run(rest, env, err)
        if cmd == "doctor":
            return cmd_doctor(rest, env, out)
        raise UsageError("unknown command %s" % cmd)
    except SecretsError as exc:
        err.write("fleet-secrets: %s\n" % exc.message)
        if isinstance(exc, UsageError):
            err.write(USAGE)
        return exc.code
