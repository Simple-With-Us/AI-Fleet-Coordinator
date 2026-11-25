"""Unpack pinned offline test/schema dependencies, no network or runtime activation."""

import hashlib
import json
import tarfile
from pathlib import Path

root = Path(__file__).resolve().parent
archive = root / "candidate-dependencies.tar.gz"
expected = json.loads((root / "dependencies-sha256.json").read_text())["sha256"]
if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
    raise ValueError("dependency-hash")
for name in ("offline_node_modules", "vendor"):
    if (root / name).exists() or (root / name).is_symlink():
        raise ValueError("fresh-private-preparation-required")
with tarfile.open(archive) as tar:
    for item in tar.getmembers():
        path = Path(item.name)
        if (
            item.issym()
            or item.islnk()
            or path.is_absolute()
            or ".." in path.parts
            or path.parts[0] not in ("offline_node_modules", "vendor")
            or not (item.isfile() or item.isdir())
        ):
            raise ValueError("unsafe-dependency-member")
    tar.extractall(root)
print("Pinned offline dependencies ready. No route installed or activated.")
