#!/usr/bin/env -S python3 -I
"""Scoped Linux auth for fleet-secrets, with an existing root-private bootstrap fallback."""

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path


def _load_core():
    here = Path(__file__).resolve().parent
    for path in (here / "core.py", here / "fleet_secrets/core.py"):
        if path.is_file():
            spec = importlib.util.spec_from_file_location("fleet_secrets_entry_core", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise RuntimeError("fleet-secrets core is unavailable")


core = _load_core()


class EntryError(Exception):
    def __init__(self, message, code=4):
        super().__init__(message)
        self.code = code


class Interrupted(Exception):
    def __init__(self, signum):
        self.signum = signum


def _bootstrap_pairs(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate bootstrap key")
        result[name] = value
    return result


def private_bootstrap():
    """Read only the fixed existing root bootstrap, without following symlinks.

    Every directory is opened relative to the last checked descriptor. The file is
    bounded and read from the descriptor whose ownership and mode were checked.
    Nothing is written or retained outside the scoped login environment.
    """
    if not sys.platform.startswith("linux") or os.geteuid() != 0:
        return {}
    fd = None
    try:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        for name in ("root", ".config", "fleet-secrets"):
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            info = os.fstat(fd)
            if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) & 0o022:
                raise ValueError("unsafe bootstrap directory")
            if name == "fleet-secrets" and stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError("unsafe bootstrap parent")
        child = os.open("bootstrap.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        os.close(fd)
        fd = child
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise ValueError("unsafe bootstrap file")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = None
            text = stream.read(16385)
        if len(text) > 16384:
            raise ValueError("oversized bootstrap")
        data = json.loads(text, object_pairs_hook=_bootstrap_pairs)
        expected = {core.ENV_AUTOMATION_ID, core.ENV_AUTOMATION_SECRET}
        if (
            not isinstance(data, dict)
            or set(data) != expected
            or any(not isinstance(v, str) or not v.strip() for v in data.values())
        ):
            raise ValueError("invalid bootstrap schema")
        return data
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError):
        raise EntryError("root-private bootstrap is invalid or unsafe") from None
    finally:
        if fd is not None:
            os.close(fd)


def load_machine_credentials(env):
    names = (core.ENV_AUTOMATION_ID, core.ENV_AUTOMATION_SECRET)
    if not any((env.get(name) or "").strip() for name in names):
        env = private_bootstrap()
    missing = [name for name in names if not (env.get(name) or "").strip()]
    if missing:
        raise EntryError("missing required environment variables: " + ", ".join(missing))
    values = [env[name].strip() for name in names]
    if any("\0" in value or "\n" in value or "\r" in value for value in values):
        raise EntryError("invalid automation environment credentials")
    return values


def _login(cli, child):
    """Bound the login and reap its child when timeout or interruption unwinds it."""
    proc = None
    previous = signal.getsignal(signal.SIGTERM)

    def interrupted(signum, frame):
        raise Interrupted(signum)

    signal.signal(signal.SIGTERM, interrupted)
    try:
        proc = subprocess.Popen(
            [cli, "login", "--method=universal-auth", "--plain", "--silent"],
            env=child,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            cwd="/",
        )
        stdout, _stderr = proc.communicate(timeout=25)
        token = stdout.strip()
        if proc.returncode or not token or any(char.isspace() for char in token) or "\0" in token:
            raise EntryError("automation environment authentication failed")
        return token
    finally:
        try:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate(timeout=3)
        finally:
            signal.signal(signal.SIGTERM, previous)


def authenticated_env():
    env = dict(os.environ)
    token = (env.get(core.ENV_TOKEN) or "").strip()
    if token:
        if any(char.isspace() for char in token) or "\0" in token:
            raise EntryError("invalid INFISICAL_TOKEN")
        env[core.ENV_TOKEN] = token
    else:
        client_id, client_secret = load_machine_credentials(env)
        cli = core.find_cli(env)
        if not cli:
            raise EntryError("infisical CLI is unavailable", 127)
        child = core._base_env(env)
        for name in core.AUTH_ENV_VARS:
            child.pop(name, None)
        child[core.ENV_CLIENT_ID] = client_id
        child[core.ENV_CLIENT_SECRET] = client_secret
        env[core.ENV_TOKEN] = _login(cli, child)
    for name in core.AUTH_ENV_VARS - {core.ENV_TOKEN}:
        env.pop(name, None)
    return env


def cli_path(env):
    """The plain CLI to delegate to: an explicit CLI path, then the install home, then the default.

    `FLEET_SECRETS_BIN` is not read here:  fleet-wide it names the *fleet-secrets command*
    (`scripts/agent_sync/zulip.py`), which on the server is this entry point itself.
    """
    explicit = (env.get("FLEET_SECRETS_CLI") or "").strip()
    if explicit:
        return Path(os.path.expanduser(explicit))
    installed = Path(__file__).resolve().parent / "fleet-secrets-cli"
    if installed.is_file():
        return installed
    return (
        Path(env.get("FLEET_SECRETS_HOME") or Path.home() / ".local/share/fleet-secrets")
        / "fleet-secrets"
    )


def _is_self(target):
    """True when the delegate would be this entry point, which would exec itself forever."""
    try:
        return target.resolve() == Path(__file__).resolve()
    except OSError:
        return False


def main():
    try:
        # Resolve and check the delegate before authenticating:  a missing or mis-pathed CLI
        # must not cost a login, and the operator must be told about the real fault.
        target = cli_path(dict(os.environ))
        if _is_self(target):
            raise EntryError("fleet-secrets CLI is this entry point (set FLEET_SECRETS_CLI)", 127)
        if not target.is_file() or not os.access(target, os.X_OK):
            raise EntryError("fleet-secrets CLI is unavailable (set FLEET_SECRETS_CLI)", 127)
        args = sys.argv[1:]
        if args and args[0] not in ("-h", "--help", "help"):
            env = authenticated_env()
        else:
            # Usage text reads no secret:  no login (so no 25 second wait when Infisical is down), and
            # the delegate gets no credential at all.
            env = {k: v for k, v in os.environ.items() if k not in core.AUTH_ENV_VARS}
        os.execve(str(target), [str(target), *args], env)
    except EntryError as exc:
        print("fleet-secrets: " + str(exc), file=sys.stderr)
        return exc.code
    except subprocess.TimeoutExpired:
        print("fleet-secrets: automation environment authentication timed out", file=sys.stderr)
        return 124
    except Interrupted as exc:
        return 128 + exc.signum
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, subprocess.SubprocessError):
        print("fleet-secrets: automation environment authentication failed", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
