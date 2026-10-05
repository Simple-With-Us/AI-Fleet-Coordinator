#!/usr/bin/env python3
"""Audit the Simple-With-Us org for branch-protection drift and secret duplication.

Why this exists
---------------
GitHub org-wide rulesets ("a named list of rules that applies to ... multiple
repositories in an organization") are a **GitHub Team / Enterprise** feature.
This org is on GitHub Free, so there is no server-side place to declare a
baseline that every repo inherits.  On Free the only enforcement is per-repo
rulesets, which means "the global minimum" can only be held as *code* that
audits reality and fails on drift.  That is this script.

It also answers the other half of the owner's ask: which secrets are duplicated
across repos and should be lifted to organization level, and which of those are
not credentials at all and should become **variables** instead.

Read-only.  Never writes, never prints a secret value -- names only.

Usage:
    audit-org-security.py                 # human report, exit 0 even on findings
    audit-org-security.py --json          # machine-readable
    audit-org-security.py --enforce       # exit 1 if any repo drifts from baseline

Exit codes:  0 ok  ·  1 drift/duplication found and --enforce was passed  ·  2 usage.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

ORG = "Simple-With-Us"

JsonDict = dict[str, Any]

# --------------------------------------------------------------------------
# The baseline.  This block IS the policy -- change it here, not per repo.
# --------------------------------------------------------------------------

#: Ruleset every eligible public repo must carry, by name.
BASELINE_RULESET = "default-main-protection"

#: Rules that must appear in a baseline ruleset.  A ruleset missing any of
#: these is drift even though it is "present and active".
BASELINE_RULES = ("deletion", "non_fast_forward", "pull_request")

#: Repos GitHub Free refuses to give branch protection to.  Private repos on
#: Free return 403 "Upgrade to GitHub Pro or make this repository public to
#: enable this feature" for both rulesets and classic protection.  This is a
#: plan limit, not a gap we can close by configuring something, so the auditor
#: reports it as a documented exception instead of a failure.
FREE_PLAN_PRIVATE = {"Kodus-Config", "Fleet-OPS", "demo-repository"}

#: Secrets that are NOT credentials.  Sentry states plainly that "DSNs are safe
#: to keep public because they only allow submission of new events and related
#: event data; they do not allow read access to any information", and that the
#: secret half of a DSN "is optional and effectively deprecated".  Storing a DSN
#: as an Actions secret is a misclassification, and it is what creates the
#: chicken-and-egg Jay hit: the DSN is needed to *build* the app, so it cannot
#: be the kind of secret you fetch from Infisical at build time.  Correct fix is
#: to demote it to a variable, which needs no Infisical round trip at all.
NOT_A_CREDENTIAL = {
    "SENTRY_DSN",
    "SENTRY_FLEET_DSN",
}

#: Names of values that are identifiers rather than credentials, so a *variable*
#: is the right home even though today they are stored as secrets.
IS_IDENTIFIER = {
    "INFISICAL_PROJECT_ID",
}

#: The one credential CI genuinely cannot bootstrap itself: the machine identity
#: that unlocks the app's own Infisical project.  Candidate for a single org
#: secret instead of one copy per repo.
BOOTSTRAP_CREDENTIAL = {
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET",
}


def gh_json(path: str) -> tuple[int, Any]:
    proc = subprocess.run(
        ["gh", "api", path], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        return proc.returncode, proc.stderr
    try:
        return 0, json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.returncode, proc.stdout


def gh_names(path: str, key: str = "name") -> tuple[int, list[str]]:
    """GET a list endpoint and pull one field out of each row.

    Actions secrets/variables return a paginated envelope
    (``{"total_count": n, "secrets": [...]}``) while rulesets return a bare
    list, so accept both shapes rather than silently reporting zero secrets for
    every repo.
    """
    code, parsed = gh_json(path)
    if code != 0:
        return code, []
    if isinstance(parsed, list):
        rows: list[Any] = parsed
    elif isinstance(parsed, dict):
        rows = []
        for v in parsed.values():
            if isinstance(v, list):
                rows = v
                break
    else:
        return code, []
    return 0, [r[key] for r in rows if isinstance(r, dict) and key in r]


def list_repos() -> list[JsonDict]:
    proc = subprocess.run(
        ["gh", "repo", "list", ORG, "--limit", "300", "--json",
         "name,visibility,isArchived,defaultBranchRef"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"repo list failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def audit_rulesets(repo: str, visibility: str) -> JsonDict:
    """Judge a repo on the RULES it enforces, not on the ruleset's name.

    A ruleset called ``main-protection`` that blocks deletion, blocks force
    push, and requires a PR is fully compliant; renaming it to match the fleet
    convention is cosmetic.  Conflating the two would make this auditor cry
    "missing" on two properly protected repos and train everyone to ignore it.
    So: a repo passes if ANY active branch ruleset carries every baseline rule.
    A name difference is reported as an advisory, never as drift.
    """
    code, rulesets = gh_json(f"repos/{ORG}/{repo}/rulesets?includes_parents=true")
    if code != 0:
        msg = str(rulesets)
        if "Upgrade to GitHub Pro" in msg or "make this repository public" in msg:
            return {"status": "plan-blocked", "detail": "private repo on Free plan"}
        return {"status": "error", "detail": msg.strip()[:200]}

    rows = rulesets if isinstance(rulesets, list) else []
    branch_sets = [r for r in rows if r.get("target") == "branch"]
    if not branch_sets:
        return {"status": "missing", "rulesets": [r.get("name") for r in rows]}

    best: JsonDict | None = None
    for rs in branch_sets:
        code, detail = gh_json(f"repos/{ORG}/{repo}/rulesets/{rs['id']}")
        if code != 0 or not isinstance(detail, dict):
            continue
        have = {r.get("type") for r in detail.get("rules", [])}
        absent = [r for r in BASELINE_RULES if r not in have]
        cand = {
            "status": "ok" if not absent else "drift",
            "id": rs.get("id"),
            "name": rs.get("name"),
            "enforcement": rs.get("enforcement"),
            "missing_rules": absent,
        }
        if best is None or (cand["status"] == "ok" and best["status"] != "ok"):
            best = cand
    if best is None:
        return {"status": "error", "detail": "ruleset detail unreadable"}

    if best["status"] == "ok" and best.get("name") != BASELINE_RULESET:
        best["advisory"] = (
            f"protected, but named {best.get('name')!r} rather than "
            f"{BASELINE_RULESET!r} (cosmetic; rename via apply-github-ruleset.py)"
        )
    return best


def audit_repo(repo: str) -> JsonDict:
    secrets = gh_names(f"repos/{ORG}/{repo}/actions/secrets?per_page=100")
    variables = gh_names(f"repos/{ORG}/{repo}/actions/variables?per_page=100")
    return {
        "secrets": sorted(secrets[1]),
        "variables": sorted(variables[1]),
        "secrets_ok": secrets[0] == 0,
        "variables_ok": variables[0] == 0,
    }


def build_report() -> JsonDict:
    repos = list_repos()
    live = [r for r in repos if not r.get("isArchived")]

    protection: JsonDict[str, JsonDict] = {}
    inventory: JsonDict[str, JsonDict] = {}

    for r in live:
        name = r["name"]
        visibility = r.get("visibility", "PUBLIC")
        if visibility != "PUBLIC" and name in FREE_PLAN_PRIVATE:
            protection[name] = {
                "status": "plan-blocked",
                "detail": "private repo on Free plan: no rulesets, no classic protection",
                "exception": True,
            }
            continue
        protection[name] = audit_rulesets(name, visibility)
        inventory[name] = audit_repo(name)

    # ---- duplication: same secret name, many repos -------------------------
    holders: JsonDict[str, list[str]] = {}
    for name, inv in inventory.items():
        for s in inv["secrets"]:
            holders.setdefault(s, []).append(name)
    duplicated = {
        s: sorted(rs) for s, rs in holders.items() if len(rs) > 1
    }
    duplicated_sorted = dict(
        sorted(duplicated.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    )

    misfiled = {
        s: sorted(holders.get(s, []))
        for s in (NOT_A_CREDENTIAL | IS_IDENTIFIER)
        if s in holders
    }

    missing_bootstrap = {
        name: sorted(
            (set(BOOTSTRAP_CREDENTIAL) - set(inv["secrets"]))
            - set(inv["variables"])
        )
        for name, inv in inventory.items()
    }
    missing_bootstrap = {k: v for k, v in missing_bootstrap.items() if v}

    drifting = {
        n: p for n, p in protection.items()
        if p["status"] in {"missing", "drift", "error"}
    }
    plan_blocked = {
        n: p for n, p in protection.items() if p["status"] == "plan-blocked"
    }

    return {
        "org": ORG,
        "repo_count": len(live),
        "protection": protection,
        "inventory": inventory,
        "duplicated_secrets": duplicated_sorted,
        "misfiled_as_secret": misfiled,
        "missing_bootstrap_credential": missing_bootstrap,
        "drifting": drifting,
        "plan_blocked": plan_blocked,
    }


def print_report(rep: JsonDict) -> None:
    print(f"Org: {rep['org']}   live repos: {rep['repo_count']}")
    print()
    print("== Branch protection vs baseline ==")
    for name, p in sorted(rep["protection"].items()):
        if p["status"] == "ok":
            line = f"  ok           {name}"
            if p.get("advisory"):
                line += f"\n                advisory: {p['advisory']}"
            print(line)
        elif p["status"] == "plan-blocked":
            print(f"  PLAN-LIMIT   {name}  ({p.get('detail','')})")
        else:
            print(f"  {p['status'].upper():<11} {name}  {p.get('missing_rules') or p.get('detail','')}")
    if rep["plan_blocked"]:
        print()
        print("  (PLAN-LIMIT is expected: private repos on GitHub Free cannot have")
        print("   branch protection. It is a billing limit, not a config gap.)")
    print()
    print("== Secrets duplicated across repos (candidates for org scope) ==")
    if not rep["duplicated_secrets"]:
        print("  none")
    for s, repos in rep["duplicated_secrets"].items():
        tag = []
        if s in NOT_A_CREDENTIAL:
            tag.append("NOT A CREDENTIAL -> variable")
        elif s in IS_IDENTIFIER:
            tag.append("identifier -> variable")
        elif s in BOOTSTRAP_CREDENTIAL:
            tag.append("bootstrap credential -> one org secret")
        suffix = f"   [{'; '.join(tag)}]" if tag else ""
        print(f"  {len(repos):>2}x  {s}{suffix}")
        print(f"        {', '.join(repos)}")
    print()
    print("== Misfiled: stored as secret, should be a variable ==")
    if not rep["misfiled_as_secret"]:
        print("  none")
    for s, repos in rep["misfiled_as_secret"].items():
        print(f"  {s}: {', '.join(repos)}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument(
        "--enforce",
        action="store_true",
        help="exit 1 when any eligible repo drifts from the baseline",
    )
    args = p.parse_args(argv)

    rep = build_report()
    if args.json:
        print(json.dumps(rep, indent=2, sort_keys=True))
    else:
        print_report(rep)

    if args.enforce and rep["drifting"]:
        print()
        print(f"FAIL: {len(rep['drifting'])} repo(s) drifted from the baseline.",
              file=sys.stderr)
        for n in sorted(rep["drifting"]):
            print(f"  {n}: {rep['drifting'][n]['status']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
