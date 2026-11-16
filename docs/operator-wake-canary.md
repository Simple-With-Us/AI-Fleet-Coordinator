# The One Wake Router:  Install And Canary

Refs AFC-694 and OPS-1048.  This is the runbook for putting the one wake router on the server as the `jay` user and proving one wake end to end, for any seat.  Nothing here is armed by merging it:  the installer and the canary are two scripts someone runs by hand, and every Infisical write and every arming step below is the owner's.

The router serves every seat from one process (`afc-wake-router.service`, merged in #1816):  the Eavesdropper posts each seat's wake to `/wake/<SEAT>`, the router picks the chat, and the seat's platform module delivers it.  The scripts live beside the unit templates in `scripts/simple_tools/server/`:

| File | What it is |
|---|---|
| `install-wake-router.sh` | Checks the host, then installs a clean `git archive` of origin/main and writes both user units.  It refuses to run as root, never uses `sudo`, never writes an Infisical value and never starts anything unless `--start` is passed.  `--dry-run` lists every refusal and changes nothing.  `--uninstall` removes exactly what it installed. |
| `wake-router-canary.sh` | Reads `/health/<SEAT>`, sends exactly one `simple-tools-wake/1` body to `/wake/<SEAT>` addressed to one letter, and follows its receipt.  `--test` asks for the pick and delivers nothing;  `--follow` sends nothing and follows a wake the Eavesdropper already sent. |
| `afc-wake-router.service` | The router's user unit template (#1816):  loopback `127.0.0.1:8794` only. |
| `afc-wake-tls.service` | The TLS front's user unit template:  `socat` as `jay` on the tailnet address, port 8795, passing each connection to the router on loopback.  It is modelled on `zc-wake-tls.service`. |

## What The Installer Puts Where

Everything is owned by `jay`, under `jay`'s home:

- `~/.local/share/afc-wake-router/releases/<sha>/scripts/`:  origin/main's `scripts/` from `git archive`, with `~/.local/share/afc-wake-router/current` pointing at it.  The router's unit runs `releases/<sha>/scripts/simple-chat wake-router serve`, so a checkout's working tree is never imported or run, and the running process's command line names the release it uses.  At the end of each install, a release is removed only when it is neither the newest nor the one the router's running process (its `MainPID`) runs from, so installing A, then B, then C without a restart keeps A while the router still runs it.  If the installer cannot read which release that process runs, it keeps every release.  A release already installed is never rebuilt.
- `~/.config/systemd/user/afc-wake-router.service` and `afc-wake-tls.service`:  rendered from the archive's templates, each with an `# Installed by install-wake-router.sh` first line.  A unit already there without that line is never overwritten.
- `~/.config/afc-wake-router/env`, mode 0600:  only `FLEET_KNOBS_INFISICAL_TOKEN`, the service token `afc-wake-router-ro` (read-only, scoped `prod:/wake-router` in AI Fleet Coordinator), plus comments.  The installer moves it there from `~/.config/infisical-staging/afc-wake-router.env`.  It refuses an `export ` prefix, quotes, a second name or a file anyone but `jay` can read.  There is no key file:  the router reads `WAKE_ROUTER_KEY` from `/wake-router` through `fleet_knobs` and holds it in memory.
- `~/.local/state/afc-wake-router/tls/cert.pem` and `key.pem` (key 0600):  copies of the tailnet certificate and key the front serves, from `--tls-cert` and `--tls-key`.

Before it writes anything, it checks:  that lingering is on for `jay`;  that no root unit named `*wake-router*` is left in `/etc/systemd/system` (it prints the owner's removal command and never runs it);  that the router binds a loopback address on the allowlist and that every port is plain decimal, above 1024 and never 8791 or 8793 (8793 is `zc-wake-tls.service`);  that the tailnet address is in Tailscale's CGNAT range (the /10 starting at 100.64);  that `socat` is installed;  that `/usr/bin/python3`, which the router's unit runs, is Python 3.11 or newer;  and that `/wake-router` holds `WAKE_ROUTER_DELIVERY_ENABLED`, `WAKE_ROUTER_KEY` and `WAKE_ROUTER_MAC_SSH`, with `WAKE_ROUTER_MAC_SSH` equal to `mac-wake`.  It reads those by name through `fleet_knobs`, running the archive's code under `/usr/bin/python3` with the staged token, and prints presence only, never a value.  `WAKE_ROUTER_DELIVERY_SEATS`, `SIMPLE_TOOLS_OPERATOR_REGISTRY_URL` and `SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY` are reported and not required:  without the registry values the router answers `registry-not-configured` in `/health/<SEAT>`.

Every `systemctl --user` call carries `XDG_RUNTIME_DIR` (the caller's, else `/run/user/<uid>`), so it reaches the user manager.  `--start` is refused while delivery is armed, or while the installer cannot confirm it is off.  It starts a stopped unit and restarts one running stale code (a new release, a changed unit or a pending daemon reload).

## The Tailnet Certificate

The front serves the server's tailnet certificate, which expires **Jan 21, 2027 at 9:05pm**.  Who renews it, and whether `tailscale cert` can run as `jay`, is not verified yet.  A renewed certificate reaches the front only when the installer runs again with `--tls-cert` and `--tls-key`, which restarts the front.  The front caps itself at 32 connections at once and refuses TLS older than 1.2.

## Live Steps, In Order

| Step | Who | What |
|---|---|---|
| 1 | Claude AA (the server seat that owns the router), as `jay` on the server | From an AI-Fleet-Coordinator checkout there, run `bash scripts/simple_tools/server/install-wake-router.sh --dry-run --tls-cert CERT --tls-key KEY`.  It must exit 0 and list no refusal.  Then run the same command without `--dry-run`, and with `--start`.  If it reports a root unit in `/etc`, lingering off or a missing knob, that step is the owner's before anything else. |
| 2 | Claude AA, as `jay` on the server | The dry run of the chain, with delivery still off:  `curl -s http://127.0.0.1:8794/health/<SEAT>` must answer `"healthy": true` (the overall `/health` can be 503 while another platform is not installed, so check the seat), then `bash scripts/simple_tools/server/wake-router-canary.sh --seat <SEAT> --letter <XX> --message-id <id> --test`, where the message is a real canary post in the work topic.  It must exit 0, which means the router picked `<XX>`.  Claude BB points the Eavesdropper's wake for the seat at the front (`https://<tailnet name>:8795/wake/<SEAT>`) and reads the setting back. |
| 3 | The owner, in Muster | Arm delivery:  set `WAKE_ROUTER_DELIVERY_ENABLED` to true and `WAKE_ROUTER_DELIVERY_SEATS` to the seat.  The router reads them at its next refresh, or at once after `systemctl --user reload afc-wake-router.service`. |
| 4 | The owner tags the letter in Zulip;  Claude AA follows it;  the woken chat confirms | One canary wake.  The owner (or a seat the owner names) posts one message in the work topic that tags `<XX>`.  The Eavesdropper captures it and logs its `wake_id`.  Claude AA runs `wake-router-canary.sh --seat <SEAT> --letter <XX> --follow <wake_id>`, which must exit 0:  the receipt has the same `wake_id` and `receipt_id`, `picked_letter` is `<XX>` and `delivered` is true.  The woken chat reads the same `wake_id` in its own prompt and says so in the topic.  Claude II posts the three together in AFC-694. |

Whether delivery stays armed after step 4 is the owner's call.

## Canary Exit Codes

`wake-router-canary.sh` exits 0 only when it proved the wake:  a test answer that picked `--letter` and says `delivered: false`, or a final receipt with the same `wake_id` and `receipt_id`, the named letter and `delivered: true`.  Either way the pick must name a `session_id` and a `picked_generation`, and with `--session PREFIX` the session id must start with that prefix.  A wake must be new:  a POST the router answers with an earlier final record (a reused `--wake-id`) fails and proves nothing.  Exit 0 proves the router's side only:  that the woken chat on that host, at that letter and generation, actually received the wake is read in that chat.  Otherwise it exits 3 when the wake was held (delivery off), 4 when it is dead or `delivered` is false or null, 5 when no final receipt came within `--wait`, 6 when another letter was picked, 1 on an unhealthy seat, a transport or router error, or an answer for another wake, and 2 on bad arguments.

It runs as `jay` and refuses root.  It reads the bearer `WAKE_ROUTER_KEY` in its own process through `fleet_knobs` from the installed release, with the token in `~/.config/afc-wake-router/env`, and sends it only in the `Authorization` header:  never printed and never on a command line.  It speaks plain http to a loopback IP literal only, uses no proxy and never follows a redirect.  `--poll-seconds` must be a plain decimal from 0.05 to 60.  It needs Python 3.9 or newer for itself, and the router's own floor (3.11) for `fleet_knobs`.

## Undo

As `jay` on the server:

```sh
bash scripts/simple_tools/server/install-wake-router.sh --uninstall --dry-run
bash scripts/simple_tools/server/install-wake-router.sh --uninstall
```

It runs `systemctl --user disable --now` on each unit it wrote, removes those two unit files, reloads the user manager, moves the token back to `~/.config/infisical-staging/afc-wake-router.env` (0600), and removes `~/.local/share/afc-wake-router` and `~/.local/state/afc-wake-router/tls`.  A unit file without its first-line marker is left alone and named.  The router's own state, `~/.local/state/wake-router` (receipts and pending wakes), is kept.  It stops and disables each unit first and checks it is down:  only systemctl's own answers for a stopped unit (`inactive` or `failed`, exit 3 or 4) count.  If a stop fails, or `is-active` answers anything else, nothing, an error or times out, it removes nothing, keeps the token where it is and exits 1.  Disarming delivery is a separate owner step in Muster:  set `WAKE_ROUTER_DELIVERY_ENABLED` to false.

## Tests

`cd scripts && python3 -m unittest simple_tools.tests.test_wake_router_install -v`.  Everything runs under a temporary test root with synthetic paths (`WAKE_ROUTER_TEST_ROOT`, ignored for root):  a synthetic git origin carrying the real unit templates and a stub `fleet_knobs`, stubs for `systemctl`, `loginctl`, `tailscale` and `socat`, and a scripted fake router on a loopback port.  No test touches the real home, `/etc`, `systemctl`, Infisical or the network.
