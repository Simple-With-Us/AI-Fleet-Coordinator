# Muster: Writing The Fleet Knobs

AFC-362 (Muster flips the resume-wake and fleet knobs) and AFC-393 (the owner-only Infisical identity).  This document is what the app does, what it refuses, and the one thing only the owner can do.

Why it is shaped this way:  Infisical is the fleet's single source of truth for settings (the `INFISICAL.md` policy), the knobs are read through `scripts/fleet_knobs`, and the daemon sees a change on its next refresh.  Muster writes the same keys the daemon reads, so Muster holds no copy of its own and never becomes a second source.

## The identity

Muster uses its own Infisical machine identity in the `Fleet-OPS` project, `prod` environment.  It is owner-only, and its role is limited to the knob keys:

- A role that reads and writes only the `fleet_knobs` keys, by secret-name condition or by a dedicated folder.  It refuses every other key, and it refuses every other environment.
- A non-tunable key such as `FLEET_FACTS_SIGNING_KEY` is refused, and so is any key that is not in the `fleet_knobs` registry.
- Access tokens with a 15-minute TTL, so a leaked token is short-lived.

Owner step, because only the owner can create identities and roles:  create the machine identity in `Fleet-OPS` `prod`, attach the scoped role, and hand the client id and client secret to the app once.  The secret goes straight into the macOS Keychain;  it is never pasted into a chat, a file, an environment variable or a log, and the agent that installs it does not read it back.

## The secret in the Keychain

The client secret lives in one Keychain item, owned by Muster's stable signing identity (`services.jays.Muster`, the same Developer ID identity the app is signed with):

- An access-control item that requires user presence, so reading the secret asks for Touch ID.
- Bound to the signing identity, so another build of the same app cannot read it.
- Readable only by that identity:  an agent shell cannot read it, and with the screen locked it cannot be used.

The app reads the item only when it is about to write, and only for the length of the write.  It never caches the secret, and it never copies the secret into a log, an error message or a plan preview.

## The read path

The app already reads the knob registry (`scripts/fleet_knobs/registry.py`, mirrored into the `INFISICAL.md` table) and the last-known-good copy of the current values.  The read path is not new:  a knob's kind, range and default come from the registry, so the app shows and accepts only valid values.

## The write path

Every write is the same five steps, and there is no path that skips one:

1. **A plan.**  The knob's name, its old value, the new value, and why.  The plan names one knob.  A write of more than one knob is a different plan, so each is confirmed on its own.
2. **A fresh check.**  The app reads the knob's current value again, and refuses the write when the value it planned against has moved.  A stale plan is never written.
3. **User presence.**  Touch ID, once, before the first write.  The app cannot skip it, and no flag can.
4. **The write.**  The app mints a short-lived access token from the Keychain's client id and secret, and writes exactly one secret path in `Fleet-OPS` `prod`.  Infisical first, then the app's cache:  a failed write fails the write, and the app says so instead of pretending.
5. **The proof.**  The app reads the value back.  The write is reported as done only when the read-back matches what was planned.

## The refusals

The app refuses, and says why, when:

- The knob is not in the `fleet_knobs` registry.
- The value does not fit the knob's kind, range or pattern.
- The knob is not a tunable one (a signing key or a credential is never a tunable knob).
- The environment is not `prod`.
- The identity's role does not cover the key.
- The plan is stale, the user presence is refused, the Keychain item is missing or locked, or the token cannot be minted.
- No identity is installed at all:  the app reads and shows, and says plainly that it cannot write until the owner installs one.  It never fails silently and never pretends a write happened.

## The audit

Two records, both without the secret:

- Infisical's own audit log records every write with Muster's identity, so the write is attributable to the app and not to a seat.
- The daemon writes a `knob_change` row (name, old value, new value;  these knobs are not secrets) to `listener/knobs.jsonl` on the refresh that sees the change, and `simple-chat status` shows each knob's value and its source.  The app does not write that row;  the daemon does, and the app waits for the refresh.

## What the app does not do

- It does not hold a copy of a knob's value as its own truth.
- It does not write through a seat's identity, and no seat's credential can write these keys.
- It does not write outside `Fleet-OPS` `prod`, and it does not write a key the registry does not name.
- It does not read a secret value:  a knob's value is a setting, and a credential is never a knob.
