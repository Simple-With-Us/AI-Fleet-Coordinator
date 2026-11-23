"""Default-off, fixed-shape catalog observation for the accepted MiniMax 0.7.4 bridge.

No tool exposure, waiting, selector mutation, calls, or ownership changes. The
publication record describes a snapshot, not any current turn. Capture describes
only the snapshot being returned to a new turn, not its final filtered tool plan.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from simple_tools import minimax_origin_bridge as bridge

BASE_SHA256 = "1dec7314fc7a9f6c87369550a0cc1d0d91a84e7bbf750df135fc67dfcb91ae62"
BACKUP_SUFFIX = ".afc-mm-catalog-0.7.4.bridge"
HELPER_ANCHOR = "function tIe(t){"
CAPTURE_OLD = (
    "captureTurnCapabilities(){return this.assertUsable(),tIe(this.snapshot.turnCapabilities)}"
)
CAPTURE_NEW = 'captureTurnCapabilities(){return this.assertUsable(),afcMmCatalogShape("capture",this.snapshot),tIe(this.snapshot.turnCapabilities)}'
PUBLISH_OLD = "replaceSnapshot(e){let r=this.snapshot;this.snapshot=e,this.desiredSnapshot=e;"
PUBLISH_NEW = 'replaceSnapshot(e){let r=this.snapshot;this.snapshot=e,this.desiredSnapshot=e;afcMmCatalogShape("publication",e);'
HELPER = """function afcMmCatalogShape(phase,snapshot){try{if(process.env.AFC_MM_CATALOG_SHAPE!=="1")return;const servers=snapshot.mcpServers.filter(x=>x.pluginName==="fleet-own-tools"&&x.server.name==="simple-tools"),bindings=snapshot.turnCapabilities.runtimeToolBindings.filter(x=>x.kind==="mcp"&&x.pluginName==="fleet-own-tools"&&x.source==="simple-tools"&&x.mcpToolName==="identities"),diags=snapshot.diagnostics.filter(x=>x.pluginName==="fleet-own-tools"&&x.capabilityName==="simple-tools"),warming=diags.some(x=>x.code==="MCP_SERVER_WARMING"),failed=diags.some(x=>x.code==="MCP_SERVER_LIST_FAILED"),match=/^plugin-snapshot-([0-9]{1,9})$/.exec(snapshot.revision);process.stderr.write(JSON.stringify({event:"afc_mm_catalog_shape",version:1,phase,snapshot_sequence:match?Number(match[1]):null,total_runtime_tools:snapshot.turnCapabilities.runtimeTools.length,fleet_server_count:servers.length,identities_binding_count:bindings.length,warming,failed,catalog_ready:servers.length===1&&bindings.length===1&&!warming&&!failed})+"\\n")}catch{}}"""


def patched(data: bytes) -> bytes:
    if bridge.digest(data) != BASE_SHA256:
        raise ValueError("unsupported MiniMax catalog base hash")
    text = data.decode("utf-8")
    if "afcMmCatalogShape" in text:
        raise ValueError("catalog diagnostic name collision")
    for old, new in (
        (HELPER_ANCHOR, HELPER + HELPER_ANCHOR),
        (CAPTURE_OLD, CAPTURE_NEW),
        (PUBLISH_OLD, PUBLISH_NEW),
    ):
        if text.count(old) != 1:
            raise ValueError("MiniMax catalog anchor is not unique")
        text = text.replace(old, new, 1)
    return text.encode("utf-8")


def run(root: Path, *, apply: bool = False, rollback: bool = False) -> dict:
    if root.absolute() != root.resolve():
        raise ValueError("name the real versioned package, not a symlink")
    for folder in (root, root / "chunks"):
        info = folder.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
            raise ValueError("unsafe MiniMax package directory")
    info = (root / "chunks").lstat()
    identity = (info.st_dev, info.st_ino)
    package, _ = bridge.private_read(root / "package.json")
    package = json.loads(package)
    if package.get("name") != "@minimax-ai/code" or package.get("version") != "0.7.4":
        raise ValueError("unsupported MiniMax package version")
    path = root / bridge.CHUNK
    current, mode = bridge.private_read(path)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if bridge.digest(current) == BASE_SHA256:
        base = current
    else:
        base, _ = bridge.private_read(backup)
        if current != patched(base):
            raise ValueError("MiniMax catalog chunk drifted")
    result = base if rollback else patched(base)
    if apply and current != result:
        if not rollback:
            try:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                fd = None
            if fd is not None:
                with os.fdopen(fd, "wb") as target:
                    target.write(base)
                    target.flush()
                    os.fsync(target.fileno())
            fd = os.open(backup, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                info = os.fstat(fd)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.geteuid()
                    or info.st_nlink != 1
                    or info.st_mode & 0o022
                ):
                    raise ValueError("unsafe MiniMax catalog backup")
                with os.fdopen(fd, "rb", closefd=False) as source:
                    saved = source.read(len(base) + 1)
                if saved != base:
                    raise ValueError("MiniMax catalog backup mismatch")
                os.fsync(fd)
            finally:
                os.close(fd)
            bridge.sync_directory(path.parent, identity)
        latest, _ = bridge.private_read(path)
        if latest != current:
            raise ValueError("MiniMax catalog chunk changed before replacement")
        bridge.replace(path, result, mode)
    return {
        "state": ("restored" if rollback else "installed") if apply else "compatible",
        "sha256": bridge.digest(result),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package_root", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(run(args.package_root, apply=args.apply, rollback=args.rollback)))
    except (OSError, ValueError, TypeError) as exc:
        raise SystemExit("minimax-catalog-observation: " + str(exc)) from None


if __name__ == "__main__":
    main()
