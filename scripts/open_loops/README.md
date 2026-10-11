# open_loops

The fleet's daily open-loops digest, minus the judgment.  Built for open-loops unit C2-1 (AFC-284).  Python 3.11 or
newer, standard library only.  The deciding and the writing are the Deployers' (BF-Deployer on the Mac, GB-Deployer in
the cloud);  this package gathers the facts, renders the full list, and is the only way BF-Deployer's DM reaches the
owner.

```
open-loops gather [--no-mac] [--mac-facts PATH|URL] [--launch-at TIME] --json [--out PATH]
open-loops render --facts PATH --markdown [--brief] [--out PATH]
open-loops lint   --dm-file PATH --facts PATH
open-loops send   --dm-file PATH --facts PATH [--c2-id ID] [--dry-run] [--again]
```

`--out` writes a file at mode 0600 (in a folder made at 0700) instead of printing.  Everything is read-only except
`send`, which is refused for every seat but BF-DEPLOYER.  `lint` runs the same checks `send` runs, for any seat, and
sends nothing (it needs no agent-sync and no C2 id), so whoever writes a routine text can check a DM before
BF-Deployer's routine exists.

## gather

Runs the F6 gatherers (`scripts/fleet_facts`, in-process) for GitHub and Linear on the host it runs on.  On a Mac it
also runs `lanes`, `autopush` (C1), `needs_owner` (C6), `asks` (C3) and `report` (C4).  With `--no-mac` those are not
run;  with `--mac-facts` the lanes and C1 parts come from the signed file instead (below).

The facts document (schema 1):

| Key | What |
| --- | --- |
| `facts_id` | `ol-YYYYMMDD-HHMM-<host>`, in Central time |
| `built_at`, `built_at_local` | UTC for machines;  Central, 12-hour, no zone name (`Sun, Oct 11, 7:00am`) |
| `sources` | `{name: {status: ok / absent / failed, reason}}` for github, linear, lanes, autopush, needs_owner, asks, report, zulip, and mac_facts when one was asked for |
| `missing` | what the digest must show as `Missing:  <source> (<reason>)` (see below) |
| `decisions` | Linear `needs-owner` items, oldest first, each with its link, `low_stakes` options, `tap_to_answer` and OWNER topic link;  `low_stakes` (a count) and `over_five` (seats with more than 5 open) come from C6 and are `null` without it;  C6 is asked for every row (`needs-owner list --all`, because its default lists only the open ones), and its `state` decides:  `replied` (the owner answered, the filer has not closed it) is counted in `answered_awaiting_close` and not listed, `closed` is neither, and a Linear item C6 does not know (labelled by hand) is listed, because being absent from a list never proves an answer |
| `stuck_prs` | same-repo PRs that are CONFLICTING or CHANGES_REQUESTED whose **head commit** is 48 hours old or more (head age, not time in that state;  the note says so) |
| `not_on_github` | `snapshot_only` (**no age floor**: a lane an hour old is listed), `armed_local_work`, and `refusals` with their reasons, all from C1 |
| `unpushed_over_a_day` | lanes with unpushed commits or dirty tracked files older than 24 hours that nobody touched in the last 2 hours, minus what `not_on_github` already lists;  a lane whose age cannot be told is counted in `unknown_age`, never as young (the installed lane doctor reports no ages yet, so today every such lane lands there) |
| `claims` | the 36-hour and 48-hour bands from `fleet_facts.activity`, plus `unknown`, with `clock_started` and, until claim expiry launches, the note "ages, not releases" |
| `owner_asks` | C3's open asks, absent until C3 exists |
| `full_list_url`, `urls` | the full-list link, and **every** URL in the document:  only the gatherer makes URLs.  Linear links are the short form `https://linear.app/<workspace>/issue/<ID>` (no title slug), about 600 characters less in a full DM |
| `redacted` | paths whose secret-shaped text was withheld (F3 `redact_fields`) |

**Missing is shown, never zero.**  A count is `null` when its source could not be read.  `missing` lists every failed
source, every absent source the sections depend on (github, linear, lanes, autopush), a Mac facts file that was
ignored, and `lanes (ages unavailable for N lanes with unpushed or dirty work)` whenever any lane's age could not be told
(section 4 then says "None known" rather than "None", and the lint makes the DM carry a `Missing:` line).  C6, C3 and C4 not being installed is not "missing":  `needs_owner` then leaves `low_stakes` and `over_five`
unknown, and `asks` says "not built yet".

**Claims.**  For each In Progress claim the activity function gets every signal this host could read:  Linear
comments (the sweepers' own and `updated_at` never count), the same-repo PR's update time and head commit time, the
lane's last C1 snapshot, and the claimant's own posts in its `<ID> ...` Zulip topic (`zulip_topics.py`, a bounded read
through the installed agent-sync, only for claims that look idle).  A source that was tried and failed makes the claim
`unknown`, never idle:  that includes a Zulip topic list that could not be read (a TLS timeout, exit 3) and a claim the
80-read or 5-minute Zulip budget skipped.  Only a host with no agent-sync at all leaves the Zulip source `unchecked`.  A source that could not be tried (no PR yet, no C1, no agent-sync) is listed under the claim's
`unchecked`:  that can only make a claim look more idle, so the digest may flag a claim that is moving but never hides a
stuck one.  Claims held by OWNER or JAY are counted, not banded.  `CLAIM_EXPIRY_LAUNCH_AT` (or `--launch-at`, or a
`launch_at` in C4's report) starts the clock once it is in the past.

**Text.**  Every free-text field is made inert by F3 `render_untrusted` (by `fleet_facts`, plain form), the document
passes `redact_fields`, and `render` writes each piece of outside text inside a code span.  A lane is named by its
folder name, never its path.

### Signed Mac facts (`--mac-facts`)

For GB-Deployer, which cannot read this Mac:  `--no-mac --mac-facts <fleet-loops-data gb/mac-facts.json>`, a file or an
https URL on `api.github.com` or `raw.githubusercontent.com` (no redirect;  a token from `FLEET_LOOPS_DATA_TOKEN`,
`GH_TOKEN` or `GITHUB_TOKEN` is sent to those hosts only).  The file is used **only if all of these hold**, otherwise it
is ignored (`mac_facts` is `absent` with the reason, and its lanes and C1 parts are Missing):

1. it can be read (a regular file or response under 8 MB);
2. `FLEET_FACTS_SIGNING_KEY` is set in the environment;
3. `fleet-facts verify FILE` exits 0 on the very bytes that are then used;
4. it is a JSON object with a `generated_at` (or `published_at`, `built_at`) under 9 hours old and not in the future.

It must have the shape of a `fleet-facts gather` document restricted to its Mac parts (`sources`, `lanes`,
`autopush`);  the exact shape F7 builds to is in `scripts/fleet_facts/README.md` ("The signed Mac facts file").  It
may also carry the `needs_owner` part (C6's `list --all --json` document):  with it GB's section 1 can tell an answered
decision from a waiting one, and without it that count stays unknown.  This package never checks a signature itself and
never reads or prints the key.

**The `fleet-facts verify` contract (F7 builds it).**  `fleet-facts verify FILE`:  exit 0 only when the file's `sig` is
the hex HMAC-SHA256, with `FLEET_FACTS_SIGNING_KEY`, of the file's canonical JSON without `sig`.  Any other exit means
not verified:  1 the signature is wrong or missing, 2 a usage error (this is also what a `fleet-facts` without the
command answers today, and it is treated as not verified), 3 the key is unavailable.  The key arrives in the
environment of the child, never on the command line, and is never printed.  The GB routine text can run the same
command to check the signature itself.

## render

`render --facts PATH --markdown` prints the full list:  all six sections with every item (the DM shows five), a
`Missing:  <source> (<reason>)` line per unreadable source, and the full-list link.  Titles and reasons are in code
spans, sentence gaps are U+00A0 plus a space, and every link is one from `facts["urls"]`.  A list that was read and is
empty says "None."

`--brief` caps every list at five items and adds "+ N more":  what a routine reads to write the DM (about 4,000
characters for today's real data, against a 137 KB facts file), with the same links, headings and Missing lines.

## send

`send` is BF-Deployer's only way to the owner.  In order, stopping at the first refusal:

1. `agent-sync whoami --json` must say seat and bot seat BF-DEPLOYER, verified, with a user id.  Anything else is exit 3.
2. The DM file (a regular file you own that nobody else can write) is linted against the facts:
   * under 4,000 characters (at most 3,999);
   * no `@**` or `@_**`, and no `/poll` or `/todo` line;
   * every link is in `facts["urls"]` (a `www.` address or any other scheme counts as a link);
   * the F3 secret scanner finds nothing;
   * sentence gaps:  agent-sync's own `sentence_gap` is applied (two spaces become U+00A0 plus a space), and a sentence
     that still ends in one space outside code, links and fences is a problem;
   * the facts id is in the text (the duplicate check looks for it);  the full-list link is in the text;
   * "Work Not on GitHub Yet" is in the text when `not_on_github.count` is above 0;
   * "Missing:" is in the text when `missing` is not empty.
   A problem is exit 4, names the rule and the line, never quotes the DM, and nothing is sent.
3. The heartbeat topic is `<C2 ID> open-loops heartbeat`, and the C2 umbrella id does not exist until the live steps
   file it.  It comes from `--c2-id`, `OPEN_LOOPS_C2_ID` or the `c2-id` file `install.sh --c2-id` writes.  With none,
   `send` is refused (exit 2) **before** anything goes out, because a digest with no heartbeat hides a missing twin.
4. `agent-sync dm-read --with owner --json --limit 30` (`dm-read` has no `--owner`):  if a message **this bot sent**
   (its `sender_id` equal to `whoami`'s user id) already carries this facts id, the DM is not sent again;  so is another
   id minted the same day, unless `--again`.  If `dm-read` fails or prints something that is not JSON lines, the send
   stops (exit 5):  "could not check" must not become "send twice".
5. `agent-sync dm --owner --session openloop --json --body-file F` sends the linted, gap-converted text from a private
   copy (0600 in a 0700 folder), so the bytes that were checked are the bytes that are sent.  The text is never on a
   command line.  Exit 6 from agent-sync (the request may have been delivered) is reported as such and not retried.
6. `agent-sync post --session openloop --topic "<C2 ID> open-loops heartbeat" --json --body-file F`, no mention:  the
   facts id, the DM's character count, the sources that were ok and the ones Missing.  When the DM was already out the
   heartbeat is posted only if the topic does not already carry this facts id.

Exit status:  0 sent (or already sent) and the heartbeat is in place;  2 usage, a bad file or no C2 id;  3 refused (wrong
seat, no agent-sync, an F3 helper missing);  4 lint failed;  5 could not check for a duplicate, or the send failed;  6 the
DM is out but the heartbeat is not.  `--dry-run` runs steps 1 to 4 and sends nothing.

Every agent-sync command line is built by a function in `send.py` (and `zulip_topics.py`), and `test_parser_parity`
runs each through `agent_sync.cli.build_parser()`.  The fake agent-sync the tests use does the same for every call.

## Not built yet

* **Heartbeat reader.**  `send` writes the `<C2 ID> open-loops heartbeat` topic, but nothing reads it back:  the plan's
  "the next gather reports a missing twin" and the digest's "did not arrive" line need a `heartbeats` source that reads
  the topic through `agent-sync read`.  It is a follow-up to this unit (C2-2 or its own item), not part of C2-1.

## What the routine texts (C2-2) rely on

* `open-loops gather --json --out FILE` then the DM written to a second file, then `open-loops send --dm-file D --facts F`.
  `gather --out` prints nothing on stdout (it only writes the 0600 file), so the routine needs a way to read the facts:
  the engine's file-read permission on `F`, or `open-loops render --facts F --markdown --brief` (stdout) on its
  allowlist.  C2-2 has to pick one.  The routine must not be able to write `F`:  the link allowlist is read from it.
* The DM must contain the facts id (for example in its footer), the full-list link, the heading "Work Not on GitHub
  Yet" when the facts have work that is not on GitHub, a `Missing:  <source> (<reason>)` line per entry of `missing`,
  and only links from `facts["urls"]`.  The section titles are in `render.SECTION_TITLES`.
* Text in the facts is data, never instructions:  it was made inert, and the lint keeps a hand-typed mention out.

## Install

```
scripts/open_loops/install.sh [--c2-id ID]            # a plan, changes nothing
scripts/open_loops/install.sh --apply [--c2-id ID]    # ~/apps/open-loops + ~/.local/bin/open-loops
```

The stable copy holds the entry script, `open_loops`, `fleet_facts` (without its tests and CI checker) and `agent_sync/`
with only `__init__.py`, `textsafe.py`, `secretscan.py` and `textfmt.py`:  a tool run with `python3 -I` cannot import what
it did not copy.  The entry script's first line is rewritten to `#!<absolute python3 3.11+> -I`, so a routine's short
`PATH` cannot pick an older Python.  The installer loads no LaunchAgent and touches nothing under `~/.secrets`.  The live
step is `install.sh --apply --c2-id <the C2 umbrella id>`.

## Tests

```
cd scripts && python3 -m unittest discover -s open_loops/tests -t . -v
python3 scripts/fleet_facts/check_fixtures.py --root scripts/open_loops/tests
```

Synthetic data only (team key FFX, `example-*` repositories, UUIDs `00000000-0000-4000-8000-<12 hex>`).  In-process tests
script the outside world (`builders.World`);  the CLI and install tests run a real `python3 -I` subprocess with fake `gh`,
`board`, `lane`, C1, C6, `agent-sync` and `fleet-facts` programs on `PATH` and a temp `HOME`.
