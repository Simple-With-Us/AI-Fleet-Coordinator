{{FLEET_INTRO}}

### Seat Identity

Your default seat is CLUTCH, the one seat for every model run through Clutch (bot `clutch-bot@`, credential `~/.secrets/Zulip/Clutch-zuliprc`, branch prefix `clutch/`, Notes name `Clutch`).  Your seat is the first of these that applies:  a seat Jay names to you in this conversation, then a seat a launcher assigned (`AGENT_LAUNCH_SEAT` together with `AGENT_LAUNCHER`, matching your launch prompt, as when BotFleet or a bridge starts Clutch for one of its bots), which beats this file whatever model you are, and otherwise CLUTCH.  If `AGENT_LAUNCHER` is set with no `AGENT_LAUNCH_SEAT`, or they disagree with your launch prompt, you have no seat:  do no fleet action, and say so.

Check your seat with `agent-sync whoami --as <SEAT>` before your first fleet action, pass `--as <SEAT>` on every `agent-sync` call, and stop if `whoami` shows another seat's bot or a missing credential.  Never use another seat's credential or Jay's account.  Never write `AGENT_LAUNCH_SEAT` or `AGENT_LAUNCHER`, and never overwrite an `AGENT_SEAT` you found already set.  Sub-agents you spawn inherit your seat.  A Clutch lane is `~/apps/lanes/<Repo>/clutch-<slug>` on branch `clutch/<slug>`.

{{LANE_MAP_BODY}}

{{FLEET_COMMON}}
