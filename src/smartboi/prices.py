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
    a close-only mark silently erased exactly those stop-outs."""

    close: float
    high: float
    low: float

# Spaces out price lookups within one polling pass so a universe of symbols
# doesn't burst historical-data requests against IB's pacing limit (~60
# requests per rolling 10-minute window per connection).
#
# This alone is NOT enough to stay legal across a large universe -- 60
# requests per 600 seconds is one per 10s, so any pass longer than ~60
# symbols has to not use IB at all. That is why the daily forward-validation
# marks over the whole universe go to Finnhub first (see engine.py's
# _run_daily_price_marks): IB's request budget is reserved for the two jobs
# that actually need a broker-quality bar -- pricing an entry, and marking
# the handful of open paper trades.
_REQUEST_GAP_SEC = 1.0
# Every IB call is wrapped in this. ib_async futures are resolved by the
# Gateway's reply, and a dropped uplink (error 1100, which this deployment
# logs regularly) does not resolve the pending ones -- so an un-timed-out
# await can hang forever. The engine is a SINGLE task: one hung await stops
# ingestion, scoring, signalling and entry evaluation together, silently and
# with no error to log. A timeout converts that into "no price from IB",
# which _price_bar already handles by falling through to Finnhub.
_IB_CALL_TIMEOUT_SEC = 20.0


class ReadOnlyPriceFeed:
    def __init__(self, host: str, port: int, client_id: int):
        self._host = host
        self._port = port
        self._client_id = client_id
        self.ib = IB()
        self._contracts: dict[str, Stock] = {}

    async def connect(self) -> None:
        await self.ib.connectAsync(self._host, self._port, clientId=self._client_id, timeout=15)
        # Allow DELAYED data (type 3) as a fallback when the account has no
        # live market-data subscription for a name. Without this, an
        # unsubscribed symbol answers reqHistoricalDataAsync with nothing at
        # all, which this module can only report as "no price" -- and a
        # no-price symbol at the entry gate is a trade that never happens.
        # Delayed bars are 15-20 minutes old, which is immaterial to a
        # strategy holding for weeks and infinitely better than none.
        #
        # This is a market-data mode, not an order permission: it cannot
        # place, route or modify anything, so the paper-only guarantee in
        # this module's docstring is untouched. Best-effort -- an older
        # Gateway that doesn't accept it must not break the connection.
        try:
            self.ib.reqMarketDataType(3)
        except Exception as exc:  # noqa: BLE001 - purely an upgrade; never fail the connect over it
            log.debug("IB did not accept the delayed market-data request: %s", exc)
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
            # Indexed, not destructured. `[qualified] = await ...` raised
            # ValueError("not enough values to unpack") whenever IB returned
            # an EMPTY list, which is its normal answer for a symbol it can't
            # resolve -- a delisted ticker, a share class SMART doesn't route,
            # or (the case that actually bit) a Gateway whose security-
            # definition farm is not connected yet. That turned a routine
            # "no price for this symbol" into an exception, and in
            # _try_open_from_signal an exception is caught and returned from
            # WITHOUT ever reaching the entry-deadline check -- so a signal
            # could sit SIGNALED indefinitely on an unqualifiable symbol.
            qualified = await asyncio.wait_for(
                self.ib.qualifyContractsAsync(candidate), timeout=_IB_CALL_TIMEOUT_SEC
            )
            if not qualified or not getattr(qualified[0], "conId", None):
                log.warning("%s: could not qualify contract for price lookup.", symbol)
                return None
            self._contracts[symbol] = qualified[0]
            contract = qualified[0]

        bars = await asyncio.wait_for(
            self.ib.reqHistoricalDataAsync(
                contract, endDateTime="", durationStr="2 D", barSizeSetting="1 day",
                whatToShow="TRADES", useRTH=True,
            ),
            timeout=_IB_CALL_TIMEOUT_SEC,
        )
        if not bars:
            return None
        bar = bars[-1]
        return PriceBar(close=float(bar.close), high=float(bar.high), low=float(bar.low))

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
