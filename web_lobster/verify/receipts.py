"""Receipts — a tamper-evident record of what each sub-goal did and how it was verified.

A receipt says whether a sub-goal counted as done and on what basis (evidence
checks, or a model's judgement when none were declared), which checks passed,
the page it ended on, and the write requests the browser sent. Receipts are
chained: each digest covers the previous one, so editing or dropping a receipt
breaks every digest after it. That proves something only if the last digest is
kept somewhere the agent can't rewrite.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Literal, Optional
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from web_lobster.core.values import origin_of
from web_lobster.verify.evidence import CheckResult
from web_lobster.verify.network import NetworkEvent

GENESIS_DIGEST = "0" * 64
MAX_RECEIPT_WRITES = 50


class ModelVerdict(BaseModel):
    achieved: bool
    confidence: float


class Receipt(BaseModel):
    sub_goal_id: int
    goal: str
    achieved: bool
    basis: Literal["evidence", "model"]
    checks: list[CheckResult] = Field(default_factory=list)
    model_verdict: Optional[ModelVerdict] = None
    page: str
    writes: list[NetworkEvent] = Field(default_factory=list)
    timestamp: float = Field(default_factory=time.time)
    prev_digest: str = GENESIS_DIGEST
    digest: str = ""

    def compute_digest(self) -> str:
        payload = self.model_dump(mode="json", exclude={"digest"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(canonical.encode()).hexdigest()

    def summary_line(self) -> str:
        icon = "✓" if self.achieved else "✗"
        if self.basis == "evidence":
            passed = sum(check.passed for check in self.checks)
            how = f"evidence {passed}/{len(self.checks)} checks"
        else:
            confidence = self.model_verdict.confidence if self.model_verdict else 0.0
            how = f"judged by model ({confidence:.2f}), not verified"
        return f"{icon} [{self.sub_goal_id}] {self.goal} ({how})"


class ReceiptLog:
    """The run's receipts, sealed into a hash chain as they're added."""

    def __init__(self):
        self.receipts: list[Receipt] = []

    @property
    def head(self) -> str:
        return self.receipts[-1].digest if self.receipts else GENESIS_DIGEST

    def append(self, receipt: Receipt) -> Receipt:
        sealed = receipt.model_copy(update={"prev_digest": self.head})
        sealed.digest = sealed.compute_digest()
        self.receipts.append(sealed)
        return sealed

    def last_for(self, sub_goal_id: int) -> Optional[Receipt]:
        return next((r for r in reversed(self.receipts) if r.sub_goal_id == sub_goal_id), None)


def verify_chain(receipts: list[Receipt]) -> bool:
    """True if every receipt is unmodified and links to the one before it."""
    previous = GENESIS_DIGEST
    for receipt in receipts:
        if receipt.prev_digest != previous or receipt.compute_digest() != receipt.digest:
            return False
        previous = receipt.digest
    return True


def page_label(url: str) -> str:
    """Origin and path of a URL. The query is left out: it often carries tokens."""
    parts = urlsplit(url)
    return origin_of(url) + (parts.path if parts.hostname else "")


def write_receipts(receipts: list[Receipt], path: str | Path) -> None:
    with open(path, "w") as f:
        for receipt in receipts:
            f.write(receipt.model_dump_json() + "\n")


def read_receipts(path: str | Path) -> list[Receipt]:
    return [
        Receipt.model_validate_json(line)
        for line in Path(path).read_text().splitlines()
        if line.strip()
    ]
