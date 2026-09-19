# Live testing

Until step 5, every result came from scripted models. This round ran real tasks on live sites with a local model, to find what breaks when web-lobster is used for real, and fixed what it found.

## Setup

- **Machine:** Apple M4 with 16 GB of memory, models served by Ollama.
- **Model:** qwen2.5-coder:7b as planner, executor, and validator ([`configs/local-16gb.yaml`](../configs/local-16gb.yaml)): text only, DOM mode, 16k context. gemma3:12b, a vision model, was also tried for reading values: it was slower and no more accurate.
- **Paths:**
  - `web-lobster run` on the CLI.
  - `web_task` in-process, through the MCP SDK's in-memory client.
  - `web-lobster mcp` over stdio as a subprocess, the way OpenClaw and Claude Code launch it.
- **Mandates:** read-only, one origin per task. Nothing was submitted, bought, or sent.

## Runs

In order. Fixes landed between runs, so each run shows the code as it stood then.

| # | Task | Path | Outcome | Steps | Time | What it showed |
|---|---|---|---|---|---|---|
| 1 | Eiffel Tower height on Wikipedia | CLI, no mandate | Done; right answer (330 m), judged by the model | 14 | 250 s | The loop works on a 7B model |
| 2 | Same, as a `number` value | MCP in-process | Done, not verified; 330 | 18 | 298 s | The mandate blocked 11 analytics beacons and reported each to the executor, which got confused. The planner didn't know the browser started on the article |
| 3 | Same | MCP in-process | Verified, **wrong value (300)** | 1 | 37 s | Values were read from the first 3,000 characters, which ended in the site's menus |
| 4 | Same | MCP in-process | Verified; 330 | 1 | 60 s | Reading the main content fixed it |
| 5 | Mount Everest's elevation via Wikipedia's search box | MCP stdio | Failed | 40 | 359 s | The stdio path works end to end. The planner set a guessed search URL (`/w/index.php?search=...`) as evidence, and Wikipedia never shows it: searches redirect to the article |
| 6 | Latest Python 3 release on python.org, as a `text` value | MCP in-process | Done, not verified; 3.14.7 | 19 | 329 s | python.org's error reporting retried a blocked POST 427 times, flooding the logs and the executor's history. The plan ended with "Extract the version number", which sent the executor clicking away from the answer |
| 7 | Everest again, with a stricter planner prompt | MCP stdio | Stopped by hand, stuck in the same loop | 16 | — | Prompting alone doesn't stop a 7B planner from writing click-by-click plans and guessing URLs |
| 8 | python.org again | MCP stdio | Done; 3.14.7, 1 of 2 steps verified | 8 | 195 s | After the noise fix, 122 blocks were all flagged as page-script traffic, with 2 warning lines in the log |
| 9 | Everest again, with plan tidying and "already there" | MCP stdio | Failed | 40 | 517 s | Sub-goal 1 was done at 18 s without acting. The plan still split the search into "type", "click search", and "click the article". Once typing and searching landed on the article, the typing step failed its own check, and the run never recovered. This is what skip-ahead fixes |
| 10 | python.org again | MCP stdio | Verified; 3.14.7 | 0 | 42 s | The browser started on the answer page, so the one sub-goal was proven before any action and the value read straight off the page |
| 11 | Everest again, with skip-ahead | MCP stdio | Done; 8,848.86 m, 3 of 4 steps verified | 35 | 478 s | The full search from the main page finished with the right value, checked as a number. The plan was still click by click ("type", "click search", "click the article"), and the last two steps took 30 steps between them. No step failed, so skip-ahead never had to fire |
| 12 | Eiffel Tower height, the README's CLI example | CLI, no mandate | Done, verified, but **no answer** | 0 | 7 s | The browser started on the article, so the one sub-goal was proven before any action. Answer extraction only ran after a page observation, and this run never made one |
| 13 | Same | CLI, no mandate | Done, verified; "The Eiffel Tower is 330 meters (1,083 feet) tall." | 0 | 21 s | Fixed: the answer is read from the live page whether or not the run observed one. Run 1 took 14 steps and 250 s |

The tower's height (330 m) and the Python release (3.14.7) were checked against the live pages. 8,848.86 m is Everest's official height from the 2020 China–Nepal survey.

## What was fixed

| Problem | Fix | Where |
|---|---|---|
| qwen2.5-coder:7b rejects images with HTTP 400 | `vision: false` per role. The Ollama backend also retries once without images and remembers the model is text only | `core/config.py`, `models/ollama_backend.py` |
| Ollama's default context cut pages short | `context_window`, sent as `num_ctx` | same |
| Values read from the site's menus | Values and the final answer come from the page's `main`, `[role=main]`, or `article` text, up to 12,000 characters, redacted before truncation | `browser/controller.py`, `core/orchestrator.py`, `models/extractor.py` |
| Planner planned steps to reach the start page | The planner is told where the browser starts: origin and path only, since query strings can carry tokens | `models/planner.py` |
| Guessed search-result URLs made sub-goals impossible | URL checks that spell out a query string are dropped | `tidy_sub_goals` in `models/planner.py` |
| "Extract the ..." steps sent the executor away from the answer | Read-only sub-goals with no evidence are dropped, and their values move to the step before | same |
| A replanned goal was named "Step 1" | Goal text is also read from `description`, `sub_goal`, `task`, and `name` | same |
| "Open the page" steps when the browser was already there | Such a step counts as done before any action when its url check already passes. Only for goals that open a page, and never with a request check | `_already_there` in `core/orchestrator.py` |
| A step like "Type the query" fails once the search lands on the result | When a failed step took the browser to a page that proves a later sub-goal (its url check passes now but didn't where the failed step began), the steps in between are skipped and the later one is completed on that proof. Never past a step with a request check, because skipped steps count toward completion | `_skip_to_later_goal` in `core/orchestrator.py` |
| Analytics and error reporting flooded the executor | Still blocked and recorded. The executor hears only about page loads, form posts, script writes back to the same site, data leaks, and expiry, with repeats collapsed | `worth_reporting` in `mandate/enforcer.py` |
| MCP results listed hundreds of identical blocks | Blocks are grouped by kind and site with counts, and page-script traffic is flagged `background` | `blocked_actions` in `mcp_server/service.py` |
| `-c configs/local-16gb.yaml` would silently switch to Claude once `ANTHROPIC_API_KEY` was set | A config passed with `-c` is used as written | `__main__.py`, `ui/server.py` |
| A run finished before any action gave no answer | The answer is read from the live page, with the URL redacted under a mandate, whether or not a page was observed | `_extract_answer` in `core/orchestrator.py` |
| httpx logged every model call into the host's server log | Quieted to warnings unless `-v` | `__main__.py` |

What these fixes do to the guarantees:

- **The mandate is unchanged.** Every request it blocked before is still blocked and recorded. Only what the executor is told changed.
- **Dropping a guessed URL check weakens that sub-goal's proof.** It falls back to the validator model, and its receipt says so. This trades verification for finishing, and only for checks that were guesses.
- **Counting a step done early and skipping ahead both rest on a url check** that passes against the live browser, the same proof evidence uses. Neither ever passes over a required write.

## Still weak

- A 7B planner still writes click-by-click plans. Tidying and skipping ahead recover some of them, not all.
- A step takes 5 to 15 seconds on this machine. Lookups that start on the right page finish in under a minute, but the Everest search from the main page took 8.
- These runs show a local model completing honest tasks, not how often it falls for planted instructions. The benchmark's live mode hasn't run yet.
- Claude configs weren't run live in this round.

## Thinking before acting (step 6)

The same machine and model, after [step 6](design/step-6-planner-briefing.md) added a brief, questions, and plan review before the browser opens. All runs over MCP stdio.

| # | Task | Tool | Outcome | Steps | Time | What it showed |
|---|---|---|---|---|---|---|
| 14 | "Find me a cheap round-trip flight to Tokyo", read-only mandate on google.com | `brief_task` | A brief with a sensible goal and assumptions, and **no questions** | — | 15 s | The planner assumed "current date as departure" rather than asking for dates or where the trip starts. A 7B planner under-asks: the prompt's "prefer a sensible assumption" won out over "ask what you can't sensibly assume" |
| 15 | Latest Python 3 release on python.org, read-only mandate | `brief_task`, then `web_task` with `brief_id` | Done and verified, but **the value was "3.15"** | 0 | 70 s, plus about 11 s for the brief | The brief rightly asked nothing and was reused without rethinking. Evidence proved the downloads page was open, but the extractor read the "3.15 pre-release" row of the active-releases table instead of the 3.14.7 download (runs 8, 10, and 13 read 3.14.7). "Verified" covers the steps, not whether a text value is right. The run was also 28 s slower than run 10, from longer prompts |
| 16 | Mount Everest's elevation from Wikipedia's main page | `web_task` | Failed | 40 | 487 s | The brief rightly asked nothing, but the plan was still four click-level steps: open the search box, type, click search, open the article. Plan review had nothing to flag, since four steps is under its limit. "Click the search button" failed its own check three times, and the article step never reached the article. Run 11 had the same plan shape and finished, so a single run is anecdote, not measurement. This run led to plan review flagging click-level steps |
| 17 | Run 14's Tokyo task again, after the question rules changed | `brief_task` | Two questions, departure and return date, each with a default (today, and a week later) | — | 17 s | Half fixed. It now asks for the dates instead of assuming them, and nothing blocks because both have defaults. It still never asked where the trip starts, even though the prompt names that case; it didn't assume a city, it just didn't think of one. A 7B planner follows a checklist unevenly |
| 18 | Everest again, with plan review flagging click-level steps | `web_task` | Done; 8,848.86 m, 3 of 4 steps verified | 27 | 442 s | Plan review made the planner rewrite its plan once (planning took 66 s, against 31 s in run 16), merging "open the search box" and "type" into one step. The rewrite still kept "Click the search button" and a separate "Extract the elevation" step, and code accepted it because it had no more issues than the original. This is the fastest Everest finish so far, but runs 11 and 16 had near-identical plans and went opposite ways, so one run per setup can't show the check caused it |
| 19 | Everest, trial 1 of 3 after code started folding click-level steps | `web_task` | Done; 8,848.86 m | 21 | 471 s | Planning took 78 s, including one rewrite ("Search the site for 'Mount Everest'"). The article step failed one of its two checks, a replan opened the article directly, and the replan's last two goals came back named "Step 1" and "Step 2": the planner returned sub-goals with no goal text. That's a new bug |
| 20 | Everest, trial 2 of 3 | `web_task` | Done; 8,848.86 m | 9 | 171 s | Three sub-goals, planned in 32 s with no rewrite: open the search box (already open), search for Mount Everest (judged by the model), and open the article (proven by both checks) |
| 21 | Everest, trial 3 of 3 | `web_task` | Done; 8,848.86 m | 9 | 166 s | The same plan as trial 2, and nearly the same run |

Runs 19 to 21 measure the folding code: 3 of 3 found the right answer, with a median of 9 steps and 171 s. Runs 11 and 18 took 35 and 27 steps, and run 16 failed. None of the three was fully verified, because the search step has no evidence and a model judged it. Three runs is still a small sample.

## Trying it out (2026-09-18)

Two runs from the command line on the same machine, after step 7.

| # | Task | Path | Outcome | Steps | Time | What it showed |
|---|---|---|---|---|---|---|
| 22 | Eiffel Tower height, starting on the article | CLI, no mandate | Done and verified; "330 meters (1,083 feet)" | 0 | 47 s | 16 s of it was the brief, which had nothing to ask. The sub-goal's url check already passed, so nothing was clicked, and the answer came from the article's main content |
| 23 | "Find me a cheap round-trip flight to Tokyo" on Google Flights | CLI, no mandate | Failed | 40 | 542 s | Two findings, below |

Run 23 is the first live test of a site that isn't a document, and it found both a good surprise and a bug:

- **The brief asked what runs 14 and 17 never did:** "What city are you departing from?", with New York as the default. The planner's question set varies between runs, so under-asking isn't constant.
- **`select` couldn't drive an autocomplete.** Google Flights' city fields are `role="combobox"` widgets, not `<select>` elements. The executor reasonably chose `select` for them, and `select` called Playwright's `select_option`, which only works on a real `<select>`. Every attempt raised an error and cost a step, so the search was never run and the task burned all 40 steps. The typing path already knew how to drive these widgets: it clicks, waits for the inner input to mount, clears it through React's setter, and types character by character so the autocomplete fires.

Driving the page by hand afterwards showed two more problems behind the first one:

- **Those fields complete text inline as you type.** Typing "Tokyo" character by character, as the controller did, raced the field's own completions and left "TokTokyoyo" in the box. With garbage in the field no suggestion matched, so nothing could be chosen.
- **The page renders several identical inputs.** "Where to?" existed twice on a fresh page, and eight "Where…" inputs appeared after a few interactions, only one of them live. Typing into a dead one does nothing and reports no error, which fits run 23 entering both cities three times with no effect.
- **Committing needs a suggestion.** These widgets only accept a city once an option from the `role="option"` list is chosen. Enter alone does nothing when no option matches.

**Fixed:** `select` uses the browser's picker on a real `<select>`, and otherwise types the text and takes the suggestion with Enter. The guards that cover typing now cover `select` too: the mandate's data-entry check already did, and the safety gate's sensitive-field check and the mandate-approves path were extended to match. `tests/sites.py` gained a real dropdown and a Google-Flights-style autocomplete to test both paths.

Typing into a field that completes inline now goes in as one insertion rather than key by key, and `select` takes the first suggestion by clicking it, falling back to Enter. The observer skips elements covered by something else, keeping them whenever the check can't tell. Plain typing deliberately still does **not** commit a suggestion, so the measured Wikipedia and python.org tasks keep behaving as they did.

| # | Task | Path | Outcome | Steps | Time | What it showed |
|---|---|---|---|---|---|---|
| 24 | Eiffel, python.org, and Everest, one run each, after the typing and observer fixes | `trials` | All three right **and verified** | 0 / 0 / 5 | 55 / 78 / 130 s | No regression from hiding covered elements. python.org came back verified, where the first comparison managed 1 of 3. One run each, so this is a check, not a measurement |
| 25 | Tokyo again, with all three fixes | CLI, no mandate | Failed | 27 | 469 s | 18 clicks and 9 types, and **no `select` at all**, so the suggestion-commit path never ran. The executor keeps choosing `type` for these fields, and `type` doesn't commit by design |

**What's still in the way:** the gesture that commits an autocomplete lives only on `select`, and a 7B executor reaches for `type`. Making `type` commit whenever a combobox is showing suggestions would likely fix Google Flights, but it changes what typing means everywhere, including the Wikipedia search step in the measured Everest task. That's a change to make with the trials tool, not by assumption.

### Making `type` commit: measured, then reverted

Nine trial runs of the three known tasks on a build where `type` took the suggestion whenever the focused field looked like a combobox, plus one more Tokyo attempt.

| Task | Config | Done | Right | Verified | Median steps | Median time |
|---|---|---|---|---|---|---|
| eiffel | `local-16gb` | 3/3 | 3/3 | 3/3 | 0 | 36 s |
| python-latest | `local-16gb` | 3/3 | 3/3 | 1/3 | 3 | 85 s |
| everest | `local-16gb` | **0/3** | 1/3 | **0/3** | 25 | 440 s |

| # | Task | Path | Outcome | Steps | Time | What it showed |
|---|---|---|---|---|---|---|
| 26 | Tokyo again, with typing that commits | CLI, no mandate | Failed | 19 | 284 s | Suggestions taken: **0**. Google Flights' city fields never offered a `role="option"` list to take in that run, so the change bought nothing where it was aimed |

**Everest is the cost.** It had been 1 of 1 right and verified in run 24, in 5 steps. Under this change no run finished, two returned no elevation at all, and the median run took 25 steps and over 7 minutes. The receipts show two of the three runs left on `en.wikipedia.org/w/index.php`, once with the URL reading `?search=&title=Special%3ASearch` — the search page with **nothing in the query**. One of those failed its search step on a text check for "Mount Everest" that wasn't on the page, because nothing had been searched for. The third run did reach the article, and then spent its steps on a model-judged "find the elevation" sub-goal that returned not achieved with confidence 0 every time.

Driving Wikipedia by hand afterwards showed why it engaged there at all. The search box is a plain `<input name="search">` on a freshly loaded page, but once the controller has clicked it and cleared it through React's setter, the widget hydrates into `role="combobox"` with `aria-autocomplete="list"` — indistinguishable, to the check, from a Google Flights city box. So every Wikipedia search became a click on whatever Wikipedia suggested first, and the step that was supposed to run a search stopped running one.

**Reverted.** Committing a suggestion stays on `select`, where every known task came back right and verified. `tests/test_select_action.py` now pins the decision from both sides: `select` takes the suggestion, `type` deliberately leaves the field uncommitted. Google Flights stays unsolved by design — the executor prefers `type` for these fields, and making `type` commit costs three known tasks to fix one unknown. The next thing to try is the other end: teaching the executor to reach for `select` on a combobox, which changes one model's choice rather than what typing means everywhere.

### Three harder tasks (2026-09-18)

Tasks chosen to be less like reading an encyclopedia: a site you have to drive, a site you have to sign in to, and a page whose answer is one item in a list of near-identical ones.

| # | Task | Runs | Outcome | Steps | Time |
|---|---|---|---|---|---|
| 27 | Tokyo again, with notes about how Google Flights works, supplied by the user | 1 | Failed | 40 | 477 s |
| 28 | Sign in to saucedemo.com and read a product price, with the credentials as `{{placeholders}}` from the environment | 4 | 1/4 done | 40 | 124 s |
| 28b | The same, after the two fixes below | 3 | **3/3 done and right**, 1/3 verified | 35 | 158 s |
| 29 | The newest commit on this repository's `main` branch, from GitHub's commits page | 2 | 2/2 done and **verified**, both values **wrong** | 0 | 35 s |

**27 — the notes fixed the planning, and planning wasn't the problem.** With the user's notes in the brief, the 7B planner wrote a one-step plan — "Search Google Flights for a round-trip from New York to Tokyo on \<dates\>" — with real, checkable evidence: `page text contains "Flight results"`. That is the best plan any config has produced for that site, and it's the answer to whether user-supplied notes help: they do, at the step where they're read. The brief's own thinking had absorbed them — "clear the default departure city, type in 'Tokyo', and select the matching suggestion" — which is the site's actual gesture, not a generic plan. The run still failed, because the executor couldn't get the search to run. The wall is the one the reverted change was aimed at, and it's in the executor, not the plan.

**28 — signing in works, and a guessed URL is what breaks it.** One run in three signed in with `{{username}}` and `{{password}}`, opened the Sauce Labs Backpack, and returned `backpack_price` 29.99, right and verified, in 36 steps (7 in a later run). Neither the username nor the password appears anywhere in the run records. The failures were all the same failure, and reading them is what the action trail was added for:

```
1. navigate https://www.saucedemo.com/login.html
2. wait
...
5. wait (element_id 1 not on page (valid: []))
6. wait (element_id 2 not on page (valid: []))
```

`https://www.saucedemo.com/login.html` doesn't exist — the login form is at `/`, where the run already started. The planner guessed the URL, its url check demanded it, and the browser went. What happens next is worth spelling out, because it defeats the obvious fix:

1. The server answers **404**.
2. The 404 page is a single-page-app fallback: it redirects to `/?/login.html`, which answers **200**, and rewrites the address back to `/login.html`.
3. So the browser sits at exactly the URL the planner guessed, with a 200 status, and **nothing rendered**.

The url check passed — right address, no error — and the executor then chose elements that weren't there for 35 of the remaining steps, because the observation was empty every time.

**Two fixes, and only the second one would have caught this.**

- The browser records the status its own document answered, and a url check fails on an error page: "page is at …/login.html, which answered 404". A status that isn't known — no navigation during that sub-goal — still passes, so nothing that worked before changes. This closes a real hole: a url check proved an address, not a page. It does **not** close saucedemo's, because that redirect leaves a 200 behind.
- An observation with no elements and no text ends the attempt. The agent goes back one page; if that page is empty too, the sub-goal fails and the plan changes, instead of 35 steps of choosing elements that aren't there.

The test site gained both shapes: a `/login.html` that 404s, and a `/nothing` that answers 200 with an empty body.

**Measured after the fixes: 3 of 3 done and right**, against 1 of 4 before, with one run fully verified and the other two carrying a model-judged sub-goal. Two of the three runs guessed `/login.html` again, and the trail shows the recovery working:

```
1. navigate https://www.saucedemo.com/login.html
2. go_back (Nothing on the page)
3. navigate https://www.saucedemo.com/login.html
4. go_back (Nothing on the page)
5. type (el=1) "{{username}}"
6. type (el=2) "{{password}}"
7. click (el=3)
```

`secret_sauce` appears in none of the run records, in any of the seven runs.

**No regression on the known tasks** after both fixes: Eiffel, python.org, and Everest each came back right in one run, two of the three verified (0, 0, and 8 steps; 50, 76, and 175 s). Everest's search step was judged by a model that run, which is the usual 7B variance rather than anything new.

**29 — a verified run with the wrong answer, which is the failure that matters most.** Both runs opened the commits page, passed their url check, and returned `latest_commit` as "Fix premature done, broken replan loop, and add answer extraction", a real commit from weeks earlier, sitting in the middle of the same page. The newest one was "Type into autocompletes in one go, and hide covered elements". This is exactly what shape checks can't catch: the value is a well-formed commit subject, from the right page, of the right type. Nothing about it is malformed — it's just not the newest, and "newest" is a property of where it sits in the list, which is what flattening the page to text throws away. A value that means "the first one" needs to be read positionally, not described to a model in prose.

### Reading the three failures, and fixing what they showed (2026-09-19)

Run records now carry the executor's action trail, so each failure could be read rather than guessed at. Each of the three turned out to be a different bug.

**Tokyo: five things, and the browser layer isn't one of them.** Driving Google Flights through the controller by hand entered both cities, took both suggestions, and clicked Search — the widgets work. Two theories died on contact: a read-only mandate blocks neither the autocomplete (5 suggestions appear with it on) nor the search. What's actually wrong:

1. **The task has no answer.** "A cheap round-trip flight to Tokyo" names no dates, and Google Flights won't search without them. With both cities filled and Search clicked, the URL becomes a real `tfs=…` search URL and the page still shows the home page.
2. **The only prices on that page are adverts** — `$126`, `$170`, `$268` are promo cards for Atlanta departures. A run that reads a price there reads an advert.
3. **The evidence could never pass.** The check was `page text contains "New York"`, and a value typed into a field is not page text: after typing, `input_value` is "New York" while `inner_text(body)` doesn't contain it. That applies to every form-filling task, not just this one.
4. **The executor** picks `type` (which never commits a suggestion), clicks blindly, and emitted **5 `type` actions with no text at all**.
5. **Half its context was noise:** 49 of 95 history entries were repeated mandate blocks — `jserror` 22 times, `batchexecute` 21 — because `worth_reporting` treats any same-origin fetch write as possibly the agent's own doing, and a Google SPA fires telemetry on every step.

**The sign-in: it logs itself back out.** Beyond the guessed URL, the receipts show sub-goal "Sign in" failing with the page back at `/` after the executor clicked around the inventory page, then retyping credentials — including `type (el=2) "{{password}}"` followed by `type (el=2) "{{username}}"`, both into the password field. And the last sub-goal ("Find the price") carried no evidence at all, so it fell to a model verdict: that is why runs that were right came back unverified.

**GitHub: the reader takes the last one, not the first.** The commits arrive in page order with the answer at character 245, so nothing is lost in flattening — the model simply returns the last matching item in whatever window it is given, identically every time at temperature 0:

| Text given to the reader | Answer returned | Its position |
|---|---|---|
| All 3,875 characters | "Redesign dashboard UI…" | 3,609 |
| First 1,200 characters | "Think before acting…" | ~1,150 |
| First 600 characters | "Make select work on autocomplete fields…" | ~560 |

Trimming the page doesn't help; it only moves the wrong answer. But the same model asked to **list** the commits in page order gets the order right, 2 of 2.

### What changed

| Fix | Why |
|---|---|
| A `text` check also passes when a **field on the page holds** the text | A value typed into a field isn't page text, so "prove I entered New York" was unsatisfiable |
| A value can declare `pick: first` or `last`; the reader is asked to **list** every match in page order and **code takes the end** | Picking one of many is what a small model gets wrong; ordering them is what it gets right |
| **The caller's value spec wins** over the planner's, by name, and a requested value the plan never declares is read on the last step | The planner rewrites the caller's values in its own words and drops their `pattern`, bounds, and `pick` |
| A step whose only job is reading gets a **value check** (`{{$x}} was read`) instead of a model verdict | A run that read the right value still came back unverified |
| The executor hears about each blocked endpoint **once per run** | Every block is still recorded and counted; the 22nd `jserror` taught it nothing |
| A `type` or `select` with no text is refused before it runs | Typing nothing clears the field the last step filled, and costs a step |

### What it did to the numbers

| Task | Before | After |
|---|---|---|
| GitHub newest commit | 0/3 right, **3/3 verified and wrong** | **3/3 right and verified**, 2 steps median |
| Tokyo, with dates and notes | 1 sub-goal, none completed, 40 steps | **3 of 5 sub-goals proven**, both cities entered |
| Sign in and read a price | 3/3 right, **1/3 verified** | 2/3 right, **2/3 verified** — every run that finished was proven |
| Eiffel / python.org / Everest | right, 2 of 3 verified | right, 2 of 3 verified (no regression) |

The Tokyo run is the clearest read on the fixes, because three of them show up in one trail:

```
2. "Clear the 'Where from?' field and type 'New York'"  ✓ found on the page
3. "Clear the 'Where to?' field and type 'Tokyo'"       ✓ found in a field on the page
4. "Set the Departure date to 2026-10-15"               ✗ not found on the page or in its fields
```

On the sign-in task the change is in what "verified" covers: the step that reads the price now proves itself with `{{$backpack_price}} was read` instead of a model's opinion, so both runs that finished came back fully verified. The one that didn't finish is the wander — it signed in, clicked on, and logged itself back out.

Sub-goal 3 is the check that could never have passed before. Mandate-block noise fell from **49 of 95** history entries to **4 of 50**, and there were no empty `type` actions at all. What stops it now is narrower than anything above: it typed `2026-10-15` into the date box, and Google Flights reformats the date it shows, so a text check for the ISO string can't match. Dates written the way a site writes them are the next thing in the way.

## Measured with `web-lobster trials` (step 7)

The same machine, after [step 7](design/step-7-trials-and-planners.md) added shape checks on values, text checks for search steps, and the trials tool. `web-lobster trials trials/web.yaml -n 3` ran each task three times under each config, interleaved by run, with fresh memory for every run and each value scored against its known answer. Mandates were read-only.

### Part A: 7B for every role against a 14B planner

18 runs in 54 minutes.

| Task | Config | Done | Right | Verified | Median steps | Median time |
|---|---|---|---|---|---|---|
| eiffel | `local-16gb` | 3/3 | 3/3 | 3/3 | 0 | 59 s |
| eiffel | `local-16gb-14b-planner` | 3/3 | 3/3 | 3/3 | 0 | 122 s |
| python-latest | `local-16gb` | 3/3 | 3/3 | 1/3 | 3 | 129 s |
| python-latest | `local-16gb-14b-planner` | 3/3 | 1/3 | 0/3 | 10 | 273 s |
| everest | `local-16gb` | 1/3 | 1/3 | 1/3 | 8 | 167 s |
| everest | `local-16gb-14b-planner` | 3/3 | 3/3 | 3/3 | 5 | 193 s |

What the run records show:

- **Everest, 7B:** the two failures (20 and 8 steps) broke on the same step. "Open the article about Mount Everest" had two checks. The url check passed, and the browser was on the article, but the planner's text check for "elevation of Mount Everest is" failed, because Wikipedia never uses that phrase. The search step, now with a text check for its term, was proven by code in every run.
- **Everest, 14B planner:** the same three-step plan every time (open the search page, search for 'Mount Everest', open the article), with only a url check on the article. All three runs were fully verified, in 5 steps each.
- **Eiffel Tower:** perfect under both configs. The 14B config took twice as long, because Ollama swaps the 9 GB planner model and the 4.7 GB executor model in and out.
- **python.org, 7B:** the typed value was 3.14.7 in all three runs, but the free-text answer said "3.15" all three times. In two runs the planner declared `latest_version` itself, without a pattern, and that replaced the trial's version with its pattern: a bug.
- **python.org, 14B planner:** the free-text answer, written by the 14B model, said 3.14.7 all three times. The typed value, read by the 7B executor model, came back with nothing that passed the version pattern in two runs, one of which never left the downloads page. Those runs reported the value as missing rather than wrong, and weren't verified. Why the reader failed there isn't explained yet.
- **Blocked requests** on python.org ranged from 61 to 585 per run, all of them the page's own scripts.

### Part B: plan reuse

`local-16gb-plan-reuse` with shared memory, so later runs of a task can reuse a plan that worked earlier, was still running when this was written.

## Reproduce

```bash
ollama pull qwen2.5-coder:7b
web-lobster run -c configs/local-16gb.yaml -u https://en.wikipedia.org/wiki/Eiffel_Tower "How tall is the Eiffel Tower?"
```

Over MCP, start `web-lobster mcp -c configs/local-16gb.yaml` from your client and call `web_task` with, for example:

```json
{"task": "Find the version number of the latest Python 3 release on python.org.",
 "mandate": {"origins": ["https://www.python.org"], "expires_in_minutes": 20},
 "start_url": "https://www.python.org/downloads/",
 "values": [{"name": "latest_version", "type": "text", "description": "the latest Python 3 release version"}],
 "include_page_text": true}
```
