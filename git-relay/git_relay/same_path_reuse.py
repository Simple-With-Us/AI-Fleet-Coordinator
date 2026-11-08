"""Metadata proof of exact same-path object reuse in one repository.

A blob SHA alone is not provenance. The immutable source commit, containing
nonrecursive trees, original path, object type, mode, size and blob SHA are
all checked here. The workflow then fetches and verifies the blob bytes,
runs the full scanner and enforces loaded/provider guards before tree creation.
This does not approve synthesized content, raw uploads or relocation.
"""

from __future__ import annotations

import re

_SHA = re.compile(r"^[0-9a-f]{40}$")

# Proposal only. No caller approval flag and no active source records.
# (repository, exact source commit, original/destination path, blob SHA, mode).
REVIEWED_SAME_PATH_SOURCES: frozenset[tuple[str, str, str, str, str]] = frozenset()


def _sha(value):
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def verify_same_path(get, repo, commit, path, blob, mode, max_bytes, cache):
    if not _sha(commit) or not _sha(blob) or mode not in ("100644", "100755"):
        raise ValueError("invalid object provenance")
    if (repo, commit, path, blob, mode) not in REVIEWED_SAME_PATH_SOURCES:
        raise ValueError("unreviewed source object")
    parts = path.split("/")
    if len(parts) > 8 or any(not p or p in (".", "..") for p in parts):
        raise ValueError("invalid source path")
    key = ("commit", repo, commit)
    if key not in cache:
        value = get(f"/repos/{repo}/git/commits/{commit}")
        if (
            not isinstance(value, dict)
            or value.get("sha") != commit
            or not isinstance(value.get("tree"), dict)
            or not _sha(value["tree"].get("sha"))
        ):
            raise ValueError("unverifiable source commit")
        cache[key] = value["tree"]["sha"]
    tree = cache[key]
    for index, part in enumerate(parts):
        key = ("tree", repo, tree)
        if key not in cache:
            value = get(f"/repos/{repo}/git/trees/{tree}")
            if (
                not isinstance(value, dict)
                or value.get("sha") != tree
                or value.get("truncated") is not False
                or not isinstance(value.get("tree"), list)
                or len(value["tree"]) > 100
            ):
                raise ValueError("unverifiable source tree")
            entries = {}
            for entry in value["tree"]:
                if (
                    not isinstance(entry, dict)
                    or not isinstance(entry.get("path"), str)
                    or entry["path"] in entries
                    or "/" in entry["path"]
                    or entry["path"] in ("", ".", "..")
                ):
                    raise ValueError("ambiguous source tree")
                entries[entry["path"]] = entry
            cache[key] = entries
        entry = cache[key].get(part)
        if not isinstance(entry, dict):
            raise ValueError("source path absent")
        if index == len(parts) - 1:
            if (
                entry.get("type") != "blob"
                or entry.get("sha") != blob
                or entry.get("mode") != mode
                or type(entry.get("size")) is not int
                or not 0 <= entry["size"] <= max_bytes
            ):
                raise ValueError("source blob proof mismatch")
            return entry["size"]
        if (
            entry.get("type") != "tree"
            or entry.get("mode") != "040000"
            or not _sha(entry.get("sha"))
        ):
            raise ValueError("source ancestor is not a tree")
        tree = entry["sha"]
    raise ValueError("source blob absent")
