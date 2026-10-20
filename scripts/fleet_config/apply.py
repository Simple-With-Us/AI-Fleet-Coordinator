"""The write side:  runs a plan through the existing installer engines.

``apply_plan`` never writes a file itself.  Every change goes through the installer that
already owns that surface,  so the backup and atomic-replace discipline, the refusal
rules and the verification stay in one place,  and this package adds no third installer.

The rules, all enforced here:

  * only a rule whose manifest entry sets ``autofixAllowed`` may be changed,  and only
    when an installer exists for it;  everything else is listed and skipped
  * a persistence-class write (hooks and plugins) needs a Touch ID confirmation,  and the
    confirmation is asked for once before the first write;  a refused confirmation refuses
    the whole run,  so nothing is written without a human
  * nothing is written outside the home the caller names,  and no file outside the
    manifest's declared targets is touched
  * the run is idempotent:  a second apply of the same plan has nothing left to do
  * after the writes,  the status runs again,  so the caller sees the new state

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from . import plan as plan_module
from . import touchid
from .aggregator import run_status
from .checks import CheckContext, CheckRegistry, resolve_home, utc_now
from .types import (
    ConfigKind,
    Manifest,
    OverallVerdict,
    StatusReport,
)

__all__ = [
    "ApplyResult",
    "ApplyStep",
    "InstallerRunner",
    "TouchIdGate",
    "apply_plan",
    "default_runner",
]

# The seconds an installer may take before the run calls it a failure.
DEFAULT_TIMEOUT = 300

# The outcome tokens a step can have.
OUTCOME_APPLIED = "applied"
OUTCOME_ALREADY_CURRENT = "already-current"
OUTCOME_REFUSED = "refused"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"

# A runner is called with the full argv and answers (returncode, stdout, stderr).  Tests
# inject one,  so no test ever starts a real installer.
InstallerRunner = Callable[[Sequence[str]], tuple[int, str, str]]

# A Touch ID gate is called with the sentence the owner reads and raises to refuse.
TouchIdGate = Callable[[str], None]


@dataclass(frozen=True)
class ApplyStep:
    """One rule the apply side acted on,  or did not."""

    rule_id: str
    seat: str
    platform: str
    kind: ConfigKind
    command: tuple[str, ...]
    files: tuple[str, ...]
    outcome: str
    detail: str
    persistence_class: bool = False

    @property
    def failed(self) -> bool:
        """True when the step did not do what it was asked to do."""
        return self.outcome in (OUTCOME_FAILED, OUTCOME_REFUSED)


@dataclass(frozen=True)
class ApplyResult:
    """What one apply did,  and the status that came after it."""

    seat: str | None
    steps: tuple[ApplyStep, ...]
    report: StatusReport | None = None
    verdict: OverallVerdict | None = None
    refused: bool = False
    refusal_reason: str = ""

    @property
    def applied(self) -> tuple[ApplyStep, ...]:
        """The steps that wrote something."""
        return tuple(step for step in self.steps if step.outcome == OUTCOME_APPLIED)

    @property
    def failed(self) -> tuple[ApplyStep, ...]:
        """The steps that failed or were refused."""
        return tuple(step for step in self.steps if step.failed)

    @property
    def skipped(self) -> tuple[ApplyStep, ...]:
        """The steps that were not eligible."""
        return tuple(step for step in self.steps if step.outcome == OUTCOME_SKIPPED)

    def render(self) -> str:
        """The apply as text for a human."""
        lines: list[str] = []
        lines.append(
            "Applied %d change%s;  %d skipped,  %d failed."
            % (
                len(self.applied),
                "" if len(self.applied) == 1 else "s",
                len(self.skipped),
                len(self.failed),
            )
        )
        for step in self.steps:
            lines.append(
                "  %-16s %s  %s  %s" % (step.outcome, step.rule_id, step.kind.value, step.platform)
            )
            if step.command:
                lines.append("      command:  %s" % " ".join(step.command))
            for path in step.files:
                lines.append("      file:  %s" % path)
            if step.detail:
                lines.append("      %s" % step.detail)
        if self.refused:
            lines.append("  REFUSED:  %s" % self.refusal_reason)
        if self.verdict is not None:
            lines.append("  status after the apply:  %s" % self.verdict.value)
        return "\n".join(lines) + "\n"


def default_runner(argv: Sequence[str], timeout: int = DEFAULT_TIMEOUT) -> tuple[int, str, str]:
    """Run an installer as a child process and answer (returncode, stdout, stderr).

    The child inherits the environment,  so the installer sees the same PATH and HOME the
    caller had.  A timeout is a failure,  not a hang.
    """
    try:
        result = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return 124, "", "the installer did not finish within %d seconds" % timeout
    except OSError as exc:
        return 127, "", "the installer could not be started: %s" % exc
    return (
        result.returncode,
        result.stdout.decode("utf-8", "replace") if result.stdout else "",
        result.stderr.decode("utf-8", "replace") if result.stderr else "",
    )


def _resolve_target(path: str, home: str) -> str:
    """Expand a manifest target into a real path under `home`.

    A relative path is relative to the home,  because that is how the plan names the
    files an installer touches.
    """
    text = path.replace("$HOME", home)
    if text.startswith("~/"):
        text = os.path.join(home, text[2:])
    elif text == "~":
        text = home
    elif not os.path.isabs(text):
        text = os.path.join(home, text)
    return text


def _outside_home(path: str, home: str) -> bool:
    """True when a path resolves outside the home the caller named."""
    target = os.path.realpath(_resolve_target(path, home))
    root = os.path.realpath(home)
    return not (target == root or target.startswith(root + os.sep))


def _touchid_gate_reason(entry: plan_module.PlanEntry) -> str:
    """The sentence the owner reads on the Touch ID prompt."""
    return "Apply the %s change for %s on %s" % (
        entry.kind.value,
        entry.rule_id,
        entry.platform,
    )


def apply_plan(
    plan: plan_module.Plan,
    *,
    home: str | None = None,
    runner: InstallerRunner | None = None,
    touchid_gate: TouchIdGate | None = None,
    manifest: Manifest | None = None,
    registry: CheckRegistry | None = None,
    ctx: CheckContext | None = None,
    recheck: bool = True,
    timeout: int = DEFAULT_TIMEOUT,
) -> ApplyResult:
    """Apply `plan` through the existing installers and return what happened.

    ``home`` is the directory the installers write in;  it defaults to the caller's home
    and must exist.  ``runner`` is the callable that starts an installer;  the default one
    runs it as a child process,  and a test injects its own so no test ever starts a real
    installer.  ``touchid_gate`` asks for the human confirmation;  the default is
    ``touchid.confirm``,  which refuses when the confirmation does not succeed.

    ``manifest`` and ``registry`` are used for the status run that follows the writes;  a
    plan applied without them reports no new status.
    """
    target_home = str(resolve_home(home))
    run = runner if runner is not None else (lambda argv: default_runner(argv, timeout))
    gate = touchid_gate if touchid_gate is not None else touchid.confirm
    steps: list[ApplyStep] = []
    refused = False
    refusal_reason = ""

    eligible = [entry for entry in plan.entries if entry.auto_fixable]
    persistence = [entry for entry in eligible if entry.persistence_class]
    if persistence:
        try:
            gate(_touchid_gate_reason(persistence[0]))
        except touchid.TouchIdError as exc:
            refused = True
            refusal_reason = str(exc)
            for entry in plan.entries:
                steps.append(
                    ApplyStep(
                        rule_id=entry.rule_id,
                        seat=entry.seat,
                        platform=entry.platform,
                        kind=entry.kind,
                        command=(),
                        files=entry.files,
                        outcome=OUTCOME_REFUSED if entry.auto_fixable else OUTCOME_SKIPPED,
                        detail=str(exc) if entry.auto_fixable else entry.blocked_reason,
                        persistence_class=entry.persistence_class,
                    )
                )
            return ApplyResult(
                seat=plan.seat,
                steps=tuple(steps),
                report=None,
                verdict=None,
                refused=True,
                refusal_reason=refusal_reason,
            )

    for entry in plan.entries:
        if not entry.auto_fixable or entry.installer is None:
            steps.append(
                ApplyStep(
                    rule_id=entry.rule_id,
                    seat=entry.seat,
                    platform=entry.platform,
                    kind=entry.kind,
                    command=(),
                    files=entry.files,
                    outcome=OUTCOME_SKIPPED,
                    detail=entry.blocked_reason or "no installer can make this change",
                    persistence_class=entry.persistence_class,
                )
            )
            continue
        outside = [path for path in entry.files if _outside_home(path, target_home)]
        if outside:
            steps.append(
                ApplyStep(
                    rule_id=entry.rule_id,
                    seat=entry.seat,
                    platform=entry.platform,
                    kind=entry.kind,
                    command=(),
                    files=entry.files,
                    outcome=OUTCOME_REFUSED,
                    detail="the file %s is outside the home %s;  the write is refused"
                    % (outside[0], target_home),
                    persistence_class=entry.persistence_class,
                )
            )
            refused = True
            refusal_reason = refusal_reason or "a target is outside the home"
            continue
        argv = list(entry.installer.argv) + ["--home", target_home]
        code, stdout, stderr = run(argv)
        if code == 0:
            outcome = OUTCOME_APPLIED
            detail = (
                (stdout or stderr).strip().splitlines()[-1] if (stdout or stderr).strip() else ""
            )
        else:
            outcome = OUTCOME_FAILED
            detail = (stderr or stdout).strip() or "the installer exited %d" % code
        steps.append(
            ApplyStep(
                rule_id=entry.rule_id,
                seat=entry.seat,
                platform=entry.platform,
                kind=entry.kind,
                command=tuple(argv),
                files=entry.files,
                outcome=outcome,
                detail=detail,
                persistence_class=entry.persistence_class,
            )
        )

    report: StatusReport | None = None
    if recheck and manifest is not None:
        # The status that follows the writes is its own run,  so it gets its own clock.
        timeout = ctx.timeout if ctx is not None else DEFAULT_TIMEOUT
        context = CheckContext(home=target_home, now=utc_now(), timeout=timeout)
        report = run_status(
            manifest,
            seat=plan.seat,
            kind=plan.kind,
            platform=plan.platform,
            ctx=context,
            registry=registry,
        )
    return ApplyResult(
        seat=plan.seat,
        steps=tuple(steps),
        report=report,
        verdict=report.summary.overall_verdict if report is not None else None,
        refused=refused,
        refusal_reason=refusal_reason,
    )
