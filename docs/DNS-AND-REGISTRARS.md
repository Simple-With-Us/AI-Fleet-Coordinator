# Fleet DNS and Registrars

Owner playbook 2026-08-24 (Jay).  Cloudflare is the DNS manager for every fleet domain.  Registrar and Cloudflare account are separate.

Do not change DNS, buy domains, mint Cloudflare accounts, install products, or spend without Jay.

## Account vs zone vs hostname

These are three different things.  Do not collapse them.

| Term | What it is | Example |
|------|------------|---------|
| **Account** | A Cloudflare login / billing container.  It holds many zones. | **Usage.Jays.Services** |
| **Zone** | One apex domain's DNS inside that account.  A new app gets its own new zone. | `contactlogo.com` created on account Usage.Jays.Services |
| **Hostname** | A name inside a zone (or the zone apex) that answers DNS. | `usage.jays.services` (already a live hostname) |

**Usage.Jays.Services is a Cloudflare account name.**  It is not a zone and not a hostname.  `usage.jays.services` is an already-live hostname.  `jays.services` is an existing DNS-only zone.

A new app such as ContactLogo gets its **own** new Cloudflare zone for that apex (`contactlogo.com`), created on account Usage.Jays.Services.  Do not put ContactLogo — or any new app — records on `usage.jays.services` or `jays.services`.

## Default for a new DNS-only zone

App hosted on Vercel, Coolify, or elsewhere, with no Workers / R2 / WAF / other Cloudflare products: create a **new zone for that app's apex** on the **Usage.Jays.Services** Cloudflare account.

Example (ContactLogo): add zone `contactlogo.com` on account Usage.Jays.Services, then move Namecheap nameservers (`dns1.registrar-servers.com` / `dns2.registrar-servers.com`) to the nameservers Cloudflare shows for that new zone.

If an app later needs extra Cloudflare services, create a dedicated Cloudflare account for that app and migrate its zone(s) there.  Do not pile product services onto the Usage.Jays.Services account.

## Cloudflare accounts

| Account | Do not |
|---------|--------|
| Usage.Jays.Services | Put ST/CT product services here.  Do not attach new-app records to `usage.jays.services` or `jays.services`. |
| SocraticTrade.com | Move unrelated new zones here |
| Congress.Trade | Move unrelated new zones here |
| old | Create or move anything here.  Leave it. |

Each account's login, and the zones each account holds, are private inventory in the Operator's own store, so this playbook carries neither.  Load them from there before you touch an account.

## Registrars

| Registrar | Use for |
|-----------|---------|
| Namecheap | Default for most domains.  `NAMECHEAP_API_KEY` lives in Infisical Fleet-OPS; read it only with `fleet-secrets get NAMECHEAP_API_KEY` (do not read process environment variables for it, and never print or commit the value).  If Infisical is unreachable or the key is missing, stop with a clear error — do not substitute another source. |
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

## See also

- Skill: [`dns-and-registrars`](fleet-skills/dns-and-registrars/SKILL.md)
- New app: [ONBOARDING-NEW-APP.md](ONBOARDING-NEW-APP.md)
