"""Bind only the existing read-only Code mirror settings credential during a fetch."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import subprocess
import sys

NAME = "code-mirrors-settings-read"
ENV = "FLEET_KNOBS_INFISICAL_TOKEN"


class _Credential:
    """Loaded material is opaque in diagnostics and exposed only for temporary binding."""
    __slots__ = ("__material",)

    def __init__(self, material):
        if not material.startswith("st.") or not 20 <= len(material) <= 8192 or any(c.isspace() for c in material):
            raise ValueError("settings-credential-unavailable")
        self.__material = material

    def __repr__(self):
        return "<CodeMirrorSettingsCredential redacted>"

    __str__ = __repr__

    def _environment_value(self):
        return self.__material


def _credential_mode_allowed(directory, info):
    if not stat.S_ISREG(info.st_mode) or not 1 <= info.st_size <= 8192:
        return False
    if not info.st_mode & 0o077 and info.st_uid in (0, os.geteuid()):
        return True
    # systemd can expose root-owned encrypted credentials to User= through an ACL.
    # Accept that one protected layout without permitting arbitrary group-readable files.
    if info.st_uid != 0 or info.st_gid != 0 or stat.S_IMODE(info.st_mode) != 0o440:
        return False
    if len(directory.parts) != 4 or directory.parts[:3] != ("/", "run", "credentials") or directory.name in (".", ".."):
        return False
    try:
        for parent in (directory, *directory.parents):
            metadata = parent.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o022:
                return False
        metadata = directory.lstat()
        return metadata.st_uid == 0 and metadata.st_gid == 0 and stat.S_IMODE(metadata.st_mode) == 0o550
    except OSError:
        return False


def _load_token():
    if sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "infisical-service-token", "-a", NAME, "-w"],
            capture_output=True, text=True, timeout=15, check=False,
        )
        if result.returncode:
            raise ValueError("settings-credential-unavailable")
        return _Credential(result.stdout.strip())
    elif sys.platform.startswith("linux"):
        raw_directory = os.environ.get("CREDENTIALS_DIRECTORY", "")
        directory = Path(raw_directory)
        if not directory.is_absolute() or directory.is_symlink() or not directory.is_dir():
            raise ValueError("settings-credential-unavailable")
        fd = os.open(directory / NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (info.st_mode & 0o077 and str(directory) != raw_directory) or not _credential_mode_allowed(directory, info):
                raise ValueError("settings-credential-unavailable")
            return _Credential(stream.read(8193).decode().strip())
    else:
        raise ValueError("settings-credential-unavailable")


@contextmanager
def settings_read_binding():
    previous = os.environ.get(ENV)
    if previous:
        yield
        return
    try:
        credential = _load_token()
    except Exception:
        raise ValueError("settings-credential-unavailable") from None
    os.environ[ENV] = credential._environment_value()
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = previous
