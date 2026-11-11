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
    wakes when no session follows it), or answers
    one of the seat's chats without tagging anyone (owner, Nov 10, 2026: an untagged
    post in a topic where the seat posted earlier wakes the chat it answers;  see
    simple-tools-listener.md, "Replies wake the chat they answer")
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
           2. else a fragment of a session id — at least 8 hex characters (a longer
              unbroken hex run is read as a commit or key id, not a fragment), compared
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
         included ("why would I want to route things and not deliver them").  Each platform
         uses whatever it needs to wake its chat (for ZCode, a headless resume of the picked
         session);  the delivery prompt carries metadata and the thread link, never the
         message body quoted, and names how the wake was linked (the `link_note`), saying
         for recency and for a decider pick that the router is not sure.
      -> the pick and the delivery are receipted (wakes.jsonl) with the fired rule's link
         note and the picked generation;  a wake addressed to a letter whose holder has
         changed ends in a dead receipt (`target-released`) and is not delivered.  The
         router retries delivery itself;  the listener never retries a 2xx.  The message
         also sits in the seat inbox and the Operator's pending-inbox, so a live session
         sees it either way.

## The Router Contract (What Every Platform's Router Implements)

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
  `{wake_id, letter, session_id, thread_link, picked, picked_generation, reason, link_note}`,
  where `picked` is one of letter | reply | session-id | linear-issue | topic-match |
  consultant | most-recent | "" and `reason` distinguishes the tie and decider cases
  (linear-issue-tie, topic-tie, consultant-pick, sole-live-chat, most-recent); `link_note`
  is the fixed sentence the receiving chat is told about the link.
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

The reference implementation is `scripts/simple_tools/wake_router.py` and its tests;  each
platform adds a thin native `deliver()` plugin to it rather than a router of its own
(ZCode's and Antigravity's bridges are such plugins).
## The Decider's Declaration

The decider's OpenRouter model is declared in the one server listener's settings, in `[decider.consult_backup]`.  It is the only record of the owner's preset for the decider, so it is kept even while Consult sits unarmed and no deployment arms it:

```toml
[decider.consult_backup]
runtime = "openrouter"                       # the only runtime recognised
model = "@preset/operator-inbox-router"      # an @preset/ reference or a plain provider/name
expected_models = ["meta/muse-spark-1.3-contributor", "minimax/minimax-m3.1-flash"]
reason = "the owner's own words for why"     # kept verbatim
declared_at = 1762575240                     # epoch seconds
# review_by = 1765000000                     # optional;  absent means it never expires
```

The values above show the shape;  the live server listener's settings hold the owner's real reason and time.  The loader (`config.py`:  `parse_decider_declaration`, `select_decider_runtime`, `Config.decider_declaration`) applies the validation the deleted Muse inbox bridge used for the same table (AFC-649):  `runtime`, `model`, `reason` and `declared_at` are required;  an `@preset/` model also needs a non-empty `expected_models` of plain `provider/name` ids, because a preset's model list lives on the dashboard and this list is the only code-side fingerprint of it;  a `review_by` that is given must be a finite time after `declared_at`, and once it passes the decider holds.  No declaration selects the primary.  A declaration that is there but malformed is a warning in `simple-chat daemon check-config`, never an error, and the decider holds on it rather than calling anything.  An unknown key in `[decider]` is an error.

## Single-Threaded Platforms (The Grok Bots GB-*, MA, JET, WISE, ECHO)

No router and no letters:  the seat is woken through its `wake = "http"` routine directly,
not by capture and surfacing alone;  there is no pick to make.
## What This Replaces

The per-seat wake types (inbox, claude, muse, openrouter as listener options), their
fallbacks, and any per-platform delivery executor.  The listener-side removal and the
MiniMax seat's migration are the listener refactor this document contracts.
