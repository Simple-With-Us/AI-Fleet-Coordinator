# Zulip Wake Routing:  The One Way

Owner rulings, Nov 10, as corrected through the same evening (verbatim in the OPS-1048 and
AFC-594 Zulip topics):  one wake path for every multithreaded platform;  the routing portion
simply does not exist for single-threaded platforms;  delivery and wake on every path, each
platform using whatever it needs to do that;  the Operator never DMs anyone, never posts in
a thread, and never asks a clarifying question (owner, Nov 10: "forget the operator asking
clarification since overcomplicates for minimal benefit" — there is no clarification step
and no destination for one, Side-Chats included);  and when the deterministic rules cannot
decide, the wake still goes to the most recent live chat, whose delivered prompt says how
the wake was linked and, for recency, that the router is not sure.  Nobody is ever asked to
clarify and an ambiguous wake is never held.
## The Chain

    a message tags a seat (any channel;  a direct mention in a presence topic still
    wakes when no session follows it), or answers one of the seat's chats without
    tagging anyone — but an untagged reply in a presence topic never wakes, even
    when the seat posted in that topic earlier (owner, Nov 10, 2026:  an untagged
    post in a non-presence topic where the seat posted earlier wakes the chat it
    answers;  see simple-tools-listener.md, "Replies wake the chat they answer",
    and the reply path, which skips presence topics)
      -> the listener (the eavesdropper instance that holds the seat) captures it to the
         seat inbox, always, and fires the seat's routine:  ONE signed POST
         (the simple-tools-wake/1 contract) to the platform's ROUTER
      -> the ROUTER resolves which chat the wake belongs to, deterministically, in the
         order below, and every fired rule is named in the receipt and in the delivered
         prompt's link note (the one shared router is scripts/simple_tools/wake_router.py,
         merged from #1743 on Nov 11):
           1. a letter the sender addressed ("@**ZCode** AA"), or the body's `letter`
              field (a wake that is an untagged reply to a chat of the seat carries one,
              with `linked_by: "reply"`), resolves through the Operator's registry
              (live holders only;  cross-host by design)
           2. else a fragment of a session id — at least 8 hex characters, while a
              plain unbroken hex run longer than 12 characters is read as a commit
              or key id, not a fragment (9-12 plain hex characters stay eligible),
              compared
              against the id's hex alone, matching exactly one live chat (transitional,
              while some seats still send a fragment;  the sender's own header fragment
              is excluded and never searched — only addressees after the arrow in a
              header are searched as addresses, and quoted or nested untrusted text
              addresses nobody)
           3. else a Linear issue id from the wake's topic or excerpt, matched against a
              chat's registry `issues` (newest `seen_at` first;  a record with no
              `issues` falls back to its title)
           4. else the topic's words in a chat's title
           5. else recency:  the registry's history ranks live registrations across hosts
           (a sub-agent session is never picked;  a tie within a step goes to the most
           recent of the tied chats;  the optional decider below may choose before
           recency is applied)
      -> the picked chat is DELIVERED TO AND WOKEN - on every path, the deterministic one
         included ("why would I want to route things and not deliver them").  Each platform
         uses whatever it needs to wake its chat (for ZCode, a headless resume of the picked
         session);  the delivery prompt carries metadata and the thread link, never the
         message body quoted, and names how the wake was linked (the `link_note`), saying
         for recency and for a decider pick that the router is not sure.
      -> the pick and the delivery are receipted (wakes.jsonl) with the fired rule's link
         note and the picked generation;  a wake addressed to a letter whose holder has
         changed ends in a dead receipt (`target-released`) and is not delivered.  The
         router retries delivery itself;  the listener never retries a 2xx.  The message
         also sits in the seat inbox and the Operator's pending-inbox, so a live session
         sees it either way.

## The Router Contract (What Every Platform's Router Implements)
## Addressing a chat

A chat is written as its platform's name and its letter, and a letter is exactly two capitals:  "Claude AA" for a main chat, "Claude DE" for a child thread.  The one router reads which chat a message addresses in one function for every platform (`letter_of` in `scripts/simple_tools/wake_router.py`), with no rule of its own for any seat.  The body's `letter` field wins.  Otherwise the first of these that names a letter for the seat gives it, in this order:

1. **A real mention and the letter**, anywhere:  `@**Claude** AA`, the letter on the mention's line or the next.  The name may be said again after the mention (`@**Claude** Claude AA`).  A real mention is the platform's mention name as Zulip writes it, so `@**CLAUDE** AA` is not one.
2. **The recipient list of the post's header**, the text after its arrow (`→`, or `->` as some type it) up to the closing bracket:  `[GROK: BB.2.M 📁AFC → CLAUDE AA, CODEX BB]` is AA for Claude and BB for Codex.  This is the canonical way to address a chat, because every fleet post begins with that header.  An item counts when it is exactly the name and the letter:  `CLAUDE AA`, `Claude AA` or `@Claude AA`, the mention name or the seat tag in any case.  The name may also be a mention (`@**Claude** AA`), which the fleet's posting tools make silent in a bot's post (`@_**Claude** AA`), and the letter may carry its session token (`CODEX BB.1.M`).  The first such item for the seat wins.  An item that is only `CLAUDE` names the seat and no chat.  Only the header that opens the message is read this way.
3. **The plain at-form**, anywhere:  `@Claude AA`, with the mention name or the seat tag after the `@` in any case (`@claude AA`), said again or not (`@Claude Claude AA`).  It does not count as the tail of a word, an address or a path (`jay@Claude AA`).  A seat tag of two capitals (`ZC`, `MC`) is left out of this form and of the next, because it is itself letter-shaped ("ZC PR is merged" names no chat);  the mention name and the header's recipient list still address.
4. **The plain form at the start of a line**, with the mention name or the seat tag as written:  `Claude AA, can you check`, or `CLAUDE AA please`.  The body's first words count as the start of a line, after the header and the mentions that follow it:  `[ECHO 📁AFC → CLAUDE] @**Claude** Claude AA, can you check`.
5. **The plain form later in a line, written as an address**:  the mention name and the letter with a comma or a colon straight after ("thanks, Claude CC, that works"), and only when the text names no other chat by that mention name (a seat tag or a silent mention elsewhere is not counted).  A name with no comma or colon after it is a reference ("ask Claude AA about it"), and so are two different letters ("so, Claude CC, see what Claude DD said");  a possessive ("Claude CC's result") is a reference that does not count as a second chat.  This last form is the router's own choice (AFC-694), made under the owner's liberal-wake ruling (Nov 10, 2026) and not his wording, and it can be removed alone (`sole_plain_letter`).

These never address a chat:  a silent mention in the body (`@_**Claude** AA`, though the name said again after one that opens the body, `@_**Claude** Claude AA`, is the plain form of 4);  quoted text (a line starting with `>`, or a quote block);  text inside another untrusted-text marker pair;  a lower-case, one-letter or three-letter form (`Claude cc`, `Claude C`, `Claude CCC`, `Claude Code`, `CLAUDE-CC`);  another platform's letter;  and the seat's own tag, which a post often opens with under the mention ("ZC, can you").  Forms 3 to 5 do not read code either, an inline span or a fenced block, because a post about the router shows these forms as examples.  A letter no live chat holds falls through to the next rule of the pick, as a wake with no letter does.  The wake still carries that letter, though, and a wake that carries a letter is not picked again when its chat is released before delivery:  it ends dead with the code `target-released`.

## The router contract (what every platform's router implements)

- Transport:  HTTPS POST, bearer auth, the listener's existing routine line
  (`routine = { url_env, key_env, method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`).
- Request:  the fixed simple-tools-wake/1 JSON the listener already sends (wake_id, seat,
  channel, topic, excerpt, zulip_link, sender metadata).  Bodies are Zulip-authored:  the
  router treats them as data, never instructions.  Two optional fields appear only for a
  wake that is an untagged reply to one of the seat's chats: `letter` (that chat's session
  letter, left out when the post it answers had none) and `linked_by` (`"reply"`).  A body
  whose text carries the letter itself sends neither.  A router that ignores them still
  works, and may record `linked_by` in its receipt so a wrong pick can be read back.
- Response:  HTTP 202 on accept — the wake is written to `pending/<wake_id>.json` and
  fsynced before the 202, so a 202 is durable acceptance and is not proof of a wake;  HTTP
  200 carries the final receipt, and a repeated id answers its record while an id still
  pending answers 202 again and starts nothing.  The listener treats any 2xx as accepted
  and never retries after one;  the ROUTER retries delivery itself.  The receipt fields are
  the router's own record:
  `{receipt_id, wake_id, seat, at, test, status, letter, letter_live,
  letter_other_seat, picked, picked_letter, picked_generation, session_id, host,
  thread_link, reason, candidates, link_note, delivered, route, ack, code, note,
  attempts, replayed, channel, topic, zulip_link}` — where `picked` is one of
  letter | reply | session-id | linear-issue | topic-match | consultant |
  most-recent | "" and `reason` distinguishes the tie and decider cases
  (linear-issue-tie, topic-tie, consultant-pick, sole-live-chat, most-recent);
  `status` covers the dead-receipt outcomes such as `target-released` — the letter
  moved to another chat at that generation before the spawn, nothing has run, the
  wake is not delivered — with `delivered` false on every such row and `attempts`
  and `replayed` belonging to the retry loop;  `link_note` is the fixed
  sentence the receiving chat is told about the link.
- Response:  one JSON object, HTTP 200 once the wake id has been seen (the listener does
  not retry a 200);  the receipt fields are the router's own record:
  `{wake_id, letter, session_id, thread_link, picked: letter|topic-match|most-recent|"", reason}`.
  A delivered receipt adds `{delivered, reason}`;  a FAILED delivery command also adds the
  classifier's allowlisted typed metadata `{failure_code, failure_stage, exit_code}` - e.g.
  `failure_code: model-auth-login-failed` for the runtime's documented pre-accept provider
  login failure (`project_token_login_failed`).  The classifier parses a bounded tail of the
  child's output in memory for documented literal signatures;  raw process output is never
  persisted (it can quote private prompt/session content), an unmatched failure records
  `unknown`, and classification never activates a retry by itself.
- Dedupe:  one receipt per wake id, under a lock;  a repeated id answers the record.
- Determinism first:  the rules above, in order.  ONE headless decider for every platform
  (the Operator's preset, via OpenRouter - the owner's words:  not "a bunch of different
  headless models on a per platform basis"), optional and off by default, consulted only
  when the deterministic rules cannot decide and before recency is applied.  It DECIDES -
  its output is a pick (a session id) and nothing else.  Since the owner's Nov 10 ruling it
  never asks anything and the Operator never relays a question:  when neither the rules nor
  the decider can pick, recency does, and the delivered link note says the router is not
  sure.  It never answers;  the woken chat answers.
- No secrets in the router's answer, none in its logs;  the signing key never leaves it.

### Turns (Several Wakes In One Idle Turn)

A chat that was busy while it was tagged may have many wakes waiting when it goes idle.  The
router may then deliver several of them as one idle turn (AFC-831):  a head and up to seven
riders, all for the same exact chat.  Each wake still keeps its own wake id, receipt id, done
file, ledger row and final status, and nothing is dropped or merged, not even two wakes for one
message.

- Opt-in, off by default:  a platform takes part only by declaring `Platform.deliver_turn` (and,
  for its Mac handler, the module constant `TURNS`), and only while the knob
  `WAKE_ROUTER_TURN_MAX_WAKES` is 2 or more.  Its default is 1, which is off, and the router
  never carries more than 8, whatever it says.  Every other seat runs exactly as before.  Muse
  Code is the only platform that declares turns today.
- Exact addresses only:  a wake rides only when its pick is a letter, a reply or a session id,
  with the router's own note for it, for the same session, letter, generation and host as the
  head.  A pick the router is not sure of (`sole-live-chat`, `most-recent`, `consultant-pick`)
  and any it inferred (a Linear issue, a topic, a tie) is delivered alone, as before, and a turn
  never makes it certain.  The pick is the router's own, made on the addressing without the
  sender's own quoted tags, so a sender quoting itself never makes a wake ride.
- Order:  the run is contiguous.  The walk from the head stops at the first wake of that chat
  that cannot ride or is not picked yet, so one chat's wakes still run in the order they came;
  another chat's wake is passed over.
- A busy chat:  a head whose chat has answered it `busy` goes alone from then on (`busy_seen` in
  its pending file, so a restart keeps it).  Riders are not marked again for every try at a chat
  that is in a live turn, where a lost answer or a crash would close them;  the wakes behind that
  head ride the next turn.
- A registry outage:  a turn answers it exactly as a wake alone does.  The check before delivery
  runs before any rider is marked, and an outage there, or a turn whose answer is the outage
  (`registry-unavailable`), sends the head back with none of its attempts spent and no
  `busy_seen`, and every rider back with nothing used.  A host with no registry address or key
  spends one of the head's attempts, as it would alone, and the riders still go back untouched.
- Before any effect:  each rider's pending file is marked first (an attempt, the in-flight mark
  and `ride`), then the head's pending file takes the manifest (`turn`:  its id, the riders and
  every message id) with its in-flight mark, as the last write before `deliver_turn`.  The
  ledger gets no row before the effect.
- The echo:  a rider counts as carried only when the far side names it, in order, in a readable
  echo of the offer.  The Mac dispatcher hands a turn only to a module that declares `TURNS`,
  marked `turn_dispatch`, and only then puts an echo in its answer, so a missing echo always
  means only the head was carried.  A rider not carried goes back to pending with nothing used,
  and leads or rides a later turn.  An echo that cannot be read after the turn ran closes the
  riders `unknown` with `turn-unconfirmed`, and one that claims riders for a turn that ran
  nothing closes them `unknown` with `turn-answer-conflict`.
- Receipts:  each member's receipt is the usual one plus a `turn` object, `{id, role: "head",
  members, message_ids, carried}` for the head and `{id, role: "rider", head, index}` for a
  rider.  The head's `carried` lists the riders the turn carried:  `[]` when it carried none and
  they went back, and null only when the answer could not say and they were closed.  A rider is
  `delivered` only when it was carried, and its `delivered` is true only with the chat's own
  confirmation or a completed turn.  A wake outside a turn has no `turn` object.
- Crashes:  every member a committed turn may have carried is closed by the replay as `unknown`,
  with `delivered` null and the code `interrupted-during-delivery`, and none is ever run again;
  a wake outside a turn keeps `dead`.  A rider whose head holds no committed manifest of its
  turn, or whose settled head says the turn did not carry it, ran nothing:  the replay takes its
  marks back with nothing used, and it is delivered later.
- Time:  a platform gives a turn its one-wake limit (`WAKE_ROUTER_RESUME_TIMEOUT_SECONDS`) once
  per member, and the ssh route waits that long plus its margin, so each wake keeps the time it
  would have had alone.  A turn can therefore outlast `WAKE_ROUTER_SHUTDOWN_WAIT_SECONDS`:  a
  restart during one closes its members as above and never runs one twice.
- Caps:  at most 8 wakes per turn, and a prompt of at most 3,500 characters (one fixed core
  text, pointer-only:  ids and the router's own notes, never a topic, a sender or the message).
  Over the ssh route the whole request stays 1 KiB under the Mac's 16 KiB stdin limit, or the
  head goes alone.  The woken chat reads each message with `zulip_get_message`, at the default
  length first and in full only when it needs the rest, so a turn asks it to read at most 8
  messages, each at most twice.  Whatever does not fit waits, untouched, for the next turn.

The reference implementation is `scripts/simple_tools/wake_router.py` and its tests;  `scripts/simple_tools/claude_wake.py` is its current registered plugin.  The standalone `scripts/zc-wake-bridge` retired on Nov 21, 2026 (deleted after the router's delivered-and-executed wake proof);  `scripts/ag-wake-bridge` still runs its own picks during Antigravity's cutover, and its link and refusal semantics are not guaranteed to match the shared router's until it migrates.

The decider's OpenRouter model is declared in the live Mac listener's `listener.toml`, in `[decider.consult_backup]` — the one server listener carries no such table today.  It is the only record of the owner's preset for the decider, so it is kept even while Consult sits unarmed and no deployment arms it:

```toml
[decider.consult_backup]
runtime = "openrouter"                       # the only runtime recognised
model = "@preset/operator-inbox-router"      # an @preset/ reference or a plain provider/name
expected_models = ["meta/muse-spark-1.3-contributor", "minimax/minimax-m3.1-flash"]
reason = "the owner's own words for why"     # kept verbatim
declared_at = 1762575240                     # epoch seconds
# review_by = 1765000000                     # optional;  absent means it never expires
```

The values above show the shape;  the live Mac file holds the owner's real reason and time.  The loader (`config.py`:  `parse_decider_declaration`, `select_decider_runtime`, `Config.decider_declaration`) applies the validation the deleted Muse inbox bridge used for the same table (AFC-649):  `runtime`, `model`, `reason` and `declared_at` are required;  an `@preset/` model also needs a non-empty `expected_models` of plain `provider/name` ids, because a preset's model list lives on the dashboard and this list is the only code-side fingerprint of it;  a `review_by` that is given must be a finite time after `declared_at`, and once it passes the decider holds.  No declaration selects the primary.  A declaration that is there but malformed is a warning in `simple-chat daemon check-config`, never an error, and the decider holds on it rather than calling anything.  An unknown key in `[decider]` is an error.

## GROK Last Mile (Why A Wake Is Refused)

GROK is delivered by `simple_tools/grok_wake.py`:  the router picks a chat from the registry, then the Mac asks `grok-drive list` about that one chat and prompts it only when a Grok TUI has it open and its turn is idle.  The registry and the TUI mean different things by "live".  A registry row stays open until the stale sweeper ends it, and the sweeper ends only a chat that is stale (36 hours) and replaced by a newer chat of the seat, so the last Grok chat the owner used stays open for good.  `grok-drive` says `live` only while `~/.grok/active_sessions.json` lists the chat, which Grok maintains while a tab is open.  A wake to a chat that was quit is therefore refused before any prompt, with one of these codes in the receipt:

- `tui-not-attached`:  the chat exists and no Grok TUI has it open.  Reopen it with `grok --resume <session id>` or start a new Grok chat, which registers and is picked from then on.
- `chat-not-listed`:  nothing on the Mac knows the chat (the leader's list and the TUI registry both lack it).
- `drive-list-unavailable`, `drive-list-timeout`, `drive-list-unreadable`:  the list could not be trusted (retried).
- `turn-working`, `turn-needs-input`:  the chat is busy (retried).

Both refusals are final and `delivered: false`.  A quit chat is never resumed headless, because that would run tools with nobody watching.  The Zulip message stays in the seat's inbox.  Until the Mac and the server both run this change, a refusal can still read `not-live`.  Deploy the server before the Mac.  The server router builds its never-ran set from its own copy of grok_wake, so a Mac that answers with the new codes before the server is updated is recorded as delivered: null rather than false.  The order the other way round is covered, because the old code stays in the set.

## Single-Threaded Platforms (The Grok Bots GB-*, MA, JET, WISE, ECHO)

Two groups.  JET and the Grok bots GB-* are woken through their `wake = "http"` routines directly;  MA, GROK-WEB, WISE and ECHO are `wake = "inbox"` seats with no routine and no pick at all:
not by capture and surfacing alone;  there is no pick to make.
## What This Replaces

The model wake types (`claude`, `muse` and `openrouter` as listener options, retired Nov 10, 2026 and refused at config load), their fallbacks, and any per-platform delivery executor.  `wake = "inbox"` stays, as capture only.  The listener-side removal is done;  the MiniMax seat's migration completes the listener refactor this document contracts.
