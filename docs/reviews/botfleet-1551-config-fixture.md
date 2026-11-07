# BotFleet PR1551 exact config fixture inspection

Inspection performed by Wise on November 6, 2026. This is a full-content
inspection record, not a claim that Kody accessed the unpublished candidate.
The separate entry PR asks normal reviewers to review this record, the exact
registry binding and its generic-only enforcement. Mechanism PR1430 alone does
not approve this entry.

## Exact source and destination

- Repository: Simple-With-Us/BotFleet
- Destination: server/config.test.ts
- Proposed bytes: 114947, UTF-8
- Full SHA256: 466868a395827bb284e392b277dff7e0f16adad66fe68306a5dfc22fff28bf19
- Recomputed Git blob: 9c049b5e6e5f53411f56869b956e627b68fa6fe7
- Main comparison blob: d512325956708d5c9c9fbea4a64263f0cbd7e463
- Local conflict-resolution commit: 9d31788c (not a remote source reference)
- Scope: +196/-2 lines in this file only. Purposeful Infisical readSources,
  source-locator, save-validation and authMethod coverage. The two removed
  assertions are replaced by explicit authMethod-aware expectations.

The candidate was not in the published partial PR1551 head when inspected.
There is no existing remote blob or public payload-review link to cite.
Wise retained the exact candidate, comparison source, diff, provenance and
scanner-context packet for authorized inspection. Publication of the file is
still subject to this entry's normal review and release gates.

## Full-content findings

Wise recomputed the full SHA256 and Git blob from all candidate bytes, compared
the full main file and diff, ran the actual canonical scanner and inspected all
match contexts. All flagged values are synthetic test data or references to
synthetic test constants. No actual credential or unrelated private content was
found. This conclusion comes from test setup, assertions, temporary-file use,
negative-path behavior and source comparison, not placeholder names alone.

The full candidate has 18 generic matches: 10 assigned secret and 8 assigned
credential. Nine whole match lines occur byte-for-byte in main; nine are new.
Neither the full candidate nor main has a dedicated or Zulip-shaped match.

| Candidate line | Generic classification | Source | Context assessment |
| --- | --- | --- | --- |
| 1545 | assigned credential | inherited | local synthetic sentinel constant |
| 1668 | assigned credential | inherited | environment assignment from sentinel constant |
| 1752 | assigned secret | inherited | fake source default test |
| 1804 | assigned secret | inherited | fake stored-config load test |
| 1823 | assigned secret | inherited | assertion of same fake stored config |
| 1848 | assigned secret | inherited | temporary config-file fixture |
| 1857 | assigned secret | inherited | parser patch fixture |
| 1858 | assigned secret | inherited | assertion of same parser fixture |
| 1942 | assigned secret | new | intentionally rejected inline source value |
| 2033 | assigned secret | new | fake primary credential-reference budget test |
| 2050 | assigned credential | new | fake primary environment bootstrap |
| 2063 | assigned credential | new | fake read bootstrap in locked-save test |
| 2064 | assigned credential | new | fake write bootstrap in locked-save test |
| 2077 | assigned secret | new | temporary metadata-save fixture |
| 2090 | assigned credential | new | fake read bootstrap, shared-reference test |
| 2091 | assigned credential | new | fake write bootstrap, shared-reference test |
| 2104 | assigned credential | new | fake bootstrap, explicit replacement test |
| 2166 | assigned credential | inherited | environment assignment from sentinel constant |

No finding remains against these exact bytes. This record is limited to the
source hash and destination above, not future edits, other paths or other files.
The entry changes only the two generic assignment heuristics for normal file
publication. Dedicated patterns, provider patterns, Zulip-shaped keys and every
loaded credential remain absolute refusals, including short loaded values.
Messages and non-file arguments retain ordinary scanning. There is no caller
approval flag, path-prefix exception, encoded payload or blanket fixture waiver.
