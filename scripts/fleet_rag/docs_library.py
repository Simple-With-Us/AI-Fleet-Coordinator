"""Read-only fleet repository discovery and safe document-library provenance.

Generated manifests are untrusted hints, never authority to read another path.
Only exact bytes of a current tracked Git blob can suppress a library copy.
"""

from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import resource
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath

MAX_BYTES = 1024 * 1024
MAX_PDF_OUTPUT_BYTES = 400 * 1024


class DocumentTooLarge(ValueError):
    """A bounded content exclusion, independent of collection integrity."""


APPROVED_OWNERS = frozenset({"simple-with-us", "jaywedgeworth22"})
TEXT_SUFFIXES = frozenset({".md", ".txt", ".html", ".htm"})
HUMAN_SUFFIXES = TEXT_SUFFIXES | {".pdf"}
MAX_PDF_BYTES = 4 * 1024 * 1024
MAX_HTML_BYTES = 4 * 1024 * 1024


def input_cap(suffix):
    suffix = suffix.casefold()
    if suffix == ".pdf":
        return MAX_PDF_BYTES
    if suffix in (".html", ".htm"):
        return MAX_HTML_BYTES
    return MAX_BYTES


BLOCKED = frozenset(
    {
        ".git",
        ".secrets",
        "secrets",
        "credentials",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "sessions",
        "transcripts",
    }
)


class GitInventoryError(RuntimeError):
    """The source cannot establish a committed inventory; no deletion is safe."""


def relative_path(value):
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        raise ValueError("invalid-document-relative-path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError("invalid-document-relative-path")
    if any(p.casefold() in BLOCKED for p in path.parts):
        raise ValueError("excluded-document-path")
    return path


def safe_file(root, relative, maximum=MAX_BYTES):
    root = Path(root).absolute()
    if root.is_symlink():
        raise ValueError("document-root-symlink-refused")
    root = root.parent.resolve(strict=True) / root.name
    parts = relative_path(relative).parts
    directory_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)
    with os.fdopen(fd, "rb") as stream:
        before = os.fstat(stream.fileno())
        if before.st_size > maximum and stat.S_ISREG(before.st_mode):
            raise DocumentTooLarge("document-input-too-large")
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("document-type-or-size-refused")
        data = stream.read(maximum + 1)
        after = os.fstat(stream.fileno())
    if len(data) > maximum or (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("document-changed-or-too-large")
    return data


def git(root, *args):
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_NAMESPACE",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_PARAMETERS",
        }
        and not key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))
    }
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_NO_LAZY_FETCH="1")
    result = subprocess.run(
        ["git", "--no-replace-objects", "-C", str(root), *args],
        capture_output=True,
        timeout=30,
        env=env,
    )
    return result.stdout if result.returncode == 0 else None


@functools.lru_cache(maxsize=64)
def _tracked_tree(root, commit):
    listing = git(root, "ls-tree", "-r", "-l", "-z", commit)
    if listing is None:
        raise GitInventoryError("tracked-git-inventory-unavailable")
    result = {}
    for record in (listing or b"").split(b"\0"):
        if not record:
            continue
        try:
            metadata, name = record.split(b"\t", 1)
            mode, kind, oid, size = metadata.split()
        except ValueError:
            raise GitInventoryError("tracked-git-inventory-malformed") from None
        relative = name.decode("utf-8")
        if (
            kind == b"blob"
            and mode in (b"100644", b"100755")
            and (not size.isdigit() or not re.fullmatch(rb"[0-9a-f]{40,64}", oid))
        ):
            raise GitInventoryError("tracked-git-inventory-malformed")
        cap = input_cap(Path(relative).suffix)
        if (
            mode in (b"100644", b"100755")
            and kind == b"blob"
            and size.isdigit()
            and int(size) <= cap
        ):
            result[relative] = oid.decode()
    return result


@dataclass(frozen=True)
class Repository:
    root: Path
    owner: str
    name: str
    commit: str

    def blob(self, relative):
        path = relative_path(relative).as_posix()
        oid = _tracked_tree(self.root, self.commit).get(path)
        if oid is None:
            return None
        data = git(self.root, "cat-file", "-p", oid)
        if data is None:
            raise GitInventoryError("tracked-git-blob-unavailable")
        return data


def discover_repositories(code_root):
    """Discover direct children by exact approved GitHub origin, not a stale name list."""
    root = Path(code_root)
    if not root.is_dir():
        return []
    repos = []
    for candidate in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if candidate.is_symlink() or not candidate.is_dir() or not (candidate / ".git").exists():
            continue
        origin = git(candidate, "remote", "get-url", "origin")
        if origin is None:
            raise GitInventoryError("repository-origin-unavailable")
        match = re.fullmatch(
            r"(?:https://github\.com/|git@github\.com:)([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?",
            origin.decode().strip(),
        )
        if not match or match[1].casefold() not in APPROVED_OWNERS or ".." in match[2]:
            continue
        top = git(candidate, "rev-parse", "--show-toplevel")
        if top is None or Path(top.decode().strip()).resolve() != candidate.resolve():
            raise GitInventoryError("repository-topology-unavailable")
        commit = git(candidate, "rev-parse", "--verify", "HEAD^{commit}")
        if commit is not None and re.fullmatch(rb"[0-9a-f]{40,64}\n?", commit):
            name = "Fleet-OPS" if match[2].casefold() == "fleet-ops" else match[2]
            repos.append(
                Repository(candidate.resolve(), "Simple-With-Us", name, commit.decode().strip())
            )
        else:
            raise GitInventoryError("repository-commit-unavailable")
    return repos


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _pdf_text(data):
    executable = shutil.which("pdftotext")
    if executable is None and os.access("/opt/homebrew/bin/pdftotext", os.X_OK):
        executable = "/opt/homebrew/bin/pdftotext"
    if executable is None:
        raise ValueError("pdf-extractor-unavailable")
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("pdf-input-too-large")

    def limits():
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_PDF_OUTPUT_BYTES, MAX_PDF_OUTPUT_BYTES))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 10))

    with tempfile.TemporaryDirectory(prefix="fleet-rag-pdf-") as temporary:
        source = Path(temporary) / "document.pdf"
        source.write_bytes(data)
        source.chmod(0o600)
        with tempfile.TemporaryFile() as output:
            try:
                result = subprocess.run(
                    [executable, "-nopgbrk", "-enc", "UTF-8", str(source), "-"],
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.DEVNULL,
                    env={"HOME": temporary, "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
                    timeout=20,
                    preexec_fn=limits,
                )
            except (OSError, subprocess.TimeoutExpired):
                raise ValueError("pdf-extraction-failed") from None
            if result.returncode != 0:
                raise ValueError("pdf-extraction-failed")
            output.seek(0)
            extracted = output.read(MAX_PDF_OUTPUT_BYTES + 1)
    if not extracted.strip() or len(extracted) > MAX_PDF_OUTPUT_BYTES:
        raise ValueError("pdf-output-empty-or-too-large")
    return extracted.decode("utf-8")


def text_from_bytes(data, suffix):
    if suffix.casefold() == ".pdf":
        return _pdf_text(data)
    if b"\x00" in data:
        raise ValueError("binary-document-refused")
    text = data.decode("utf-8")
    if suffix.casefold() in (".html", ".htm"):
        if len(data) > MAX_HTML_BYTES:
            raise DocumentTooLarge("html-input-too-large")
        parser = _HTMLText()
        parser.feed(text)
        text = "".join(parser.parts)
        if len(text.encode("utf-8")) > MAX_BYTES:
            raise DocumentTooLarge("html-visible-text-too-large")
    if (
        any(len(line) >= 8000 for line in text.splitlines())
        and text
        and sum(c.isspace() for c in text) / len(text) < 0.05
    ):
        raise ValueError("minified-document-refused")
    return text


def library_documents(
    root, repositories, canonical, make_doc, excluded, warn, known_library_hashes=None
):
    """Yield unique legacy documents; attach exact copies to canonical document aliases.

    ``canonical`` maps a canonical document id to (Doc, exact tracked blob bytes).
    Generated reports/catalogs are never treated as primary corpus documents.
    """
    root = Path(root)
    if root.is_symlink():
        warn("doc: library root symlink refused")
        return
    if not root.is_dir():
        return
    root = root.resolve()
    repo_by_name = {repo.name.casefold(): repo for repo in repositories}
    hashes = {}
    for doc, raw in canonical.values():
        hashes.setdefault(hashlib.sha256(raw).hexdigest(), doc)
    shared = {} if known_library_hashes is None else known_library_hashes
    hashes.update(shared)
    generated = []
    if (root / "Generated").is_dir() and not (root / "Generated").is_symlink():
        generated.append(root / "Generated")
    # On the server the generated collections live directly under Docs.
    generated_collections = set()
    for directory in (root, *generated):
        for child in sorted(directory.iterdir()):
            if child.is_symlink() or not child.is_dir():
                continue
            try:
                raw = safe_file(child, "manifest.json", maximum=8 * 1024 * 1024)
                manifest = json.loads(raw)
                if not isinstance(manifest, dict):
                    raise ValueError("invalid-generated-manifest")
                if manifest.get("generated_by") != "collect-repo-documents.py" or manifest.get(
                    "schema_version"
                ) not in (1, 2):
                    continue
                # A failed generated collection is quarantined, not walked as legacy reports.
                generated_collections.add(child)
                if manifest.get("repo") != child.name or not re.fullmatch(
                    r"[0-9a-f]{40,64}", manifest.get("source_commit", "")
                ):
                    raise ValueError("invalid-generated-manifest")
                owner, name = manifest["owner_repo"].split("/")
                if owner.casefold() not in APPROVED_OWNERS or name != child.name:
                    raise ValueError("invalid-generated-owner")
                repo = repo_by_name.get(name.casefold())
                if not isinstance(manifest.get("files"), dict):
                    raise ValueError("invalid-generated-files")
                pending_aliases = []
                pending_canonical = {}
                pending_hashes = {}
                historical = []
                for relative, record in manifest["files"].items():
                    if not isinstance(record, dict):
                        raise ValueError("invalid-generated-record")
                    output = relative_path(relative).as_posix()
                    source = relative_path(record["source_path"]).as_posix()
                    if not output.startswith("documents/") or output != "documents/" + source:
                        raise ValueError("invalid-generated-document-path")
                    if Path(source).suffix.casefold() not in HUMAN_SUFFIXES:
                        continue
                    if Path(source).suffix.casefold() == ".pdf" and any(
                        part.casefold().endswith(".xcassets") for part in Path(source).parts
                    ):
                        warn("doc: image asset PDF excluded; no OCR requested")
                        continue
                    if excluded(Path(source)):
                        continue
                    try:
                        data = safe_file(
                            child,
                            output,
                            maximum=input_cap(Path(source).suffix),
                        )
                    except DocumentTooLarge:
                        warn(
                            "doc: unreadable-or-excluded generated document input above bounded size cap"
                        )
                        continue
                    digest = hashlib.sha256(data).hexdigest()
                    if digest != record.get("sha256"):
                        raise ValueError("generated-content-hash-mismatch")
                    canonical_name = repo.name if repo else name
                    did = "doc/" + canonical_name + "/" + source
                    entry = canonical.get(did) or pending_canonical.get(did)
                    tracked = repo.blob(source) if repo else None
                    exact_alias = (
                        entry is not None
                        and tracked is not None
                        and tracked == data
                        and entry[1] == data
                    )
                    if not exact_alias:
                        try:
                            rendered = text_from_bytes(data, Path(source).suffix)
                        except DocumentTooLarge:
                            warn("doc: unreadable-or-excluded generated visible text above bounded size cap")
                            continue
                        except ValueError:
                            if Path(source).suffix.casefold() == ".pdf":
                                warn(
                                    "doc: generated PDF not indexed; local bounded extraction unavailable or failed"
                                )
                                continue
                            raise
                    if exact_alias:
                        pending_aliases.append((entry[0], str(child / output)))
                    elif tracked is not None and tracked == data:
                        # The library may select a tracked document outside the old root/docs walk.
                        doc = make_doc(child / output)
                        doc.doc_id = did
                        doc.path = str(repo.root / source)
                        doc.text_markdown = rendered
                        doc.url = (
                            "https://github.com/"
                            + repo.owner
                            + "/"
                            + repo.name
                            + "/blob/"
                            + repo.commit
                            + "/"
                            + source
                        )
                        doc.extra.update(
                            source_commit=repo.commit,
                            source_sha256=digest,
                            source_aliases=[str(child / output)],
                        )
                        pending_canonical[did] = (doc, data)
                    else:
                        # A historical or otherwise unique body must not vanish behind a newer Git document.
                        duplicate = hashes.get(digest) or pending_hashes.get(digest)
                        if duplicate is not None:
                            pending_aliases.append((duplicate, str(child / output)))
                            continue
                        doc = make_doc(child / output)
                        doc.doc_id = "doc/library/history/" + digest
                        doc.text_markdown = rendered
                        source_commit = record.get("source_commit", manifest["source_commit"])
                        if not isinstance(source_commit, str) or not re.fullmatch(
                            r"[0-9a-f]{40,64}", source_commit
                        ):
                            raise ValueError("invalid-generated-source-commit")
                        doc.extra.update(
                            source_sha256=digest,
                            library_source_path=source,
                            library_source_commit=source_commit,
                        )
                        historical.append(doc)
                        pending_hashes[digest] = doc
                # Publish provenance only after the complete manifest validates.
                for document, alias in pending_aliases:
                    document.extra.setdefault("source_aliases", []).append(alias)
                canonical.update(pending_canonical)
                for document, data in pending_canonical.values():
                    hashes.setdefault(hashlib.sha256(data).hexdigest(), document)
                hashes.update(pending_hashes)
                shared.update(pending_hashes)
                yield from historical
            except FileNotFoundError:
                if child in generated_collections:
                    warn(
                        "doc: invalid or unreadable generated manifest/content; no aliases trusted"
                    )
                continue
            except (ValueError, TypeError, KeyError, OSError, UnicodeError):
                warn("doc: invalid or unreadable generated manifest/content; no aliases trusted")
    for directory, dirs, files in os.walk(root, followlinks=False):
        current = Path(directory)
        dirs[:] = sorted(
            name
            for name in dirs
            if name.casefold() not in BLOCKED
            and name.casefold() != "generated"
            and not name.startswith(".generated-docs-")
            and not (current / name).is_symlink()
            and (current / name) not in generated_collections
        )
        for name in sorted(files):
            path = current / name
            if path.suffix.casefold() not in HUMAN_SUFFIXES or path.is_symlink() or excluded(path):
                continue
            if (
                path.parent == root
                and name == "README.md"
                and any(p.parent == root for p in generated_collections)
            ):
                continue
            relative = path.relative_to(root).as_posix()
            try:
                data = safe_file(
                    root,
                    relative,
                    maximum=input_cap(path.suffix),
                )
                text = text_from_bytes(data, path.suffix)
            except (OSError, ValueError, UnicodeError) as exc:
                reason = (
                    str(exc)
                    if isinstance(exc, ValueError)
                    and str(exc)
                    in (
                        "pdf-extractor-unavailable",
                        "pdf-extraction-failed",
                        "pdf-output-empty-or-too-large",
                        "pdf-input-too-large",
                    )
                    else "unreadable-or-excluded"
                )
                warn("doc: legacy document skipped (" + reason + ")")
                continue
            original = hashes.get(hashlib.sha256(data).hexdigest())
            if original is not None:
                original.extra.setdefault("source_aliases", []).append(str(path))
                continue
            doc = make_doc(path)
            doc.doc_id = "doc/library/legacy/" + hashlib.sha256(data).hexdigest()
            doc.text_markdown = text
            doc.extra.update(
                source_sha256=hashlib.sha256(data).hexdigest(), library_relative_path=relative
            )
            if path.suffix.casefold() == ".pdf":
                doc.extra["extractor"] = "local-pdftotext"
            hashes[hashlib.sha256(data).hexdigest()] = doc
            shared[hashlib.sha256(data).hexdigest()] = doc
            yield doc
