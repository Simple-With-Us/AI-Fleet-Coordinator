# Copy-paste prompt for a chat-only seat

For a seat with no headless CLI (Clutch's web profile, Grok on web or iOS, Jet).  On the Mac, print the prompt for one PR with YOUR seat (the chat seat's own tag; workers never borrow another seat), copy it, and paste it as the first message:

```bash
AGENT_SEAT=CLUTCH scripts/pr-sweep prompt Simple-With-Us/BotFleet#1219 | pbcopy
```

Then add this line under the pasted text:

> Work this PR with your terminal tool, following every rule above.  When you finish, answer with the result fields the rules list.

Do not retype or shorten the rules: the printed prompt is generated from `scripts/pr_sweep/rules.md` and is the only correct copy.  If the chat has no terminal tool, use the pasted prompt as a review checklist only and leave the PR to a seat that can run git.  Afterwards, check GitHub rather than the answer: `scripts/pr-sweep status Simple-With-Us/BotFleet#1219`.
