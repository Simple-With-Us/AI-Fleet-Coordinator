# Agent seat logos (fleet daily digest HTML)

Used by `scripts/build-fleet-daily-digest.py` → copied into `site/agent-logos/`
on each build. The HTML page shows these chips **instead of** printing seat
names like `[GROK]` / `[CODEX]` / `[CLAUDE]`.

| File | Seat |
|------|------|
| `grok.svg` | Grok — the **Grok** mark (`Grok-icon.svg` from `~/Code/Icons - Logos`).  NOT the xAI mark; owner-corrected 2026-08-20. |
| `grok-bot.svg` / `grok-bot.png` | Grok Bot — dark-mode invertible vector mark from `codecaps` (with PNG fallback).  Grok Bot is a separate cloud seat from Grok. |
| `xai.svg` | The xAI company mark, kept under an honest name.  Not used for any seat chip. |
| `codex.svg` | Codex (OpenAI mark) |
| `claude.svg` | Claude (Anthropic mark) |
| `cursor.png` | Cursor — the real Cursor app icon (owner-supplied `cursor.png`), not a generic pointer glyph. |
| `ag.svg` / `gemini.svg` | Antigravity / Gemini |
| `deepseek.svg` | DeepSeek |
| `kimi.svg` | Kimi |
| `minimax.png` | MiniMax |
| `muse-assist.png` / `muse-assist.svg` | Muse Assist (`[MA]`) — transparent-background blue "M" mark from `~/Code/Icons-Logos/AI Platforms : Models/Muse_BlueM_TransparentBG.png` |
| `muse.svg` | Muse Code (`[MC]`) — vector mark for terminal coding agent |
| `sentry.svg` | Sentry |
| `monet.svg` | Monet |
| `owner.svg` / `owner.png` | Jay signature (asset kept for future use) |
| `app-st.svg` / `app-st.png` | Socratic Trade — the offset-candlestick mark.  The SVG is transparent; the PNG is the App Store icon (`Socratic.Trade/graphics/asc-app-icon-1024.png`, black plate) for surfaces that need a solid tile. |
| `app-ct.png` / `app-um.png` / `app-dd.png` / `app-ps.png` / `app-ar.png` / `app-cl.png` / `app-bf.png` | Product app icons (CT, UM, DealDex, Personal Site, Autorotate, ContactLogo, BotFleet) |
| `app-hh.png` | Hog Hunter — the Mac app icon from `HogHunter/Assets.xcassets/AppIcon.appiconset` |
| `app-clutch.png` | Clutch — placeholder C-monogram icon from `Clutch/assets/clutch-icon-1024.png` (formerly `app-harness.png`) |
| `app-cc.png` | CodeCaps — teal "C-with-cap" mark from `codecaps/assets/icon-1024.png` |
| `app-mm.png` | Retired 2026-09-30: MiniMax Remote app icon (app superseded by the Harness iOS app); historical only |

Most vendor marks came from `Socratic.Trade/public/model-logos/`; Grok, Grok Bot,
and Cursor are owner-supplied from `~/Code/Icons - Logos/`.

**Mixed extensions are expected.** Grok Bot and Cursor ship as PNG, the rest are
SVG, so never assume `<slug>.svg` — resolve the extension (see `logo_file()` in
`scripts/build-fleet-daily-digest.py` and `load_agent_logos()` in
`~/apps/mac-collab/mac-collab-server.py`).

**Owner / Jay:** the orange signature is stored as `owner.svg` (PNG embedded) and
`owner.png`, but the daily digest does **not** show an Owner chip. Rows that say
`OWNER ACTION` stay plain text — that label is “needs human follow-up”, not a
coding seat like Grok/Codex/Claude.
