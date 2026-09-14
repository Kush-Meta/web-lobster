# web-lobster MCP server

`web-lobster mcp` lets other agents run web tasks through web-lobster: OpenClaw, Claude Code, or any MCP client. Every task runs inside a mandate that the browser enforces, and results are built so the calling agent can rely on them without being exposed to the web pages behind them.

Why it's built this way: [design/step-5-mcp-server.md](design/step-5-mcp-server.md).

## Run it

```bash
web-lobster mcp                                   # stdio, for agents that launch it
web-lobster mcp --transport streamable-http --port 8765   # http://127.0.0.1:8765/mcp
```

| Option | Default | Meaning |
|---|---|---|
| `-c, --config` | as `web-lobster run` | Model config (planner, executor, validator) |
| `--transport` | `stdio` | `stdio` or `streamable-http` |
| `--host`, `--port` | `127.0.0.1`, `8765` | Where streamable HTTP listens |
| `--runs-dir` | `~/.web_lobster/runs` | Run records and receipts |
| `--max-concurrent` | `1` | Tasks at once (each drives a browser) |
| `--approve-confirmations` | off | Approve the safety gate's confirmation prompts instead of declining them |
| `--headed` | off | Show the browser |
| `-v, --verbose` | off | Debug logs |

Logs always go to stderr, because stdout carries the protocol. The server speaks MCP 2026-07-28 and still serves 2025-era clients.

## Connect an agent

**OpenClaw.** Register the server with a long request timeout (web tasks take minutes, and OpenClaw's default is 60 seconds) and have OpenClaw ask before each call:

```bash
openclaw mcp set web-lobster '{"command":"web-lobster","args":["mcp"],"requestTimeoutMs":900000}'
openclaw mcp configure web-lobster --approval prompt
openclaw mcp doctor web-lobster --probe
```

Then add the skill from [integrations/web-lobster-plugin](../integrations/web-lobster-plugin) so the agent knows how to build mandates. Alternatively, install that directory as a bundle with `openclaw plugins install`.

**Claude Code** or any client that reads `.mcp.json`:

```json
{"mcpServers": {"web-lobster": {"command": "web-lobster", "args": ["mcp"]}}}
```

**User data.** Put values the agent may type in web-lobster's `.env` as `WEB_LOBSTER_DATA_*` variables (for example `WEB_LOBSTER_DATA_EMAIL=you@example.com`) and grant them with `value_env`. The value then never passes through the calling model. Only variables with that prefix can be read, so a hijacked caller can't hand the server's other secrets to a website.

## Tools

### `web_task`

Runs a task in a real browser under a mandate. Annotated as destructive and open-world, so hosts that honour annotations ask before calling it.

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `task` | string | required | What to do |
| `mandate.origins` | string[] | required | Sites the browser may use; `https://*.site.com` matches subdomains only |
| `mandate.data` | `{name, value \| value_env, origins}`[] | `[]` | Values the task may type, each limited to its sites; typed as `{{name}}` |
| `mandate.writes` | string[] | `[]` | Changes the task may make, as `METHOD URL-pattern` (POST, PUT, PATCH, DELETE, WS, `*`). Empty means read-only |
| `mandate.allow_any_write` | bool | `false` | Allow any change on the allowed sites instead of listing writes |
| `mandate.expires_in_minutes` | number | `30` | Everything stops after this (maximum 240) |
| `start_url` | string | first non-wildcard origin | Must be on an allowed site |
| `values` | `{name, type, description?, choices?}`[] | `[]` | Typed values to read from the final page: number, integer, boolean, date, choice, text |
| `include_page_text` | bool | `false` | Also return the page-derived answer and text values |
| `max_steps` | int | config | Cap on browser actions |

Progress is reported while the task runs, as step counts and the plan's own sub-goal text.

Result:

| Field | Meaning |
|---|---|
| `run_id` | For `get_run` and `verify_receipts` |
| `done` | Every sub-goal was completed |
| `verified` | Done, and every completed sub-goal was proven by evidence checks rather than judged by a model |
| `summary` | Written by web-lobster from counts; contains no page text |
| `values` | Requested values that passed their type checks. Text values are `withheld` unless `include_page_text` |
| `answer` | Page-derived answer; `null` unless `include_page_text` |
| `receipts` | Per sub-goal: done, `evidence` or `model`, checks passed, writes sent |
| `receipt_chain_head` | Digest of the last receipt; keep it |
| `blocked` | What the mandate stopped, as kind and site (paths left out) |
| `steps`, `seconds`, `error` | Run stats; URLs in errors are reduced to origins |

### `check_mandate`

Validates a mandate without running anything and returns `approval_text` to show the user, for example:

```
Task: Rebook my trip for Dec 22
Sites: https://www.united.com
Data it may type: email (only on https://www.united.com)
Writes it may make: POST https://www.united.com/api/rebook*
Expires: 30 minutes after it starts
```

Data is named, never shown. Problems are listed without echoing values.

### `get_run`

Returns a past run's record: the task, the mandate as run (without data values), and the result. With `include_page_text`, it adds the answer, text values, full receipts, and full violation details, all of which quote pages.

### `verify_receipts`

Recomputes a run's saved receipt chain. `intact` says whether every receipt is unmodified and linked; `matches_expected` says whether the chain ends at the `expected_head` you pass. The check is only as good as where you kept the head: store it outside the agent's reach.

## Trust model

- **The mandate is the permission.** The browser enforces its sites, data grants, writes, and expiry on every request, whatever the models decide. A task without a mandate isn't accepted, and a mandate without writes is read-only.
- **Approval happens before the run.** Mid-call questions don't work under the current MCP protocol for a live browser session, so approval is up front: the host's tool approval on `web_task`, ideally after showing `check_mandate`'s text. Safety-gate confirmations during a run are declined unless the server runs with `--approve-confirmations`. Typing data the mandate grants, on a site that grant covers, counts as approved by the mandate and doesn't wait on a confirmation.
- **The caller doesn't read the web by default.** Web pages can carry instructions. Inside web-lobster, the planner never sees them (see the main README). The same boundary extends to the calling agent: results carry counts, typed values, planner-written sub-goal text, and origins, not page text, unless the caller asks with `include_page_text`.
- **Records don't hold secrets.** Run records store the mandate without data values, and granted values are redacted from receipts and violations.

## Limits

- Each task starts a fresh browser profile, so tasks that need you to be signed in don't work yet.
- Hosts with short request timeouts cut long tasks off; raise the timeout (see OpenClaw above).
- Write rules scope endpoints, not what's sent to them (the benchmark's `allowed-write-abuse` gap).
- Behaviour with live models hasn't been benchmarked yet; the scripted benchmark measures the defenses, not how often a model falls for a page.
