# Operation-specific declared reader source candidate

This is a NEW source candidate composed on main a8da86a491c59488cd3f319a983016fb59b3e908. It replaces the single Fleet-OPS binding design in draft 2412, not the installed app or reader.

The native app preserves the installed reader pathname, using system Python with -I and a HOME/system-PATH environment. It preserves these existing operation scopes and exact accounts:

| Operation | Project | Folder | Expected account |
| --- | --- | --- | --- |
| Named value get | ai-fleet-coordinator | / | muster-afc-ro |
| Default names and doctor | fleet-ops | / | muster-fleetops-ro |
| Explicit AFC names | ai-fleet-coordinator | / | muster-afc-ro |
| Explicit AFC names | ai-fleet-coordinator | /code-mirrors | muster-afc-code-mirrors-ro |
| Explicit AFC names | ai-fleet-coordinator | /wake-router | muster-afc-wake-router-ro |

Every call selects consumer muster-data-reader and a protected declaration under ~/.config/infisical/muster/<expected-account>.json. The additive --consumer-account assertion checks declaration.tokenName before any store access. Existing declared callers may omit it; undeclared callers cannot use it. No runtime fallback, scope broadening, migration or replacement account is added. Unknown project/folder pairs fail before launch. Existing Controls provider-folder listing and current values remain unchanged.

Intentional safety differences: a 45-second outer bound leaves room for the existing bounded worker/store budgets; exit124 and unavailable failures stay unavailable; thrown cancellation propagates, but active task cancellation does not newly terminate an existing blocking child. Doctor permits only fixed metadata and the fixed refusal category, never provider prose. Output-budget exhaustion remains fail closed with empty stdout and is classified unavailable by the bounded core, not necessarily a literal timeout.

The shared reader/client must be a matched, qualified pair before deployment. Source preparation does not prove finite grants, registry provenance, protected declarations, own login-Keychain custody, Finder acceptance, installation or publication authority. No such effects were performed. Prior restriction claims remain unresolved provenance, not a new authorization created by this document.
