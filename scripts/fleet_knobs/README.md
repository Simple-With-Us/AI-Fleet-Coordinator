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

- **One fetch per process**, on the first read, through the installed `infisical` CLI with
  `--env=prod` (prod is the only environment).  A failed fetch is not retried;
  `knobs.refresh()` forgets it for a long-running process.
- **Auth like fleet-secrets.**  `INFISICAL_TOKEN` if set, else (on Linux) the automation pair
  `INFISICAL_AUTOMATION_CLIENT_ID` / `INFISICAL_AUTOMATION_CLIENT_SECRET` in the environment,
  else the machine identity in the login Keychain, then the System keychain,
  all through fleet-secrets' helper when it can be found, else the CLI session.  Needed under
  pm2 (a system LaunchDaemon, no login Keychain) and on the Hetzner server (no keychain at
  all; a half-set pair is reason `automation-env-incomplete:missing-<var>` (the variable name in lowercase, as the failure marker keeps it) ``, not a keychain
  fault).  The
  token is only ever in the CLI child's environment, and the automation pair never reaches
  the export child.  The whole fetch (keychain
  lookups, login and export) shares one 8 second budget;  running out is `timeout`.
- **Negative cache.**  A failed fetch writes `fetch-failed.json` (0600, timestamp and reason
  token) beside the copy;  for 60 seconds other processes skip the fetch and read the copy
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

1. Add a `Spec` to `registry.py` (owner component, kind, default, and how a local file may
   narrow it).
2. From `scripts/`:  `python3 -m fleet_knobs inventory --write ../INFISICAL.md`.  CI fails
   when the registry and the INFISICAL.md block differ.
3. Read it with `knobs.get`.  Never read the environment or a local file for a mode.

## CLI (run from `scripts/`)

```
python3 -m fleet_knobs status [--json]     source and set or unset per knob;  secrets never shown
python3 -m fleet_knobs get NAME            one non-secret value (stale goes to stderr)
python3 -m fleet_knobs inventory [--check|--write FILE]
python3 -m fleet_knobs set NAME VALUE (--plan|--yes) [--json]
```

`status` exits 3 when Infisical was unreachable.  It is the check after the first values are
set in Infisical.

`set` (`setter.py`) is the one write path, and Muster's Controls tab calls it.  It refuses an
unknown name, any secret and any value the knob's `Spec` rejects, through the same
`values.parse` the readers use, and writes the canonical text.  `--plan` shows the old and new
value and the CLI argv and writes nothing; `--yes` runs `infisical secrets set NAME=VALUE
--env=prod --path=/ --projectId=<id>` with a write-capable service token in the child's
`INFISICAL_TOKEN` only.  The token comes from `FLEET_KNOBS_WRITE_TOKEN` or the Mac keychain item
`fleet-knobs-write` (account `INFISICAL_TOKEN`); without one the answer is `no write token`. 
After a write the last-known-good copy is refreshed (or, when the re-read fails, patched for
that key with its age kept), and every `--yes` appends a row to
`~/.agent-sync/listener/knobs.jsonl` (0600): time, seat, key, old and new value, result.  Exit 0
planned or done, 1 failed, 2 refused.

## Packaging

A stable copy run with `python3 -I` must carry this package (`scripts/fleet_knobs/`, without
`tests/`).  It imports nothing outside the standard library, so no other fleet package comes
with it, and `tests/test_inventory_cli.py` runs a bare copy under `python3 -I` to keep it so.

## Tests

```
cd scripts && python3 -m unittest discover -s fleet_knobs/tests -t . -v
```

The tests put a fake `infisical` executable first and alone on `PATH`, point `HOME` at a temp
directory and patch out the CLI fallback directories, so they never reach the network, the
real CLI, `~/.secrets` or the real state directory.
