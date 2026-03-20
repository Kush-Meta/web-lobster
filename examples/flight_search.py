"""Example: Multi-step web task with safety rails.

Demonstrates how Web Lobster handles a complex multi-step task
with dry-run mode enabled so nothing actually gets submitted.

Usage:
    python examples/flight_search.py
"""

import asyncio

from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.utils.logging import print_banner, set_log_level


async def main():
    print_banner()
    set_log_level("INFO")

    config = WebLobsterConfig.default()

    # Safety first: dry-run mode logs all actions without executing
    config.safety.dry_run = True
    config.browser.headless = False

    orchestrator = Orchestrator(config)
    result = await orchestrator.run(
        task=(
            "Go to Google Flights, search for the cheapest round-trip flight "
            "from LAX to JFK departing December 15 and returning December 22, "
            "then report the top 3 cheapest options with prices and airlines."
        ),
        start_url="https://www.google.com/travel/flights",
    )

    print(result.summary())

    # In dry-run mode, we can inspect what WOULD have been done
    if result.plan:
        print("\nPlanned sub-goals:")
        for sg in result.plan.sub_goals:
            print(f"  {sg.id}. {sg.goal}")
            print(f"     Success criteria: {sg.success_criteria}")
            print()


if __name__ == "__main__":
    asyncio.run(main())
