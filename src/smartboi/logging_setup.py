"""Console + rotating file logging setup."""
from __future__ import annotations

import logging
import re
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ib_async logs these at ERROR even though they're routine IB Gateway
# connectivity blips / account-summary-limit noise, not real problems:
# 1100/1102 (connectivity lost/restored), 2104/2106/2158 (market/HMDS/
# sec-def data farm "connection is OK" -- yes, logged as an error), 322
# (account summary request cap). setLevel(WARNING) alone can't drop these
# -- ERROR is ABOVE WARNING in severity, so it still passes a level
# filter; only a message-level Filter actually removes them, and it does
# so without raising the whole logger to CRITICAL and hiding a genuine IB
# failure along with the noise.
_IB_BENIGN_ERROR_CODES = frozenset({"1100", "1102", "2104", "2106", "2158", "322"})
_IB_ERROR_CODE_RE = re.compile(r"\bError (\d+)\b")


class _IbBenignErrorFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        match = _IB_ERROR_CODE_RE.search(record.getMessage())
        return match is None or match.group(1) not in _IB_BENIGN_ERROR_CODES


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
    # per dashboard poll. This also quiets ib_async's own INFO-level
    # chatter (Connecting/Connected/API ready) -- but NOT its ERROR-level
    # noise, which needs the filter below.
    for noisy_logger in ("httpx", "httpcore", "aiohttp.access", "ib_async"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    # See _IbBenignErrorFilter above for why setLevel alone doesn't drop
    # this. filters.clear() first keeps this idempotent across repeated
    # setup_logging() calls (tests, or any future re-init) -- otherwise
    # every call would stack another copy of the same filter.
    ib_logger = logging.getLogger("ib_async")
    ib_logger.filters.clear()
    ib_logger.addFilter(_IbBenignErrorFilter())
