# OpenCode Session Letters

Every OpenCode chat, on the Mac (OpenCode.app and its CLI) and on the server, takes an Operator session letter, a pair from AA to ZZ, and is titled `XX: Title` (AFC-564, owner request Oct 29, 2026).  A chat another chat started (OpenCode's `task` tool makes one, with a `parentID`) takes the pair right after its parent's: parent CC, child CD, then CE.  A main chat takes the doubled pairs in order, oldest first (AA, BB, CC).  The Operator's registry decides; this plugin only asks.

## The Parts

- `index.js` is the OpenCode plugin.  It runs inside OpenCode, watches the chat's events and puts the title on the chat.
- `../tests/opencode_letter_plugin.test.mjs` is its node test (`node --test scripts/agent_sync/tests/opencode_letter_plugin.test.mjs`, also run by `agent_sync/tests/test_platform_letter_opencode.py`).  It is under `tests/` so it is not part of the shipped runtime.
- `../platform_letter_opencode.py` is the adapter on the shared library `../platform_letter.py` (the spec, and the installer for the `plugins` array of `opencode.json`).
- `scripts/platform-letter` is the command the plugin runs: `platform-letter hook --platform opencode`, with the event as JSON on stdin, and one JSON line back.  Everything about letters (the registry, the note, the retries, the secret scan of a title) is in the library.

## What It Does

- A new chat (`session.created`) waits a moment (1.5 seconds) for its first prompt, or for a title the client gives it, and is then registered and titled `XX: New Chat`.  Its first prompt names it (`XX: Fix Login Page`).  A prompt that comes during the wait registers and names the chat in one step, with one hook call and no placeholder.
- OpenCode names a chat itself from its first prompt, with a model.  The plugin never writes over a name it did not give: it reads the chat's title before it writes, and a name that arrived meanwhile is kept, with the letter put in front (`AA: Mutex definition in one sentence`).
- A rename someone makes (the title drops the letter, or changes) puts the letter back, and sends the new words to the Operator.
- A fork (`session.forked`) is a main chat of its own, not a child of the chat it was copied from.  OpenCode copies the other chat's title, letter included (`AA: Fix Login (fork #1)`); the plugin starts it as a fork, which keeps the words and drops the other chat's letter (`AB: Fix Login (fork #1)`).
- A chat another chat started is registered after its parent.  The parent goes first: started when the plugin has not seen it, waited for when its own hook is still running (a task-tool child is made right after the parent's first prompt), and asked once more, no sooner than ten minutes after the last try, when that hook did not answer.
- Archiving a chat closes its registration, which frees its pair; unarchiving registers it again; deleting it closes it for good.  The events an archived chat still sends, or those of a chat OpenCode cannot read, start nothing: the first is not looked at again until the chat is unarchived or prompted, the second not for a minute.
- A chat that is already open when the plugin is first loaded is registered with its first event.  OpenCode loads a plugin with the first prompt of a fresh server, so a chat made before that is first seen through its first prompt.
- Only a prompt that names the chat, and then at most one prompt every 25 minutes (the touch that marks the chat seen), runs the hook.  A steady-state prompt costs nothing.  A chat that has no letter yet (the Mac asleep: the hook exits 0 with a line of context and no label, or does not answer at all) is asked again ten minutes later; a chat that has a letter and no name (a first prompt of only a link, an image, or emoji) is asked at its next prompt, because that prompt may name it.  A title that OpenCode refused is tried again a minute later.
- One engine for the whole OpenCode process.  OpenCode loads the plugin once for each directory it uses and tells every copy the events of every chat in the process; the copies share one engine kept on `globalThis` and drop an event they were told before (its durable sequence number), so a chat costs one hook call however many directories are in use.  Each copy lends the engine its OpenCode handle, and a copy whose directory has gone away is skipped.
- The hook never blocks a chat, and neither does the plugin: nothing it does is awaited by OpenCode, every failure is swallowed, and nothing is printed.

## Who Takes No Letter

Where the plugin runs decides what it can see.  `opencode run` and `opencode mini` use the background service (`opencode serve --service`), one detached process that serves every client.  Its environment is the environment of whichever client started it, days ago, and says nothing about the client of a chat.  A private server (`serve --stdio`) is the client's own child, started by ACP (BotFleet's engine) and by `--standalone`, and has the client's environment.  The plugin tells the two apart by how its process was started (`options.shared` says it outright).

- **In a private server**, the caller's environment applies as it does for every platform: `SESSION_LETTER=0` or `OPENCODE_LETTER=0` turns the plugin off, and a session a launcher started (`AGENT_LAUNCHER` or `AGENT_LAUNCH_SEAT`: a BotFleet bot on the opencode engine has its own letter under BotFleet) takes none.
- **In the shared service** those variables are ignored, and taken out of the environment the hook is run with, so one launcher-run client that started the service cannot switch the letters off for every later chat, and a plain client that started it cannot switch them on for a launcher's.  The signals are per chat instead:
  - a title that begins with `[no-letter]`: `opencode run --title "[no-letter] pr-sweep" ...`.  The sweep's `opencode` entry (`scripts/pr_sweep/platforms.json`) carries it, because its workers are headless runs and nothing in an event tells a one-shot from a chat in the app (both create the session and send the prompt within milliseconds);
  - session metadata `{"platform_letter": "off"}`, which an API client sets when it creates the session;
  - a chat that one of those started (the sub-agents of a sweep worker).
  The title is read when the chat is registered, a moment after it is created, so a title given with the chat counts.  A launcher that cannot use either, or run `--standalone`, has no way to keep the shared service from lettering its chats; the wake path for OPENCODE must use one of them before it is turned on.
- The hook refuses a payload whose title carries the mark too (the spec's `kind` answers `headless`).

## Install

Install the Mac first, so the registry side exists before a server hook depends on it.

```
# On the Mac (from the managed runtime, so the entry names a stable path):
~/.local/bin/platform-letter install --platform opencode
# On the server:
~/.local/bin/platform-letter install --platform opencode --location server --remote mac
# Either host: report, change nothing / take it out again:
~/.local/bin/platform-letter install --platform opencode --check
~/.local/bin/platform-letter install --platform opencode --remove
```

The installer adds the plugin directory to the `plugins` array of `~/.config/opencode/opencode.json` (under `$XDG_CONFIG_HOME` when that is set) and touches nothing else.  The Mac's file has no `plugins` array yet and gets one; the server's file has the lane guard first and keeps it there.  Our entry goes last.  It is idempotent (a current entry is left alone, an outdated one is replaced where it stands, and the options set by hand, `log`, `command`, `shared` and `settle`, are kept by a refresh and do not make `--check` call the entry outdated), takes a dated backup of the file before it rewrites it (`opencode.json.bak-letter-<UTC stamp>`, 0600), keeps the file's mode and indent, and refuses a file that is not a JSON object, has a duplicate key, or whose `plugins` is not a list, untouched.  The server's entry is the object form, `{"package": "<dir>", "options": {"location": "server", "remote": "mac"}}`; the Mac's is the bare path.  A task lane's checkout is refused, because its path goes away: run it from `~/.local/bin/platform-letter`.

There is no trust step.  OpenCode loads a plugin listed in its config with no approval.  A running OpenCode loads it with its next restart.

## What OpenCode Gives a Plugin (v2.1.1 on the server, v2.2.2 on the Mac)

A plugin in the `plugins` array is loaded as `{ id, setup(ctx) }`.  `setup(ctx)` is called; the older `server(input)` entry is not (a diagnostic plugin with both was loaded five times on 2.1.1 and `server` ran never), so the v1 SDK client (`client.session.update({ path: { id }, body: { title } })`) is not handed to a plugin listed in the config.  The same update is `ctx.session.update({ sessionID, title })` in the v2 context, which answers with nothing and emits `session.renamed`; `ctx.session.get({ sessionID })` reads the chat (`parentID`, `fork`, `title`, `metadata`, `time.archived`), and `ctx.event.subscribe()` carries the events of every chat in the process (`session.created`, `session.forked`, `session.inbox.enqueued`, `session.renamed`, `session.archived`, `session.unarchived`, `session.deleted`).  The plugin uses those, and does nothing at all when `ctx.event` or `ctx.session.update` is missing.

Read from a live service (v2.1.1) for this version of the plugin: the plugin is loaded once for each directory in use, all in one process, and a copy was told the created, prompted and forked events of a chat in another directory.  A fork emits `session.forked` and no `session.created`; its `parentID` is the chat it was copied from, the session itself has `fork.sessionID` and no `parentID`, and its title is the other chat's with ` (fork #1)` added.  `opencode run --title X` creates the session and renames it before it sends the prompt.  The shared service is `opencode serve --service` (`process.argv` ends `serve --service`), a private server `serve --stdio`; `opencode acp` hosts a `serve --stdio --port 0` child of its own, in which the launcher's `AGENT_LAUNCHER` was visible; `opencode run` and `opencode mini` connect to the service, and any `opencode serve` without `--stdio` is a shared server like it.

## No Double Registration

OpenCode with a Claude model is still OpenCode.  The string `CLAUDE_CODE_ENTRYPOINT`, which `claude_letter` gates on, is not in the OpenCode binary on either host (0 matches in `~/.opencode/bin/opencode` v2.1.1 and in `OpenCode.app/Contents/Resources/opencode-cli` v2.2.2), and Claude's hook lives in the `hooks` of `~/.claude/settings.json`, which only Claude Code runs.  Measured on a scratch OpenCode on each host: the environment carried `CLAUDE_CODE_ENTRYPOINT=cli`, and `~/.claude/settings.json` held SessionStart, UserPromptSubmit and SessionEnd hooks that write a marker file.  After parent, child, rename, archive, unarchive and delete, no marker existed and the registry held no `claude` row; every row was `opencode`, seat `OPENCODE`.  The plugin registers the platform `opencode` only.  Going the other way, a launcher's session (`AGENT_LAUNCHER`, `AGENT_LAUNCH_SEAT`) and `SESSION_LETTER=0` or `OPENCODE_LETTER=0` turn the plugin off in a private server; see Who Takes No Letter for the shared service.

## Limits

- The plugin cannot tell an `opencode run` one-shot from a chat in the app: both create the session and send the prompt within milliseconds.  A one-shot takes a letter and a title unless it is marked (`--title "[no-letter] ..."`), because the shared service outlives the client and finishes the registration and the title; the hook is started detached, so it also finishes when a private server (`--standalone`) exits first.  The sweep's workers are marked.  Anything else that runs `opencode run` unattended (cron, a wake path) must be too, or use `--standalone` with `SESSION_LETTER=0`.
- A launcher's environment cannot reach the shared service, so a launcher that starts the OpenCode chats of its bots there (not through ACP) is not recognised; its chats take a letter under OpenCode, besides any BotFleet takes for them, unless they carry the mark or the metadata.
- The runtime sync moves the Mac's checkout to origin/main and never runs `install.sh`, so the Mac gets `~/.local/bin/platform-letter` only when someone reruns `scripts/agent_sync/install.sh` there (see the Platform Session Letters section of `../README.md`).
- Shipping: the plugin is `index.js` in this directory.  The managed checkouts (the Mac's runtime, the server's `fleet-cli` `current` link) hold the whole repository, so it is there once a merge lands.  The paused bundle path of `scripts/fleet-cli-release.py` allows only `scripts/agent_sync/*.py` and the files in `EXACT`; if that path is ever turned back on, `scripts/agent_sync/opencode_letter_plugin/index.js` has to be added to `EXACT` (and counts one file against `MAX_FILES`).

## Verified

On Oct 29, 2026, on scratch OpenCodes (own XDG tree, own `opencode.db`, a temporary registry, the free `opencode/big-pickle` model; no real config, database or registry touched):

- Server, OpenCode v2.1.1, the plugin installed by `platform-letter install --platform opencode --location server --remote mac` and the registry reached over a local stand-in for the ssh hop: parent `AA: Mutex definition in one sentence` (OpenCode's own name kept, letter put in front), child `AB: New Chat` then `AB: Name Three Primary Colors in One Line`, a rename to `Billing Audit` became `AB: Billing Audit`, archive closed the registration, unarchive registered it again, delete closed it.  The titles were read back from `opencode.db` `session_v2`.
- Server, with PR 1127's pair-aware registry: two main chats took AA and BB, and a chat the first one started took AB.
- Mac, OpenCode v2.2.2 (`opencode-cli`, entirely under `/tmp`, registry in process): parent `AA: Explain in One Sentence What a Mutex`, child `AB: ...`, the same rename, archive, unarchive and delete.
