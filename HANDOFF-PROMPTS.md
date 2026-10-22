# Handoff prompts — MM, Sun Oct 11 2026

Each block below is self-contained: paste it into a fresh chat. Verify anything
it asserts before trusting it; MM ran low on context and the last few items were
handed over mid-flight.

Create your lane with `AGENT_SEAT=<SEAT> ~/apps/lane new <app> <slug>`. Never work
in `~/Code/<App>`. Run AFC Python with `/opt/homebrew/bin/python3.14`.

---

## 1. Land PR #503 (AI-Fleet-Coordinator) — highest value, nearly done

```
Take over PR #503 in Simple-With-Us/AI-Fleet-Coordinator, branch
minimax/afc-skills-repo-trees-all-platforms.  Claim a Linear issue for it
under AFC before editing.

What it does.  Every fleet platform now has a repo-tracked skills tree
rendered for its own seat, not just .claude, .cursor and .grok.
repo_platform_copies() in scripts/fleet_skill_identity.py was derived from
each Seat's own dest so a new platform cannot be missed.  It adds
.minimax/skills among nine trees.  It also fixes a stale Linear team list
in board-ops and pins it with a drift test against TEAM_KEYS.

Why it exists.  MiniMax Code resolved board-ops from the repo's
.claude/skills, so the MM seat was handed a pack saying "This install is
for CLAUDE ... Zulip bot claude-bot@ ... credential file
~/.secrets/Zulip/Claude-zuliprc".  It also told MM that $(…), pipes and
&& chains "do not" work, which is false under MiniMax Code's
permissionMode: bypassPermissions.  The MM seat followed all of it.  With
.minimax/skills present the loader (priority 65) beats .claude/skills
(60) and .agents/skills (55).

What I changed on top of the original commit, and why.
- Reverted an edit to AGENT-SYNC.md.  That file is a generated mirror of
  the Fleet-OPS handbook and check-generated-mirror.py refuses an edit
  there;  CI failed in 6s and was right to.
- Removed the "Shared ~/.claude/skills ... other tools scan it too ...
  must take identity from their own pack" banner.  Its premise is now
  false and it was ineffective:  the pack told MM not to be read by MM
  while the loader handed it over anyway.
- Narrowed the team-list drift test to files this repo owns, because it
  had been requiring AGENT-SYNC.md to carry the list, which the repo
  forbids editing.
- Renamed that test's assertion, which had required the banner to exist.

State when I ran out of context:  77/77 in
`cd scripts && python3.14 -m unittest test_fleet_skill_identity` pass
locally.  I never saw CI go green.  Confirm before claiming it is done.

Your job:  check CI, fix anything red, then merge with
`gh pr merge 503 --squash --auto`.  If CI is red from something
unrelated to skills, say so rather than fixing it silently.

Do NOT regenerate platform tool homes.  `install-fleet-skills.py
--repo-only` writes repo trees only;  without that flag it overwrites real
homes in ~ and that is a separate owner-approved step.

Verify after merging that .minimax/skills/board-ops/SKILL.md says MM,
[MM·session8], minimax/, and contains zero occurrences of CLAUDE.
```

---

## 2. SWU is not routable — `board file --app SWU` misfiles into AFC

```
Real bug, small fix, worth doing today.

`board file --app SWU` silently files Simple With Us website issues into
the AFC team with an app:swu label.  Reads look fine —
`board list --app SWU` returns 18 real issues — so the problem only
appears on write.

Cause, in scripts/linear-board/board, route_app():
    if a.upper() in TEAM_KEYS:        # SWU is absent
        return a.upper(), None
    t = BOARD_SLUG_TEAM.get(a.lower())  # no "swu" entry
    if t:
        return t, None
    return FALLBACK_TEAM, slugify(a)   # -> ("AFC", "swu")

Fix.  Add "SWU" to TEAM_KEYS at scripts/linear-board/board:179, AND to
FALLBACK_TEAM_KEYS in scripts/fleet_facts/check_fixtures.py.  The second
one is not optional:  it is a hand-maintained mirror of TEAM_KEYS, pinned
by fleet_facts.tests.test_fixtures_check.RealTree.test_team_keys_match_the_board_script,
and omitting it fails CI exactly the way it failed once already today.

Then add a routing test next to test_fleetlink_files_to_its_own_team in
scripts/linear-board/test_board.py, and add "CC" to the mock team list in
that file's setup while you are in it.

Verify:  `board list --app SWU` still returns the 18, and a dry inspection
of route_app("SWU") returns ("SWU", None) rather than ("AFC", "swu").
Do not actually file a test issue unless you clean it up after.

Note:  FLL stays out of TEAM_KEYS on purpose.  fleetlink-legacy still
serves fleetlink.online and fleetlink.app from its own wrangler.toml and
has no Linear team of its own.  Do not add it.

Branch minimax/<id>-swu-routing, PR it, merge it.
```

---

## 3. Merge fleet-ops PR #98 (handbook team list)

```
fleet-ops PR #98, branch minimax/linear-teams-fl-cc, is open and pushes a
one-line correction to the source that generates AGENT-SYNC.md.

handbook/work/linear-claims.md, the "What Linear Is" bullet:
-  ... OPS, CK
+  ... OPS, CK, FL, CC, SWU

FL (FleetLink) and CC (CodeCaps) got their own Linear teams on Oct 11 and
205 issues moved onto them.  SWU is the Simple With Us website team and
has 18 live issues;  I initially left it out believing it was only the
workspace root, which was wrong, and Jay corrected it.

This file is the handbook source, so the AFC mirror regenerates from it.
That is the only reason this is a PR to fleet-ops rather than an edit in
AI-Fleet-Coordinator, where check-generated-mirror.py refuses a direct
edit.

Your job:  review it, and merge if fleet-ops CI is green.  Read the
one-line diff rather than trusting this description.

Coordinate with task 2.  Once this lands the handbook says SWU, and the
AFC drift test anchors to TEAM_KEYS, which lacks SWU until that fix
lands.  If task 2 is not done first the two lists will disagree the next
time anyone syncs them, and the drift guard will correctly fail.
```

---

## 4. AFC-257 — #alerts digest for server-side held-back wakes

```
Build AFC-257 in AI-Fleet-Coordinator.  It is already filed, claimed by
MM, and carries the owner decision, so start from the issue rather than
re-deciding anything.

The problem.  There are two listeners.  The Mac one is now visible after
AFC-255/256 (PR #498).  The hosted server listener writes a held-back
wake to /data/<SEAT>/owner-queue.jsonl and NOTHING reads it.  Held-back
means the seat was out of budget or the queue was full, so the wake never
ran and the message sits unread in that seat's inbox.  Entries past the
caps are evicted, and the eviction is recorded in the same unread place.
The Mac half of this was fixed today;  the server half is not.

Owner decision, Jay, Oct 11 2026, 4:44pm:  post to #alerts (which already
exists for watchdogs and CI failures —  do not create a channel), and
fire on ANY hold-back, not only on eviction.  Jay chose the louder
trigger knowingly after being told a seat at 39 of its 40 daily wakes
would post the same way.  Do not narrow it to eviction-only.

Design constraints from that decision, not negotiable:
- Coalesce every server seat into ONE post per window.  Ten seats holding
  at once must not be ten posts.
- Rate-limit on a window and post nothing when nothing is held.
- Name the seat, the reason and the count, so it is actionable rather
  than blame.  HOLD_REASONS in scripts/agent_sync/heldback.py is
  wakes_per_hour, wakes_per_day, per_topic_per_hour, owner_per_day,
  owner_per_topic_per_hour, usd_per_day, overflow.

Useful anchors:  scripts/agent_sync/daemon.py has _held_digest and the
lost notice;  heldback.py summary() already returns count, overflow,
evicted, oldest_age, per-reason counts and lifetime totals, so most of
the payload exists.  AFC-255's _guard_drop is a good model for a
once-per-window notice:  it uses notifier.notify(..., once_per=...).

Tests and docs.  Two ASCII spaces between sentences in all prose;  the
Kody autofix bot will collapse them otherwise.

Note the irony worth avoiding:  the repo already had a banner warning
other seats off .claude/skills, and it did nothing because the loader
had already chosen the file.  A notice nobody reads is not a control.
```

---

## 5. Muse Spark 1.3 Contributor as the listener's headless wake

```
Build a new headless wake type for the agent-sync listener in
AI-Fleet-Coordinator, running muse instead of claude.  File and claim a
Linear issue under AFC first.

Owner direction (Jay, Oct 11 2026):  do not optimise for cost.  Only two
things matter — not spamming the fleet, and not causing downstream cost
by fanning one wake out into many.  Pick on technical merit.  Keep the
existing caps purely as a spam and cascade guard.

The runtime:  ~/.local/bin/muse, subcommand `muse exec`.
    muse exec --model muse-spark-1.3-contributor \
              --reasoning-effort none \
              --prompt-file <wake contract> --output-schema <schema> \
              --json --approval-mode never \
              --disable-shell --disable-write --disable-web-tools \
              --no-session-log --workspace <inert dir> \
              --max-model-steps N --no-foreign-personal-context

The model id is muse-spark-1.3-contributor, NOT muse-spark-1.3.  The
catalog carries three variants — muse-spark-1.3,
muse-spark-1.3-contributor, muse-spark-1.3-internal — and the plain one
is 15x dearer.  The contributor tier still reports
default_reasoning_effort: high, so --reasoning-effort none must be
passed explicitly or the saving never happens.  Confirm with
`muse model-profile show muse-spark-1.3-contributor --effort none`.

Why this runtime over the claude wake it would replace:
- --output-schema shapes the final answer to a JSON schema, which is the
  entire contract the wake depends on.  Constraint:  meta provider only,
  and meta is the default.
- The sandbox is ON by default;  --disable-sandbox is what turns it off.
  The claude wake had to work to reach an equivalent state.
- --disable-shell, --disable-write and --no-session-log are separate
  explicit switches, so "read this topic, touch nothing" is stated.
- --approval-mode never cannot block on a prompt.

Mapping from the current claude argv in scripts/agent_sync/adapters.py:
claude_argv uses -p, --output-format stream-json, --json-schema,
--max-turns, --permission-mode dontAsk, --tools "", --max-budget-usd
0.25, --no-session-persistence, --append-system-prompt-file.  Map each
to the muse equivalent above.  Note there is NO per-run dollar cap, and
a subscription-backed wake has no marginal cost, so the $0.25-per-wake
reservation that feeds the held-back and eviction machinery has nothing
to reserve against.  Decide deliberately how a subscription-backed seat
is capped (count caps such as wakes_per_hour 6, wakes_per_day 40,
per_topic_per_hour 2, owner_per_day 20 from config.py:48-49 still make
sense as a spam guard) and say so in the PR body.

Adding the wake type:  WORKER_WAKES is ("claude", "http") in
scripts/agent_sync/config.py:39 — there is no muse adapter today.  Give
it its own binary pin and a test-wake equivalent so it is verified
before it is trusted, the same discipline AFC-256 applied to a stale
claude pin.  A seat whose pin has never been verified should HOLD and
say why, never self-heal:  the pin records that a person checked that
build's wake contract.

FIRST PROVE THIS, before writing any adapter:  execute one `muse exec`
with the real wake schema in --output-schema and confirm the wake
contract survives.  This has never been run.  If the schema does not
round-trip, stop and report rather than building on it.

Also consider mcode exec as a second candidate — it has the same shape
(--output-schema, --permission off, --max-steps, --timeout).  grok
looked TUI-first.

Never modify a running seat's wake until it is verified and the owner has
switched it over deliberately.  Build it alongside claude.
```

---

## 6. Small cleanup — untracked stale skill tree

```
There is an untracked .agents/skills directory in the integration tree at
/Users/jay/Code/AI-Fleet-Coordinator, dated Oct 5, carrying MONET
identity.  It is untracked, so it is not in git and not in any PR.

It sits at loader priority 55, below .minimax/skills (65) and
.claude/skills (60), so after PR #503 lands it can no longer win.  It is
nevertheless still a wrong-identity file on disk.

Confirm it exists and carries a dead seat identity, then ask Jay before
deleting — deleting is not reversible and it is his tree.  Do not add it
to git, and do not commit a deletion of a file git does not track.
```