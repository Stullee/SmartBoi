"""Reg SHO threshold list: fetching, parsing, and the borrow flag it feeds."""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from smartboi.paper_journal import assumes_borrow
from smartboi.regsho import RegShoClient

# Real shape: pipe-delimited, header row, trailing record count.
_FILE = (
    "Date|Symbol|SecurityName|Market Category|Reg SHO Threshold Flag|Rule 4320\n"
    "20260807|ABCD|Some Corp Common Stock|Q|Y|N\n"
    "20260807|WXYZ|Another Inc|N|Y|N\n"
    "20260807|BRK.A|Class A|N|Y|N\n"
    "File Creation Time: 0807202611:30|||||\n"
)


def _client(handler) -> RegShoClient:
    return RegShoClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.regsho_network
@pytest.mark.asyncio
async def test_the_current_days_list_is_parsed():
    client = _client(lambda request: httpx.Response(200, text=_FILE))

    assert await client.refresh(today=date(2026, 8, 7)) is True
    assert client.is_threshold("ABCD") is True
    assert client.is_threshold("abcd") is True  # case-insensitive
    assert client.is_threshold("BRK.A") is True  # dotted share class survives
    assert client.is_threshold("NOPE") is False
    assert client.count == 3
    assert client.as_of == "2026-08-07"


@pytest.mark.regsho_network
@pytest.mark.asyncio
async def test_the_header_and_trailer_rows_are_not_mistaken_for_tickers():
    """The file carries a header and a 'File Creation Time' trailer. Either one
    parsed as a symbol would silently flag a nonexistent ticker as
    hard-to-borrow, which reads as a data problem nowhere."""
    client = _client(lambda request: httpx.Response(200, text=_FILE))
    await client.refresh(today=date(2026, 8, 7))

    assert client.is_threshold("Symbol") is False
    assert not any("|" in s or " " in s for s in client._symbols)


@pytest.mark.regsho_network
@pytest.mark.asyncio
async def test_a_missing_file_walks_back_to_the_most_recent_published_day():
    """The list is published per SETTLEMENT day and lags -- today's file
    routinely 404s well into the session. Threshold status is persistent by
    construction (five consecutive fail-days on, five clean ones off), so the
    previous file is a good answer where no file is not."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "20260807" in str(request.url):
            return httpx.Response(404)
        return httpx.Response(200, text=_FILE)

    client = _client(handler)
    assert await client.refresh(today=date(2026, 8, 7)) is True
    assert client.as_of == "2026-08-06"
    assert len(seen) == 2


@pytest.mark.regsho_network
@pytest.mark.asyncio
async def test_weekends_are_skipped_rather_than_requested():
    """No settlement file exists on a Saturday or Sunday, so requesting one is
    a guaranteed 404 and a wasted round trip."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=_FILE)

    client = _client(handler)
    await client.refresh(today=date(2026, 8, 9))  # a Sunday

    assert "20260809" not in "".join(seen)
    assert "20260808" not in "".join(seen)  # Saturday
    assert client.as_of == "2026-08-07"  # Friday


@pytest.mark.regsho_network
@pytest.mark.asyncio
async def test_a_failed_refresh_keeps_the_previous_list():
    """Clearing on failure would silently flip every short back to the
    market-cap proxy on one bad fetch, changing the meaning of the recorded
    flag for a reason that has nothing to do with borrow."""
    ok = _client(lambda request: httpx.Response(200, text=_FILE))
    await ok.refresh(today=date(2026, 8, 7))

    ok._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(500)
    ))
    assert await ok.refresh(today=date(2026, 8, 14)) is False
    assert ok.is_threshold("ABCD") is True  # retained
    assert ok.as_of == "2026-08-07"


@pytest.mark.regsho_network
@pytest.mark.asyncio
async def test_a_network_error_is_never_raised_to_the_caller():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = _client(boom)
    assert await client.refresh(today=date(2026, 8, 7)) is False
    assert client.count == 0


def test_threshold_presence_flags_borrow_regardless_of_market_cap():
    """The whole point: a $5B name on the threshold list is hard to borrow as
    an observed fact, and the cap proxy would have called it freely
    borrowable."""
    assert assumes_borrow("SHORT", 5000.0, on_threshold_list=True) is True
    assert assumes_borrow("SHORT", 5000.0, on_threshold_list=False) is False


def test_absence_from_the_list_falls_back_to_the_proxy_rather_than_clearing():
    """The list names securities already FAILING to deliver, a subset of what
    is hard to borrow -- a thin micro-cap can be unborrowable with nobody
    having failed on it. So absence must not clear the flag."""
    assert assumes_borrow("SHORT", 120.0, on_threshold_list=False) is True
    assert assumes_borrow("SHORT", 120.0, on_threshold_list=None) is True
    assert assumes_borrow("SHORT", None, on_threshold_list=False) is True


def test_longs_never_assume_a_borrow():
    assert assumes_borrow("LONG", 50.0, on_threshold_list=True) is False


@pytest.mark.regsho_network
@pytest.mark.asyncio
async def test_a_failure_reports_every_url_it_tried(caplog):
    """Regression, found live: 48 consecutive failures logged only 'not found
    in the last 6 days', which cannot distinguish a wrong URL from a blocked
    host from a file that genuinely is not published -- three problems with
    three different fixes."""
    import logging

    client = _client(lambda request: httpx.Response(404))
    with caplog.at_level(logging.WARNING):
        assert await client.refresh(today=date(2026, 8, 7)) is False

    assert "nasdaqth20260807.txt" in caplog.text
    assert "nasdaqth20260806.txt" in caplog.text


@pytest.mark.regsho_network
@pytest.mark.asyncio
async def test_a_200_that_parses_to_nothing_says_so_with_the_body(caplog):
    """The most misleading of the three failures: host up, URL right, FORMAT
    changed. Only the body prefix distinguishes it from a 404."""
    import logging

    client = _client(lambda request: httpx.Response(200, text="<html>Maintenance</html>"))
    with caplog.at_level(logging.WARNING):
        assert await client.refresh(today=date(2026, 8, 7)) is False

    assert "0 symbols parsed" in caplog.text
    assert "Maintenance" in caplog.text
