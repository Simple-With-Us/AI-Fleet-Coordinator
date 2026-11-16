"""Scan an org (or one repo) for default-branch CI failure streaks."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

try:
    from . import github
except ImportError:
    import github

SCHEMA_VERSION = 1

DEFAULT_ORG = "Simple-With-Us"
DEFAULT_MAX_RUNS = 15


def _repo_access_error(owner: str, repo: str, exc: github.GitHubError, *, context: str) -> str:
    full = f"{owner}/{repo}"
    status = exc.status
    if status == 404:
        return f"{full}: not found (404) while reading {context}"
    if status == 403:
        # GitHub uses 403 for both missing permission and exhausted rate limits.
        return f"{full}: HTTP 403 (forbidden or rate limited) while reading {context}"
    # Only allow-listed fields: never interpolate the exception text or body.
    return f"{full}: cannot read {context} ({type(exc).__name__}, status {status})"


@dataclass(frozen=True)
class RedWorkflow:
    repo: str
    workflow_name: str
    workflow_path: str
    streak_length: int
    first_red_run_id: int
    first_red_run_url: str
    first_red_run_created_at: str
    first_red_commit_sha: str
    latest_run_id: int
    latest_run_url: str
    latest_run_created_at: str
    latest_commit_sha: str
    failing_job_name: str
    failing_step_name: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "workflow_name": self.workflow_name,
            "workflow_path": self.workflow_path,
            "streak_length": self.streak_length,
            "first_red_run_id": self.first_red_run_id,
            "first_red_run_url": self.first_red_run_url,
            "first_red_run_created_at": self.first_red_run_created_at,
            "first_red_commit_sha": self.first_red_commit_sha,
            "latest_run_id": self.latest_run_id,
            "latest_run_url": self.latest_run_url,
            "latest_run_created_at": self.latest_run_created_at,
            "latest_commit_sha": self.latest_commit_sha,
            "failing_job_name": self.failing_job_name,
            "failing_step_name": self.failing_step_name,
        }


def streak_from_runs(runs: Sequence[dict[str, Any]]) -> tuple[int, list[dict[str, Any]]]:
    """Walk newest-first runs; count leading failures; ignore skipped/cancelled/neutral."""
    streak: list[dict[str, Any]] = []
    for run in runs:
        conclusion = run.get("conclusion")
        kind = github.classify_run_conclusion(conclusion if isinstance(conclusion, str) else None)
        if kind == "ignore":
            continue
        if kind == "success":
            break
        if kind == "fail":
            streak.append(run)
            continue
        # Unknown conclusion: stop counting so we do not invent streaks.
        break
    return len(streak), streak


def scan_repo(
    client: github.GitHubClient,
    owner: str,
    repo: str,
    *,
    max_runs_per_workflow: int,
    errors: list[str],
) -> tuple[list[RedWorkflow], int]:
    """Return red workflows for one repo and the number of workflows scanned."""
    repo_path = f"/repos/{owner}/{repo}"
    try:
        meta = client.get_json(repo_path)
    except github.GitHubError as exc:
        errors.append(_repo_access_error(owner, repo, exc, context="repo"))
        return [], 0
    if not isinstance(meta, dict):
        errors.append(f"{owner}/{repo}: repo metadata is not an object")
        return [], 0
    if meta.get("archived"):
        return [], 0
    default_branch = meta.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch:
        errors.append(f"{owner}/{repo}: missing default_branch")
        return [], 0

    try:
        wf_list = client.get_json(f"{repo_path}/actions/workflows", params={"per_page": "100"})
    except github.GitHubError as exc:
        errors.append(_repo_access_error(owner, repo, exc, context="workflows"))
        return [], 0
    workflows = wf_list.get("workflows") if isinstance(wf_list, dict) else None
    if not isinstance(workflows, list):
        return [], 0

    red_rows: list[RedWorkflow] = []
    scanned = 0
    for wf in workflows:
        if not isinstance(wf, dict):
            continue
        if wf.get("state") != "active":
            continue
        wf_id = wf.get("id")
        if not isinstance(wf_id, int):
            continue
        scanned += 1
        wf_name = str(wf.get("name") or "")
        wf_path = str(wf.get("path") or "")
        try:
            runs_data = client.get_json(
                f"{repo_path}/actions/workflows/{wf_id}/runs",
                params={
                    "branch": default_branch,
                    "status": "completed",
                    "per_page": str(min(100, max_runs_per_workflow)),
                },
            )
        except github.GitHubError as exc:
            errors.append(f"{owner}/{repo} workflow {wf_name}: cannot list runs ({exc})")
            continue
        runs = runs_data.get("workflow_runs") if isinstance(runs_data, dict) else None
        if not isinstance(runs, list) or not runs:
            continue
        # API returns newest first; cap after fetch for early-stop semantics in tests.
        trimmed = runs[:max_runs_per_workflow]
        length, streak_runs = streak_from_runs(trimmed)
        if length == 0:
            continue
        latest = streak_runs[0]
        oldest = streak_runs[-1]
        job_name, step_name = "", ""
        latest_id = latest.get("id")
        if isinstance(latest_id, int):
            try:
                jobs = client.get_json(
                    f"{repo_path}/actions/runs/{latest_id}/jobs", params={"per_page": "100"}
                )
                job_name, step_name = github.pick_failing_job_step(
                    jobs if isinstance(jobs, dict) else {}
                )
            except github.GitHubError as exc:
                errors.append(f"{owner}/{repo} run {latest_id}: cannot list jobs ({exc})")
        red_rows.append(
            RedWorkflow(
                repo=repo,
                workflow_name=wf_name,
                workflow_path=wf_path,
                streak_length=length,
                first_red_run_id=int(oldest.get("id") or 0),
                first_red_run_url=str(oldest.get("html_url") or ""),
                first_red_run_created_at=str(oldest.get("created_at") or ""),
                first_red_commit_sha=str(oldest.get("head_sha") or ""),
                latest_run_id=int(latest.get("id") or 0),
                latest_run_url=str(latest.get("html_url") or ""),
                latest_run_created_at=str(latest.get("created_at") or ""),
                latest_commit_sha=str(latest.get("head_sha") or ""),
                failing_job_name=job_name,
                failing_step_name=step_name,
            )
        )
    return red_rows, scanned


def list_org_repos(client: github.GitHubClient, org: str) -> list[str]:
    names: list[str] = []
    for repo in client.paginate(f"/orgs/{org}/repos", params={"type": "all"}):
        if isinstance(repo, dict) and not repo.get("archived"):
            name = repo.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def build_report(
    client: github.GitHubClient,
    *,
    org: str,
    repos: Iterable[str] | None = None,
    max_runs_per_workflow: int = DEFAULT_MAX_RUNS,
    visibility_note: str = "",
) -> dict[str, Any]:
    errors: list[str] = []
    target_repos = list(repos) if repos is not None else list_org_repos(client, org)
    all_red: list[RedWorkflow] = []
    workflows_scanned = 0
    repos_scanned = 0
    for repo in target_repos:
        reds, wf_count = scan_repo(
            client,
            org,
            repo,
            max_runs_per_workflow=max_runs_per_workflow,
            errors=errors,
        )
        repos_scanned += 1
        workflows_scanned += wf_count
        all_red.extend(reds)
    all_red.sort(key=lambda row: (-row.streak_length, row.repo, row.workflow_path))
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "org": org,
        "repos_scanned": repos_scanned,
        "workflows_scanned": workflows_scanned,
        "red_count": len(all_red),
        "visibility_note": visibility_note,
        "red_workflows": [row.to_dict() for row in all_red],
        "errors": errors,
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# CI red streak report")
    lines.append("")
    lines.append(f"- Org: `{report.get('org')}`")
    lines.append(f"- Generated: {report.get('generated_at')}")
    lines.append(f"- Repos scanned: {report.get('repos_scanned')}")
    lines.append(f"- Workflows scanned: {report.get('workflows_scanned')}")
    lines.append(f"- Red workflows: {report.get('red_count')}")
    note = report.get("visibility_note")
    if isinstance(note, str) and note.strip():
        lines.append(f"- Note: {note.strip()}")
    lines.append("")
    reds = report.get("red_workflows")
    if not isinstance(reds, list) or not reds:
        lines.append("_No failing streaks on default branch._")
        lines.append("")
        return "\n".join(lines)
    lines.append("| Repo | Workflow | Streak | Latest run | Failing job / step |")
    lines.append("| --- | --- | ---: | --- | --- |")
    for row in reds:
        if not isinstance(row, dict):
            continue
        wf = f"{row.get('workflow_name')} (`{row.get('workflow_path')}`)"
        latest = row.get("latest_run_url") or ""
        job = row.get("failing_job_name") or ""
        step = row.get("failing_step_name") or ""
        fail = job + (f" / {step}" if step else "")
        lines.append(
            f"| {row.get('repo')} | {wf} | {row.get('streak_length')} | {latest} | {fail} |"
        )
    lines.append("")
    return "\n".join(lines)
