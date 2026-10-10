"""Fixture table for the temp-checkout guard: real commands from the 2026-10 sweep plus variants.

Every row carries `source`, which says where the command came from.  "spec" rows are the cases
the owner listed; "sweep" rows are commands found in platform session logs and on disk during the
2026-10-07 discovery sweep (producers-tmp-checkouts.md, tmp-evidence.txt, platform-matrix.json);
"variant" rows are invented to cover quoting, line continuations, env prefixes, cd chains,
subshells, and absolute, tilde and HOME-relative paths.

All rows run against one context: home /Users/jay, TMPDIR in the macOS per-user temp dir, seat
CLAUDE, case-insensitive comparison (APFS), and the frozen REGISTRY_DATA below, so the table
never moves when the real fleet-apps.json gains a row.  A row with `env` replaces that env
wholesale.  `dest`, when set, must equal Decision.destination exactly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

HOME = "/Users/jay"
TMPDIR = "/var/folders/0b/_hqdjtss47j2vcp2fhvfp4r40000gn/T/"
TMPDIR_N = "/var/folders/0b/_hqdjtss47j2vcp2fhvfp4r40000gn/T"     # as the guard normalizes it
SCRATCH = "/private/tmp/claude-501/-Users-jay-Code-AI-Fleet-Coordinator/43eb9ff7/scratchpad"
BASE_ENV: Mapping[str, str] = {"HOME": HOME, "TMPDIR": TMPDIR, "AGENT_SEAT": "CLAUDE"}

# Rule ids, repeated here so the table reads on its own (test_guard checks they match guard's).
CLONE = "clone-into-temp"
GH_CLONE = "gh-clone-into-temp"
WT_ADD = "worktree-add-into-temp"
WT_MOVE = "worktree-move-into-temp"
ARCHIVE = "archive-into-temp"
TARBALL = "tarball-into-temp"
INIT_FETCH = "init-fetch-into-temp"
EXPORT = "checkout-index-into-temp"

# The fleet-apps.json shape, frozen (same rows as test_layout.REGISTRY_DATA).
_APPS = [
    ("Socratic-Trade", "ST", "trading", "Socratic-Trade"),
    ("Congress.Trade", "CT", "congress", "Congress.Trade"),
    ("Usage-Monitor", "UM", "usage", "Usage-Monitor"),
    ("congress-trading-shared", "CTS", "cts", "congress-trading-shared"),
    ("DealDex", "DD", "dealdex", "DealDex"),
    ("AI-Fleet-Coordinator", "AFC", "fleet", "AI-Fleet-Coordinator"),
    ("Personal-Site", "PS", "personal", "Personal-Site"),
    ("Autorotate", "AR", "autorotate", "Autorotate"),
    ("ContactLogo", "CL", "contactlogo", "ContactLogo"),
    ("BotFleet", "BF", "botfleet", "BotFleet"),
    ("HogHunter", "HH", "hoghunter", "HogHunter"),
    ("fleet-ops", "OPS", "fleet-ops", "fleet-ops"),
    ("Clutch", "CK", "clutch", "Clutch"),
]
_SEATS = [
    ("CLAUDE", "claude", ["claude/", "agent/claude"], False),
    ("MONET", "monet", ["monet/"], False),
    ("CODEX", "codex", ["codex/"], False),
    ("AG", "antigravity", ["ag/", "agent/antigravity"], False),
    ("CURSOR", "cursor", ["cursor/"], False),
    ("GROK", "grok", ["grok/"], False),
    ("GROK-BUILD", "grok-build", ["grok-build/"], False),
    ("DSH", "deepseek", ["deepseek/"], True),
    ("HARNESS", "harness", ["harness/"], False),
    ("GROK-BOT", "cursor", ["cursor/"], False),
    ("RENOIR", "renoir", ["renoir/"], False),
    ("FX", "fx", ["fx/"], False),
    ("KIMI", "kimi", ["kimi/"], True),
    ("MM", "minimax", ["minimax/"], False),
    ("MA", "muse-assist", ["muse-assist/", "muse/"], False),
    ("MC", "muse-code", ["muse-code/"], False),
]
REGISTRY_DATA = {
    "owner": "Simple-With-Us",
    "codeRoot": "/Users/jay/Code",
    "appsRoot": "/Users/jay/apps",
    "apps": [{"repo": r, "acronym": a, "worktreePrefix": p, "codeDir": c, "displayName": r, "kind": "product"}
             for r, a, p, c in _APPS],
    "seats": [{"tag": t, "worktreeSuffix": s, "branchPrefixes": b, **({"retired": True} if ret else {})}
              for t, s, b, ret in _SEATS],
}


@dataclass(frozen=True)
class GuardFixture:
    id: str
    command: str
    expect: str                      # "allow" or "deny"
    rule: str = ""                   # expected rule_id for a deny
    source: str = ""                 # provenance of the command
    cwd: str = HOME
    dest: str | None = None          # expected Decision.destination for a deny
    scratchpad_dir: str | None = None
    env: Mapping[str, str] | None = field(default=None)


def deny(id: str, command: str, rule: str, source: str, *, cwd: str = HOME, dest: str | None = None,
         env: Mapping[str, str] | None = None) -> GuardFixture:
    return GuardFixture(id, command, "deny", rule, source, cwd, dest, None, env)


def allow(id: str, command: str, source: str, *, cwd: str = HOME, scratchpad_dir: str | None = None,
          env: Mapping[str, str] | None = None) -> GuardFixture:
    return GuardFixture(id, command, "allow", "", source, cwd, None, scratchpad_dir, env)


_DD = "https://github.com/Simple-With-Us/DealDex.git"
_BF = "https://github.com/Simple-With-Us/BotFleet.git"

DENY_FIXTURES: tuple[GuardFixture, ...] = (
    # ---- the owner's list
    deny("spec-mm-dealdex-cd-clone",
         "cd /tmp && rm -rf dealdex-work && git clone --depth 50 https://github.com/Simple-With-Us/DealDex.git dealdex-work",
         CLONE, "spec; MiniMax Code 2026-10-03/05 session, sweep /private/tmp/dealdex-work",
         dest="/tmp/dealdex-work"),
    deny("spec-grok-botfleet-wt",
         "git -C ~/Code/BotFleet worktree add -b grok/fix-thing /tmp/fix-thing origin/main",
         WT_ADD, "spec; Grok 2026-09-30 BotFleet Publisher workspace", dest="/tmp/fix-thing"),
    deny("spec-um-1595-private-tmp",
         "git -C /Users/jay/Code/Usage-Monitor worktree add /private/tmp/um-1595-wt origin/main",
         WT_ADD, "spec; sweep /private/tmp/um-1595-wt (UM PR #1595)", dest="/private/tmp/um-1595-wt"),
    deny("spec-mktemp-clutch",
         "d=$(mktemp -d) && git clone https://github.com/Simple-With-Us/Clutch.git $d/repo",
         CLONE, "spec", dest=TMPDIR_N + "/tmp.XXXXXXXXXX/repo"),
    deny("spec-mktemp-template-botfleet",
         "D=$(mktemp -d /tmp/gt.XXXXXX); git clone git@github.com:jaywedgeworth22/BotFleet.git $D/bf",
         CLONE, "spec; MiniMax gt2./gittest. mktemp habit", dest="/tmp/gt.XXXXXX/bf"),
    deny("spec-mm-cc-baseline-dot",
         "mkdir -p /tmp/cc-baseline && cd /tmp/cc-baseline && git clone --filter=blob:none "
         "https://github.com/Simple-With-Us/Congress.Trade.git .",
         CLONE, "spec; platform-matrix MM /tmp/cc-baseline 2026-10-05", dest="/tmp/cc-baseline"),
    deny("spec-grok-bf898-codeload",
         "curl -sL https://codeload.github.com/Simple-With-Us/BotFleet/tar.gz/cfb3c43 -o /tmp/bf898.tgz "
         "&& mkdir /tmp/bf898 && tar -xzf /tmp/bf898.tgz -C /tmp/bf898",
         TARBALL, "spec; Grok BotFleet Designer workspace chat_history, sweep /private/tmp/bf898",
         dest="/tmp/bf898.tgz"),
    deny("spec-mm-bf841-archive",
         "git -C ~/Code/BotFleet archive 570af65da | tar -x -C /tmp/bf841prefix",
         ARCHIVE, "spec; MiniMax Kody sweep PR #841, sweep /private/tmp/bf841prefix",
         dest="/tmp/bf841prefix"),
    deny("spec-gh-hoghunter",
         "gh repo clone Simple-With-Us/HogHunter /tmp/hh-verify",
         GH_CLONE, "spec; MiniMax hh-verify scratch", dest="/tmp/hh-verify"),
    deny("spec-tmpdir-codecaps",
         "cd $TMPDIR && git clone https://github.com/Simple-With-Us/CodeCaps.git",
         CLONE, "spec", dest=TMPDIR_N + "/CodeCaps"),
    deny("spec-mm-afc-baseline-cwd",
         "git worktree add /tmp/afc-baseline origin/main",
         WT_ADD, "spec; platform-matrix MM /tmp/afc-baseline 2026-10-04",
         cwd="/Users/jay/Code/AI-Fleet-Coordinator", dest="/tmp/afc-baseline"),

    # ---- found in session logs and on disk
    deny("sweep-grok-st-eod",
         "git -C /Users/jay/Code/Socratic.Trade worktree add -b grok/st-eod-freshness /tmp/st-eod-freshness",
         WT_ADD, "sweep tmp-evidence.txt grok (x6)", dest="/tmp/st-eod-freshness"),
    deny("sweep-grok-st-disclosure-dash-dir",
         "git -C /Users/jay/Code/Socratic-Trade worktree add -b grok/st-disclosure-feed /tmp/st-disclosure-feed",
         WT_ADD, "sweep tmp-evidence.txt grok; Socratic-Trade spelling", dest="/tmp/st-disclosure-feed"),
    deny("sweep-grok-ct-poll-lock",
         "git -C /Users/jay/Code/Congress.Trade worktree add -b grok/ct-poll-lock /tmp/ct-poll-lock",
         WT_ADD, "sweep tmp-evidence.txt grok", dest="/tmp/ct-poll-lock"),
    deny("sweep-grok-botfleet-staging",
         "git worktree add -f /tmp/botfleet-staging",
         WT_ADD, "sweep tmp-evidence.txt grok 2026-09-18 (cwd ~/Code/BotFleet)",
         cwd="/Users/jay/Code/BotFleet", dest="/tmp/botfleet-staging"),
    deny("sweep-mm-afc-detach",
         "git -C /Users/jay/Code/AI-Fleet-Coordinator worktree add -q --detach /tmp/afc-baseline",
         WT_ADD, "sweep tmp-evidence.txt minimax", dest="/tmp/afc-baseline"),
    deny("sweep-mm-verify-clone",
         "git clone -q --depth 1 --branch main https://github.com/jaywedgeworth22/AI-Fleet-Coordinator.git /tmp/mm-verify",
         CLONE, "sweep tmp-evidence.txt minimax; pre-transfer owner", dest="/tmp/mm-verify"),
    deny("sweep-mm-bf-main-lint-from-lane",
         "git worktree add /tmp/bf-main-lint origin/main",
         WT_ADD, "sweep tmp-evidence.txt minimax (cwd a flat MM lane)",
         cwd="/Users/jay/apps/botfleet-minimax-lint", dest="/tmp/bf-main-lint"),
    deny("sweep-bf-st4013-workspace-clone",
         "git worktree add -q /tmp/st4013",
         WT_ADD, "sweep platform-matrix BotFleet native logs (bot workspace nested clone)",
         cwd="/Users/jay/.botfleet/workspaces/e8da75c0-8b66-4ab5-9dbf-fe6ad7466db4/Socratic.Trade",
         dest="/tmp/st4013"),
    deny("sweep-bf-st-4013-verify",
         "git worktree add --detach /tmp/st-4013-verify",
         WT_ADD, "sweep tmp-evidence.txt botfleet", cwd="/Users/jay/Code/Socratic.Trade",
         dest="/tmp/st-4013-verify"),
    deny("sweep-relay-st-pr-ssh",
         "mkdir -p /tmp/st-pr && git clone ssh://github.com/jaywedgeworth22/Socratic-Trade.git /tmp/st-pr/repo",
         CLONE, "sweep /private/tmp/st-pr/repo reflog (bundle relay)", dest="/tmp/st-pr/repo"),
    deny("sweep-ag-cc163-verify",
         "git clone https://github.com/Simple-With-Us/CodeCaps.git /private/tmp/cc163-verify && "
         "cd /private/tmp/cc163-verify && git checkout ag/settings-window-layering",
         CLONE, "sweep /private/tmp/cc163-verify reflog", dest="/private/tmp/cc163-verify"),
    deny("sweep-bf-c-blobless",
         "git clone --filter=blob:none https://github.com/Simple-With-Us/BotFleet.git /tmp/bf-c",
         CLONE, "sweep /private/tmp/bf-c reflog", dest="/tmp/bf-c"),
    deny("sweep-ct2656-review",
         "git -C ~/Code/Congress.Trade worktree add --detach /private/tmp/ct2656-review pr-2656",
         WT_ADD, "sweep /private/tmp/ct2656-review (linked worktree)", dest="/private/tmp/ct2656-review"),
    deny("sweep-codecaps-pr179-review",
         "cd ~/Code/CodeCaps && git fetch origin pull/179/head:pr-179 && "
         "git worktree add /private/tmp/codecaps-pr179-review pr-179",
         WT_ADD, "sweep /private/tmp/codecaps-pr179-review (linked worktree)",
         dest="/private/tmp/codecaps-pr179-review"),
    deny("sweep-um-relay-init-fetch",
         "mkdir -p /tmp/um-relay && cd /tmp/um-relay && git init -q && "
         "git remote add origin git@github.com:Simple-With-Us/Usage-Monitor.git && git fetch --depth 1 origin main",
         INIT_FETCH, "sweep /private/tmp/um-relay (relay clones); init-plus-fetch shape", dest="/tmp/um-relay"),
    deny("sweep-redirect-log-tmp-cwd",
         "git clone --depth 50 -b main https://github.com/Simple-With-Us/DealDex.git dealdex-work > /tmp/clone.log",
         CLONE, "sweep tmp-evidence.txt botfleet/minimax (run from /tmp)", cwd="/tmp",
         dest="/tmp/dealdex-work"),

    # ---- variants: init plus fetch
    deny("var-init-pull-url",
         "mkdir -p /tmp/dd-pull && cd /tmp/dd-pull && git init && git pull " + _DD + " main",
         INIT_FETCH, "variant of sweep-um-relay-init-fetch", dest="/tmp/dd-pull"),
    deny("var-init-fetch-dash-c",
         "git init /tmp/afc-fetch && git -C /tmp/afc-fetch remote add origin "
         "https://github.com/Simple-With-Us/AI-Fleet-Coordinator.git && git -C /tmp/afc-fetch fetch origin main",
         INIT_FETCH, "variant of sweep-um-relay-init-fetch with -C", dest="/tmp/afc-fetch"),
    deny("var-remote-add-f",
         "mkdir /tmp/dd-f && cd /tmp/dd-f && git init -q && git remote add -f origin " + _DD,
         INIT_FETCH, "variant: remote add -f fetches at once", dest="/tmp/dd-f"),

    # ---- variants: bare and mirror
    deny("var-mirror",
         "git clone --mirror " + _BF + " /tmp/bf-mirror.git",
         CLONE, "variant", dest="/tmp/bf-mirror.git"),
    deny("var-bare-var-tmp",
         "git clone --bare git@github.com:Simple-With-Us/HogHunter.git /var/tmp/hh.git",
         CLONE, "variant", dest="/var/tmp/hh.git"),

    # ---- variants: TMPDIR, variables and mktemp
    deny("var-tmpdir-brace-default",
         'git clone https://github.com/Simple-With-Us/DealDex "${TMPDIR:-/tmp}/dd-check"',
         CLONE, "variant; update-botfleet.sh TMPDIR idiom", dest=TMPDIR_N + "/dd-check"),
    deny("var-tmpdir-brace-unset",
         'git clone https://github.com/Simple-With-Us/DealDex "${TMPDIR:-/tmp}/dd"',
         CLONE, "variant with TMPDIR unset", dest="/tmp/dd",
         env={"HOME": HOME, "AGENT_SEAT": "CLAUDE"}),
    deny("var-tmpdir-strip-suffix",
         'git clone ' + _DD + ' "${TMPDIR%/}/dd-strip"',
         CLONE, "variant", dest=TMPDIR_N + "/dd-strip"),
    deny("var-runner-temp-default",
         'git clone https://github.com/Simple-With-Us/Usage-Monitor.git "${RUNNER_TEMP:-/tmp}/um-ci"',
         CLONE, "variant; UM verify-apple-projects.sh RUNNER_TEMP idiom", dest="/tmp/um-ci"),
    deny("var-assign-tmp",
         "T=/tmp/x; git clone https://github.com/Simple-With-Us/Autorotate.git $T",
         CLONE, "variant; scratch tmp-guard-prototype.py missed case", dest="/tmp/x"),
    deny("var-export-var",
         'export WORK=/private/tmp/ps-work && git clone https://github.com/Simple-With-Us/Personal-Site.git "$WORK/ps"',
         CLONE, "variant", dest="/private/tmp/ps-work/ps"),
    deny("var-mktemp-inline",
         'git clone https://github.com/Simple-With-Us/ContactLogo.git "$(mktemp -d)/cl"',
         CLONE, "variant", dest=TMPDIR_N + "/tmp.XXXXXXXXXX/cl"),
    deny("var-mktemp-backticks",
         "git clone https://github.com/Simple-With-Us/ContactLogo.git `mktemp -d`/cl",
         CLONE, "variant", dest=TMPDIR_N + "/tmp.XXXXXXXXXX/cl"),
    deny("var-mktemp-t-gh",
         'W=$(mktemp -d -t ct-review) && gh repo clone Simple-With-Us/Congress.Trade "$W/ct"',
         GH_CLONE, "variant; BotFleet publish-ios-versions.sh mktemp -t + gh repo clone",
         dest=TMPDIR_N + "/ct-review/ct"),
    deny("var-mktemp-tmpdir-template-archive",
         'BOOT="$(mktemp -d "${TMPDIR:-/tmp}/bf-boot.XXXXXX")" && git -C ~/Code/BotFleet archive HEAD | tar -x -C "$BOOT"',
         ARCHIVE, "variant of update-botfleet.sh bootstrap with a resolvable fleet repo",
         dest=TMPDIR_N + "/bf-boot.XXXXXX"),
    deny("var-gatetest-pid",
         "git clone " + _DD + " /tmp/gatetest-$$",
         CLONE, "variant; MiniMax /tmp/gatetest-$$ habit", dest="/tmp/gatetest-0"),
    deny("var-for-loop-unknown-name",
         "for r in DealDex HogHunter; do git clone https://github.com/Simple-With-Us/$r.git /tmp/check-$r; done",
         CLONE, "variant", dest="/tmp/check-<unknown>"),

    # ---- variants: shell structure
    deny("var-subshell-cd",
         "(cd /tmp && git clone https://github.com/Simple-With-Us/Personal-Site.git ps-check)",
         CLONE, "variant", dest="/tmp/ps-check"),
    deny("var-line-continuation",
         "git clone \\\n  --depth 1 \\\n  https://github.com/Simple-With-Us/fleet-ops.git \\\n  /tmp/fleet-ops-check",
         CLONE, "variant", dest="/tmp/fleet-ops-check"),
    deny("var-env-prefix",
         "GIT_TERMINAL_PROMPT=0 GIT_LFS_SKIP_SMUDGE=1 git clone " + _BF + " /tmp/bf-nolfs",
         CLONE, "variant", dest="/tmp/bf-nolfs"),
    deny("var-env-wrapper",
         "env GIT_TERMINAL_PROMPT=0 git clone " + _BF + " /tmp/bf-env",
         CLONE, "variant", dest="/tmp/bf-env"),
    deny("var-timeout-wrapper",
         "timeout 600 git clone " + _BF + " /tmp/bf-timeout",
         CLONE, "variant", dest="/tmp/bf-timeout"),
    deny("var-command-wrapper",
         "command git -C ~/Code/DealDex worktree add /tmp/dd-cmd",
         WT_ADD, "variant", dest="/tmp/dd-cmd"),
    deny("var-home-var",
         'git -C "$HOME/Code/DealDex" worktree add -b claude/x /tmp/dd-x',
         WT_ADD, "variant", dest="/tmp/dd-x"),
    deny("var-git-dir-global",
         "git --git-dir=/Users/jay/Code/HogHunter/.git worktree add /tmp/hh-wt main",
         WT_ADD, "variant", dest="/tmp/hh-wt"),
    deny("var-config-global",
         "git -c core.longpaths=true -C ~/Code/Clutch worktree add -B claude/tmp /tmp/clutch-wt origin/main",
         WT_ADD, "variant", dest="/tmp/clutch-wt"),
    deny("var-pushd",
         "pushd /private/tmp >/dev/null && gh repo clone Simple-With-Us/Autorotate && popd",
         GH_CLONE, "variant", dest="/private/tmp/Autorotate"),
    deny("var-cd-dash",
         "cd /tmp && cd ~/apps && cd - && git clone " + _DD + " dd-back",
         CLONE, "variant", dest="/tmp/dd-back"),
    deny("var-or-true",
         "git clone " + _DD + " /tmp/dd-or 2>/dev/null || true",
         CLONE, "variant", dest="/tmp/dd-or"),
    deny("var-quoted-space",
         'git clone ' + _DD + ' "/tmp/dd with space"',
         CLONE, "variant", dest="/tmp/dd with space"),
    deny("var-upper-case",
         "git clone https://github.com/simple-with-us/dealdex.git /TMP/DealDex-Upper",
         CLONE, "variant; APFS is case-insensitive", dest="/TMP/DealDex-Upper"),
    deny("var-if-then",
         "if [ ! -d /tmp/um-x ]; then git -C ~/Code/Usage-Monitor worktree add /tmp/um-x origin/main; fi",
         WT_ADD, "variant", dest="/tmp/um-x"),
    deny("var-multiline",
         "cd ~/Code/BotFleet\ngit fetch origin\ngit worktree add /tmp/bf-review origin/main",
         WT_ADD, "variant", dest="/tmp/bf-review"),
    deny("var-bash-c",
         "bash -lc 'cd /tmp && git clone https://github.com/Simple-With-Us/DealDex.git dd-bash'",
         CLONE, "variant", dest="/tmp/dd-bash"),
    deny("var-heredoc-to-shell",
         "bash <<'EOF'\ncd /tmp\ngit clone https://github.com/Simple-With-Us/DealDex.git dd-heredoc\nEOF",
         CLONE, "variant", dest="/tmp/dd-heredoc"),
    deny("var-eval",
         'eval "git clone ' + _DD + ' /tmp/dd-eval"',
         CLONE, "variant", dest="/tmp/dd-eval"),

    # ---- variants: sources
    deny("var-local-path-source",
         "git clone ~/Code/BotFleet /tmp/bf-local",
         CLONE, "variant", dest="/tmp/bf-local"),
    deny("var-file-url-source",
         "git clone file:///Users/jay/Code/HogHunter /tmp/hh-file",
         CLONE, "variant", dest="/tmp/hh-file"),
    deny("var-flat-lane-source",
         "git -C ~/apps/botfleet-claude-kody worktree add /tmp/bf-kody-check origin/main",
         WT_ADD, "variant", dest="/tmp/bf-kody-check"),
    deny("var-nested-lane-relative-dest",
         "git worktree add ../../../../../../tmp/dd-rel",
         WT_ADD, "variant", cwd="/Users/jay/apps/lanes/DealDex/claude-x", dest="/tmp/dd-rel"),
    deny("var-tilde-user",
         "git clone " + _DD + " ~jay/../../tmp/dd-tilde",
         CLONE, "variant", dest="/tmp/dd-tilde"),
    deny("var-credential-url",
         "git clone https://agentuser:hunter2@github.com/Simple-With-Us/DealDex.git /tmp/dd-tok",
         CLONE, "variant; credentials in the URL must never reach the reason", dest="/tmp/dd-tok"),
    deny("var-separate-git-dir",
         "git clone --separate-git-dir=/tmp/dd-gitdir " + _DD + " ~/apps/lanes/DealDex/claude-sep",
         CLONE, "variant: the checkout is a lane but its git dir is in temp", dest="/tmp/dd-gitdir"),
    deny("var-worktree-move",
         "git -C ~/Code/BotFleet worktree move ~/apps/botfleet-claude-x /tmp/bf-moved",
         WT_MOVE, "variant", dest="/tmp/bf-moved"),
    deny("var-var-folders-direct",
         "git clone " + _DD + " /private/var/folders/0b/abc123/T/dd",
         CLONE, "variant; another user temp dir under /private/var/folders", dest="/private/var/folders/0b/abc123/T/dd"),
    deny("var-checkout-index",
         "git -C ~/Code/DealDex checkout-index -a -f --prefix=/tmp/dd-export/",
         EXPORT, "variant", dest="/tmp/dd-export"),

    # ---- variants: gh
    deny("var-gh-url",
         "gh repo clone https://github.com/Simple-With-Us/Usage-Monitor /tmp/um-gh",
         GH_CLONE, "variant", dest="/tmp/um-gh"),
    deny("var-gh-bare-name",
         "gh repo clone DealDex /tmp/dd-gh",
         GH_CLONE, "variant; gh defaults to the signed-in owner", dest="/tmp/dd-gh"),
    deny("var-gh-dash-dash",
         "gh repo clone Simple-With-Us/BotFleet /tmp/bf-shallow -- --depth=1",
         GH_CLONE, "variant", dest="/tmp/bf-shallow"),

    # ---- variants: tarballs and archives
    deny("var-api-tarball-pipe",
         "curl -sSL -H 'Accept: application/vnd.github+json' "
         "https://api.github.com/repos/Simple-With-Us/DealDex/tarball/main | tar -xz -C /tmp/dd-src",
         TARBALL, "variant", dest="/tmp/dd-src"),
    deny("var-gh-api-redirect",
         "gh api repos/Simple-With-Us/HogHunter/tarball/main > /tmp/hh.tgz",
         TARBALL, "variant", dest="/tmp/hh.tgz"),
    deny("var-wget-archive",
         "wget -q https://github.com/Simple-With-Us/Clutch/archive/refs/heads/main.tar.gz -O /tmp/clutch.tgz",
         TARBALL, "variant", dest="/tmp/clutch.tgz"),
    deny("var-wget-pipe-old-tar",
         "wget -qO- https://codeload.github.com/Simple-With-Us/Clutch/tar.gz/main | tar xz -C /tmp/clutch-src",
         TARBALL, "variant", dest="/tmp/clutch-src"),
    deny("var-zipball-unzip",
         "curl -L -o ~/Downloads/dd.zip https://github.com/Simple-With-Us/DealDex/archive/main.zip && "
         "unzip -q ~/Downloads/dd.zip -d /tmp/dd-zip",
         TARBALL, "variant: downloaded outside temp, extracted into it", dest="/tmp/dd-zip"),
    deny("var-curl-remote-name",
         "cd /tmp && curl -LO https://github.com/Simple-With-Us/DealDex/archive/refs/heads/main.zip",
         TARBALL, "variant", dest="/tmp/main.zip"),
    deny("var-curl-pipe-subshell-tar",
         "curl -sL https://codeload.github.com/Simple-With-Us/HogHunter/tar.gz/main | (cd /tmp/hh-tree && tar -xz)",
         TARBALL, "variant", dest="/tmp/hh-tree"),
    deny("var-archive-output",
         "git -C ~/Code/DealDex archive --format=tar.gz -o /tmp/dd.tar.gz HEAD",
         ARCHIVE, "variant", dest="/tmp/dd.tar.gz"),
    deny("var-archive-cd-pipe",
         "cd ~/Code/BotFleet && git archive --prefix=bf/ HEAD | tar -xf - -C /private/tmp",
         ARCHIVE, "variant", dest="/private/tmp"),
    deny("var-archive-remote",
         "git archive --remote=git@github.com:Simple-With-Us/fleet-ops.git main | tar -x -C /tmp/ops",
         ARCHIVE, "variant", dest="/tmp/ops"),
    deny("var-archive-redirect",
         "git -C ~/Code/HogHunter archive HEAD > /tmp/hh.tar",
         ARCHIVE, "variant", dest="/tmp/hh.tar"),
    deny("var-archive-gunzip-stage",
         "git -C ~/Code/Clutch archive --format=tar.gz HEAD | gunzip | tar -x -C /tmp/clutch-tree",
         ARCHIVE, "variant", dest="/tmp/clutch-tree"),

    # ---- review 2026-10-07 (guard-bypass): an unknown repo name with an implicit destination
    deny("rev-loop-implicit-dest",
         "cd /tmp && for r in DealDex HogHunter; do git clone https://github.com/Simple-With-Us/$r.git; done",
         CLONE, "review finding 1; loop extension of spec-tmpdir-codecaps", dest="/tmp/<unknown>"),
    deny("rev-gh-loop-implicit-dest",
         "for r in DealDex BotFleet; do gh repo clone Simple-With-Us/$r -- --depth 1; done",
         GH_CLONE, "review finding 1; gh form", cwd="/tmp/sweep", dest="/tmp/sweep/<unknown>"),
    deny("rev-positional-repo-var",
         'REPO=$1; git clone "https://github.com/Simple-With-Us/${REPO}.git"',
         CLONE, "review finding 1; script-argument form", cwd="/tmp", dest="/tmp/<unknown>"),
    deny("rev-mktemp-t-unknown-template",
         'W=$(mktemp -d -t "$1") && gh repo clone Simple-With-Us/Congress.Trade "$W/ct"',
         GH_CLONE, "review finding 1; same root cause in mktemp -t", dest=TMPDIR_N + "/<unknown>/ct"),

    # ---- review 2026-10-07: a pathspec that still covers the whole tree is a checkout
    deny("rev-archive-dot-pathspec",
         "git -C ~/Code/BotFleet archive HEAD -- . | tar -x -C /tmp/bf-dot",
         ARCHIVE, "review finding 2; `.` is the whole tree", dest="/tmp/bf-dot"),
    deny("rev-archive-top-magic",
         "git -C ~/Code/BotFleet archive HEAD -- ':/' | tar -x -C /tmp/bf-top",
         ARCHIVE, "review finding 2; pathspec magic for the top of the tree", dest="/tmp/bf-top"),
    deny("rev-archive-exclude-only",
         "git -C ~/Code/BotFleet archive HEAD -- ':!docs' | tar -x -C /tmp/bf-nodocs",
         ARCHIVE, "review finding 2; an exclude-only pathspec is everything else", dest="/tmp/bf-nodocs"),
    deny("rev-archive-glob",
         "git -C ~/Code/BotFleet archive HEAD '*' | tar -x -C /tmp/bf-glob",
         ARCHIVE, "review finding 2; a top-level glob spans the tree", dest="/tmp/bf-glob"),
    deny("rev-archive-parent-of-cwd",
         "git archive HEAD .. | tar -x -C /tmp/bf-parent",
         ARCHIVE, "review finding 2; `..` from a subdirectory", cwd="/Users/jay/Code/BotFleet/scripts",
         dest="/tmp/bf-parent"),
    deny("rev-archive-add-file",
         "git -C ~/Code/BotFleet archive --add-file extra.txt HEAD | tar -x -C /tmp/bf-extra",
         ARCHIVE, "review finding 2; a separate option value is not the tree-ish", dest="/tmp/bf-extra"),
    deny("rev-checkout-index-stdin",
         "git -C ~/Code/DealDex ls-files -z | git -C ~/Code/DealDex checkout-index -z --stdin --prefix /tmp/dd-stdin/",
         EXPORT, "review finding 2; ls-files piped to --stdin is the full-export idiom", dest="/tmp/dd-stdin"),
    deny("rev-checkout-index-combined-a",
         "git -C ~/Code/DealDex checkout-index -fa --prefix=/tmp/dd-fa/",
         EXPORT, "review finding 2; -a inside combined short flags", dest="/tmp/dd-fa"),

    # ---- review 2026-10-07: fetch or pull of a local fleet path into a temp repo
    deny("rev-init-pull-local-path",
         "mkdir /tmp/ip1 && cd /tmp/ip1 && git init && git pull ~/Code/BotFleet main",
         INIT_FETCH, "review finding 3", dest="/tmp/ip1"),
    deny("rev-init-fetch-local-path-dash-c",
         "git init /tmp/ip2 && git -C /tmp/ip2 fetch ~/Code/BotFleet main",
         INIT_FETCH, "review finding 3; -C form", dest="/tmp/ip2"),
    deny("rev-fetch-absolute-lane-path",
         "cd /tmp/ip3 && git init -q && git fetch /Users/jay/apps/lanes/DealDex/claude-x HEAD",
         INIT_FETCH, "review finding 3; an absolute lane path", dest="/tmp/ip3"),
)

_COMMIT_HEREDOC = (
    'git commit -m "$(cat <<\'EOF\'\n'
    "fix(guard): block git clone https://github.com/Simple-With-Us/DealDex.git /tmp/dd\n"
    "\n"
    "It doesn't allow `cd /tmp && git clone ...` (or a worktree add into /tmp) any more.\n"
    "\n"
    "Co-Authored-By: Claude <noreply@anthropic.com>\n"
    "EOF\n"
    ')"'
)
_PR_HEREDOC = (
    "gh pr create --title \"guard: no fleet checkouts in temp\" --body \"$(cat <<'EOF'\n"
    "## Summary\n"
    "- denies `git -C ~/Code/BotFleet worktree add /tmp/x` and `gh repo clone Simple-With-Us/HogHunter /tmp/hh`\n"
    "- it's a PreToolUse hook (fails open)\n"
    "1) clone, 2) worktree, 3) archive\n"
    "EOF\n"
    ')"'
)

ALLOW_FIXTURES: tuple[GuardFixture, ...] = (
    # ---- the owner's list
    allow("spec-scratch-clone",
          "git clone " + _DD + " " + SCRATCH + "/dd",
          "spec: the harness scratchpad (payload scratchpad_dir)", scratchpad_dir=SCRATCH),
    allow("spec-init-test-repo",
          "d=$(mktemp -d) && git init $d && cd $d && git commit --allow-empty -m x",
          "spec: git init alone in temp"),
    allow("spec-third-party",
          "git clone https://github.com/someone/else.git /tmp/else", "spec"),
    allow("spec-status-temp-checkout",
          "git -C /tmp/dealdex-work status --short", "spec: reading an existing checkout"),
    allow("spec-worktree-remove",
          "git worktree remove --force /tmp/um-1595-wt", "spec: cleanup must stay possible",
          cwd="/Users/jay/Code/Usage-Monitor"),
    allow("spec-rm-rf", "rm -rf /tmp/bf898 /tmp/bf898.tgz", "spec"),
    allow("spec-lane-worktree",
          "git -C ~/Code/BotFleet worktree add -b claude/x ~/apps/lanes/BotFleet/claude-x origin/main", "spec"),
    allow("spec-lane-clone",
          "git clone https://github.com/Simple-With-Us/DealDex.git ~/apps/lanes/DealDex/claude-foo", "spec"),
    allow("spec-echo", "echo cloning into /tmp is forbidden", "spec"),
    allow("spec-grep", "grep -rn 'git clone' /tmp/notes.txt", "spec"),
    allow("spec-curl-non-fleet", "curl -sL https://example.com/x.tgz -o /tmp/x.tgz", "spec"),
    allow("spec-ls-status", "ls /tmp && git status", "spec"),

    # ---- the harness scratchpad
    allow("scratch-generic-prefix-cd",
          "cd /private/tmp/claude-501/-Users-jay-Code-AI-Fleet-Coordinator/43eb9ff7/scratchpad && git clone " + _BF,
          "variant: claude-<uid> under a temp root without scratchpad_dir"),
    allow("scratch-tmp-alias-worktree",
          "git -C ~/Code/BotFleet worktree add /tmp/claude-501/proj/sess/scratchpad/bf origin/main",
          "variant: /tmp spelling of the scratchpad"),
    allow("scratch-heredoc-write",
          "cat > /private/tmp/claude-501/x/scratchpad/notes.md <<'EOF'\ngit clone " + _DD + " /tmp/dd\nEOF",
          "spec: file writes to the scratchpad"),
    allow("scratch-tmpdir-is-scratchpad",
          "d=$(mktemp -d) && git clone " + _DD + " $d/dd",
          "variant: sandboxed Claude Bash gets TMPDIR under claude-<uid>",
          env={"HOME": HOME, "TMPDIR": "/private/tmp/claude-501/", "AGENT_SEAT": "CLAUDE"}),

    # ---- cleanup and reads
    allow("worktree-list", "git -C ~/Code/BotFleet worktree list --porcelain", "variant"),
    allow("worktree-prune", "git -C ~/Code/Usage-Monitor worktree prune -v", "variant"),
    allow("worktree-remove-loop",
          "for w in /tmp/st4013 /tmp/st4013b; do git -C ~/Code/Socratic.Trade worktree remove --force $w; done",
          "variant: cleanup of sweep-bf-st4013"),
    allow("git-show-redirect", "git -C ~/Code/BotFleet show HEAD:package.json > /tmp/bf-package.json",
          "variant: one file, not a checkout"),
    allow("git-diff-redirect", "git -C ~/Code/DealDex diff origin/main > /tmp/dd.diff", "variant"),
    allow("bundle-create", "git -C ~/Code/CodeCaps bundle create /tmp/cc-161.bundle origin/main..feat/x",
          "sweep: /private/tmp/kodus-bundles and cc-161.bundle (a bundle is not a checkout)"),
    allow("archive-list-only", "git -C ~/Code/BotFleet archive HEAD | tar -t", "variant: tar lists, no extract"),
    allow("cat-diff-apply",
          "cat /tmp/BotFleet-PR894-permission-wire-id.diff | git -C ~/apps/lanes/BotFleet/claude-x apply",
          "sweep: diff beside /private/tmp/botfleet-pr894-review"),
    allow("log-grep", "git log --oneline | grep clone | head -5", "variant",
          cwd="/Users/jay/Code/BotFleet"),

    # ---- mentions only
    allow("echo-clone-command", "echo 'git clone https://github.com/Simple-With-Us/DealDex.git /tmp/dd'", "variant"),
    allow("comment-only", "git status # never git clone https://github.com/Simple-With-Us/DealDex.git /tmp/dd",
          "variant"),
    allow("comment-line-cd",
          "# cd /tmp\ngit clone " + _DD + " dd",
          "variant: a commented-out cd is not run", cwd="/Users/jay/apps/lanes/DealDex"),
    allow("printf-append-notes",
          "printf '%s\\n' 'git worktree add /tmp/x' >> ~/apps/lanes/AI-Fleet-Coordinator/claude-x/notes.md", "variant"),
    allow("python-print",
          "python3 -c \"print('git clone https://github.com/Simple-With-Us/DealDex.git /tmp/dd')\"", "variant"),
    allow("gh-pr-view-grep", "gh pr view 894 --json body -q .body | grep -n 'worktree add'", "variant"),
    allow("commit-heredoc-body", _COMMIT_HEREDOC,
          "Claude Code commit idiom; the body names a denied command",
          cwd="/Users/jay/Code/AI-Fleet-Coordinator"),
    allow("pr-body-heredoc", _PR_HEREDOC,
          "Claude Code PR idiom; the body has apostrophes, backticks and a bare )",
          cwd="/Users/jay/apps/lanes/AI-Fleet-Coordinator/claude-x"),
    allow("lane-heredoc-file",
          "cat > ~/apps/lanes/AI-Fleet-Coordinator/claude-x/notes.md <<EOF\ncd /tmp && git clone " + _DD + "\nEOF",
          "variant"),
    allow("single-quoted-var",
          "d=/tmp/x; git clone " + _DD + " '$d'",
          "variant: a single-quoted $d is a literal directory name", cwd="/Users/jay/apps/lanes/DealDex"),

    # ---- not temp, or not provably temp
    allow("redirect-log-lane-cwd",
          "git clone --depth 50 -b main https://github.com/Simple-With-Us/DealDex.git dealdex-work > /tmp/clone.log",
          "sweep tmp-evidence.txt (the log is in /tmp, the clone is not)", cwd="/Users/jay/apps/lanes/DealDex"),
    allow("pnpm-runner-store",
          "git clone git@github.com:jaywedgeworth22/Harness.git "
          "/home/runner/.local/share/pnpm/store/v10/tmp/_tmp_2373_73cd96a5c890493e34ca95f2d2bd2c7b",
          "sweep tmp-evidence.txt minimax/botfleet (a CI runner path, not a temp root)"),
    allow("downloads-clone", "git clone " + _DD + " ~/Downloads/dd",
          "variant: not temp (other tooling reports it)"),
    allow("unknown-dest-var", 'git clone ' + _DD + ' "$DEST"', "variant: unresolvable destination"),
    allow("unknown-cwd", 'cd "$WORK" && git clone ' + _DD, "variant: unresolvable cwd"),
    allow("subshell-scope",
          "(cd /tmp && ls) && git clone " + _DD + " dd",
          "variant: the subshell cd does not leak", cwd="/Users/jay/apps/lanes/DealDex"),
    allow("mktemp-under-review-root",
          "d=$(mktemp -d ~/apps/lanes/DealDex/review-pr-1.XXXX) && git clone " + _DD + " $d/repo",
          "variant: an explicit template outside temp"),
    allow("review-root-worktree",
          "git -C ~/Code/Congress.Trade worktree add --detach ~/apps/lanes/Congress.Trade/review-pr-2656 pr-2656", "variant"),
    allow("flat-lane-worktree",
          "git -C ~/Code/DealDex worktree add -b claude/fix ~/apps/dealdex-claude-fix origin/main", "variant"),
    allow("relative-lane-worktree",
          "git worktree add ../../apps/lanes/BotFleet/claude-rel -b claude/rel", "variant",
          cwd="/Users/jay/Code/BotFleet"),
    allow("lane-fetch-pull",
          "cd ~/apps/lanes/DealDex/claude-x && git fetch origin && git pull --rebase", "variant"),
    allow("gh-pr-checkout-lane", "cd ~/apps/lanes/BotFleet/claude-x && gh pr checkout 894", "variant"),
    allow("tarball-to-review-root",
          "curl -sL https://codeload.github.com/Simple-With-Us/BotFleet/tar.gz/main -o ~/apps/lanes/BotFleet/bf.tgz",
          "variant"),
    allow("archive-to-review-root",
          "git -C ~/Code/BotFleet archive HEAD | tar -x -C ~/apps/lanes/BotFleet/bf-tree", "variant"),

    # ---- temp, but not a fleet source
    allow("temp-repo-worktree", "git worktree add /tmp/scratch-wt", "variant: a throwaway repo in temp",
          cwd="/tmp/scratch-repo"),
    allow("init-tmp-plain",
          "mkdir -p /tmp/gittest && cd /tmp/gittest && git init -q && echo hi > a && git add a && git commit -qm init",
          "variant"),
    allow("mm-gittest",
          "T=$(mktemp -d /tmp/gittest.XXXXXX); cd $T && git init -q && git config user.email t@t && git config user.name t",
          "sweep /private/tmp/gittest.* (MiniMax throwaway repos)"),
    allow("remote-add-no-fetch",
          "cd /tmp/x && git init && git remote add origin " + _DD, "variant: no fetch, so no checkout"),
    allow("fetch-non-fleet-temp",
          "cd /tmp/x && git init && git remote add origin https://github.com/someone/else.git && git fetch origin",
          "variant"),
    allow("gh-third-party", "gh repo clone cli/cli /tmp/gh-cli", "variant"),
    allow("fork-same-name", "git clone https://github.com/someone/DealDex.git /tmp/dd-fork",
          "variant: a fork under another owner is third-party"),
    allow("release-asset",
          "curl -sL https://github.com/Simple-With-Us/homebrew-tap/releases/download/v1.0.0/tool.tar.gz -o /tmp/tool.tgz",
          "variant: a release asset is not a source tarball"),
    allow("curl-pipe-non-fleet", "curl -sL https://example.com/x.tgz | tar -xz -C /tmp/x", "variant"),
    allow("ssh-remote-clone",
          "ssh root@203.0.113.7 'git clone https://github.com/Simple-With-Us/DealDex.git /tmp/dd'",
          "variant: runs on another host"),

    # ---- self-cleaning scratch scripts
    allow("script-update-botfleet", "bash ~/apps/update-botfleet.sh --force",
          "spec: ~/apps/update-botfleet.sh run as a file"),
    allow("script-check-hetzner", "bash /Users/jay/apps/check-hetzner-cx43.sh",
          "spec: ~/apps/check-hetzner-cx43.sh run as a file"),
    allow("inline-hetzner-mktemp",
          'TMP="$(mktemp -d)"; trap \'rm -rf "$TMP"\' EXIT; ssh root@203.0.113.7 \'df -h\' > "$TMP/df.txt"',
          "check-hetzner-cx43.sh:34-35 pattern inline (no checkout; no trigger word)"),
    allow("inline-update-botfleet-unknown-checkout",
          'BOOTSTRAP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/botfleet-updater.XXXXXX")"; '
          'git -C "$BOTFLEET_CHECKOUT" archive "$BOOTSTRAP_COMMIT" -- scripts/update-botfleet-mac.mjs '
          '| tar -x -C "$BOOTSTRAP_DIR"',
          "update-botfleet.sh:259-300 inline: the source repo is an unresolvable variable"),
    allow("mktemp-nongit-scratch",
          "d=$(mktemp -d) && cp ~/Code/BotFleet/package.json $d/ && jq .version $d/package.json && rm -rf $d",
          "variant: non-git scratch (no trigger word)"),
    allow("mktemp-nongit-fetch-word",
          "d=$(mktemp -d) && curl -s https://example.com/fetch.json -o $d/fetch.json && rm -rf $d",
          "variant: non-git scratch that trips the keyword prefilter"),

    # ---- review 2026-10-07: named files are an extraction, not a checkout
    allow("rev-archive-pathspec-bootstrap",
          'B="$(mktemp -d)"; git -C ~/Code/BotFleet archive origin/main -- scripts/update-botfleet-mac.mjs '
          '| tar -x -C "$B" && node "$B/scripts/update-botfleet-mac.mjs" --check',
          "review finding 2; update-botfleet.sh:296 bootstrap inlined with a literal checkout"),
    allow("rev-archive-pathspec-no-dashdash",
          "git -C ~/Code/BotFleet archive --format=tar HEAD package.json scripts/x.mjs | tar -x -C /tmp/bf-files",
          "review finding 2; pathspecs without --"),
    allow("rev-archive-subdir-glob",
          "git -C ~/Code/BotFleet archive HEAD -- 'scripts/*.mjs' | tar -x -C /tmp/bf-scripts",
          "review finding 2; a glob below a named directory"),
    allow("rev-archive-pathspec-output-then-extract",
          "git -C ~/Code/DealDex archive -o ~/Downloads/dd-one.tar HEAD -- package.json && "
          "tar -xf ~/Downloads/dd-one.tar -C /tmp/dd-one",
          "review finding 2; a file archive written outside temp, extracted into it"),
    allow("rev-archive-unknown-pathspec",
          'git -C ~/Code/BotFleet archive HEAD -- "$F" | tar -x -C /tmp/bf-f',
          "review finding 2; an unknown pathspec counts as a file (fail open)"),
    allow("rev-checkout-index-named-file",
          "git -C ~/Code/BotFleet checkout-index --prefix=/tmp/one/ package.json",
          "review finding 2"),
    allow("rev-checkout-index-named-files-dashdash",
          "git -C ~/Code/BotFleet checkout-index -f --prefix /tmp/two/ -- package.json README.md",
          "review finding 2; separate --prefix value and --"),
    allow("rev-git-show-file",
          "git -C ~/Code/BotFleet show origin/main:scripts/update-botfleet-mac.mjs > /tmp/updater.mjs",
          "review finding 2; the workaround the reviewer named"),
    allow("rev-pull-non-fleet-path",
          "cd /tmp/x && git init && git pull /opt/vendor/lib main",
          "review finding 3; a local path outside the fleet roots"),
    allow("rev-pull-in-lane-from-integration-tree",
          "cd ~/apps/lanes/DealDex/claude-x && git pull ~/Code/DealDex main",
          "review finding 3; the destination is a lane, not temp"),
)

FIXTURES: tuple[GuardFixture, ...] = DENY_FIXTURES + ALLOW_FIXTURES
