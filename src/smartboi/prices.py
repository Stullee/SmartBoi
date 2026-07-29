"""Read-only Interactive Brokers price/equity client -- used ONLY to mark
hypothetical paper trades to market (see paper_journal.py) and for context
on the dashboard. This file contains NO order-placement code whatsoever: no
placeOrder, no Trade, no bracket -- by design, so there is no code path
through which this system could ever submit a real order. See README's
"hardcoded paper-only" guarantee, which this file is the concrete
implementation of.

Disabled by default (ENABLE_IB_PRICE_FEED=false). Until configured, the
ingestion/graph/dossier/signal pipeline still runs and logs fully -- it
just can't mark a hypothetical position to market or open a new one yet
(see engine.py, which logs signals to signals.jsonl regardless)."""
from __future__ import annotations

import asyncio
import logging
from typing import NamedTuple

from ib_async import IB, Stock

log = logging.getLogger(__name__)


class PriceBar(NamedTuple):
    """The most recent daily bar's close plus its intraday extremes --
    high/low exist so paper-trade stop/target checks can see a level that
    traded intraday but recovered by the close (see paper_journal.update);
    a close-only mark silently erased exactly those stop-outs.

    `open` and `date` (the bar's SESSION date, ISO, exchange calendar) exist
    so the engine can tell whether this bar's session actually postdates a
    signal: when the market is closed, IB returns the last COMPLETED bar,
    whose close predates any evidence that arrived after the bell -- filling
    a paper trade from it books the next session's reaction gap as P&L no
    real order could have captured (see engine._try_open_from_signal).
    Both default empty/zero so price sources that can't supply them (tests,
    fallbacks) degrade to the old close-only behavior instead of breaking."""

    close: float
    high: float
    low: float
    open: float = 0.0
    date: str = ""

# Spaces out price lookups within one polling pass so a ~40-symbol universe
# doesn't burst 40 historical-data requests at once against IB's pacing
# limit (~60 requests per rolling 10-minute window per connection).
_REQUEST_GAP_SEC = 1.0


class ReadOnlyPriceFeed:
    def __init__(self, host: str, port: int, client_id: int):
        self._host = host
        self._port = port
        self._client_id = client_id
        self.ib = IB()
        self._contracts: dict[str, Stock] = {}

    async def connect(self) -> None:
        await self.ib.connectAsync(self._host, self._port, clientId=self._client_id, timeout=15)
        log.info("Connected read-only price feed to IB at %s:%s (client_id=%s)", self._host, self._port, self._client_id)

    async def ensure_connected(self) -> bool:
        """(Re)connects if the session is down -- IB Gateway restarts itself
        daily by design, and the Gateway may simply not be running yet when
        the engine starts, so the engine calls this before every price poll
        instead of trusting a connection made once at startup."""
        if self.ib.isConnected():
            return True
        try:
            await self.connect()
            return True
        except Exception as exc:  # noqa: BLE001 - an unreachable Gateway must never kill the poll loop
            log.warning("IB price feed unreachable at %s:%s (%s) -- will retry.", self._host, self._port, exc)
            return False

    async def last_bar(self, symbol: str) -> PriceBar | None:
        contract = self._contracts.get(symbol)
        if contract is None:
            candidate = Stock(symbol, "SMART", "USD")
            [qualified] = await self.ib.qualifyContractsAsync(candidate)
            if qualified is None or not getattr(qualified, "conId", None):
                log.warning("%s: could not qualify contract for price lookup.", symbol)
                return None
            self._contracts[symbol] = qualified
            contract = qualified

        bars = await self.ib.reqHistoricalDataAsync(
            contract, endDateTime="", durationStr="2 D", barSizeSetting="1 day",
            whatToShow="TRADES", useRTH=True,
        )
        if not bars:
            return None
        bar = bars[-1]
        # bar.date is the bar's session date for daily bars (a datetime.date).
        return PriceBar(close=float(bar.close), high=float(bar.high), low=float(bar.low),
                        open=float(bar.open), date=str(bar.date))

    async def last_price(self, symbol: str) -> float | None:
        bar = await self.last_bar(symbol)
        return bar.close if bar is not None else None

    async def last_bars(self, symbols: list[str]) -> dict[str, PriceBar]:
        """Sequential with a small gap between requests -- see module
        docstring on pacing. A failure for one symbol must not stop the
        rest of the universe from being priced."""
        bars: dict[str, PriceBar] = {}
        for i, symbol in enumerate(symbols):
            if i > 0:
                await asyncio.sleep(_REQUEST_GAP_SEC)
            try:
                bar = await self.last_bar(symbol)
            except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                log.exception("%s: price lookup failed.", symbol)
                continue
            if bar is not None:
                bars[symbol] = bar
        return bars

    async def last_prices(self, symbols: list[str]) -> dict[str, float]:
        return {symbol: bar.close for symbol, bar in (await self.last_bars(symbols)).items()}

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
