#!/usr/bin/env python3
"""Test double for the gh CLI.  Serves canned API responses from the scenario
file at $FAKE_GH_SCENARIO and appends every invocation (argv + stdin) as a
JSON line to $FAKE_GH_LOG.  Not shipped; used by test_guard_integration.py."""

import json
import os
import re
import sys
import tempfile

_SCENARIO_MAX_BYTES = 1_048_576
_SCENARIO_SECTIONS = {
    "fail",
    "labels",
    "events_seq",
    "events",
    "pulls",
    "check_runs",
    "armed",
}
_LIST_SECTIONS = ("labels", "events_seq", "events", "pulls", "check_runs")

# Kody r4373585476: invocation argv/stdin is untrusted text; refuse to
# persist anything shaped like a credential, naming only the field.
_SECRET_RE = re.compile(
    r"(?i)(?:(?:sk-|gh[pousr]_|xox[baprs]-)[A-Za-z0-9_-]{16,}"
    r"|AKIA[0-9A-Z]{16}|bearer\s+[A-Za-z0-9._~+/=-]{16,})"
)


def _load_scenario(path):
    """Bounded, shape-validated scenario load (Kody r4373585668): reject
    oversize files, unknown sections and wrong nested types up front, so a
    malformed scenario fails precisely instead of serving partial data."""
    if os.path.getsize(path) > _SCENARIO_MAX_BYTES:
        raise ValueError("FAKE_GH_SCENARIO: file exceeds 1048576 bytes")
    with open(path, encoding="utf-8") as fh:
        value = json.load(fh)
    if not isinstance(value, dict):
        raise ValueError("FAKE_GH_SCENARIO: expected a JSON object")
    unknown = set(value) - _SCENARIO_SECTIONS
    if unknown:
        raise ValueError(f"FAKE_GH_SCENARIO: unknown section(s): {sorted(unknown)}")
    if not isinstance(value.get("armed", False), bool):
        raise ValueError("FAKE_GH_SCENARIO.armed: expected a boolean")

    def _check_fail_spec(spec, where):
        if not isinstance(spec, dict):
            raise ValueError(f"{where}: expected an object")
        if not isinstance(spec.get("stderr", "boom"), str):
            raise ValueError(f"{where}.stderr: expected a string")
        rc = spec.get("rc", 1)
        if not isinstance(rc, int) or isinstance(rc, bool):
            raise ValueError(f"{where}.rc: expected an integer")

    for key, spec in value.get("fail", {}).items():
        _check_fail_spec(spec, f"FAKE_GH_SCENARIO.fail.{key}")
    for section in _LIST_SECTIONS:
        entries = value.get(section, {})
        if not isinstance(entries, dict):
            raise ValueError(f"FAKE_GH_SCENARIO.{section}: expected an object")
        for key, items in entries.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise ValueError(f"FAKE_GH_SCENARIO.{section}: invalid key")
            if not isinstance(items, list) or len(items) > 1000:
                raise ValueError(f"FAKE_GH_SCENARIO.{section}.{key}: expected a bounded array")
            for item in items:
                if section in ("events_seq", "events"):
                    # event pages are lists of event objects; a page may be a
                    # {"__fail__": spec} injection instead
                    if isinstance(item, dict) and "__fail__" in item:
                        _check_fail_spec(item["__fail__"], f"{section}.{key}.__fail__")
                        continue
                    if not isinstance(item, list):
                        raise ValueError(f"{section}.{key}: expected event-page arrays")
                elif not isinstance(item, dict):
                    raise ValueError(f"{section}.{key}: expected object items")
    for key, pulls in value.get("pulls", {}).items():
        for pull in pulls:
            if not isinstance(pull.get("number"), int) or isinstance(pull.get("number"), bool):
                raise ValueError(f"FAKE_GH_SCENARIO.pulls.{key}: number must be an integer")
    for key, runs in value.get("check_runs", {}).items():
        for run in runs:
            if not isinstance(run.get("id"), int) or isinstance(run.get("id"), bool):
                raise ValueError(f"FAKE_GH_SCENARIO.check_runs.{key}: id must be an integer")
            app = run.get("app", "github-actions")
            if not isinstance(app, (str, dict)):
                raise ValueError(
                    f"FAKE_GH_SCENARIO.check_runs.{key}: app must be a string or object"
                )
            if isinstance(app, dict) and not isinstance(app.get("slug", ""), str):
                raise ValueError(f"FAKE_GH_SCENARIO.check_runs.{key}: app.slug must be a string")
    return value


def _atomic_write(path, text):
    """Kody r4373585857: tempfile + flush + fsync + os.replace + dir fsync,
    so a crash mid-write never leaves a truncated sequence counter."""
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(path) or ".",
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        dir_fd = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


scenario = _load_scenario(os.environ["FAKE_GH_SCENARIO"])
log_path = os.environ["FAKE_GH_LOG"]
argv = sys.argv[1:]

stdin_data = ""
if "--input" in argv:
    stdin_data = sys.stdin.read()

for field, text in (("argv", json.dumps(argv)), ("stdin", stdin_data)):
    if _SECRET_RE.search(text):
        sys.stderr.write(f"fake-gh refused invocation log: code=secret field={field}\n")
        sys.exit(2)
with open(log_path, "a") as fh:
    fh.write(json.dumps({"argv": argv, "stdin": stdin_data}) + "\n")


def out(obj):
    print(json.dumps(obj))
    sys.exit(0)


def fail(key):
    f = scenario.get("fail", {}).get(key)
    if f:
        sys.stderr.write(f.get("stderr", "boom") + "\n")
        sys.exit(f.get("rc", 1))


if argv and argv[0] == "api":
    path = next((a for a in argv[1:] if a.startswith("repos/")), "")
    # error injection keys: 'labels/<n>', 'events/<n>', 'check-runs', 'pulls/<sha>'
    parts = path.split("/")
    if path.endswith("/labels") and "-X" not in argv:
        n = parts[4]
        fail(f"labels/{n}")
        out(scenario.get("labels", {}).get(n, []))
    if path.endswith("/labels") and "-X" in argv:
        n = parts[4]
        fail(f"labels-post/{n}")
        out({})
    if path.endswith("/events"):
        n = parts[4]
        fail(f"events/{n}")
        seq = scenario.get("events_seq", {}).get(n)
        if seq:
            # position persists across invocations in a side file
            posfile = os.environ["FAKE_GH_SCENARIO"] + f".seqpos-{n}"
            pos = 0
            if os.path.exists(posfile):
                pos = int(open(posfile).read() or 0)
            _atomic_write(posfile, str(pos + 1))
            item = seq[min(pos, len(seq) - 1)]
            if isinstance(item, dict) and "__fail__" in item:
                f = item["__fail__"]
                sys.stderr.write(f.get("stderr", "boom") + "\n")
                sys.exit(f.get("rc", 1))
            out(item)
        out(scenario.get("events", {}).get(n, [[]]))
    if "/commits/" in path and path.endswith("/pulls"):
        sha = parts[4]
        fail(f"pulls/{sha}")
        pulls = scenario.get("pulls", {}).get(sha, [])
        if "--jq" in argv and argv[argv.index("--jq") + 1] == ".[].number":
            for p in pulls:
                print(p["number"])
            sys.exit(0)
        out(pulls)
    if "/commits/" in path and "check-runs" in path and "-X" not in argv:
        sha = parts[4]
        fail(f"sha-checks/{sha}")
        runs = scenario.get("check_runs", {}).get(sha, [])
        # gh api --paginate --slurp emits one array of page objects
        if "--paginate" in argv:
            out([{"check_runs": runs, "total_count": len(runs)}])
        out({"check_runs": runs, "total_count": len(runs)})
    if "/check-runs/" in path and "-X" in argv and "PATCH" in argv:
        run_id = int(path.split("/check-runs/")[1])
        fail(f"patch/{run_id}")
        for runs in scenario.get("check_runs", {}).values():
            for r in runs:
                if r["id"] == run_id:
                    app = r.get("app", "github-actions")
                    slug = app.get("slug") if isinstance(app, dict) else app
                    if slug != "github-actions":
                        sys.stderr.write("Resource not accessible by integration\n")
                        sys.exit(1)
                    out(r)
        sys.stderr.write("Not Found\n")
        sys.exit(1)
    if path.endswith("/check-runs"):
        fail("check-runs")
        out({"id": 1001})
    out({})
if argv and argv[0] == "pr":
    if argv[1] == "view":
        # gh pr view <url> --json autoMergeRequest --jq ...
        # fleet disarm contract: state read defines success, not the
        # mutation exit code (ST scripts/disable-pr-automerge.sh)
        fail("pr_view")
        print("armed" if scenario.get("armed", False) else "off")
        sys.exit(0)
    if argv[1] == "comment":
        n = argv[2] if len(argv) > 2 else "?"
        fail(f"comment/{n}")
        out({})
    if argv[1] == "merge":
        # enforce the real contract (cli.github.com/manual/gh_pr_merge):
        # --disable-auto disables; --auto arms.  Unknown/legacy flags fail.
        known = {
            "--auto",
            "--disable-auto",
            "--match-head-commit",
            "--squash",
            "--merge",
            "--rebase",
            "-d",
            "--delete-branch",
        }
        for a in argv[2:]:
            if a.startswith("-") and a not in known:
                sys.stderr.write(f"unknown flag: {a}\n")
                sys.exit(1)
        if "--disable" in argv:
            sys.stderr.write("unknown flag: --disable\n")
            sys.exit(1)
        if "--disable-auto" in argv and not scenario.get("armed", False):
            sys.stderr.write("auto-merge is not enabled for this pull request\n")
            sys.exit(1)
        fail("pr_merge")
        out({})
    out({})
out({})
