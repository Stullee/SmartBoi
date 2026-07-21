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

from ib_async import IB, Stock

log = logging.getLogger(__name__)

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

    async def last_price(self, symbol: str) -> float | None:
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
        return float(bars[-1].close)

    async def last_prices(self, symbols: list[str]) -> dict[str, float]:
        """Sequential with a small gap between requests -- see module
        docstring on pacing. A failure for one symbol must not stop the
        rest of the universe from being priced."""
        prices: dict[str, float] = {}
        for i, symbol in enumerate(symbols):
            if i > 0:
                await asyncio.sleep(_REQUEST_GAP_SEC)
            try:
                price = await self.last_price(symbol)
            except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                log.exception("%s: price lookup failed.", symbol)
                continue
            if price is not None:
                prices[symbol] = price
        return prices

    def account_net_liquidation(self) -> float | None:
        for v in self.ib.accountValues():
            if v.tag == "NetLiquidation":
                return float(v.value)
        return None

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
