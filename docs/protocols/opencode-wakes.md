# OpenCode wakes:  the Operator reaching a specific OpenCode chat

AFC-865.  This document is what the OpenCode seat has for the Operator, and what it still
needs.  It is written from the server, where the OpenCode seat runs, and it names what was
verified and when.

## The letter half

An OpenCode chat takes an Operator session letter and is titled `XX: Words` (AFC-564).  The
letter comes from the plugin inside OpenCode (`scripts/simple_tools/opencode_letter_plugin/`)
and the Operator's one registry; nothing about it is OpenCode's own.

The plugin hears a chat's own events.  That is enough for a chat that is in use, and it is not
enough for a chat that went quiet before the plugin was loaded:  no event comes for it, so it
keeps the name OpenCode gave it and takes no letter.  That gap is real and it was found on
Nov 21, 2026, when a chat from that morning ("Mac iOS apps UI UX improvement") and four
sub-threads of a lettered chat had no letter while their parent had `AA`.

The fix is the fleet's own repair pass, not a second mechanism.  The adapter
(`scripts/simple_tools/platform_letter_opencode.py`) now names this host's unlettered chats
from the runtime's own store (`~/.local/share/opencode/opencode.db`, the `session_v2` table),
and the library's `repair_after_start` sets off the shared `platform-letter repair` after a
chat's start, at most once an hour, at most five chats a pass.  The repair replays each
chat's own start, which registers it with the Operator and puts the letter on the chat through
the runtime's own API (`opencode api session.update`).

What the repair did on Nov 21, 2026, on the server, with the real registry and the real
runtime:

| Chat | Before | After |
|---|---|---|
| `Mac iOS apps UI UX improvement` | no letter | `CC: Mac iOS apps UI UX improvement` |
| `Competitive benchmark` (child of `AA`) | no letter | `AB: Competitive benchmark` |
| `macOS UX audit` (child of `AA`) | no letter | `AC: macOS UX audit` |
| `Design system audit` (child of `AA`) | no letter | `AD: Design system audit` |
| `iOS UX audit` (child of `AA`) | no letter | `AE: iOS UX audit` |

The children took the pairs right after their parent's (`AA` -> `AB`, `AC`, `AD`, `AE`), which
is the rule for a sub-thread, and the older main chat took the next free pair for the seat
(`CC`).  The probe chat marked `[no-letter]` was left alone, as it takes none by design.

A chat marked `[no-letter]` takes no letter:  it is how a headless run says so, and the sweep's
workers carry the mark (`scripts/pr_sweep/platforms.json`).  A chat the plugin never saw does
not carry the mark, so the repair letters it.

**How far back the repair reaches.**  A chat older than fourteen days is left alone, and that
is a bound and not an oversight.  The fleet has twenty-six main pairs and the whole seat's
history on this host is larger than that:  thirty-eight chats on Nov 21 were older than the
bound, and twenty-six of them would have been main chats.  Lettering them all would have
taken every main pair for chats from weeks ago and left the live ones with none, which is the
wrong-chat delivery the registry exists to refuse.  The bound keeps the repair on the chats
the owner is actually looking at, and the oldest of the thirty-eight was seventeen days old.

## The wake half

The seat can be woken through the one wake router, with `scripts/simple_tools/opencode_wake.py`
as its platform module and one `BUILDERS` row in `scripts/simple_tools/wake_service.py`.  The
delivery is `opencode run --session <id>` with the wake text on the child's stdin:  the CLI
continues that one chat and no other, so the chat keeps its own title, its own letter and its
own model, and the message lands in the session's own row of the runtime's store.

The binding is the session's own store row.  A chat another chat started refuses
(`session-subagent`), an archived chat refuses (`session-archived`), a chat with a turn
running is busy and is retried, and a chat whose recorded directory is gone refuses
(`session-cwd-gone`).  The deployment may name a `workspace` root to narrow the delivery; it
is empty by default, because every OpenCode chat has its own project directory and a wrong
default is the wrong-chat delivery this platform exists to refuse.

The child runs with `SESSION_LETTER=0`, so a private server the CLI starts for itself takes no
letter of its own and puts none on the chat it is woken into.  The shared service is the one
that holds the chat's letter, and a wake adds a message to the chat and changes nothing about
its name.

Verified on the server on Nov 21, 2026:  `opencode run --session <id>` continued a scratch
session, the prompt landed in that session's own row (`session_message`), the title did not
change, and the CLI talked to the shared service the runtime's own
`~/.local/state/opencode/service.json` names.

## What the cutover still needs

The module lands with this document; the delivery is not armed yet.  Arming is the owner's
step, and it is the same shape the ZC cutover took (`docs/protocols/zcode-wakes.md`):

1. The release on the server carries `opencode_wake.py` (the fleet-cli release follows
   `origin/main`), and `/health/OPENCODE` answers healthy with delivery on.
2. `WAKE_ROUTER_DELIVERY_ENABLED` is true and `WAKE_ROUTER_DELIVERY_SEATS` names OPENCODE, both
   in the router's own Infisical folder `/wake-router`.
3. The listener's sample (`scripts/simple_tools/server/listener.toml`) moves the seat from
   `wake = "inbox"` to the router's binding, and `apply_live_config.py --apply` writes it into
   the live file.  The apply reads the sample from the image's own copy, so the image comes
   first, and the container is restarted with it.
4. One real wake is verified end to end:  the router's receipt shows the picked session and a
   delivered outcome, and the woken chat says the wake id in its own prompt.

Until then the seat's wakes stay in its inbox, which is where they were before this work, and
nothing that worked is taken away.
