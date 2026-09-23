# web-lobster documentation

Start with the [project README](../README.md) for what web-lobster is and how to run it.

## The documents

| | What's in it | Read it when |
|---|---|---|
| [architecture.md](architecture.md) | How the pieces fit: the life of a task, every module, the trust boundaries, the layers of enforcement, and the data contracts | You want to understand or change the code |
| [roadmap.md](roadmap.md) | What's built, where things stand, the known gaps, what's next — and the ideas that were measured and dropped | You want to know what to work on |
| [learnings.md](learnings.md) | What building and running it has taught us, with the evidence behind each lesson | You want the reasoning, not the code |
| [live-testing.md](live-testing.md) | Every live run, what broke, and what changed because of it | You hit something odd on a real site, or want the numbers |
| [mcp-server.md](mcp-server.md) | The MCP tools in detail: setup, every field, and what comes back | You're calling web-lobster from another agent |

## Design records

One per step, written before the code and kept as the record of why it is the way it is.

| | Step |
|---|---|
| [step-5-mcp-server.md](design/step-5-mcp-server.md) | Serving other agents, with page text withheld by default |
| [step-6-planner-briefing.md](design/step-6-planner-briefing.md) | Thinking before acting: a brief, questions, mandate gaps, and plan review |
| [step-7-trials-and-planners.md](design/step-7-trials-and-planners.md) | Measuring runs against known answers, and trying a stronger planner |
| [step-8-signed-in-tasks.md](design/step-8-signed-in-tasks.md) | Persistent profiles and a password manager — designed, not built |

Steps 1 to 4 (mandates, planner isolation, evidence and receipts, and the poisoned-page benchmark) predate the design-record habit; they're described in [architecture.md](architecture.md) and the [README](../README.md).
