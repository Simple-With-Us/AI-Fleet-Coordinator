"""Build one finite client release, offline. Never install or alter the source release."""
import argparse
import hashlib
import json
import os
from pathlib import Path

BASE_COMMIT = "0fac2e8417218ff8791da9d699b66de0770bddca"
FILES = {
    "scripts/simple-git": "7c375cbe1b13c836b382871e05f63a4ceb5590a3d967912ddc000ec42a873a92",
    "scripts/git-tools/git": "8b8cc9d0b1e0b1f58d82d024deaa96edb5fd470f990b321ed3e2051a0c06ff2e",
    "scripts/git-tools/catalog.json": "a92812f6893791957552246167fe6f46baaf198f73f0d5be427f668ce05971ea",
    "scripts/git-tools/export_catalog.py": "727f0e66aee78660c01cb9d36e5861a547fd2d962664e54f1397e9d4d57ceed9",
    "scripts/simple_tools/secretscan.py": "f29d222d0d6af9726b93c268134cf4fe4c84af10a60da9460250aef9892c253c",
}
RESULT_SHA = "7ec709b38325aebe3e97f6f1ef85cfa3b4973527c7ea679f3deaa3155e169414"
BEFORE = '''    if path in (".github", ".github/workflows") or path.startswith(".github/workflows/"):
        die(f"{path!r} is a workflow path;  publish cannot write it", EXIT_USAGE)
'''
AFTER = '''    # Actual workflow files use the same hosted batch route as other files.  The relay
    # checks this seat's workflows:write ceiling and mints that permission only when
    # needed.  Never mistake the directory entries themselves for writable files.
    if path in (".github", ".github/workflows"):
        die(f"{path!r} is a workflow directory, not a writable file", EXIT_USAGE)
'''


def digest(data):
    return hashlib.sha256(data).hexdigest()


def build(source, destination):
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or not source.is_dir():
        raise ValueError("source must be a real immutable release directory")
    payload = {}
    for name, expected in FILES.items():
        path = source / name
        current = path
        while current != source:
            if current.is_symlink():
                raise ValueError("symlink in source path")
            current = current.parent
        data = path.read_bytes()
        if digest(data) != expected:
            raise ValueError("baseline digest mismatch: " + name)
        payload[name] = data
    implementation = "scripts/git-tools/git"
    text = payload[implementation].decode("utf-8")
    if text.count(BEFORE) != 1:
        raise ValueError("baseline admission block mismatch")
    payload[implementation] = text.replace(BEFORE, AFTER).encode("utf-8")
    if digest(payload[implementation]) != RESULT_SHA:
        raise ValueError("candidate implementation digest mismatch")
    # All validation happens before creating output. An interrupted build may leave
    # a partial directory; callers must not activate it or reuse that destination.
    if destination.exists() or destination.is_symlink():
        raise ValueError("destination already exists")
    destination.mkdir(mode=0o700)
    for name, data in payload.items():
        path = destination / name
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with path.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o755 if name in ("scripts/simple-git", implementation) else 0o644)
    manifest = {
        "base_commit": BASE_COMMIT,
        "production_delta": "_publish_path_check only",
        "activation_performed": False,
        "files": {name: digest(data) for name, data in payload.items()},
    }
    (destination / "workflow-release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.destination), indent=2))
