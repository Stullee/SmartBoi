"""Console + rotating file logging setup."""
from __future__ import annotations

import logging
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    Path(log_dir).mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s UTC | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Every other timestamp this system produces (evidence, dossiers, paper
    # trades) is UTC -- force log timestamps to match so they're never
    # silently comparing two different timezones.
    fmt.converter = time.gmtime

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        Path(log_dir) / "smartboi.log", maxBytes=5_000_000, backupCount=5
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # httpx logs full request URLs at INFO, which for Finnhub includes the
    # API key as a query parameter -- see TradingBot's logging_setup.py for
    # the confirmed-live leak this avoids. aiohttp's access log adds a line
    # per dashboard poll. ib_async logs routine IB Gateway connectivity
    # blips (Error 1100/1102) and account-summary noise (Error 322) at
    # ERROR even though they're harmless library/Gateway artifacts that
    # don't affect price marks -- confirmed live: ~100 of these a day,
    # drowning out anything that's an actual SmartBoi problem.
    for noisy_logger in ("httpx", "httpcore", "aiohttp.access", "ib_async"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)
