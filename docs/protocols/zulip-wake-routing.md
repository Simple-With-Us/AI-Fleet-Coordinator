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
           1. a letter the sender addressed ("@**ZCode** AA"), or the body's `letter`
              field (a wake that is an untagged reply to a chat of the seat carries one,
              with `linked_by: "reply"`), resolves through the Operator's registry
              (live holders only;  cross-host by design)
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
