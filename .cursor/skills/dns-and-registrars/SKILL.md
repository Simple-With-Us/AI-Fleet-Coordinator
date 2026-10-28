---
name: dns-and-registrars
description: Fleet DNS and registrar playbook.  Cloudflare is DNS for every fleet domain; registrar and Cloudflare account are separate.  Usage.Jays.Services is a Cloudflare account name, not the hostname usage.jays.services.  Create a new zone for each app apex on that account.  Use when adding a domain, linking a host, choosing a Cloudflare account, or touching nameservers.  Do not buy domains, mint accounts, or enable paid Cloudflare products without Jay.
---

# Fleet DNS and Registrars (ALL AGENTS)

> **This install is for `CURSOR`.**  Chat tag `[CURSOR·session8]`.  Notes `Cursor`.  Branches `cursor/`.  Lanes `~/apps/lanes/<Repo>/cursor-<slug>`.  Do not inherit another seat's tag from a shared template.  Zulip bot for seat `CURSOR`, credential in Infisical Fleet-OPS `/zulip` (`CURSOR_ZULIP_EMAIL`, `CURSOR_ZULIP_KEY`, `CURSOR_ZULIP_SITE`).  Session tag `[CURSOR·session8]`, and the `simple-chat` CLI writes it for you.

> **Runtime fork (Cursor).**  Local Cursor IDE / Auto on this Mac is `[CURSOR]`.  If this session is a **Cursor cloud agent spawned as Grok Bot**, your chat tag is `[GB-<NAME>]` (GB-DIRECTOR, GB-FIXER, GB-DESIGNER, GB-COMPILER, GB-HOUSEKEEPER, GB-PUBLISHER, GB-DEPLOYER, GB-MONITOR, GB-PLUMBER, GB-ORACLE, GB-TRADER, GB-ENGINEER) — not `[GROK-BOT]`, not `[CURSOR]`, and not `[GROK]`.  A DeepSeek *model* inside Cursor is still `[CURSOR]` unless you are the separate DeepSeek Harness seat (`[DSH]`).  Never `[MONET]`.


Cloudflare is the DNS manager for every fleet domain.  Registrar and Cloudflare account are separate.

**Canonical:** `docs/DNS-AND-REGISTRARS.md` in `AI-Fleet-Coordinator`.

Do not change DNS, buy domains, mint Cloudflare accounts, install products, or spend without Jay.

## Account vs zone vs hostname

**Usage.Jays.Services is a Cloudflare account name**.  It is not a zone and not a hostname.  `usage.jays.services` is an already-live hostname.  `jays.services` is an existing DNS-only zone.

Each new app gets its **own** new Cloudflare zone for that app's apex on account Usage.Jays.Services.  Do not attach a new app's records to `usage.jays.services` or `jays.services`.

| Term | Example |
|------|---------|
| **Account** | Usage.Jays.Services |
| **New zone** | `example.com` on that account |
| **Do not use as the new-app zone** | `usage.jays.services`, `jays.services` |

## Default for a new DNS-only zone

App hosted on Vercel, Coolify, or elsewhere, with no Workers / R2 / WAF / other Cloudflare products: create a **new zone for that app's apex** on the **Usage.Jays.Services** account.

Example: add zone `example.com` on account Usage.Jays.Services, then move Namecheap nameservers (`dns1.registrar-servers.com` / `dns2.registrar-servers.com`) to the nameservers Cloudflare shows for that new zone.

If an app later needs extra Cloudflare services, create a dedicated Cloudflare account for that app and migrate its zone(s) there.  Do not pile product services onto the Usage.Jays.Services account.

## Cloudflare accounts

| Account | Do not |
|---------|--------|
| Usage.Jays.Services | Put ST/CT product services here.  Do not attach new-app records to `usage.jays.services` or `jays.services`. |
| SocraticTrade.com | Move unrelated new zones here |
| Congress.Trade | Move unrelated new zones here |
| old | Create or move anything here.  Leave it. |

Each account's login, and the zones each account holds, are private inventory in the Operator's own store, so this pack carries neither.  Load them from there before you touch an account.

## Registrars

| Registrar | Use for |
|-----------|---------|
| Namecheap | Default for most domains.  `NAMECHEAP_API_KEY` is in Infisical Fleet-OPS: read the process environment first when it already holds the value, and use `fleet-secrets get NAMECHEAP_API_KEY` only as the approved fallback (never print or commit it). |
| UnstoppableDomains | `jays.services` only |
| Cloudflare registrar | SocraticTrade.com and Socratic.Trade only (the ST Cloudflare account) |

After registration, point nameservers at Cloudflare and manage records in the **app's own zone** on the correct Cloudflare account.  Do not leave a live product on Namecheap parking / URL Forward.

## Linking a domain

1. Confirm host + required records on the **app's own zone**.
2. Confirm that zone is on the right Cloudflare **account** (Usage.Jays.Services unless the app already has a dedicated account).
3. Change DNS at Cloudflare unless nameservers are still at the registrar (then move NS first).
4. On an NS move, copy existing MX onto the new Cloudflare zone (Namecheap mail is often `eforward*.registrar-servers.com`).  Missing MX kills mail.  Do not copy URL Forward or parking CNAMEs (`parkingpage.namecheap.com`).
5. Cloudflare orange-cloud (proxied) hides the origin from HTTP-01. Issue the first cert grey-cloud (DNS only) or use a Cloudflare origin cert, then proxy.  Preferred end state: proxied A/CNAME like `host.jays.services`.

Do not mint Cloudflare accounts, buy domains, or enable paid Cloudflare products without Jay.  Env/secrets stay with Meteorologist.
