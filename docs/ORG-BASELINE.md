# Org Baseline: Branch Protection, Org Secrets, and Infisical as Source of Truth

**Owner request (2026-10-04):** org-wide branch protection minimums; use
organization secrets/variables instead of per-repo copies for anything shared by
many repos; make sure every repo has the access it needs; and have GitHub Actions
pull an app's secrets from that app's own Infisical project so Infisical is the
sole source of truth to the fullest extent possible.

Board: `14e07c4d34c145d780a2570fd4badac1` · Seat: MINIMAX · Branch:
`minimax/org-baseline`

---

## 1. What the audit found

`scripts/audit-org-security.py` is the reproducible version of this section.
Run it any time; it is read-only and prints names, never values.

### Branch protection

All **16 public repos** carry an active branch ruleset that blocks deletion,
blocks force-push, and requires a pull request.  Two of them
(`Socratic-Trade`, `congress-trading-shared`) were compliant but named
`main-protection` instead of the fleet name `default-main-protection`; both are
now renamed, and both keep their own stricter required checks.

Three repos — `Fleet-OPS`, `Kodus-Config`, `demo-repository` — are **private**,
and GitHub answers every ruleset and branch-protection request on them with:

> Upgrade to GitHub Pro or make this repository public to enable this feature.

That is a billing limit, not a configuration gap.  No amount of automation
closes it.  The auditor reports it as `PLAN-LIMIT` rather than as a failure, so
it stays visible without crying wolf.

### The org-wide ruleset does not exist on this plan

GitHub documents org-wide rulesets as a feature for *"customers on GitHub Team
and GitHub Enterprise plans"*.  This org is on Free — which is also why the
three private repos have no protection.  So there is **no server-side object to
hold "the global minimum"**, and no setting to flip.

That reframes the request: on Free, the baseline can only be held as *code that
audits reality and fails on drift*, plus a sync that repairs it.  That is what
`audit-org-security.py` and `apply-github-ruleset.py --all` now do.

### Secret duplication

| Copies | Secret | Verdict |
|---|---|---|
| 8 | `SENTRY_AUTH_TOKEN` | real credential, shared — candidate for one org secret, or per-app Infisical |
| 8 | `SENTRY_FLEET_DSN` | **not a credential** — should be a variable |
| 7 / 6 | `IOS_DIST_P12_BASE64` / `_PASSWORD` | per-app signing identity, not shared |
| 5 | `INFISICAL_UNIVERSAL_AUTH_CLIENT_ID` / `_SECRET` | **the bootstrap credential** — one org secret |
| 5 | `INFISICAL_PROJECT_ID` | an identifier, not a secret — should be a variable |
| 5 | `INFISICAL_CLIENT_ID` / `_SECRET` | duplicate of the universal-auth identity |
| 4 | `DEEPSEEK_API_KEY` | shared |
| 3 | `GH_PAT` | shared |
| 2 | `SENTRY_DSN` | **not a credential** — should be a variable |

---

## 2. The Infisical design

### The chicken-and-egg you suspected

You wrote:

> I know that sometimes need like Sentry DSN before the app has time to get
> secrets from infisical (but maybe that is wrong)

**Your instinct is right that the problem exists, but the reason is wrong, and
the fix is much better than working around it.**

Sentry's own documentation says:

> DSNs are safe to keep public because they only allow submission of new events
> and related event data; they do not allow read access to any information.

and the secret half of a DSN is *"optional and effectively deprecated"*.  A DSN
is an **identifier**, not a credential.  The Infisical data confirms the
misclassification has spread: `Autorotate` and `ContactLogo` each store four DSN
keys (`SENTRY_DSN`, `SENTRY_DSN_MACOS`, `SENTRY_DSN_WEB`, `VITE_SENTRY_DSN`) as
Infisical *secrets*, and `shared-at-ct` holds two more as
`PERSONAL_SITE_SENTRY_DSN`.

So the bootstrap dependency is not real.  It exists **only because DSNs were
filed as secrets**.  Store a DSN as a GitHub **variable** and the problem
disappears: variables need no secret store, no Infisical round trip, and no
trust chain.  The app reads `vars.SENTRY_FLEET_DSN` at build time.

This is why 10 misfiled DSN copies can be demoted from secret to variable with
**zero** risk of breaking anything.

### The real circular dependency

There is a worse one, and it is not theoretical.  The `usage-monitor` Infisical
project stores Infisical's own login credentials:

```
INFISICAL_AUTOMATION_CLIENT_ID      INFISICAL_ST_CLIENT_ID
INFISICAL_AUTOMATION_CLIENT_SECRET  INFISICAL_ST_CLIENT_SECRET
INFISICAL_SHARED_CLIENT_ID          INFISICAL_CT_CLIENT_ID
INFISICAL_SHARED_CLIENT_SECRET      INFISICAL_CT_CLIENT_SECRET
INFISICAL_ST_PROJECT_ID             INFISICAL_CT_PROJECT_ID
INFISICAL_SHARED_PROJECT_ID         INFISICAL_UM_PROJECT_ID
```

The key to the lockbox is inside the lockbox.  It only resolves because a
**duplicate of that key lives in five repos' GitHub secrets**, which is exactly
the arrangement you are asking to get rid of.

### Target shape

```
                 ┌──────────────────────────────────────┐
                 │  Infisical — one project per app      │
                 │  the ONLY home for real credentials  │
                 └──────────────────────────────────────┘
                            ▲                    ▲
      fetch at job start    │                    │
                 ┌──────────┴─────────┐   ┌──────┴───────────────┐
                 │ 1 org secret       │   │  repo variables      │
                 │ INFISICAL_UNI-     │   │  INFISICAL_PROJECT_ID│
                 │ VERSAL_AUTH_       │   │  SENTRY_FLEET_DSN    │
                 │ CLIENT_ID/SECRET   │   │  (identifiers, not    │
                 └────────────────────┘   │   credentials)        │
                 the only bootstrap      └──────────────────────┘
                 credential
```

Per repo, GitHub then holds **one** secret (the shared Infisical identity) plus
a few non-secret variables.  Everything real comes from Infisical at job start.

```yaml
- uses: ./.github/actions/infisical-env
  with:
    project: ${{ vars.INFISICAL_PROJECT_ID }}
    keys: SENTRY_AUTH_TOKEN,DEEPSEEK_API_KEY
```

### Why the per-app split matters

"Use organization secrets for shared ones" and "let each app read its own
Infisical" are the same decision, not competing ones.  Per-app keys
(`ASC_ISSUER_ID`, `IOS_DIST_P12_BASE64`, `ADMIN_TOKEN`) are **not shared** — they
must not become org secrets, because an org secret with `visibility: all` is
readable by all 16 public repos.  Only the Infisical identity is genuinely
shared, so it is the only thing that belongs at org level.

---

## 3. Repo → Infisical project map (verified)

Resolved live from the Infisical API on 2026-10-04.  Key *names* were read to
confirm each mapping points at a real project; values were never read.

| Repo | Infisical project | prod keys | State |
|---|---|---|---|
| BotFleet | `bot-fleet-g-qx-3` | 40 | healthy |
| Socratic-Trade | `socratic-trade` | 280+ | healthy |
| Congress.Trade | `congress-trade` | 190+ | healthy |
| Usage-Monitor | `usage-monitor` | 190+ | healthy, **stores Infisical's own credentials** |
| DealDex | `deal-dex-a6-p6` | 6 | partial |
| congress-trading-shared | `shared-at-ct` | 100+ | healthy |
| Autorotate | `autorotate-em-mu` | 4 | DSNs only |
| ContactLogo | `contact-logo-lf-j8` | 4 | DSNs only |
| HogHunter | `hog-hunter-w-kmz` | **0** | **empty — GitHub is still the only copy** |
| CodeCaps | `code-caps-qey-g` | **0** | **empty** |
| Personal-Site | `personal-site-miyr` | **0** | **empty** |
| Clutch | `clutch` | **0** | **empty** |
| AI-Fleet-Coordinator | `ai-fleet-coordinator-ox-z2` | **0** | **empty** |
| Fleet-OPS | `fleet-ops-mwb-6` | **0** | empty |
| Fleet Secrets | `fleet-secrets-ek1-v` | **0** | empty |
| Certificate Manager | `cert-manager-w-ppb` | **0** | empty |

An empty project means the repo's GitHub secrets are still the only copy, so
those repos cannot be migrated to Infisical-sourced CI until the project is
backfilled first.  Migration order must start with the backfill, not with the
workflow change.

---

## 4. Blocked: no org-admin Actions credential

Org-level secrets and variables cannot be created with any credential on this
Mac.  Both candidates fail, for different reasons:

- the logged-in `gh` token has scopes `gist, read:org, repo, workflow` — no
  `admin:org`, so every org endpoint returns 403;
- `GITHUB_ADMIN_PAT` is a **fine-grained** PAT, and the org policy rejects it:
  *"The 'Simple-With-Us' organization forbids access via a fine-grained personal
  access tokens if the token's lifetime is greater than 366 days."*

Note the private-repo rule and the DSN question are unaffected by this: repo-level
variables and rulesets already work, and the DSNs need no org scope to demote.

**One of these unblocks it** — both need a browser consent only Jay can give:

```bash
# option A — add admin:org to the gh CLI token
gh auth refresh -h github.com -s admin:org

# option B — shorten the existing fine-grained PAT's lifetime to <= 366 days
#   https://github.com/settings/personal-access-tokens/17683090
```

Option A is faster and needs no new secret.  Either way the fix belongs in
Infisical as `INFISICAL_GITHUB_ADMIN_CLIENT_ID` / `_SECRET` so the fleet can mint
its own, rather than in a hand-managed local file.

---

## 5. Migration order

Each step is independently reversible, and no step deletes a secret that a live
workflow still reads.

1. **Add** the org Infisical identity (or the per-repo copy) — additive.
2. **Add** `INFISICAL_PROJECT_ID` and the DSNs as repo **variables** — additive;
   the secret copies keep working untouched.
3. **Add** the `infisical-env` composite action to one low-risk repo.
4. Migrate workflows to read `vars.*` first, `secrets.*` as fallback:
   `${{ vars.SENTRY_FLEET_DSN || secrets.SENTRY_FLEET_DSN }}`.
5. Backfill empty Infisical projects (HogHunter, CodeCaps, Personal-Site, …).
6. **Only then** delete the redundant repo secrets.
7. Remove the Infisical credentials from the `usage-monitor` project once the
   org secret exists, breaking the circular dependency.

Steps 1–4 are safe to run unattended.  Step 6 is the first destructive one and
should be gated on a green CI run that used the new path.

---

## 6. Two traps this work hit, worth not re-learning

**`PATCH` is not implemented on the repository-rulesets endpoint.**  It returns
404; only `PUT` works, and `PUT` is a *full replace*.  So renaming a ruleset by
writing the baseline body over it would have silently deleted the
`required_status_checks` rule — dropping `verify`+`gitleaks` from
Socratic-Trade and `verify` from congress-trading-shared, and letting PRs merge
on red CI.  `apply-github-ruleset.py` now does a read-modify-write and carries
the live rules through, changing only `name`.

**An auditor that cries wolf gets ignored.**  Judging repos by ruleset *name*
would have reported Socratic-Trade and congress-trading-shared as "missing"
protection when both were fully protected.  The auditor judges the *rules
present* and reports naming as an advisory, so a real `MISSING` still means
something.
