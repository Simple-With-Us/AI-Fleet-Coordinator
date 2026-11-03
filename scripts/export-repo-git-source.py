#!/usr/bin/env python3
"""Export a pinned document snapshot through the installed, authenticated Git relay CLI."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import remote_git_source as remote


def relay_read(tool: str, arguments: dict) -> dict:
    if tool not in {"gr_wf_repo_get", "gr_wf_branch_get", "gr_wf_git_tree_get", "gr_wf_file_get"}:
        raise remote.SourceError("Exporter attempted an unsupported relay operation")
    if not os.environ.get("SIMPLE_TOOLS_BEARER"):
        raise remote.SourceError("Exporter requires an existing process SIMPLE_TOOLS_BEARER")
    # The installed CLI verifies the authenticated seat; no credential locator is read here.
    try:
        with tempfile.TemporaryFile() as output:
            result = subprocess.run(
                ["simple-git", "call", tool, "--args-json", "-", "--json"],
                input=remote.canonical(arguments),
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=310,
                check=False,
            )
            if result.returncode != 0:
                raise remote.SourceError(
                    "Assigned Git relay refused or failed a bounded read: " + tool
                )
            if output.tell() > 4_000_000:
                raise remote.SourceError("Assigned Git relay response exceeded its byte bound")
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
