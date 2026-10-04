# fleet-recall-service

Hetzner-side HTTP front for the fleet-agents corpus, so cloud seats and phones reach fleet
recall even while the Mac is asleep.  It runs on the Coolify box next to Qdrant and TEI and
imports `scripts/fleet_rag/recall_api.py` unchanged, so the semantics are exactly those of the
`recall` CLI, the stdio `fleet-recall-mcp.py`, and the seat-mcp tools.

Public URL (once deployed): **https://recall.jays.services** — Cloudflare Access + bearer.
`agents.jays.services` stays up as the Mac-side hop that also carries the seat tools; this
service is the one that does not depend on the Mac.

## Endpoints

| Method | Path | Auth | What |
|---|---|---|---|
| GET | `/health` | none | `{ok, name, version, fleet_rag, collection, points, backend_ok, mcp, recall}` — `points` is the live count, cached 30 s; `ok` is the process, `backend_ok` is Qdrant |
| GET | `/recall/stats` | bearer | `recall_stats()` |
| POST | `/recall/search` | bearer | `recall_search(**body)` — `query` required; `limit`, `category`, `app`, `source`, `seat`, `since_days` |
| POST | `/recall/contribute` | bearer | `recall_contribute(**body)` — `text`, `category`, **`seat` required**; `app`, `title`, `url` |
| POST | `/mcp` | bearer | streamable-HTTP JSON-RPC: `initialize` (sets `MCP-Session-Id`), `notifications/*` (202), `ping`, `tools/list`, `tools/call`, `server/discover` — same framing as seat-mcp, so a client can swap the URL |

Auth is `Authorization: Bearer <RECALL_API_TOKEN>` on everything except `/health`, compared in
constant time over bytes (a non-ASCII bearer is simply wrong — 401, never a traceback);
anything else answers 401 with `WWW-Authenticate: Bearer`.  REST errors are
`{"ok": false, "error": ...}` — 400 for caller mistakes (including a body the peer closed
mid-way), 408 for a body that stalls past the socket timeout, 411 for a chunked body (send
`Content-Length`), 413 over 2 MB, 502 (exception class only) when Qdrant or TEI fails.  MCP tool
errors come back as `isError: true` results so the model sees them; unknown tools / methods are
JSON-RPC errors (-32602 / -32601); a message whose `id` is absent **or null** is a notification
(202, no body).  `seat` is required on `recall_contribute` and the process's own `AGENT_SEAT` is
never used for a remote caller.

**Keep-alive discipline.**  The server speaks HTTP/1.1 behind Traefik, which pools connections.
Every reply sent before the request body was consumed — 401, 411, 413, a 404/405 on a POST, and
*every* GET/HEAD, since no GET route reads a body — drains a body of up to 2 MB and closes the
connection (`Connection: close`), so an unread body can never be parsed as the *next* request
on the pooled socket.  (A `GET /health` carrying `Content-Length` or `Transfer-Encoding` used
to leave its bytes on the socket: the pipelined caller got a 501, and a body that was itself a
well-formed request was executed.)  The drain runs once per request, so the 200 / 401 / 404
that follows never blocks on a second read.  Body-less GETs and authenticated, well-formed
POSTs keep the connection open as usual.

**Socket timeout.**  Every connection carries a socket timeout (`RECALL_SOCKET_TIMEOUT`
seconds, default `15`), so a client that declares a body and stalls — `Content-Length: 500000`
with ten bytes sent — can no longer park a handler thread forever in the drain.  When the
timeout fires the pending reply (401 / 404 / 405) still goes out with `Connection: close`; a
stalled body on an authenticated POST is answered **408** with close.  Neither path raises into
`handle_error`, so there is no traceback, and an idle keep-alive connection is closed by
`http.server` after the same timeout (Traefik reconnects transparently).

**Contribute gate.**  `recall_contribute` over this surface is gated exactly as on the Mac: the
regex scrub runs, then gitleaks scans the scrubbed text and refuses anything it still flags
(fail closed if gitleaks cannot deliver a verdict).  `bootstrap.sh` and the `Dockerfile`
install the pinned gitleaks release (`GITLEAKS_VERSION`, default `8.30.1`) for that reason.  If
the download fails the service still starts, the log says so, and every contribution's
`scrubbed` list carries `gitleaks-unavailable` so callers can see the gate was regex-only.

Configuration is **Infisical-first** (see `INFISICAL.md` at the repo root): on startup the service loads every key below from the **AI Fleet Coordinator** Infisical project into an in-memory cache, refreshed in the background; the container never reads `~/.secrets`.  The machine identity (`INFISICAL_AUTOMATION_CLIENT_ID` / `INFISICAL_AUTOMATION_CLIENT_SECRET`) reaches the container through the environment.  When no identity is configured, the keys below fall back to the process environment exactly as before.  Admin changes go through `POST /admin/settings` (write-through to Infisical) with the admin bearer, or the Infisical UI/API plus `POST /admin/reload-settings` / `SIGHUP`.

| Variable | Required | Notes |
|---|---|---|
| `RECALL_API_TOKEN` | yes | bearer for every non-health route; the process refuses to start without it |
| `RECALL_ADMIN_TOKEN` | for admin routes | bearer for `GET`/`POST` `/admin/settings` and `POST` `/admin/reload-settings`; the admin surface answers 403 until it is set |
| `QDRANT_URL` | yes | `http://100.69.77.26:6333` from a container on the box (see *Network*) |
| `QDRANT_API_KEY` | yes | write key, used only by `recall_contribute` |
| `QDRANT_READONLY_API_KEY` | no | used for every read when present |
| `QDRANT_FLEET_COLLECTION` | yes | `fleet-agents` |
| `TEI_URL` | yes | `http://100.69.77.26:8081` |
| `TEI_API_KEY` | yes | |
| `TEI_EMBED_MODEL` | no | informational |
| `PORT` / `HOST` | no | default `8080` / `0.0.0.0` |
| `RECALL_SOCKET_TIMEOUT` | no | per-connection socket timeout in seconds (default `15`); a stalled body is answered and closed instead of parking a thread.  Empty / unparsable / non-positive values fall back to the default |
| `SETTINGS_REFRESH_SECONDS` | no | background settings-refresh interval in seconds (default `300`); itself tunable in Infisical |
| `INFISICAL_ENVIRONMENT` | no | which Infisical env the service reads: `dev` / `staging` / `prod` (default `dev`) |
| `RECALL_REF` | no | git ref the bootstrap fetches (default `main`) |
| `RECALL_FAKE` | no | `1` serves the in-process fake corpus (tests, smoke) |
| `GITLEAKS_VERSION` | no | gitleaks release the bootstrap / image installs (default `8.30.1`) |
| `RECALL_GITLEAKS_DIR` | no | install dir (default `/usr/local/bin`) |
| `RECALL_GITLEAKS_URL` | no | override the release tarball URL (mirrors, tests) |
| `RECALL_GITLEAKS_SHA256` | no | when set, the tarball must match this sha256 |
| `RECALL_GITLEAKS_REQUIRED` | no | `1` makes a failed install abort the bootstrap (the `Dockerfile` sets it for builds); default is log-and-continue |
| `RECALL_GITLEAKS_SKIP` | no | `1` skips the gitleaks step entirely |

Logs go to stdout as `time method path status`; no bodies, headers, query strings, or tokens.

## Files

- `server.py` — the service (stdlib only, `ThreadingHTTPServer`)
- `bootstrap.sh` — fetch the public repo tarball with `urllib`, extract `scripts/fleet_rag` and
  `scripts/fleet-recall-service` into `/app`, install gitleaks, `exec` the server.  Honors
  `RECALL_REF`, `RECALL_APP_DIR`, `RECALL_TARBALL` (reused when present), `RECALL_TARBALL_URL`,
  `RECALL_BOOTSTRAP_ONLY=1`, and the `GITLEAKS_VERSION` / `RECALL_GITLEAKS_*` knobs above.
  The gitleaks step downloads
  `https://github.com/gitleaks/gitleaks/releases/download/v<ver>/gitleaks_<ver>_linux_<x64|arm64>.tar.gz`
  (arch from `uname -m`) with `urllib`, checks the size is plausible (1 MB..200 MB, optional
  sha256), extracts the binary, requires `gitleaks version` to print the pin, then installs it
  atomically to `/usr/local/bin/gitleaks`.  A failure is logged as `bootstrap: gitleaks: … --
  continuing WITHOUT gitleaks` and the server starts anyway.  `RECALL_GITLEAKS_ONLY=1` runs only
  that step (what the `Dockerfile` uses).
- `compose.example.yaml` — the Coolify resource: stock `python:3.12-slim`, a `python -c` command
  that downloads the tarball, extracts `bootstrap.sh`, and execs it; healthcheck on `/health`;
  1 CPU / 1 GiB; `restart: unless-stopped`; `expose: 8080` with no host binding
- `Dockerfile` — COPY-based image for anyone who prefers a build (`docker build -f
  scripts/fleet-recall-service/Dockerfile scripts`), a `RUN` step that installs gitleaks via the
  same bootstrap code (build args `GITLEAKS_VERSION`, `RECALL_GITLEAKS_REQUIRED=1`), non-root
  uid 10001, same healthcheck
- Tests: `scripts/fleet_rag/tests/test_service.py` (`cd scripts && python3 -m unittest
  fleet_rag.tests.test_service -v`)

## Run it locally

```bash
cd scripts
RECALL_API_TOKEN=dev RECALL_FAKE=1 HOST=127.0.0.1 PORT=8080 python3 fleet-recall-service/server.py
curl -s http://127.0.0.1:8080/health
```

Against the live backend, export the `QDRANT_*` / `TEI_*` names from Infisical shared/prod
into the environment first (never paste them into a file), then drop `RECALL_FAKE`.

## Network (checked 2026-09-02 from the box)

From a `python:3.12-slim` container on the `coolify` docker network:

| Target | Result |
|---|---|
| `http://100.69.77.26:6333/healthz` (host Tailscale address, Qdrant) | **200** |
| `http://100.69.77.26:8081/health` (host Tailscale address, TEI) | **200** |
| `http://10.0.1.1:6333/healthz` (docker0 gateway) | connection refused |
| `http://host.docker.internal:6333/healthz` | no such host |
| `qdrant` / `tei` container DNS names | not on the `coolify` network (each Coolify service has its own network `ookh0qml…` / `cday9viy…`) |

So the compose file defaults `QDRANT_URL` and `TEI_URL` to the Tailscale address, the same
values every Mac client already uses.  If the host ever loses Tailscale, the fallback is to
attach `recall-api` to the two service networks and use the `qdrant` / `tei` aliases.

## Deploy on Coolify (orchestrator applies; nothing here is automated)

Targets: project `l11qegr5vie93o7dqlfjgd6n`, environment `production`, server
`jxzqcs3h6g1wiipnnblhismp` (the Hetzner box, public `167.233.254.55` /
`2a01:4f8:1c1b:e6a5::1`), FQDN `https://recall.jays.services`.

1. **Coolify resource.**  Project → production → *New Resource* → *Docker Compose* (empty) on
   server `jxzqcs3h6g1wiipnnblhismp`.  Paste `compose.example.yaml`.  Name it `recall-api`.
   Leave *Connect to predefined network* on (the default) so Traefik can reach it.
2. **Environment variables** (the compose `${…}` references appear in the resource's
   *Environment Variables* tab).  From Infisical shared/prod (project
   `18f563a3-9c88-454c-96eb-28fc9678f3ba`) set `QDRANT_API_KEY`, `QDRANT_READONLY_API_KEY`,
   `TEI_API_KEY`; keep the defaults for `QDRANT_URL`, `TEI_URL`, `QDRANT_FLEET_COLLECTION`,
   `TEI_EMBED_MODEL`, `RECALL_REF=main`.  `SERVICE_PASSWORD_RECALLTOKEN` is generated by Coolify
   — copy its value into Infisical shared/prod as **`RECALL_API_TOKEN`** so Infisical stays the
   canonical home and the Mac seats can read it from there.  Use the Coolify UI or the MCP
   `bulk_env_update` tool; never put a value in a tracked file.
3. **Domain.**  On the `recall-api` service set the domain to `https://recall.jays.services`
   (port 8080).  Traefik issues the Let's Encrypt certificate once DNS resolves.
4. **Cloudflare DNS** (zone `jays.services`, account *Usage.Jays.Services*): `A recall →
   167.233.254.55`, proxied; optionally `AAAA recall → 2a01:4f8:1c1b:e6a5::1`, proxied.  The
   zone is already *Full (strict)*, which is what the other Coolify-hosted names use.
5. **Cloudflare Access** (org `silent-frost-37e0.cloudflareaccess.com`), mirroring
   `agents.jays.services` (app `1e1a5fc4-0f7d-44ac-bb0d-5fa6d5d73ddf`):
   - self-hosted app `recall.jays.services`, session 24 h;
   - policy *Allow* emails `mail@jays.services`, `jaywedgeworth22@gmail.com` (One-time PIN
     IdP `76030d10-ec5b-46ff-ba9d-a4412cd755ce`);
   - policy *Service Auth* with the existing service token
     `215fc9ae-4a1d-4b0e-8ec3-9bd0c49ee1fd` (so every cloud client keeps the same
     `CF-Access-Client-Id` / `CF-Access-Client-Secret` pair it already has for agents);
   - a second app `recall.jays.services/health` with a *Bypass everyone* policy so the Coolify
     healthcheck, UptimeRobot, and `recall doctor` can read health without a token (mirrors
     bypass app `99c0aacc-9e84-49b4-89b0-51bdb4b6bf36`).
6. **Deploy** and watch the log: `compose: fetching …` → `bootstrap: extracted N files` →
   `bootstrap: gitleaks: installed 8.30.1 at /usr/local/bin/gitleaks` → `listening on
   0.0.0.0:8080`.  The first start downloads a ~4 MB source tarball and a ~10 MB gitleaks
   tarball; `start_period` is 120 s.  `docker logs` never shows a token.
7. **Verify** from anywhere: `/health` must be public and report `backend_ok: true` with the
   live point count (38,7xx); `/recall/stats` must be 401 without headers and 200 with them.
8. **Record it**: fleet-ops `ATTACK-MAP.md` (host row), the Coolify resource UUID in
   `docs/RAG-FLEET-INFRA.md` *Health rows*, and an UptimeRobot monitor on
   `https://recall.jays.services/health`.

**Updating**: the container fetches `RECALL_REF` on every start, so *Restart* in Coolify picks
up the latest `main`.  Pin `RECALL_REF` to a tag or sha for a frozen deploy.

**Until this lane lands on `main`** the tarball does not contain `scripts/fleet-recall-service`
and the bootstrap exits with `server.py missing after extract`; deploy after the merge, or set
`RECALL_REF` to the merged branch name.

## Clients

Every request needs the bearer plus the two Cloudflare Access service-token headers (the
service token gets you through Access; the bearer gets you into the service).  Values come from
the environment / a secret store, never from a file in a repo.

### curl (REST)

```bash
curl -sS https://recall.jays.services/health

curl -sS https://recall.jays.services/recall/stats \
  -H "Authorization: Bearer $RECALL_API_TOKEN" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET"

curl -sS https://recall.jays.services/recall/search \
  -H "Authorization: Bearer $RECALL_API_TOKEN" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"query":"handoff file grep trap","limit":3,"app":"fleet"}'

curl -sS https://recall.jays.services/recall/contribute \
  -H "Authorization: Bearer $RECALL_API_TOKEN" \
  -H "CF-Access-Client-Id: $CF_ACCESS_CLIENT_ID" \
  -H "CF-Access-Client-Secret: $CF_ACCESS_CLIENT_SECRET" \
  -H "Content-Type: application/json" \
  -d '{"text":"<one reusable paragraph, 40..4000 chars>","category":"lesson","app":"fleet","seat":"CURSOR","url":"https://github.com/..."}'
```

### MCP (remote, streamable HTTP) — Claude Code Cloud, Cursor cloud, Codex cloud

Claude Code (`claude mcp add`, or the JSON below in `~/.claude.json` / the cloud seat's MCP
config):

```bash
claude mcp add --transport http fleet-recall https://recall.jays.services/mcp \
  --header "Authorization: Bearer ${RECALL_API_TOKEN}" \
  --header "CF-Access-Client-Id: ${CF_ACCESS_CLIENT_ID}" \
  --header "CF-Access-Client-Secret: ${CF_ACCESS_CLIENT_SECRET}"
```

```json
{
  "mcpServers": {
    "fleet-recall": {
      "type": "http",
      "url": "https://recall.jays.services/mcp",
      "headers": {
        "Authorization": "Bearer ${RECALL_API_TOKEN}",
        "CF-Access-Client-Id": "${CF_ACCESS_CLIENT_ID}",
        "CF-Access-Client-Secret": "${CF_ACCESS_CLIENT_SECRET}"
      }
    }
  }
}
```

Cursor (`~/.cursor/mcp.json`, same shape without `"type"`):

```json
{
  "mcpServers": {
    "fleet-recall": {
      "url": "https://recall.jays.services/mcp",
      "headers": {
        "Authorization": "Bearer ${RECALL_API_TOKEN}",
        "CF-Access-Client-Id": "${CF_ACCESS_CLIENT_ID}",
        "CF-Access-Client-Secret": "${CF_ACCESS_CLIENT_SECRET}"
      }
    }
  }
}
```

Codex (`~/.codex/config.toml`; `env_http_headers` names the environment variables that hold
the header values, so `RECALL_AUTH_HEADER` must contain `Bearer <token>`):

```toml
[mcp_servers.fleet-recall]
url = "https://recall.jays.services/mcp"
env_http_headers = { "Authorization" = "RECALL_AUTH_HEADER", "CF-Access-Client-Id" = "CF_ACCESS_CLIENT_ID", "CF-Access-Client-Secret" = "CF_ACCESS_CLIENT_SECRET" }
```

The tracked example is `scripts/seat-mcp/mcp.example.json` (entry `fleet-recall`).  Tools:
`recall_search`, `recall_stats`, `recall_contribute` (pass `seat`).

## Tests

```bash
cd scripts && python3 -m unittest fleet_rag.tests.test_service -v   # 52 tests, no network
cd scripts && python3 -m unittest fleet_rag.tests                     # whole suite
```

The tests bind `127.0.0.1:0`, install `recall_api`'s fake backend, and cover: `/health` without
auth (and with the backend down), 401 without / with a wrong / with a non-ASCII token and 200
with the right one, every REST route, argument validation, the seat requirement, the 502
class-only path, keep-alive discipline (an unauthenticated POST-with-body pipelined with an
authenticated GET on one raw socket, the 413 / 411 / POST-404 paths, a GET/HEAD `/health` with a
Content-Length or chunked body — including a body that is itself a full request — pipelined
with an authenticated GET, an unauthenticated GET with a body, and that body-less GET and
authenticated keep-alive still work), the socket timeout (a stalled `Content-Length: 500000`
body on an unauthenticated POST → 401, on an authenticated POST / `/mcp` → 408, on a GET →
200, each within the timeout with `Connection: close` and no `handle_error`; a peer closing
mid-body → 400; `RECALL_SOCKET_TIMEOUT` parsing), the MCP subset (`initialize` + `MCP-Session-Id`, notifications and
`id: null` → 202, `ping`, `tools/list`, `tools/call`, `server/discover`, framing errors,
protocol-version and `Mcp-Method` checks), `bootstrap.sh` under `RECALL_BOOTSTRAP_ONLY=1`
against a local `http.server` (a fake source tarball plus a fake gitleaks tarball whose binary
is a shell script printing `v8.30.1`: installed and executable; download failure, wrong
version, missing binary, corrupt tarball, size and sha256 rejections all log and continue;
`RECALL_GITLEAKS_REQUIRED=1` aborts; `RECALL_GITLEAKS_ONLY=1` skips the source fetch), and
the real entry point (refuses without `RECALL_API_TOKEN`; `RECALL_FAKE=1` serves; the log never
contains the token).
