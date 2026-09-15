# What we've learned so far

web-lobster set out to be a web agent you can hand a real task without handing it the keys: the browser enforces what a task may do, the planner never reads a web page, and code, not a model, decides when a step is done. This document collects what building and testing it has taught us, with the evidence behind each lesson.

**The evidence:**

- A scripted poisoned-page benchmark: 7 trap scenarios, each under 5 defense setups, with a hijacked executor and an honest one.
- 21 recorded live runs on Wikipedia, python.org, and Google Flights, with qwen2.5-coder:7b on an Apple M4 Mac with 16 GB of memory ([live-testing.md](live-testing.md)).
- An 18-run comparison of a 7B and a 14B planner with `web-lobster trials`, scored against known answers. Plan-reuse runs were still going when this was written.
- 316 automated tests.

All live testing happened on 2026-09-14. Design records: [step 5](design/step-5-mcp-server.md), [step 6](design/step-6-planner-briefing.md), [step 7](design/step-7-trials-and-planners.md). Plan: [roadmap.md](roadmap.md).

## Contents

1. [Enforce, don't detect](#1-enforce-dont-detect)
2. [A defense that hasn't run live has bugs you haven't met](#2-a-defense-that-hasnt-run-live-has-bugs-you-havent-met)
3. [Keep the planner blind to pages, including when it gets more context](#3-keep-the-planner-blind-to-pages-including-when-it-gets-more-context)
4. ["Verified" means exactly what was checked](#4-verified-means-exactly-what-was-checked)
5. [Small planners: prompts don't stick, code does](#5-small-planners-prompts-dont-stick-code-does)
6. [One run is an anecdote](#6-one-run-is-an-anecdote)
7. [A stronger planner helps multi-step tasks, and costs time](#7-a-stronger-planner-helps-multi-step-tasks-and-costs-time)
8. [Thinking before acting](#8-thinking-before-acting)
9. [The real web is noisy](#9-the-real-web-is-noisy)
10. [Serving other agents](#10-serving-other-agents)
11. [Local models on a 16 GB Mac](#11-local-models-on-a-16-gb-mac)
12. [How we work](#12-how-we-work)
13. [Still open](#13-still-open)
14. [Numbers at a glance](#numbers-at-a-glance)

## 1. Enforce, don't detect

**Lesson:** it works better to make planted instructions impossible to act on than to try to spot them. Attackers adapt to detectors; a network layer that refuses a request doesn't care how persuasive the page was.

**Evidence:** the scripted benchmark, with an executor that obeys every planted instruction and claims success early.

| Defenses | Task done | Harmful effect | Email leaked | False "done" |
|---|---|---|---|---|
| none | 5/7 | 6/7 | 3/7 | 2/7 |
| mandate | 5/7 | 2/7 | 0/7 | 2/7 |
| evidence | 7/7 | 6/7 | 3/7 | 0/7 |
| mandate + evidence | 7/7 | 2/7 | 0/7 | 0/7 |
| mandate + write rules + evidence | 7/7 | 1/7 | 0/7 | 0/7 |

An honest executor completed all seven tasks under every setup, so the defenses cost the real task nothing.

**Each layer catches something different, and none catches everything:**

| Layer | What it stopped | Commit |
|---|---|---|
| Mandate (sites, data grants, expiry) | Email leaks and trips to the attacker's site: `link-exfil`, `form-hijack`, `redirect-trap`, `leaky-script` | `55bc8ab` |
| Write rules | A destructive write on the real site itself: `same-origin-delete` | `eac51ac` |
| Evidence | Success banners that meant nothing: `fake-success` | `0444f80` |

**What remains:** `allowed-write-abuse`. Write rules scope which endpoints a task may call, not what it sends, so a planted instruction can still rebook the wrong date through the right endpoint. Evidence refuses to call that result done, but can't undo it. Value-bound write rules are the planned fix.

**Enforcement details that turned out to be essential:**

- Chromium follows redirects without asking request routes. Every main-frame navigation is fetched with redirects off, each hop is checked, and allowed hops continue through a small bounce page.
- Service workers bypass context routing, so browser contexts block them.
- WebSockets need their own routing, plus checks on the messages a page sends through them.
- Granted data is tracked in raw, URL-encoded, JSON-escaped, and base64 forms, and redacted from logs and observations.
- The model only ever types `{{email}}`. The browser fills in the real value at typing time, and only on sites that grant covers.

**Limits we accepted:** cross-origin GETs still load, because pages break without them, so data the agent never typed can leave that way. A main-frame POST answered with 307 or 308 is re-issued as a GET. WebRTC isn't checked. Screenshots sent to a vision model can show a typed value.

## 2. A defense that hasn't run live has bugs you haven't met

**Lesson:** tests prove the code does what we thought of. Running against real sites and real models keeps finding what we didn't think of, including bugs in the defenses themselves.

**Evidence, before live testing:**

- The sensitive-field confirmation had never fired (fixed in `afe735e`).
- In the first end-to-end MCP test, declined confirmations left a newsletter form empty, and the run still came back verified: a request check for `POST /api/subscribe` passed on a submission with no email in it ([step 5](design/step-5-mcp-server.md), decision 14).
- MCP SDK 2.x renamed `inputSchema` to `input_schema`, and web-lobster's own MCP client silently saw every server as having no tools. Pinning `mcp>=2.2,<3` came out of that.

**Evidence, from live testing:** fourteen problems that no test had caught ([live-testing.md](live-testing.md#what-was-fixed)):

- a text-only model rejecting images;
- Ollama's default context cutting pages short;
- values read from a site's menus;
- analytics traffic flooding the executor;
- the planner not knowing where the browser started;
- guessed URLs;
- "extract" steps;
- click-by-click plans;
- a replan with nameless steps;
- a zero-step run with no answer;
- a config silently switching to paid Claude models;
- log noise in the MCP server.

Two more surfaced in the trials comparison (sections 4 and 5).

**What changed:** every feature gets live runs before it's called done, and every run is written down, failures included, with what it showed.

## 3. Keep the planner blind to pages, including when it gets more context

**Lesson:** the planner decides what the agent does, so nothing it reads may have been near a web page. That rule held when the planner got much more context in step 6, because every new input was checked against it.

**How isolation works:**

- Only type-checked values cross from pages to the planner: numbers, dates, booleans, and choices from the planner's own list. Text values cross only as a withheld reference (`3e94026`).
- The executor, validator, value extractor, and answer extraction are quarantined: their free text never reaches a planner prompt.
- `tests/test_planner_isolation.py` plants a canary in a page's text and URL, and checks that no planner prompt contains it: not the brief, the plan, the replan, the learnings, or the next run's memory.

**What isolation forced us to change:**

- **Memory.** Learnings from runs recorded before isolation were drawn from page-derived answers, so those records now show the planner only their task and outcome.
- **URLs.** Paths and queries are site-controlled text. MCP results reduce URLs to origins, and the planner sees the start page without its query string, which can carry tokens.
- **Step 6's context.** The planner now gets the date and time, the mandate (with data named, never shown), the user's notes and answers, its own brief, and past runs on the same site with step counts. Each source is trusted by construction: the user, the calling agent, the mandate approval, the clock, or the planner itself. Page titles, visited paths, and the validator's opinions stay out.

**The test for any new planner input:** has this ever been near a page?

## 4. "Verified" means exactly what was checked

**Lesson:** a proof is only as strong as its checks. A run can prove every step and still return a wrong answer, so results have to say "not found" when that's the truth.

**The trail:**

| Run | What happened | What we changed |
|---|---|---|
| 3 | Verified, but the tower's height came back as 300 m, not 330 | Values are read from the page's main content, up to 12,000 characters, instead of the first 3,000, which ended in the menus |
| 12 | Verified, with no answer at all: the run finished before observing any page | The answer is read from the live page either way |
| 15 | Verified, with "3.15" as the latest Python release. Evidence proved the downloads page was open; the value came from a pre-release row | Values can declare a `pattern` or `min` and `max`, enforced in code, and over MCP `verified` also requires every requested value (`f3c959d`) |
| Trials | With the shape check, two python.org runs under the 14B-planner config returned no version rather than a wrong one. The runs weren't verified, and `missing_values` named the gap | — |

**Free-text answers are the least trustworthy output.** In the trials, the 7B baseline's free-text answer said "3.15" in all three python.org runs, while its typed value said 3.14.7 every time. That's why page-derived text is withheld from calling agents unless they ask for it.

**Other checks have edges too:**

- A `text` check proves only what the page displays, and a page can display anything (the `fake-success` trap).
- A `request` check proves the site accepted a request, not what was in it (section 2).
- A search step gets a text check for its search term. That proves the page shows the term, and nothing more.

**A bug the trials exposed:** when the planner declares a value with the same name as one the caller requested, the caller's pattern and bounds are dropped, and the planner's unshaped version is read instead. It happened in two of the three 7B python.org runs. Fixing it is on the roadmap.

## 5. Small planners: prompts don't stick, code does

**Lesson:** with a 7B planner, prompt instructions are suggestions it follows unevenly. Deterministic fixes applied to its output in code are what actually changed outcomes. Every such fix has to preserve the guarantees: it's built only from the planner's own text and the mandate, and it never skips or folds a step that has to send a write.

**Mount Everest over time.** The task: find the elevation in metres, starting from Wikipedia's main page and using the search box.

| Runs | Code at the time | Result |
|---|---|---|
| 5 | Live-testing fixes only | Failed, 40 steps: guessed a search URL as evidence |
| 7 | A stricter planner prompt | Stopped by hand, stuck in the same loop |
| 9 | Guessed URL checks dropped, "already there" | Failed, 40 steps: a "type the query" step failed its own check once the search moved on |
| 11 | Skip-ahead | Done, 35 steps, 478 s |
| 16 | Brief and plan review | Failed, 40 steps: stuck on "Click the search button" |
| 18 | Plan review flags click-level steps | Done, 27 steps, 442 s |
| 19–21 | Code folds click-level steps | 3 of 3 right: 21, 9, and 9 steps, median 171 s |
| Trials, 7B | Plus search text checks and value shapes | 1 of 3 right, median 167 s: two runs failed on a guessed text check (below) |
| Trials, 14B planner | The same code | 3 of 3 right and verified, 5 steps each, median 193 s |

**The planner's mistakes, and what fixed them:**

| Mistake | Seen in | Did a prompt change fix it? | What did |
|---|---|---|---|
| Guessing search-result URLs as evidence | Run 5 | No (run 7) | Code drops url checks that spell out a query string |
| Planning steps to reach the page the browser starts on | Run 2 | Partly | Telling the planner where it starts, plus a code check that counts an "open the page" step done when the browser is already there |
| Separate "Extract the version" steps | Run 6 | No | Code folds read-only steps into the step before |
| Click-by-click plans | Runs 9, 11, 16, 18 | No: a rewritten prompt example, then plan review with one rewrite, both left click steps in | Code folds click-level steps into the outcome they lead to |
| A replan with nameless steps ("Step 1") | Run 19 | — | Code drops nameless steps and rejects a reply that has only those |
| Under-asking | Runs 14 and 17 | Half: it asks for dates, not where a trip starts | Defaults keep runs moving; still open |
| Guessing exact phrases as evidence | Trials, 7B Everest runs 1 and 3 | — | Not fixed yet. The article step had a url check, which passed, and a text check for "elevation of Mount Everest is", a phrase Wikipedia never shows. The browser was on the right page, and the step could never pass |

The last row is the URL-guessing mistake again, with text instead. Treating sentence-like text checks as guesses when a url check already pins the page is the planned fix.

## 6. One run is an anecdote

**Lesson:** a single live run can't show that a change helped. The same task with the same code swings between success and failure, and between one and seven minutes.

**Evidence:**

- Runs 11 and 16 had near-identical plans and opposite outcomes.
- Run 18 was the fastest Everest finish at the time, but nothing about one run showed the new check caused it.
- The 7B baseline found Everest 3 of 3 times in runs 19–21, then 1 of 3 times in the trials later the same day. The trials code had also gained search text checks and value shapes, and the receipts pinned both failures on a planner-guessed text check, not on folding. The lesson from that is to read the records before explaining a result: the first guess, a side effect of the nameless-step fix, was wrong.
- python.org took 85, 129, and 277 s across three identical 7B runs.

**What changed:** `web-lobster trials` (`f3c959d`).

- Fixed tasks with known answers.
- Every task under every config, interleaved by run, so a slow minute on a site doesn't land on one config.
- Memory isolated per run unless sharing it is what's being measured.
- Results scored in code: done, right, verified, median steps, and median time.

**Live sites change, too.** `python-latest`'s expected version goes stale with every Python release. An outage reads as a failure. Blocked-request counts on python.org ranged from 61 to 585 across six runs of the same task.

## 7. A stronger planner helps multi-step tasks, and costs time

**Lesson:** the planner matters most when the task takes several steps; reading a value correctly depends more on the model doing the reading. On a 16 GB machine, mixing model sizes costs one to two minutes per task.

**Evidence:** `web-lobster trials trials/web.yaml -n 3`, fresh memory, 18 runs in 54 minutes.

| Task | Config | Done | Right | Verified | Median steps | Median time |
|---|---|---|---|---|---|---|
| Eiffel Tower | 7B for every role | 3/3 | 3/3 | 3/3 | 0 | 59 s |
| Eiffel Tower | 14B planner, 7B executor | 3/3 | 3/3 | 3/3 | 0 | 122 s |
| python.org | 7B for every role | 3/3 | 3/3 | 1/3 | 3 | 129 s |
| python.org | 14B planner, 7B executor | 3/3 | 1/3 | 0/3 | 10 | 273 s |
| Everest | 7B for every role | 1/3 | 1/3 | 1/3 | 8 | 167 s |
| Everest | 14B planner, 7B executor | 3/3 | 3/3 | 3/3 | 5 | 193 s |

**What it shows:**

- **Everest:** the 14B planner wrote the same clean plan every time: open the search page, search for 'Mount Everest', open the article, with a url check on the article. All three runs were verified, the first fully verified Everest runs. The 7B baseline's two failures came from a guessed text check (section 5).
- **Eiffel Tower**, which starts on the answer page: both configs were perfect, but the 14B config took twice as long. The 9 GB planner and the 4.7 GB executor don't both stay loaded next to a browser, so Ollama swaps them.
- **python.org:** the 14B config found the right version in its free-text answer all three times, because the planner's model writes the answer. But typed values are read by the executor's model, the same 7B model in both configs. Under the 14B config it returned nothing that passed the version pattern twice, including a run that never left the downloads page, where the baseline's reader succeeded. Why isn't explained yet. What is clear: a stronger planner doesn't make a weaker reader better, and the swaps doubled the time.

**Still to measure:**

- Plan reuse: `configs/local-16gb-plan-reuse.yaml` with shared memory, running as this was written.
- A Claude planner: `configs/claude-planner-local.yaml` needs an API key.

## 8. Thinking before acting

**Lesson:** a brief before the browser opens is cheap and catches doomed runs early. But a small model's judgement about what to ask is weak, so defaults and code checks carry most of the weight.

**Evidence and findings** (step 6, `ab76209`):

- **Briefs worked mechanically.** Every brief parsed, and over MCP a saved brief was reused without the planner rethinking the task.
- **A 7B planner under-asks.** Asked for a cheap round trip to Tokyo, it asked nothing and assumed today's date (run 14). After the question rules changed, it asked for departure and return dates, but still not where the trip starts (run 17).
- **Questions have to come before the browser opens.** Over MCP a server can't ask mid-call, and a person may take minutes to answer, so thinking runs before the time limit starts.
- **Question ids are the planner's own,** so answers given up front have to count even when no question's id matches them.
- **One rewrite isn't enough.** Plan review gives the planner one rewrite, and on a 7B model the rewrite kept the click-level steps it was asked to remove (run 18). Code folding (section 5) finished the job.
- **It costs time.** python.org took 70 s after step 6 against 42 s before, from longer prompts, and a rewrite round added about 35 s of planning on the 7B model.

## 9. The real web is noisy

**Lesson:** blocking a page's own background traffic is right, but reporting every block to the agent is wrong. The agent can't act on it, and it drowns out what the agent does need to know.

**Evidence:**

- In run 2, Wikipedia's analytics beacons were blocked 11 times, and each block was reported to the executor, which got confused.
- In run 6, python.org's error reporting retried a blocked POST 427 times, flooding both the logs and the executor's history.

**What changed:** everything is still blocked and recorded. The executor now hears only about blocks its own action could have caused: page loads, form posts, script writes back to the same site, data leaks, and expiry, with repeats collapsed. MCP results group blocks by kind and site, and flag the page's own scripts as `background`.

## 10. Serving other agents

**Lesson:** when another agent calls web-lobster, that agent must not become the next thing a web page can prompt-inject, and nothing can depend on asking a question mid-call.

**Evidence and decisions** (step 5, `ba61aaf`):

- **No mid-call questions.** The MCP 2026-07-28 protocol has no back channel for them. Approval happens up front, on the mandate, and step 6's questions use a stateless round trip: `brief_task`, or a `web_task` that returns `needs_input`, then `web_task` again with `brief_id` and answers.
- **Page text is withheld from the calling agent by default,** and URLs are reduced to origins. A result carries counts, typed values, planner-written text, and receipts.
- **Server-side data is locked down.** It must come from environment variables prefixed `WEB_LOBSTER_DATA_`, so a hijacked caller can't hand the server's own API keys to a website.
- **Host defaults matter.** OpenClaw's MCP timeout is 60 s, while web tasks take minutes. Hosts also launch servers from their own directory, so configs need absolute paths.
- **A config you pass must be the config you get.** Since `8096ea2`, a local config silently became paid Claude models whenever `ANTHROPIC_API_KEY` was set. That cost trap was fixed in `fcb0a7a`.

## 11. Local models on a 16 GB Mac

**What we found:**

- qwen2.5-coder:7b rejects images (HTTP 400), so the local config reads pages as text in DOM mode.
- Ollama's default context window cut pages short; the local config uses 16,000 tokens.
- A step takes 5 to 15 s. A lookup that starts on the answer page takes about a minute, and a multi-step search takes 3 to 8 minutes.
- gemma3:12b, a vision model, was slower than the 7B text model at reading values and no more accurate.
- The 14B and 7B models (9 GB and 4.7 GB) don't both stay loaded next to a browser, so a mixed config pays for swaps on every task.

## 12. How we work

- **Record every run,** including failures and what each one showed ([live-testing.md](live-testing.md)).
- **Say which of done, verified, and right a result is.** They're different claims.
- **Check answers against the source.** 330 m and 3.14.7 were confirmed against the live pages, and "3.15" turned out to be a pre-release row.
- **Read the records before explaining a result** (section 6).
- **Commit checkpoints.** A session's scratch files, including half-finished logs, were wiped once mid-run.
- **Measure before calling something an improvement,** with `web-lobster trials` rather than one run.

## 13. Still open

In rough order of what the evidence says matters (the [roadmap](roadmap.md) has the plan):

1. **Guessed text checks** made two 7B Everest runs fail on the right page (section 5).
2. **The caller's value checks are dropped** when the planner declares a value of the same name (section 4).
3. **The reader model decides whether values come back right** (section 7). A stronger model for reading values is worth measuring alongside planners.
4. **Plan reuse and a Claude planner** are built but not yet measured.
5. **How often real models fall for planted instructions** is unmeasured: the benchmark's live mode hasn't run.
6. **Value-bound write rules** would close `allowed-write-abuse`.
7. **Signed-in tasks** need mandate-scoped browser profiles, and a design record first.

## Numbers at a glance

| Measure | Value |
|---|---|
| Scripted benchmark, hijacked executor, all defenses | 7/7 done, 1/7 harmful, 0/7 leaked, 0/7 false "done" |
| Scripted benchmark, honest executor | 7/7 done under every defense setup |
| Recorded live runs | 21, plus 18 scored trials (plan-reuse trials in progress) |
| Problems found only by live runs | 16 |
| Everest, before click-level folding | 1 of 5 attempts finished (runs 5, 7, 9, 11, 16) |
| Everest, 7B, after folding (runs 19–21) | 3/3 right, median 9 steps, 171 s |
| Everest, trials, 7B / 14B planner | 1/3 / 3/3 right; 3/3 verified with the 14B planner |
| Lookups that start on the answer page | About 1 minute on 7B; about 2 minutes with the 14B planner |
| Automated tests | 316 |
