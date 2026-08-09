"""Entry point: `python -m smartboi.main`"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

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
    task = asyncio.ensure_future(engine.run_forever())
    # HA add-ons and Docker stop a container with SIGTERM, which Python's
    # default handler turns into an immediate process exit WITHOUT raising
    # KeyboardInterrupt -- so run_forever's finally-cleanup (close the HTTP
    # clients, disconnect IB) never ran on a normal add-on stop/restart, only
    # on Ctrl-C (SIGINT). Translate both signals into task cancellation so the
    # existing cleanup path runs on every shutdown, which the several-restarts-
    # a-day HA reality exercises constantly.
    loop = asyncio.get_running_loop()

    def _request_shutdown(signame: str) -> None:
        log.info("Shutdown requested (%s) -- stopping engine.", signame)
        task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _request_shutdown, sig.name)

    try:
        await task
    except (asyncio.CancelledError, KeyboardInterrupt):
        log.info("Shutdown complete.")


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
