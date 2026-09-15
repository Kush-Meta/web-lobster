# web-lobster plugin

A bundle that connects an agent to the web-lobster MCP server and teaches it how to use it. It uses the Claude plugin layout, which OpenClaw also detects as a bundle, so the same directory works in both.

```
web-lobster-plugin/
├── .claude-plugin/plugin.json   # plugin manifest
├── .mcp.json                    # starts `web-lobster mcp` over stdio
└── skills/web-lobster/SKILL.md  # when and how to use web_task
```

## Prerequisites

web-lobster must be installed where the agent runs, with `web-lobster` on `PATH`:

```bash
pip install -e /path/to/web-lobster
playwright install chromium
```

Configure models the way you would for `web-lobster run` (see the main README). To use a specific config, such as the local 16 GB one, add it to the server's arguments with an absolute path, because hosts start the server from their own working directory: `"args": ["mcp", "-c", "/path/to/web-lobster/configs/local-16gb.yaml"]`. If `web-lobster` isn't on the host's `PATH`, set `command` to the full path of the script, for example `/path/to/web-lobster/.venv/bin/web-lobster`. Put user data the agent may type in web-lobster's `.env` file as `WEB_LOBSTER_DATA_*` variables, such as `WEB_LOBSTER_DATA_EMAIL=you@example.com`, so values never pass through the calling model.

## OpenClaw

Install the bundle, then check that OpenClaw sees it:

```bash
openclaw plugins install ./integrations/web-lobster-plugin
openclaw plugins inspect web-lobster
```

Web tasks take minutes, and OpenClaw's default MCP request timeout is 60 seconds. If you'd rather control the timeout and approvals directly, register the server instead of (not as well as) installing the bundle:

```bash
openclaw mcp set web-lobster '{"command":"web-lobster","args":["mcp"],"requestTimeoutMs":900000}'
openclaw mcp configure web-lobster --approval prompt
openclaw mcp doctor web-lobster --probe
```

With `--approval prompt`, OpenClaw asks before every `web_task` call and shows its arguments, including the mandate. Then copy `skills/web-lobster` into your workspace skills folder (for example `~/.openclaw/workspace/skills/`).

OpenClaw exposes the tools as `web-lobster__web_task`, `web-lobster__check_mandate`, and so on.

## Claude Code

Add the server to a project's `.mcp.json` (the same content as this bundle's `.mcp.json`), or install this directory as a plugin from a marketplace you control.

## Other MCP clients

Start the server with `web-lobster mcp` over stdio, or `web-lobster mcp --transport streamable-http --port 8765` and connect to `http://127.0.0.1:8765/mcp`. See [docs/mcp-server.md](../../docs/mcp-server.md).
