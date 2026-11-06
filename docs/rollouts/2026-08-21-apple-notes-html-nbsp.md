# Apple Notes HTML sentence gap

Historical identifiers have been redacted. These records do not certify current deployment state.

Owner: remember the double space after a period when writing HTML too.

## Why

Notes.app is an HTML renderer.  Two ASCII spaces in a `<p>` collapse to one, so `--html` notes looked single-spaced even when the source had two.

## What

- `--html` and MD-converted bodies: leftover `.  ` / `!  ` / `?  ` become `.&nbsp; ` (skipped inside `<code>` / `<pre>`).
- Skills + FLEET-UI-COPY + [retired identifier] HTML that a renderer will show uses `&nbsp; ` after sentence punctuation.  Markdown files still use two ASCII spaces.
- Incident note rewritten with `&nbsp; `.

## Verify

`Hello.  World.` in a `<p>` becomes `Hello.&nbsp; World.`; the same sequence inside `<code>` is left alone.
