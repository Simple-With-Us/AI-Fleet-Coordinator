# fleet_knobs

How the fleet's short-lived jobs (LaunchAgents, hooks and CLIs from the open-loops plan)
read their modes, caps and flags from Infisical.  Stdlib only.  The contract and the key
inventory are in `INFISICAL.md` § Fleet knobs;  this file is the developer view.

```python
from fleet_knobs import knobs

mode = knobs.get("CLAIM_EXPIRY_MODE", "dry-run", "enum",
                 local=cfg.get("mode"),            # a job's own config file:  can only narrow
                 stop=os.path.exists(STOP_FILE))   # a STOP file:  forces the narrowest value
caps = knobs.get("CLAIM_EXPIRY_CAPS")              # {"releases_per_run": 20, ...}
report["knobs"] = knobs.report()                   # names, sources, "stale";  never a value

key = knobs.get("FLEET_FACTS_SIGNING_KEY")         # raises SecretUnavailable:  stop the job
```

## Rules a job gets for free

- **One fetch per process**, on the first read, through the low-level scoped REST reader in
  `fleet_secrets/core.py`, for prod only.  A failed fetch is not retried;
  `knobs.refresh()` forgets it for a long-running process.
- **Scoped service token.**  Every automated reader requires the per-use-case
  `FLEET_KNOBS_INFISICAL_TOKEN`, passed explicitly to the verifier.  Metadata must prove the
  independently expected project, exact prod folder and read-only permission before named
  values are fetched.  Missing or malformed tokens fail closed; there is no machine-identity
  or CLI-session fallback.  One 8 second deadline bounds metadata and all named requests.
- **Negative cache.**  A failed fetch writes `fetch-failed.json` (0600, timestamp and reason
  token) beside the copy, except reasons `service-token-required` and
  `scoped-reader-unavailable`, which belong to this one process (no usable reader token, or no
  reader code) and write no marker, so a sibling that holds the token can still fetch;  for 60 seconds other processes skip the fetch and read the copy
  (`fetch_reason` `recent-<token>`).  A good fetch clears it;  `refresh()` ignores it.
- **Last-known-good.**  A good fetch refreshes `~/.local/state/fleet-knobs/lkg.json`
  (dir 0700, file 0600) with the registered non-secret knobs.  If Infisical cannot be reached
  the job reads it, `knobs.report()["stale"]` is true, and a copy older than 48 hours (or any
  untrusted file) is ignored.  With no usable copy the registry default is returned.  A read of
  a non-secret knob never raises.  The copy matches what a live read answers:  an unset knob
  and a value that fails validation are both dropped from it.  An export with none of the
  registered names is a failed fetch (`fetch_reason` is `empty`) while the copy still has
  knobs, so a blank answer cannot erase it.
- **Secrets are never cached** to disk, defaulted or exported.  An unreachable, unset or
  malformed secret raises `SecretUnavailable`, and the job must stop.
- **Local can only narrow.**  `local=` and `stop=` can lower a cap, shrink a list, force an
  enum toward its first choice or set a flag to its safe value.  A value that would widen is
  ignored (`local_ignored` in the report).  A default you pass can only narrow the registry
  default too.
- **Registered names only.**  Anything else raises `KnobError` before any fetch.
- **Names only in logs and reports.**

## Kinds

`str`, `int`, `bool`, `enum` (choices ordered most conservative first), `list` (a tuple of
strings, comma, space or newline separated, or a JSON array) and `caps` (a dict of names to
counts, `a=1,b=2` or a JSON object).

## Adding a knob

1. Add a `Spec` to `registry.py` (owner component, kind, default, how a local file may
   narrow it, its group and plain label, and the machines and jobs that read it:  `hosts`
   and `consumers`, or `unknown` and none when nothing here reads it yet).  Then also run
   `python3 -m fleet_knobs inventory --write-json fleet_knobs/registry.json`, which Muster's
   Controls tab reads;  a test fails when it drifts.
2. From `scripts/`:  `python3 -m fleet_knobs inventory --write ../INFISICAL.md`.  CI fails
   when the registry and the INFISICAL.md block differ.
3. Read it with `knobs.get`.  Never read the environment or a local file for a mode.

## CLI (run from `scripts/`)

```
python3 -m fleet_knobs status [--json]     source and set or unset per knob;  secrets never shown
python3 -m fleet_knobs status --json --values
                                           the same plus non-secret values, write_token present
                                           or missing (never the token) and checked_epoch;  Muster
python3 -m fleet_knobs get NAME            one non-secret value (stale goes to stderr)
python3 -m fleet_knobs inventory [--check|--write FILE]
python3 -m fleet_knobs inventory [--json|--check-json|--write-json FILE]
                                           fleet_knobs/registry.json, every field Muster shows
python3 -m fleet_knobs set NAME VALUE (--plan|--yes) [--json]
python3 -m fleet_knobs unset NAME (--plan|--yes) [--json]
```

`status` exits 3 when Infisical was unreachable.  It is the check after the first values are
set in Infisical.

`set` (`setter.py`) is the one write path, and Muster's Controls tab calls it.  It refuses an
unknown name, any secret and any value the knob's `Spec` rejects, through the same
`values.parse` the readers use, and writes the canonical text.  `--plan` shows the old and new
value and the CLI argv and writes nothing, and it refuses (exit 2) when the write could not run
in this process, such as when no write token is present, so a plan that succeeds is one a
`--yes` here could carry out; `--yes` runs `infisical secrets set NAME=VALUE
--env=prod --path=/ --projectId=<id>` with a write-capable service token in the child's
`INFISICAL_TOKEN` only.  The token comes only from `FLEET_KNOBS_WRITE_TOKEN`, with its exact
project, prod root folder and read/write grant verified first; without one the answer is `no write token`. 
After a write the last-known-good copy is refreshed (or, when the re-read fails, patched for
that key with its age kept), and every `--yes` appends a row to
`~/.agent-sync/listener/knobs.jsonl` (0600): time, seat, key, old and new value, result.  Exit 0
planned or done, 1 failed, 2 refused.

## Packaging

A stable copy run with `python3 -I` must carry this package (`scripts/fleet_knobs/`, without
`tests/`) plus the existing low-level verifier files `fleet_secrets/__init__.py` and
`fleet_secrets/core.py`.  Both packages use only the standard library.  The fleet-facts and
needs-owner installers copy those two verifier files explicitly and remove stale package
extras; they do not install a fleet-secrets executable, `cli.py` or `__main__.py`.  Their
installed `python3 -I` tests exercise real metadata and named-value verification through a
loopback fixture, including successful reads and rejection before values for mismatched grants.

## Tests

```
cd scripts && python3 -m unittest discover -s fleet_knobs/tests -t . -v
```

Tests use injected helper fixtures or a loopback HTTP API with synthetic tokens and values,
point `HOME` at a temporary directory and disable CLI fallbacks.  They do not read real
credential stores, invoke the real Infisical CLI or access live Infisical.
