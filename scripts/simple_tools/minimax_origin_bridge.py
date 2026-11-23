"""Exact-version MiniMax plugin context bridge. No credentials or startup-session fallback.

The installed 0.7.4 plugin runtime receives native ToolContext but drops it before
its shared MCP pool. Only fleet-own-tools/simple-tools receives this projection.
Upstream changes refuse installation; missing per-call metadata holds origins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import tempfile
from pathlib import Path

VERSION = "0.7.4"
CHUNK = "chunks/chunk-YYSTRBDA.js"
ORIGINAL_SHA256 = "4c5111aad07b0706b0e6c76ae0ddbf1b1230cfb4374bb4a493fcadfce69d1510"
CONTEXT_CHUNK = "chunks/chunk-66K6XTTT.js"
CONTEXT_SHA256 = "6d2291a40bd1641653c43f10d7ecb47b546b34000b00b26e99ccf8974c55a08c"
CONTEXT_OLD = "return{sessionId:e.lease.sessionId,turnId:e.lease.turnId,agentName:e.agent.agentName,parentAgentConfig:e.preparation.agentConfig,"
CONTEXT_NEW = "return{afcNativeLease:{sessionId:e.lease.sessionId,turnId:e.lease.turnId},sessionId:e.lease.sessionId,turnId:e.lease.turnId,agentName:e.agent.agentName,parentAgentConfig:e.preparation.agentConfig,"
BACKUP_SUFFIX = ".afc-mm-origin-0.7.4.original"
META_KEY = "com.simplewithus/native-origin/1"
PLUGIN_OLD = "this.pool.callTool(Go(e),r.name,CNt(a),void 0,o?{signal:o}:void 0)"
PLUGIN_NEW = (
    "this.pool.callTool(Go(e),r.name,CNt(a),void 0,"
    "{...o?{signal:o}:{},"
    '...e.pluginName==="fleet-own-tools"&&e.server.name==="simple-tools"'
    "&&s?.afcNativeLease?.sessionId===s?.sessionId&&s?.afcNativeLease?.turnId===s?.turnId"
    '&&typeof s?.sessionId==="string"&&/^mvs_[0-9a-f]{32}$/.test(s.sessionId)'
    '&&typeof s?.turnId==="string"&&s.turnId.length>0&&s.turnId.length<=256'
    '&&typeof s?.toolCallId==="string"&&s.toolCallId.length>0&&s.toolCallId.length<=256'
    '?{afcNativeOrigin:{version:1,platform:"minimax",session_id:s.sessionId,'
    "turn_id:s.turnId,tool_call_id:s.toolCallId}}:{}})"
)
POOL_OLD = "c.client.callTool({name:r,arguments:n},void 0,{timeout:l,"
POOL_NEW = (
    "c.client.callTool({name:r,arguments:n,"
    '...s?.afcNativeOrigin?{_meta:{"' + META_KEY + '":s.afcNativeOrigin}}:{}},'
    "void 0,{timeout:l,"
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def patched(data: bytes, *, context: bool = False) -> bytes:
    if digest(data) != (CONTEXT_SHA256 if context else ORIGINAL_SHA256):
        raise ValueError("unsupported MiniMax chunk hash")
    text = data.decode("utf-8")
    changes = (
        ((CONTEXT_OLD, CONTEXT_NEW),)
        if context
        else ((PLUGIN_OLD, PLUGIN_NEW), (POOL_OLD, POOL_NEW))
    )
    for old, new in changes:
        if text.count(old) != 1:
            raise ValueError("MiniMax bridge anchor is not unique")
        text = text.replace(old, new, 1)
    return text.encode("utf-8")


def private_read(path: Path) -> tuple[bytes, int]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_nlink != 1
            or info.st_mode & 0o022
        ):
            raise ValueError("unsafe MiniMax source ownership or mode")
        with os.fdopen(fd, "rb", closefd=False) as source:
            data = source.read(32 * 1024 * 1024 + 1)
        if len(data) > 32 * 1024 * 1024:
            raise ValueError("MiniMax source exceeds size bound")
        return data, stat.S_IMODE(info.st_mode)
    finally:
        os.close(fd)


def replace(path: Path, data: bytes, mode: int) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".afc-mm-origin-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            os.fchmod(target.fileno(), mode)
            target.write(data)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def run_chunk(
    root: Path, *, apply: bool = False, rollback: bool = False, context: bool = False
) -> dict[str, str]:
    # No current-release symlink: the operator must name the exact versioned package.
    if root.absolute() != root.resolve():
        raise ValueError("name the real versioned package, not a symlink")
    for directory in (root, root / "chunks"):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("unsafe MiniMax package directory")
    package, _ = private_read(root / "package.json")
    identity = json.loads(package)
    if identity.get("name") != "@minimax-ai/code" or identity.get("version") != VERSION:
        raise ValueError("unsupported MiniMax package version")
    path = root / (CONTEXT_CHUNK if context else CHUNK)
    current, mode = private_read(path)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if rollback:
        original_hash = CONTEXT_SHA256 if context else ORIGINAL_SHA256
        if digest(current) == original_hash:
            # An interrupted pair install may not have touched/backed up this chunk.
            # Exact known original bytes need no backup or write to be restored.
            return {"state": "restored" if apply else "rollback-ready", "sha256": original_hash}
        original, _ = private_read(backup)
        expected = patched(original, context=context)
        if current not in (original, expected):
            raise ValueError("MiniMax chunk drifted since bridge installation")
        if apply and current != original:
            latest, _ = private_read(path)
            if latest != current:
                raise ValueError("MiniMax chunk changed during rollback")
            replace(path, original, mode)
        return {"state": "restored" if apply else "rollback-ready", "sha256": digest(original)}
    if digest(current) == (CONTEXT_SHA256 if context else ORIGINAL_SHA256):
        original = current
    else:
        original, _ = private_read(backup)
        if current != patched(original, context=context):
            raise ValueError("unsupported MiniMax chunk hash")
    result = patched(original, context=context)
    if apply and current != result:
        if backup.exists():
            saved, _ = private_read(backup)
            if saved != original:
                raise ValueError("MiniMax backup does not match original")
        else:
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(original)
                target.flush()
                os.fsync(target.fileno())
        # Recheck immediately before atomic replacement. No overwrite of upstream drift.
        latest, _ = private_read(path)
        if latest != current:
            raise ValueError("MiniMax chunk changed during bridge installation")
        replace(path, result, mode)
    return {"state": "installed" if apply else "compatible", "sha256": digest(result)}


def run(root: Path, *, apply: bool = False, rollback: bool = False) -> dict:
    # Validate BOTH chunks before writing either. Install positive lease marker first;
    # remove projection first on rollback. Interrupted partial states hold origin.
    context_status = run_chunk(root, rollback=rollback, context=True)
    plugin_status = run_chunk(root, rollback=rollback)
    if not apply:
        return {"context": context_status, "plugin": plugin_status}
    if rollback:
        plugin_status = run_chunk(root, apply=True, rollback=True)
        context_status = run_chunk(root, apply=True, rollback=True, context=True)
    else:
        context_status = run_chunk(root, apply=True, context=True)
        plugin_status = run_chunk(root, apply=True)
    return {"context": context_status, "plugin": plugin_status}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--apply", action="store_true", help="write after compatibility checks")
    parser.add_argument("--rollback", action="store_true", help="restore the verified original")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.package_root, apply=args.apply, rollback=args.rollback)))
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit("minimax-origin-bridge: " + str(exc)) from None


if __name__ == "__main__":
    main()
