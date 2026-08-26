import logging
import time

from smartboi.logging_setup import setup_logging


def _log_lines(tmp_path, logger_name: str, *messages: str) -> str:
    """What actually lands in smartboi.log when `logger_name` logs.

    Goes through the whole real chain -- originating logger, propagation to
    root, root's handlers and their filters -- rather than inspecting one
    logger's filters. That distinction is the entire bug these tests missed:
    ib_async logs through the CHILD loggers ib_async.wrapper and
    ib_async.client, Python does not run an ancestor's filters on propagation,
    and so the benign-error filter sat on `ib_async` doing nothing while 100
    records it names were written to the live log."""
    setup_logging(level="INFO", log_dir=str(tmp_path))
    logger = logging.getLogger(logger_name)
    for message in messages:
        logger.error(message)
    for handler in logging.getLogger().handlers:
        handler.flush()
    return (tmp_path / "smartboi.log").read_text()


def test_setup_logging_quiets_info_level_chatter(tmp_path):
    """setLevel(WARNING) suppresses INFO-level chatter (e.g. ib_async's
    Connecting/Connected/API ready messages) -- but does NOT by itself
    drop ERROR-level noise, since ERROR is above WARNING in severity. See
    the ib_async-specific tests below for that half."""
    setup_logging(level="INFO", log_dir=str(tmp_path))
    for name in ("httpx", "httpcore", "aiohttp.access", "ib_async"):
        assert logging.getLogger(name).level == logging.WARNING


def test_ib_async_benign_error_codes_are_filtered(tmp_path):
    text = _log_lines(
        tmp_path, "ib_async.wrapper",
        "Error 1100, reqId -1: Connectivity between IB and TWS has been lost.",
        "Error 1102, reqId -1: Connectivity between IB and TWS has been restored.",
        "Error 2104, reqId -1: Market data farm connection is OK.",
        "Error 2106, reqId -1: HMDS data farm connection is OK.",
        "Error 2158, reqId -1: Sec-def data farm connection is OK.",
        "Error 322, reqId 3: Maximum number of account summary requests exceeded.",
    )
    assert text.strip() == ""


def test_ib_async_genuine_errors_still_come_through(tmp_path):
    text = _log_lines(
        tmp_path, "ib_async.wrapper",
        "Error 200, reqId 5: No security definition has been found for the request.",
    )
    assert "Error 200" in text


def test_ib_async_filter_only_matches_the_error_n_shape(tmp_path):
    # Contains a benign code's digits, but not in the "Error N" shape the
    # filter matches on -- must not be treated as a false positive match.
    text = _log_lines(
        tmp_path, "ib_async.wrapper",
        "Something failed while processing reqId 1100, unrelated to an error code.",
    )
    assert "unrelated to an error code" in text


def test_setup_logging_does_not_stack_duplicate_filters(tmp_path):
    setup_logging(level="INFO", log_dir=str(tmp_path))
    setup_logging(level="INFO", log_dir=str(tmp_path))
    # root.handlers.clear() drops the old handlers and their filters with
    # them, so a second call cannot stack a second copy of either filter.
    for handler in logging.getLogger().handlers:
        assert len(handler.filters) == 2
    assert logging.getLogger("ib_async").filters == []


# --- The filters run where the records actually pass ---------------------

def test_a_benign_code_from_ib_asyncs_CHILD_logger_is_filtered(tmp_path):
    """The live case. ib_async never logs through `ib_async` itself: all 100
    unfiltered records in the retained log came from ib_async.wrapper."""
    text = _log_lines(
        tmp_path, "ib_async.wrapper",
        "Error 1100, reqId -1: Connectivity between IB and TWS has been lost.",
        "Error 322, reqId 3: Maximum number of account summary requests exceeded.",
    )
    assert "Error 1100" not in text
    assert "Error 322" not in text


def test_a_genuine_error_from_a_child_logger_still_lands(tmp_path):
    text = _log_lines(
        tmp_path, "ib_async.client",
        "Error 200, reqId 5: No security definition has been found for the request.",
    )
    assert "Error 200" in text


# --- Collapsing a repeated template --------------------------------------

def test_a_repeated_template_is_collapsed_and_the_rest_counted(tmp_path):
    """109,092 identical warnings were 66.9% of the retained log and cut the
    forensic window from ~40 days to 6.4. The burst still shows the pattern;
    the remainder is reported as a count, not silently dropped."""
    from smartboi.logging_setup import _REPEAT_BURST

    setup_logging(level="INFO", log_dir=str(tmp_path))
    logger = logging.getLogger("smartboi.dossier")
    for symbol in ("SPWR", "AOSL", "INTT", "RJET", "BKTI", "WOLF", "AGEN", "POWI"):
        logger.warning("%s: dossier update proposal failed: %s", symbol, "boom")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (tmp_path / "smartboi.log").read_text()

    # Same TEMPLATE, different symbols -- collapsed as the one repeated event
    # it is, which is what makes this work on a per-symbol flood at all.
    assert text.count("dossier update proposal failed") == _REPEAT_BURST
    assert "SPWR" in text and "POWI" not in text


def test_a_different_template_is_not_collapsed_with_it(tmp_path):
    setup_logging(level="INFO", log_dir=str(tmp_path))
    logger = logging.getLogger("smartboi.dossier")
    for _ in range(10):
        logger.warning("%s: dossier update proposal failed", "SPWR")
    logger.warning("%s: something entirely different", "SPWR")
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = (tmp_path / "smartboi.log").read_text()

    assert "something entirely different" in text


def test_the_suppressed_count_is_reported_when_the_window_rolls(tmp_path):
    from smartboi.logging_setup import _CollapseRepeats

    collapse = _CollapseRepeats(burst=2, window_sec=0.0)   # every call a new window
    records = []
    for _ in range(3):
        record = logging.LogRecord("smartboi.dossier", logging.WARNING, "f", 1,
                                   "%s: failed", ("SPWR",), None)
        if collapse.filter(record):
            records.append(record)
    # window_sec=0 means each call opens a fresh window, so nothing is
    # suppressed and nothing is annotated.
    assert len(records) == 3

    collapse = _CollapseRepeats(burst=1, window_sec=3600.0)
    kept = []
    for _ in range(5):
        record = logging.LogRecord("smartboi.dossier", logging.WARNING, "f", 1,
                                   "%s: failed", ("SPWR",), None)
        if collapse.filter(record):
            kept.append(record)
    assert len(kept) == 1          # one through, four suppressed and counted
    assert collapse._state[("smartboi.dossier", logging.WARNING, "%s: failed")][2] == 4


def test_an_annotated_message_still_formats_its_args(tmp_path):
    """The suppression note is appended to the TEMPLATE, and getMessage() then
    does `msg % args`. A stray % in that note would raise here."""
    from smartboi.logging_setup import _CollapseRepeats

    collapse = _CollapseRepeats(burst=1, window_sec=0.001)
    first = logging.LogRecord("n", logging.WARNING, "f", 1, "%s: failed", ("SPWR",), None)
    assert collapse.filter(first)
    assert not collapse.filter(
        logging.LogRecord("n", logging.WARNING, "f", 1, "%s: failed", ("AOSL",), None))
    time.sleep(0.002)
    third = logging.LogRecord("n", logging.WARNING, "f", 1, "%s: failed", ("INTT",), None)
    assert collapse.filter(third)
    assert third.getMessage() == "INTT: failed [+1 identical message(s) suppressed in the previous 0s]"
