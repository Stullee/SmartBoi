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


# How many identical messages pass freely per window before the rest are
# collapsed into a count, and how long that window is.
#
# The 2026-08-21 SDK outage emitted one WARNING per failed call with no dedup:
# 109,092 lines, 66.9% of the entire 26.8MB retained log, which cut the
# forensic window from about 40 days to 6.4 and destroyed the pre-outage
# baseline the incident needed to be measured against. Nothing was learned
# from line 109,092 that line 5 had not already said.
#
# Keyed on the message TEMPLATE, not the formatted text, so a per-symbol flood
# ("SPWR: ...", "AOSL: ...", "INTT: ...") collapses as the one repeated event
# it actually is. Five per five minutes still shows the pattern and the
# symbols; the rest arrive as a count on the next line that gets through, so
# the volume is reported rather than silently dropped.
_REPEAT_BURST = 5
_REPEAT_WINDOW_SEC = 300.0
# Bound on distinct templates tracked, so a process that logs unbounded
# distinct templates cannot grow this without limit.
_REPEAT_MAX_KEYS = 512


class _CollapseRepeats(logging.Filter):
    """Rate-limits identical log TEMPLATES, reporting what it suppressed.

    One instance per handler -- filter() runs once per handler, so a shared
    instance would count each record twice and halve the real burst."""

    def __init__(self, burst: int = _REPEAT_BURST, window_sec: float = _REPEAT_WINDOW_SEC) -> None:
        super().__init__()
        self._burst = burst
        self._window = window_sec
        # key -> [window_started_at, emitted_in_window, suppressed_in_window]
        self._state: dict[tuple, list] = {}

    def filter(self, record: logging.LogRecord) -> bool:
        key = (record.name, record.levelno, str(record.msg))
        now = time.monotonic()
        state = self._state.get(key)
        if state is None or now - state[0] >= self._window:
            suppressed = state[2] if state is not None else 0
            if len(self._state) >= _REPEAT_MAX_KEYS and state is None:
                # Drop the coldest window rather than grow without bound.
                oldest = min(self._state, key=lambda k: self._state[k][0])
                self._state.pop(oldest, None)
            self._state[key] = [now, 1, 0]
            if suppressed:
                # Appended AFTER any %-args are accounted for: getMessage()
                # does `msg % args`, so this text must contain no % itself.
                record.msg = (f"{record.msg} [+{suppressed} identical message(s) suppressed "
                              f"in the previous {self._window:.0f}s]")
            return True
        if state[1] < self._burst:
            state[1] += 1
            return True
        state[2] += 1
        return False


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

    # Both filters go on the HANDLERS, not on a logger.
    #
    # This is the whole reason _IbBenignErrorFilter never worked: it was
    # attached to the `ib_async` logger, but ib_async logs through the CHILD
    # loggers `ib_async.wrapper` and `ib_async.client`, and Python does not run
    # an ancestor's filters when a record propagates upward -- only the
    # originating logger's, then every handler's. Measured on the retained log:
    # 100 records carrying codes this filter names (76x 1100, 19x 1102, 5x 322)
    # were written anyway, every one of them from ib_async.wrapper.
    #
    # A handler filter sees every record that reaches the handler regardless of
    # which logger emitted it, which is what both of these need. A separate
    # _CollapseRepeats per handler: filter() runs once per handler, so sharing
    # one instance would count each record twice.
    for handler in (console, file_handler):
        handler.addFilter(_IbBenignErrorFilter())
        handler.addFilter(_CollapseRepeats())

    # httpx logs full request URLs at INFO, which for Finnhub includes the
    # API key as a query parameter -- see TradingBot's logging_setup.py for
    # the confirmed-live leak this avoids. aiohttp's access log adds a line
    # per dashboard poll. This also quiets ib_async's own INFO-level
    # chatter (Connecting/Connected/API ready) -- but NOT its ERROR-level
    # noise, which needs the filter below.
    for noisy_logger in ("httpx", "httpcore", "aiohttp.access", "ib_async"):
        logging.getLogger(noisy_logger).setLevel(logging.WARNING)

    # Left clear on purpose: the filter that used to live here could not see
    # ib_async's child loggers at all (see the handler loop above). Cleared
    # rather than merely unused so a re-init cannot resurrect a stale copy.
    logging.getLogger("ib_async").filters.clear()
