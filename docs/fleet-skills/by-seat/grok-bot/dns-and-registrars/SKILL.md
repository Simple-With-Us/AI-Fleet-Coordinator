---
name: dns-and-registrars
description: >-
  Fleet DNS and registrar playbook.  Cloudflare is DNS for every fleet domain; registrar and Cloudflare account are separate.  Load account, zone, and apex assignments from the operator's private store — never from public skill text. Use when adding a domain, linking a host, choosing a Cloudflare account, or touching nameservers.  Do not buy domains, mint accounts, or enable paid Cloudflare products without Jay.
---

# Fleet DNS and Registrars (ALL AGENTS)

> **This install is for Grok Bot roles.**  Chat tag is `[GB-<NAME>]` — `[GB-DIRECTOR]`, `[GB-FIXER]`, `[GB-DESIGNER]`, `[GB-COMPILER]` (Compiler), `[GB-HOUSEKEEPER]`, `[GB-PUBLISHER]`, `[GB-DEPLOYER]`, `[GB-MONITOR]`, `[GB-PLUMBER]`, `[GB-ORACLE]`, `[GB-TRADER]`, `[GB-ENGINEER]`.  Notes name is the role in Title Case (`Director`, `Monitor`, …).  Cloud branches are often `cursor/`.  Never `[GROK-BOT]`, `[CURSOR]`, `[GROK]`, or `[MONET]`.


Cloudflare is the DNS manager for every fleet domain.  Registrar and Cloudflare account are separate.

**Canonical:** `docs/DNS-AND-REGISTRARS.md` in `AI-Fleet-Coordinator`.

Do not change DNS, buy domains, mint Cloudflare accounts, install products, or spend without Jay.

## Account vs zone vs hostname

A Cloudflare **account** is not a zone and not a hostname.  Each new app gets its **own** new DNS zone for that app's apex.  Do not attach a new app's records to an existing product hostname or shared zone that already serves another app.

| Term | Meaning |
|------|---------|
| **Account** | The Cloudflare account that owns a zone (load the approved assignment from the operator's private store) |
| **New zone** | One apex domain for one app (for example `example.com`) |
| **Do not reuse** | Another app's live hostname or shared zone as the new app's home |

## Default for a new DNS-only zone

App hosted on Vercel, Coolify, or elsewhere, with no Workers / R2 / WAF / other Cloudflare products: create a **new zone for that app's apex** on the **account the operator assigned** for that app (see the private store).

Example: add zone `example.com`, then move registrar nameservers to the nameservers Cloudflare shows for that new zone (Namecheap defaults are in the operator's private store when relevant).

If an app later needs extra Cloudflare services, create a dedicated Cloudflare account for that app and migrate its zone(s) there.  Do not pile unrelated product services onto a shared account without Jay.

## Cloudflare accounts

Each account's login, and the zones each account holds, are private inventory in the operator's own store, so this pack carries neither.  Load them from there before you touch an account.  The canonical doc names which account owns which product family; do not copy that table into public source.

## Registrars

| Registrar | Use for |
|-----------|---------|
| Namecheap | Default for most domains.  `NAMECHEAP_API_KEY` lives in Infisical Fleet-OPS; read it only with `fleet-secrets get NAMECHEAP_API_KEY` (do not read process environment variables for it, and never print or commit the value).  If Infisical is unreachable or the key is missing, stop with a clear error — do not substitute another source. |
| UnstoppableDomains | The operator's private store names which apex lives here |
| Cloudflare registrar | The ST product domains only (assignment in the private store) |

After registration, point nameservers at Cloudflare and manage records in the **app's own zone** on the correct Cloudflare account.  Do not leave a live product on Namecheap parking / URL Forward.

## Linking a domain

1. Confirm host + required records on the **app's own zone**.
2. Confirm that zone is on the **account the operator assigned** (dedicated app account vs shared account — private store).
3. Change DNS at Cloudflare unless nameservers are still at the registrar (then move NS first).
4. On an NS move, copy existing MX onto the new Cloudflare zone (registrar mail forwards are often generic `eforward*.registrar-servers.com`).  Missing MX kills mail.  Do not copy URL Forward or parking CNAMEs.
5. Cloudflare orange-cloud (proxied) hides the origin from HTTP-01. Issue the first cert grey-cloud (DNS only) or use a Cloudflare origin cert, then proxy.  Preferred end state: proxied A/CNAME to the app hostname the operator named.

Do not mint Cloudflare accounts, buy domains, or enable paid Cloudflare products without Jay.  Env/secrets stay with Meteorologist.
