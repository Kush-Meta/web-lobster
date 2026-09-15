"""Episodic Task Memory — the agent learns from every run.

Web Lobster is the first open-source web agent with persistent episodic memory.
After each task, the agent saves what worked (and what didn't) to disk. Before
planning a new task, it retrieves the most similar past experiences and uses
them as in-context examples — dramatically improving planning quality over time.

Memory feeds the planner, so it holds page content back: a record's answer is
kept for the user but never shown to the planner, and records written before
learnings came from trusted inputs only show the planner their task and outcome.

Storage: ~/.web_lobster/memories/tasks.jsonl (one JSON record per line)
Retrieval: Jaccard similarity on word tokens, top-k results above threshold,
plus recent trusted runs on the same sites, with the steps each sub-goal took
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)

MEMORY_DIR = Path.home() / ".web_lobster" / "memories"
MEMORY_FILE = MEMORY_DIR / "tasks.jsonl"
# Runs of other tasks on the same sites to show the planner
MAX_SITE_RECORDS = 2
# How similar a past task must be for its plan to be offered for reuse
REUSE_SIMILARITY = 0.5
MAX_REUSE_PLAN_CHARS = 4000


@dataclass
class TaskRecord:
    """A single completed task stored in episodic memory."""
    task: str
    success: bool
    final_url: str
    steps_taken: int
    elapsed_seconds: float
    timestamp: float
    # The plan that was executed (list of sub-goal strings)
    sub_goals: list[str] = field(default_factory=list)
    # Sub-goals that actually completed successfully
    completed_goals: list[str] = field(default_factory=list)
    # Sub-goals that had to be replanned
    replanned_goals: list[str] = field(default_factory=list)
    # The extracted answer (if any). Read from a page: for the user, never the planner.
    answer: Optional[str] = None
    # Key learnings extracted by the model after the run
    learnings: Optional[str] = None
    # True when the plan and learnings came only from trusted inputs. Records
    # written before planner isolation load as False, since their learnings
    # were drawn from page-derived answers.
    trusted: bool = False
    # Origins the run visited
    origins: list[str] = field(default_factory=list)
    # Per sub-goal: goal text, done, how that was decided ("evidence"/"model"), steps taken.
    # Counts and planner-written text only, so it's safe to show a future planner.
    goal_stats: list[dict] = field(default_factory=list)
    # A successful run's completed sub-goals as the planner wrote them (goal, criteria,
    # values, evidence), so a later run on the same site can reuse the plan.
    plan: list[dict] = field(default_factory=list)


class TaskMemory:
    """Persistent episodic memory for the agent.

    Stores task records to disk and retrieves semantically similar past tasks
    to inform planning. Uses fast token-level Jaccard similarity — no extra
    API calls needed for retrieval.
    """

    def __init__(self, memory_file: Path = MEMORY_FILE):
        self.memory_file = memory_file
        self._records: list[TaskRecord] = []
        self._loaded = False

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self.memory_file.exists():
            return
        try:
            with open(self.memory_file) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        data = json.loads(line)
                        self._records.append(TaskRecord(**data))
            logger.info("memory_loaded", records=len(self._records))
        except Exception as e:
            logger.warning("memory_load_error", error=str(e))

    @staticmethod
    def _tokenize(text: str) -> set[str]:
        """Split text into lowercase word tokens for similarity comparison."""
        return set(re.findall(r'\b[a-z0-9]+\b', text.lower()))

    @staticmethod
    def _jaccard(a: set[str], b: set[str]) -> float:
        if not a or not b:
            return 0.0
        return len(a & b) / len(a | b)

    def find_similar(
        self,
        task: str,
        top_k: int = 3,
        min_similarity: float = 0.15,
    ) -> list[tuple[float, TaskRecord]]:
        """Return the top-k most similar past tasks above the similarity threshold."""
        self._ensure_loaded()
        if not self._records:
            return []

        task_tokens = self._tokenize(task)
        scored = [
            (self._jaccard(task_tokens, self._tokenize(r.task)), r)
            for r in self._records
        ]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [(s, r) for s, r in scored if s >= min_similarity][:top_k]

    def format_for_prompt(
        self,
        task: str,
        top_k: int = 3,
        origins: Optional[list[str]] = None,
        reuse_plans: bool = False,
    ) -> Optional[str]:
        """Format past experience as a prompt block for the planner.

        Similar tasks come first. With origins (trusted: the mandate's sites and
        the start page), recent trusted runs of other tasks on those sites follow,
        so what the agent learned about a site carries over. With reuse_plans, a
        plan that worked for a similar task on one of those sites leads the block,
        in full. Returns None if nothing is relevant. Answers are never included.
        """
        self._ensure_loaded()
        wanted = set(origins or [])
        reusable = self._plan_to_reuse(task, wanted) if reuse_plans and wanted else None
        similar = [(score, rec) for score, rec in self.find_similar(task, top_k=top_k) if rec is not reusable]
        shown = {id(rec) for _, rec in similar} | ({id(reusable)} if reusable else set())
        same_site = [
            rec for rec in reversed(self._records)
            if rec.trusted and id(rec) not in shown and wanted & set(rec.origins)
        ][:MAX_SITE_RECORDS]

        blocks = []
        if reusable:
            blocks.append("\n".join(self._reuse_lines(reusable)))
        if similar or same_site:
            lines = ["RELEVANT PAST EXPERIENCE (use as guidance, adapt as needed):"]
            for i, (score, rec) in enumerate(similar, 1):
                lines.append(f"\n[Memory {i}] (similarity: {score:.0%}) {self._outcome(rec)}")
                lines.append(f"Task: {rec.task}")
                if not rec.trusted:
                    lines.append("(Plan details withheld: recorded before page content was kept out of planning.)")
                    continue
                lines.extend(self._plan_lines(rec))
            for rec in same_site:
                site = sorted(wanted & set(rec.origins))[0]
                lines.append(f"\n[Same site: {site}] {self._outcome(rec)}")
                lines.append(f"Task: {rec.task}")
                lines.extend(self._plan_lines(rec))
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) or None

    def _plan_to_reuse(self, task: str, origins: set[str]) -> Optional[TaskRecord]:
        """The most similar trusted, successful run on one of these sites that saved its plan."""
        task_tokens = self._tokenize(task)
        best: Optional[tuple[float, TaskRecord]] = None
        for rec in self._records:
            if not (rec.trusted and rec.success and rec.plan and origins & set(rec.origins)):
                continue
            score = self._jaccard(task_tokens, self._tokenize(rec.task))
            if score >= REUSE_SIMILARITY and (best is None or (score, rec.timestamp) > (best[0], best[1].timestamp)):
                best = (score, rec)
        return best[1] if best else None

    @staticmethod
    def _reuse_lines(rec: TaskRecord) -> list[str]:
        done = [stat for stat in rec.goal_stats if stat.get("done")]
        proven = sum(stat.get("basis") == "evidence" for stat in done)
        plan = json.dumps(rec.plan, indent=1)
        return [
            "A PLAN THAT WORKED (a past run of a similar task on the same site: reuse it if the task is "
            "the same, adapt it if not):",
            f"Task: {rec.task}",
            f"It took {rec.steps_taken} steps, and {proven} of {len(done)} completed sub-goals were proven by evidence.",
            plan[:MAX_REUSE_PLAN_CHARS],
        ]

    @staticmethod
    def _outcome(rec: TaskRecord) -> str:
        status = "✓ succeeded" if rec.success else "✗ failed"
        return f"{status} in {rec.steps_taken} steps" if rec.trusted else status

    @staticmethod
    def _plan_lines(rec: TaskRecord) -> list[str]:
        lines = []
        if rec.goal_stats:
            lines.append("Plan that was used:")
            for stat in rec.goal_stats:
                tick = "  ✓" if stat.get("done") else "  ✗"
                details = []
                steps = stat.get("steps")
                if isinstance(steps, int):
                    details.append(f"{steps} step" + ("" if steps == 1 else "s"))
                basis = {"evidence": "proven", "model": "judged by a model"}.get(stat.get("basis"))
                if basis:
                    details.append(basis)
                goal = str(stat.get("goal", ""))[:200]
                lines.append(f"{tick} {goal}" + (f" ({', '.join(details)})" if details else ""))
        elif rec.sub_goals:
            completed_set = set(rec.completed_goals)
            lines.append("Plan that was used:")
            for sg in rec.sub_goals:
                tick = "  ✓" if sg in completed_set else "  ✗"
                lines.append(f"{tick} {sg}")
        if rec.learnings:
            lines.append(f"Key learnings: {rec.learnings}")
        return lines

    def save(self, record: TaskRecord) -> None:
        """Persist a task record to disk."""
        self._ensure_loaded()
        self._records.append(record)

        self.memory_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.memory_file, "a") as f:
                f.write(json.dumps(asdict(record)) + "\n")
            logger.info("memory_saved", task=record.task[:60], success=record.success)
        except Exception as e:
            logger.warning("memory_save_error", error=str(e))

    def __len__(self) -> int:
        self._ensure_loaded()
        return len(self._records)
