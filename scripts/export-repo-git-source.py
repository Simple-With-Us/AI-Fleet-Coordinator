#!/usr/bin/env python3
"""Export a pinned document snapshot through the installed, authenticated Git relay CLI."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import resource
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True
script_directory = str(Path(__file__).resolve().parent)
if script_directory not in sys.path:
    sys.path.insert(0, script_directory)

import remote_git_source as remote
from fleet_lanes.code_main_sync import run_child

MAX_RELAY_RESPONSE_BYTES = 4_000_000
RELAY_READ_TIMEOUT_SECONDS = 310
_MANAGED_ROUTE_VERIFIED = False
_MANAGED_ROUTE_ACTOR = None
_MANAGED_ROUTE_KEY = None


def verify_managed_route(environ: dict[str, str] | None = None) -> str:
    """Accept only the installed managed wrapper and its already assigned authenticated seat."""
    global _MANAGED_ROUTE_VERIFIED, _MANAGED_ROUTE_ACTOR, _MANAGED_ROUTE_KEY
    environ = os.environ if environ is None else environ
    launcher = (environ.get("AGENT_LAUNCHER") or "").strip()
    launch_seat = (environ.get("AGENT_LAUNCH_SEAT") or "").strip().upper()
    seats = [
        (environ.get(key) or "").strip().upper()
        for key in ("AGENT_LAUNCH_SEAT", "AGENT_SEAT", "AGENT_TAG")
    ]
    seats = [seat for seat in seats if seat]
    if (
        bool(launcher) != bool(launch_seat)
        or not seats
        or len(set(seats)) != 1
        or not re.fullmatch(r"[A-Z][A-Z0-9_-]{0,63}", seats[0])
    ):
        raise remote.SourceError("Managed exporter requires one existing assigned seat")
    command = shutil.which("simple-git", path=environ.get("PATH"))
    managed = Path.home() / ".local/bin/simple-git"
    if command is None or Path(command).resolve() != managed.resolve() or not managed.is_file():
        raise remote.SourceError("Assigned managed Git wrapper is unavailable")
    info = managed.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid not in (0, os.getuid())
        or info.st_mode & 0o022
    ):
        raise remote.SourceError("Assigned managed Git wrapper permissions are unsafe")
    for parent in managed.parents:
        info = parent.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid not in (0, os.getuid())
            or info.st_mode & 0o022
        ):
            raise remote.SourceError("Assigned managed Git wrapper parent is unsafe")
    with managed.open("rb") as source:
        marker = source.read(2048)
    if b"# fleet-cli managed wrapper v1" not in marker:
        raise remote.SourceError("Assigned Git entrypoint is not the installed managed wrapper")
    cache_key = (
        launcher,
        launch_seat,
        tuple(seats),
        command,
        environ.get("HOME"),
        environ.get("PATH"),
    )
    if _MANAGED_ROUTE_VERIFIED and _MANAGED_ROUTE_KEY == cache_key:
        return _MANAGED_ROUTE_ACTOR
    try:
        with tempfile.TemporaryFile() as output:
            result = run_child(
                [command, "whoami", "--json"],
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=60,
                start_new_session=True,
                preexec_fn=_limit_relay_output,
                env=environ,
                check=False,
            )
            if result.returncode != 0 or os.fstat(output.fileno()).st_size > 65536:
                raise remote.SourceError("Assigned managed Git identity verification failed")
            output.seek(0)
            response = json.loads(output.read(65537))
            if not isinstance(response, dict) or not isinstance(response.get("structured"), dict):
                raise remote.SourceError(
                    "Managed Git identity response requires structured objects"
                )
            identity = response["structured"]
        fields = [identity.get(key) for key in ("authenticated_seat", "default_identity")]
        advertised = identity.get("identities")
        if (
            any(not isinstance(value, str) or value != seats[0] for value in fields)
            or not isinstance(advertised, list)
            or not any(
                isinstance(item, dict) and item.get("identity") == seats[0] for item in advertised
            )
        ):
            raise remote.SourceError("Assigned managed Git identity does not match this process")
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        raise remote.SourceError("Assigned managed Git identity verification failed") from exc
    _MANAGED_ROUTE_VERIFIED = True
    _MANAGED_ROUTE_ACTOR = seats[0]
    _MANAGED_ROUTE_KEY = cache_key
    return _MANAGED_ROUTE_ACTOR


def _limit_relay_output() -> None:
    # One overflow byte makes a swallowed SIGXFSZ failure observable to the parent.
    ceiling = MAX_RELAY_RESPONSE_BYTES + 1
    for limit in resource.getrlimit(resource.RLIMIT_FSIZE):
        if limit != resource.RLIM_INFINITY:
            ceiling = min(ceiling, limit)
    resource.setrlimit(resource.RLIMIT_FSIZE, (ceiling, ceiling))


def relay_read(tool: str, arguments: dict, *, environ: dict[str, str] | None = None) -> dict:
    if tool not in {"gr_wf_repo_get", "gr_wf_branch_get", "gr_wf_git_tree_get", "gr_wf_file_get"}:
        raise remote.SourceError("Exporter attempted an unsupported relay operation")
    bound = os.environ if environ is None else environ
    if not bound.get("SIMPLE_TOOLS_BEARER"):
        verify_managed_route(bound)
    # The installed CLI verifies the authenticated seat; no credential locator is read here.
    try:
        with tempfile.TemporaryFile() as output:
            result = run_child(
                ["simple-git", "call", tool, "--args-json", "-", "--json"],
                input=remote.canonical(arguments),
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=RELAY_READ_TIMEOUT_SECONDS,
                check=False,
                preexec_fn=_limit_relay_output,
                env=environ,
            )
            if os.fstat(output.fileno()).st_size > MAX_RELAY_RESPONSE_BYTES:
                raise remote.SourceError("Assigned Git relay response exceeded its byte bound")
            if result.returncode != 0:
                raise remote.SourceError(
                    "Assigned Git relay refused or failed a bounded read: " + tool
                )
            output.seek(0)
            response = json.load(output)
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("structured"), dict)
            or not response["structured"]
        ):
            raise remote.SourceError("Assigned Git relay did not return structured read data")
        return response["structured"]
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        if isinstance(exc, remote.SourceError):
            raise
        raise remote.SourceError("Assigned Git relay read could not complete: " + tool) from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--owner-repo", required=True)
    parser.add_argument("--expected-repository-id", type=int, required=True)
    parser.add_argument("--visibility", choices=("public", "private"), required=True)
    parser.add_argument("--commit", required=True, help="expected immutable default-branch commit")
    parser.add_argument(
        "--private-docs-repo", help="exact private repository opt-in, also required on import"
    )
    parser.add_argument(
        "--output", type=Path, required=True, help="new descriptor path in an owner-only directory"
    )
    args = parser.parse_args(argv)
    spec = importlib.util.spec_from_file_location(
        "collect_repo_documents", Path(__file__).with_name("collect-repo-documents.py")
    )
    assert spec is not None and spec.loader is not None
    collector = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = collector
    spec.loader.exec_module(collector)
    try:
        if (
            remote.REPOSITORY.fullmatch(args.owner_repo) is None
            or remote.OID.fullmatch(args.commit) is None
        ):
            raise remote.SourceError(
                "Exporter requires an exact GitHub repository and full SHA-1 commit"
            )
        if args.visibility == "private" and args.private_docs_repo != args.owner_repo:
            raise remote.SourceError(
                "Private export requires its exact --private-docs-repo identity"
            )
        if args.private_docs_repo is not None and (
            args.visibility != "private" or args.private_docs_repo != args.owner_repo
        ):
            raise remote.SourceError("Private-docs opt-in must name this exact private source")
        if (
            args.visibility == "private"
            and args.owner_repo.casefold() not in collector.VERIFIED_PRIVATE_DOC_REPOS
        ):
            raise remote.SourceError(
                "Private export is not configured for a verified private repository"
            )
        snapshot = remote.export_snapshot(relay_read, args.owner_repo, args.visibility, args.commit)
        if snapshot.identity["repository_id"] != args.expected_repository_id:
            raise remote.SourceError(
                "Provider repository id does not match the explicit expected identity"
            )
        owner, name = args.owner_repo.split("/")
        repo = collector.RepoInfo(
            args.output.absolute(),
            owner,
            name,
            snapshot.identity["source_ref"],
            args.commit,
            snapshot,
        )
        selected, _, _ = collector._select_blobs(
            repo,
            collector._parse_tree(repo),
            include_private_audits=args.private_docs_repo == args.owner_repo,
        )
        snapshot.require_selected(set(selected))
        remote.write_descriptor(snapshot, args.output)
        print(
            json.dumps(
                {
                    "owner_repo": args.owner_repo,
                    "source_commit": args.commit,
                    "tracked_entries": len(snapshot.entries),
                    "selected_blobs": len(selected),
                    "descriptor": str(args.output.absolute()),
                },
                sort_keys=True,
            )
        )
        return 0
    except (remote.SourceError, collector.CollectionError) as exc:
        print("error: " + str(exc), file=sys.stderr)
        return 1
    except (KeyError, TypeError, RecursionError):
        print("error: provider response is missing or malformed", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
