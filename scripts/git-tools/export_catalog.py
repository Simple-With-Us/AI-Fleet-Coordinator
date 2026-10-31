#!/usr/bin/env python3
"""Regenerate catalog.json from a captured live tools/list of the hosted simple-tools MCP git tools.

The catalog's schema authority is the DEPLOYED server's own tools/list answer (the repo's source modules can
lag the deployment), captured by the host with its own seat and supplied privately.  This script neutralizes
the caller-relative parts (the identity enum names the capturing seat; the description's fallback seat does
too) and tags each tool read/write from the relay's workflow read-action set.  It performs no network access.

Usage:  python3 export_catalog.py CAPTURED_TOOLS_LIST_JSON > catalog.json
"""

import json
import re
import sys

# The relay's workflow READ actions (gr-workflow-v1.js WF_V1_READ_ACTIONS at capture time), plus the two typed
# read tools.  Everything else the hosted server lists under gr_ is a write for transport purposes:  write
# uncertainty wording, identity injection and no-retry are the conservative defaults.
WF_V1_READ_ACTIONS = ["wf_pr_commits_page", "wf_pr_review_get", "wf_release_latest", "wf_release_by_tag", "wf_commit_comments_page", "wf_commit_comment_get", "wf_pr_requested_reviewers", "wf_repo_tags", "wf_repo_languages", "wf_pr_list_page", "wf_compare_commits_page", "wf_pr_reviews_page", "wf_pr_comments_page", "wf_check_runs_page", "wf_check_run_get", "wf_file_history", "wf_runs_page", "wf_workflows_page", "wf_pr_files_page", "wf_issue_parent", "wf_issue_subissues", "wf_issue_blocked_by", "wf_issue_blocking", "wf_commit_statuses", "wf_git_blob_get", "wf_git_commit_get", "wf_git_tree_get", "wf_release_asset_download", "wf_job_log", "wf_run_logs", "wf_artifact_download", "wf_milestones", "wf_milestone_get", "wf_deployments", "wf_deployment_get", "wf_deployment_statuses", "wf_environments", "wf_environment_get", "wf_release_list", "wf_release_get", "wf_tag_get", "wf_review_threads", "wf_issue_list", "wf_issue_get", "wf_pr_get", "wf_pr_reviews", "wf_pr_comments", "wf_issue_comments", "wf_commit_get", "wf_compare", "wf_branch_get", "wf_repo_get", "wf_labels", "wf_workflows", "wf_run_get", "wf_run_artifacts", "wf_release_assets", "wf_release_asset_get", "wf_pr_list", "wf_pr_files", "wf_file_get", "wf_tree", "wf_branches", "wf_commits", "wf_code_search", "wf_runs", "wf_run_jobs", "wf_checks", "wf_merge_queue_entry", "wf_job_get", "wf_workflow_get", "wf_artifact_get", "wf_artifacts_page", "wf_caches_page", "wf_cache_usage", "wf_run_attempt_get", "wf_run_attempt_jobs", "wf_run_pending_deployments", "wf_repo_variables", "wf_repo_variable_get", "wf_issue_events", "wf_issue_timeline", "wf_git_blob_binary_get"]
READ_TOOLS = frozenset(
    ["gr_v1_pr_state", "gr_v1_pr_hold_state"] + ["gr_" + a for a in WF_V1_READ_ACTIONS]
)

# Per-action input byte limits (gr-workflow-v1.js WF_V1_INPUT_LIMITS; default 64 KiB).
INPUT_LIMITS = {
    "gr_wf_release_asset_upload_binary": 253952,
    "gr_wf_file_put": 6 * 1024 * 1024,
    "gr_wf_files_put": 6 * 1024 * 1024,
}
DEFAULT_INPUT_LIMIT = 65536

IDENTITY_DESCRIPTION = (
    "The bot this call acts as.  Writes must name it;  reads without it act as the calling seat.  "
    "identities lists the set."
)


def neutralize(schema):
    """Drop the caller-relative identity enum and reword its description;  leave every other field byte-equal."""
    out = dict(schema)
    props = dict(out.get("properties") or {})
    if "identity" in props:
        ident = dict(props["identity"])
        ident.pop("enum", None)
        ident["description"] = IDENTITY_DESCRIPTION
        props["identity"] = ident
        out["properties"] = props
    return out


def main(argv):
    if len(argv) != 1:
        sys.stderr.write(__doc__)
        return 2
    captured = json.load(open(argv[0]))
    tools = captured["tools"] if isinstance(captured, dict) else captured
    catalog = []
    for tool in sorted(tools, key=lambda t: t["name"]):
        name = tool["name"]
        if not re.fullmatch(r"gr_[a-z0-9_]{1,80}", name):
            continue
        entry = {
            "name": name,
            "description": tool.get("description", ""),
            "read": name in READ_TOOLS,
            "input_bytes": INPUT_LIMITS.get(name, DEFAULT_INPUT_LIMIT),
            "schema": neutralize(tool.get("inputSchema") or {}),
        }
        if tool.get("title"):
            entry["title"] = tool["title"]
        catalog.append(entry)
    if not catalog:
        sys.stderr.write("no gr_* tools in the capture\n")
        return 1
    json.dump(
        {
            "provenance": "hosted simple-tools MCP tools/list, captured by the host's own seat 2026-10-31; "
            "identity enum neutralized (caller-relative); list presence is not execution proof",
            "tools": catalog,
        },
        sys.stdout,
        indent=1,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
