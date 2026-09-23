# web-lobster roadmap

How the pieces fit together: [architecture.md](architecture.md). What building it taught us: [learnings.md](learnings.md). Every live run: [live-testing.md](live-testing.md).

## Direction

Personal agents can now act on the web for their users, and agentic browsers are routinely hijacked by instructions planted in the pages they read. Making the model better at spotting those instructions doesn't close the problem, because attackers adapt. web-lobster takes the other route: make acting on a planted instruction impossible, and make every claimed result provable.

The goal is a web agent you can hand a logged-in browser and a goal, knowing it can't be turned against you, with a record of exactly what it did. Other agents should be able to delegate web work to it on those terms.

## Built

| Step | What | Commits |
|---|---|---|
| 1 | **Mandates.** Sites, data grants, and expiry, enforced in the browser's network layer; data typed as `{{placeholders}}` | `55bc8ab`, `afe735e` |
| 2 | **Planner isolation.** The planner never reads page content; pages reach it only as type-checked values | `3e94026` |
| 3 | **Evidence and receipts.** Sub-goals are proven done by checks run in code; every run leaves a hash-chained receipt log | `0444f80` |
| 4 | **Poisoned-page benchmark.** Trap sites, scored from what their servers received | `fcacf12` |
| 4b | **Write rules.** Mandates list the writes a task may make, closing the benchmark's same-site gap | `eac51ac` |
| 5 | **MCP server.** Other agents run web tasks under a mandate, with page text withheld by default ([design](design/step-5-mcp-server.md)) | `ba61aaf` |
| 5b | **Live testing.** Real tasks on live sites with a local 7B model, and the fixes they called for ([notes](live-testing.md)) | `fcb0a7a`–`ea6ce24` |
| 6 | **Think before acting.** A brief, questions for the user, mandate-gap checks, richer trusted context, and code review of the plan ([design](design/step-6-planner-briefing.md)) | `ab76209`, `4cec5b2` |
| 7 | **Trials, and a stronger planner.** Repeat runs scored against known answers; shape checks on values; 14B-planner and plan-reuse configs ([design](design/step-7-trials-and-planners.md)) | `f3c959d` |
| 7b | **Making it work on real pages.** Autocompletes, date pickers, positional values, readable run records, and evidence that can see what was entered ([notes](live-testing.md#what-live-testing-found)) | `8c2f502`–`6fd1006` |

## Where things stand

**Scripted benchmark**, fully hijacked executor, all defenses on: 7/7 tasks done, 1/7 harmful effects, 0/7 leaks, 0/7 false "done". An honest executor completes every task under every defense setup.

**Live**, local 7B on a 16 GB Mac ([numbers](live-testing.md#where-it-stands)): the three lookup tasks come back right and verified. Signing in to a practice site and reading a price is 3 of 3 right and verified, in a median of 14 steps and 63 s. A GitHub commits lookup is 3 of 3. Google Flights proves five of six sub-goals and doesn't finish.

## Known gaps

- **Driving a page.** A 7B executor picks the wrong verb and the wrong element when a page has more than one input: on Google Flights it spends forty steps moving between two city boxes. The browser layer can drive that site end to end when told what to do — the choice is the problem. *This is the next thing to work on.*
- **Guessed url paths in evidence.** We drop a planner url check that spells out a query string, because those are guesses. A check on an invented *path* — `…/speakers/2026` for a site that has no such page — is the same mistake, and the run chases it until the budget runs out. The executor's own guessing is handled now; the planner's isn't.
- **Guessed text checks.** A planner-invented text check ("elevation of Mount Everest is", a phrase Wikipedia never shows) failed two runs on the right page. Sentence-like text checks next to a url check that already pins the page should be treated as guesses, the way guessed search URLs are.
- **Write content.** Write rules scope endpoints, not what's sent to them, so a planted instruction can misuse an allowed endpoint (`allowed-write-abuse`). Evidence refuses to call the wrong result done, but can't undo it. Next: value-bound write rules — a rebooking date that must match a typed value from the task.
- **Live models, unmeasured.** Measurements are a handful of runs on one local 7B model. The benchmark's live mode hasn't run, so how often a real model falls for a planted instruction is still unknown. Claude configs have never been run live.
- **Staying signed in.** Every task starts with a fresh browser profile, so no session carries over between runs. Signing in *inside* a run works. Persistent profiles are designed ([design](design/step-8-signed-in-tasks.md)) but not built.
- **Dashboard.** The web dashboard asks the planner's questions, but doesn't accept mandates or notes, or show receipts.
- **Cross-origin reads.** Data the agent never typed (page text, cookies) can still leave through cross-origin GETs that pages need in order to load.

## Next

### Phase A: make the executor choose well

The last thing between web-lobster and a page it has to drive. Everything under it is already built.

- **The right verb.** `select` commits a suggestion; `type` doesn't, deliberately ([why](live-testing.md#driving-a-real-widget)). A 7B executor reaches for `type` on a combobox. Change the choice, not the primitive.
- **The right element.** On a page with several inputs sharing a label, it revisits the one it just filled. The observer already hides covered elements; what it doesn't do is tell the executor which one it used last.
- *Done when* the Google Flights task runs its search, measured over repeated runs.

### Phase B: close the safety gaps

- **Value-bound write rules.** The user's answers bind what a write may send, closing `allowed-write-abuse`, measured with the benchmark.
- **A live benchmark run.** How often real models fall for each trap, with and without the defenses.

### Phase C: real-world use

- **Signed-in tasks.** Persistent browser profiles scoped to a mandate, and secrets from a password manager rather than environment variables ([design](design/step-8-signed-in-tasks.md)).
- **The dashboard.** Mandate entry, receipts, and a notes field.

### Phase D: speed

- Shorter prompts for small models.
- Skip the brief, or merge it with planning, when the task already says everything.

Both judged with `web-lobster trials`, so speed never costs correctness unnoticed.

### What would change the order

- An API key becomes available: try a Claude planner early, to learn whether planning is the ceiling.
- Signed-in tasks become the priority: phase C's profiles move ahead of phase B, design first.
- The executor turns out to be a prompt problem rather than a model one: phase A gets much cheaper, and phase B starts sooner.

## Settled, with the evidence

Things that looked like good ideas and were measured instead of assumed:

| Idea | Verdict |
|---|---|
| Make `type` commit an autocomplete suggestion | **Reverted.** Cost Everest every run it had been winning (0 of 3 done), and bought nothing on Google Flights ([numbers](live-testing.md#driving-a-real-widget)) |
| A 14B planner | **Not the default.** Fixed multi-step planning, doubled lookup time, didn't help value reading ([numbers](live-testing.md#one-comparison-worth-keeping-a-14b-planner)) |
| Let a reading step prove itself with "the value was read" | **Kept, narrowed.** As first written it let a Tokyo run come back verified on an advert's price |
| Trim the page so the reader sees less | **Doesn't work.** The reader takes the last match in whatever window it gets; trimming moves the wrong answer |
