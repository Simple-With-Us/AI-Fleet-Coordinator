#!/usr/bin/env python3
"""PreToolUse hook for Agent / Task / Workflow -- binding enforcement of the
fleet's delegation rules.

WHY THIS IS A HOOK AND NOT A PARAGRAPH
The rules below already exist in CLAUDE.md and AGENT-SYNC.md, and are violated on
essentially every spawn anyway.  That is structural, not forgetful: a subagent
with no `model` INHERITS the session model, nothing in the run output states which
tier was used, and the overspend is only visible afterward by reading agent meta
files.  A default that is invisible AND always wrong in one direction cannot be
fixed by restating it.  Compare the secret-guard hook next to this one: that rule
has never been violated, because it stops being a thing anyone has to remember.

DELEGATION IS THE GOAL, NOT THE COST
None of this discourages spawning agents.  Done right, delegation is faster AND
cheaper AND it keeps the owner's conversation open -- workers grind while the
managing agent stays responsive to chat.  Every deny below exists to stop the
three ways delegation gets done WRONG:
  1. every worker on the frontier tier because nobody chose one
  2. a worker that blocks the main loop, so the owner cannot talk to anyone
  3. two worktrees per agent, paying isolation cost twice for one benefit
The right move is almost always MORE agents at the RIGHT tier, in the background.
"""
import json
import re
import sys

KNOWN_TIERS = {"haiku", "sonnet", "opus", "fable"}


def resolve_tier(raw):
    """Map a model value to its fleet tier.

    Claude Code passes bare tier names (sonnet).  Cursor's Task tool only accepts
    full model slugs, and the Claude slugs embed the tier name
    (claude-sonnet-5-thinking-high).  Composer is Cursor's cheap sibling and
    counts as the haiku tier.  Anything else returns None and is denied.
    """
    m = (raw or "").strip().lower()
    if m in KNOWN_TIERS:
        return m
    for tier in KNOWN_TIERS:
        if tier in m:
            return tier
    if m.startswith("composer"):
        return "haiku"
    return None

TIER_GUIDANCE = (
    "Choose from the shape of the work.  "
    "SMALL (haiku): mechanical and well-specified -- a rename, a fixture update, a doc that "
    "matches an existing format, a mirror sync.  "
    "MID (sonnet): ordinary implementation, and wide-but-shallow sweeps -- audit N files and "
    "apply a known change, fix a failing test, land a routine PR.  This is the DEFAULT; reach "
    "for it before reaching up.  "
    "FRONTIER (opus): design-heavy work, money-path or security-subtle logic, and critical "
    "verification such as checking a diff for credential leakage.  "
    "Verify agents are tiered independently of what they review -- a mechanical build needs "
    "only a mechanical review.  Escalate on a FAILED verification, never on a hunch."
)

DELEGATE_MORE = (
    "This is not a reason to spawn fewer agents.  More workers at the right tier is faster, "
    "cheaper, and keeps this conversation free for the owner while they run."
)

MAKES_OWN_WORKTREE = re.compile(r"git\s+worktree\s+add", re.I)
AGENT_CALL = re.compile(r"\bagent\s*\(")
MODEL_VALUE = re.compile(r"model\s*:\s*['\"]([a-zA-Z0-9._-]+)['\"]")


def deny(reason):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def check_agent(tool_input):
    model = (tool_input.get("model") or "").strip().lower()

    if not model:
        deny(
            "No `model` on this Agent call, so the subagent silently inherits this session's "
            "tier.  That is the single most common way the fleet's model-economy rule gets "
            "broken, and it is invisible in the run output.  Add an explicit `model`.  "
            + TIER_GUIDANCE
            + "  "
            + DELEGATE_MORE
        )

    if resolve_tier(model) is None:
        deny(
            f"`model: \"{model}\"` is not a tier this tool accepts.  Use one of: "
            f"{', '.join(sorted(KNOWN_TIERS))} -- or a Cursor model slug containing one "
            f"(claude-sonnet-5-thinking-high); composer-2.5-fast counts as haiku.  "
            + TIER_GUIDANCE
        )

    # Blocking the main loop is the one thing that costs the owner their own
    # session: they cannot talk to the managing agent while it sits on a worker.
    if tool_input.get("run_in_background") is False:
        deny(
            "`run_in_background: false` blocks this session until the worker finishes, which "
            "takes away the owner's ability to chat with the managing agent while work happens "
            "-- the main reason to delegate at all.  Leave it in the background and do other "
            "useful work; you are notified on completion.  Pass false ONLY when the very next "
            "action genuinely cannot proceed without the result AND nothing else could run."
        )

    if tool_input.get("isolation") == "worktree":
        prompt = tool_input.get("prompt", "") or ""
        if MAKES_OWN_WORKTREE.search(prompt):
            deny(
                "This sets isolation:\"worktree\" AND tells the agent to run `git worktree add` "
                "itself, so one agent gets two worktrees -- the harness's, plus the lane it "
                "actually works in.  The first is then pure setup cost and disk.  Keep exactly "
                "one: isolation when the harness should own the checkout, or the instruction "
                "when the agent needs a named lane under ~/apps/lanes/<Repo>/."
            )


def check_workflow(tool_input):
    script = tool_input.get("script", "") or ""
    if not script:
        return  # saved workflow by name, or scriptPath -- nothing to inspect

    agent_calls = len(AGENT_CALL.findall(script))
    if not agent_calls:
        return

    models = [m.lower() for m in MODEL_VALUE.findall(script)]

    if not models:
        deny(
            f"This workflow spawns agents ({agent_calls} agent() call sites) and assigns no "
            "model anywhere, so every worker inherits this session's tier.  Set opts.model per "
            "agent().  " + TIER_GUIDANCE + "  " + DELEGATE_MORE
        )

    unknown = sorted(set(models) - KNOWN_TIERS)
    if unknown:
        deny(
            f"Unrecognized model tier(s) in this workflow: {', '.join(unknown)}.  "
            f"Use one of: {', '.join(sorted(KNOWN_TIERS))}."
        )

    # Everything on the frontier across a multi-agent fan-out means the tiers
    # were not actually considered -- a real fan-out has mechanical lanes in it.
    frontier = models.count("opus")
    if agent_calls >= 3 and frontier == len(models) and len(models) >= 2:
        deny(
            f"Every model in this workflow is `opus` across {agent_calls} agent() call sites.  "
            "A genuine fan-out has cheap lanes in it -- doc edits, fixture updates, mechanical "
            "reviews -- so an all-frontier assignment means the tiers were inherited in spirit "
            "even though they were typed out.  Re-tier per lane, keeping opus for the "
            "design-heavy or security-subtle ones and the critical verifications.  "
            + TIER_GUIDANCE
        )

    if "isolation: 'worktree'" in script or 'isolation: "worktree"' in script:
        if MAKES_OWN_WORKTREE.search(script):
            deny(
                "This workflow sets isolation:\"worktree\" AND its prompts tell agents to run "
                "`git worktree add`, so every agent gets two worktrees.  Drop one.  Isolation is "
                "also unnecessary for lanes that touch DIFFERENT repos, or that only read."
            )
        if agent_calls == 1:
            deny(
                "isolation:\"worktree\" on a single-agent workflow isolates that agent from "
                "nothing.  Worktrees cost setup time and disk -- use them only when parallel "
                "agents would otherwise write the same checkout."
            )


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # never block on a hook bug

    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input", {}) or {}

    if tool in ("Agent", "Task"):
        check_agent(tool_input)
    elif tool == "Workflow":
        check_workflow(tool_input)

    sys.exit(0)


if __name__ == "__main__":
    main()
