# Fleet UI copy conventions (owner, 2026-08-07)

Binding for **every app, every bot, every platform** the owner can see —
**Socratic.Trade**, **Congress.Trade**, **Usage Monitor**, **DealDex**,
**Personal-Site**, **Autorotate**, **ContactLogo**, **BotFleet**, and any
future product.  Web, iOS, Android, chat replies, Bot settings, routine
settings, marketing HTML, App Store fields.



## Theme default = system (owner ruling 2026-09-19 — supersedes 2026-08-10 — ALL apps, ALL agents)

Owner: default UI theme is **system** — the app follows the OS appearance.
A Mac in light boots light; a Mac in dark boots dark; an iPhone in dark
boots dark.  This supersedes the 2026-08-10 ruling that pinned the default
to light and warned against system/dark-first defaults.  That ruling
existed because most fleet agents develop on a Mac that is dark, and a
system-default would land them on dark every time.  The owner has decided
that is no longer the trade-off they want — the OS preference is the
right default for first-time visitors, and it eliminates the dev-Mac
optical illusion in both directions.

- **Default for first visit / no stored preference:** **system** (follow the OS).
- **Stored preferences are never rewritten** by this rule.  A returning user
  who chose Light or Dark stays on it; only first paint with no record
  defaults to system.
- Light, Dark, and System remain available choices everywhere a picker
  ships; System is just the new default.
- **Screenshots / ASC / marketing / design previews:** capture on a
  **light** Mac unless the owner explicitly asks for dark.  The product
  default is system, the capture default is still light, because marketing
  assets need a deterministic palette regardless of who happens to be
  developing them.  Existing ASC packs that are already light do not need
  a redo for this rule alone.
- Applies to every fleet app (web + iOS + Android + BotFleet chrome +
  CodeCaps).  Each seat lands the default flip in their own app; the
  FLEET-UI-COPY change here is the canonical signal, not the per-app
  code change.
- The dev-Mac-dark-footgun still applies to any *picked* dark theme — a
  light capture is still the right answer for marketing.  The reversal is
  only about the *default*, not about choice.

## Proper nouns

- **Congress** and **Congressional** always take a capital **C** (U.S. proper nouns) in product copy, App Store text, and UI.
- Brand: **Congress.Trade**. Display URLs may use `https://Congress.Trade` (hostnames are case-insensitive; DNS/cert still resolve).
- Brand: **DealDex**. Do not reintroduce retired product names in user-facing copy.
- Keep technical identifiers lowercase: `trade.congress.ios`, `congress.trade` event names, email local-parts as configured.
## Headings / titles / buttons (strengthened 2026-09-03)

Owner, in-conversation 2026-09-03, **ALL bots ALL platforms ALL apps**:

Use **Title Case** (capitalize main words) for buttons, headings, titles,
menu items, tab labels, section names, dialog titles, Bot settings,
routine settings, and any chrome a human clicks or that names a region.

- Examples: `Agent Controls`, `Run Once`, `Win Rate`, `Needs Attention`,
  `Pending Proposals`, `Review 2 Proposals`, `Current Policy`,
  `Connected Accounts`, `Delete Account`, `Price Alerts`, `Last Run`,
  `Backend Remains Authoritative`, `Portfolio Brief`, `User Info`,
  `How It Works`, `Import From This Phone`, `Try Another`,
  `Working Folder`, `Tasks & Routines`.

## Values / answers / body (right side of labeled rows, subtitles that are data)

Use **sentence case** for body prose.  Use **sentence case or lowercase**
when the string is not a full sentence — not Title Case:

- Examples: `not reported`, `ask-first`, `intraday`, `not scheduled`,
  `every 60 min`, `open holdings`, `none waiting`,
  `account return minus SPY…` (lowercase leading **a**).

## Special cases
- **Latency vs other providers (CT, 2026-08-16):** never print `+` or `−` on
  lead/lag.  Positive seconds means we published first.  Say **earlier**
  (green) or **later** (red), plus the magnitude (`13.0h earlier`,
  `5.7h later`).  Later is always red.  Earlier is always green.
- `vs SPY` — leave as-is (exception to value casing).
- `Use` buttons — short; leave as `Use`.
- Prefer **not** saying “Live” for account reality. All connected accounts are real money.
  Paper only: `Alpaca (paper)` with lowercase **p**. No “Live” dots/pills next to account rows.
- Market session banner (when shown): stream glyph + **`Market Closed`** / **`Market Open`**
  (not bare `Closed` alone if redundant with a Markets card).

## Times and dates (owner 2026-08-09, amended 2026-08-12, strengthened 2026-08-22)
- **Product UI renders in the VIEWER's device timezone** — iOS, browser, desktop.  A user in
  another zone reading their own trade times in Central would be the bug, not the fix.
- **Two deliberate exceptions, both about accounting honesty, not localization:**
  - Console surfaces that pin a **market-day boundary** stay `America/Chicago`
    (`app/console/lib/format.ts`), because they must agree with the day boundary the P&L
    accounting itself uses (`startOfDayInTimeZone`, `src/lib/db-execution.ts`).  A "today"
    that means a different day than the numbers under it is wrong.
  - **Market-session times** are the market's, not the viewer's: say `9:30 AM ET`, labeled.
- Label a zone in product UI only when the time is not the viewer's clock
  (`9:30 AM ET` for a market bell).  Relative times (`3m ago`) need no label.
- Owner-facing agent writing (chat, boards, rollouts, Zulip, Notes, release notes,
  bot-to-bot) is the owner's clock: **12-hour, with am or pm**, on every agent, bot,
  and platform.  That clock is Central.  Do not type CDT, CST, or CT.  Write `3:15am`,
  not `3:15am CDT` and not `08:15Z`.  When the day matters, write
  `Sun, Oct 5, 2026 at 3:15am`.  Give only the Central time:  convert UTC and every
  other zone first, with no stamp beside it (owner 2026-10-18).  `00:00 UTC` is 7:00pm the previous
  calendar day during daylight saving, and 6:00pm the previous calendar day after
  the fall-back.  See `/Users/jay/apps/FLEET-PROTOCOL.md` § Timestamps: Central Time.

## Money
- Compact suffixes **lowercase**: `$99.8k`, `$1.2m`, `$3.4b`.
- Home / hero equity (and places with room): **full** currency `$99,812.34`, not compact.

## iOS navigation titles
Always `.navigationBarTitleDisplayMode(.inline)` (small, centered) on root tab screens —
**not** large left-aligned titles that collapse only after scroll.

## Congress.Trade — no asset-class dropdown (owner 2026-08-14)

Do **not** add an All Assets / Public Equities, Funds, & ETFs / Stocks and ETF
dropdown on web or iOS.  It is worthless, unused, and a waste of space.
Never reintroduce it.  The server `assetClass=` param may stay for API clients;
the public UI must not grow a control for it.

## Congress.Trade — store listing and corpus accuracy (owner 2026-08-14)

The product corpus is **House, Senate, and Executive Branch** (OGE 278-T), not
Congress alone.  App Store description, promotional text, keywords, review
notes, IAP review notes, OG/RSS, and in-app blurbs must say so.  Do not write
“Congressional trades” or “House and Senate” as if that were the whole feed
(fixed 2026-08-13 in-app; App Store listing caught 2026-08-14).

Premium trial copy must match the live App Store introductory offer:
**2 weeks** (`FREE_TRIAL` / `TWO_WEEKS` on `trade.congress.premium.monthly`
and `.annual`).  Never ship leftover “1-month” / “1 month” trial text.

## Ticker logos
Show company logos next to ticker symbols wherever symbols appear (positions, orders,
watchlist, fills, proposals, scan tables). Fall back to monogram; never leave a blank hole.
Same open icon source as ST: `ticker-logos` / app logo proxy.

## Nothing is truncated without recourse (owner ruling 2026-09-04 — ALL apps, ALL agents)

Owner, in-conversation 2026-09-04: "errors that are ever truncated or anything
truncated should be able to be seen in full on mouseover anywhere on any app in
my opinion."

Binding for every fleet app — web, iOS, desktop, console tables, log and diff
viewers, Bot settings, routine settings, toasts and banners.  This is a bug
class, not a style preference.

- **If the UI shortens it, the UI owes the full value.**  Any string the layout
  clips — a table cell, a card title, a company name, a filename, a URL, a
  rationale, a stack frame, a chat message — must expose its complete text on
  hover.  Native `title` is the baseline and is always acceptable; a real
  tooltip component is better where the app already has one (Socratic.Trade
  console: the `Tooltip` primitive in `app/console/ui/primitives.tsx` — prefer
  it there over a bare `title=`).
- **Errors are the strict case.**  Error text must never be truncated without
  recourse.  An error someone cannot finish reading is an error they cannot act
  on — no offending field, no request id, no symbol, nothing to paste to an
  agent.  If an error banner clips, the full message goes in the hover title,
  and if it is long enough to need scrolling, give it an expand and a copy
  affordance too.
- **Truncating in code is worse than truncating in CSS.**  `.slice(0, 60)`,
  `substring`, a hand-appended `…`, or a server that returns a pre-shortened
  string destroys the full value before it ever reaches the DOM — nothing
  downstream can surface it, and the hover has nothing left to show.  Prefer CSS
  truncation (`text-overflow: ellipsis`, `line-clamp`) with the whole value
  still in the element or its `title`, and let the browser do the clipping.
  When a payload genuinely must be capped for size or cost, cap it at the edge
  and say so (`… 12 more lines`), never silently.
- **Hover is the floor, not the ceiling.**  Hover does not exist on touch and
  does not fire on keyboard focus, so a hover-only `title` leaves phone and
  screen-reader users with the clipped string and no way out.  The same full
  value must also be reachable by tap and by focus — an accessible
  tooltip/popover, a tap-to-expand, or an inline `Show More`.  Fleet precedent:
  board `404f7cc4` (Congress.Trade, hover-only titles on mobile cards) and the
  Socratic.Trade console pass that replaced bare `title=` with the accessible
  `Tooltip` primitive.
- **Never put a secret in a hover title.**  No API key, token, password, session
  cookie, signed URL, or `Authorization` value goes into a `title`, a tooltip,
  or an expanded error.  Titles get copied, screenshotted, pasted into Zulip,
  and read aloud by screen readers.  Redact and keep the shape (`sk-…4f2a`), or
  surface a request id the owner can hand to an agent instead.

**Agent failure mode:** shipping a clipped error toast, a `line-clamp-2`
rationale, or a `.slice(0, 40)` label with no way to read the rest.  Fix on
sight.

## What is NOT in scope
- Code identifiers, API enums, log lines, internal “live stream” engineering labels
  (SSE, live snapshot) unless user-facing product chrome.

## Two spaces between sentences, made with a U+00A0 (owner rule 2026-08-08, strengthened 2026-08-10, 2026-08-14, 2026-08-19 and 2026-09-03; the per-surface gap mechanics re-ruled 2026-10-22, then replaced 2026-10-23 by one mechanism and its two-chat-pane exception — ALL agents, ALL contexts)

Owner: **two spaces between sentences everywhere (a real U+00A0 plus a normal space, commit messages included, except the two chat panes named below, which take the `&nbsp;` entity plus a space), including App Store
submission fields.**  Not optional.  Not “web only.”  Not “UI only.”
**Every single agent, every app, every surface, forever.**

**Strengthened again 2026-08-19 (owner, in-conversation, with the two spaces made as "HOW to emit it" below says, never as two plain ASCII spaces):** "For any and all paragraphs in
any context, always use 2 spaces to separate a period from the beginning of a new sentence."
This closes the last loophole -- the rule is NOT limited to product/user-facing copy.  It
covers every paragraph an agent writes anywhere: **chat replies to the owner**, PR titles and
bodies, commit messages, Zulip posts to #Work-In-Progress, Apple Notes, Linear issues and comments, rollout
notes, review reports and design docs.  If it is prose, it gets the gap.  (Single space
remains correct after a non-terminal abbreviation -- "e.g.", "v1.2.3".  In HTML preserve the
gap with NBSP+space or SENTENCE_GAP, since raw double spaces collapse.  In Markdown, note that
two spaces at the END of a line is a hard line break -- a different thing; this rule is about
the gap BETWEEN sentences.)

**HOW to emit it (owner ruling 2026-10-23, the same on every surface but two chat panes):**  between sentences, write a real U+00A0 (no-break space) followed by a normal space.  Never the `&nbsp;` entity outside the chat panes named below, because everywhere else it shows up as literal text in squash-merge commits, plain-text email and many APIs, and never two plain ASCII spaces, which rendered surfaces collapse to one.  This replaces the earlier per-surface bullets (the entity in chat panes and on GitHub, two ASCII spaces in files, commits, terminals and untested surfaces).

- **Claude Code Code tab and MiniMax Code chat panes (the one exception, owner ruling 2026-10-23):**
  the `&nbsp;` entity plus a normal space, outside code spans.  These panes normalize a raw U+00A0 away and collapse two ASCII spaces, so the entity is the form that renders a real gap there
  (owner-verified 2026-10-08 with a screenshot for the Code tab and 2026-10-18 for the MiniMax Code pane, and confirmed by the owner for both on 2026-10-24).
- **Apple Note bodies (no exception):**  a note in the Coding folder, written by `scripts/apple-notes-coding.sh`, takes the same real U+00A0 plus a normal space as every other surface.  Notes turns the no-break space into an ordinary space on import and still shows the pair as a wide gap (measured Oct 29, 2026).
- **Chat replies in every other pane, Zulip, Linear, Slack, cloud and BotFleet chat:**  a real U+00A0 plus a
  space.  If a pane strips it, leave the text as it is, report the pane, and do not switch to the entity.
- **GitHub PR and issue titles, bodies and comments, review comments, and Zulip posts**
  (anything a tool writes that a Markdown or HTML renderer then shows):  a real U+00A0 plus a
  space.  Never the entity there, because GitHub can copy a PR body
  into a plain-text squash commit, where it would show literally.  You cannot type U+00A0 in
  chat, and a double space you emit into a tool call *sometimes* arrives collapsed to a single
  space — intermittently, not deterministically, so never diagnose it and always verify.  So
  generate the gap in the shell, matching one *or more* spaces so it works in both states:
  ``perl -CSDA -pe 's/(?<![0-9])([.!?])((?:\]\([^()\s]{1,255}\)|[*_`~)\]"\x{27}\x{2019}\x{201D}])*) +(?=[A-Z0-9"*`#@\x{27}\x{2018}\x{201C}\[])/$1$2\x{a0} /g'``.  The ``([*_`~)\]"\x{27}\x{2019}\x{201D}]*)`` group catches a
  sentence end *inside* markup or inside a quoted span, where the space comes after the closing
  span or the closing quote and not after the period.  Then run both checks before posting:
  ``grep -oE $'[.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])*\xc2\xa0' body.nbsp.txt | wc -l`` must be above `0`, and ``grep -cE $'[^0-9][.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])* +[A-Z0-9"*`#@\'‘“[]' body.nbsp.txt``
  must be `0`, because a partly converted body can hold real U+00A0 and still carry one leftover
  single-space gap.  A conversion that matched nothing still exits 0, so a failed run is silent.
  The two checks are here as a block you can copy and run as they are:

  ```sh
  grep -oE $'[.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])*\xc2\xa0' body.nbsp.txt | wc -l
  grep -cE $'[^0-9][.!?](\]\([^[:space:]()]{1,255}\)|[]*_`~)"\'’”])* +[A-Z0-9"*`#@\'‘“[]' body.nbsp.txt
  ```
  `simple-chat post --body-file` additionally refuses any file whose mode is not `0600` or `0400`,
  so `chmod 600` it first.  Code, code comments, identifiers and config never get a U+00A0,
  which is a real defect there rather than a fix.
- **Repo docs and Markdown, skills, agent instructions and commit messages** (owner ruling
  2026-10-23):  the same U+00A0 plus a space.  Rewrite only text you are already editing;  do
  not mass-reformat existing prose.
- **HTML a renderer shows** (Apple Notes `--html`, in-app HTML/JSX, SwiftUI):  a real U+00A0 plus
  a space, spelled Swift `"\u{00A0} "`, JS/TS strings `"\u00A0 "`, JSX text `{'\u00A0'}` plus a space or `&#160;`, HTML markup source `&#160;`, or a shared `SENTENCE_GAP` constant.
  Notes.app is an HTML renderer, so two ASCII spaces in a `<p>` collapse to one.  Never
  `&nbsp; `.
- **Any surface not listed above:**  the same U+00A0 plus a space.

**Rendering trap — history (chat advice reversed twice; the 2026-10-23 ruling above supersedes all of it except in the chat panes it names as its exception):** the 2026-08-19 entity advice for
chat replies was superseded by the 2026-09-04 ASCII ruling for the Code tab, and that was
superseded on 2026-10-08 by the entity again.  Do not re-run these tests.  Owner ruling
2026-10-08: when a surface is known to collapse two typed spaces (any Markdown or HTML
renderer), use its working mechanism without asking; confirmation is no longer needed per
surface.  Owner ruling 2026-10-22 split the surfaces (GitHub the entity, Zulip a real U+00A0,
files read as source two ASCII spaces);  the 2026-10-23 ruling above replaced that split with one mechanism and its two-pane exception.

**What does NOT work, tested in front of the owner:** a raw U+00A0 character in chat
in the **Claude Code Code tab** (normalized away in the view, even though copy-paste showed
two spaces -- do not be fooled by copy-paste).  The same is true in the MiniMax Code
pane:  the working mechanism there is the `&nbsp;` entity plus a normal space, the same
form as the Code tab, NOT a raw U+00A0.  The owner confirmed both panes on 2026-10-24
(Zulip #Policy):  the Claude Code and MiniMax Code chat panes require the
`&nbsp;` entity plus a normal space,
while GitHub, Zulip, docs and UI text take a real U+00A0 plus a space.  Also not
worth trying: app settings (none exist -- `outputStyle` changes tone only, `--output-format` is
headless `claude -p` only, `axScreenReader` only drops borders); patching the client (the CLI is
a ~277MB compiled Mach-O binary, the desktop app is a signed native bundle -- patching breaks
code signing and is wiped by auto-update; do not attempt).  (History only — two literal ASCII spaces in chat
were logged here as not working on 2026-08-19, and they still do not work in the Code tab
(re-confirmed 2026-10-08); the Code tab and the MiniMax Code pane use the entity instead — see
"HOW to emit it" above.)

**Process lesson that cost four rounds of owner correction:** when an instruction appears not to
take effect, diagnose the RENDERING/transport layer between you and the reader -- and ask what
they actually see on screen -- before restating a promise to comply.  Repeating "fixed" on output
the reader cannot see reads as ignoring them.

**Strengthened 2026-09-03 (owner, in-conversation, ALL bots ALL platforms ALL apps):**
the six characters `&nbsp;` must **never appear on screen** in cloud text, bot
answers, messages, ContactLogo.com, Bot settings, routine settings, or any
other owner-visible surface.  The gap still has to be two visible spaces.
If a renderer collapses ASCII doubles, the **backend** inserts a real U+00A0
(or equivalent) before the pixels.  BotFleet desktop `ChatMarkdown` already
maps the entity to U+00A0 for bot bubbles.  JSX/settings/iOS/marketing HTML
must not leak the entity as text — use the per-language spelling above or a shared
`SENTENCE_GAP` helper.  Since 2026-10-23, agents writing into BotFleet /
OpenMausBot chat emit a real U+00A0 plus a space like everywhere else.

Caught live 2026-08-14: Congress.Trade App Store **review notes** still said
“1-month free trial” with single spaces after periods, while the description
already said 2 weeks.  Agents had treated review notes as exempt.  They are
not.

**Where it applies (non-exhaustive — if a human reads it, use the gap):**
- In-app UI strings (web, PWA, iOS native, widgets)
- **App Store Connect — all of it:** description, promotional text, What’s New,
  **App Review notes**, **subscription / IAP review notes**, subscription
  localization descriptions, support/marketing blurbs
- Push / email / Zulip-to-owner product copy / help / privacy / terms prose
- Apple Notes completion notes, rollouts meant for the owner, README user prose
- Marketing, screenshot captions, TestFlight “What to Test”

**How:**
- Between sentences in a paragraph, on every surface:  a real U+00A0 plus a space after `.` `!` `?` (owner ruling 2026-10-23).  Never `&nbsp;` except in the Claude Code Code tab and MiniMax Code chat panes, never two plain ASCII spaces.
- HTML/JSX/SwiftUI source:  Swift `"\u{00A0} "`, JS/TS strings `"\u00A0 "`, JSX text `{'\u00A0'}` plus a space or `&#160;`, HTML markup source `&#160;`, or a shared `SENTENCE_GAP` constant (ST: `SENTENCE_GAP`)
- Code, identifiers and config never get a U+00A0.
- Prefer ONE paragraph for short related sentences over stacked one-liners when
  the owner asked for density (see Socratic proposals empty-state, 2026-08-08)
- Do not insert spaces after brand periods (`Congress.Trade`), URLs, emails, or `U.S.`

**Does NOT apply:** code identifiers, log lines, API enums, pure bullet lists of fragments with no
sentence terminator.

**Agent failure mode:** shipping App Store description, review notes, or UI
paragraphs with single spaces after periods, or stale trial/corpus copy.
Fix on sight.  Protocol: `/Users/jay/apps/FLEET-PROTOCOL.md` § Two spaces.

## Run-once glyph = emoji bolt (owner preference 2026-08-08, Socratic.Trade)
The Run-once affordance uses the colored emoji ⚡ (U+26A1), not a line-icon Zap —
owner: the emoji "reads better than the one on the site." Keep Start/Resume on the
Play glyph (two "go" line-icons side-by-side read as competing primaries). When copy
references the control inline, use the ⚡ emoji there too.
