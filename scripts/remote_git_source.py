"""Bounded, offline-verifiable Git relay snapshots for document collection."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

PROVIDER = "simple-tools-git-relay-v1"
MAX_DESCRIPTOR_BYTES = 24 * 1024 * 1024
MAX_METADATA_BYTES = 8 * 1024 * 1024
MAX_BLOB_BYTES = 512 * 1024
MAX_COMPACT_BASE64_CHARS = 4 * ((MAX_BLOB_BYTES + 2) // 3)
MAX_RAW_BASE64_CHARS = MAX_COMPACT_BASE64_CHARS + (MAX_COMPACT_BASE64_CHARS + 59) // 60
MAX_SELECTED_BYTES = 16 * 1024 * 1024
MAX_SELECTED_BLOBS = 2048
MAX_TREES = 1024
MAX_ENTRIES = 20000
MAX_DEPTH = 64
MAX_PATH_BYTES = 4096
MAX_API_CALLS = 4096
OID = re.compile(r"[0-9a-f]{40}\Z")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
IDENTITY_KEYS = {
    "owner_repo",
    "repository_id",
    "visibility",
    "default_branch",
    "source_ref",
    "source_commit",
    "root_tree",
}


class SourceError(ValueError):
    """An invalid or incomplete source that must not reach a collection write."""


def canonical(value: Any) -> bytes:
    return (
        json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n"
    ).encode("ascii")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def object_id(kind: str, content: bytes) -> str:
    return hashlib.sha1(
        kind.encode("ascii") + b" " + str(len(content)).encode("ascii") + b"\0" + content
    ).hexdigest()


def _oid(value: Any) -> str:
    if not isinstance(value, str) or OID.fullmatch(value) is None:
        raise SourceError("Remote source contains an invalid SHA-1 object id")
    return value


def _text(value: Any, maximum: int) -> str:
    if not isinstance(value, str):
        raise SourceError("Remote source text is missing")
    try:
        encoded = value.encode("utf-8", "strict")
    except UnicodeError as exc:
        raise SourceError("Remote source text is not lossless UTF-8") from exc
    if not encoded or len(encoded) > maximum or "\0" in value:
        raise SourceError("Remote source text exceeds its bounds")
    return value


def verify_commit(commit: str, proof: dict[str, Any]) -> str:
    """Rebuild GitHub's signed commit without trimming its signature newline."""
    _oid(commit)
    if not isinstance(proof, dict) or set(proof) != {"verified", "reason", "payload", "signature"}:
        raise SourceError("Remote commit proof is missing or unsupported")
    if proof["verified"] is not True or proof["reason"] != "valid":
        raise SourceError("Remote commit has no provider-verified signature proof")
    payload = _text(proof["payload"], 65536)
    signature = _text(proof["signature"], 32768)
    if "\r" in payload or "\r" in signature or "\n\n" not in payload:
        raise SourceError("Remote commit proof has unsupported line encoding")
    if not signature.startswith("-----BEGIN PGP SIGNATURE-----\n") or not signature.endswith(
        "\n-----END PGP SIGNATURE-----\n"
    ):
        raise SourceError("Remote commit signature format is unsupported")
    headers, message = payload.split("\n\n", 1)
    lines = headers.split("\n")
    if not lines or not re.fullmatch(r"tree [0-9a-f]{40}", lines[0]):
        raise SourceError("Remote commit tree binding is missing")
    index = 1
    while index < len(lines) and re.fullmatch(r"parent [0-9a-f]{40}", lines[index]):
        index += 1
    if (
        len(lines) != index + 2
        or not lines[index].startswith("author ")
        or not lines[index + 1].startswith("committer ")
    ):
        raise SourceError("Remote commit headers are unsupported")
    raw = (headers + "\ngpgsig " + signature.replace("\n", "\n ") + "\n\n" + message).encode(
        "utf-8"
    )
    if object_id("commit", raw) != commit:
        raise SourceError("Remote signed commit object id does not match")
    return lines[0][5:]


def verify_tree(tree_id: str, entries: Any) -> list[dict[str, Any]]:
    _oid(tree_id)
    if not isinstance(entries, list) or len(entries) > 100:
        raise SourceError("Remote nonrecursive tree exceeds its entry limit")
    names: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in entries:
        if not isinstance(item, dict) or set(item) != {"path", "mode", "type", "sha"}:
            raise SourceError("Remote tree entry shape is unsupported")
        name = _text(item["path"], 1024)
        if (
            name in {".", ".."}
            or "/" in name
            or "\\" in name
            or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or name in names
        ):
            raise SourceError("Remote tree has a duplicate or unsafe entry name")
        names.add(name)
        mode = "40000" if item["mode"] == "040000" else item["mode"]
        kind = {
            "40000": "tree",
            "100644": "blob",
            "100755": "blob",
            "120000": "blob",
            "160000": "commit",
        }.get(mode)
        if kind is None or item["type"] != kind:
            raise SourceError("Remote tree has an unsupported mode or object type")
        normalized.append({"path": name, "mode": mode, "type": kind, "sha": _oid(item["sha"])})
    normalized.sort(
        key=lambda x: x["path"].encode("utf-8") + (b"/" if x["type"] == "tree" else b"\0")
    )
    raw = b"".join(
        x["mode"].encode("ascii")
        + b" "
        + x["path"].encode("utf-8")
        + b"\0"
        + bytes.fromhex(x["sha"])
        for x in normalized
    )
    if object_id("tree", raw) != tree_id:
        raise SourceError("Remote tree object id does not match its entries")
    return normalized


def tracked_entries(root: str, trees: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if (
        not isinstance(trees, dict)
        or not 1 <= len(trees) <= MAX_TREES
        or len(canonical(trees)) > MAX_METADATA_BYTES
    ):
        raise SourceError("Remote tree metadata exceeds its bounds")
    verified = {key: verify_tree(key, value) for key, value in trees.items()}
    used: set[str] = set()
    result: dict[str, dict[str, Any]] = {}

    def walk(tree_id: str, prefix: str, depth: int, ancestors: set[str]) -> None:
        if depth > MAX_DEPTH or tree_id in ancestors or tree_id not in verified:
            raise SourceError("Remote tree is incomplete, cyclic or too deep")
        used.add(tree_id)
        for item in verified[tree_id]:
            path = prefix + item["path"]
            if len(path.encode("utf-8")) > MAX_PATH_BYTES or path in result:
                raise SourceError("Remote tracked path exceeds its bounds or is duplicated")
            if item["type"] == "tree":
                walk(item["sha"], path + "/", depth + 1, ancestors | {tree_id})
            else:
                result[path] = {
                    "path": path,
                    "mode": item["mode"],
                    "object_type": item["type"],
                    "object_id": item["sha"],
                    "size_bytes": None,
                }
                if len(result) > MAX_ENTRIES:
                    raise SourceError("Remote tracked file count exceeds its bound")

    walk(_oid(root), "", 0, set())
    if used != set(trees):
        raise SourceError("Remote descriptor contains unbound tree metadata")
    return result


def verify_identity(identity: Any) -> dict[str, Any]:
    if not isinstance(identity, dict) or set(identity) != IDENTITY_KEYS:
        raise SourceError("Remote repository identity is incomplete")
    name = _text(identity["owner_repo"], 255)
    if REPOSITORY.fullmatch(name) is None or any(p in {".", ".."} for p in name.split("/")):
        raise SourceError("Remote repository identity is invalid")
    if (
        type(identity["repository_id"]) is not int
        or identity["repository_id"] <= 0
        or identity["visibility"] not in {"public", "private"}
    ):
        raise SourceError("Remote repository id or visibility is invalid")
    branch = _text(identity["default_branch"], 255)
    if (
        any(ord(c) < 33 or c in "\\~^:?*[" for c in branch)
        or ".." in branch
        or "@{" in branch
        or branch.startswith("/")
        or branch.endswith(("/", ".", ".lock"))
        or "//" in branch
    ):
        raise SourceError("Remote default branch is invalid")
    if identity["source_ref"] != "refs/heads/" + branch:
        raise SourceError("Remote source ref does not match the default branch")
    _oid(identity["source_commit"])
    _oid(identity["root_tree"])
    return dict(identity)


def verify_blob(response: Any, entry: dict[str, Any]) -> bytes:
    if not isinstance(response, dict) or set(response) != {
        "path",
        "sha",
        "size",
        "encoding",
        "content",
    }:
        raise SourceError("Remote selected blob response is missing or unsupported")
    size = response["size"]
    if (
        type(size) is not int
        or not 0 <= size <= MAX_BLOB_BYTES
        or response["encoding"] != "base64"
        or not isinstance(response["content"], str)
    ):
        declared = str(size) if type(size) is int else "unknown"
        raise SourceError(
            f"Remote selected blob {entry['path']!r} is missing, oversized or unsupported (declared bytes: {declared}; limit: {MAX_BLOB_BYTES})"
        )
    if (
        response["path"] != entry["path"]
        or response["sha"] != entry["object_id"]
        or entry["mode"] not in {"100644", "100755"}
        or entry["object_type"] != "blob"
    ):
        raise SourceError("Remote selected blob does not match its verified tree path")
    content = response["content"]
    if len(content) > MAX_RAW_BASE64_CHARS:
        raise SourceError("Remote selected blob raw encoding exceeds its bound")
    compact = content.replace("\n", "")
    if len(compact) > MAX_COMPACT_BASE64_CHARS:
        raise SourceError("Remote selected blob compact encoding exceeds its bound")
    try:
        raw = base64.b64decode(compact, validate=True)
    except (ValueError, UnicodeError) as exc:
        raise SourceError("Remote selected blob encoding is invalid") from exc
    if len(raw) != size or object_id("blob", raw) != entry["object_id"]:
        raise SourceError("Remote selected blob length or object id does not match")
    return raw


class Snapshot:
    def __init__(
        self,
        identity: dict[str, Any],
        proof: dict[str, Any],
        trees: dict[str, Any],
        reader: Callable[[str], dict[str, Any]] | None = None,
    ):
        self.identity = verify_identity(identity)
        if verify_commit(identity["source_commit"], proof) != identity["root_tree"]:
            raise SourceError("Remote commit and root tree disagree")
        self.entries = tracked_entries(identity["root_tree"], trees)
        self.proof, self.trees, self.reader = proof, trees, reader
        self.responses: dict[str, dict[str, Any]] = {}
        self.contents: dict[str, bytes] = {}
        self.descriptor_sha256: str | None = None
        self.receipt_sha256: str | None = None

    def read_blob(self, path: str) -> bytes:
        if path not in self.contents:
            if path not in self.entries or self.reader is None:
                raise SourceError("Remote descriptor is missing selected blob content")
            response = self.reader(path)
            raw = verify_blob(response, self.entries[path])
            if (
                len(self.contents) >= MAX_SELECTED_BLOBS
                or sum(map(len, self.contents.values())) + len(raw) > MAX_SELECTED_BYTES
            ):
                raise SourceError("Remote selected content exceeds its total bound")
            self.responses[path] = response
            self.contents[path] = raw
            self.entries[path]["size_bytes"] = len(raw)
        return self.contents[path]

    def require_selected(self, paths: set[str]) -> None:
        if paths != set(self.responses):
            raise SourceError("Remote descriptor has extra or missing policy-selected content")

    def provenance(self) -> dict[str, Any]:
        return {
            **self.identity,
            "source_transport": "git-relay",
            "descriptor_sha256": self.descriptor_sha256,
            "receipt_sha256": self.receipt_sha256,
        }


def _no_duplicates(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise SourceError("Remote descriptor JSON contains duplicate keys")
        result[key] = value
    return result


def _read_private_json(path: Path, maximum: int) -> dict[str, Any]:
    try:
        parent = path.parent.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700
        ):
            raise SourceError("Remote source directory must be owner-owned mode 0700")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_size > maximum
            ):
                raise SourceError("Remote source file must be bounded, owner-owned mode 0600")
            raw = handle.read(maximum + 1)
        if len(raw) > maximum:
            raise SourceError("Remote source file exceeds its byte bound")
        value = json.loads(raw, object_pairs_hook=_no_duplicates)
        if not isinstance(value, dict):
            raise SourceError("Remote source file must contain a JSON object")
        return value
    except (OSError, ValueError, RecursionError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError("Remote source file cannot be safely read as JSON") from exc


def load_descriptor(path: Path) -> Snapshot:
    descriptor = _read_private_json(path, MAX_DESCRIPTOR_BYTES)
    receipt = _read_private_json(path.with_name(path.name + ".receipt.json"), 8192)
    if (
        set(descriptor)
        != {
            "schema_version",
            "provider",
            "object_format",
            "identity",
            "commit_proof",
            "trees",
            "selected_blobs",
        }
        or type(descriptor["schema_version"]) is not int
        or descriptor["schema_version"] != 1
        or descriptor["provider"] != PROVIDER
        or descriptor["object_format"] != "sha1"
    ):
        raise SourceError("Remote descriptor schema or provider is unsupported")
    if (
        set(receipt) != {"schema_version", "provider", "identity", "descriptor_sha256"}
        or type(receipt["schema_version"]) is not int
        or receipt["schema_version"] != 1
        or receipt["provider"] != PROVIDER
        or receipt["identity"] != descriptor["identity"]
        or receipt["descriptor_sha256"] != digest(descriptor)
    ):
        raise SourceError("Remote provider receipt does not bind this descriptor")
    snapshot = Snapshot(descriptor["identity"], descriptor["commit_proof"], descriptor["trees"])
    responses = descriptor["selected_blobs"]
    if not isinstance(responses, dict) or len(responses) > MAX_SELECTED_BLOBS:
        raise SourceError("Remote selected blob set exceeds its count bound")
    snapshot.reader = lambda relative: responses[relative]
    for relative in responses:
        if relative not in snapshot.entries:
            raise SourceError("Remote selected path is not bound to the commit tree")
        snapshot.read_blob(relative)
    snapshot.reader = None
    snapshot.descriptor_sha256 = digest(descriptor)
    snapshot.receipt_sha256 = digest(receipt)
    return snapshot


def export_snapshot(
    call: Callable[[str, dict[str, Any]], dict[str, Any]],
    owner_repo: str,
    expected_visibility: str,
    expected_commit: str,
) -> Snapshot:
    """Only metadata and policy-selected blob reads are permitted by this adapter."""
    calls = 0

    def read(tool: str, args: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls > MAX_API_CALLS:
            raise SourceError("Remote source API call bound was exceeded")
        return call(tool, {"repo": owner_repo, **args})

    repo = read("gr_wf_repo_get", {})["repository"]
    if (
        repo.get("full_name") != owner_repo
        or repo.get("name") != owner_repo.split("/")[-1]
        or repo.get("visibility") != expected_visibility
        or repo.get("private") is not (expected_visibility == "private")
    ):
        raise SourceError("Provider repository identity or visibility does not match the request")
    branch = read("gr_wf_branch_get", {"branch": repo["default_branch"]})["branch"]
    commit = branch["commit"]
    if branch["name"] != repo["default_branch"] or commit["sha"] != expected_commit:
        raise SourceError("Provider default branch does not match the pinned source commit")
    body = commit["commit"]
    verification = body["verification"]
    proof = {key: verification.get(key) for key in ("verified", "reason", "payload", "signature")}
    root = verify_commit(expected_commit, proof)
    if body["tree"]["sha"] != root:
        raise SourceError("Provider commit tree does not match its signed proof")
    identity = verify_identity(
        {
            "owner_repo": owner_repo,
            "repository_id": repo["id"],
            "visibility": expected_visibility,
            "default_branch": repo["default_branch"],
            "source_ref": "refs/heads/" + repo["default_branch"],
            "source_commit": expected_commit,
            "root_tree": root,
        }
    )
    trees: dict[str, Any] = {}
    metadata_bytes = len(canonical({}))
    pending = [(root, 0)]
    while pending:
        tree_id, depth = pending.pop()
        if depth > MAX_DEPTH:
            raise SourceError("Remote source tree depth exceeds its bound")
        if tree_id in trees:
            continue
        if len(trees) >= MAX_TREES:
            raise SourceError("Remote source tree count exceeds its bound")
        response = read("gr_wf_git_tree_get", {"sha": tree_id})["tree"]
        if response.get("sha") != tree_id or response.get("truncated") is not False:
            raise SourceError("Provider tree is missing, truncated or rebound")
        entries = verify_tree(tree_id, response.get("entries"))
        # Each member excludes the empty object's braces/newline and adds its comma.
        metadata_bytes += len(canonical({tree_id: entries})) - 3 + bool(trees)
        if metadata_bytes > MAX_METADATA_BYTES:
            raise SourceError("Remote source tree metadata exceeds its byte bound")
        trees[tree_id] = entries
        pending.extend((item["sha"], depth + 1) for item in entries if item["type"] == "tree")
    return Snapshot(
        identity,
        proof,
        trees,
        lambda path: read("gr_wf_file_get", {"path": path, "ref": expected_commit}),
    )


def write_descriptor(snapshot: Snapshot, path: Path) -> None:
    descriptor = {
        "schema_version": 1,
        "provider": PROVIDER,
        "object_format": "sha1",
        "identity": snapshot.identity,
        "commit_proof": snapshot.proof,
        "trees": snapshot.trees,
        "selected_blobs": snapshot.responses,
    }
    receipt = {
        "schema_version": 1,
        "provider": PROVIDER,
        "identity": snapshot.identity,
        "descriptor_sha256": digest(descriptor),
    }
    raw = canonical(descriptor)
    if len(raw) > MAX_DESCRIPTOR_BYTES:
        raise SourceError("Remote descriptor exceeds its byte bound")
    path = Path(os.path.abspath(path))
    try:
        path.parent.mkdir(mode=0o700, parents=False)
    except FileExistsError:
        pass
    except OSError as exc:
        raise SourceError("Remote export directory could not be created") from exc
    else:
        path.parent.chmod(0o700)
    info = path.parent.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise SourceError("Remote export directory must be owner-owned mode 0700")
    created: list[Path] = []
    try:
        for target, content in (
            (path.with_name(path.name + ".receipt.json"), canonical(receipt)),
            (path, raw),
        ):
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            created.append(target)
            with os.fdopen(fd, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        for target in reversed(created):
            target.unlink()
        raise SourceError("Remote export could not create a new private descriptor pair") from exc
