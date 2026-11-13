"""The shared direct Infisical command, invoked by its isolated absolute script path.

  run [--path /] [--only NAME,NAME] -- CMD [ARG...]
        exec CMD with the secrets as environment variables.  Existing variables of the same
        name are replaced.  A few names (PATH, HOME, LD_*, DYLD_* ...) are never injected.
  get NAME [NAME ...] [--path /] [--project fleet-ops|ai-fleet-coordinator]
        print one value, exactly, with no trailing newline.  With several names, print a JSON
        object of name to value instead (only the requested named reads).  Exit 3 when
        any NAME is absent;  nothing is printed then.
  names [--path /] [--project fleet-ops|ai-fleet-coordinator]
        key names only, one per line.  --project picks the Infisical project (read-only;  default fleet-ops).
  set NAME [--path /] [--state-dir ABS] [--purpose SLUG]
        [--request-id UUID --request-created-at EPOCH_MS] [--reconcile]
        create or update one allowlisted secret in Fleet-OPS prod using its own scoped writer.
        The value comes from stdin, with one trailing newline removed.  Output is a JSON receipt
        with nonsecret recovery identity.  CLI and login sync share the declared protected state
        root, default ~/.local/state/fleet-infisical-writes, across purposes.  An uncertain write
        blocks fresh attempts.  Reconcile requires the original identity, reads no stdin and
        never retries a provider mutation.  A separate store or external writer is outside this
        local coordination guarantee.
  doctor [--json] [--project fleet-ops|ai-fleet-coordinator] [--path /]
        booleans and counts only, never a value.  An explicit project or path checks only that route.

Exit codes:  0 ok, 2 usage or refused configuration, 3 key absent, 4 fetch or auth failure,
5 a value came back as an unresolved Infisical reference, 126/127 the command in `run` could
not be started.  Error text names keys and reason tokens, never values.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TextIO

if __name__ == "__main__":
    # Isolated absolute-path callers import only this reviewed scripts directory.
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import fleet_infisical as core
from fleet_infisical import SecretsError, UsageError

USAGE = (
    "usage: fleet-secrets run [--path /] [--only NAME,NAME] -- CMD [ARG...]\n"
    "       fleet-secrets get NAME [NAME ...] [--path /] [--project fleet-ops|ai-fleet-coordinator]\n"
    "       fleet-secrets get-verified NAME --path /folder   (fails unless the row proves its folder)\n"
    "       fleet-secrets names [--path /] [--project fleet-ops|ai-fleet-coordinator]\n"
    "       fleet-secrets set NAME --path /linear [--state-dir ABS] [--purpose SLUG]\n"
    "           [--request-id UUID --request-created-at EPOCH_MS] [--reconcile]\n"
    "           (value on standard input; reconcile reads no value)\n"
    "       fleet-secrets doctor [--json] [--project fleet-ops|ai-fleet-coordinator] [--path /]\n"
)


def _parse(args: Sequence[str], allow_only: bool, positional: int) -> dict[str, object]:
    """Tiny parser:  --path P, --only A,B, and `positional` bare arguments.  Raises SecretsError(code 2)."""
    opts: dict[str, object] = {"path": "/", "only": None, "args": []}
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--name", "--from", "--var", "--service", "--account"):
            if i + 1 >= len(args):
                raise UsageError(f"{a} needs a value")
            opts[a[2:]] = args[i + 1]
            i += 2
        elif (
            a.startswith("--name=")
            or a.startswith("--from=")
            or a.startswith("--var=")
            or (a.startswith("--service=") or a.startswith("--account="))
        ):
            key, _, val = a[2:].partition("=")
            opts[key] = val
            i += 1
        elif a in ("--path", "--only"):
            if a == "--only" and not allow_only:
                raise UsageError("unknown option --only for this command")
            if i + 1 >= len(args):
                raise UsageError(f"{a} needs a value")
            opts["path" if a == "--path" else "only"] = args[i + 1]
            i += 2
        elif a.startswith("--path=") or (allow_only and a.startswith("--only=")):
            key, _, val = a[2:].partition("=")
            opts[key] = val
            i += 1
        elif a.startswith("-") and a != "-":
            raise UsageError(f"unknown option {a}")
        else:
            opts["args"].append(a)  # type: ignore[union-attr]
            i += 1
    if positional < 0:  # at least -positional arguments
        if len(opts["args"]) < -positional:  # type: ignore[arg-type]
            raise UsageError("wrong number of arguments")
    elif len(opts["args"]) != positional:  # type: ignore[arg-type]
        raise UsageError("wrong number of arguments")
    return opts


def _only_names(raw: object | None) -> list[str] | None:
    if raw is None:
        return None
    names = [core.check_name(n.strip()) for n in str(raw).split(",") if n.strip()]
    if not names:
        raise UsageError("--only needs at least one key name")
    return names


def _take_project(args: Sequence[str]) -> tuple[str, list[str]]:
    """Split a read-only `--project NAME` (or `--project=NAME`) out of `args`.  Default fleet-ops."""
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
        raise UsageError("unknown --project:  use {}".format(" or ".join(core.PROJECT_SOURCES)))
    return project, rest


def cmd_get(args: Sequence[str], env: Mapping[str, str], out: TextIO) -> int:
    project, rest = _take_project(args)
    opts = _parse(rest, False, -1)
    names = [core.check_name(str(a)) for a in opts["args"]]  # type: ignore[union-attr]
    path = core.check_path(str(opts["path"]))
    project_id = core.infisical_project_id(env, core.home_dir(env), project)
    secrets = core.fetch_project(project_id, path, env, names=tuple(names))
    # Bound token reads request only the named keys, never the rest of the folder.
    # Nothing is printed unless every requested name resolves.
    missing = [n for n in names if n not in secrets]
    if missing:
        raise SecretsError(
            "key {} is not in Infisical at {}".format(", ".join(missing), opts["path"]), 3
        )
    core.reject_unresolved(secrets, names)
    if len(names) == 1:
        out.write(secrets[names[0]])
    else:
        out.write(json.dumps({n: secrets[n] for n in names}))
    out.flush()
    return 0


def cmd_get_verified(args: Sequence[str], env: Mapping[str, str], out: TextIO) -> int:
    opts = _parse(args, False, 1)
    name = core.check_name(str(opts["args"][0]))  # type: ignore[index]
    path = core.check_path(str(opts["path"]))
    if path == "/":
        raise UsageError("get-verified needs an explicit --path:  the project root proves nothing")
    value = core.fetch_verified(name, path, env)
    out.write(value)
    out.flush()
    return 0


def write_intent_for_cli(
    env: Mapping[str, str],
    purpose: str,
    request_id: str | None = None,
    created_at_ms: int | None = None,
    state_dir: str | Path | None = None,
) -> core.WriteIntent:
    """One caller-declared state root for CLI and login sync; allocation performs no I/O."""
    directory = (
        core.home_dir(env) / ".local/state/fleet-infisical-writes"
        if state_dir is None
        else Path(state_dir)
    )
    if (request_id is None) != (created_at_ms is None):
        raise UsageError("write-intent-identity-pair-required")
    if request_id is None:
        return core.new_write_intent(directory, purpose)
    return core.WriteIntent(directory, purpose, request_id, created_at_ms)


def _write_options(args: Sequence[str]) -> dict[str, object]:
    """Strict set-only options; malformed input is refused before stdin or state access."""
    options: dict[str, object] = {"path": "/", "purpose": "fleet-infisical-cli", "reconcile": False}
    names = []
    seen = set()
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--reconcile":
            key, value = "reconcile", True
        elif arg.startswith("--"):
            key, separator, value = arg[2:].partition("=")
            if key not in {"path", "purpose", "state-dir", "request-id", "request-created-at"}:
                raise UsageError("write-option-invalid")
            if not separator:
                i += 1
                if i >= len(args):
                    raise UsageError("write-option-value-required")
                value = args[i]
            if not value:
                raise UsageError("write-option-value-required")
        elif arg.startswith("-"):
            raise UsageError("write-option-invalid")
        else:
            names.append(arg)
            i += 1
            continue
        if key in seen:
            raise UsageError("write-option-duplicate")
        seen.add(key)
        options[key] = value
        i += 1
    if len(names) != 1:
        raise UsageError("wrong number of arguments")
    options["name"] = core.check_name(names[0])
    options["path"] = core.check_path(str(options["path"]))
    core.check_writable(options["name"], options["path"])
    if "request-created-at" in options:
        raw = options["request-created-at"]
        if not str(raw).isascii() or not str(raw).isdecimal() or len(str(raw)) > 20:
            raise UsageError("write-intent-time-invalid")
        options["request-created-at"] = int(raw)
    if options["reconcile"] and "request-id" not in options:
        raise UsageError("reconcile requires the original request identity")
    return options


def _write_report(result, name: str, path: str, intent: core.WriteIntent) -> dict[str, object]:
    """Only fixed outcomes and nonsecret recovery identity cross the command boundary."""
    if not isinstance(result, core.WriteError) and (
        not isinstance(result, core.WriteReceipt)
        or result.outcome != "ok"
        or result.request_id != intent.request_id
        or result.created_at_ms != intent.created_at_ms
        or result.result not in ("created", "updated")
        or type(result.replayed) is not bool
    ):
        raise SecretsError("write-receipt-invalid")
    report = {
        "request_id": result.request_id,
        "created_at_ms": result.created_at_ms,
        "purpose": getattr(result, "purpose", intent.purpose),
        "name": getattr(result, "name", None) or name,
        "path": path,
        "outcome": result.outcome,
    }
    if isinstance(result, core.WriteError):
        report.update(
            provider_status=result.provider_status,
            retryable=result.retryable,
            request_may_have_been_sent=result.request_may_have_been_sent,
        )
    else:
        report.update(result=result.result, replayed=result.replayed)
    return report


def cmd_set(args: Sequence[str], env: Mapping[str, str], out: TextIO, stdin: TextIO) -> int:
    opts = _write_options(args)
    name, path = opts["name"], opts["path"]
    intent = write_intent_for_cli(
        env, opts["purpose"], request_id=opts.get("request-id"),
        created_at_ms=opts.get("request-created-at"), state_dir=opts.get("state-dir"),
    )
    if opts["reconcile"]:
        try:
            result = core.reconcile_secret(name, path, env, intent=intent)
        except core.WriteError as exc:
            out.write(json.dumps(_write_report(exc, name, path, intent), sort_keys=True) + "\n")
            out.flush()
            raise
        out.write(json.dumps(_write_report(result, name, path, intent), sort_keys=True) + "\n")
        out.flush()
        return 0
    value = stdin.read(core.MAX_VALUE_BYTES + 1)
    value = value.removesuffix("\n")
    if not value:
        raise UsageError("set reads the value from standard input and it was empty")
    try:
        result = core.put_secret(name, path, value, env, intent=intent)
    except core.WriteError as exc:
        out.write(json.dumps(_write_report(exc, name, path, intent), sort_keys=True) + "\n")
        out.flush()
        raise
    finally:
        value = ""
    out.write(json.dumps(_write_report(result, name, path, intent), sort_keys=True) + "\n")
    out.flush()
    return 0


def cmd_names(args: Sequence[str], env: Mapping[str, str], out: TextIO) -> int:
    project, rest = _take_project(args)
    opts = _parse(rest, False, 0)
    path = core.check_path(str(opts["path"]))
    project_id = core.infisical_project_id(env, core.home_dir(env), project)
    # Metadata-only reads use the same scoped REST client without exporting values.
    names = core.list_project_names(project_id, path, env)
    out.write("".join(name + "\n" for name in names))
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
                f"--only cannot inject reserved name {reserved[0]} (PATH, HOME, SHELL, interpreter, LD_* and DYLD_* "
                "variables are never injected);  read it with `get` if you really need the value"
            )
    secrets = core.fetch_all(core.check_path(str(opts["path"])), env)
    if only is not None:
        for name in only:
            if name not in secrets:
                raise SecretsError("key {} is not in Infisical at {}".format(name, opts["path"]), 3)
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
        raise SecretsError(f"command not found: {command[0]}", 127) from None
    except PermissionError:
        raise SecretsError(f"command not executable: {command[0]}", 126) from None
    except OSError:
        raise SecretsError(f"could not start: {command[0]}", 126) from None
    return 0  # unreachable after a real exec


def doctor_report(
    env: Mapping[str, str], *, project: str | None = None, path: str | None = None
) -> dict[str, object]:
    """Masked diagnostics for the legacy set or one explicitly selected project/folder."""
    selected = project is not None or path is not None
    primary_project = core.DEFAULT_PROJECT if project is None else project
    if primary_project not in core.PROJECT_SOURCES:
        raise UsageError("unknown Infisical project")
    path = core.check_path("/" if path is None else path)
    rep: dict[str, object] = {}
    rep["environment_ok"] = core.environment_refusal(env) is None
    cli = core.find_cli(env)
    rep["cli_present"] = cli is not None
    rep["cli_version"] = core.cli_version(cli, env) if cli else None
    try:
        core.infisical_project_id(env, core.home_dir(env), primary_project)
        rep["project_id_resolvable"] = True
    except SecretsError:
        rep["project_id_resolvable"] = False
    rep["reader_configured"] = bool(
        (env.get(core.project_token_name(primary_project)) or "").strip()
    )
    rep["auth_mode"] = "token" if rep["reader_configured"] else "missing"
    rep["writer_configured"] = primary_project == core.DEFAULT_PROJECT and bool(
        (env.get(core.ENV_WRITE_TOKEN) or "").strip()
    )
    rep["fetch_names_ok"] = False
    rep["names_count"] = 0
    rep["unresolved_reference_count"] = 0
    rep["failure_reason"] = None
    try:
        secrets = core.fetch_all(path, env, primary_project)
        rep["fetch_names_ok"] = True
        rep["names_count"] = len(secrets)
        rep["unresolved_reference_count"] = sum(1 for v in secrets.values() if core.REF_RE.match(v))
    except SecretsError as exc:
        rep["failure_reason"] = exc.message.split(":")[0][:60]
    # The unqualified legacy doctor also probes the read-only FleetLink folder.
    other_ok = True
    for other_project in core.PROJECT_SOURCES if not selected else ():
        if other_project == core.DEFAULT_PROJECT:
            continue
        slug = other_project.replace("-", "_")
        try:
            core.infisical_project_id(env, core.home_dir(env), other_project)
            rep[f"{slug}_project_id_resolvable"] = True
        except SecretsError:
            rep[f"{slug}_project_id_resolvable"] = False
        rep[f"{slug}_fetch_ok"] = False
        if rep[f"{slug}_project_id_resolvable"]:
            try:
                core.fetch_all("/fleetlink", env, other_project)
                rep[f"{slug}_fetch_ok"] = True
            except SecretsError:
                pass
        other_ok = other_ok and bool(rep[f"{slug}_fetch_ok"])
    ok = (
        rep["environment_ok"]
        and rep["project_id_resolvable"]
        and rep["fetch_names_ok"]
        and rep["unresolved_reference_count"] == 0
        and (
            other_ok or not core.is_linux()
        )  # the server needs the FleetLink tokens;  a Mac reads its own
    )
    rep["ok"] = bool(ok)
    return rep


def cmd_doctor(args: Sequence[str], env: Mapping[str, str], out: TextIO) -> int:
    as_json = False
    rest = list(args)
    if "--json" in rest:
        as_json = True
        rest.remove("--json")
    selected = any(
        a in ("--project", "--path") or a.startswith(("--project=", "--path=")) for a in rest
    )
    project, rest = _take_project(rest)
    opts = _parse(rest, False, 0)
    path = core.check_path(str(opts["path"]))
    rep = doctor_report(env, project=project, path=path) if selected else doctor_report(env)
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
            out.write(f"{key}: {shown}\n")
    out.flush()
    return 0 if rep["ok"] else 1


def main(
    argv: Sequence[str] | None = None,
    env: Mapping[str, str] | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    *,
    prog: str = "fleet-secrets",
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if not argv or argv[0] in ("-h", "--help", "help"):
        (out if argv else err).write(USAGE.replace("fleet-secrets", prog))
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "get":
            return cmd_get(rest, env, out)
        if cmd == "get-verified":
            return cmd_get_verified(rest, env, out)
        if cmd == "names":
            return cmd_names(rest, env, out)
        if cmd == "set":
            return cmd_set(rest, env, out, sys.stdin)
        if cmd == "run":
            return cmd_run(rest, env, err)
        if cmd == "doctor":
            return cmd_doctor(rest, env, out)
        raise UsageError(f"unknown command {cmd}")
    except SecretsError as exc:
        err.write(f"{prog}: {exc.message}\n")
        if isinstance(exc, UsageError):
            err.write(USAGE.replace("fleet-secrets", prog))
        return exc.code


if __name__ == "__main__":
    sys.exit(main(prog="python3 -I fleet_infisical_cli.py"))
