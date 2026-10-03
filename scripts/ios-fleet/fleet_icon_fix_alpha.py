#!/usr/bin/env python3
"""
fleet_icon_fix_alpha.py — remove the alpha channel from iOS 1024 App Store icons.

WHY
Apple rejects an App Store icon that carries transparency as ITMS-90717, and the app then
effectively has no valid icon. The fleet icon audit (fleet-icon-audit.mjs) detects it;
this is the other half. It COMPOSITES the icon onto an opaque background rather than
merely discarding the alpha channel, which would turn every transparent pixel black.

SAFETY — read before running against a repo
  * Default writes to a STAGING directory only. Nothing under ~/Code is modified.
  * --in-place rewrites the source file, but REFUSES to touch ~/Code (the owner's
    integration tree is reset by a daemon). Land changes from a worktree instead.
  * macOS icon sets are SKIPPED. macOS app icons are REQUIRED to carry alpha
    (rounded rect + vibrancy); flattening them would break the Mac app. Only the iOS
    1024 marketing icon is touched.
  * --bg should match the design's own background when the art is a transparent shape
    on transparency. The default white is correct for rounded-corner art, because Apple
    applies its own mask anyway.

Usage:
  python3 fleet_icon_fix_alpha.py                     # stage fixes for every offending app
  python3 fleet_icon_fix_alpha.py --app hoghunter     # one app
  python3 fleet_icon_fix_alpha.py --bg '#0A2540'      # background for transparent-shape art
  python3 fleet_icon_fix_alpha.py --in-place          # rewrite sources (never under ~/Code)
"""

import argparse
import json
import os
import sys

from PIL import Image

IOS_FLEET = "/Users/jay/apps/ios-fleet"
REPORT_PATH = os.path.join(IOS_FLEET, "artifacts", "icon-audit.json")
STAGE_DIR = os.path.join(IOS_FLEET, "artifacts", "icon-fixes")
INTEGRATION_ROOT = "/Users/jay/Code"


def parse_hex(hex_str):
    h = hex_str.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    n = int(h, 16)
    return ((n >> 16) & 255, (n >> 8) & 255, n & 255)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default=None, help="limit to one app key")
    ap.add_argument("--bg", default="#FFFFFF", help="opaque background to flatten onto")
    ap.add_argument("--in-place", action="store_true", help="rewrite source files")
    args = ap.parse_args()

    if not os.path.exists(REPORT_PATH):
        print(f"No audit report at {REPORT_PATH}. Run: node {IOS_FLEET}/fleet-icon-audit.mjs", file=sys.stderr)
        return 2

    with open(REPORT_PATH) as fh:
        report = json.load(fh)

    rgb = parse_hex(args.bg)
    os.makedirs(STAGE_DIR, exist_ok=True)

    fixable = {"ICON_TRANSPARENT", "ICON_ALPHA_CHANNEL"}
    offending = [f for f in report.get("findings", []) if f.get("code") in fixable]
    if args.app:
        offending = [f for f in offending if f.get("appKey") == args.app]
    if not offending:
        print("No alpha-channel iOS icons to fix.")
        return 0

    print(f"Flattening onto {args.bg} (opaque). Mode: {'IN PLACE' if args.in_place else 'stage only'}.")
    fixed, written, skipped = 0, [], []

    for finding in offending:
        key = finding.get("appKey")
        app = (report.get("apps") or {}).get(key) or {}
        repo_root = app.get("repoRoot")
        if not repo_root:
            continue
        abs_repo = os.path.join("/Users/jay", repo_root)

        for icon_set in app.get("sets", []):
            if icon_set.get("platform") != "ios":
                continue  # never touch macOS sets
            if icon_set.get("masterHasAlphaChannel") is not True:
                continue
            if icon_set.get("masterTransparentPct") is None:
                continue
            src = os.path.join(abs_repo, icon_set["path"], icon_set.get("masterFile") or "")
            if not os.path.isfile(src):
                skipped.append(f"{key}: missing {src}")
                continue

            try:
                with Image.open(src) as im:
                    if im.mode not in ("RGBA", "LA", "P"):
                        skipped.append(f"{key}: mode {im.mode} carries no alpha")
                        continue
                    rgba = im.convert("RGBA")
                    bg = Image.new("RGB", rgba.size, rgb)
                    flat = Image.alpha_composite(bg.convert("RGBA"), rgba).convert("RGB")

                    if args.in_place:
                        if src.startswith(INTEGRATION_ROOT + os.sep):
                            skipped.append(f"{key}: REFUSED integration tree — land from a worktree")
                            continue
                        flat.save(src, "PNG")
                        print(f"  FIXED in place: {src}")
                    else:
                        out = os.path.join(STAGE_DIR, f"{repo_root}__{icon_set['path']}__{icon_set.get('masterFile')}".replace("/", "__"))
                        os.makedirs(os.path.dirname(out), exist_ok=True)
                        flat.save(out, "PNG")
                        print(f"  staged: {out}")
                    written.append({"app": key, "source": src, "set": icon_set["path"]})
                    fixed += 1
            except Exception as exc:  # noqa: BLE001 - report and continue
                skipped.append(f"{key}: {exc}")

    for s in skipped:
        print(f"  skip: {s}")

    print(f"\n{fixed} icon file(s) flattened onto {args.bg}.")
    if fixed and not args.in_place:
        print(f"Staged copies: {STAGE_DIR}")
        print("Next: inspect the staged file, copy it over the source in your worktree, commit, re-run the audit.")

    with open(os.path.join(IOS_FLEET, "artifacts", "icon-fix-log.json"), "w") as fh:
        json.dump({"at": None, "bg": args.bg, "inPlace": args.in_place, "written": written}, fh, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
