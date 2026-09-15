# Step 6: think before acting

**Status:** built on `feat/planner-briefing`, 2026-09-14. Architecture overview: [../architecture.md](../architecture.md).

## Goal

Make the planner smarter without breaking planner isolation. Before the browser opens, the planner should:

- understand the task,
- say what it will assume,
- ask the user what it can't sensibly assume,
- check that the mandate allows what the task needs,
- plan with far more context than the task text alone.

Then code should check the plan, and the planner should get a chance to fix it.

## What we found before building

1. **The planner planned nearly blind.** It got the task, the start page's path, and memory of similar tasks. It didn't know:
   - the mandate, so it planned submissions a read-only mandate would block;
   - today's date, so "next Friday" meant nothing;
   - which values the caller wanted, which were bolted onto the last sub-goal after planning;
   - anything about the user.
2. **The costliest failures were decided before the first click.** In live testing ([../live-testing.md](../live-testing.md)), a 7B planner split searches into click-level steps and guessed URLs. The Everest run took 35 steps. Prompting alone didn't fix it (run 7). Missing information costs a run too: a task that needs a date the user never gave can only guess.
3. **Asking mid-run doesn't work everywhere.** Over MCP, a server can't ask a question in the middle of a call ([step 5](step-5-mcp-server.md), finding 2), and no one is watching a stdio call. Before the browser starts, though, a stateless round trip works: return the questions, and take the answers on the next call.
4. **"More context" has to come from trusted sources.** The planner never reads pages. Anything that has been near a page (text, paths, the validator's opinions) can't feed it.

## Decisions

| # | Decision | Why | Alternatives considered |
|---|---|---|---|
| 1 | Thinking is its own planner call, before the browser opens, producing a brief | Nothing has loaded, so the brief is trusted by construction. Questions get asked before anything irreversible, and stopping costs nothing | Folding it into the planning prompt (small models skip the thinking); thinking mid-run (the browser is already spending time and taking actions) |
| 2 | The brief is structured: goal, thinking, assumptions, questions, success, sites, data, changes_something, risks. It's parsed tolerantly, and an unreadable reply means no brief, not a failed run | Code needs fields it can check (sites, data, writes). A bad reply from a small model shouldn't cost the task | Free-text reasoning (nothing to check); failing the run on a parse error |
| 3 | Ask only about facts only the user knows and the task leaves out: dates, where a trip starts, how many people, a budget, which account, whether to really submit. Every question carries a best-guess default. Everything else is an assumption, and there are at most 3 questions | Every question costs the user attention, and defaults let unattended runs go ahead. In live run 14, the first version ("prefer a sensible assumption") let a 7B planner assume today's date for a flight search instead of asking | Preferring assumptions (the first version; it under-asked); asking about every ambiguity |
| 4 | Answers are settled in this order: answers supplied up front, then whoever can be asked (dashboard modal, terminal prompt), then defaults. A question with no default blocks only when nobody can be asked and `agent.questions` is `ask`. `assume` never blocks | Interactive surfaces ask, unattended ones use defaults, and a run stops only for a question it really can't guess | Always blocking (breaks unattended runs); never asking (the runs that most need a date guess one) |
| 5 | Over MCP, a stateless two-call flow. `brief_task`, or a `web_task` that returns `needs_input`, saves the brief under an id. `web_task(brief_id, answers)` runs that brief without rethinking it, and the task has to match | Works on every protocol version. A second brief could ask different questions, which would make the answers meaningless | Elicitation (fails on 2026-07-28 clients); rethinking on the second call |
| 6 | Mandate gaps are computed in code from the brief (sites, data names, writes) and become one yes/no question with no default. Declining stops the run before the browser opens. Matching is lenient: parent domains and data-name substrings count as covered | Catches a doomed run before it spends minutes. A false alarm costs one question, a missed gap costs only what it costs today, and the mandate still enforces everything either way | Blocking on gaps outright (a false alarm would stop valid runs); leaving it to the planner (it can't compare its needs with the mandate reliably) |
| 7 | More context, all of it trusted: the current date and time, the mandate (data named, never shown), the values the caller wants, the user's notes, the brief, the answers, and memory (similar tasks, plus the same sites' recent runs with steps per sub-goal) | Each of these comes from the user, the caller, the mandate approval, the clock, or the planner itself | Adding page-derived hints such as site maps or prior page titles (breaks isolation) |
| 8 | Answers are trusted like the task text, and text answers reach the planner in full | They come from the same person or calling agent that wrote the task. That's unlike text values read off pages, which stay withheld | Withholding text answers (the planner couldn't use "20:00") |
| 9 | The executor gets a short briefing: the goal, the answers, the assumptions, and the notes, capped at 1,000 characters | Choices mid-page ("which cabin?") need the answers, and a 7B context has room for a short block | Only the sub-goal text (the executor can't see the user's decisions) |
| 10 | Plan review is code. It flags too many sub-goals, `{{$refs}}` to values no earlier step reads, `{{placeholders}}` the mandate doesn't grant, write steps without a request check or under a read-only mandate, and evidence on sites the mandate doesn't allow. The planner gets one revision round, and the revision is kept unless it leaves more issues | The review is deterministic and trusted, so the model only has to fix. One round bounds the cost | A model critic (another model call, and no better at mandate arithmetic); unlimited rounds |
| 11 | Write steps are recognised by their opening verb ("Submit…", "Book…", "Delete…") | "Open the order history" mustn't count as ordering | Matching the verb anywhere in the goal |
| 12 | Memory records gain `origins` and `goal_stats` (goal text, done, how that was decided, steps). Same-site experience is shown only for origins the current task is trusted to use, and holds only planner-written text and counts | Carries lessons like "typing the search took 12 steps on this site" to the next task there, without page content | Storing visited paths or page titles (site-controlled) |
| 13 | Thinking runs before the wall-clock limit starts, and the live terminal display starts after it | A person may take minutes to answer, and a prompt can't share the screen with a live panel | Counting answer time against `max_seconds` |
| 14 | Everything is configurable: `agent.briefing`, `agent.questions`, `agent.plan_review`, `agent.max_sub_goals`, `agent.notes_file`, and on the CLI `--no-brief`, `--assume`, `--answer ID=VALUE`, and `--notes FILE`. A run still needing answers exits with code 2 | Scripts and CI need predictable behaviour, and people need to turn it off | — |
| 15 | The benchmark runs with `questions = assume`. Scripted planners have no brief, so scripted results are unchanged | No one answers a benchmark, and the scripted numbers stay comparable across steps | — |
| 16 | When a plan has more than one step, code folds click-level steps into the outcome they lead to. These are goals starting with type, enter, press, or scroll, or clicking a button, box, or field. "Click the search button" becomes the start of "…, then click on the article", proven by that step's evidence. A step with its own url, request, or value check, values to read, or wording about a change isn't folded; plan review flags it for the planner instead | In live run 16, a 7B planner with a brief still split a Wikipedia search into typing, clicking the search button, and opening the article. The button step failed its own check once the page moved on, and the run spent its budget there | Prompt changes alone (runs 7 and 16 show a 7B planner ignores them); plan review and one rewrite only (run 18: the rewrite kept two click-level steps) |
| 17 | Answers supplied before any question count even when their label matches no question. They're shown to the brief so it doesn't ask again, and to the plan and the executor | Question ids are the planner's own, so `--answer` or a calling agent can't know them in advance | Ignoring unmatched answers (silently lost); requiring `brief_task` first (fine over MCP, awkward on the CLI) |
| 18 | Folding happens whenever a plan is parsed (the plan, a revision, or a replan), before review. Read-only steps fold into the step before, bringing their text and value checks with them | In run 18 the planner's rewrite still kept two click-level steps, and the rewrite added about 35 s of planning. Folding in code is deterministic and free, and leaves the rewrite for problems only the planner can fix | A second rewrite round (more time, same model); folding only after review (the planner would spend its one rewrite on something code does anyway) |

## How a task flows now

1. The entry point (CLI, dashboard, or MCP) builds an `Orchestrator`, with a mandate if one was given and an `asker` if someone can answer.
2. `recall` fetches memory: similar tasks, and recent runs on the mandate's sites and the start page's origin.
3. `think` builds a `PlanningContext` from trusted inputs and asks the planner for a brief. It then computes mandate gaps and settles questions (decision 4). If questions are still unanswered, or the user declined a gap, the run ends here with `needs_input` or an error, and the browser never starts.
4. The browser starts. The planner plans with the full context, `review_plan` checks the plan, and the planner revises once if needed.
5. Execution is unchanged, except that every executor step gets the briefing and replans get the context without memory.
6. Memory saves the run with per-sub-goal stats and the origins visited.

## Tests

- `tests/test_briefing.py`: pure code, no browser or model.
  - Tolerant brief parsing: fences and prose, identifier cleanup, type aliases, at most three questions, and the reserved mandate-question id.
  - Answer checking: problem messages that never echo the answer, and defaults that don't fit are dropped.
  - Lenient mandate gaps.
  - Every context section, and memory left out of replans.
  - The executor briefing.
  - Each plan-review check, including click-level steps and a clean plan.
  - Answers given up front, shown even when no question matches them.
- `tests/test_thinking_orchestrator.py`: a real browser with a scripted planner backend.
  - Unanswerable questions stop the run before the browser starts.
  - Answers reach the plan prompt and every executor step, and memory records steps per sub-goal and same-site experience.
  - `assume` applies defaults and passes open questions to the planner.
  - Declining a mandate gap stops the run.
  - Plan review gets one revision round.
  - Answers given up front reach the brief, and move under the question they answer in the plan.
- `tests/test_parsing.py`: click-level steps fold into the outcome they lead to, and extract steps fold back with their checks. Steps with their own proof or wording about a change stay. Trailing click steps fold backwards, and a plan made only of click steps is left alone.
- `tests/test_planner_isolation.py`: the brief is a fourth planner prompt that must stay free of the canary. The brief reaches the replan and every executor step.
- `tests/test_mcp_server.py`: `brief_task` returns questions, required ids, defaults, and approval text. `web_task` without answers returns `needs_input` without starting. `web_task` with `brief_id` and answers runs without rethinking. A `brief_id` for another task is rejected.

## Known limits and open questions

- **Brief quality depends on the model.** A 7B model may ask needless questions or miss real ones. Defaults and the three-question cap bound the cost, but they don't fix judgement.
- **Plan review sees structure, not meaning.** It can't tell whether "Search for flights" searches for the right flights.
- **Gap detection trusts the brief's own list** of sites and data. If the brief misses a need, no gap is raised, and the mandate still blocks it at run time.
- **Answers aren't bound into write rules.** If the user answers "Dec 22", a write rule can't yet require the rebooking request to carry Dec 22. That's the benchmark's `allowed-write-abuse` gap, and the next step it points to.
- **The dashboard asks questions but has no notes field yet.** The start API accepts `notes`.
- **Claude's extended thinking isn't used for the brief.** The brief's `thinking` field works on every backend. Native thinking on Anthropic backends is a possible follow-up.
- **Text values aren't checked for shape.** In live run 15 the extractor read "3.15", a pre-release row, as the latest Python release, and the run still counted as verified: evidence proves the steps, not the value. A pattern on text values (such as `^\d+\.\d+\.\d+$`) would have caught it.
- **A 7B planner under-asks and over-splits.** Decisions 3 and 16 are backstops for what live testing showed. Their effect is measured in the re-runs in [../live-testing.md](../live-testing.md), not assumed.
- **Folded steps read mechanically and check less along the way.** A folded goal joins the original goals with ", then", and only its final step's evidence runs, so more happens before anything is checked. Steps that could change something are never folded.
