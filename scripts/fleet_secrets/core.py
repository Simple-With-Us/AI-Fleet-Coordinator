"""Compatibility import for the shared direct Infisical client.

New consumers import fleet_infisical directly.  This alias retains the existing
CLI and import contract without another reader implementation.

This compatibility path trusts its Python process.  The preload check compares
declared routing origin; it does not authenticate executed provenance against
arbitrary runtime code.  Warmed module and exception identity, including
intentional in-process patches, remain shared.
"""

import importlib.util
import os
import stat
import sys
from importlib._bootstrap import _ModuleLockManager
from pathlib import Path
from types import ModuleType

_MAX_CLIENT_BYTES = 2 * 1024 * 1024


def _load_client():
    expected_path = Path(__file__).resolve().parents[1] / "fleet_infisical.py"
    if expected_path.is_symlink():
        raise ImportError("shared Infisical client unavailable")
    expected = expected_path.resolve()
    # Share the normal import lock so either spelling waits for complete initialization.
    with _ModuleLockManager("fleet_infisical"):
        if "fleet_infisical" in sys.modules:
            client = sys.modules["fleet_infisical"]
            try:
                if not isinstance(client, ModuleType):
                    raise ValueError()
                # Compare declared origin without invoking the module's __getattr__ hook.
                metadata = vars(client)
                origin = vars(metadata.get("__spec__")).get("origin")
                if (
                    not isinstance(metadata.get("__file__"), str)
                    or not isinstance(origin, str)
                    or Path(metadata["__file__"]).resolve() != expected
                    or Path(origin).resolve() != expected
                ):
                    raise ValueError()
            except Exception:
                raise ImportError("shared Infisical client origin invalid") from None
        else:
            spec = importlib.util.spec_from_file_location("fleet_infisical", expected)
            if spec is None or spec.loader is None:
                raise ImportError("shared Infisical client unavailable")
            client = importlib.util.module_from_spec(spec)
            spec._initializing = True
            sys.modules[spec.name] = client
            try:
                flags = (
                    os.O_RDONLY
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0)
                    | getattr(os, "O_NONBLOCK", 0)
                )
                fd = os.open(expected_path, flags)
                with os.fdopen(fd, "rb") as source:
                    before = os.fstat(source.fileno())
                    if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_CLIENT_BYTES:
                        raise ImportError("shared Infisical client unavailable")
                    raw = source.read(_MAX_CLIENT_BYTES + 1)
                    after = os.fstat(source.fileno())
                path_info = expected_path.lstat()
                fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
                if (
                    len(raw) > _MAX_CLIENT_BYTES
                    or any(getattr(before, f) != getattr(after, f) for f in fields)
                    or any(getattr(before, f) != getattr(path_info, f) for f in fields)
                ):
                    raise ImportError("shared Infisical client unavailable")
                # Execute only pinned source bytes, never timestamp-valid cached bytecode.
                exec(compile(raw, str(expected), "exec"), vars(client))
            except BaseException as exc:
                if sys.modules.get(spec.name) is client:
                    del sys.modules[spec.name]
                if isinstance(exc, Exception):
                    raise ImportError("shared Infisical client unavailable") from None
                raise
            finally:
                spec._initializing = False
        return client


_client = _load_client()

_compat_name = __name__
globals().update(vars(_client))
sys.modules[_compat_name] = _client
