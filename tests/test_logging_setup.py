import logging

from smartboi.logging_setup import setup_logging


class _RecordCollector(logging.Handler):
    """Attached directly to the logger under test rather than relying on
    caplog -- setup_logging() calls root.handlers.clear() (by design, so
    repeated calls don't stack duplicate handlers), which would also wipe
    out caplog's own handler if it's attached to root, making caplog
    unreliable here regardless of call order. A handler attached straight
    to the logger under test isn't affected by anything happening to root,
    and observes exactly the thing being tested: did a record survive that
    logger's own filters."""

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _collect_ib_async_records(*messages: str) -> list[logging.LogRecord]:
    ib_logger = logging.getLogger("ib_async")
    collector = _RecordCollector()
    ib_logger.addHandler(collector)
    try:
        for message in messages:
            ib_logger.error(message)
    finally:
        ib_logger.removeHandler(collector)
    return collector.records


def test_setup_logging_quiets_info_level_chatter(tmp_path):
    """setLevel(WARNING) suppresses INFO-level chatter (e.g. ib_async's
    Connecting/Connected/API ready messages) -- but does NOT by itself
    drop ERROR-level noise, since ERROR is above WARNING in severity. See
    the ib_async-specific tests below for that half."""
    setup_logging(level="INFO", log_dir=str(tmp_path))
    for name in ("httpx", "httpcore", "aiohttp.access", "ib_async"):
        assert logging.getLogger(name).level == logging.WARNING


def test_ib_async_benign_error_codes_are_filtered(tmp_path):
    setup_logging(level="INFO", log_dir=str(tmp_path))
    records = _collect_ib_async_records(
        "Error 1100, reqId -1: Connectivity between IB and TWS has been lost.",
        "Error 1102, reqId -1: Connectivity between IB and TWS has been restored.",
        "Error 2104, reqId -1: Market data farm connection is OK.",
        "Error 2106, reqId -1: HMDS data farm connection is OK.",
        "Error 2158, reqId -1: Sec-def data farm connection is OK.",
        "Error 322, reqId 3: Maximum number of account summary requests exceeded.",
    )
    assert records == []


def test_ib_async_genuine_errors_still_come_through(tmp_path):
    setup_logging(level="INFO", log_dir=str(tmp_path))
    records = _collect_ib_async_records(
        "Error 200, reqId 5: No security definition has been found for the request."
    )
    assert len(records) == 1
    assert "Error 200" in records[0].getMessage()


def test_ib_async_filter_only_matches_the_error_n_shape(tmp_path):
    # Contains a benign code's digits, but not in the "Error N" shape the
    # filter matches on -- must not be treated as a false positive match.
    setup_logging(level="INFO", log_dir=str(tmp_path))
    records = _collect_ib_async_records(
        "Something failed while processing reqId 1100, unrelated to an error code."
    )
    assert len(records) == 1


def test_setup_logging_does_not_stack_duplicate_filters(tmp_path):
    setup_logging(level="INFO", log_dir=str(tmp_path))
    setup_logging(level="INFO", log_dir=str(tmp_path))
    assert len(logging.getLogger("ib_async").filters) == 1
