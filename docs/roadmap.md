# web-lobster roadmap

How the pieces fit together today: [architecture.md](architecture.md). What building and testing it has taught us: [learnings.md](learnings.md).

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
| 6 | **Think before acting.** Before the browser opens: a brief, questions for the user, mandate-gap checks, richer trusted context for the planner, and code review of the plan, with click-level steps folded in code ([design](design/step-6-planner-briefing.md)) | Done | `ab76209`, `4cec5b2` |

## Where things stand

Scripted benchmark with a fully hijacked executor, all defenses on: 7/7 tasks done, 1/7 harmful effects, 0/7 leaks, 0/7 false "done". An honest executor completes every task under every defense setup.

Live, with a local 7B model on a 16 GB Mac ([notes](live-testing.md)): lookups that start on the right page finish verified in under a minute (python.org, 42 s). After step 6, three repeated searches from Wikipedia's main page all found Mount Everest's elevation, with a median of 9 steps and 171 s. Before it, the same task took 27 to 35 steps or failed. The brief asks questions, but a 7B planner still under-asks.

## Known gaps

- **Write content.** Write rules scope endpoints, not what's sent to them, so a planted instruction can misuse an allowed endpoint (`allowed-write-abuse`). Evidence refuses to call the wrong result done, but can't undo it. Next: value-bound write rules, for example a rebooking date that must match a typed value from the task.
- **Live models.** Measurements so far are a handful of runs per task on one local 7B model ([notes](live-testing.md)). A text value can be wrong while the run counts as verified (run 15), search steps are judged by a model, and Claude configs haven't been run live. The benchmark's live mode hasn't run either, so how often a real model falls for a planted instruction is still unmeasured.
- **Interactive sites.** Choosing from an autocomplete works, but only through `select`, and a 7B executor reaches for `type` on those fields. Making `type` commit the suggestion was measured over nine runs and reverted: it cost the Everest task every run it had been winning, and still didn't get Google Flights to search ([notes](live-testing.md#making-type-commit-measured-then-reverted)). Next: get the executor to pick `select` on a combobox.
- **Login.** Tasks start with a fresh browser profile, so signed-in tasks don't work ([design](design/step-8-signed-in-tasks.md)).
- **Dashboard.** The web dashboard asks the planner's questions, but doesn't accept mandates or notes, or show receipts.
- **Cross-origin reads.** Data the agent never typed (page text, cookies) can still leave through cross-origin GETs that pages need in order to load.

## Next

Based on live runs 14 to 21 ([notes](live-testing.md)). Each phase says what finishes it.

### Phase 1: make results measurable and trustworthy

*Status:* items 1 to 4 are built (`f3c959d`). The first trials comparison found two more problems, added below as items 5 and 6.

1. **A repeat-run tool.** Run a task N times against a known answer and report the success rate, the correct-value rate, and median steps and time. Every later change is judged with it. *Done when* a live trial of a task is one command, and its report goes into the notes.
2. **Shape checks on values.** A `pattern`, `min` and `max`, or an allowed list on requested values, enforced in code, so a misread like run 15's "3.15" can't come back as verified. *Done when* a value that fails its shape is rejected in a test and in a live rerun.
3. **No goal-less sub-goals.** A planner reply whose sub-goals have no goal text is rejected or retried, instead of running as "Step 1" (run 19). *Done when* a parsing test covers it.
4. **Evidence for search steps.** Every Everest run had its search step judged by a model, so none came back fully verified. Search-shaped steps should get a checkable outcome where the page allows one. *Done when* repeated Everest runs come back verified.
5. **Guessed text checks.** In the trials, the 7B planner gave the article step a text check for "elevation of Mount Everest is", a phrase Wikipedia never shows, and two runs failed on the right page. Sentence-like text checks next to a url check that already pins the page should be treated as guesses, the way guessed search URLs are. *Done when* a parsing test covers it and repeated 7B Everest runs recover.
6. **The caller's value checks win.** When the planner declares a value with the same name as one the caller requested, the caller's `pattern`, `min`, and `max` are dropped; it happened in two of three python.org trials. *Done when* a test shows the caller's checks apply either way.

### Phase 2: a stronger planner

*Status:* the 14B-planner, Claude-planner, and plan-reuse configs are built (`f3c959d`). In the first comparison, the 14B planner found Mount Everest 3 of 3 times, fully verified, against 1 of 3 for the 7B baseline. It doubled the time on lookups and read the python.org version right only 1 of 3 times, because the value reader is still the 7B model. Plan-reuse runs are in progress, and the Claude planner needs an API key ([learnings](learnings.md#7-a-stronger-planner-helps-multi-step-tasks-and-costs-time)).

Compare, with the phase 1 tool and on the same tasks:

- qwen2.5-coder:14b as the planner, with the 7B executor. It's already installed; the cost is Ollama swapping models between calls.
- A Claude planner with a local executor and validator. It needs an API key and costs money.
- Plans reused from memory: a plan that worked on a site, offered again for the next task there.

Judge them by success rate, question quality (a 7B planner still under-asks, as in run 17), time, and cost. *Done when* one is the recommended setup, with the numbers behind it.

### Phase 3: close the safety gaps

- **Value-bound write rules.** The user's answers can bind what a write sends, such as the date of a rebooking. That closes `allowed-write-abuse`, measured with the benchmark.
- **A live benchmark run.** How often real models fall for each trap, with and without the defenses.

### Phase 4: real-world use

- **Signed-in tasks.** Persistent browser profiles scoped to a mandate, and secrets from a password manager rather than environment variables. Designed in [design/step-8-signed-in-tasks.md](design/step-8-signed-in-tasks.md); not built.
- **The dashboard.** Mandate entry, receipts, and a notes field.

### Phase 5: speed

- Shorter prompts for small models.
- Skip the brief, or merge it with planning, when the task already says everything.

Both are judged with the phase 1 tool, so speed never costs correctness unnoticed.

### What would change the order

- The phase 1 tool shows the 7B planner falling short on more tasks: phase 2 comes first.
- An API key becomes available: try a Claude planner early, to learn whether planning is the ceiling.
- Signed-in tasks become the priority: phase 4's profiles move ahead of phase 3, design first.
