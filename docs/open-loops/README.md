# Open-Loops Routine Texts

The fleet's open-loops routines (a daily digest, a daily backup-health line and a weekly landing-sweep report) run as a pair on BotFleet and on Grok Bot.  Both platforms run the same text.  This folder is the public pointer.  The texts themselves are private, and they never enter this repository.  Only their checker (`scripts/check-routine-prompts.py`) and its synthetic fixtures (`scripts/tests/fixtures/routines/`) live here.  Two ASCII spaces between sentences.

## Where the Texts Live

| Place | What is there |
| --- | --- |
| `Simple-With-Us/fleet-loops-data`, `docs/routines/` | The canonical copies.  A private repository with no GitHub Apps and no Actions.  Copied in by the open-loops live step L18a |
| `/Users/jay/apps/state/routines-private/` | The Mac staging folder where they are written and checked first.  Mode 0700, files 0600, outside every worktree so no `git add -A` or idle commit can reach them |

Never copy a text into a lane, into this repository, into a PR body or into chat.  Findings from the checker print a file, a line and a rule name and never the text.

## The Six Texts

| File | What it is | Who uses it | How it reaches the routine |
| --- | --- | --- | --- |
| `open-loops-digest.md` | Core:  one direct message to Jay at 7:00am Central | BF-Deployer and GB-Deployer | BF:  the digest installer (unit C2-2) POSTs it with the BotFleet block.  GB:  pasted with `gb-deployer.md` |
| `backup-health.md` | Core:  one status line at 8:00am Central in the backup topic, plus the taking-it protocol | BF-Plumber and GB-Plumber | BF:  the backup-health installer (unit P2).  GB:  pasted with `gb-plumber.md` |
| `weekly-landing-sweep.md` | Core:  the Monday report-mode sweep | BF-Housekeeper (9:00am) and GB-Housekeeper (3:00pm) | BF:  the sweep installer (unit C5-0).  GB:  pasted with `gb-housekeeper.md` |
| `gb-deployer.md` | Grok Bot block for the digest | GB-Deployer | Pasted once |
| `gb-plumber.md` | Grok Bot block for backup health | GB-Plumber | Pasted once |
| `gb-housekeeper.md` | Grok Bot block for the sweep | GB-Housekeeper | Pasted once |

The three cores are shared unchanged by both platforms.  A BotFleet block (written by C2-2, P2 and C5-0) goes after the core.  A Grok Bot routine is its block first, then its core, joined by a line of `---`, pasted as one routine.  Each pasted pair must stay under 20,000 characters, and the checker measures the pair, not only the files.

## Grok Bot Blocks Upgrade Themselves

Each block does the same five things, in order:

1. Calls `GET /users/me` and stops unless the email is its own `<role>-grok-bot@` address.
2. Verifies Jay's Zulip user id, written in the text with its provenance, with `GET /users/<id>` (a human with the owner role) before any direct message.
3. Tries the shared tool first, from a fresh `--depth 1` clone of this repository's main branch.  It runs the tool with `--no-mac --mac-facts <signed file from fleet-loops-data>` when the tool exists.
4. Otherwise gathers natively with GitHub and Linear reads and says "gathered natively".
5. Posts and direct-messages by the Zulip REST API under its own label `[GB-<ROLE>]`, and ends its chat with "sent".

So when the shared tools land on main (open-loops gather, the fleet-facts reader, `lane sweep`), a pasted block starts using them with no re-paste.  Each block also carries the trigger line (`CRON_TZ=America/Chicago` plus a cron expression).

## Placeholders to Fill Before the Copy

The digest and the sweep post in topics named after the Linear ids of the C2 and C5 component issues.  The staged texts carry `<C2 ID>` and `<C5 ID>` until those issues exist.  Replace them, then run the checker with `--no-placeholders`, and only then copy the texts to fleet-loops-data.

## Changing a Text

1. Edit the staged copy (or fetch the fleet-loops-data copy to the staging folder).
2. Run the checker over the folder (below).  It must print its pass line.
3. Copy the changed file to `docs/routines/` in fleet-loops-data with a sha-aware `gh api -X PUT`.
4. Make the platform pick it up:  re-paste the Grok Bot routine, or re-POST the BotFleet routine through its installer.  A core change must reach both platforms the same week, so the pair stays comparable.

## The Checker

```
python3 scripts/check-routine-prompts.py DIR_OR_FILE ...
python3 scripts/check-routine-prompts.py --private --no-placeholders /Users/jay/apps/state/routines-private
python3 scripts/check-routine-prompts.py --join gb-deployer.md open-loops-digest.md
python3 scripts/test_check_routine_prompts.py
```

It exits 0 when clean, 1 on any finding and 2 on a usage error.  CI runs the tests and the checker over the good fixtures on every push.  The rules:

| Rule | What it flags |
| --- | --- |
| `too-long` | More than 20,000 characters (BotFleet's routine prompt cap, counted in UTF-16 units) |
| `typed-mention` | A hand-typed Zulip mention.  Mentions are built at run time from the live user list |
| `code-path` | A path under `~/Code`, the human's integration tree |
| `slack-helper` | The retired Slack helper or a Slack endpoint |
| `single-space-gap` | A sentence boundary with one ASCII space.  Fences, inline code, URLs, `e.g.` and list markers are skipped |
| `reminder-missing` | The owner's standing reminder is not quoted verbatim |
| `secret-shaped` | A secret-shaped string, by the agent-sync secret scanner plus a local copy of the Linear, Sentry, xAI, Infisical, Cloudflare and Coolify patterns |
| `gb-label` | A `gb-<role>.md` text without its own `[GB-<ROLE>]` label and `<role>-grok-bot@` email, or with another role's;  or any GB label in a core |
| `placeholder` | With `--no-placeholders`:  a leftover `<C2 ID>`, `<C5 ID>`, TODO or FIXME |
| `private-dir` | With `--private`:  the folder is not 0700, a file is group or world readable, or the folder is inside a git worktree |

Test fixtures:  `scripts/tests/fixtures/routines/good/` passes, and each file in `bad/` fails on exactly its own rule.  The too-long, secret-shaped, placeholder, private-dir and unreadable cases are built at test time, so no scanner ever sees a whole secret-shaped string in the source.
