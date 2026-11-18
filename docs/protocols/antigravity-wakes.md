# Antigravity wakes: the Operator reaching a specific Antigravity conversation

Owner request, Sun, Nov 8, 2026 (AFC-665):  configure the Operator's session letters, catalog
discovery and HTTP wake bridge so Antigravity (`AG`) works equally well on the server and on the
Mac alongside all other platforms.

## The chain, end to end

    Zulip tag @AG or DM ag-bot
      -> the listener that holds the seat records the message in the seat inbox
      -> [seat.AG] wake = "http" POSTs simple-tools-wake/1, bearer-signed with AG_ROUTINE_KEY
           -> ag-wake-bridge on the target host (tailnet or loopback address, port 8792)
                validates the contract, remembers the wake id (dedupe),
                resolves the lettered target conversation from the Operator registry
                (topic words over the title, else the most recent live conversation on that host),
                and runs the delivery command in that conversation
                    agy --conversation <uuid> -p "<[AG·wake] ... thread link ...>"

The delivered prompt carries metadata only (channel, topic, the Zulip link, the wake id):  a
message body is never quoted into a prompt, matching the wake alerter and `zc-wake-bridge`
discipline.  The woken agent reads its own inbox with `simple-chat inbox --peek` and answers in
the thread.  Wakes held back by the seat's budget, and messages that arrive while no delivery ran,
stay in the inbox; nothing is lost when the bridge is down, and a wake id is marked seen only
after the POST is handled.

## What is built and tested (AFC-665)

- `scripts/ag-wake-bridge`:  the HTTP receiver, the dedupe store, the target resolver and the
  delivery executor.  State lives under `~/.local/state/ag-wake/` (0700):  `routine-key` (0600,
  the only secret), `config.json` (the delivery switch, `target_host` and command template),
  `seen.json` and `delivery.jsonl`.  `ag-wake-bridge deliver --wake-id ... --topic ...` runs one
  delivery by hand using the same code path as the POST handler.
- `scripts/simple_tools/server/ag-wake-bridge.service`:  the systemd unit template for the server
  (port 8792).  On the Mac, `ag-wake-bridge serve --bind 127.0.0.1 --port 8792` runs with
  `target_host` set to `"mac"` in `~/.local/state/ag-wake/config.json`.
- Registry settings for a hook:  a letter hook is launched with no profile, so `SIMPLE_TOOLS_OPERATOR_REGISTRY_URL` and
  its key reach it through the host's `~/.local/bin/platform-letter` wrapper, which sources the host's registry env
  script in shell before it runs the checkout's `scripts/platform-letter` (on the Mac a script of the host's, on the
  server the fleet-cli managed wrapper).  The letter client never reads that file itself.  `platform_letter.with_registry_env`
  is the wake delivery's helper (the bridges' own delivery calls), not the hooks'.
- Target rules:  target IDs must match a strict UUID regex (`TARGET_RE`), sub-agent conversations
  (`parent_conversation_id` or `nesting_depth > 0` in `conversation_summaries.db`) and killed
  conversations (`killed != 0`) are skipped, sessions on another host (`verified_host`) are
  skipped, and a conversation held by an active delivery lock answers `session-busy`.

## Arming the wake

**Legacy launch point (AFC-838).**  The bridge and these steps predate the wake router.  The
bridge's `deliver` (`scripts/ag-wake-bridge`, lines 417-423) starts `agy` with inherited stdin,
which under launchd or systemd is `/dev/null`, a character device, so it does not have the
no-browser fix below.  The server has no bridge unit (its user units were listed on Nov 18,
2026);  the Mac was not checked.  The wake router is the one wake path, and its `deliver_local`
is the launch point that has the fix;  starting the bridge again would bring back one that does
not.

1. Mint `AG_ROUTINE_KEY` into Infisical Fleet-OPS `/zulip` (32+ characters) via `fleet-secrets`.
2. Write the same value to `~/.local/state/ag-wake/routine-key` (mode `0600`).
3. Configure `~/.local/state/ag-wake/config.json` with `{"delivery_enabled": true, "target_host": "server"}` on the server (or `"target_host": "mac"` on the Mac).
4. In the listener config, set `[seat.AG]` `wake = "http"`, `url_env = "AG_ROUTINE_URL"` and `key_env = "AG_ROUTINE_KEY"`.
5. Start `afc-ag-wake-bridge.service` and verify `curl -s http://127.0.0.1:8792/health`.

## Resuming Without A Browser (AFC-838)

**What happened.**  From router receipts relayed through Echo, not re-checked here:  since 11:19 PM
on Nov 17, 2026, no Antigravity wake was delivered.  On the Mac the resume ended `resume-exit-1`
and opened a Safari sign-in.  On the server it ended `resume-exit-3`:  the CLI's own log shows a
silent sign-in that succeeded, then an individual-quota `RESOURCE_EXHAUSTED` 429.  The adapter
threw the CLI's output away, so both read as a bare exit code.

**The upstream limit.**  The installed `agy` (the server's Linux builds 1.5.5 and 1.5.10) has no
flag or environment variable that only turns off its browser sign-in:  `NO_BROWSER`,
`no-browser` and `--no-launch-browser` do not occur in the binary, the flags registered at
`cmd/cli/main.go:312` include none, and nothing on the print-mode sign-in path reads `CI`, `TERM`,
`BROWSER` or `DISPLAY`.  The browser opens in `auth.(*oauthMethod).StartInteractive`
(`cli/backend/auth/browser.go:79-80`) unless `SSH_CLIENT`, `SSH_TTY` or `SSH_CONNECTION` is
set.  That check only skips the launch:  the CLI still prints the URL and waits 60 seconds
(`printmode.go:465`).

**Why the `SSH_*` variables are not passed through.**  They are not a browser switch alone.  The
same three variables make the language server's token store skip the system keyring
(`codeassistclient.sshDetector`, `keyring_detector_ssh.go:9-10`, read by `shouldBypassKeyring` at
`keyring_detector.go:26`, which `NewCompositeTokenStorage` calls at
`composite_token_storage.go:174`), and they turn on remote-session handling elsewhere
(`store.detectRemoteSession`, `terminal_info_manager.go:311-313`).  On the Mac, where
`simple-chat wake-deliver` does run under `ssh`, passing them through could move the silent
sign-in away from a token kept in the Keychain (the darwin build was not read).  The no-terminal
rule below ends the run before the browser path, so the adapter keeps its clean environment, as
before.

**What holds instead.**  After a silent sign-in fails, `printmode.waitForAuth`
(`cli/printmode/printmode.go:403-493`) asks `interactiveInput` (`printmode.go:390-397`) for a
terminal:  stdin when it is a character device, else `/dev/tty`
(`cli/printmode/tty_posix.go:10-11`).  With neither, it prints
`Error: authentication required. Run '<cli>' to log in, then retry.` and exits 1
(`printmode.go:426-429`, 287-289, 67-77) before `StartOAuthProcess` (`printmode.go:433`), the only
direct print-mode call into the browser sign-in, and before the turn starts (`printmode.go:286`,
against `session.start` at 351 and `runTurn` at 365).  The adapter used to pass `/dev/null` on
stdin, a character device, so every wake looked interactive.  `deliver_local`, the router's one
launch point for the server and for the Mac's `simple-chat wake-deliver`, now runs `agy` with an
empty pipe on stdin and in a session of its own, so it has no `/dev/tty`.  `CI=true` and
`NO_BROWSER` are not applied:  the compiled code checks neither.

**The language server's own login.**  The CLI also starts its language server in process
(`cmd/cli/main.go:955-964`, `CreateLanguageServerAndServe`), and its `setUpClients` has a headless
login that opens a browser without looking at stdin (`language_server/server.go:1538-1561`,
`AuthClient.Login`, then `authclient.openBrowser` at `auth_client.go:1959-1963`).  That block
runs only when the config's headless field is set.  `main.main` sets it in one place
(`main.go:869-873`), and only when `maybeSetRemoteControl` answers true, which needs
`--remote-control` or the daemon command (`main.go:513-516`, `daemon.go:61-62`).  The adapter's
`agy --conversation <id> -p <text>` sets neither.  The server's `antigravity-cli-daemon.service`
runs `agy remote-control serve`, a separate process with that mode on;  a resume is not that
process.

**Wise's observation.**  All of this covers `-p` print mode only.  `agy acp` signs in through its
own server (`acp/server`, `serverTurns.StartOAuthProcess`) and `acp/auth`'s loopback flow
(`oauth_loopback.go`), not through `printmode.waitForAuth`, so the stdin check does not guard
it.  An `agy acp` run with pipes and no terminal can still open a browser, which is what Wise
saw.  The empty-pipe rule here is not a fix for `scripts/agy-acp-runtime`.

**Reading how a run ended.**  The CLI's stderr goes into a pipe that a thread drains, keeping
only the last 8192 bytes in memory;  nothing reaches disk.  After `agy` exits the pipe gets 2
seconds to end;  a process `agy` left holding it is not waited for, and the thread keeps reading
it, within the cap.  These are the CLI's own lines:  `printmode.run` writes control output to
`os.Stderr` unless its caller passes a writer (`printmode.go:213-218`), the text emitter writes
`error: %v` and the `AGY_ERROR` line there (`emitter.go:108`, 126-135), and `waitForAuth` gets
that writer at `printmode.go:286`.  The last line that names an ending decides, since the CLI
writes how it ended last.  Only a fixed code reaches the receipt.  Wakes stay enabled, and no
code holds or pauses a later wake to the same chat.  On the Mac the route reads `ssh:resume`.

**The codes.**

- `resume-quota-exhausted`:  refused, dead, `delivered` null, no retry.  An `error:` or
  `AGY_ERROR:` line names `RESOURCE_EXHAUSTED` (or `ResourceExhausted`), the number 429,
  `Too Many Requests`, `individual quota`, `exhausted your quota` or
  `credits balance is too low` (the server case).
- `resume-signin-required`:  refused, dead, `delivered` false, no retry until a person signs
  in.  The line is `Error: authentication required. Run '<cli>' to log in, then retry.`, the
  fast exit at `printmode.go:428` (the Mac case, now with no browser).
- `resume-signin-interactive`:  refused, dead, `delivered` false, no retry;  a browser may have
  opened, so an operator looks.  The line is the browser branch's
  `Error: authentication required. Run '<cli>' to log in.` (`printmode.go:440`, after
  `StartOAuthProcess` ran), or it starts `Error: authentication timed out.`, `interrupted.` or
  `failed:` (473-488).
- `resume-new-chat`:  refused, dead, `delivered` null, no retry.  `agy` exited 0 but printed
  `warning: conversation "<id>" not found` and ran the turn in a new chat
  (`projectresolve.go:74-78`).  This is the only line an exit 0 is read for.
- `resume-exit-N`:  refused, dead, `delivered` null, no retry, as before.  Any other nonzero
  exit.
- `resume-capture-<Error>`:  failed, a counted retry, since nothing ran.  The pipe or its thread
  could not be made.

A quota or new-chat code keeps `delivered` null because a turn had started.  None of these codes
is `failed`, `busy` or `unreachable`, since each of those would retry against the quota or the
missing sign-in.

**Not proven.**

- The Mac's own `agy` (a darwin build, version unknown) was not read;  the mechanism comes from
  shared POSIX source and the 1.1.2 changelog ("truly headless runs fail fast").  The first Mac
  receipt after rollout tells:  `resume-signin-required` means it held, `resume-signin-interactive`
  means it did not, and `resume-exit-1` means an older runtime or CLI.
- Why the silent sign-in fails on the Mac over `ssh -T` was not checked, and who has to act on it is
  not established.  Those wakes now end fast with no popup, but they still do not deliver.
- A session of its own (setsid) is expected not to change the Mac's Keychain access;  that is not
  verified.
- The exact stderr bytes of a quota failure were never seen;  if no pattern matches, the code
  stays `resume-exit-3`.  A capacity error that says `RESOURCE_EXHAUSTED` or 429 is named
  quota too.
- Only direct calls were traced for the language server.  Browser routes it reaches through an
  interface or an RPC (`SwitchLoginToAuthCode`, `signInPluginThroughRemote`) were not followed,
  and a settings file that turns on remote control was not checked.
- When the `--conversation` id is unknown, `agy` warns and starts a new chat
  (`projectresolve.go:74-78`), and no strict flag exists:  the summaries-row check before launch is
  the only guard, and `resume-new-chat` only names it afterwards.  If more than 8192 bytes of
  stderr follow the warning, the run reads as sent.
- Whether a quota failure leaves the wake prompt in the chat is unknown.  On a timeout only `agy`
  is killed, as before.  An exit 0 is read only for the new-chat warning, so an older CLI that
  exits 0 on a failed turn still reads as sent.
- The 1.2.9 changelog says a `-p` run now ends the background processes it started;  a process
  that still holds stderr is drained, not waited for, and that case was not seen live.
- The Mac keeps answering `resume-exit-1`, with the popup risk, until its `simple-chat` runtime
  carries this module.  Deployment is out of scope here.
