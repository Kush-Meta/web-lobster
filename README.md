# 🦞 Web Lobster

An autonomous web agent powered by a suite of open-source AI models. Web Lobster uses a **Planner → Executor → Validator** pipeline to break down complex web tasks and complete them autonomously.

## Architecture

```
User Task
    │
    ▼
┌─────────────┐
│   Planner   │  Large model (70B) — decomposes task into sub-goals
└──────┬──────┘
       │
       ▼
┌─────────────────────────────────────────┐
│            Agent Loop                    │
│  Observer → Executor → Browser → repeat  │
└──────┬──────────────────────────────────┘
       │
       ▼
┌─────────────┐
│  Validator   │  Vision model — confirms sub-goal completion
└─────────────┘
```

## Quick Start

```bash
# 1. Install dependencies
pip install -e .
playwright install chromium

# 2. Pull required models via Ollama
ollama pull qwen2.5:72b        # Planner
ollama pull qwen2.5:7b         # Executor
ollama pull minicpm-v:8b       # Validator (vision)

# 3. Launch the dashboard (recommended)
python -m web_lobster ui

# Or run a task directly from CLI
python -m web_lobster run "Find the cheapest flight from LAX to JFK on Dec 15"
```

## Web Dashboard

The dashboard is the primary way to use Web Lobster. Launch it with:

```bash
python -m web_lobster ui                  # default: http://127.0.0.1:7860
python -m web_lobster ui --port 8080      # custom port
python -m web_lobster ui -c my_config.yaml
```

**Features:**
- **Task presets** — one-click templates for common tasks (flight search, product comparison, job search, etc.) with fillable parameters
- **Model configuration** — swap planner/executor/validator models live, choose from hardware-tier presets (Full Power, Balanced, Lightweight, CPU-only)
- **Live screenshot** — watch the annotated browser view update in real time as the agent works
- **Sub-goal tracker** — see the plan decomposition and progress through each goal
- **Action log** — streaming feed of every click, type, scroll, and navigation
- **Safety controls** — approve/decline high-risk actions via modal, toggle dry-run and ensemble voting
- **Pause/Resume/Stop** — full control over the agent at any point

## Configuration

Copy `configs/default.yaml` and customize model choices, safety rails, timeouts, etc.

```bash
cp configs/default.yaml configs/my_config.yaml
python -m web_lobster --config configs/my_config.yaml "your task here"
```

## Features

- **Multi-model pipeline**: Planner (large), Executor (small/fast), Validator (vision)
- **Hybrid perception**: Annotated screenshots + accessibility tree extraction
- **Grammar-constrained output**: Optional llama.cpp backend for guaranteed valid JSON actions
- **Safety rails**: Action gating, URL allowlists, confirmation checkpoints, dry-run mode
- **Ensemble voting**: Optional multi-model consensus for high-stakes actions
- **Extensible actions**: Add custom browser actions via the action registry

## Project Structure

```
web_lobster/
├── __init__.py
├── __main__.py              # CLI entry point (run + ui commands)
├── core/
│   ├── orchestrator.py      # Main agent loop coordinator
│   ├── schemas.py           # All data contracts (Pydantic models)
│   └── config.py            # Configuration loader
├── models/
│   ├── base.py              # Abstract model interface
│   ├── ollama_backend.py    # Ollama API client
│   ├── llamacpp_backend.py  # llama.cpp with GBNF grammar support
│   ├── planner.py           # Task decomposition model
│   ├── executor.py          # Single-action decision model
│   └── validator.py         # Vision-based goal verification
├── browser/
│   ├── controller.py        # Playwright browser management
│   ├── observer.py          # Page state extraction (hybrid)
│   └── annotator.py         # Screenshot annotation with element labels
├── actions/
│   ├── registry.py          # Action type registry
│   └── safety.py            # Action gating and confirmation logic
├── ui/
│   ├── server.py            # FastAPI backend + WebSocket streaming
│   ├── state.py             # Shared state bridge (orchestrator ↔ UI)
│   ├── presets.py           # Task and model preset definitions
│   └── dashboard.html       # Single-file interactive dashboard
└── utils/
    ├── logging.py           # Structured logging
    └── retry.py             # Retry and backoff utilities
```

## License

MIT
