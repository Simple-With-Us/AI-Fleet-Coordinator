"""run: sweep the open PRs with any platform's agent CLI, one worker per PR.

    pr-sweep run --platform codex --dry-run            # the plan, each decision, and the exact commands
    pr-sweep run --platform codex --apply [--auto-approve] [--repos BotFleet,DealDex] [--prs O/R#1,...]

What it does, in order:
  1. inventory (or the --prs list), plan (skip drafts, deferred and fork PRs; stacks kept together; chains);
  2. per chain, one PR after another; chains (so repos) in parallel, at most --max-parallel at once;
  3. for each PR, right before launching: the CURRENT status from GitHub (status.py).  No status: nothing is
     launched for it and the run ends 69 (a missing status is never "needs work").  Activity in the last 15
     minutes by anyone but this sweep: in-flight-skipped.  needs_work false: nothing to do;
  4. otherwise it launches the platform's agent command (platforms.json) with the worker prompt (prompt.py,
     rendered from rules.md) and waits (--agent-timeout).  The outcome is read back from GitHub, not from the
     agent's answer.  A PR that merged gets `lane retire pr<N>-sweep` for the worker's lane;
  5. repair rounds (--rounds, default 3 in all) for PRs that still need work;
  6. the deterministic audit (audit.py) over every PR it touched, and one deploy-check worker per repo that
     merged something (rules.md ## DEPLOY), unless --no-deploy.

The workers inherit AGENT_SEAT; this never sets AGENT_LAUNCH_SEAT or AGENT_LAUNCHER.  Agent output goes to
~/apps/scratch/<seat>/pr-sweep/<stamp>/ (0700).  --dry-run launches nothing, writes nothing and calls nothing but
read-only gh queries.
"""

from __future__ import annotations

import concurrent.futures
import datetime as _dt
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import audit as A
from . import plan as P
from . import prompt as PR
from . import status as S
from .fleet import Fleet, SeatInfo
from .gh import Gh

PLATFORMS_PATH = Path(__file__).resolve().parent / "platforms.json"
DEFAULT_TIMEOUT = 2700.0
DEFAULT_PARALLEL = 4
DEFAULT_ROUNDS = 3

LaunchFn = Callable[[Sequence[str], "str | None", float, Path], "tuple[int, str]"]
LaneFn = Callable[[Sequence[str]], "tuple[int, str]"]


class RunError(RuntimeError):
    pass


def load_platforms(path: Path = PLATFORMS_PATH) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return data


def platform(name: str, path: Path = PLATFORMS_PATH) -> dict[str, Any]:
    data = load_platforms(path)
    plats = data.get("platforms") or {}
    if name in plats:
        return plats[name]
    chat = data.get("chat_only") or {}
    if name in chat:
        raise RunError(f"{name} has no headless CLI: {chat[name]}")
    raise RunError(
        f"unknown platform {name!r}; known: {', '.join(sorted(plats))} (chat only: {', '.join(sorted(chat))})"
    )


def build_command(
    cfg: Mapping[str, Any],
    prompt_text: str,
    *,
    tier: str = "mid",
    auto_approve: bool = False,
    prompt_file: Path | None = None,
    agent_cmd: str | None = None,
) -> tuple[list[str], str | None]:
    """(argv, stdin text).  `agent_cmd` (a shell-quoted template) replaces the platform argv when given."""
    argv = shlex.split(agent_cmd) if agent_cmd else list(cfg.get("argv") or [])
    if not argv:
        raise RunError("the platform template has no argv")
    model = (cfg.get("models") or {}).get(tier)
    extra: list[str] = []
    if model and cfg.get("model_flag"):
        extra += [cfg["model_flag"], model]
    if auto_approve:
        extra += list(cfg.get("auto_flags") or [])
    # options go right after the subcommand words, before any {prompt} or {prompt_file} argument
    tail_at = next((i for i, a in enumerate(argv) if "{prompt" in a), len(argv))
    argv = argv[:tail_at] + extra + argv[tail_at:]
    uses_stdin = bool(cfg.get("stdin")) and not any("{prompt" in a for a in argv)
    out: list[str] = []
    for a in argv:
        if "{prompt_file}" in a:
            if prompt_file is None:
                raise RunError("this platform needs a prompt file")
            out.append(a.replace("{prompt_file}", str(prompt_file)))
        elif "{prompt}" in a:
            out.append(a.replace("{prompt}", prompt_text))
        else:
            out.append(a)
    if not uses_stdin and not any(prompt_text in a or str(prompt_file or "\0") in a for a in out):
        raise RunError(
            "the agent command takes the prompt neither on stdin nor as {prompt} / {prompt_file}"
        )
    return out, (prompt_text if uses_stdin else None)


def preview(argv: Sequence[str], prompt_text: str, stdin_text: str | None) -> str:
    shown = [("<prompt: %d chars>" % len(prompt_text)) if a == prompt_text else a for a in argv]
    return " ".join(shlex.quote(a) for a in shown) + (
        "  < <prompt on stdin: %d chars>" % len(prompt_text) if stdin_text is not None else ""
    )


def default_launch(
    argv: Sequence[str], stdin_text: str | None, timeout: float, log: Path
) -> tuple[int, str]:
    """Run one agent from the home directory (never inside ~/Code) and keep its output in `log` (0600)."""
    fd = os.open(str(log), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        try:
            proc = subprocess.Popen(
                list(argv),
                cwd=os.path.expanduser("~"),
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=fh,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            return 127, f"{type(exc).__name__}: {exc}"
        try:
            proc.communicate(input=stdin_text, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
            return 124, "timed out"
    return proc.returncode, ""


def default_lane(argv: Sequence[str]) -> tuple[int, str]:
    lane = os.path.expanduser("~/apps/lane")
    if not os.path.exists(lane):
        return 127, "~/apps/lane is not installed"
    proc = subprocess.run(
        [lane, *argv],
        cwd=os.path.expanduser("~"),
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    return proc.returncode, (proc.stderr or proc.stdout).strip()[-400:]


@dataclass
class Outcome:
    pr: str
    round: int
    action: (
        str  # launched | would-launch | in-flight-skipped | nothing-to-do | status-missing | failed
    )
    why: str = ""
    command: str = ""
    final: dict[str, Any] = field(default_factory=dict)
    lane: str = ""
    log: str = ""


class Sweep:
    def __init__(
        self,
        *,
        gh: Gh,
        fleet: Fleet,
        seat: SeatInfo,
        platform_name: str,
        cfg: Mapping[str, Any],
        apply: bool,
        auto_approve: bool = False,
        agent_cmd: str | None = None,
        max_parallel: int = DEFAULT_PARALLEL,
        rounds: int = DEFAULT_ROUNDS,
        agent_timeout: float = DEFAULT_TIMEOUT,
        deploy: bool = True,
        clock: Callable[[], _dt.datetime] = lambda: _dt.datetime.now(_dt.timezone.utc),  # noqa: UP017 (3.9 has no datetime.UTC)
        launch: LaunchFn = default_launch,
        lane: LaneFn = default_lane,
        log_root: Path | None = None,
        say: Callable[[str], None] = lambda m: None,
    ) -> None:
        self.gh, self.fleet, self.seat, self.cfg = gh, fleet, seat, cfg
        self.platform_name, self.apply, self.auto_approve, self.agent_cmd = (
            platform_name,
            apply,
            auto_approve,
            agent_cmd,
        )
        self.max_parallel, self.rounds, self.timeout, self.deploy = (
            max(1, max_parallel),
            max(1, rounds),
            agent_timeout,
            deploy,
        )
        self.clock, self.launch, self.lane, self.say = clock, launch, lane, say
        self.log_root = log_root
        self.outcomes: list[Outcome] = []
        self.ours: dict[
            str, _dt.datetime
        ] = {}  # PR key -> when this sweep's last worker on it finished
        self.errors: list[str] = []
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- one PR
    def _record(self, o: Outcome) -> None:
        with self._lock:
            self.outcomes.append(o)
        self.say(f"{o.pr} r{o.round}: {o.action}" + (f" ({o.why})" if o.why else ""))

    def _logdir(self) -> Path:
        if self.log_root is None:
            stamp = self.clock().strftime("%Y%m%dT%H%M%SZ")
            self.log_root = Path(
                os.path.expanduser(f"~/apps/scratch/{self.seat.folder}/pr-sweep/{stamp}")
            )
        self.log_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        return self.log_root

    def _status(self, key: str) -> dict[str, Any]:
        return S.status_many(self.gh, [key], self.clock())[key]

    def _fresh_by_others(self, key: str, s: Mapping[str, Any]) -> bool:
        if not s.get("fresh"):
            return False
        ours = self.ours.get(key)
        if ours is None:
            return True
        newest = max(
            [t for t in (S.parse_time(s.get("updated")), S.parse_time(s.get("committed"))) if t],
            default=None,
        )
        return newest is not None and newest > ours + _dt.timedelta(seconds=60)

    def work_one(
        self, item: Mapping[str, Any], allprs: Sequence[Mapping[str, Any]], round_no: int
    ) -> Outcome:
        key = f"{item['full']}#{item['n']}"
        s = self._status(key)
        if s.get("error"):
            o = Outcome(key, round_no, "status-missing", s["error"])
            with self._lock:
                self.errors.append(f"{key}: no status ({s['error']}); nothing launched for it")
            self._record(o)
            return o
        if self._fresh_by_others(key, s):
            o = Outcome(
                key,
                round_no,
                "in-flight-skipped",
                f"activity {s.get('minutes_since_activity')} min ago (under {S.FRESH_MINUTES})",
            )
            self._record(o)
            return o
        parent = P.stack_parent(item, allprs)
        parent_state = None
        if parent is not None:
            ps = self._status(f"{parent['full']}#{parent['n']}")
            parent_state = ps.get("state") if not ps.get("error") else None
        try:
            need, why = S.needs_work(item.get("mode", "merge"), s, parent_state=parent_state)
        except S.StatusMissing as exc:
            o = Outcome(key, round_no, "status-missing", str(exc))
            self._record(o)
            return o
        if not need:
            o = Outcome(key, round_no, "nothing-to-do", why, final=s)
            self._record(o)
            return o
        merged = dict(item)
        merged.update(
            {
                k: s.get(k)
                for k in (
                    "mstate",
                    "review",
                    "unresolved",
                    "ci",
                    "auto",
                    "updated",
                    "base",
                    "head",
                    "labels",
                    "title",
                    "author",
                )
                if s.get(k) is not None
            }
        )
        ours = (
            self.ours[key].strftime("%Y-%m-%dT%H:%M:%SZ")
            if (round_no > 1 and key in self.ours)
            else None
        )
        text = PR.worker_prompt(
            merged,
            self.seat,
            trailer=self.cfg.get("trailer"),
            mode=item.get("mode", "merge"),
            round_no=round_no,
            parent=parent,
            code_dir=self.fleet.code_dir(item["full"]),
            ours_until=ours,
        )
        with tempfile.TemporaryDirectory(prefix="pr-sweep-") as tmp:
            pfile = Path(tmp) / "prompt.txt"
            argv, stdin_text = build_command(
                self.cfg,
                text,
                tier=item.get("tier", "mid"),
                auto_approve=self.auto_approve,
                prompt_file=pfile,
                agent_cmd=self.agent_cmd,
            )
            shown = preview(argv, text, stdin_text)
            if not self.apply:
                o = Outcome(key, round_no, "would-launch", why, command=shown)
                self._record(o)
                return o
            fd = os.open(str(pfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            log = self._logdir() / f"{item['full'].replace('/', '_')}_{item['n']}_r{round_no}.log"
            rc, err = self.launch(argv, stdin_text, self.timeout, log)
        with self._lock:
            self.ours[key] = self.clock()
        after = self._status(key)
        o = Outcome(
            key,
            round_no,
            "launched" if rc == 0 else "failed",
            why if rc == 0 else f"agent exit {rc} {err}".strip(),
            command=shown,
            final=after,
            log=str(log),
        )
        if after.get("state") == "MERGED":
            lrc, lmsg = self.lane(
                ["retire", f"pr{item['n']}-sweep", "--repo", self.fleet.code_dir(item["full"])]
            )
            if lrc == 0:
                o.lane = "retired"
            elif "no lane " in (lmsg or ""):
                o.lane = "already retired by its worker (or never made)"
            else:
                o.lane = f"kept ({lmsg or 'exit ' + str(lrc)})"
        self._record(o)
        return o

    # ---------------------------------------------------------------- chains and rounds
    def _run_chains(
        self,
        chains: Sequence[Mapping[str, Any]],
        allprs: Sequence[Mapping[str, Any]],
        round_no: int,
    ) -> None:
        def run_chain(ch: Mapping[str, Any]) -> None:
            for item in ch["items"]:
                try:
                    self.work_one(item, allprs, round_no)
                except Exception as exc:  # one PR's failure never stops its chain silently
                    with self._lock:
                        self.errors.append(
                            f"{item['full']}#{item['n']}: {type(exc).__name__}: {exc}"
                        )
                    self._record(
                        Outcome(f"{item['full']}#{item['n']}", round_no, "failed", str(exc)[:300])
                    )

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_parallel) as pool:
            list(pool.map(run_chain, chains))

    def sweep(
        self, prs: Sequence[Mapping[str, Any]], chains_limit: Mapping[str, int] | None = None
    ) -> dict[str, Any]:
        plan = P.make_plan(prs, chains_limit)
        work = []
        modes = {}
        for ch in plan["chains"]:
            for it in ch["items"]:
                modes[f"{ch['full']}#{it['n']}"] = it
        for p in prs:
            k = f"{p['full']}#{p['n']}"
            if k in modes:
                rec = dict(p)
                rec.update(mode=modes[k]["mode"], tier=modes[k]["tier"])
                work.append(rec)
        chains = P.build_chains(work, chains_limit)
        self.say(
            f"plan: {len(work)} PRs to work in {len(chains)} chains, {len(plan['skipped'])} skipped"
        )
        self._run_chains(chains, prs, 1)
        if self.apply:
            for round_no in range(2, self.rounds + 1):
                st = S.status_many(self.gh, [f"{p['full']}#{p['n']}" for p in work], self.clock())
                missing = [k for k, v in st.items() if v.get("error")]
                if missing:
                    self.errors.append(
                        "status missing for " + ", ".join(missing) + "; repair rounds stopped"
                    )
                    break
                need = []
                for p in work:
                    k = f"{p['full']}#{p['n']}"
                    par = P.stack_parent(p, prs)
                    pstate = st.get(f"{par['full']}#{par['n']}", {}).get("state") if par else None
                    if S.needs_work(p.get("mode", "merge"), st[k], parent_state=pstate)[0]:
                        need.append(p)
                self.say(f"after round {round_no - 1}: {len(need)} PR(s) still need work")
                if not need:
                    break
                self._run_chains(P.build_chains(need, chains_limit), prs, round_no)
        touched = sorted({o.pr for o in self.outcomes if o.action in ("launched", "failed")})
        audits = A.audit(self.gh, touched) if (self.apply and touched) else []
        final = (
            S.status_many(self.gh, [f"{p['full']}#{p['n']}" for p in work], self.clock())
            if work
            else {}
        )
        merged_by_repo: dict[str, list[dict[str, Any]]] = {}
        for k, s in final.items():
            if s.get("state") == "MERGED" and k in touched:
                full, n = S.split_key(k)
                merged_by_repo.setdefault(full, []).append({"n": n, "title": s.get("title", "")})
        deploys = []
        for full, merged in sorted(merged_by_repo.items()):
            text = PR.deploy_prompt(full, merged, self.seat)
            if self.apply and self.deploy:
                with tempfile.TemporaryDirectory(prefix="pr-sweep-") as tmp:
                    pfile = Path(tmp) / "prompt.txt"
                    argv, stdin_text = build_command(
                        self.cfg,
                        text,
                        tier="mid",
                        auto_approve=self.auto_approve,
                        prompt_file=pfile,
                        agent_cmd=self.agent_cmd,
                    )
                    fd = os.open(str(pfile), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        fh.write(text)
                    log = self._logdir() / f"deploy_{full.replace('/', '_')}.log"
                    rc, err = self.launch(argv, stdin_text, self.timeout, log)
                deploys.append(
                    {
                        "repo": full,
                        "merged": [m["n"] for m in merged],
                        "agent_exit": rc,
                        "log": str(log),
                    }
                )
            else:
                deploys.append(
                    {
                        "repo": full,
                        "merged": [m["n"] for m in merged],
                        "agent_exit": None,
                        "note": "deploy check not launched (dry run or --no-deploy): run the deploy-verify skill",
                    }
                )
        return {
            "platform": self.platform_name,
            "seat": self.seat.tag,
            "apply": self.apply,
            "plan": plan["counts"],
            "chains_per_repo": plan["chains_per_repo"],
            "skipped": plan["skipped"],
            "outcomes": [o.__dict__ for o in self.outcomes],
            "final": {
                k: {
                    f: v.get(f)
                    for f in ("state", "mstate", "ci", "unresolved", "auto", "review", "error")
                }
                for k, v in final.items()
            },
            "audit": audits,
            "deploys": deploys,
            "errors": self.errors,
            "log_dir": str(self.log_root) if self.log_root else None,
        }


def which(cfg: Mapping[str, Any], agent_cmd: str | None) -> str | None:
    argv = shlex.split(agent_cmd) if agent_cmd else list(cfg.get("argv") or [])
    return shutil.which(argv[0]) if argv else None
