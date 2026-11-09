# board.jays.services → mac.jays.services/board

Owner asked for a hostname short link to THE BOARD.

## Why

`https://mac.jays.services/board` is the canonical URL (pm2 `mac-collab` behind Jay's Tunnel).  `https://board.jays.services` is easier to type and matches the other `jays.services` short links (`activity`, `github`).

## What landed

Cloudflare zone `jays.services` (Usage.Jays.Services):

1. Proxied dummy `AAAA board.jays.services 100::` (record `2d158080a5c1cfa7eaab1b8a004825e8`).  Same origin trick as `activity` / `github`.
2. Single Redirect rule (ruleset `a54b5601ec3f40a487ce46f6287e8e92`, rule `a36fc13833144181b291a4cb7d106854`):
   - expression `(http.host eq "board.jays.services")`
   - 302 to `https://mac.jays.services/board`
   - query string preserved
   - existing `activity` and `github` rules left in place

`CLOUDFLARE_FLEET_API_TOKEN` can write DNS on this zone.  It cannot read/write Single Redirects (403).  Redirects were applied with `CLOUDFLARE_JAY_API_KEY` + `CLOUDFLARE_JAY_ACCOUNT_EMAIL`.

## Verification (2026-08-22)

Local resolver still NXDOMAIN-cached the new name for a bit.  Forced via `--resolve` to Cloudflare anycast `104.21.11.118` (also live on `1.1.1.1`):

```text
curl -sI --resolve board.jays.services:443:104.21.11.118 https://board.jays.services/
# HTTP/2 302
# location: https://mac.jays.services/board

curl -sI --resolve board.jays.services:443:104.21.11.118 "https://board.jays.services/?q=test"
# location: https://mac.jays.services/board?q=test

curl -sI --resolve board.jays.services:80:104.21.11.118 http://board.jays.services/
# HTTP/1.1 302 Found
# Location: https://mac.jays.services/board
```

Universal SSL already covers `*.jays.services`.  Basic Auth on `/board` is unchanged.

## Follow-ups

None required.  Do not add a Mac process, tunnel hostname, or extra `mac-collab` route.  The dummy DNS record has no origin; Cloudflare answers the redirect at the edge.
