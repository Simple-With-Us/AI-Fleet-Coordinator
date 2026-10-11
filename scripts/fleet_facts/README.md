# fleet_facts

Read-only gatherers for the fleet's open-loops digest, and the one definition of a claim's activity.
Built for open-loops unit F6 (AFC-283).  Python 3.11 or newer, standard library only.  C2-1 (the digest), C4-1
(claim expiry) and C5 (the landing sweep) call it;  F7 publishes the Mac side of it.

```
fleet-facts gather [--part NAME]... [--no-mac] [--json]
```

| Part | Reads | Mac only |
| --- | --- | --- |
| `github` | one GraphQL census of the open PRs in the org (`github_prs.py`) | no |
| `linear` | claims, orphans and needs-owner items, through the installed `board` CLI (`board.py`) | no |
| `lanes` | the lane doctor report, `lane ls --json` (`lanes.py`) | yes |
| `autopush` | C1 `fleet-autopush status --json` (`interfaces.py`) | yes |
| `backup` | P1 `fleet_work_backup.py health --json` (`backup.py`) | yes |
| `asks` | C3 `agent-sync asks status --json` | yes |
| `report` | C4 `~/apps/claim-expiry/state/report-latest.json` | yes |
| `needs_owner` | C6 `needs-owner list --json` | yes |
| `consult` | C7 `consult status --all --json` | yes |

With no `--part` every part runs.  `--no-mac` is for a cloud host:  every Mac-only part is marked absent and is
not run.  The command is read-only.  It reads GitHub (one search), Linear (list and show verbs) and local
state, and it writes nothing except the lane report cache `~/.cache/fleet-facts/lanes.json` (0600).

Exit status:  0 the document was built (a part may still be `failed`;  see its `sources` entry and stderr),
2 usage, 3 an F3 helper is missing or Python is too old (nothing is printed), 1 an unexpected error.

## The document (schema 1)

```json
{"schema": 1, "tool": "fleet-facts", "version": "1.0.0", "generated_at": "2026-10-11T09:00:00Z", "host": "...",
 "no_mac": false, "parts": ["github", "..."],
 "sources": {"github": {"status": "ok", "reason": null},
             "lanes":  {"status": "absent", "reason": "--no-mac"},
             "linear": {"status": "failed", "reason": "board exited 5"}},
 "github": {"prs": [...], "outside_fork_prs": {"count": 1, "by_repo": {...}}, "pages": 3, "issue_count": 68, "mergeable_pending": 0},
 "linear": {"claims": [...], "orphans": [...], "needs_owner": [...], "in_progress_total": 165, "detail_failed": 0, "truncated": false},
 "lanes":  {"total": 344, "by_safety": {...}, "attention_count": 224, "attention": [...], "reused_report": false},
 "backup": {"report": {...}, "verdict": "ok"},
 "redacted": []}
```

`sources[...].status` is `ok`, `absent` (nothing to read here, or `--no-mac`) or `failed` (with a reason).  A
failed part may still carry partial data under its key.  `redacted` lists the paths whose secret-shaped text
was withheld.  Every free-text field was passed through F3 `render_untrusted` (plain form);  the whole
document passes F3 `redact_fields` before it is printed.

## The single activity definition (`activity.py`)

Pure functions:  no state, no I/O, no clock.  C2-1 and C4-1 import them, and a test fails if any other module
defines them.  A claim's last activity is the latest of

* a push to its branch (`branch_push`), or a C1 snapshot or auto-commit for it (`c1_snapshot`);
* an update to a same-repo PR from that branch (`pr_update`);
* any Linear comment on the issue except the sweepers' own, whose text starts `[claim-expiry]` or `[sweep]`
  (`linear_comments`;  any other seat's comment, or a person's, counts);
* a post by the claiming seat in the issue's `<ID> <subject>` Zulip topic (`zulip_topic`, supplied by C2-1);

and never earlier than `launch_at`, the launch time of claim expiry (the floor).  `claimed_at`, the claim's own
start, is a baseline too.  **Linear `updated_at` is never an input**:  the import and the relabel touched every
issue.  Nothing reads it, and a test pins that.

```python
from fleet_facts import activity

signals = {"linear_comments": claim["linear_comments"],              # straight from `fleet-facts gather`
           "pr_update": {"ok": True, "events": [{"at": "2026-10-10T14:00:00Z", "same_repo": True}]},
           "branch_push": {"ok": True, "events": []},
           "c1_snapshot": {"ok": False},                              # could not be read
           "zulip_topic": {"ok": True, "events": [{"at": "...", "by": "CLAUDE"}]}}
item = {"signals": signals, "launch_at": "2026-10-13T00:00:00Z", "claimed_at": claim["claimed_at"],
        "seat": claim["seat"], "required": activity.SOURCES}
activity.idle_band(item, "2026-10-14T12:00:00Z")      # "none", "36h", "48h" or "unknown"
```

A source that is not `{"ok": true, ...}` makes the result **unknown, never idle**:  a source that could not be
read is not evidence that nothing happened.  `idle_band` returns `unknown` in that case, and `unknown` is not
one of `BANDS` (`none`, `36h`, `48h`).  A caller that acts on a band tests for the band it acts on, never
"not none".  Pass `required=SOURCES` when a release depends on the answer, so a source nobody passed is
unknown too.  See the module docstring for the event shapes.

## Safety

* **GitHub.**  One search `org:Simple-With-Us is:pr is:open archived:false`, `first: 30`, the cursor variable
  named exactly `$endCursor`.  The module pages by hand, retries a 502, 503 or 504, fetches `mergeable: UNKNOWN`
  PRs again, and fails the source if a cursor or a whole page repeats.  A PR from outside the org leaves the
  module only as a count by repository.  PR URLs are built here, never taken from the response.
* **Linear.**  `board.py` runs `list` and `show` only and refuses any other verb in code, because the installed
  `board` wrapper turns writes on by itself.  Calls are sequential (the Fleet app's budget is shared), with a
  timeout each and a ten-minute budget for the detail pass;  a `show` that fails marks that issue's comments
  unreadable, which makes its activity unknown.  Comment text is dropped;  only author, time, a sweeper flag
  and a claimed flag are kept.
* **Lanes.**  A doctor report with `gh` or `lsof` not `ok`, a non-zero exit, a timeout (20 minutes) or an odd
  shape gives a failed source with no counts.
* **Text.**  Titles, labels, file paths, check names and reasons are untrusted:  cleaned with
  `render_untrusted` (no code span) and, as a whole document, redacted with `redact_fields`.  A branch name
  that is made only of plain characters stays exactly as git spelled it (it is a join key);  any other branch
  is cleaned and marked `*_exact: false`.

## Install (stable copy)

```
scripts/fleet_facts/install.sh            # a plan, changes nothing
scripts/fleet_facts/install.sh --apply    # ~/apps/fleet-facts + ~/.local/bin/fleet-facts
```

The stable copy holds the entry script, the package (no tests), the `mac-facts-publish` launcher that F7's
LaunchAgent will run, and `agent_sync/` with only `__init__.py`, `textsafe.py` and `secretscan.py`:  a tool
run with `python3 -I` cannot import what it did not copy, and `-I` leaves the script's own folder off
`sys.path`, so the entry script puts its real directory first.  A test installs into a temp HOME and runs
`python3 -I <stable copy>/fleet-facts`.  The installer loads no LaunchAgent and touches nothing under
`~/.secrets`.  `mac-facts-publish` calls `fleet-facts publish-mac`, which F7 builds;  until then it exits 3.

Another unit that imports `fleet_facts` (C2-1, C4-1, C5) copies this package and the two F3 helpers into its
own stable folder the same way.  C2-1 (`scripts/open_loops/install.sh`, stable copy `~/apps/open-loops`) does, and adds
a third helper, `textfmt.py`, which `safety.textfmt()` loads only when asked (the DM lint's sentence-gap rule);  this
package's own stable copy does not carry it.

## `fleet-facts verify` (built by F7;  the contract C2-1 relies on)

`open-loops gather --mac-facts` reads the signed `gb/mac-facts.json` and uses it only if `fleet-facts verify FILE`
exits 0.  F7 builds the command to this contract:

* `fleet-facts verify FILE` exits **0** only when the file's `sig` is the hex HMAC-SHA256, with
  `FLEET_FACTS_SIGNING_KEY`, of the file's canonical JSON without `sig`.
* Any other exit is "not verified":  **1** the signature is wrong or missing, **2** a usage error (what a `fleet-facts`
  without the command answers today), **3** the key is unavailable.
* The key comes in the environment of the command, never on the command line, and is never printed.
* The file is read once, from the path given.  The caller then uses the very bytes it handed over.

## Tests

```
cd scripts && python3 -m unittest discover -s fleet_facts/tests -t . -v
python3 scripts/fleet_facts/check_fixtures.py
```

Synthetic data only, built in `tests/builders.py` and `tests/fixtures/`:  repositories are `example-*`, Linear
identifiers use the team key `FFX` (not a real team), every UUID is `00000000-0000-4000-8000-<12 hex>`.
`check_fixtures.py` (a CI step, with its own planted-bad tests) fails on `/Users/` or `/Volumes/`, a real team
key plus a number, any other UUID, the owner's or the realm's names, or secret-shaped text.  The subprocess
tests put fake `gh`, `board`, `lane` and backup programs on PATH and use a temp HOME.
