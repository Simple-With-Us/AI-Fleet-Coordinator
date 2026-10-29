#!/usr/bin/env python3
"""Read-only Linux destination-volume probe. Run ON the storage host over a verified route.

Does not inspect a remote URL or prove which host it names. Endpoint/collection
binding must be qualified by the operator against the service's actual mounts.
"""

import argparse
import json
import os
import pathlib
import re
import time


def observe(endpoint, collection, storage_path, mount, device):
    path = pathlib.Path(storage_path).resolve(strict=True)
    root = pathlib.Path(mount).resolve(strict=True)
    path.relative_to(root)
    # Linux mountinfo escapes whitespace and backslashes with octal sequences.
    unescape = lambda s: re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), s)
    matches = []
    for line in pathlib.Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields = left.split()
        dest = pathlib.Path(unescape(fields[4]))
        if path == dest or dest in path.parents:
            matches.append((len(dest.parts), dest, unescape(right.split()[1])))
    _, actual_mount, actual_device = max(matches)
    if actual_mount != root or actual_device != device or path.stat().st_dev != root.stat().st_dev:
        raise ValueError("storage path is not on the configured destination volume")
    stat = os.statvfs(path)
    return dict(
        endpoint=endpoint,
        collection=collection,
        storage_path=str(path),
        mount=str(root),
        device=actual_device,
        observed_at=time.time(),
        available_bytes=stat.f_bavail * stat.f_frsize,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("endpoint", "collection", "storage-path", "mount", "device"):
        parser.add_argument("--" + name, required=True)
    args = parser.parse_args()
    try:
        print(
            json.dumps(
                observe(args.endpoint, args.collection, args.storage_path, args.mount, args.device)
            )
        )
    except (OSError, ValueError):
        parser.exit(2, "destination volume could not be verified\n")
