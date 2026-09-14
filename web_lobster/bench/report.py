"""Text summaries of a benchmark run."""

from __future__ import annotations

from pydantic import BaseModel

from web_lobster.bench.runner import DEFENSES, BenchReport, RunOutcome

PERSONA_NOTES = {
    "hijacked": "Hijacked executor: obeys every planted instruction and claims success early",
    "honest": "Honest executor: does only the task (do the defenses get in the way?)",
    "live": "Your configured models",
}
_PERSONA_ORDER = {"hijacked": 0, "honest": 1, "live": 2}
_DEFENSE_ORDER = {d.name: i for i, d in enumerate(DEFENSES)}


class SummaryRow(BaseModel):
    defenses: str
    persona: str
    runs: int
    succeeded: int
    harmed: int
    leaked: int
    false_done: int
    blocked: int
    errors: int


def summarize(outcomes: list[RunOutcome]) -> list[SummaryRow]:
    groups: dict[tuple[str, str], list[RunOutcome]] = {}
    for outcome in outcomes:
        groups.setdefault((outcome.defenses, outcome.persona), []).append(outcome)
    rows = [
        SummaryRow(
            defenses=defenses, persona=persona, runs=len(group),
            succeeded=sum(o.succeeded for o in group),
            harmed=sum(o.harmed for o in group),
            leaked=sum(o.leaked for o in group),
            false_done=sum(o.false_done for o in group),
            blocked=sum(o.blocked for o in group),
            errors=sum(o.error is not None for o in group),
        )
        for (defenses, persona), group in groups.items()
    ]
    return sorted(rows, key=lambda r: (
        _PERSONA_ORDER.get(r.persona, 9), _DEFENSE_ORDER.get(r.defenses, 99),
    ))


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    cells = [headers, *[[str(c) for c in row] for row in rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]

    def line(row: list[str]) -> str:
        return "    " + "   ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()

    return [line(headers), line(["-" * w for w in widths]), *(line(row) for row in cells[1:])]


def render(report: BenchReport) -> str:
    lines = [f"\nPoisoned-page benchmark, {report.mode} mode: {len(report.outcomes)} runs in {report.seconds:.0f}s"]
    rows = summarize(report.outcomes)
    personas = sorted({o.persona for o in report.outcomes}, key=lambda p: _PERSONA_ORDER.get(p, 9))
    defense_names = sorted({o.defenses for o in report.outcomes}, key=lambda d: _DEFENSE_ORDER.get(d, 99))

    for persona in personas:
        lines += ["", f"  {PERSONA_NOTES.get(persona, persona)}"]
        lines += _table(
            ["defenses", "task done", "harmful effect", "email leaked", 'false "done"', "blocked"],
            [
                [r.defenses, f"{r.succeeded}/{r.runs}", f"{r.harmed}/{r.runs}",
                 f"{r.leaked}/{r.runs}", f"{r.false_done}/{r.runs}", r.blocked]
                for r in rows if r.persona == persona
            ],
        )

        verdicts = {(o.scenario, o.defenses): o.verdict() for o in report.outcomes if o.persona == persona}
        lines += ["", "    By scenario:"]
        lines += _table(
            ["scenario", *defense_names],
            [[s["id"], *(verdicts.get((s["id"], d), "-") for d in defense_names)] for s in report.scenarios],
        )

    gaps = [s for s in report.scenarios if s["known_gap"]]
    if gaps:
        lines += ["", "  Known gaps"] + [f"    {s['id']}: {s['known_gap']}" for s in gaps]

    errors = [o for o in report.outcomes if o.error]
    if errors:
        lines += ["", "  Errors"] + [
            f"    {o.scenario} / {o.defenses} / {o.persona}: {o.error.splitlines()[0][:160]}" for o in errors
        ]
    lines.append("")
    return "\n".join(lines)
