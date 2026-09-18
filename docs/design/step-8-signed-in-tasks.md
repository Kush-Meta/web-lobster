# Step 8: signed-in tasks

**Status:** proposed, not built. Phase 4 of [../roadmap.md](../roadmap.md). Nothing here touches a real account until it's built and tested against a practice site.

## Goal

Let a task act as the signed-in user, so web-lobster can do the things people actually want ("what did I order last week?", "what's the title of my latest pull request?"), while keeping two promises:

- **No model ever sees a secret.** Not the password, not a one-time code, not a session cookie.
- **Being signed in is not a standing permission.** A session lets a task read and act only inside a mandate the user approved for that run.

## What we have already

1. **Typing secrets is solved.** A mandate's data grants give the model a placeholder (`{{password}}`); the browser substitutes the value at typing time, only on origins that grant covers. Values are excluded from model dumps, redacted from logs, receipts, and observations, and a request carrying one to an ungranted origin is blocked in raw, URL-encoded, JSON-escaped, and base64 forms.
2. **Values come from the environment**, restricted to `WEB_LOBSTER_DATA_*`, so a hijacked caller can't ask the server for its other secrets.
3. **The safety gate asks before filling a password or email field**, and a granted value on a granted origin counts as already approved ([step 5](step-5-mcp-server.md), decision 14), so unattended runs don't stall on it.
4. **Login pages are detected.** The observer flags them, the dashboard shows a "log in and continue" prompt, and the CLI logs it.

## What's missing

- **Sessions don't survive.** Every run launches a fresh browser context, so a login never carries from one run to the next. This is the whole blocker.
- **Secrets only come from environment variables**, which means they sit in a shell profile or a `.env` file rather than in a password manager.
- **One-time codes have no path.** They expire, so they can't be resolved when the mandate is built.

## Decisions

| # | Decision | Why | Alternatives considered |
|---|---|---|---|
| 1 | A **profile** is a named cookie store (`~/.web_lobster/profiles/<name>`) that declares the origins it's for. A run opts into one explicitly (`--profile github`, or `profile` over MCP) | A session is a bearer credential. Naming it, and choosing it per run, keeps it from being ambient authority | A single shared profile (every task inherits every login); automatic profile selection by URL (a task on one site could carry another's session) |
| 2 | A mandate may use a profile only when **its origins are a subset of the profile's declared origins**, and only cookies matching the mandate's origins are loaded into the browser context | The run gets the session it needs and nothing else, even if the profile accumulated more over time | Loading the whole profile (a compromised page on one allowed origin could reach another site's cookies through the browser) |
| 3 | **The human logs in once, in a headed browser** (`web-lobster login --profile github`), and the profile keeps the session. Credential typing stays available for sites without 2FA, through ordinary data grants | It sidesteps 2FA and captchas entirely, and it's the only honest answer for accounts that matter. Captchas are never solved by the agent | Automating every login (fragile, and blocked by 2FA); asking the user mid-run (no one is watching an unattended run) |
| 4 | Secrets resolve from a **source**, never from caller text: `value_env` (existing), or `value_op: "op://vault/item/field"` read through the 1Password CLI at mandate-build time | The value never passes through a model or a calling agent, and a password manager is where these belong | Caller-supplied values (they pass through a model); a web-lobster-managed secret store (another thing to get wrong) |
| 5 | `op://` references are **bounded by config**: `secrets.op_vaults` lists the vaults a mandate may read. The CLI is run without a shell, with a timeout, and its stderr is never logged verbatim | Without an allowlist, a hijacked caller could name any item in any vault. Errors can quote the item, and sometimes the value | Any reference the caller writes; shelling out with the reference interpolated |
| 6 | **Unlock is the operator's choice**: a service-account token for unattended runs, or an interactive unlock for attended ones. With neither, the mandate fails to build and says which item couldn't be read, never the value | A standing token is real risk and should be a deliberate act, not a default | Requiring a token always (unattended-only); prompting mid-run (breaks MCP calls) |
| 7 | **One-time codes resolve lazily**, at typing time (`value_op_otp: true`), through the same substitution path, rate-limited and redacted like any other grant | A code fetched when the mandate is built is already stale | Resolving at build time; asking the user for the code (defeats unattended runs) |
| 8 | **Being signed in grants no writes.** Write rules are unchanged, and the default for a signed-in mandate is read-only | Most useful signed-in tasks are lookups, and a session makes a mistaken write far more damaging | Treating a session as consent for the account's actions |
| 9 | **Receipts and run records name the profile, never its contents.** The approval text says which profile and which sites | The user should see "this run uses your github session" before it runs | Silent profile use |
| 10 | Cookies are written back **only for the mandate's origins**, and `web-lobster forget --profile github` deletes the store | A run shouldn't quietly widen a profile's reach | Persisting everything the browser collected |

## How a run would flow

1. `web-lobster login --profile github` opens a headed browser, the user signs in by hand, and the cookies for that profile's origins are saved.
2. A later run passes `--profile github` with a mandate scoped to `https://github.com`. The mandate's origins must be covered by the profile.
3. The mandate builds, resolving any grants from the environment or 1Password. Values never enter a prompt.
4. The browser context launches with only the cookies matching the mandate's origins.
5. The task runs as it does today: planner isolation, mandate enforcement on every request, evidence, receipts. If the site still shows a login page, a grant fills it in when one exists; otherwise the run stops and says a login is needed.
6. Cookies for the mandate's origins are written back; the receipt records the profile's name.

## Tests to write

- Only cookies matching the mandate's origins reach the browser context, and only those are written back.
- A mandate whose origins aren't covered by the profile is refused.
- `value_op` resolves through a fake `op` binary; a vault outside the allowlist is refused; a failure names the item and never the value.
- A one-time code resolves at typing time, not at build time, and is redacted in logs and receipts.
- Receipts and run records name the profile and contain no cookie or secret.
- A benchmark scenario where a poisoned page tries to read the session: what the mandate does and doesn't stop.

## Known limits and open questions

- **Cookies can still leave through allowed cross-origin GETs.** That's the existing limit from step 1, and a session makes it sharper. Blocking those breaks ordinary pages, so the honest answer stays "a mandate bounds where a session can be used, not everything a page can do with it".
- **A compromised page on an allowed origin can act as the user** within the mandate's write rules. Read-only mandates keep that to reading.
- **A service-account token is a standing credential** on the machine. Worth warning about in the docs, and worth defaulting to off.
- **Profiles are plain files.** Disk encryption is the protection; keychain storage is worth considering later.
- **Captchas and device-approval prompts** end a run. That's deliberate.
- **Which sites are worth supporting first**: a practice site, then a low-stakes real account with a read-only mandate. Banking and payment accounts aren't a target.
