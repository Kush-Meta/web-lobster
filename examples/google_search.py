"""Example: Simple Google search with Web Lobster.

Demonstrates the basic usage of the orchestrator to perform
a Google search and extract results.

Usage:
    python examples/google_search.py
"""

import asyncio

from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.utils.logging import print_banner, set_log_level


async def main():
    print_banner()
    set_log_level("DEBUG")

    # Use default config (modify as needed)
    config = WebLobsterConfig.default()

    # Override for this example: use headed browser so we can watch
    config.browser.headless = False
    config.browser.slow_mo = 200  # slow down so we can see what happens

    orchestrator = Orchestrator(config)
    result = await orchestrator.run(
        task="Search Google for 'best open source AI models 2025' and find the top 3 results",
        start_url="https://www.google.com",
    )

    print(result.summary())


if __name__ == "__main__":
    asyncio.run(main())
