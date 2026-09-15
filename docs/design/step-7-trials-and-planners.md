# Step 7: measure, then try a stronger planner

**Status:** built on `feat/planner-briefing`, 2026-09-14. These are phases 1 and 2 of [../roadmap.md](../roadmap.md). Live results are in [../live-testing.md](../live-testing.md).

## Goal

- **Phase 1: make live results measurable and trustworthy.** A change should be judged by a success rate and a correct-value rate, not by one run, and "verified" should never come back with a wrong or missing value.
- **Phase 2: find out whether a stronger planner helps a 16 GB machine,** or whether reusing plans that worked does. Measure it; don't assume it.

## What we found before building

1. **One run per setup was anecdote.** Runs 11 and 16 had near-identical plans and opposite outcomes.
2. **"Verified" could come back with a wrong value.** Run 15 read "3.15", a pre-release row, as the latest Python release. Its evidence only proved the downloads page was open.
3. **A replan could return sub-goals with no goal text.** They ran as "Step 1" and "Step 2" (run 19).
4. **Search steps had no evidence.** Every Everest run's search step was judged by a model, so none came back fully verified.
5. **The 14B and 7B models don't fit in memory together.** qwen2.5-coder:14b is 9.0 GB and the 7B model is 4.7 GB. Next to a browser on a 16 GB Mac, a 14B planner means Ollama swapping models between planner and executor calls.
6. **Claude can't be tried on this machine yet.** There's no API key.

## Decisions

| # | Decision | Why | Alternatives considered |
|---|---|---|---|
| 1 | `web-lobster trials` runs the tasks in a YAML file N times under one or more configs, and scores their values against expectations in code: a number within a tolerance, text ignoring case and surrounding space, or a full-match pattern | A success rate and a correct-value rate are what every later change gets judged by | Reading logs by eye; the poisoned-page benchmark (it scores traps on local sites, not tasks on real ones) |
| 2 | Each run goes through `WebTaskService`, as a calling agent's MCP call would: a mandate, requested values, and `on_questions=assume` | Trials measure the path real callers use, and never stall on a question | Driving the orchestrator directly (it would skip the mandate and value handling callers get) |
| 3 | Runs go one at a time, interleaved by run: run 1 of every task and config, then run 2, and so on | Local models share one machine, and interleaving spreads a slow minute on a site across configs | Grouping by config (a bad patch of network would land on one config) |
| 4 | Memory is fresh for every run by default. `--memory shared` gives each task and config one memory file | Fresh runs are independent samples, and shared memory is how plan reuse gets measured | Using the user's own memory (runs would contaminate each other and the user's history) |
| 5 | Requested values can declare `pattern` (text must match in full), `min`, and `max` (numbers). `coerce_value` enforces them, and the extractor is told the shape | A misread like "3.15" is rejected in code instead of trusted | A model judging values (the thing that was wrong); checking only inside trials (callers would still get the wrong value) |
| 6 | A pattern that repeats a repeating group, like `(a+)+`, is rejected. Patterns are capped at 200 characters, and text values at 500 | Python's `re` has no timeout, so a catastrophic pattern could hang a run | Matching in a subprocess with a timeout (heavier); trusting planner-written patterns as they are |
| 7 | A sub-goal with no goal text is dropped. A reply with no goal text at all is an error, which planning retries and a replan treats as failed. The goal can also come from `title`, `objective`, `action`, or `step` | A placeholder like "Step 1" gives the executor nothing to act on | Inventing a name (run 19) |
| 8 | A search step with a quoted term and no evidence gets a text check for that term | Searching for 'X' should end on a page showing X, so a model's judgement becomes a code check | Asking the planner to add evidence (a 7B planner rarely does); url checks (search URLs get guessed wrong) |
| 9 | Over MCP, `verified` also requires every requested value to have been read and to have passed its checks, and `missing_values` lists any that weren't | A run that proved its steps but couldn't read the answer shouldn't come back as verified | Leaving values out of `verified` (run 15) |
| 10 | Memory saves a successful run's completed sub-goals as the planner wrote them. With `agent.reuse_plans`, the planner sees, in full, the most similar such plan (at least 50% word overlap) from a run on the same site, and is told to reuse or adapt it | A proven plan is the cheapest reliable context a small planner can get, and it holds only planner-written text | Replaying the plan without the planner (it breaks when the task differs); reuse across sites (plans don't transfer) |
| 11 | Three new configs: a 14B planner with a 7B executor, a Claude planner with a local executor and validator, and the 7B config with plan reuse | The planner makes only a few calls per task, so it's the cheapest place for a bigger model | A 14B executor (every step would pay the cost) |

## Tests

- `tests/test_value_shapes.py`: patterns must match in full, bounds apply, shapes that can't work are rejected, and the extractor is told the shape.
- `tests/test_parsing.py`: sub-goals without goal text are dropped or rejected, and search steps get a text check for their term.
- `tests/test_memory.py`: a plan that worked is offered only when reuse is on, only on the same site, and only after a success.
- `tests/test_mcp_server.py`: a missing requested value means not verified, and the value is listed.
- `tests/test_thinking_orchestrator.py`: memory saves the plan.
- `tests/test_trials.py`:
  - expectations;
  - the shipped trial file loads, and its mandates are read-only;
  - summaries and reports;
  - a two-config run against a local site, with fresh memory;
  - a run with shared memory.

## Known limits

- **Live sites change.** `python-latest` needs its expected version updated with each release, and a site outage reads as a failure.
- **Three runs per setup still leaves wide error bars.**
- **A text check on a search term proves only that the page shows it.**
- **Plan reuse picks one plan by word overlap.** A task that looks similar but wants something else gets a misleading plan, which the planner is told to adapt.
- **The Claude planner config is untested** until an API key is available.
