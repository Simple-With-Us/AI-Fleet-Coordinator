#!/usr/bin/env python3
"""Upsert default-branch GitHub rulesets for fleet repos.

User accounts cannot attach org-wide rulesets to future repos (no org).
This is the create-time hook: call from onboard-new-app.sh, or by hand.

Kinds (match fleet-apps.json):
  product / site — PR required, conversation resolution, optional CI checks
  library        — same; never "strict" up-to-date (that saturates CI)
  infra          — PR required, conversation resolution, no default CI check

Never requires approving reviews (solo owner).  Never bypass actors.
Never force-push / delete default branch.  Status checks are not strict.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

RULESET_NAME = "default-main-protection"
GITHUB_ACTIONS_INTEGRATION_ID = 15368
JsonDict = dict[str, Any]


def gh_json(args: list[str], input_obj: Any | None = None) -> tuple[int, Any, str]:
    cmd = ["gh", "api", *args]
    raw_in = None
    if input_obj is not None:
        cmd.extend(["--input", "-"])
        raw_in = json.dumps(input_obj)
    proc = subprocess.run(
        cmd,
        input=raw_in,
        capture_output=True,
        text=True,
        check=False,
    )
    parsed: Any = None
    if proc.stdout.strip():
        try:
            parsed = json.loads(proc.stdout)
        except json.JSONDecodeError:
            parsed = proc.stdout
    return proc.returncode, parsed, proc.stderr


def pull_request_rule() -> JsonDict:
    return {
        "type": "pull_request",
        "parameters": {
            "required_approving_review_count": 0,
            "dismiss_stale_reviews_on_push": False,
            "required_reviewers": [],
            "require_code_owner_review": False,
            "require_last_push_approval": False,
            "required_review_thread_resolution": True,
            "require_extra_approval_for_unattributed_changes": True,
            "allowed_merge_methods": ["merge", "squash", "rebase"],
        },
    }


def status_checks_rule(contexts: list[str]) -> JsonDict:
    return {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": False,
            "do_not_enforce_on_create": True,
            "required_status_checks": [
                {
                    "context": name,
                    "integration_id": GITHUB_ACTIONS_INTEGRATION_ID,
                }
                for name in contexts
            ],
        },
    }


def ruleset_body(contexts: list[str]) -> JsonDict:
    rules: list[JsonDict] = [
        {"type": "deletion"},
        {"type": "non_fast_forward"},
        pull_request_rule(),
    ]
    if contexts:
        rules.append(status_checks_rule(contexts))
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "bypass_actors": [],
        "conditions": {
            "ref_name": {
                "include": ["~DEFAULT_BRANCH"],
                "exclude": [],
            }
        },
        "rules": rules,
    }


def contexts_for_kind(kind: str, extra: list[str]) -> list[str]:
    if extra:
        return extra
    if kind in {"product", "site", "library"}:
        return []
    return []


def find_named_ruleset(owner_repo: str) -> int | None:
    code, parsed, err = gh_json([f"repos/{owner_repo}/rulesets"])
    if code != 0:
        raise SystemExit(f"list rulesets failed for {owner_repo}: {err.strip()}")
    rows = parsed if isinstance(parsed, list) else []
    for row in rows:
        if isinstance(row, dict) and row.get("name") == RULESET_NAME:
            ident = row.get("id")
            if isinstance(ident, int):
                return ident
    return None


def find_protected_ruleset(owner_repo: str) -> int | None:
    """Id of a branch ruleset that already enforces the baseline rules, if any.

    Used by --all so a repo whose ruleset is correct but named differently is
    RENAMED in place rather than having a second, redundant ruleset created
    next to it.  Two overlapping rulesets on one branch is exactly the drift we
    are trying to eliminate, so introducing one here would be self-defeating.
    """
    code, parsed, err = gh_json([f"repos/{owner_repo}/rulesets"])
    if code != 0:
        raise SystemExit(f"list rulesets failed for {owner_repo}: {err.strip()}")
    for row in parsed if isinstance(parsed, list) else []:
        if not isinstance(row, dict) or row.get("target") != "branch":
            continue
        ident = row.get("id")
        if not isinstance(ident, int):
            continue
        dcode, detail, _derr = gh_json([f"repos/{owner_repo}/rulesets/{ident}"])
        if dcode != 0 or not isinstance(detail, dict):
            continue
        have = {r.get("type") for r in detail.get("rules", [])}
        if all(r in have for r in ("deletion", "non_fast_forward", "pull_request")):
            return ident
    return None


def list_live_repos() -> list[str]:
    code, parsed, err = gh_json([
        "orgs/Simple-With-Us/repos", "--paginate", "-q", "[.[].name]",
    ])
    if code != 0:
        raise SystemExit(f"list org repos failed: {err.strip()}")
    return [r for r in (parsed or []) if isinstance(r, str)]


def upsert(owner_repo: str, body: JsonDict, dry_run: bool, existing: int | None) -> str:
    if existing is None:
        method = "POST"
        path = f"repos/{owner_repo}/rulesets"
        action = "created"
    else:
        method = "PUT"
        path = f"repos/{owner_repo}/rulesets/{existing}"
        action = "updated"
    if dry_run:
        contexts = [c["context"] for r in body["rules"]
                    if r.get("type") == "required_status_checks"
                    for c in r["parameters"]["required_status_checks"]]
        return f"DRY {method} {path} checks={contexts}"
    code, parsed, err = gh_json(["--method", method, path], body)
    if code != 0:
        raise SystemExit(f"{method} {path} failed: {err.strip()}")
    ident = parsed.get("id") if isinstance(parsed, dict) else existing
    return f"{action} {owner_repo} ruleset {ident} ({body['name']})"


def rename_only(owner_repo: str, ruleset_id: int, dry_run: bool) -> str:
    """Rename an already-compliant ruleset while carrying its rules forward.

    Two things make this non-obvious:

    1. ``PATCH`` is not implemented on the repository-rulesets endpoint -- it
       answers 404 -- so a rename cannot be a partial update.  It has to be a
       ``PUT``, i.e. a full replace.
    2. The baseline body carries no ``required_status_checks`` rule, so PUTting
       the baseline over a repo whose ruleset also requires CI (Socratic-Trade
       requires verify+gitleaks, congress-trading-shared requires verify) would
       strip that requirement and silently let merges land on red CI.

    So: GET the current ruleset, copy its rules/conditions/bypass actors
    through untouched, and change only ``name``.
    """
    code, detail, err = gh_json([f"repos/{owner_repo}/rulesets/{ruleset_id}"])
    if code != 0 or not isinstance(detail, dict):
        raise SystemExit(f"read {owner_repo} ruleset {ruleset_id} failed: {err.strip()}")

    checks = [
        c["context"]
        for r in detail.get("rules", [])
        if r.get("type") == "required_status_checks"
        for c in r.get("parameters", {}).get("required_status_checks", [])
    ]
    # Carry the live configuration forward verbatim; only `name` changes.
    body = {
        "name": RULESET_NAME,
        "target": detail.get("target", "branch"),
        "enforcement": detail.get("enforcement", "active"),
        "bypass_actors": detail.get("bypass_actors", []),
        "conditions": detail.get("conditions", {}),
        "rules": detail.get("rules", []),
    }
    if dry_run:
        return (f"DRY PUT repos/{owner_repo}/rulesets/{ruleset_id} "
                f"name={RULESET_NAME} preserved_checks={checks}")
    code, _parsed, err = gh_json(
        ["--method", "PUT", f"repos/{owner_repo}/rulesets/{ruleset_id}"], body
    )
    if code != 0:
        raise SystemExit(f"PUT {owner_repo} ruleset {ruleset_id} failed: {err.strip()}")
    kept = f" (required checks preserved: {', '.join(checks)})" if checks else ""
    return f"renamed {owner_repo} ruleset {ruleset_id} -> {RULESET_NAME}{kept}"


def sync_all(dry_run: bool) -> int:
    """Bring every live public repo to the baseline ruleset."""
    failures = 0
    for repo in sorted(list_live_repos()):
        code, info, _ = gh_json([f"repos/Simple-With-Us/{repo}"])
        if code == 0 and isinstance(info, dict) and info.get("private"):
            print(f"SKIP {repo} (private; rulesets are not available on this plan)")
            continue
        try:
            named = find_named_ruleset(f"Simple-With-Us/{repo}")
            if named is not None:
                print(f"ok    {repo} (already {RULESET_NAME})")
                continue
            protected = find_protected_ruleset(f"Simple-With-Us/{repo}")
            if protected is not None:
                # Compliant but misnamed: rename only, preserve every rule.
                print(rename_only(f"Simple-With-Us/{repo}", protected, dry_run))
            else:
                print(upsert(f"Simple-With-Us/{repo}", ruleset_body([]), dry_run, None))
        except SystemExit as e:
            print(f"FAIL {repo}: {e}")
            failures += 1
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="owner/name")
    parser.add_argument(
        "--all",
        action="store_true",
        help="sync the baseline ruleset across every live public repo in the org",
    )
    parser.add_argument(
        "--kind",
        choices=("product", "site", "library", "infra"),
        default="product",
    )
    parser.add_argument(
        "--checks",
        action="append",
        default=[],
        help="Required GitHub Actions check context. Repeatable. Empty = PR gate only.",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.all:
        return 1 if sync_all(args.dry_run) else 0

    if not args.repo:
        parser.error("--repo is required unless --all is passed")
    owner_repo = args.repo.strip()
    if "/" not in owner_repo:
        raise SystemExit("--repo must be owner/name")
    contexts = [c for c in args.checks if c.strip()]
    if not contexts:
        contexts = contexts_for_kind(args.kind, [])
    body = ruleset_body(contexts)
    print(upsert(owner_repo, body, args.dry_run, find_named_ruleset(owner_repo)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
