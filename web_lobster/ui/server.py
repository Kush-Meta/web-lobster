"""Web dashboard server — FastAPI backend with WebSocket streaming.

Serves the dashboard HTML and provides:
- REST endpoints for config, task management, and presets
- WebSocket endpoint for real-time agent state streaming
- Control endpoints (start, pause, resume, stop, confirm)
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from web_lobster.core.config import WebLobsterConfig, ModelConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.ui.state import SharedState
from web_lobster.ui.presets import TASK_PRESETS, MODEL_PRESETS
from web_lobster.utils.logging import get_logger

logger = get_logger("ui.server")

app = FastAPI(title="Web Lobster Dashboard", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared state — single instance
shared = SharedState()
_orchestrator: Optional[Orchestrator] = None
_agent_task: Optional[asyncio.Task] = None


# ── Dashboard HTML ────────────────────────────────────────

DASHBOARD_PATH = Path(__file__).parent / "dashboard.html"


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the dashboard UI."""
    return DASHBOARD_PATH.read_text()


# ── REST: State & Config ─────────────────────────────────

@app.get("/api/state")
async def get_state():
    """Current agent state snapshot."""
    return shared.snapshot()


@app.get("/api/config")
async def get_config():
    """Current configuration."""
    return shared.config.model_dump()


class ConfigUpdate(BaseModel):
    planner: Optional[dict] = None
    executor: Optional[dict] = None
    validator: Optional[dict] = None
    browser: Optional[dict] = None
    safety: Optional[dict] = None
    agent: Optional[dict] = None
    mcp_servers: Optional[list] = None
    # Convenience shorthands forwarded into agent sub-dict
    dom_mode: Optional[bool] = None


@app.put("/api/config")
async def update_config(update: ConfigUpdate):
    """Update configuration (only while agent is idle)."""
    if shared.phase.value not in ("idle", "completed", "failed"):
        return JSONResponse(
            {"error": "Cannot update config while agent is running"},
            status_code=409,
        )
    data = shared.config.model_dump()
    update_dict = update.model_dump(exclude_none=True)
    # Forward convenience shorthands into their sub-dicts
    if "dom_mode" in update_dict:
        data["agent"]["dom_mode"] = update_dict.pop("dom_mode")
    for key, val in update_dict.items():
        if key not in data:
            continue
        if isinstance(val, dict):
            data[key].update(val)
        else:
            data[key] = val  # handles lists (mcp_servers) and scalars
    shared.config = WebLobsterConfig(**data)
    logger.info("config_updated")
    return {"status": "ok", "config": shared.config.model_dump()}


# ── REST: Presets ─────────────────────────────────────────

@app.get("/api/presets/tasks")
async def get_task_presets():
    return TASK_PRESETS


@app.get("/api/presets/models")
async def get_model_presets():
    return MODEL_PRESETS


# ── REST: Agent Control ───────────────────────────────────

class TaskRequest(BaseModel):
    task: str
    start_url: str = "https://www.google.com"
    notes: Optional[str] = None


@app.post("/api/agent/start")
async def start_agent(req: TaskRequest):
    """Start executing a task."""
    global _orchestrator, _agent_task

    if shared.phase.value not in ("idle", "completed", "failed"):
        return JSONResponse(
            {"error": "Agent is already running"},
            status_code=409,
        )

    _orchestrator = Orchestrator(shared.config, shared_state=shared)
    shared.max_steps = shared.config.agent.max_steps

    # Run in background
    _agent_task = asyncio.create_task(
        _run_agent(req.task, req.start_url, req.notes)
    )

    return {"status": "started", "task": req.task}


async def _run_agent(task: str, start_url: str, notes: Optional[str] = None):
    """Background coroutine that runs the orchestrator."""
    try:
        result = await _orchestrator.run(task, start_url=start_url, notes=notes)
        await shared.on_task_complete(
            result.success,
            result.summary(),
            answer=result.answer,
            memory_hits=getattr(result, "memory_hits", 0),
        )
    except asyncio.CancelledError:
        await shared.on_task_complete(False, "Task cancelled by user")
    except Exception as e:
        logger.error("agent_error", error=str(e))
        await shared.on_task_complete(False, f"Error: {e}")


@app.post("/api/agent/pause")
async def pause_agent():
    shared.pause()
    await shared.emit("paused")
    return {"status": "paused"}


@app.post("/api/agent/resume")
async def resume_agent():
    shared.resume()
    await shared.emit("resumed")
    return {"status": "resumed"}


@app.post("/api/agent/stop")
async def stop_agent():
    global _agent_task
    if _agent_task and not _agent_task.done():
        _agent_task.cancel()
    shared.phase = shared.phase.IDLE
    await shared.emit("stopped")
    return {"status": "stopped"}


class ConfirmRequest(BaseModel):
    approved: bool


@app.post("/api/agent/confirm")
async def confirm_action(req: ConfirmRequest):
    """Approve or decline a safety-flagged action."""
    shared.resolve_confirmation(req.approved)
    return {"status": "confirmed", "approved": req.approved}


class AnswersRequest(BaseModel):
    answers: Optional[dict] = None


@app.post("/api/agent/answers")
async def submit_answers(req: AnswersRequest):
    """The user's answers to the planner's questions, or null to skip them."""
    shared.resolve_answers(req.answers)
    return {"status": "answered"}


@app.post("/api/agent/login_done")
async def login_done():
    """User signals they have finished logging in — resume the agent."""
    shared.resolve_login()
    return {"status": "resumed"}


# ── WebSocket: Real-time event stream ─────────────────────

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    """Stream agent events to the dashboard in real time."""
    await ws.accept()
    queue = shared.subscribe()
    logger.info("ws_connected")

    try:
        # Send current state on connect
        await ws.send_json({"type": "state_sync", "data": shared.snapshot()})

        while True:
            event = await queue.get()
            await ws.send_text(event.to_json())
    except WebSocketDisconnect:
        logger.info("ws_disconnected")
    except Exception as e:
        logger.debug("ws_error", error=str(e))
    finally:
        shared.unsubscribe(queue)


# ── Server launcher ───────────────────────────────────────

def run_server(host: str = "127.0.0.1", port: int = 7860, config: Optional[WebLobsterConfig] = None):
    """Launch the dashboard server."""
    import uvicorn

    if config:
        shared.config = config

    logger.info("dashboard_starting", url=f"http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
