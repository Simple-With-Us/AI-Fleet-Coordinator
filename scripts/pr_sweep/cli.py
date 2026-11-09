"""pr-sweep: a platform-neutral PR sweep for the fleet (docs/PR-SWEEP.md).

    pr-sweep inventory [--owner O ...] [--fleet-only] [--repos A,B]           every open PR (JSON)
    pr-sweep plan [--inventory FILE] [--chains REPO=N ...]                     skip/merge, stacks, chains (JSON)
    pr-sweep status O/R#N ... | --inventory FILE | --repos A,B                 CURRENT facts per PR (JSON)
    pr-sweep needs-work O/R#N ... | --inventory FILE                           the repair decision per PR (JSON)
    pr-sweep prompt O/R#N [--platform P] [--round N] [--deploy] [--common]     the full worker prompt (text)
    pr-sweep audit O/R#N ... [--since ISO] [--allow-dismisser LOGIN ...]       deterministic audit (JSON)
    pr-sweep lanes [lane prune options]                                        your lanes (calls ~/apps/lane prune)
    pr-sweep run --platform P (--dry-run | --apply) [options]                  launch one agent per PR
    pr-sweep platforms                                                         the agent templates (JSON)

Exit codes: 0 ok, 64 usage or refusal (unset AGENT_SEAT, unknown platform), 69 gh failed or a status is
missing, 70 a bug.  Everything but `run --apply` and `lanes --apply` is read-only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from typing import Any

from . import audit as A
from . import fleet as F
from . import inventory as I
from . import plan as P
from . import prompt as PR
from . import run as R
from . import status as S
from .gh import Gh, GhError

EXIT_OK, EXIT_USAGE, EXIT_TOOL, EXIT_BUG = 0, 64, 69, 70


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)  # noqa: UP017 (3.9 has no datetime.UTC)


_COMPACT = {"on": False}


def fnv1a(text: str) -> int:
    """32-bit FNV-1a over an ASCII string (the adapter computes the same over charCodeAt)."""
    h = 0x811C9DC5
    for b in text.encode("ascii"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def _dump(obj: Any, out) -> None:
    if _COMPACT["on"]:
        # One ASCII line of JSON, then a check line, so a caller that has the output copied by an agent (the
        # Claude adapter) can tell a faithful copy from a shortened or altered one.
        text = json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=True)
        out.write(text + "\n" + f"#fnv1a={fnv1a(text):08x} len={len(text)}\n")
    else:
        out.write(json.dumps(obj, indent=2, sort_keys=False, default=str) + "\n")


def _csv(text: str | None) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def _load_inventory(path: str) -> list[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    prs = data.get("prs") if isinstance(data, dict) else data
    if not isinstance(prs, list):
        raise ValueError(f'{path}: expected a list of PR records or {{"prs": [...]}}')
    return prs


def _filter(
    prs: list[dict[str, Any]], repos: Sequence[str], fleet_only: bool, fleet: F.Fleet
) -> list[dict[str, Any]]:
    want = {r.lower() for r in repos}
    out = []
    for p in prs:
        if fleet_only and not p.get("fleet"):
            continue
        if (
            want
            and p["full"].lower() not in want
            and str(p["repo"]).lower() not in want
            and fleet.code_dir(p["full"]).lower() not in want
        ):
            continue
        out.append(p)
    return out


def _inventory(args, gh: Gh, fleet: F.Fleet) -> list[dict[str, Any]]:
    if getattr(args, "inventory", None):
        prs = _load_inventory(args.inventory)
    else:
        prs = I.inventory(gh, F.owners(fleet, getattr(args, "owner", None) or []), fleet.repos)
    return _filter(
        prs, _csv(getattr(args, "repos", None)), bool(getattr(args, "fleet_only", False)), fleet
    )


def _keys(args, gh: Gh, fleet: F.Fleet) -> list[str]:
    keys = list(getattr(args, "prs", None) or [])
    for k in keys:
        S.split_key(k)
    if not keys:
        keys = [I.key(p) for p in _inventory(args, gh, fleet)]
    return keys


def _build() -> _Parser:
    p = _Parser(
        prog="pr-sweep", description="A platform-neutral PR sweep for the fleet.  docs/PR-SWEEP.md"
    )
    p.add_argument(
        "--compact", action="store_true", help="one-line JSON (for an agent to copy back verbatim)"
    )
    sub = p.add_subparsers(
        dest="cmd", metavar="{inventory,plan,status,needs-work,prompt,audit,lanes,run,platforms}"
    )

    def scope(sp, *, inv: bool = True) -> None:
        sp.add_argument(
            "--owner", action="append", metavar="O", help="another owner to search (repeatable)"
        )
        sp.add_argument(
            "--repos", metavar="A,B", help="only these repos (name, owner/name or ~/Code folder)"
        )
        sp.add_argument(
            "--fleet-only", action="store_true", help="only repos registered in fleet-apps.json"
        )
        if inv:
            sp.add_argument(
                "--inventory",
                metavar="FILE",
                help="read the inventory JSON instead of asking GitHub",
            )

    sp = sub.add_parser("inventory", help="every open PR across the fleet owners (JSON)")
    scope(sp, inv=False)
    sp = sub.add_parser("plan", help="classification, stacks and chains (JSON)")
    scope(sp)
    sp.add_argument(
        "--chains",
        action="append",
        metavar="REPO=N",
        help="chains for a repo (default 1; ST 3, BF and AFC 2)",
    )
    for name, helptext in (
        ("status", "CURRENT facts per PR (JSON)"),
        ("needs-work", "the repair decision per PR (JSON)"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("prs", nargs="*", metavar="O/R#N")
        scope(sp)
    sp = sub.add_parser("prompt", help="the full worker prompt for any agent (text)")
    sp.add_argument("pr", nargs="?", metavar="O/R#N")
    sp.add_argument("--platform", default="", help="use that platform's commit trailer")
    sp.add_argument("--seat", help="seat tag (default AGENT_SEAT)")
    sp.add_argument("--round", type=int, default=1)
    sp.add_argument(
        "--ours",
        metavar="ISO",
        help="repair rounds: activity up to this UTC time is the sweep's own",
    )
    sp.add_argument(
        "--deploy", metavar="O/R", help="print the deploy-check prompt for this repo instead"
    )
    sp.add_argument("--merged", metavar="N,N", help="with --deploy: the PR numbers that merged")
    sp.add_argument(
        "--common", action="store_true", help="only the identity, lane, forbidden and writing rules"
    )
    sp.add_argument(
        "--offline",
        action="store_true",
        help="do not read the PR from GitHub (placeholders instead)",
    )
    sp = sub.add_parser("audit", help="deterministic audit of PRs (JSON)")
    sp.add_argument("prs", nargs="+", metavar="O/R#N")
    sp.add_argument("--since", metavar="ISO", help="only events at or after this UTC time")
    sp.add_argument(
        "--allow-dismisser",
        action="append",
        metavar="LOGIN",
        help=f"an account allowed to dismiss kody-ai reviews (default {', '.join(A.DEFAULT_ALLOWED_DISMISSERS)})",
    )
    sp = sub.add_parser(
        "lanes", help="your lanes: runs ~/apps/lane prune with the options after it"
    )
    sp.add_argument("rest", nargs=argparse.REMAINDER)
    sp = sub.add_parser("run", help="launch one agent per PR that needs work")
    sp.add_argument("--platform", required=True)
    mode = sp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    sp.add_argument(
        "prs", nargs="*", metavar="O/R#N", help="only these PRs (default: the whole inventory)"
    )
    scope(sp)
    sp.add_argument("--chains", action="append", metavar="REPO=N")
    sp.add_argument(
        "--auto-approve",
        action="store_true",
        help="add the platform's auto-approve flags (platforms.json)",
    )
    sp.add_argument(
        "--agent-cmd", metavar="TEMPLATE", help="a command template instead of the platform's argv"
    )
    sp.add_argument("--max-parallel", type=int, default=R.DEFAULT_PARALLEL)
    sp.add_argument("--rounds", type=int, default=R.DEFAULT_ROUNDS)
    sp.add_argument("--agent-timeout", type=float, default=R.DEFAULT_TIMEOUT, metavar="SECONDS")
    sp.add_argument(
        "--no-deploy", action="store_true", help="do not launch deploy-check workers after merges"
    )
    sp.add_argument("--json", action="store_true", help="print the report as JSON")
    sub.add_parser("platforms", help="the agent command templates (JSON)")
    return p


def _chains(values: Sequence[str] | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values or []:
        name, sep, num = v.partition("=")
        if not sep or not num.isdigit() or int(num) < 1:
            raise ValueError(f"--chains takes REPO=N, got {v!r}")
        out[name] = int(num)
    return out


def _records_for(gh: Gh, keys: Sequence[str], fleet: F.Fleet) -> list[dict[str, Any]]:
    """Inventory-shaped records for explicit PR keys, from their current status."""
    st = S.status_many(gh, keys, _now())
    out = []
    for k, s in st.items():
        if s.get("error"):
            raise GhError(f"{k}: {s['error']}")
        full, n = S.split_key(k)
        owner, repo = full.split("/", 1)
        rec = {
            "owner": owner,
            "repo": repo,
            "full": full,
            "n": n,
            "fleet": fleet.is_fleet(full),
            "cross_repo": False,
        }
        rec.update(
            {
                f: s.get(f)
                for f in (
                    "title",
                    "author",
                    "head",
                    "base",
                    "default_branch",
                    "head_oid",
                    "draft",
                    "labels",
                    "updated",
                    "committed",
                    "mstate",
                    "review",
                    "threads",
                    "unresolved",
                    "ci",
                    "auto",
                )
            }
        )
        out.append(rec)
    return out


def _render_run(rep: dict[str, Any]) -> str:
    lines = [
        f"pr-sweep run ({rep['platform']}, seat {rep['seat']}, {'APPLY' if rep['apply'] else 'dry run'})",
        f"plan: {rep['plan']}",
        f"chains per repo: {rep['chains_per_repo']}",
        "",
    ]
    for o in rep["outcomes"]:
        lines.append(f"  r{o['round']} {o['pr']:<45} {o['action']:<18} {o['why']}")
        if o.get("command") and not rep["apply"]:
            lines.append(f"      $ {o['command']}")
        if o.get("final"):
            f = o["final"]
            if rep["apply"]:
                lines.append(
                    f"      now: {f.get('state')}/{f.get('mstate')} ci={f.get('ci')} threads={f.get('unresolved')} "
                    f"auto={f.get('auto')}" + (f"  lane {o['lane']}" if o.get("lane") else "")
                )
    if rep["skipped"]:
        lines += ["", "skipped:"] + [f"  {s['pr']}  ({s['why']})" for s in rep["skipped"]]
    for a in rep["audit"]:
        if not a.get("ok"):
            lines.append(
                f"AUDIT {a['pr']}: "
                + "; ".join(f"{v['rule']} {v['evidence']}" for v in a["violations"])
                + (f" error {a['error']}" if a.get("error") else "")
            )
    for d in rep["deploys"]:
        lines.append(
            f"deploy check {d['repo']} (merged {d['merged']}): "
            + (
                f"agent exit {d['agent_exit']}, log {d.get('log')}"
                if d.get("agent_exit") is not None
                else d["note"]
            )
        )
    for e in rep["errors"]:
        lines.append("error: " + e)
    return "\n".join(lines) + "\n"


def main(
    argv: Sequence[str] | None = None,
    *,
    env=None,
    gh: Gh | None = None,
    stdout=None,
    stderr=None,
    launch: R.LaunchFn | None = None,
    lane: R.LaneFn | None = None,
    clock=None,
) -> int:
    env = os.environ if env is None else env
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    raw = list(sys.argv[1:] if argv is None else argv)
    if "lanes" in raw[:2] and all(
        a in ("lanes", "--compact") for a in raw[: raw.index("lanes") + 1]
    ):
        # argparse REMAINDER cannot take leading options such as --apply, so `lanes` skips the parser: every
        # word after it goes to `~/apps/lane prune` as it is.
        rest = raw[raw.index("lanes") + 1 :]
        if rest[:1] in (["-h"], ["--help"]):
            out.write("usage: pr-sweep lanes [lane prune options]   (see ~/apps/lane prune -h)\n")
            return EXIT_OK
        try:
            F.seat(F.load(env), env)
        except F.FleetError as exc:
            err.write(f"pr-sweep: refused: {exc}\n")
            return EXIT_USAGE
        rc, msg = (lane or _lane_passthrough)(["prune", *[a for a in rest if a != "--"]])
        if msg:
            out.write(msg + "\n")
        return rc
    try:
        args = _build().parse_args(raw)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    if not args.cmd:
        _build().print_usage(err)
        return EXIT_USAGE
    _COMPACT["on"] = bool(args.compact)
    gh = gh or Gh()
    try:
        fleet = F.load(env)
        if args.cmd == "platforms":
            _dump(R.load_platforms(), out)
            return EXIT_OK
        if args.cmd == "inventory":
            prs = _inventory(args, gh, fleet)
            _dump({"generated_at": _now().isoformat(), "count": len(prs), "prs": prs}, out)
            return EXIT_OK
        if args.cmd == "plan":
            _dump(P.make_plan(_inventory(args, gh, fleet), _chains(args.chains)), out)
            return EXIT_OK
        if args.cmd in ("status", "needs-work"):
            keys = _keys(args, gh, fleet)
            st = S.status_many(gh, keys, (clock or _now)())
            missing = [k for k, v in st.items() if v.get("error")]
            if args.cmd == "status":
                _dump(
                    {
                        "generated_at": _now().isoformat(),
                        "requested": keys,
                        "status": st,
                        "missing": missing,
                    },
                    out,
                )
            else:
                rows = {}
                by_head = {
                    (S.split_key(k)[0], v.get("head")): v
                    for k, v in st.items()
                    if not v.get("error")
                }
                for k in keys:
                    s = st[k]
                    if s.get("error"):
                        rows[k] = {"needs_work": None, "why": "status missing: " + s["error"]}
                        continue
                    mode = (
                        "skip"
                        if s.get("draft")
                        or "deferred" in [str(x).lower() for x in s.get("labels") or []]
                        else "merge"
                    )
                    parent = by_head.get((S.split_key(k)[0], s.get("base")))
                    need, why = S.needs_work(
                        mode, s, parent_state=parent.get("state") if parent else None
                    )
                    rows[k] = {
                        "needs_work": need,
                        "why": why,
                        "fresh": s.get("fresh"),
                        "mode": mode,
                        "minutes_since_activity": s.get("minutes_since_activity"),
                    }
                _dump(
                    {
                        "generated_at": _now().isoformat(),
                        "requested": keys,
                        "decisions": rows,
                        "missing": missing,
                    },
                    out,
                )
            if missing:
                err.write("pr-sweep: status missing for " + ", ".join(missing) + "\n")
                return EXIT_TOOL
            return EXIT_OK
        if args.cmd == "prompt":
            seat = F.seat(fleet, env, args.seat)
            trailer = R.platform(args.platform).get("trailer") if args.platform else None
            if args.deploy:
                merged = [{"n": int(x)} for x in _csv(args.merged) if x.isdigit()]
                out.write(PR.deploy_prompt(args.deploy, merged, seat))
                return EXIT_OK
            if args.common:
                out.write(PR.common_rules(seat, trailer) + "\n")
                return EXIT_OK
            if not args.pr:
                raise ValueError("prompt needs O/R#N (or --deploy O/R, or --common)")
            full, n = S.split_key(args.pr)
            if args.offline:
                rec: dict[str, Any] = {"full": full, "n": n}
            else:
                rec = _records_for(gh, [args.pr], fleet)[0]
            out.write(
                PR.worker_prompt(
                    rec,
                    seat,
                    trailer=trailer,
                    round_no=args.round,
                    code_dir=fleet.code_dir(full),
                    ours_until=args.ours,
                )
            )
            return EXIT_OK
        if args.cmd == "audit":
            since = S.parse_time(args.since) if args.since else None
            if args.since and since is None:
                raise ValueError(f"--since must be an ISO time, got {args.since!r}")
            res = A.audit(
                gh,
                args.prs,
                since=since,
                allowed_dismissers=args.allow_dismisser or A.DEFAULT_ALLOWED_DISMISSERS,
            )
            _dump(
                {
                    "generated_at": _now().isoformat(),
                    "results": res,
                    "ok": all(r.get("ok") for r in res),
                },
                out,
            )
            return EXIT_OK if all(not r.get("error") for r in res) else EXIT_TOOL
        if args.cmd == "lanes":
            F.seat(fleet, env)
            rest = [a for a in args.rest if a != "--"]
            rc, msg = (lane or _lane_passthrough)(["prune", *rest])
            if msg:
                out.write(msg + "\n")
            return rc
        if args.cmd == "run":
            seat = F.seat(fleet, env)
            cfg = R.platform(args.platform)
            if args.apply and R.which(cfg, args.agent_cmd) is None:
                raise ValueError(
                    f"the {args.platform} command is not on PATH; install it or pass --agent-cmd"
                )
            if args.prs:
                prs = _records_for(gh, args.prs, fleet)
            else:
                prs = _inventory(args, gh, fleet)
            sweep = R.Sweep(
                gh=gh,
                fleet=fleet,
                seat=seat,
                platform_name=args.platform,
                cfg=cfg,
                apply=args.apply,
                auto_approve=args.auto_approve,
                agent_cmd=args.agent_cmd,
                max_parallel=args.max_parallel,
                rounds=args.rounds,
                agent_timeout=args.agent_timeout,
                deploy=not args.no_deploy,
                launch=launch or R.default_launch,
                lane=lane or R.default_lane,
                clock=clock or _now,
                say=lambda m: err.write("pr-sweep: " + m + "\n"),
            )
            rep = sweep.sweep(prs, _chains(args.chains))
            out.write(
                json.dumps(rep, indent=2, default=str) + "\n" if args.json else _render_run(rep)
            )
            return EXIT_TOOL if rep["errors"] else EXIT_OK
    except (F.FleetError, R.RunError, PR.RulesError, ValueError) as exc:
        err.write(f"pr-sweep: refused: {exc}\n")
        return EXIT_USAGE
    except (GhError, S.StatusMissing) as exc:
        err.write(f"pr-sweep: error: {exc}\n")
        return EXIT_TOOL
    return EXIT_BUG


def _lane_passthrough(argv: Sequence[str]):
    lane = os.path.expanduser("~/apps/lane")
    if not os.path.exists(lane):
        return (
            69,
            "pr-sweep: ~/apps/lane is not installed (python3 -m fleet_lanes.install_tools apply tools)",
        )
    proc = subprocess.run([lane, *argv], cwd=os.path.expanduser("~"), check=False)
    return proc.returncode, ""


if __name__ == "__main__":
    sys.exit(main())
