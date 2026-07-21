"""Entry point: `python -m smartboi.main`"""
from __future__ import annotations

import asyncio
import logging
import os

from smartboi.config import load_settings
from smartboi.engine import Engine
from smartboi.logging_setup import setup_logging

log = logging.getLogger(__name__)


async def _amain() -> None:
    settings = load_settings()
    setup_logging(settings.log_level, settings.log_dir)

    # Set by the add-on Dockerfile at build time -- see ha-addons/smartboi.
    # Falls back to "dev"/"unknown" for a plain repo checkout.
    version = os.environ.get("SMARTBOI_VERSION", "dev")
    commit = os.environ.get("SMARTBOI_COMMIT", "unknown")
    log.info("=== SmartBoi version=%s commit=%s ===", version, commit)
    log.info(
        "PAPER-ONLY: this system contains no order-placement code (see prices.py, "
        "paper_journal.py). Every trade it 'makes' is a logged hypothetical, never a real order."
    )

    engine = Engine(settings)
    try:
        await engine.run_forever()
    except KeyboardInterrupt:
        log.info("Shutdown requested by user.")


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
