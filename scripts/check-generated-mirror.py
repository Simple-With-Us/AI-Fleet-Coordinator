#!/usr/bin/env python3
"""check-generated-mirror.py -- keep a generated mirror honest.

Fleet policy is written in the private Fleet-OPS repository, in `handbook/`.  The sync job builds
`generated/FLEET-PROTOCOL.md` there and mirrors it into this repository as `FLEET-PROTOCOL.md`, by a pull
request from the branch `handbook-sync/mirror`.  Line 1 of the mirror is the header the generator
(`scripts/handbook/build.mjs`, `headerLine()`) writes:

    <!-- GENERATED from Simple-With-Us/Fleet-OPS handbook/.  Do not edit here; open a PR to handbook/.  body-sha256:<64 hex> -->

and the hash is the sha256 of every byte after that line's newline.  For each generated path this
check fails when

  1. the file has no header, unless it is still the exact pre-flip text pinned below;
  2. the header is not the generator's line, or the body does not hash to the header's value;
  3. (a pull request:  --base and --head-branch) the path changes against the base, the file carries
     a header before or after the change, and the head branch is not `handbook-sync/mirror`;
  4. (a pull request from `handbook-sync/mirror`) the branch changes anything but generated paths.

Rule 3 stops a hand edit with a recomputed hash, and (on the mirror branch only) rejects a strip of the
GENERATED header while the base still has it.  Rule 1 stops an unheadered file whose text is not the
pinned pre-flip bytes.  On a non-mirror branch rule 3 already refuses the strip on attribution grounds;
on the mirror branch the pin exemption and the rule 3 exemption together would let a headered-to-
unheadered change pass, so the mirror branch is held to a strip refusal here.  The pin still excuses a
header-less file before the first mirror PR lands.  The hash cannot tell a real build from a forged one,
since anyone can recompute it;  authenticity is the Mac sync job's work, which refuses a hash it did
not write.  The mirror PR carries only the generated file, so nothing in this script can change in the
same PR as the first header:  the pin is how the check passes before that PR and refuses a hand edit
after it.

This is a hard gate.  Branch protection on main requires the `test` check (strict, so the branch must also be up to
date), and this check is a step in that job, so a red result here blocks the merge of every pull request, not only
one that touches a generated path.  A bug in this script, or a bad state on main, therefore blocks the whole
repository.  Break-glass:  enforce_admins is false, so an admin can bypass the protection to merge a fix;  and the
sync job's own pull request, from `handbook-sync/mirror` with a valid header and hash, passes by design.  The check
cannot prove a header was written by the generator;  the Mac sync job (which refuses a hash it did not write) is
the authenticity enforcer, and this check is the gate that stops a hand edit from merging.

Usage:
    python3 scripts/check-generated-mirror.py                                      header and hash
    python3 scripts/check-generated-mirror.py --base <sha> --head-branch <name>    a pull request

Exit codes:  0 clean, 1 a rule failed (one `error:` line each), 2 a usage or git problem.  A base that
does not resolve is a problem, never a pass, so a shallow clone that lacks it fails closed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

MIRROR_BRANCH = "handbook-sync/mirror"

# What a header attempt starts with.  Anything that starts like this is a header and must be exactly the
# generator's line, so a typo cannot slip past as "no header".
HEADER_ATTEMPT = re.compile(rb"<!--\s*GENERATED", re.IGNORECASE)

# The generator's headerLine(), verbatim (two ASCII spaces between sentences), with a lowercase 64-digit hash.
HEADER_RE = re.compile(
    rb"<!-- GENERATED from Simple-With-Us/Fleet-OPS handbook/\.  Do not edit here; open a PR to handbook/\.  "
    rb"body-sha256:([0-9a-f]{64}) -->"
)


@dataclass(frozen=True)
class GeneratedPath:
    path: str
    # Until the first mirror PR lands the header, the path is the text the handbook was imported from, and this is
    # its sha256.  A header-less file that is not exactly this text fails, so a hand edit during the freeze is
    # refused too.  None means the header is required from the start.  Drop the pin in a one-line follow-up once
    # the header is on main;  until then the mirror-branch strip check in branch_problems refuses a mirror PR that
    # reverts to the pinned text (rule 3 alone would let it through, because the pin exemption accepts header-less
    # bytes and the mirror branch is exempt from rule 3's branch attribution check).
    pre_flip_sha256: str | None
    # Migration scaffolding for a rename:  the other name of the same document.  Exactly one of the two must exist
    # (the sync job's mirror pull request adds one and deletes the other in a single commit), and whichever exists
    # is held to every rule below.  None for a path that is not being renamed.  Remove with the old name.
    alternate: str | None = None


# The document was renamed:  Fleet-OPS renamed the generated handbook to FLEET-PROTOCOL.md in c5adf8d, the mirror
# carries that name now, and the old name is gone.  The first header-bearing copy arrives by the mirror pull
# request, never by hand.
GENERATED_PATHS: tuple[GeneratedPath, ...] = (
    GeneratedPath("FLEET-PROTOCOL.md", None),
    # Phase B (U9) adds the Zulip guide here, in an ordinary PR, with the pin set to the guide's sha256 at that
    # commit;  the first header then arrives by the mirror PR.  Listed now it would fail, because the guide is
    # hand-written.
    # GeneratedPath("docs/protocols/zulip-fleet-guide.md", "<sha256 of the guide when U9 lands>"),
)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def has_header(data: bytes | None) -> bool:
    return data is not None and HEADER_ATTEMPT.match(data) is not None


def content_problems(spec: GeneratedPath, data: bytes) -> tuple[list[str], str]:
    """Rules 1 and 2 for one file.  Returns (problems, a one-line note for a clean file)."""
    if not has_header(data):
        digest = sha256_hex(data)
        if spec.pre_flip_sha256 is not None and digest == spec.pre_flip_sha256:
            return [], f"pre-flip text (sha256 {digest[:12]}), no GENERATED header yet"
        if spec.pre_flip_sha256 is None:
            return [
                "has no GENERATED header on line 1;  it is generated from Fleet-OPS handbook/, so open a PR there"
            ], ""
        return [
            "has no GENERATED header and is not the pinned pre-flip text "
            f"(sha256 {digest}, pinned {spec.pre_flip_sha256});  it is generated from Fleet-OPS handbook/, "
            "so open a PR there instead of editing it here"
        ], ""
    first, sep, body = data.partition(b"\n")
    match = HEADER_RE.fullmatch(first)
    if not match:
        return [
            "line 1 starts a GENERATED header but is not the generator's line;  expected "
            "'<!-- GENERATED from Simple-With-Us/Fleet-OPS handbook/.  Do not edit here; open a PR to handbook/.  "
            "body-sha256:<64 lowercase hex> -->' with nothing after it"
        ], ""
    if not sep or not body:
        return ["the GENERATED header has no body after it"], ""
    claimed = match.group(1).decode("ascii")
    actual = sha256_hex(body)
    if claimed != actual:
        return [
            f"the body does not match its header:  header body-sha256 {claimed}, body hashes to {actual};  "
            "the mirror was edited by hand, so open a PR to Fleet-OPS handbook/ and let the sync job rewrite it"
        ], ""
    return [], f"GENERATED header valid, body sha256 {actual[:12]}"


def branch_problems(
    spec: GeneratedPath,
    base: bytes | None,
    head: bytes | None,
    head_branch: str,
    mirror_branch: str,
) -> list[str]:
    """Rule 3:  only the mirror branch may change a path that carries a header.

    The pin exemption in rule 1 accepts an unheadered file whose bytes match the pre-flip text, and the
    mirror branch is exempt from rule 3 entirely.  On a non-mirror branch the strip is still refused by
    rule 3 (the change attribution is wrong);  on the mirror branch the sync job is the only legitimate
    writer, so the same strip would slip through.  Once the base has the header, dropping it on a mirror
    PR drops the only thing this gate can verify going forward, so the mirror branch itself is held to a
    strip refusal here.  The pin still excuses a header-less file before the first mirror PR lands.
    """
    if head_branch == mirror_branch:
        # A deletion is not a strip when the path has an alternate:  during a rename the old name is removed in the
        # commit that adds the new one, and check() fails the pair unless exactly one of them exists.
        if (
            has_header(base)
            and not has_header(head)
            and not (head is None and spec.alternate is not None)
        ):
            return [
                "strips the GENERATED header;  the base already has it, and on the mirror branch rule 3 would "
                "otherwise pass the change.  Removing it leaves main without the invariant this gate enforces, "
                "so even the sync job must rewrite the body without stripping the header"
            ]
        return []
    if not (has_header(head) or has_header(base)):
        return []  # hand-written on both sides;  rule 1 holds the text to the pin
    return [
        f"is changed by this pull request, and the head branch is '{head_branch}', not '{mirror_branch}';  this file is "
        "generated from Fleet-OPS handbook/, so only the sync job's mirror pull request may change it"
    ]


class GitError(RuntimeError):
    pass


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, check=False, timeout=60
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args)}: {exc}") from exc


def resolve_base(root: Path, rev: str) -> str:
    done = _git(root, "rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
    if done.returncode != 0:
        raise GitError(
            f"--base {rev!r} is not a commit in this checkout (in CI, fetch it first: "
            "git fetch --no-tags --depth=1 origin <sha>)"
        )
    return done.stdout.decode().strip()


def base_bytes(root: Path, commit: str, path: str) -> bytes | None:
    """The file at the base commit, or None when the base does not have it."""
    if _git(root, "cat-file", "-e", f"{commit}:{path}").returncode != 0:
        return None
    done = _git(root, "show", f"{commit}:{path}")
    if done.returncode != 0:
        raise GitError(
            f"git show {commit}:{path} failed: {done.stderr.decode(errors='replace').strip()}"
        )
    return done.stdout


def changed_paths(root: Path, commit: str) -> set[str]:
    """Tracked paths whose content differs between the base commit and the working tree (a rename counts as a
    delete plus an add, so moving a generated file away is a change too).  In CI the working tree is the merge commit."""
    done = _git(root, "diff", "--no-renames", "--name-only", "-z", commit)
    if done.returncode != 0:
        raise GitError(f"git diff {commit} failed: {done.stderr.decode(errors='replace').strip()}")
    return {p for p in done.stdout.decode("utf-8", "surrogateescape").split("\0") if p}


def check(
    root: Path,
    specs: tuple[GeneratedPath, ...],
    base: str | None,
    head_branch: str | None,
    mirror_branch: str = MIRROR_BRANCH,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Returns (problems as (path, message), notes)."""
    problems: list[tuple[str, str]] = []
    notes: list[str] = []
    commit = resolve_base(root, base) if base else None
    changed = changed_paths(root, commit) if commit and head_branch is not None else None
    if commit and head_branch is None:
        notes.append("branch rule skipped:  no head branch (a push or a local run)")
    for spec in specs:
        target = root / spec.path
        data = target.read_bytes() if target.is_file() else None
        if spec.alternate is not None and data is not None and (root / spec.alternate).is_file():
            problems.append(
                (
                    spec.path,
                    f"exists together with {spec.alternate};  during a rename exactly one of the "
                    "two may exist, so the mirror pull request must delete the old name",
                )
            )
        if data is None and spec.alternate is not None and (root / spec.alternate).is_file():
            notes.append(
                f"{spec.path}: absent while {spec.alternate} holds the document (rename in flight)"
            )
        elif data is None:
            problems.append(
                (
                    spec.path,
                    "is missing;  a generated path must exist"
                    + (f" (nor is {spec.alternate})" if spec.alternate else ""),
                )
            )
        else:
            found, note = content_problems(spec, data)
            problems += [(spec.path, m) for m in found]
            if note:
                notes.append(f"{spec.path}: {note}")
        if changed is not None and spec.path in changed:
            prior = base_bytes(root, commit, spec.path)
            problems += [
                (spec.path, m)
                for m in branch_problems(spec, prior, data, head_branch, mirror_branch)
            ]
    if changed is not None and head_branch == mirror_branch:
        stray = sorted(changed - {s.path for s in specs})
        if stray:
            shown = ", ".join(stray[:5]) + (f" (+{len(stray) - 5} more)" if len(stray) > 5 else "")
            problems.append(
                (
                    stray[0],
                    f"is changed by the branch '{mirror_branch}', which may change generated paths "
                    f"only;  it also changes {shown}",
                )
            )
    return problems, notes


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Check the generated mirrors (GENERATED header, body hash, who changed them)."
    )
    ap.add_argument(
        "--root", type=Path, default=ROOT, help="repository checkout to check (default: this one)"
    )
    ap.add_argument(
        "--base",
        help="the pull request's base commit;  with --head-branch it turns the branch rules on",
    )
    ap.add_argument(
        "--head-branch",
        help="the pull request's head branch;  write fork:<owner/repo>:<branch> for a fork",
    )
    ap.add_argument("--mirror-branch", default=MIRROR_BRANCH, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if args.head_branch is not None and not args.base:
        ap.error("--head-branch needs --base")
    try:
        problems, notes = check(
            args.root, GENERATED_PATHS, args.base, args.head_branch, args.mirror_branch
        )
    except GitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    for note in notes:
        print(f"ok: {note}")
    for path, message in problems:
        print(f"error: {path}: {message}")
        if annotate:
            print(f"::error file={path}::{message}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
