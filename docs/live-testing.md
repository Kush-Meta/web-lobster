# Live testing

Everything through step 4 was measured with scripted models on local trap sites. This is the other half: real tasks, on live sites, driven by a local 7B model — and the bugs that only appear there.

Every run is recorded, failures included. Where a run changed the code, the change is named. Lessons drawn from all of it are in [learnings.md](learnings.md); what's still open is in [roadmap.md](roadmap.md).

## Where it stands

The latest measured run of each task, fresh memory, `configs/local-16gb.yaml` (qwen2.5-coder:7b for every role) on an M4 with 16 GB. The three lookups are a one-run regression check; the rest are three runs each, except Google Flights at two:

| Task | Done | Right | Verified | Median steps | Median time |
|---|---|---|---|---|---|
| Eiffel Tower height, starting on the article | 2/2 | 2/2 | 2/2 | 0 | 34 s |
| Latest Python 3 release, starting on the downloads page | 2/2 | 2/2 | 2/2 | 1.5 | 55 s |
| Mount Everest's elevation, from Wikipedia's main page through its search box | 2/2 | 2/2 | 2/2 | 4 | 71 s |
| Sign in to a practice site and read a product price | 3/3 | 3/3 | 3/3 | 14 | 63 s |
| Newest commit on a GitHub commits page | 3/3 | 3/3 | 3/3 | 2 | 95 s |
| Cheapest New York → Tokyo flight on Google Flights | 0/2 | — | 0/2 | 40 | 537 s |

Google Flights is the one that doesn't finish. It proves five of its six sub-goals — both cities and both dates, all by code — and then spends its remaining steps moving between the two city boxes instead of running the search. That last step is the executor choosing the wrong verb on the wrong element, which no browser-layer fix reaches.

## Setup

- **Machine:** Apple M4, 16 GB, models served by Ollama.
- **Model:** qwen2.5-coder:7b as planner, executor, and validator ([`configs/local-16gb.yaml`](../configs/local-16gb.yaml)) — text only, DOM mode, 16k context. gemma3:12b, a vision model, was tried for reading values: slower, no more accurate.
- **Paths exercised:** `web-lobster run` on the CLI; `web_task` in-process through the MCP SDK's in-memory client; `web-lobster mcp` over stdio as a subprocess, the way OpenClaw and Claude Code launch it; and `web-lobster trials` for anything measured.
- **Mandates:** read-only, one origin per task. Nothing has been submitted, bought, or sent. The one signed-in task uses a practice site whose credentials are published on its own front page.

## Reproduce

```bash
ollama pull qwen2.5-coder:7b
web-lobster run -c configs/local-16gb.yaml -u https://en.wikipedia.org/wiki/Eiffel_Tower "How tall is the Eiffel Tower?"
```

Measured, rather than once:

```bash
web-lobster trials trials/web.yaml -c configs/local-16gb.yaml -n 3
```

Over MCP, start `web-lobster mcp -c configs/local-16gb.yaml` from your client and call `web_task`:

```json
{"task": "Find the version number of the latest Python 3 release on python.org.",
 "mandate": {"origins": ["https://www.python.org"], "expires_in_minutes": 20},
 "start_url": "https://www.python.org/downloads/",
 "values": [{"name": "latest_version", "type": "text", "description": "the latest Python 3 release version"}],
 "include_page_text": true}
```

## What live testing found

Grouped by what broke, not by the day it broke. Run numbers point into [the run log](#the-runs).

### Reading a page

| What happened | Why | What changed |
|---|---|---|
| A verified run said the Eiffel Tower is 300 m (run 3) | Values were read from the first 3,000 characters, which ended inside the site's menus | Values and the final answer come from the page's `main`, `[role=main]` or `article` text, up to 12,000 characters, redacted before truncation |
| A verified run gave no answer at all (run 12) | Answer extraction only ran after a page observation, and that run finished before observing one | The answer is read from the live page either way, with the URL redacted under a mandate |
| "3.15" came back as the latest Python release (run 15) | The reader took a pre-release row; nothing in code could tell it was wrong | Values carry shape checks — `pattern`, `min`, `max` — enforced in code. A value that fails counts as not read |
| Asked for the **newest** commit on a page of commits, the reader returned one from the middle — identically, three times at temperature 0 (run 29) | It returns the **last** match in whatever text it is given. Trimming the page only moved the wrong answer: at 3,875 characters it picked the one at 3,609; at 1,200 the one at ~1,150; at 600 the one at ~560. The right answer sat at 245 every time | A value can declare `pick: first` or `last`. The reader is asked to **list** every match in page order — which the same model does correctly — and code takes the end. 0 of 3 right became 3 of 3 right and verified |
| The caller's `pattern` was silently dropped in two of three python.org runs, and `pick` was ignored entirely | The planner is told which values the caller wants and writes its own specs for them, in its own words | The caller's spec replaces the planner's by name, and a requested value the plan never declares is read on the last step |

### Plans a small model writes

| What happened | Why | What changed |
|---|---|---|
| A sub-goal could never be proven (run 5) | The planner invented a search-results URL as evidence, and Wikipedia redirects searches straight to the article | URL checks that spell out a query string are dropped as guesses |
| "Extract the version number" sent the executor clicking away from the answer (run 6) | A read-only step with no evidence became a step to act on | Read-only sub-goals with no evidence fold into the step before, taking their values with them |
| Click-by-click plans stalled: "Click the search button" failed its own check once the search had already landed (runs 9, 16) | A click-level step's check stops being true the moment the page moves on | Click-level steps fold into the outcome they lead to. A failed step that carried the browser to a later step's page skips ahead instead of replanning |
| A replan produced sub-goals named "Step 1" and "Step 2" (run 19) | The planner returned sub-goals with no goal text | Goal text is also read from `description`, `sub_goal`, `task`, `name`, `title`, `objective`, `action` and `step`; a reply with none is an error |
| Steps planned to open a page the browser was already on | Nothing checked | Such a step counts as done before any action when its url check already passes — never with a request check |

A 7B planner still under-asks in the brief: it will assume a departure date rather than ask for one (runs 14, 17). The question rules and plan review push back; they don't fix it.

### Proving a step

| What happened | Why | What changed |
|---|---|---|
| Every Everest run had its search step judged by a model | A search step had no checkable outcome | A search step with a quoted term gets a text check for that term |
| A url check passed on a **404** (run 28) | A guessed URL that doesn't exist still puts the browser at the address the check asked for | The browser records the status its own document answered; a url check fails on an error page. An unknown status still passes |
| "Prove I entered New York" could never pass (run 27) | A value typed into a field is not page text — `inner_text` skips it | A text check also passes when a field on the page holds the text, read live at check time |
| A date check could never pass either | The site redisplays `2026-10-15` as `Thu, Oct 15` | A text check matches a date however a site writes it: `Oct 15`, `October 15`, `15 Oct`, `15 October`, `10/15/2026`, `15/10/2026`. A different day still fails |
| Runs that read the right value came back unverified (run 28) | A step whose only job is reading had no evidence, so a model judged it | Such a step gets a `{{$value}} was read` check |
| **Then a Tokyo run came back "done, verified" on a price from an advert** | That new check was given to *any* sub-goal with values and no evidence — including one whose job was to run a search. Reading any number satisfied it | The check is only for a step whose whole job is reading (read, find, get, locate, look up, check, extract, note, record, report, identify, determine, retrieve, copy, write down — and no mention of changing anything). A step that has to act first goes back to a model verdict, which is weaker and says so |

### Driving a real widget

Google Flights is where this was learned, and the same shape appears one layer at a time.

| What happened | Why | What changed |
|---|---|---|
| `select` raised an error on every city box, burning the whole step budget (run 23) | They are `role="combobox"` widgets, not `<select>` elements, and Playwright's `select_option` only works on a real `<select>` | `select` uses the browser's picker on a real `<select>`, and otherwise types, takes the suggestion, and falls back to Enter |
| Typing "Tokyo" produced "TokTokyoyo" | The field completes inline as you type, racing the keystrokes | A field that completes inline gets the whole value in one insertion. Granted values go in the same way, so a secret can't leak as prefixes |
| Both cities were entered three times with no effect | The page renders several identical inputs; only one is live | The observer skips elements covered by something else, keeping them when the check can't tell |
| Cities were typed and the search ran with neither (run 25) | These widgets commit only when a suggestion is chosen, and the executor reaches for `type`, which doesn't commit | See below — making `type` commit was measured and reverted |
| Both dates were typed and the search ran with neither | Same shape, one layer along: a date box keeps nothing until its picker's **Done** is pressed | A `type` that opens a dialog presses that dialog's own confirm control. Only a dialog that wasn't open before the typing — checked live, neither Wikipedia's search box nor python.org's opens one |

**Making `type` commit a suggestion: measured, then reverted.** It looked like the fix for the city boxes. Nine trial runs said otherwise:

| Task | Before | With typing that commits |
|---|---|---|
| eiffel | right + verified | 3/3 right + verified, 36 s |
| python-latest | right + verified | 3/3 right, 1/3 verified, 85 s |
| everest | right + verified, 5 steps | **0/3 done**, 0/3 verified, 25 steps, 440 s |

Wikipedia's search box becomes a `role="combobox"` the moment the controller clicks it and clears it, so every search turned into a click on Wikipedia's first guess; two runs ended on its search page with an **empty** query. The Tokyo run it was aimed at took no suggestions at all. Committing stays on `select`, and the tests pin both sides.

### Knowing what a run did

Two of the hardest failures were unreadable from their outcomes.

- **A sign-in that went nowhere** turned out to be one line: `navigate .../login.html`, then thirty-five rejections of `element_id 1 not on page (valid: [])`. The planner had guessed a URL that doesn't exist. The server 404s, its single-page-app fallback redirects to a 200 and rewrites the address back, so the browser sat at exactly the URL the check asked for with nothing rendered.
- **A run that signed in and then failed the sign-in step** was the agent clicking on into the menu and logging itself back out.

Run records now keep the executor's action trail, redacted — every action, and every blocked or refused one, one line each. An observation with no elements and no text also ends the attempt after one step back, instead of spending the budget choosing elements that aren't there.

### Noise

| What happened | Why | What changed |
|---|---|---|
| The executor was told about 11 blocked analytics beacons and got confused (run 2); python.org's error reporting retried a blocked POST 427 times (run 6) | Every block was reported | Everything is still blocked and recorded. The executor hears only about blocks its own action could have caused |
| Half the executor's history was mandate blocks — 49 of 95 entries, `jserror` 22 times (run 27) | A single-page app fires same-origin telemetry on every step, and those look like an app's own submit | Each blocked endpoint is reported to the executor once per run. After: 4 of 50 |
| MCP results listed hundreds of identical blocks | — | Blocks are grouped by kind and site with counts, and page-script traffic is flagged `background` |
| Five of forty steps were `type` actions with no text | Nothing checked | A `type` or `select` with nothing to enter is refused before it runs |
| `httpx` logged every model call into the host's server log | — | Quieted to warnings unless `-v` |

### Two that weren't about the web at all

- **The dashboard offered a model nobody had pulled.** Its model list was hardcoded, and the default config still named `qwen2.5:72b` — which no 16 GB Mac can run. A run picked it, and Ollama's 404 surfaced only after the brief, after the browser opened, and after two retries, as `Ollama returned 404 for qwen2.5:72b`. Now the list is filled from the machine's own Ollama, presets whose models aren't pulled are dimmed and labelled, and a 404 from Ollama becomes: *"Ollama hasn't pulled 'qwen2.5:72b'. It has: … Run `ollama pull qwen2.5:72b`, or choose one of those."* It isn't retried, and no browser opens. The defaults changed too: the dashboard now starts on the same one-model stack the README tells you to pull and the live tests use, and there's a preset that says so.
- **`-c configs/local-16gb.yaml` silently switched to Claude** once `ANTHROPIC_API_KEY` was set. A config passed with `-c` is now used as written.
- **qwen2.5-coder:7b rejects images with HTTP 400.** `vision: false` per role, and the Ollama backend retries once without images and remembers the model is text only. Ollama's default context also cut pages short, hence `context_window`.

### Three found by someone just trying it (2026-09-22)

A task typed into the dashboard — "find the speakers at the Jaipur Literature Festival 2027" — turned up three bugs in one run, two of them ours from the days before.

**A slow page was being called dead.** The empty-page rule added two days earlier fired on `jaipurliteraturefestival.org`, went back, and lost the page it wanted. The site isn't empty; it renders nothing for three and a half seconds:

```
t+ 2.6s  elements=  0  text=    0
t+ 3.6s  elements=  1  text=  385
t+ 5.6s  elements=  5  text= 2999
```

An empty observation is now waited on — re-observed every half second up to `agent.evidence_wait_seconds` — before the page counts as dead. Not until the network goes quiet, which was the first attempt: plenty of pages render from a timer with nothing in flight, and the local test site proved it. In the rerun `page_arrived_late` fired five times and `empty_page` never.

**A replan that came back unusable ended the run.** The planner returned sub-goals with no goal text, code dropped them (rightly), and the run stopped. First planning is retried twice; replanning wasn't. Now it is.

**And the expensive one: `{{$value}} was read` was blocking the already-there path.** A sub-goal whose url check already passes is counted done before any action — except that all of its checks have to pass, and a value check can't until the value is read, which happens a moment later in the same verify. So the day reading steps started proving they had read, every task that started on its answer page lost its zero-step path. `_already_there` now ignores value checks.

| Task | Blocked by the value check | After |
|---|---|---|
| eiffel | 7 steps, 107 s | **0 steps, 34 s** |
| python-latest | 3 steps, 88 s | **1.5 steps, 55 s** |
| everest | 12–16 steps, 233–312 s | **4 steps, 71 s** |

All six runs right and verified — the best numbers the three tasks have had. The regression had been sitting in two commits, passing every test, because nothing in the suite checked *how many steps* a task that starts on its answer takes.

**The task itself was unanswerable, and the run said so.** The festival's speakers page offers 2026, 2025, 2024, 2023 and 2022 — there is no 2027. The run reported "The page does not provide information about speakers at the Jaipur Literature Festival 2027", with **0 sub-goals verified**. It didn't invent a list. That is the right outcome, and the opposite of run 29, where a plausible wrong answer came back verified.

One thing it did badly: after failing, it replanned into eight sub-goals, several of them paragraphs of instructions — "Navigate to the … website and locate the section for the 2027 festival. Click on the link to go to the 2027 festival page." Those can't be judged done or not. Plan review now flags a sub-goal that runs past 25 words or more than one sentence.

## What the fixes do to the guarantees

- **The mandate is unchanged.** Every request blocked before is still blocked and recorded. Only what the executor is *told* changed.
- **Dropping a guessed url check weakens that sub-goal's proof.** It falls back to the validator model, and its receipt says so. That trades verification for finishing, and only for checks that were guesses.
- **Counting a step done early, skipping ahead, and stopping when a goal is proven all rest on checks evaluated against the live browser** — the same proof evidence uses. None of them passes over a required write.
- **Widening a text check to fields and date renderings** widens what counts as the same fact, not how strongly a page's claim counts. A text check was already the weakest evidence there is.

## The runs

In order. Fixes landed between runs, so each shows the code as it stood then.

### Step 5b: first contact with live sites (runs 1–13)

| # | Task | Path | Outcome | Steps | Time | What it showed |
|---|---|---|---|---|---|---|
| 1 | Eiffel Tower height on Wikipedia | CLI | Done; 330 m, judged by the model | 14 | 250 s | The loop works on a 7B model |
| 2 | Same, as a `number` value | MCP in-process | Done, not verified; 330 | 18 | 298 s | 11 analytics beacons blocked and each reported to the executor. The planner didn't know where the browser started |
| 3 | Same | MCP in-process | Verified, **wrong (300)** | 1 | 37 s | Values read from the site's menus |
| 4 | Same | MCP in-process | Verified; 330 | 1 | 60 s | Reading the main content fixed it |
| 5 | Everest via Wikipedia's search box | MCP stdio | Failed | 40 | 359 s | The stdio path works. A guessed search URL as evidence made the sub-goal impossible |
| 6 | Latest Python 3 release | MCP in-process | Done, not verified; 3.14.7 | 19 | 329 s | 427 blocked retries flooded the executor. "Extract the version number" sent it away from the answer |
| 7 | Everest, stricter planner prompt | MCP stdio | Stopped by hand | 16 | — | Prompting alone doesn't stop click-by-click plans or guessed URLs |
| 8 | python.org again | MCP stdio | Done; 3.14.7, 1 of 2 verified | 8 | 195 s | 122 blocks, all flagged as page-script traffic, 2 log lines |
| 9 | Everest, with plan tidying | MCP stdio | Failed | 40 | 517 s | The typing step failed its own check once the search had landed. This is what skip-ahead fixes |
| 10 | python.org again | MCP stdio | Verified; 3.14.7 | 0 | 42 s | Proven before any action, value read off the page |
| 11 | Everest, with skip-ahead | MCP stdio | Done; 8,848.86 m, 3 of 4 verified | 35 | 478 s | Finished, still click by click; no step failed, so skip-ahead never fired |
| 12 | Eiffel, the README's example | CLI | Done, verified, **no answer** | 0 | 7 s | Answer extraction only ran after an observation |
| 13 | Same | CLI | Done, verified; "330 meters (1,083 feet)" | 0 | 21 s | Fixed. Run 1 took 14 steps and 250 s |

### Step 6: thinking before acting (runs 14–21)

| # | Task | Tool | Outcome | Steps | Time | What it showed |
|---|---|---|---|---|---|---|
| 14 | Tokyo flights, read-only mandate | `brief_task` | A brief, and **no questions** | — | 15 s | It assumed a departure date rather than asking. A 7B planner under-asks |
| 15 | python.org | `brief_task` + `web_task` | Done and verified, **value "3.15"** | 0 | 70 s | Evidence proved the page was open; the reader took a pre-release row |
| 16 | Everest | `web_task` | Failed | 40 | 487 s | Four click-level steps; "Click the search button" failed three times. Led to plan review flagging click-level steps |
| 17 | Tokyo again, new question rules | `brief_task` | Two questions, both with defaults | — | 17 s | It now asks for dates. It still never asked where the trip starts |
| 18 | Everest, plan review on | `web_task` | Done; 3 of 4 verified | 27 | 442 s | One rewrite, merging two steps. Fastest Everest at the time |
| 19 | Everest, code folding click-level steps | `web_task` | Done | 21 | 471 s | A replan returned sub-goals with no goal text — a new bug |
| 20 | Everest, trial 2 | `web_task` | Done | 9 | 171 s | Three sub-goals, no rewrite |
| 21 | Everest, trial 3 | `web_task` | Done | 9 | 166 s | Nearly the same run |

Runs 19–21: 3 of 3 right, median 9 steps and 171 s, against 27 to 35 steps or failure before folding. None fully verified — the search step had no evidence yet.

### Step 7 onward: measured, and harder tasks (runs 22–31)

| # | Task | Outcome | Steps | Time | What it showed |
|---|---|---|---|---|---|
| 22 | Eiffel, starting on the article | Done and verified | 0 | 47 s | 16 s of it was the brief, which had nothing to ask |
| 23 | Tokyo flights | Failed | 40 | 542 s | The brief asked what runs 14 and 17 never did. `select` couldn't drive an autocomplete |
| 24 | The three known tasks, after the typing and observer fixes | All right **and verified** | 0 / 0 / 5 | 55 / 78 / 130 s | A check, not a measurement |
| 25 | Tokyo again | Failed | 27 | 469 s | 18 clicks, 9 types, **no `select` at all** — the commit path never ran |
| 26 | Tokyo, with typing that commits | Failed | 19 | 284 s | Suggestions taken: **0**. The change bought nothing where it was aimed, and cost Everest every run |
| 27 | Tokyo, with the user's notes | Failed | 40 | 477 s | The notes gave the best plan any config has produced for that site. 49 of 95 history entries were mandate blocks |
| 28 | Sign in to saucedemo, read a price | 1 of 4 done | 40 | 124 s | A guessed URL 404s into a blank single-page app; the url check passed on it |
| 29 | Newest commit on a GitHub commits page | 2/2 done and **verified**, both **wrong** | 0 | 35 s | The reader returns the last match in whatever text it gets |
| 30 | Sign-in, after the guessed-URL and evidence fixes | 3/3 right and verified | 14 | 63 s | From 40 steps and 218 s. No credentials re-entered, no logout |
| 31 | Tokyo, with dates supplied and the date fix | 0/2 done, **5 of 6 sub-goals proven** | 40 | 537 s | Both cities and both dates proven by code. What's left is running the search |

Values were checked against the live pages: 330 m, 3.14.7, and 8,848.86 m (the 2020 China–Nepal survey).

### One comparison worth keeping: a 14B planner

18 runs, `web-lobster trials trials/web.yaml -n 3` across two configs, 54 minutes.

| Task | Config | Done | Right | Verified | Median steps | Median time |
|---|---|---|---|---|---|---|
| eiffel | `local-16gb` | 3/3 | 3/3 | 3/3 | 0 | 59 s |
| eiffel | `local-16gb-14b-planner` | 3/3 | 3/3 | 3/3 | 0 | 122 s |
| python-latest | `local-16gb` | 3/3 | 3/3 | 1/3 | 3 | 129 s |
| python-latest | `local-16gb-14b-planner` | 3/3 | 1/3 | 0/3 | 10 | 273 s |
| everest | `local-16gb` | 1/3 | 1/3 | 1/3 | 8 | 167 s |
| everest | `local-16gb-14b-planner` | 3/3 | 3/3 | 3/3 | 5 | 193 s |

A bigger planner fixed multi-step planning and doubled the time on lookups, because Ollama swaps a 9 GB and a 4.7 GB model in and out. It didn't make values more reliable: those are read by the executor's 7B model in both configs. The 7B failures here were a planner-invented text check ("elevation of Mount Everest is", a phrase Wikipedia never shows) — still open as roadmap item 5.

Plan reuse (`local-16gb-plan-reuse`, shared memory) has not been measured; its first run was lost.

## Still weak

- **Driving a page.** A 7B executor picks the wrong verb and the wrong element on a page with more than one input. That is the last thing between web-lobster and the Google Flights task.
- **Sample sizes.** Three runs per setup leaves wide error bars, and some numbers here are one run.
- **Live sites change.** `python-latest` needs its expected version updated with each release; an outage reads as a failure.
- **Honest tasks only.** These runs show a local model doing real work, not how often it falls for a planted instruction. The benchmark's live mode hasn't run.
- **Claude configs have never been run live.** They need an API key.
