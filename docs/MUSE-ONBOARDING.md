# Muse Onboarding: Muse Code Checklist And The Muse Assist Card

Owner-facing and short.  Two products share the name Muse and they are two seats.  **Muse Code** (seat `MC`, tag `[MC]`, branch prefix `muse-code/`) is the local `muse` CLI on this Mac.  **Muse Assist** (seat `MA`, tag `[MA]`, branch prefix `muse-assist/`) is a Meta cloud assistant on its own VM, started from the Mac and iOS apps.  Where each puts checkouts, and why: `docs/protocols/lane-map.md`, platform table.  Written 2026-10-07; nothing below is installed unless a step says so.

## Muse Code Checklist

Each step is **AUTOMATED** (an installer subcommand does it), **OWNER-ACTION** (only the owner can), or **UNKNOWN-TEST** (the outcome is not known; run the test and record the answer).  Run installers from a fresh worktree at `origin/main`, never `~/Code`, and read each `plan` before `apply`; each step names the folder to run from.

1. **Install the tools.  AUTOMATED, installed once on this Mac.**  From `scripts/`: `python3 -m fleet_lanes.install_tools plan tools`, then `apply tools`, then `verify tools`.  This writes `~/apps/lane-tools` (stable copy, hook script) and `~/apps/lane`.  Both exist (installed 2026-10-07 from an older commit), so run `apply tools` AGAIN after this change merges to pick up `muse-seat`.  The seat wrapper `~/apps/lane-tools/muse-seat` and the plugin folder for step 3 come from the same installer.  UNVERIFIED: whether the `tools` target or a `muse` target writes them; `plan tools` and `plan muse` list every file, so read both.
2. **Put the Lane Map rules where Muse reads them.  AUTOMATED, done on this Mac.**  Muse Code loads `~/.claude/CLAUDE.md` as a user-scope fallback, so the block that `python3 -m fleet_lanes.install_rules apply claude --i-own-this-file` writes there (from `scripts/`) reaches Muse with nothing extra.  On 2026-10-07 `install_rules verify claude` printed OK.  `install_rules` has no `muse-code` target on purpose.  **Do not create `~/.config/muse/AGENTS.md`** unless it carries the full fleet protocol: a native user rules file probably replaces the CLAUDE.md fallback (UNVERIFIED) and would drop everything else in it.  UNKNOWN-TEST: start `muse` in a lane, then run `grep -c "fleet-lane-map:begin" <newest session.jsonl>` (path in step 9).  A count above 0 proves the block reached the model; 0 means ask Muse to quote its Lane Map rule and record what it says.
3. **Install and approve the plugin hook.  OWNER-ACTION.**  `<dir>` is the folder that `install_tools plan muse` (from `scripts/`) prints (UNVERIFIED until that target exists).  From a directory Muse already trusts, such as `~/apps`:
   ```
   cd ~/apps
   muse plugins install <dir> --scope user
   muse plugins approve fleet-lane-guard
   ```
   Then start a NEW session; a running one is not assumed to reload plugins (UNVERIFIED).  Run these from a trusted directory because `muse plugins` refused to run in an undecided workspace without an interactive terminal (seen 2026-10-07).  The install and approve syntax comes from the research notes, not from `muse plugins --help`, which could not be read that way.  Approval is a trust decision, which is why `install_tools` cannot do it.
4. **Refresh the skills.  OWNER-ACTION.**  `~/.config/muse/skills` holds all 19 fleet skills and shadows the Claude and Codex copies.  Five were stale on 2026-10-07 (`board-ops`, `fleet-coordination`, `land-lane`, `session-start`, `unstick-pr`), and once this change merges all 19 differ, because the Muse Code banner changed.  `python3 scripts/install-fleet-skills.py` refreshes them, but it has no per-seat flag and rewrites EVERY tool home (Claude, Cursor, Grok, Codex and the rest), not only Muse.  To touch only Muse, copy the pack, which is the same text the installer writes (run both from the repo root of the fresh worktree):
   ```
   for d in docs/fleet-skills/by-seat/muse-code/*/; do n=$(basename "$d"); mkdir -p ~/.config/muse/skills/"$n"; cp "$d/SKILL.md" ~/.config/muse/skills/"$n"/SKILL.md; done
   ```
5. **Adopt the `muse-seat` wrapper.  OWNER-ACTION.**  A past Muse session echoed an empty `AGENT_SEAT`, and `lane new` takes the seat only from that variable.  The wrapper sets `AGENT_SEAT=MC` and `AGENT_TAG=MC`, then runs `muse`.  Add one line to `~/.zshrc`: `alias muse='~/apps/lane-tools/muse-seat'`.  An alias does not reach scripts or other tools that start `muse`, so those call the wrapper by its path.  UNKNOWN-TEST: in a new session ask Muse to run `printenv AGENT_SEAT AGENT_TAG`.  Expect `MC` and `MC`.  If either is empty, the shell tool strips the environment: stop and report it, and do not let Muse guess a seat.
6. **Always start Muse inside a lane.  OWNER-ACTION (a habit).**  `d=$(AGENT_SEAT=MC ~/apps/lane new <app> <slug>) && cd "$d" && ~/apps/lane-tools/muse-seat` (after the step 5 alias, the last word may be `muse`).  Do not shorten it to `cd "$(~/apps/lane new ...)" && muse`: your own shell has no `AGENT_SEAT`, so `lane` refuses and prints nothing, `cd ""` succeeds without moving, and Muse then starts in whatever folder you are standing in, possibly the integration tree `~/Code/<App>` that a daemon resets.  Setting `AGENT_SEAT=MC` on the `lane` call itself keeps a seat exported in your shell from creating the lane for another seat, and the `&&` chain stops when `lane` refuses.  Never run `muse -w`: it appears to create worktrees at `~/Code/<App>/.muse/worktrees`, inside the human's integration tree, and the root cannot be moved (UNVERIFIED, from strings in the binary).  The doctor and the guard treat that folder as sanctioned only so that such worktrees are tracked, not because it is wanted.  Keep the experimental `agents` plugin off: it would create a new folder, `<repo-parent>/<repo>-threads`, beside `~/Code/<App>`.
7. **Trust the lanes.  UNKNOWN-TEST.**  `~/.config/muse/trust.json` lists `/Users/jay`, `/Users/jay/Code`, `/Users/jay/apps` and a few repos, and not `~/apps/lanes`.  In a throwaway test a trusted parent entry did NOT make its child directory "decided", so expect one trust question for each new lane.  Test: start `muse` in a brand-new lane from an interactive terminal and answer the question.  `muse plugins` and `muse exec` refuse an undecided lane when there is no interactive terminal, and `--trust-workspace` trusts for one run only.
8. **Prove the guard denies.  Two probes, neither of which can create a checkout.**
   - **Pipe probe (needs no Muse session).**  Feeds a deny payload to the installed hook command:
     ```
     printf '%s' '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"false && git clone https://github.com/Simple-With-Us/AI-Fleet-Coordinator.git /tmp/fleet-probe"},"cwd":"/Users/jay"}' | ~/apps/lane-tools/lane-guard-hook --format muse
     ```
     Expect one JSON line with `"permissionDecision": "deny"` that names `~/apps/lane new`.  The same command with `echo hi` as the command prints nothing.  Checked 2026-10-07 against the installed hook.  Use the command string the plugin entry actually holds if it differs.
   - **Live probe (needs steps 3 and 5).**  In a new session inside a lane, say: "Run exactly this command line and tell me the output: `false && git clone https://github.com/Simple-With-Us/AI-Fleet-Coordinator.git /tmp/fleet-probe`".  `false` fails first, so the clone never runs even if the hook fails open, and the hook must still block it before it runs.  Pass: Muse reports a block that mentions `~/apps/lane new`.  Fail: Muse reports a plain exit status 1 (the hook did not run).  Afterwards `ls -d /tmp/fleet-probe` must say no such file.  UNKNOWN-TEST: Muse may refuse, reword or split the line, in which case record that and use the pipe probe as the proof.
9. **Confirm in the session log that the hook ran.  UNKNOWN-TEST.**  Muse writes each session to `~/.local/share/muse/sessions/<yyyy>/<mm>/<dd>/<id>/session.jsonl`, plus a `cli-*.log` beside it.  How a hook decision appears in them is not known.  After the live probe run `grep -l "not allowed in temp directories" ~/.local/share/muse/sessions/$(date +%Y/%m/%d)/*/session.jsonl`.  A hit shows the deny text reached Muse.  A miss while the block was visible on screen means the log does not carry hook output; record that.  Keep that phrase out of your own prompt, or it will match itself.

## Muse Assist Card

Muse Assist runs on a Meta VM, cannot see local files and has no hooks, so no installer can reach it.  The owner pastes the card below into the assistant's `Soul.md` (or its Memory file, whichever it reads at the start of every task) in the mobile app.  The owner fills the two bracketed lines.  Card text is credential-free by design.

```
Muse Assist card, version 2 (2026-10-09).  You are Muse Assist, seat MA in Jay's agent fleet.  Your chat tag is [MA] and your branch prefix is muse-assist/ (older work used muse/).  You are not Muse Code ([MC]) and you never sign as another seat.
1. You run on a cloud VM and cannot see Jay's Mac, so everything you change reaches the fleet through a pull request: branch muse-assist/<slug>, push it, open the PR, arm auto-merge once checks pass (gh pr merge <n> --squash --auto), and fix whatever blocks it.  Never push to main.
2. Never clone, worktree, extract or fetch a fleet repo (anything under github.com/Simple-With-Us) into /tmp, /var/tmp, $TMPDIR or any scratch folder.  Keep one checkout per repo in a durable folder in your home and reuse it.  Scratch files and third-party clones in /tmp are fine.
3. There are no lanes on the VM.  Do not create ~/apps/lanes folders and do not run the Mac-only lane command.
4. Post progress to the fleet's #agent-sync channel on Zulip (https://simplewithus.zulipchat.com), tag [MA], with repo: first, as your own bot (muse-assist-bot@simplewithus.zulipchat.com).  Slack is retired.  How you reach Zulip from the VM: [OWNER FILLS THIS IN].  If it still says that, ask Jay before posting anywhere.
5. Never print, echo, paste, log or commit a credential.  Use your secure store.  If you find one in a file or a message, stop and tell Jay without repeating it.
6. Put two spaces between sentences in every message, commit message, PR and document.
7. Long jobs: say what you will run and for how long in chat before starting, and report when it ends.  When a rule here is unclear, ask Jay instead of guessing.
[OWNER FILLS THIS IN: any extra repos or tools this assistant is allowed to use.]
```

**What cannot be automated, and why.**
- The card needs pasting by hand: the mobile app has no file or API path from this Mac, and no custom-instructions box (UNVERIFIED).  Re-paste it whenever the card changes; it carries a version line for that.
- Nothing can deny a bad command on the VM, so the card is a request, not a lock.  The doctor only sees this Mac, so check the assistant's work in its pull requests (branch prefix `muse-assist/`).
- Credentials are entered by the owner in the assistant's secure store, never in chat.
- The chat endpoint is the owner's to supply while the move to Zulip is in progress.

## UNVERIFIED

- `muse -w` layout and branch name (binary strings only), and that no root setting exists.
- A native `~/.config/muse/AGENTS.md`, and whether creating it suppresses the CLAUDE.md fallback.
- `muse plugins install` and `approve` syntax, the plugin folder and `muse-seat` target names (written in the same PR as this file), the claude-like deny shape, and whether a plugin hook runs in untrusted or headless (`muse exec`) runs.
- If `muse -w` takes `<repo>` to be the git top level of the current folder, running it inside a lane nests a worktree inside that lane, and the `muse-repo` pattern does not match it.
- Trust inheritance for `~/apps/lanes`, and whether `AGENT_SEAT` survives into Muse's shell tool.
- How a hook decision shows in the session log.
- Everything about Muse Assist comes from third-party sources: Soul.md, Identity.md and Memory.md, the Debian VM, the CLIs it can run, and its secure store.
