"""Git-path policy and catalog builder for the repository document library."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from pathlib import PurePosixPath
from typing import Any

CATALOG_SCHEMA_VERSION = 2
CATALOG_POLICY_ID = "tracked-tree-roles-v2"
COLLECTION_POLICY_ID = "human-docs-v3"

HUMAN_DOCUMENT_SUFFIXES = {
    ".md",
    ".rst",
    ".adoc",
    ".txt",
    ".pdf",
    ".doc",
    ".docx",
    ".odt",
    ".rtf",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
    ".ipynb",
    ".tex",
    ".html",
    ".htm",
}
HTML_DOCUMENT_SUFFIXES = {".html", ".htm"}
PATH_SCOPED_DOCUMENT_SUFFIXES = {
    ".doc",
    ".docx",
    ".ipynb",
    ".odt",
    ".ppt",
    ".pptx",
    ".xls",
    ".xlsx",
}
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
EVIDENCE_SUFFIXES = {".csv", ".tsv", ".json", ".jsonl", ".xml", ".yaml", ".yml"}

DOCUMENT_ROOTS = {
    "doc",
    "docs",
    "documentation",
    "handbook",
    "manual",
    "manuals",
    "runbook",
    "runbooks",
    "plan",
    "plans",
    "protocol",
    "protocols",
    "guide",
    "guides",
    "playbook",
    "playbooks",
    "skill",
    "skills",
    "bot",
    "bots",
    "reference",
    "references",
    "design",
    "designs",
    "audit",
    "audits",
    "review",
    "reviews",
    "report",
    "reports",
    "rollout",
    "rollouts",
    "research",
    "architecture",
    "decision",
    "decisions",
    "adr",
    "adrs",
    "spec",
    "specs",
}
SCRATCH_PREFIXES = {"archive", "backup", "scratch", "temp", "tmp"}

ROOT_DOCUMENT_STEMS = {
    "agents",
    "authors",
    "changelog",
    "claude",
    "code_of_conduct",
    "contributing",
    "copying",
    "deploy",
    "development",
    "infisical",
    "install",
    "license",
    "notice",
    "plan",
    "project",
    "readme",
    "security",
    "setup",
    "status",
    "testing",
}
ROOT_EXTENSIONLESS_DOCUMENT_NAMES = {
    "authors",
    "contributing",
    "copying",
    "license",
    "notice",
    "readme",
}

SENSITIVE_DIRS = {
    ".aws",
    ".azure",
    ".codex",
    ".docker",
    ".gnupg",
    ".infisical",
    ".kube",
    ".secrets",
    ".ssh",
    ".terraform",
    ".vault",
    "keychains",
    "credentials-store",
    "credential-store",
    "secrets",
    "secret",
    "private",
    "private-audits",
    "credentials",
    "credential",
    "tokens",
    "keys",
}
SENSITIVE_SUFFIXES = {".env", ".pem", ".p8", ".p12", ".pfx", ".key", ".jks", ".keystore"}
SENSITIVE_FILENAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "auth.json",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
    "service-account.json",
}

VENDOR_DIRS = {
    "vendor",
    "third_party",
    "third-party",
    "node_modules",
    "bower_components",
    "jspm_packages",
    "pods",
    "carthage",
}
GENERATED_DIRS = {
    ".build",
    ".cache",
    ".next",
    ".output",
    "build",
    "cache",
    "caches",
    "coverage",
    "deriveddata",
    "dist",
    "generated",
    "logs",
    "out",
    "target",
}
FIXTURE_DIRS = {
    "__fixtures__",
    "__snapshots__",
    "fixtures",
    "fixture",
    "snapshots",
    "test-data",
    "test-fixtures",
    "testdata",
}
APP_ROOTS = {"app", "apps", "clients", "ios", "macos", "native", "services", "web", "workers"}
DEPLOYMENT_ROOTS = {"deploy", "deployment", "infra", "infrastructure", "terraform", "helm", "k8s"}
BUILD_MANIFEST_NAMES = {
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "cargo.toml",
    "cargo.lock",
    "pyproject.toml",
    "go.mod",
    "package.swift",
    "pubspec.yaml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
}


def _parts(relative: str) -> tuple[str, ...]:
    return tuple(part.casefold() for part in PurePosixPath(relative).parts)


def path_is_safe(relative: str) -> bool:
    if (
        not relative
        or "\\" in relative
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in relative)
    ):
        return False
    path = PurePosixPath(relative)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def is_sensitive_path(relative: str, allow_private_audit: bool = False) -> bool:
    parts = _parts(relative)
    allowed_private_audit = (
        allow_private_audit and len(parts) >= 2 and parts[:2] == ("docs", "private-audits")
    )
    sensitive_dirs = SENSITIVE_DIRS - ({"private-audits"} if allowed_private_audit else set())
    if any(part in sensitive_dirs for part in parts):
        return True
    name = PurePosixPath(relative).name.casefold()
    suffix = PurePosixPath(name).suffix.casefold()
    return (
        name == ".env"
        or name.startswith(".env.")
        or name.endswith(".env")
        or name in SENSITIVE_FILENAMES
        or suffix in SENSITIVE_SUFFIXES
    )


def is_vendor_path(relative: str) -> bool:
    return bool(set(_parts(relative)) & VENDOR_DIRS)


def is_generated_path(relative: str) -> bool:
    return bool(set(_parts(relative)) & GENERATED_DIRS)


def is_fixture_path(relative: str) -> bool:
    return bool(set(_parts(relative)) & FIXTURE_DIRS)


def is_scratch_path(relative: str) -> bool:
    for part in _parts(relative):
        stem = PurePosixPath(part).stem
        if any(
            part == prefix
            or part.startswith((f"{prefix}-", f"{prefix}_"))
            or stem == prefix
            or stem.startswith((f"{prefix}-", f"{prefix}_"))
            for prefix in SCRATCH_PREFIXES
        ):
            return True
    return False


def is_document_rooted(relative: str) -> bool:
    return any(part in DOCUMENT_ROOTS for part in _parts(relative)[:-1])


def is_human_document(relative: str, allow_private_audit: bool = False) -> bool:
    if not path_is_safe(relative):
        return False
    if is_sensitive_path(relative, allow_private_audit=allow_private_audit):
        return False
    if is_scratch_path(relative):
        return False
    if is_vendor_path(relative) or is_generated_path(relative) or is_fixture_path(relative):
        return False

    path = PurePosixPath(relative)
    suffix = path.suffix.casefold()
    stem = path.stem.casefold()
    parts = _parts(relative)
    if suffix not in HUMAN_DOCUMENT_SUFFIXES:
        if suffix:
            return False
        return len(parts) == 1 and stem in ROOT_EXTENSIONLESS_DOCUMENT_NAMES

    if suffix in HTML_DOCUMENT_SUFFIXES:
        in_document_root = is_document_rooted(relative)
        root_doc_name = len(parts) == 1 and stem in ROOT_DOCUMENT_STEMS
        return in_document_root or root_doc_name
    if suffix in PATH_SCOPED_DOCUMENT_SUFFIXES:
        return is_document_rooted(relative)
    return True


def is_linkable_evidence(relative: str, allow_private_audit: bool = False) -> bool:
    if not path_is_safe(relative) or is_sensitive_path(
        relative, allow_private_audit=allow_private_audit
    ):
        return False
    if is_scratch_path(relative):
        return False
    if is_vendor_path(relative) or is_generated_path(relative) or is_fixture_path(relative):
        return False
    if PurePosixPath(relative).suffix.casefold() not in EVIDENCE_SUFFIXES:
        return False
    return any(part in DOCUMENT_ROOTS for part in _parts(relative)[:-1])


def role_for_path(
    relative: str,
    mode: str,
    object_type: str,
    *,
    selected: bool = False,
    allow_private_audit: bool = False,
) -> tuple[str, str | None]:
    if object_type == "commit" or mode == "160000":
        return "submodule", "submodule reference has no in-repository file content"
    if mode == "120000":
        return "symlink", "symlink targets are never followed or copied"
    if not path_is_safe(relative):
        return "unsafe-path", "path is not safe for portable mirror output"
    if is_sensitive_path(relative, allow_private_audit=allow_private_audit):
        return "credential-store", "sensitive path or credential-shaped filename"
    if is_scratch_path(relative):
        return "scratch-or-archive", "scratch, temporary, backup, or archive path"
    if is_vendor_path(relative):
        return "third-party-vendor", "vendored or dependency tree"
    if is_generated_path(relative):
        return "generated-output", "generated output or cache tree"
    if is_fixture_path(relative):
        return "test-fixture", "test fixture or snapshot data"
    if selected:
        suffix = PurePosixPath(relative).suffix.casefold()
        if suffix in ASSET_SUFFIXES:
            return "linked-asset", None
        if suffix in EVIDENCE_SUFFIXES:
            return "linked-evidence", None
        return "human-document", None

    if PurePosixPath(relative).suffix.casefold() in PATH_SCOPED_DOCUMENT_SUFFIXES:
        return "ambiguous-document-data", "office document or notebook outside a documentation root"

    parts = _parts(relative)
    name = PurePosixPath(relative).name.casefold()
    if any(part in DEPLOYMENT_ROOTS for part in parts[:-1]) or name in {
        "dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
    }:
        return "deployment-config", "not linked human documentation"
    if any(part in {"tests", "test", "__tests__"} for part in parts[:-1]):
        return "test-source", "test code or non-document test data"
    if any(part in {"tools", "tooling"} for part in parts[:-1]):
        return "development-tool", "reusable development tooling source"
    if any(part in {"scripts", "script"} for part in parts[:-1]):
        return "operations-script", "operational entrypoint or script source"
    if any(part in {"packages", "shared", "libraries", "library"} for part in parts[:-1]):
        return "shared-library", "shared library or package source"
    if any(part in APP_ROOTS for part in parts[:-1]) or parts[0] in APP_ROOTS:
        return "application-source", "deployable application source"
    if name in BUILD_MANIFEST_NAMES:
        return "build-manifest", "build or dependency metadata"
    if PurePosixPath(relative).suffix.casefold() in ASSET_SUFFIXES:
        return "source-asset", "unlinked source asset"
    return "other-source", "not selected by the human-document or linked-evidence policy"


def repository_profile(repo_name: str, paths: Iterable[str]) -> tuple[str, list[str]]:
    path_set = set(paths)
    tops = {
        PurePosixPath(path).parts[0].casefold() for path in path_set if PurePosixPath(path).parts
    }
    package_roots = sorted(
        str(PurePosixPath(path).parent)
        for path in path_set
        if PurePosixPath(path).name.casefold()
        in {
            "package.json",
            "cargo.toml",
            "pyproject.toml",
            "go.mod",
            "package.swift",
            "pubspec.yaml",
            "pom.xml",
            "build.gradle",
            "build.gradle.kts",
        }
    )
    if "casks" in tops:
        return "distribution-tap", ["top-level Casks directory"]
    if repo_name.casefold().endswith(("-shared", "-common", "-sdk")):
        return "shared-package", ["repository name identifies a shared package"]
    app_roots = sorted(tops & APP_ROOTS)
    if "apps" in tops or len(app_roots) > 1:
        return "multi-application", ["top-level application roots: " + ", ".join(app_roots)]
    if app_roots:
        return "single-application", ["top-level application root: " + app_roots[0]]
    if "site" in tops or "static" in tops:
        return "static-site", ["top-level static site root"]
    if package_roots:
        return "shared-package", ["tracked build manifests: " + ", ".join(package_roots)]
    if "handbook" in tops or "infra" in tops or "infrastructure" in tops:
        return "operations", ["top-level handbook or infrastructure root"]
    if "scripts" in tops or "tools" in tops:
        return "tooling", ["top-level scripts or tools root"]
    return "mixed-repository", [
        "no single dominant deployment or package profile inferred from paths"
    ]


def build_catalog(
    *,
    owner_repo: str,
    repo_name: str,
    source_ref: str,
    source_commit: str,
    tracked_files: Iterable[dict[str, Any]],
    copied_paths: set[str],
    private_docs_opt_in: bool = False,
) -> dict[str, Any]:
    rows = []
    path_list: list[str] = []
    for item in tracked_files:
        path = item["path"]
        path_list.append(path)
        selected = path in copied_paths
        role, exclusion_reason = role_for_path(
            path,
            item["mode"],
            item["object_type"],
            selected=selected,
            allow_private_audit=private_docs_opt_in,
        )
        rows.append(
            {
                "path": path,
                "mode": item["mode"],
                "object_type": item["object_type"],
                "object_id": item["object_id"],
                "size_bytes": item.get("size_bytes"),
                "size_unavailable_reason": (
                    "blob size is unavailable locally; lazy fetch is disabled"
                    if item["object_type"] == "blob" and item.get("size_bytes") is None
                    else None
                ),
                "role": role,
                "copied_to_documents": selected,
                "exclusion_reason": exclusion_reason,
            }
        )

    profile, profile_evidence = repository_profile(repo_name, path_list)
    role_counts = Counter(row["role"] for row in rows)
    top_level_entries = sorted(
        {PurePosixPath(path).parts[0] for path in path_list if PurePosixPath(path).parts},
        key=str.casefold,
    )
    build_manifests = sorted(
        path for path in path_list if PurePosixPath(path).name.casefold() in BUILD_MANIFEST_NAMES
    )
    package_boundaries = sorted(
        {PurePosixPath(path).parent.as_posix() for path in build_manifests},
        key=str.casefold,
    )
    root_names = {name.casefold() for name in top_level_entries}
    return {
        "schema_version": CATALOG_SCHEMA_VERSION,
        "catalog_policy_id": CATALOG_POLICY_ID,
        "collection_policy_id": COLLECTION_POLICY_ID,
        "generated_by": "repo_catalog.py",
        "owner_repo": owner_repo,
        "repo": repo_name,
        "source_ref": source_ref,
        "source_commit": source_commit,
        "repository_profile": profile,
        "profile_evidence": profile_evidence,
        "structural_inventory": {
            "top_level_entries": top_level_entries,
            "application_roots": sorted(root_names & APP_ROOTS),
            "deployment_roots": sorted(root_names & DEPLOYMENT_ROOTS),
            "build_manifests": build_manifests,
            "package_boundaries": package_boundaries,
        },
        "role_counts": dict(sorted(role_counts.items())),
        "tracked_entry_count": len(rows),
        "copied_entry_count": sum(1 for row in rows if row["copied_to_documents"]),
        "files": rows,
    }
