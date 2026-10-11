# fleet_facts

Read-only gatherers for the fleet's open-loops digest, and the one definition of a claim's activity.
Built for open-loops unit F6 (AFC-283).  Python 3.11 or newer, standard library only.  C2-1 (the digest), C4-1
(claim expiry) and C5 (the landing sweep) call it;  F7 (AFC-287) publishes the Mac side of it, signed (see "Publishing the Mac facts").

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
| `needs_owner` | C6 `needs-owner list --all --json` (every labelled issue, answered and closed ones too) | yes |
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

## Publishing the Mac facts (open-loops F7)

```
fleet-facts publish-mac [--dry-run [--out DIR]]       one cycle:  what the LaunchAgent com.jay.mac-facts-publish runs every 3 hours
fleet-facts publish --path REL --file LOCAL [--dry-run]    sign (a .json file) or scan (anything else) one file and write it
fleet-facts verify FILE [--path REL]                       check a signed file
```

A cycle gathers the Mac parts (`lanes`, `autopush`, `backup`, `needs_owner`) and the full list (`open-loops gather` then
`open-loops render --markdown`), and writes up to three files to the PRIVATE repository `Simple-With-Us/fleet-loops-data` through
sha-aware `gh api -X PUT` calls (no clone, never forced), in this order:

| File | What it is |
| --- | --- |
| `gb/full-list.md` | the digest's full list;  its SHA-256 and size are in the signed JSON (`full_list`), which covers the Markdown.  When the list could not be built (open-loops not installed, a failed step, a secret in it) the file is replaced by a short "Full List Not Available" note with the reason words, and `full_list.status` and `full_list.placeholder` say so, so an old list is never read as today's |
| `gb/mac-facts.json` | the copy for Grok Bot:  no `path` key, no `~/`, `/Users/` or `/Volumes/` text, every string cut to 80 characters (a branch, repository, lane name, identifier and topic are never cut) |
| `mac-facts.json` | the full copy, with lane paths.  **Off by default**:  everything that can read the repository (Grok Bot's token included) can read it, which would undo the path-free copy.  The Infisical knob `FLEET_FACTS_PUBLISH_FULL_COPY` turns it on;  while it is off each run removes the file from the branch first thing, if an earlier run left one, before the key, the load check or the repository gate (a sha-aware delete, the only delete this job can make;  git history keeps the old commits) |

Both JSON files have the shape of `fleet-facts gather` (schema 1:  `generated_at`, `sources`, then `lanes`, `autopush`, `backup` and
`needs_owner` with the same data as the gatherers give) plus `kind`, `signed_for` (the path in the repository), `full_list`,
`sources.publish.redacted` (the paths of fields that were withheld, never their text), `sig_alg` and `sig`.  `open_loops.macfacts` reads
`gb/mac-facts.json` through exactly this;  `tests/test_publish_contract.py` runs the real digest over what the publisher wrote.

**Signature.**  `sig` is the HMAC-SHA256 of the object without `sig`, serialized as `json.dumps(obj, sort_keys=True,
separators=(",", ":"), ensure_ascii=True, allow_nan=False)` encoded as ASCII, keyed with the text of `FLEET_FACTS_SIGNING_KEY` (trimmed;  32 to 256
letters, digits and `+ / = _ -`), as lower-case hex.  `signed_for` and `sig_alg` are part of what is signed.  Any Python can check a file:

```python
import hashlib, hmac, json, os
doc = json.load(open("mac-facts.json")); sig = doc.pop("sig")
body = json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
ok = hmac.compare_digest(sig, hmac.new(os.environ["FLEET_FACTS_SIGNING_KEY"].strip().encode(), body, hashlib.sha256).hexdigest())
```

`fleet-facts verify FILE` does that and exits **0** verified, **1** not verified (unsigned, tampered, wrong key, not JSON), **3** cannot check (no usable key
in the environment);  **2** is only ever a usage error, because `open_loops.macfacts` reads 2 as "this fleet-facts has no verify command".  It
reads the key from the environment (the cloud and Grok Bot hosts have no Infisical);  `--path REL` also requires `signed_for` to equal `REL`.  To
check the Markdown, compare the SHA-256 of `gb/full-list.md` with `full_list.sha256` in the verified `gb/mac-facts.json`.  Freshness is the
consumer's job:  `generated_at` is the time the facts were gathered and is never refreshed on a reused file.

**The gate.**  Nothing is written unless `repo.guard()` has just said:  visibility PRIVATE, not archived, not a fork;  no webhook (deploy
webhooks included) and no deploy key;  and no GitHub App installed on the organization can read the repository (an installation on all
repositories can;  one on selected repositories is listed with `user/installations/<id>/repositories`).  Any read that fails is a refusal.  Apps the owner
accepts are named in the Infisical knob `FLEET_LOOPS_DATA_ALLOWED_APPS` (empty by default).  Not checked:  organization-level webhooks (they need the
`admin:org_hook` scope).  `publish --path` writes only `mac-facts.json` (only while the full-copy knob is on) or a file under `gb/` other than the two `publish-mac` owns (`gb/mac-facts.json` carries the list's hash, so it and `gb/full-list.md` are only ever written together) (no `..`, no dotfile, never `.github/`), and everything under `gb/` goes through the same path-free copy rule, by hand as in a cycle:  a JSON file is minimized before it is signed, and a Markdown file with a Mac path in it is refused.  The full list has its Mac paths replaced by `[path]` before it is hashed.

**Secrets.**  `FLEET_FACTS_SIGNING_KEY` comes from K1 (`fleet_knobs`, imported only by `publish` and `publish-mac`), is never cached or printed, and is also a
`known` value for the redactor, so it cannot be published even inside a field.  Each document gets per-field `redact_fields`, then a whole-document scan:  a
hit that survives refuses the cycle (nothing is written).  A secret-shaped string in the full list withholds just the list.  No key means nothing is published.

**Writes that do not finish.**  The list is written first, because the signed JSON carries its hash.  If the JSON write then fails, the new list is replaced by the not-available note, so a new list never stands beside the previous run's facts (but when the JSON landed and only the optional full copy failed, the list stays, because the JSON describes it) (git data commits would make the three files one commit;  this job stays on the contents API the plan names).

**Time and load.**  One lock (a second run exits), a wall-clock cap of 20 minutes (a timer ends the run and kills the children), every child's timeout
clamped to the time left, no write phase with under a minute left, and a skip when load1 is over 250.  The cap and the limit are the Infisical knobs
`FLEET_FACTS_PUBLISH_CAP_MINUTES` (1 to 60) and `FLEET_FACTS_PUBLISH_MAX_LOAD1` (1 to 1000);  a local setting can only lower them.  A skip writes nothing to the repository:  the
files stay, `generated_at` keeps ageing, and `~/.local/state/fleet-facts/mac-facts-publish.json` says `stale`.

Exit status of `publish-mac` and `publish`:  0 published, or skipped on purpose;  3 nothing published because a safety condition failed (no key, the gate, a
secret, a missing helper);  4 a write failed or the cap was reached;  2 usage.  The status file, the lock and the log
(`~/Library/Logs/mac-facts-publish.log`) hold results, counts and reason words, never fact text or the key.

Live steps after merge (the main session):  L4 (create the private repository;  decide the installed-apps rule), L5 (the key in Infisical), `scripts/fleet_facts/install.sh
--apply`, `fleet-facts publish-mac --dry-run` to look at the files, L15 (Removable Volumes for `~/apps/fleet-facts/mac-facts-publish`), L16c (load the LaunchAgent).

## Install (stable copy)

```
scripts/fleet_facts/install.sh            # a plan, changes nothing
scripts/fleet_facts/install.sh --apply    # ~/apps/fleet-facts + ~/.local/bin/fleet-facts
```

The stable copy holds the entry script, the package (no tests), the `mac-facts-publish` launcher that F7's
LaunchAgent runs, `fleet_knobs/` (K1, no tests;  `publish` and `publish-mac` fetch the signing key through it), and `agent_sync/` with only `__init__.py`, `textsafe.py` and `secretscan.py`:  a tool
run with `python3 -I` cannot import what it did not copy, and `-I` leaves the script's own folder off
`sys.path`, so the entry script puts its real directory first.  A test installs into a temp HOME and runs
`python3 -I <stable copy>/fleet-facts`.  The installer loads no LaunchAgent and touches nothing under
`~/.secrets`.  `mac-facts-publish` calls `fleet-facts publish-mac`.

Another unit that imports `fleet_facts` (C2-1, C4-1, C5) copies this package and the two F3 helpers into its
own stable folder the same way.  C2-1 (`scripts/open_loops/install.sh`, stable copy `~/apps/open-loops`) does, and adds
a third helper, `textfmt.py`, which `safety.textfmt()` loads only when asked (the DM lint's sentence-gap rule);  this
package's own stable copy does not carry it.

## `fleet-facts verify` (the contract C2-1 relies on;  built by F7)

`open-loops gather --mac-facts` reads the signed `gb/mac-facts.json` and uses it only if `fleet-facts verify FILE --path
gb/mac-facts.json` exits 0 (the path pin means the root `mac-facts.json`, which keeps lane paths, is never accepted as the Grok Bot copy,
even with a good signature).  The command meets this contract:

* `fleet-facts verify FILE [--path REL]` exits **0** only when the file's `sig` is the hex HMAC-SHA256, with
  `FLEET_FACTS_SIGNING_KEY`, of the file's canonical JSON without `sig` (and, with `--path`, the file's signed `signed_for` equals REL).
* Any other exit is "not verified":  **1** the signature is wrong or missing, **2** a usage error (what a `fleet-facts`
  without the command answers today), **3** the key is unavailable.
* The key comes in the environment of the command, never on the command line, and is never printed.
* The file is read once, from the path given.  The caller then uses the very bytes it handed over.

### The signed Mac facts file (what F7 publishes as `gb/mac-facts.json`;  the shape C2-1 reads)

A `fleet-facts gather` document restricted to its Mac parts, plus `sig`.  `open-loops gather --no-mac --mac-facts` uses
it only when `fleet-facts verify` accepts the very bytes and it is fresh;  anything else is ignored and the Mac parts
show as Missing.  Shape:

* a JSON object with a time it was built in `generated_at` (or `published_at`, or `built_at`), ISO 8601 with a zone,
  **less than 9 hours old** and not in the future (F7 publishes every 3 hours, so one missed run is still fresh);
* `sources`: `{part: {"status": "ok" | "absent" | "failed", "reason": ...}}` for each part below;
* `lanes`: the `lane doctor` report part, `{"attention": [...], "total": N}` (each record carries `repo`, `branch`,
  `unpushed`, `dirty_tracked`, `dirty_untracked`, `never_pushed`, `safety` and, when the doctor has them,
  `last_change_at`, `dirty_oldest_age_hours`, `never_pushed_age_hours`);  a record with none of the age fields cannot be
  aged and the digest says so;
* `autopush`: `{"document": <what fleet-autopush status --json prints>}`;
* `needs_owner` (optional): `{"document": <what needs-owner list --all --json prints>}`;
* `sig`: the HMAC-SHA256 above.  The publisher also writes `kind`, `signed_for`, `full_list`, `sig_alg` and `sources.publish`, which this reader ignores.

A part whose `sources` status is `ok` must have its data key, or it is read as failed.  Missing parts are Missing in the
digest, never zero.

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
