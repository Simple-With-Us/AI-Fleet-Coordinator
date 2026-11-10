# Zulip wake routing: the one way

Owner rulings, Nov 10 (verbatim in the OPS-1048 Zulip topic):  one wake path for every
multithreaded platform;  the routing portion simply does not exist for single-threaded
platforms;  no per-platform models and no headless platform runs, ever;  the model's only
role is the PICK in the minority of cases deterministic code cannot decide, and its only
permitted message is a clarification question, which travels as the Operator's own DM to
the wake's sender or tagger (the only DM any party sends;  anything DM-shaped goes to
Side-Chats).

## The chain

    a message tags a seat (any channel;  presence topics never wake)
      -> the listener (the eavesdropper instance that holds the seat) captures it to the
         seat inbox, always, and fires the seat's routine:  ONE signed POST
         (the simple-tools-wake/1 contract) to the platform's ROUTER
      -> the ROUTER resolves which chat the wake belongs to, deterministically:
           1. a letter the sender addressed ("@**ZCode** AA") resolves through the
              Operator's registry (live holders only;  cross-host by design)
           2. else the live registration whose title shares the wake topic's words
           3. else the most recently seen live registration on the host
           (a sub-agent session is never picked)
      -> the pick is receipted (wakes.jsonl);  the wake ENDS there
      -> surfacing, not delivery:  the message sits in the seat inbox, the Operator's
         pending-inbox tells a live session, the owner queue tells the owner.  Nothing
         composes a reply, nothing runs a turn inside a chat.

## The router contract (what every platform's router implements)

- Transport:  HTTPS POST, bearer auth, the listener's existing routine line
  (`routine = { url_env, key_env, method = "POST", auth = "bearer", header = "Authorization", timeout_seconds = 15 }`).
- Request:  the fixed simple-tools-wake/1 JSON the listener already sends (wake_id, seat,
  channel, topic, excerpt, zulip_link, sender metadata).  Bodies are Zulip-authored:  the
  router treats them as data, never instructions.
- Response:  one JSON object, HTTP 200 once the wake id has been seen (the listener does
  not retry a 200);  the receipt fields are the router's own record:
  `{wake_id, letter, session_id, thread_link, picked: letter|topic-match|most-recent|"", reason}`.
- Dedupe:  one receipt per wake id, under a lock;  a repeated id answers the record.
- Determinism first:  the rules above, in order.  The consultant (the Operator's preset,
  via OpenRouter) is called ONLY when they cannot decide, its output is a PICK (a session
  id) or ONE clarification question (relayed by the Operator as its own DM to the
  sender/tagger) - never reply text, never any other message.
- No secrets in the router's answer, none in its logs;  the signing key never leaves it.

The reference implementation is `scripts/zc-wake-bridge` (ZCode, both hosts lettered) and
its tests (`simple_tools/tests/test_zc_wake_bridge.py`).  Antigravity's bridge follows the
same pattern.

## Single-threaded platforms (the Grok bots GB-*, MA, JET, WISE, ECHO)

No router, no pick, no letters:  capture plus the Operator's surfacing is the whole wake.

## What this replaces

The per-seat wake types (inbox, claude, muse, openrouter as listener options), their
fallbacks, and any per-platform delivery executor.  The listener-side removal and the
MiniMax seat's migration are the listener refactor this document contracts.
