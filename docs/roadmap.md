# web-lobster roadmap

How the pieces fit together today: [architecture.md](architecture.md).

## Direction

Personal agents like OpenClaw can now act on the web for their users, and agentic browsers are routinely hijacked by instructions planted in the pages they read. Making the model better at spotting those instructions doesn't close the problem, because attackers adapt. web-lobster takes the other route: make acting on a planted instruction impossible, and make every claimed result provable.

The goal is a web agent you can hand a logged-in browser and a goal, knowing it can't be turned against you, with a record of exactly what it did. Other agents should be able to delegate web work to it on those terms.

## Status

| Step | What | Status | Commits |
|---|---|---|---|
| 1 | **Mandates.** Sites, data grants, and expiry, enforced in the browser's network layer; data typed as `{{placeholders}}` | Done | `55bc8ab` (with fix `afe735e`) |
| 2 | **Planner isolation.** The planner never reads page content; pages reach it only as type-checked values | Done | `3e94026` |
| 3 | **Evidence and receipts.** Sub-goals are proven done by checks run in code; every run leaves a hash-chained receipt log | Done | `0444f80` |
| 4 | **Poisoned-page benchmark.** Trap sites, scored from what their servers received | Done | `fcacf12` |
| 4b | **Write rules.** Mandates list the writes a task may make, closing the benchmark's same-site gap | Done | `eac51ac` |
| 5 | **MCP server.** Other agents run web tasks under a mandate, with page text withheld from them by default ([design](design/step-5-mcp-server.md)) | Done | `ba61aaf` |
| 5b | **Live testing.** Real tasks on live sites with a local 7B model, over the CLI and MCP over stdio, and the fixes they called for ([notes](live-testing.md)) | Done | `fcb0a7a`–`ea6ce24` |
| 6 | **Think before acting.** Before the browser opens: a brief, questions for the user, mandate-gap checks, richer trusted context for the planner, and code review of the plan ([design](design/step-6-planner-briefing.md)) | Built | — |

## Where things stand

Scripted benchmark with a fully hijacked executor, all defenses on: 7/7 tasks done, 1/7 harmful effects, 0/7 leaks, 0/7 false "done". An honest executor completes every task under every defense setup.

Live, with a local 7B model on a 16 GB Mac: lookups that start on the right page finish verified in under a minute (python.org, 42 s). A search that starts from Wikipedia's main page found Mount Everest's elevation correctly, but took 35 steps and 8 minutes ([notes](live-testing.md)).

## Known gaps

- **Write content.** Write rules scope endpoints, not what's sent to them, so a planted instruction can misuse an allowed endpoint (`allowed-write-abuse`). Evidence refuses to call the wrong result done, but can't undo it. Next: value-bound write rules, for example a rebooking date that must match a typed value from the task.
- **Live models.** Real tasks have run on live sites with a local 7B model, over the CLI and over MCP stdio ([notes](live-testing.md)). Lookups that start near the answer work; multi-page flows with a 7B planner are hit or miss, and Claude configs haven't been run live. The benchmark's live mode hasn't run either, so how often a real model falls for a planted instruction is still unmeasured.
- **Login.** Tasks start with a fresh browser profile, so signed-in tasks don't work.
- **Dashboard.** The web dashboard doesn't accept mandates or show receipts.
- **Cross-origin reads.** Data the agent never typed (page text, cookies) can still leave through cross-origin GETs that pages need in order to load.

## Candidates after step 6

1. Value-bound write rules, measured with the benchmark. The user's answers make these natural: a rebooking date they gave can bind the rebooking request.
2. A live-model benchmark run, to measure how often real models fall for each trap with and without defenses.
3. Persistent, mandate-scoped browser profiles for signed-in tasks.
4. Mandates, receipts, and a notes field in the dashboard (it already asks the planner's questions).
5. Native extended thinking for Claude planners, alongside the brief's `thinking` field.
