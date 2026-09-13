"""Tests for the receipt chain."""

from __future__ import annotations

from web_lobster.verify.evidence import CheckResult
from web_lobster.verify.network import NetworkEvent
from web_lobster.verify.receipts import (
    GENESIS_DIGEST,
    ModelVerdict,
    Receipt,
    ReceiptLog,
    page_label,
    read_receipts,
    verify_chain,
    write_receipts,
)


def _receipt(sub_goal_id: int, achieved: bool = True) -> Receipt:
    return Receipt(
        sub_goal_id=sub_goal_id,
        goal=f"Goal {sub_goal_id}",
        achieved=achieved,
        basis="evidence",
        checks=[CheckResult(type="url", description="page URL matches x", passed=achieved, detail="d")],
        page="https://shop.example/confirmation",
        writes=[NetworkEvent(seq=1, method="POST", url="https://shop.example/api/book", status=201)],
    )


def _log(count: int = 3) -> ReceiptLog:
    log = ReceiptLog()
    for i in range(1, count + 1):
        log.append(_receipt(i))
    return log


def test_chain_links_and_verifies():
    log = _log()
    first, second, third = log.receipts
    assert first.prev_digest == GENESIS_DIGEST
    assert second.prev_digest == first.digest and third.prev_digest == second.digest
    assert log.head == third.digest
    assert verify_chain(log.receipts)


def test_editing_a_receipt_breaks_the_chain():
    log = _log()
    log.receipts[0].achieved = False
    assert not verify_chain(log.receipts)

    # Re-sealing the edited receipt doesn't help: the next one still points at the old digest.
    log.receipts[0].digest = log.receipts[0].compute_digest()
    assert not verify_chain(log.receipts)


def test_dropping_a_receipt_breaks_the_chain():
    log = _log()
    assert not verify_chain(log.receipts[1:])
    assert not verify_chain([log.receipts[0], log.receipts[2]])


def test_file_round_trip(tmp_path):
    log = _log()
    path = tmp_path / "run.jsonl"
    write_receipts(log.receipts, path)
    loaded = read_receipts(path)
    assert [r.digest for r in loaded] == [r.digest for r in log.receipts]
    assert verify_chain(loaded)


def test_summary_line_says_how_it_was_decided():
    assert "evidence 1/1 checks" in _receipt(1).summary_line()
    judged = Receipt(
        sub_goal_id=2, goal="Search", achieved=True, basis="model",
        model_verdict=ModelVerdict(achieved=True, confidence=0.82), page="https://shop.example/",
    )
    assert "judged by model (0.82), not verified" in judged.summary_line()


def test_last_for():
    log = ReceiptLog()
    log.append(_receipt(1, achieved=False))
    log.append(_receipt(2))
    log.append(_receipt(1))
    assert log.last_for(1).achieved is True
    assert log.last_for(9) is None


def test_page_label_drops_query():
    assert page_label("https://shop.example/confirmation/1?token=abc") == "https://shop.example/confirmation/1"
