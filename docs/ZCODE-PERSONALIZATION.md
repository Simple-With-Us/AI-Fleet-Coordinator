# Z Code Personalization

Policy provenance includes Jay's [November 11 peer-request ruling](https://simplewithus.zulipchat.com/#narrow/near/639610865), the repository's One Operator and manual Kody sections, and the current owner-confirmed header spacing.  The pasted personalization was reconciled against those sources rather than retained as a second independent rule set.

The complete maintained source is `scripts/fleet_lanes/rules/seat-identity.zcode.md`, composed with `fleet-intro.md`, `lane-map.full.md`, and `fleet-common.md` by the shared `fleet_lanes.install_rules` installer.  Do not maintain a second policy copy here.  The installed target is `~/.zcode/AGENTS.md` on Mac and server.

Review the source and rendered plan in an owned lane.  These commands inspect or install only the named platform; `--home` must name the intended existing home directory.  An authorized live install is separate from a fixture check:

```sh
cd <owned-lane>/scripts
python3 -m fleet_lanes.install_rules plan zcode --home <home>
python3 -m fleet_lanes.install_rules apply zcode --home <home>
python3 -m fleet_lanes.install_rules verify zcode --home <home>
```

Use `--create` with plan/apply only when creating a missing rules file is authorized.  The shared installer backs up changes, writes atomically, and preserves bytes outside its `fleet-lane-map` marker block.  A successful marker verification alone therefore does not prove stale personalization outside the block was removed.  Read the whole planned result and reconcile contradictory outside-block content with a reviewed backup/replacement; never silently discard unrelated instructions.

The same source covers portable paths, ZC identity and current `zc/` branches/`zc-` lanes, accepted legacy `zcode/` work, the shared Operator, current peer policy, manual Kody receipts, U+200A header spacing, the compact-header exception for PR titles and comments only, Notes, and chat-pane sentence gaps.  PR descriptions keep full fleet headers.  Prose files and Notes take U+00A0 plus space; only the three named chat panes take the entity.  Never put resolved private server inventory into the source.

Verify rendered template content and code points before applying to live homes, then verify the complete installed file and retain backup receipts.  Installer verification proves file bytes, not that an already-running Z Code chat reloaded personalization.
