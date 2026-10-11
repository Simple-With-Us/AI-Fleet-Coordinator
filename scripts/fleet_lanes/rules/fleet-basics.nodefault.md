{{FLEET_INTRO}}

### Seat Identity

This tool has no default seat.  It is an engine-only CLI (Droid, Hermes, Qwen, Kimi, Vibe, Copilot CLI, DeepSeek, Pi) or an app several seats share (Conductor), and none of those is a seat.  Your seat is the first of these that applies:  a seat Jay names to you in this conversation, or a seat a launcher assigned (`AGENT_LAUNCH_SEAT` together with `AGENT_LAUNCHER`, matching your launch prompt, as when BotFleet starts you for one of its bots).  If neither applies, ask Jay which seat you are before any fleet action, and never infer one from the folder, the branch, the model, a skill or another tool's rules file.  If `AGENT_LAUNCHER` is set with no `AGENT_LAUNCH_SEAT`, or they disagree with your launch prompt, you have no seat:  do no fleet action, and say so.

Once you have a seat, check it with `agent-sync whoami --as <SEAT>` before your first fleet action, pass `--as <SEAT>` on every `agent-sync` call, and stop if `whoami` shows another seat's bot or a missing credential.  Never use another seat's credential or Jay's account.  Never write `AGENT_LAUNCH_SEAT` or `AGENT_LAUNCHER`, and never overwrite an `AGENT_SEAT` you found already set.  Sub-agents you spawn inherit your seat.

{{LANE_MAP_BODY}}

{{FLEET_COMMON}}
