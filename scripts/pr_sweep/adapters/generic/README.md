# pr-sweep on any platform

Recipes for running the sweep from Codex, Cursor, Grok and the other headless CLIs, and from chat-only seats.  The worker rules are not in this folder: they live in `scripts/pr_sweep/rules.md`, and `pr-sweep prompt` prints them filled in for one PR and your seat.  Everything here is a way to get that prompt to an agent.

Set up once per sweep, from a fresh checkout of `origin/main` (never `~/Code`):

```bash
export AGENT_SEAT=CODEX                                   # your own seat; workers inherit it
d=$(~/apps/lane new AI-Fleet-Coordinator pr-sweep-run) && cd "$d"
PS="$PWD/scripts/pr-sweep"
$PS inventory --fleet-only | head -40                     # what is open
$PS plan                                                  # skip/merge, stacks, chains per repo
$PS needs-work                                            # which PRs need a worker right now, and why
```

## The whole sweep, one command

`pr-sweep run` does the plan, the per-PR status check, the freshness guard, the launch, the read-back, repair rounds, the audit, `lane retire` after a merge and the deploy checks.  Always look at the dry run first.

```bash
$PS run --platform codex --dry-run                         # decisions and the exact command per PR; launches nothing
$PS run --platform codex --apply --auto-approve            # Codex: codex exec, prompt on stdin
$PS run --platform cursor --apply --auto-approve           # Cursor: cursor-agent -p --output-format text <prompt>
$PS run --platform grok --apply --auto-approve             # Grok: grok --prompt-file <0600 file>
$PS run --platform antigravity --apply                     # agy -p <prompt>
$PS run --platform minimax --apply                         # mcode exec --input - (stdin)
$PS run --platform muse --apply                            # muse exec <prompt>
$PS run --platform fx --apply                              # fx ask <prompt>
$PS run --platform opencode --apply                        # opencode run <prompt>
$PS run --platform codex --apply Simple-With-Us/BotFleet#1219   # one PR only
$PS run --platform codex --apply --repos BotFleet,DealDex --max-parallel 2
```

`--auto-approve` adds the platform's own auto-approve flags from `platforms.json` (for example Codex's `--dangerously-bypass-approvals-and-sandbox`); without it a headless CLI may stop at its first permission prompt.  It is your call, made per run.  `--agent-cmd 'my-cli run --flag {prompt}'` uses any other command that takes the prompt as `{prompt}`, `{prompt_file}` or (with no placeholder and `stdin: true` in a template) on standard input.

## One PR by hand

```bash
$PS prompt Simple-With-Us/BotFleet#1219 --platform codex | codex exec
cursor-agent -p --force "$($PS prompt Simple-With-Us/BotFleet#1219 --platform cursor)"
$PS prompt Simple-With-Us/BotFleet#1219 --platform grok > ~/apps/scratch/grok/pr.txt && grok --prompt-file ~/apps/scratch/grok/pr.txt
```

Afterwards check what is true, not what the agent said: `$PS status Simple-With-Us/BotFleet#1219`, and `$PS audit Simple-With-Us/BotFleet#1219 --since <start time>`.

## Chat-only seats (Clutch, Grok on web or iOS, Jet)

See `chat-prompt.md`.  A chat seat needs a terminal tool to do the git work; without one it can only review.
