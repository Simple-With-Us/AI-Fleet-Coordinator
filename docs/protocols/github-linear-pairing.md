# GitHub and Linear Pairing

Every fleet GitHub issue has exactly one Linear twin, linked, and the two agree on open or closed.  Linear is the system of record:  seats claim and close the Linear issue, and the GitHub copy follows.  Owner ask 2026-10-17;  tool and runbook AFC-339.

## Contents

- [The Rule](#the-rule)
- [Where It Stood](#where-it-stood)
- [How Pairs Stay 1:1](#how-pairs-stay-11)
- [The Tool](#the-tool)
- [Write Phases](#write-phases)
- [Owner-Only Settings](#owner-only-settings)
- [Noise](#noise)
- [Never](#never)
- [Changelog](#changelog)

## The Rule

- **One twin.**  A GitHub issue in a paired repo has one Linear issue, and that Linear issue stands for one GitHub issue.
- **Linked.**  The twin carries a GitHub attachment for the issue (`attachmentLinkGitHubIssue`).  The import footer `<!-- migrated-from: gh:Owner/Repo#N -->` finds twins that predate linking, but it is not a link.
- **Linear wins.**  When the two disagree on open or closed, the Linear state is right:  a closed twin closes the GitHub issue (`completed` for Done or Deployed, `not_planned` for Canceled, Won't Fix or Duplicate).  A GitHub issue closed while its twin is open is reported, never acted on automatically.
- **Both stay open while the work is open.**  The cutover plan's blanket close of every GitHub copy (step 4a in [linear-migration.md](linear-migration.md)) never ran and is replaced by this rule.
- **Paired repos** are the `REPOS` table in `scripts/gh_linear_pairing.py`:  every app repo in `fleet-apps.json` except fleetlink-legacy (gone), plus Kodus-Config and homebrew-tap, whose twins live in AFC.

## Where It Stood

Read on Sun, Oct 18, 2026, with `scripts/gh_linear_pairing.py plan` (read-only):

| Measure | Count |
| --- | --- |
| GitHub issues in the 18 paired repos (no PRs) | 5,403 (1,288 open) |
| Pairs by import footer | 5,118, none of them linked |
| GitHub issues with no twin | 285:  272 merge-shepherd duplicates and 13 real issues |
| GitHub open, Linear closed | 150:  139 effort-board mirrors whose twin is a Duplicate, 11 real |
| GitHub closed, Linear open | 0 |
| Conflicts (two twins, or a twin with two issues) | 0 |
| Labelled Linear issues with no GitHub issue | 5 (CC-164, CC-149, CC-152, BF-1133, FL-12) |

## How Pairs Stay 1:1

1. **Existing pairs are linked by the tool.**  Linear's own GitHub Issues importer would create new Linear issues and duplicate every twin, so it is never used for these repos.
2. **New GitHub issues arrive by Linear's one-way Issues Sync**, one link per repo to its team ([Owner-Only Settings](#owner-only-settings)).  One-way sync creates a linked Linear issue for each new GitHub issue and carries comments and open or closed changes across.
3. **Two-way sync is never turned on.**  Every new Linear issue in a two-way team would be published as a GitHub issue, and 16 of the 17 org repos are public.
4. **The tool reports drift** (`report --fail-on missing_twin,conflict,gh_closed_linear_open`) and repairs only the classes the owner has approved.

## The Tool

`scripts/gh_linear_pairing.py`, standard library only.  Reads need `AGENT_SEAT` (Linear, through the board CLI's client and the seat's credentials) and `GITHUB_TOKEN`.

```bash
python3 scripts/gh_linear_pairing.py report                      # read-only:  totals and drift classes
python3 scripts/gh_linear_pairing.py plan --out DIR              # read-only:  report.json plus plan-*.jsonl
python3 scripts/gh_linear_pairing.py apply DIR/plan-link.jsonl --limit 10      # writes, ledgered
```

- **Drift classes:**  `missing_twin`, `conflict`, `gh_closed_linear_open`, `gh_open_linear_closed`, `unlinked`, `orphan_linear`, `team_mismatch` (the import's deliberate exceptions are allowed in `TEAM_EXCEPTIONS`) and `footer_disagrees`.
- **Plans:**  `plan-link` (real open pairs first, noise last), `plan-create` (real GitHub issues with no twin), `plan-close-github` (parity closes, then shepherd duplicates) and `plan-cancel-linear` (open twins of shepherd duplicates).
- **Apply** re-checks live state before every operation (`attachmentsForURL` before a link or create, the GitHub state before a close, the Linear state before a cancel), writes `<plan>.ledger.jsonl` beside the plan so a rerun skips what landed, and paces itself (`--per-hour`, default 400).  `--only-real` skips automation noise.
- **Gates:**  Linear writes go through `board api mutation`, so they need `LINEAR_BOARD_WRITES=1` and carry the seat's tags and an idempotency key.  GitHub writes also need `PAIRING_GITHUB_WRITES=1`.  Every link uses a deterministic attachment id derived from the GitHub `node_id`, so a retried link cannot add a second attachment.
- **Tests:**  `cd scripts && python3 -m unittest test_gh_linear_pairing` (offline, fakes only;  CI runs them).

## Write Phases

Each phase runs only on the owner's go, from a fresh `plan`, in small batches first.

| Phase | What | Before it runs |
| --- | --- | --- |
| P1 Pilot | Link one throwaway pair in private Kodus-Config, then one real pair of each kind | Owner approval.  Record what linking does:  `syncedWith`, field sync and its direction, linkback comments, behaviour on closed, archived and user-owned repos. |
| P2 Link | `plan-link`, 10, then 100, then the rest | Pilot result read;  linkbacks set for the bulk window |
| P3 Create | `plan-create` (13) | Owner approval |
| P4 Close and cancel | `plan-close-github` (150 parity, then shepherd), `plan-cancel-linear` (625) | The shepherd fix ([Socratic-Trade#4849](https://github.com/Simple-With-Us/Socratic-Trade/pull/4849)) merged and the Mac's host copy refreshed |
| P5 Sync | Owner adds the one-way links;  the report runs on a schedule | Owner approval |

## Owner-Only Settings

No seat can do these:  the Linear apps are not workspace admins.

1. **Look first.**  linear.app/simple-with-us/settings/integrations/github, section **GitHub Issues**:  note any existing repo-to-team links and their mode, and the **Linkbacks** setting.  Under Settings › Administration › Import & Export › GitHub, check that no import job is pending, and do not resume one.
2. **Linkbacks for the bulk link.**  If the pilot shows that linking an issue posts a comment on GitHub, turn linkbacks off for the bulk window (or keep descriptions off):  otherwise about 5,000 comments land on public repos.
3. **One-way links,** after the shepherd fix ships:  **+**, pick the repo, the team and **One-way**, with no label filter.  Socratic-Trade→ST, Congress.Trade→CT, Usage-Monitor→UM, congress-trading-shared→CTS, DealDex→DD, AI-Fleet-Coordinator→AFC, Personal-Site→PS, ContactLogo→CL, BotFleet→BF, HogHunter→HH, Clutch→C, CodeCaps→CC, FleetLink→FL, Simple-With-Us→SWU, and Fleet-OPS→OPS if Linear offers a repo owned by a user account.  Skip Autorotate (archived).

## Noise

Automation output is paired when it already has a twin, but never gets a new one.

- **Merge shepherd status** (Socratic-Trade):  one issue every 30 minutes from a stale host copy of `scripts/merge-shepherd.sh`, 897 open.  Fixed in ST-4553;  every open copy is closed `not_planned` and its open twin is canceled.
- **Effort-board mirrors** (`effort-board` label, `github-actions[bot]`):  the mirror workflows are off.  A mirror whose twin is closed is closed with it;  one whose twin is open stays until the twin is triaged.
- **Renovate's Dependency Dashboard:**  kept open on GitHub, as Renovate expects.

## Never

- Linear's GitHub Issues importer, or two-way sync, for these repos.
- Deleting a GitHub attachment from a twin:  that ends Linear's sync for it.
- Settings writes through the API (`integrationUpdate`, `integrationSettingsUpdate`) or `issueExternalSyncDisable`.
- A bulk write without a fresh `plan`, or past its approved phase.

## Changelog

| Version | When | Who | What |
| --- | --- | --- | --- |
| v1 | 2026-10-18 | Claude | First version (AFC-339):  the rule, the starting counts, the tool, the write phases, the owner-only settings, and the noise policy. |
