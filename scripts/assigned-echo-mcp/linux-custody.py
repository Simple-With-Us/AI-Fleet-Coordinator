#!/usr/bin/python3 -I
"""ECHO-only native custody operations. No alternate source, seat or code path."""

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

CODE = Path("/usr/local/libexec/assigned-echo-mcp")
ENCRYPTED = Path("/etc/credstore.encrypted/assigned-echo-mcp")
CREDENTIAL = Path("/run/credentials/assigned-echo-mcp.service/assigned-echo-mcp")
TOKEN = re.compile(rb"sbt_echo_[A-Za-z0-9_-]{43}")


def check_code():
    for path in (Path("/usr/local"), Path("/usr/local/libexec"), CODE):
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("unsafe installed code")
    for name in ("linux-custody.py", "assigned-hosted-stdio.py"):
        info = (CODE / name).lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("unsafe installed code")


def ingress(stream):
    """Owner-only provisioning after separate approval. Plaintext never hits disk."""
    check_code()
    if os.geteuid() != 0:
        raise ValueError("owner-only ingress")
    parent_fd = os.open(ENCRYPTED.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != 0
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise ValueError("unsafe encrypted credential directory")
        try:
            os.stat(ENCRYPTED.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise ValueError("existing encrypted credential requires owner review")
        raw = stream.read(257)
        if len(raw) > 256:
            raise ValueError("oversized assigned bearer")
        raw = raw.strip()
        if not TOKEN.fullmatch(raw):
            raise ValueError("expected assigned ECHO bearer")
        result = subprocess.run(
            [
                "/usr/bin/systemd-creds",
                "encrypt",
                "--with-key=host",
                "--name=assigned-echo-mcp",
                "-",
                "-",
            ],
            input=raw,
            capture_output=True,
            env={"PATH": "/usr/bin:/bin"},
            timeout=30,
            check=False,
        )
        if (
            result.returncode
            or not result.stdout
            or len(result.stdout) > 65536
            or raw in result.stdout
        ):
            raise ValueError("native encryption failed")
        # Atomic exclusive creation prevents a race overwriting another credential.
        fd = os.open(
            ENCRYPTED.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=parent_fd,
        )
        with os.fdopen(fd, "wb") as target:
            target.write(result.stdout)
        return 0
    finally:
        os.close(parent_fd)


def child():
    check_code()
    # systemd materializes this credential only inside the fixed service context.
    fd = os.open(CREDENTIAL, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as source:
        info = os.fstat(source.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("unsafe service credential")
        raw = source.read(257)
        if len(raw) > 256:
            raise ValueError("oversized service credential")
        raw = raw.strip()
    if not TOKEN.fullmatch(raw):
        raise ValueError("expected assigned ECHO bearer")
    # Only the token and a fixed PATH reach the reviewed adapter. No reader auth.
    os.execve(
        "/usr/bin/python3",
        ["/usr/bin/python3", "-I", str(CODE / "assigned-hosted-stdio.py")],
        {"PATH": "/usr/bin:/bin", "ASSIGNED_MCP_BEARER": raw.decode("ascii")},
    )


def launch():
    check_code()
    if os.geteuid() != 0:
        raise ValueError("owner-controlled system service launch required")
    argv = [
        "/usr/bin/systemd-run",
        "--quiet",
        "--pipe",
        "--wait",
        "--collect",
        "--service-type=exec",
        "--unit=assigned-echo-mcp",
        "--property=DynamicUser=yes",
        "--property=NoNewPrivileges=yes",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes",
        "--property=PrivateTmp=yes",
        "--property=ProtectProc=invisible",
        "--property=RestrictSUIDSGID=yes",
        "--property=CapabilityBoundingSet=",
        "--property=RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX",
        "--property=LoadCredentialEncrypted=assigned-echo-mcp:" + str(ENCRYPTED),
        "/usr/bin/python3",
        "-I",
        str(CODE / "linux-custody.py"),
        "child",
    ]
    os.execve(argv[0], argv, {"PATH": "/usr/bin:/bin"})


def main(argv):
    try:
        if argv == ["ingress"]:
            return ingress(sys.stdin.buffer)
        if argv == ["child"]:
            child()
            return 0
        if not argv:
            launch()
            return 0
        raise ValueError("no seat, credential or path selectors are supported")
    except (OSError, ValueError, subprocess.SubprocessError):
        print("assigned-echo-mcp: native custody operation refused or unavailable", file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
