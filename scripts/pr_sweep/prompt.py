"""prompt: the full worker prompt for ANY agent, rendered from rules.md (the one source of the rules).

worker_prompt(pr, seat, platform, ...)   the task header for one PR plus every rule and the result format
deploy_prompt(full, merged, seat)        the deploy check for one repo after merges
common_rules(seat, platform)             the identity, lane, forbidden and writing rules only (side tasks)
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .fleet import SeatInfo

RULES_PATH = Path(__file__).resolve().parent / "rules.md"
COMMON_RULES = (1, 2, 8, 13)
DEFAULT_TRAILER = (
    "End every commit message with your platform's usual co-author trailer, if it has one."
)
_PLACEHOLDER = re.compile(r"\{\{([A-Z_]+)\}\}")


class RulesError(ValueError):
    pass


def load_sections(path: Path = RULES_PATH) -> dict[str, str]:
    """{'RULES': ..., 'RESULT': ..., 'DEPLOY': ...} with the maintainer comment removed."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^## ([A-Z]+)\s*$", line)
        if m:
            current = m.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    out = {k: "\n".join(v).strip() for k, v in sections.items()}
    for need in ("RULES", "RESULT", "DEPLOY"):
        if not out.get(need):
            raise RulesError(f"{path} has no ## {need} section")
    return out


def parse_rules(block: str) -> dict[int, str]:
    """{number: text} from the RULES section (a rule may run over several lines)."""
    rules: dict[int, list[str]] = {}
    cur: int | None = None
    for line in block.splitlines():
        m = re.match(r"^(\d+)\. ", line)
        if m:
            cur = int(m.group(1))
            rules[cur] = [line]
        elif cur is not None and line.strip():
            rules[cur].append(line)
    return {n: "\n".join(v) for n, v in rules.items()}


def fill(text: str, values: Mapping[str, str]) -> str:
    def sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values:
            raise RulesError(f"rules.md uses {{{{{name}}}}}, which pr-sweep does not fill")
        return values[name]

    return _PLACEHOLDER.sub(sub, text)


def _values(
    seat: SeatInfo, trailer: str | None, extra: Mapping[str, str] | None = None
) -> dict[str, str]:
    vals = {
        "SEAT": seat.tag,
        "BRANCH_PREFIX": seat.branch_prefix,
        "TRAILER_RULE": trailer or DEFAULT_TRAILER,
        "CLI": "pr-sweep",
    }
    vals.update(extra or {})
    return vals


def rules_text(
    seat: SeatInfo, trailer: str | None = None, only: Sequence[int] | None = None
) -> str:
    sections = load_sections()
    header, _, _ = sections["RULES"].partition("\n")
    rules = parse_rules(sections["RULES"])
    picked = [rules[n] for n in sorted(rules) if only is None or n in only]
    return fill("\n".join([header] + picked), _values(seat, trailer))


def common_rules(seat: SeatInfo, trailer: str | None = None) -> str:
    return rules_text(seat, trailer, COMMON_RULES)


def stack_note(
    pr: Mapping[str, Any], parent: Mapping[str, Any] | None, default_branch: str = "main"
) -> str:
    if (pr.get("base") or default_branch) == default_branch:
        return ""
    par = f" (PR #{parent['n']})" if parent else ""
    return (
        f"STACK: this PR is stacked on branch {pr.get('base')}{par}, not on {default_branch}.  If the parent PR is "
        "still open, resolve conflicts and threads against the base branch and push, but do NOT merge this PR "
        'into the parent branch: report outcome blocked with the blocker "waiting on parent".  If the parent has '
        f"merged, retarget with gh pr edit <N> --base {default_branch}, merge origin/{default_branch}, resolve, "
        "and continue normally."
    )


def worker_prompt(
    pr: Mapping[str, Any],
    seat: SeatInfo,
    *,
    trailer: str | None = None,
    mode: str = "merge",
    round_no: int = 1,
    parent: Mapping[str, Any] | None = None,
    code_dir: str | None = None,
    default_branch: str = "main",
    ours_until: str | None = None,
) -> str:
    """`ours_until` (an ISO UTC time) tells a repair-round worker that activity up to then is the sweep's own
    earlier worker, so the freshness guard (rule 4) applies only to activity after it."""
    full, n = pr["full"], pr["n"]
    snap = (
        f"mergeStateStatus={pr.get('mstate', '?')} review={pr.get('review') or 'none'} "
        f"unresolvedThreads={pr.get('unresolved', '?')} ci={pr.get('ci', '?')} autoMerge={pr.get('auto', '?')} "
        f"updatedAt(UTC)={pr.get('updated') or '?'}"
    )
    lines = [
        f"TASK: bring {full} PR #{n} to a state with no merge conflicts and no unresolved review threads, and land it "
        "(merge by default; apply the owner-needed test in rule 10).",
        f"PR: https://github.com/{full}/pull/{n}   title: {pr.get('title', '')}",
        f"head: {pr.get('head', '?')}   base: {pr.get('base', '?')}   author: {pr.get('author', '?')}   "
        f"labels: {','.join(pr.get('labels') or []) or 'none'}",
        f"Repo folder under ~/Code (for lane new): {code_dir or full.split('/', 1)[1]}",
        f"Snapshot when this prompt was made (stale, re-check everything): {snap}",
        f"MODE: {mode}"
        + (
            f"   (repair round {round_no}: an earlier worker already worked this PR; read its commits "
            "and replies first and do not redo or contradict them)"
            if round_no > 1
            else ""
        ),
    ]
    note = stack_note(pr, parent, default_branch)
    if note:
        lines.append(note)
    sections = load_sections()
    rules = rules_text(seat, trailer)
    if ours_until:
        rules = rules.replace(
            "4. FRESHNESS GUARD:",
            f"4. FRESHNESS GUARD (activity up to {ours_until} is this sweep's "
            "own earlier worker; apply the guard only to activity after that):",
            1,
        )
    lines += ["", rules, "", fill(sections["RESULT"], _values(seat, trailer))]
    return "\n".join(lines) + "\n"


def deploy_prompt(full: str, merged: Sequence[Mapping[str, Any]], seat: SeatInfo) -> str:
    sections = load_sections()
    listing = (
        "; ".join(f"#{p['n']} {p.get('title', '')}".strip() for p in merged) or "(none listed)"
    )
    return fill(sections["DEPLOY"], _values(seat, None, {"REPO": full, "MERGED": listing})) + "\n"
