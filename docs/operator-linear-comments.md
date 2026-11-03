# The Operator's Zulip -> Linear living comments

The standalone Zulip -> Linear relay becomes a job of the Operator (owner directive,
2026-11-03).  This note is the inventory of what the relay is today, the design of the
fold-in, and the retirement sequence for the relay's bot and process.  States are kept
distinct throughout:  this lane BUILDS and TESTS the fold-in;  nothing here is deployed,
no live Linear comment is written by it, and the running relay is untouched until the
retirement sequence at the end runs, in order, after verification.

## The contract (owner rulings)

- **Just one comment per Zulip topic.**  The lifecycle is keyed by the topic (stream id +
  topic).  For each Linear issue mentioned in that topic, exactly one living comment on
  that issue:  created on the first mention in the topic, then EDITED in place as the
  topic grows — never a stream of new comments.  One topic naming two issues gets one
  comment on each issue;  two topics naming the same issue get separate comments, one
  per topic.
- **The comment covers the whole topic** (2026-11-03):  including posts from before the
  issue was first mentioned — a first mention in the third post still pulls posts 1–2
  in — and everything added afterward, whether or not it repeats the key.  The poster
  often didn't know there was a Linear issue, didn't find the related ones, or didn't
  make one, so the issue's comment carries the full thread context, not the thread from
  the mention onward.
- Public streams only;  never a direct message, never a private stream (2026-10-21).
  Human and bot posts both count.
- Issue-key extraction is deterministic and constrained to the fleet's real Linear team
  keys (`linear_relay.DEFAULT_TEAMS`, kept in step with `scripts/zulip_linkifiers.py`),
  matched in topic titles and message bodies.  The thread registry's sheet column
  harvests more broadly for display;  comments are written only for real team keys.

## Inventory: the relay as it stands

- **Code:** `scripts/agent_sync/linear_relay.py` (this repo, on main).  A standalone
  long-running process:  `python3 -m agent_sync.linear_relay run` (and `check` for a
  no-writes credential/stream/auth probe).  It holds its own Zulip event queue;  the
  listener daemon does not feed it and it is not part of the daemon today.
- **Zulip identity:** a dedicated member (not admin) bot, bound by the env names
  `ZULIP_RELAY_EMAIL` / `ZULIP_RELAY_API_KEY` (site from `ZULIP_SITE`).  The local
  credential file for that bot is `~/.secrets/zulip-linear-bot-zuliprc`.  The bot
  auto-subscribes to every public stream;  private streams are never joined, and the
  worker drops anything whose stream is not in the public set it loaded.
- **Linear identity:** a dedicated Linear app created for the relay (owner decision
  2026-10-23:  comments are NOT posted as the Wise app), bound as `LINEAR_RELAY_TOKEN`
  (an OAuth app token), loaded by the server container from Infisical.  Scopes are
  `read` + `comments:create`;  whether `commentUpdate` succeeds under those scopes is
  recorded in the module as UNVERIFIED — the sandbox proof was still owed when the
  fold-in was directed.  The retirement sequence below keeps that proof as a gate.
- **Comment format:** first line `<!-- zulip-relay -->` plus a per-conversation key (the
  issue and the id of the first message recorded for the topic, so a topic rename keeps
  editing the same comment);  then `**Zulip discussion:** #<stream> > <topic>`;  then a
  bullet per recorded message — timestamp link (a `near/<message id>` permalink built
  from the id alone, never from topic text), sender, and a neutralised quote (280 chars,
  credential-shaped fields withheld whole, markdown defanged);  at most 20 bullets, with
  an "_N earlier message(s) not listed._" line when the thread is longer;  footer
  "_Mirrored one way from Zulip by the relay.  Edit in Zulip, not here._"
- **Dedup state:** a JSON ledger, `~/.agent-sync/linear-relay.json` on the host that runs
  it (override `LINEAR_RELAY_STATE`), keyed `ISSUE|stream_id|sha256(topic)[:32]`, holding
  the comment id, the recorded links, seen message ids, counts, edit timing and retry
  state.  Atomic owner-only writes under an flock;  a create is written ahead as pending
  and reconciled by marker scan before any re-create, so the design is at-least-once-safe
  against crashes but — as the module itself states — NOT exactly-once:  a lost ledger,
  a second relay process, or a create that landed while its answer was lost can each
  produce a second comment, narrowed by the marker reconciliation.

## The fold-in (this lane)

- `scripts/agent_sync/operator_linear.py` (new):  `OperatorLinearComments` hosts the
  relay's own `RelayWorker` inside the listener daemon.  The daemon feeds it the stream
  messages it already routes (`route_message`) and the `update_message` events it
  already handles (`handle_events`), and ticks it every 30 seconds;  observation is
  best-effort and can never break routing, and the worker deduplicates by message id, so
  a message routed for several seats counts once.  The public-stream set is loaded with
  a routing seat's existing daemon client (a read);  the only writes anywhere are the
  Linear ones, under the Operator's token alone.  Nothing posts to Zulip.
- **Whole-topic scope, two halves.**  (a) At creation:  the worker takes a topic-history
  reader (the daemon's messages API, paged forward from the topic's start, capped at
  `MAX_BACKFILL` = 1000 messages);  when a first mention creates an entry, the topic's
  earlier messages are recorded into it before the create renders, each with its own
  timestamp, the writer's own posts skipped as on the live path.  A history read that
  fails is logged and never blocks the create (the comment then starts at the first
  mention — visible in the log, never silent).  (b) After creation:  every later message
  in a tracked topic is recorded on its standing entries, key or not, so each edit
  reflects the whole current thread.  Both halves are in `linear_relay.py`, additive and
  covered by tests;  the standalone process passes no history reader and its ingest is
  otherwise unchanged.
- **State:** the hosted ledger is the same format at
  `<state root>/operator/linear-comments.json` (server:  `/data/operator/linear-comments.json`),
  beside the session registry.  The marker line is deliberately unchanged
  (`<!-- zulip-relay -->`), so the hosted worker ADOPTS the comments the standalone
  relay already created instead of stacking new ones;  the footer byline becomes
  "the Operator".  A refused Linear token stops the engine (logged) instead of stopping
  a seat thread;  the daemon keeps routing.
- **Switches:** `[linear_comments] enabled` in listener.toml (parsed in config.py;
  a malformed section never turns it on;  the server instance only, so two instances
  can never stack comments), the sample section ships in
  `scripts/agent_sync/server/listener.toml`, and `apply_live_config.py` adds the section
  to a live volume that lacks it.  The server image copies the whole `agent_sync`
  package, so no Dockerfile change is needed.
- **Update policy** (inherited, unchanged):  create immediately on first mention;  edits
  batched to at most one per 10 minutes per comment;  one cycle's writes share a 120 s
  budget so a slow Linear never stalls the listener;  transient failures back off
  exponentially (30 s doubling, capped at 600 s) and retry on later cycles;  a missing
  issue is re-looked-up after an hour, a permanently refused write is not retried until
  the topic gives the entry new content;  an uncertain create is reconciled by marker
  before any re-create, at startup and before the next write.

## The credential seam (the one thing that must be created)

The Operator has no Linear credential of its own for commenting.  Before this can be
enabled, an owner/admin must:

1. Create a dedicated Linear app (or a dedicated Operator Linear user with an API key)
   for the Operator's Zulip -> Linear comments, with scopes `read` + `comments:create`.
   Do NOT reuse the relay app's token, a seat's Linear OAuth app, or the
   `/operator-linkifiers` `LINEAR_API_KEY` (that one reads projects for the webhook).
2. Store its token in Infisical, project AI Fleet Coordinator, prod, folder
   `/operator-linear-comments`, name `OPERATOR_LINEAR_TOKEN` (registered in
   INFISICAL.md), and bind it to the server container as the runtime-only variable
   `OPERATOR_LINEAR_TOKEN` — the same delivery route `LINEAR_RELAY_TOKEN` used.
3. Prove the edit scope in the Linear sandbox (the relay's open question):  create a
   comment with the new credential, then edit it, before any live enablement.

`OPERATOR_ZULIP_EMAIL` (the Operator bot's own address, already bound on the container
for the Operator voice) is read when present so the engine skips the Operator's own
posts, as the relay skipped its own.

## Tests

`scripts/agent_sync/tests/test_operator_linear.py` (new, 19 tests):  mention first
appearing in the 3rd message produces a comment covering messages 1–3;  one comment per
(topic, issue) across repeats, two issues in one topic, and one issue in two topics;
edits in place reflecting the whole thread;  restart recovery from the ledger with no
second create;  no keys → no comment and no entry;  key-shaped false positives
(UTF-8, COVID-19, A320-200, HELLO-123, lowercase keys) ignored;  backfill skips the
writer's own posts, orders links oldest first, shares one fetch across the issues one
message names, and never blocks a create when the history read fails;  a topic-title
mention backfills from the first routed message;  tracked topics take every later
message, untracked topics take none;  history paging and narrow shape;  the engine
end-to-end and across a restart;  silence until the public-stream set loads;  private
streams never commented;  dry run writes nothing;  the ledger path;  and the
`[linear_comments]` config parsing.  The relay's own suite
(`test_linear_relay.py`, 78 tests) stays green unchanged in behaviour except the
whole-topic continuation above.

## Retirement sequence for the old relay (documented, NOT executed)

Run only after the fold-in is merged and deployed, in this order — the order is the
safety property:  at no point are both writers live on the same topic, and at no point
is neither live.

1. **Credential + sandbox proof.**  Create the Operator's Linear credential (seam
   above);  in the sandbox, create and then EDIT a comment with it.  If the edit is
   refused under `read` + `comments:create`, widen the app's scopes first — do not
   proceed on a create-only proof.
2. **Deploy dark.**  Ship the fold-in with `[linear_comments] enabled = false` (the
   sample's default) in the server image;  restart the app;  confirm the daemon is
   healthy and logs no `linear-comments` errors.  The standalone relay keeps running.
3. **Shadow check.**  Bind `OPERATOR_LINEAR_TOKEN`, set `OPERATOR_LINEAR_DRY_RUN=1`,
   enable `[linear_comments]`, reload.  For one active topic that names an issue,
   confirm from the log that the engine would create ONE comment and that its rendered
   coverage is the whole topic (the dry-run ledger is a sibling file;  the live ledger
   and the running relay are untouched).  Turn dry run back off.
4. **Stop the relay first.**  Stop the standalone relay process on the server host and
   disable its unit/entry so it cannot restart.  Note the exact ledger path it leaves
   (`~/.agent-sync/linear-relay.json` or its `LINEAR_RELAY_STATE`).
5. **Adopt, don't migrate.**  Do NOT copy the relay's ledger into the hosted one:  the
   hosted worker adopts each standing comment by its marker line the first time its
   topic is mentioned again, and topics that never speak again need no adoption.  (The
   hosted comment for a reawakened topic starts its recorded list fresh;  the adopted
   comment keeps its earlier bullets only if they were in the rendered body the worker
   re-renders from its own ledger — they are not, so a reawakened topic's comment is
   re-rendered from the hosted ledger's records plus the backfilled whole-topic history,
   which is exactly the whole-topic scope.  No comment is ever duplicated by adoption:
   `find_comment` runs before every create.)
6. **Enable live.**  Remove the dry-run variable, keep `[linear_comments]` enabled,
   reload, and verify on one live thread:  one comment, whole-topic coverage, an edit
   in place after further posts, and the footer naming the Operator.
7. **Remove the wiring.**  Only after step 6 verifies:  delete the relay's Zulip bot
   credential env bindings (`ZULIP_RELAY_EMAIL`, `ZULIP_RELAY_API_KEY`) and
   `LINEAR_RELAY_TOKEN` from the container/Infisical, retire the relay Zulip bot and
   the relay Linear app in their consoles, archive its ledger file with the closeout
   note, and remove the standalone `run`/`check` entry point from `linear_relay.py` in
   a follow-up PR (the worker, ledger and client stay:  the Operator hosts them).
   Record the DONE closeout in the claim topic.

## Rebase note

PR #1258 renames `scripts/agent_sync` -> `scripts/simple_tools` (and the standalone
relay's replacement is part of that stack's world).  Every path in this note moves with
that rename;  the fold-in's diff is additive to `linear_relay.py` (history reader,
byline, whole-topic continuation), one new module, one new test file, and small daemon
and config hooks, so it should rebase mechanically onto the renamed tree.
