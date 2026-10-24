# v9.2 delta — narrow-viewport bearer safety disclosure (pages.js only)

Base: reviewed head (v9 archive 0e121456 + v9.1 delta b018b472, i.e. branch
echo/gr-shared-bearer-v9-proposal through 33232d13 + ed11c004 comment fix).

Defect (found in pixel review): at <=38rem the bearer table switches to
display:block with white-space:nowrap. The rotation-consequence warning
(.bearer-warn) and the consent checkbox label (.bearer-form .bearer-accept)
inherited the nowrap, so at phone widths the disclosure text ran past the
table's scrollable width and was clipped mid-sentence: the part explaining
that rotating unticked REMOVES existing /mcp access was unreadable even after
scrolling. Safety disclosure, not cosmetic.

Fix (one CSS rule inside the existing @media(max-width:38rem) block):
  .bearer-warn,.bearer-form .bearer-accept{white-space:normal;max-width:22rem}
Both elements wrap again, bounded to 22rem so the full warning and label are
readable within a 390px viewport once the Actions column is scrolled into
view. Warning/label stay inside their per-identity row, so identity-to-action
association is unchanged. Desktop styles untouched (rule lives only in the
media query); STYLE_HASH recomputed (test/pages.test.mjs enforces the match).

Validation: full module suite 621 tests, 613 pass, 8 skip, 0 fail after the
change (identical counts to reviewed head). Pixel evidence from the fixed
source: 390px unscrolled and 390px scrolled-to-Actions captures show the
complete warning text and checkbox label; desktop capture unchanged.

Known residual (flagged for review judgment, not changed): at 390px the
bearer table still scrolls horizontally, so Identity and Actions are not
simultaneously visible; association is structural (same row). Same pattern as
the other admin tables. Changing that would be a layout redesign beyond this
bounded fix.
