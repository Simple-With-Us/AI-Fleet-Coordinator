# Zulip wake routing: the one way

Owner rulings, Nov 10, as corrected the same day (verbatim in the OPS-1048 Zulip topic,
including the corrections relayed by Claude AA):  one wake path for every multithreaded
platform;  the routing portion simply does not exist for single-threaded platforms;  ONE
headless decider for all platforms (OpenRouter on the Operator's preset) used only when
the deterministic rules cannot decide, whose only permitted message is a clarification
question traveling as the Operator's own DM to the wake's sender or tagger (the only DM
any party sends;  anything DM-shaped goes to Side-Chats);  and every path then DELIVERS
AND WAKES the picked chat, each platform using whatever it needs to do that.

## The chain

    a message tags a seat (any channel;  presence topics never wake), or answers
    one of the seat's chats without tagging anyone (owner, Nov 10, 2026: an untagged
    post in a topic where the seat posted earlier wakes the chat it answers;  see
    simple-tools-listener.md, "Replies wake the chat they answer")
      -> the listener (the eavesdropper instance that holds the seat) captures it to the
         seat inbox, always, and fires the seat's routine:  ONE signed POST
         (the simple-tools-wake/1 contract) to the platform's ROUTER
      -> the ROUTER resolves which chat the wake belongs to, deterministically:
           1. a letter the sender addressed ("@**ZCode** AA", or the post header's
              recipient "→ ZC AA";  every form is in "Addressing a chat" below), or the
              body's `letter` field (a wake that is an untagged reply to a chat of the
              seat carries one, with `linked_by: "reply"`), resolves through the
              Operator's registry (live holders only;  cross-host by design)
           2. else the live registration whose title shares the wake topic's words
           3. else the most recently seen live registration on the host
           (a sub-agent session is never picked)
      -> the picked chat is DELIVERED TO AND WOKEN - on every path, the deterministic one
         included ("why would I want to route things and not deliver them").  Each platform
         uses whatever it needs to wake its chat (for ZCode, a headless resume of the picked
         session);  the delivery prompt carries metadata and the thread link, never the
         message body quoted.
      -> the pick and the delivery are receipted (wakes.jsonl);  a failed delivery retries
         through the listener.  The message also sits in the seat inbox and the Operator's
         pending-inbox, so a live session sees it either way.

## Addressing a chat

A chat is written as its platform's name and its letter, and a letter is exactly two capitals:  "Claude AA" for a main chat, "Claude DE" for a child thread.  The one router reads which chat a message addresses in one function for every platform (`letter_of` in `scripts/simple_tools/wake_router.py`), with no rule of its own for any seat.  The body's `letter` field wins.  Otherwise the first of these that names a letter for the seat gives it, in this order:

1. **A real mention and the letter**, anywhere:  `@**Claude** AA`, the letter on the mention's line or the next.  The name may be said again after the mention (`@**Claude** Claude AA`).  A real mention is the platform's mention name as Zulip writes it, so `@**CLAUDE** AA` is not one.
2. **The recipient list of the post's header**, the text after its arrow (`→`, or `->` as some type it) up to the closing bracket:  `[GROK: BB.2.M 📁AFC → CLAUDE AA, CODEX BB]` is AA for Claude and BB for Codex.  This is the canonical way to address a chat, because every fleet post begins with that header.  An item counts when it is exactly the name and the letter:  `CLAUDE AA`, `Claude AA` or `@Claude AA`, the mention name or the seat tag in any case.  The name may also be a mention (`@**Claude** AA`), which the fleet's posting tools make silent in a bot's post (`@_**Claude** AA`), and the letter may carry its session token (`CODEX BB.1.M`).  The first such item for the seat wins.  An item that is only `CLAUDE` names the seat and no chat.  Only the header that opens the message is read this way.
3. **The plain at-form**, anywhere:  `@Claude AA`, with the mention name or the seat tag after the `@` in any case (`@claude AA`), said again or not (`@Claude Claude AA`).  It does not count as the tail of a word, an address or a path (`jay@Claude AA`).
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
- Determinism first:  the rules above, in order.  ONE headless decider for every platform
  (the Operator's preset, via OpenRouter - the owner's words:  not "a bunch of different
  headless models on a per platform basis"), called ONLY when the deterministic rules
  cannot decide.  It DECIDES - its output is a pick (a session id) or ONE clarification
  question (relayed by the Operator as its own DM to the sender/tagger) - it never writes
  reply text.  It never answers;  the woken chat answers.
- No secrets in the router's answer, none in its logs;  the signing key never leaves it.

The reference implementation is `scripts/zc-wake-bridge` (ZCode, both hosts lettered) and
its tests (`simple_tools/tests/test_zc_wake_bridge.py`).  Antigravity's bridge follows the
same pattern.

## The Decider's Declaration

The decider's OpenRouter model is declared in the listener's `listener.toml`, in `[decider.consult_backup]`.  It is the only record of the owner's preset for the decider, so it is kept even while no code calls it:

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

**The old location.**  Until Nov 10, 2026 the same table lived at `[muse_bridge] consult_backup`, inside the Muse inbox bridge's settings (AFC-326), which were deleted on the owner's instruction that day.  A file that still holds `[muse_bridge]` keeps loading:  the table is ignored with one warning that says it can be deleted, and its `consult_backup` is read as `[decider.consult_backup]` while the new table is absent (when both are there, the new one wins).  Move the table to `[decider.consult_backup]`, then delete `[muse_bridge]`.  Muster's Decider line reads the same two places in the same order.

## Single-threaded platforms (the Grok bots GB-*, MA, JET, WISE, ECHO)

No router, no pick, no letters:  capture plus the Operator's surfacing is the whole wake.

## What this replaces

The per-seat wake types (inbox, claude, muse, openrouter as listener options), their
fallbacks, and any per-platform delivery executor.  The listener-side removal and the
MiniMax seat's migration are the listener refactor this document contracts.
