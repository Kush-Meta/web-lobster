# Step 5: web-lobster as an MCP server

**Status:** built on `feat/mandate-enforcement`, 2026-09-14. User guide: [../mcp-server.md](../mcp-server.md).

## Goal

Let other agents (OpenClaw first, then Claude Code or any MCP client) hand web-lobster a web task and a mandate, and get back a result they can act on. The calling agent must not become the next thing a web page can prompt-inject.

This is the last step of the roadmap in [../roadmap.md](../roadmap.md). Steps 1–4 built the pieces this exposes: mandates, planner isolation, evidence and receipts, and a benchmark.

## What we found before building

1. **The MCP SDK had drifted a major version.** `pyproject.toml` asked for `mcp>=1.0.0`, and pip installed 2.2.0. Version 2 renames FastMCP to `MCPServer`, injects `Context` as a tool parameter, and speaks the stateless 2026-07-28 protocol while still serving 2025-era clients. It also renamed `Tool.inputSchema` to `input_schema`, which had silently broken web-lobster's own MCP client (`tools/mcp_manager.py`): every server it connected to reported no tools. That's fixed here, and the dependency is pinned to `mcp>=2.2,<3`.
2. **A server can't ask the user a question mid-call under the new protocol.** A probe tool calling `ctx.elicit(...)` worked for a client on protocol `2025-11-25` and failed for one on `2026-07-28` with "no back-channel for server-initiated requests". The replacement, multi-round-trip requests, ends the call with "input required" and expects the client to call again. A live browser session can't be parked between those calls.
3. **Structured results, tool annotations, and progress notifications work on both protocol versions**, confirmed by the same probe.
4. **OpenClaw consumes MCP servers in two ways:** saved definitions under `mcp.servers` (`openclaw mcp add`/`set`), and bundles. A Claude-format bundle (`.claude-plugin/plugin.json`, `.mcp.json`, `skills/`) is detected by OpenClaw and is also a Claude Code plugin. OpenClaw exposes bundle tools as `server__tool`, applies its tool-approval posture to MCP calls, and defaults to a 60-second MCP request timeout.

## Decisions

| # | Decision | Why | Alternatives considered |
|---|---|---|---|
| 1 | No task without a mandate: `web_task` requires `mandate.origins` | The mandate is the only thing that makes handing a browser to a model safe | An optional mandate, as the CLI allows. Rejected: the caller is itself an agent, and unscoped defaults would be the common path |
| 2 | Approval happens before the run, on the mandate. `web_task` is annotated destructive and open-world so hosts prompt, and `check_mandate` renders plain approval text | Finding 2 rules out mid-run questions; the host's approval prompt already shows the call's arguments | Elicitation (only works on old clients); multi-round-trip resumption (would need a parked browser) |
| 3 | Safety-gate confirmations during a run are declined unless the operator starts the server with `--approve-confirmations` | No person is attached to a stdio call; declining fails safe, and the mandate already bounds what can happen | Auto-approve (the benchmark's worst case); fail the task (too blunt) |
| 4 | Read-only by default: `writes` defaults to empty, and `allow_any_write` must be explicit and can't be combined with a list | An agent that doesn't think about writes should get a read-only task, not an unscoped one | Nullable `writes` meaning "any", as in YAML mandates. Rejected: omitting a field would widen permissions |
| 5 | Page text withheld by default. Results carry counts, typed values, planner-written sub-goal text, and origins; the page-derived answer and text values come back only with `include_page_text`. URLs in errors and blocked-action reports are reduced to origins | Extends step 2's planner-isolation boundary to the calling agent. Paths and queries are site-controlled text too | Always returning the answer (makes web-lobster an injection relay); stripping "instructions" from text (detection loses to adaptive attacks) |
| 6 | Data from the server's environment, restricted to `WEB_LOBSTER_DATA_*` variables, via `value_env` | Values can skip the calling model entirely. The prefix stops a hijacked caller from granting `ANTHROPIC_API_KEY` to an attacker's site | Any environment variable (a secret-exfiltration primitive); values only in arguments (every value passes through a model) |
| 7 | Validation problems never echo data values (`errors(include_input=False)`) | A typo in a mandate shouldn't print the password it contains | — |
| 8 | Mandates expire: 30 minutes by default, 240 at most | A forgotten or looping task stops on its own | No default expiry |
| 9 | Receipts outlive the call: saved as `<runs-dir>/<run_id>.receipts.jsonl`, with the chain head in the result, and `verify_receipts(run_id, expected_head)` | The hash chain only proves something against a head kept outside the agent's reach; the caller is that place | Returning full receipts inline (large, and their check details quote pages) |
| 10 | `verified` is stricter than `done`: done means every sub-goal completed; verified adds that every completed sub-goal was proven by evidence | Callers need to tell "a model thinks so" from "the browser proved it" | A single success flag |
| 11 | One task at a time by default (`--max-concurrent`) | Each task drives a real browser; concurrency is an operator choice | Unbounded concurrency |
| 12 | A transport-independent `WebTaskService`, with `server.py` as a thin MCP layer | The logic is testable without MCP, and future transports reuse it | Logic inside tool functions |
| 13 | Run ids are 12 hex characters, validated before any file access | `get_run("../../etc/passwd")` must not read files | Arbitrary ids |
| 14 | Typing data the mandate grants, on a page that grant covers, doesn't wait on the safety gate's sensitive-field confirmation | Found by the first end-to-end test. With confirmations declined (decision 3), the email field was never filled, the newsletter form went out empty, and the result still came back verified. The mandate already records the user's approval for exactly that entry | Approving every confirmation (far broader than the mandate); leaving it (grants become unusable over MCP) |
| 15 | Expected failures, such as a bad or unknown run id, raise the SDK's `ToolError` | The caller gets the reason; the server log doesn't show a crash | Plain exceptions (the SDK hides their message from the caller and logs a traceback) |

## How a call flows

1. `web_task` validates arguments against `MandateInput` and builds an enforceable `Mandate`, resolving `value_env` on the server.
2. `WebTaskService.run` takes a slot, creates an `Orchestrator` with the mandate and a `ServerUI`, and wraps the planner so requested values are read on the last sub-goal of every plan.
3. The orchestrator runs as it does from the CLI: mandate enforcement, planner isolation, evidence, receipts. `ServerUI` turns its events into progress notifications built only from trusted parts, and declines confirmations.
4. The service builds a `WebTaskResult`, withholding page text unless asked. It saves the full record (without data values) and the receipts, then returns.

## Tests

`tests/test_mcp_server.py`:

- Mandate building: the env prefix, exactly one value source, read-only default, env resolution, no values in problem messages, expiry, URL sanitizing.
- The bridge: confirmations declined by default, progress messages free of page text, value requests merged once.
- Through the SDK's in-memory client: tool list and annotations, and `check_mandate` naming data without showing it.
- A full `web_task` against the benchmark's `link-exfil` trap with a hijacked scripted executor. The task completes and is verified. The attacker receives nothing, and the one blocked action is reported. Typed values come back while text values and the answer are withheld from the result and from progress. `get_run` withholds page text unless asked. `verify_receipts` confirms the chain, then detects tampering. A path-traversal run id is rejected.
- Over real stdio: `python -m web_lobster mcp` serves both the SDK client and web-lobster's own `MCPManager`, which exercises the v2 fix.

## Known limits and open questions

- **Login.** Each task starts with a fresh browser profile, so tasks that need a signed-in account don't work. A persistent, per-mandate profile is the likely next step.
- **Host timeouts.** Tasks take minutes. OpenClaw's default MCP timeout is 60 seconds, so the docs configure 15 minutes. The 2026-07-28 tasks extension could make long runs pollable instead.
- **Live models.** Everything here is tested with scripted models. Behaviour with real models, including how often they build good mandates, is unmeasured.
- **The write-content gap** from the benchmark (`allowed-write-abuse`) applies here too: write rules scope endpoints, not what's sent to them. Evidence has the same blind spot. In the bug behind decision 14, a `request` check for `POST /api/subscribe` passed on a submission with no email in it. A request check that could also match body content would have caught it.
