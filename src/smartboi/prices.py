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
import time
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

# Data-level circuit breaker. The per-call timeout above bounds a SINGLE call,
# but a half-dead Gateway (isConnected() true, so ensure_connected never
# reconnects, while its data farms hang EVERY request) makes every symbol cost
# the full timeout, every poll, forever: the single-task engine then spends
# ~N x 21s per pass hung on IB before falling to Finnhub -- measured at ~10.5
# min of a 15-min cycle at 30 open positions, and up to ~73 min for a
# whole-universe daily-marks pass. After this many CONSECUTIVE call timeouts
# the breaker opens: IB is skipped entirely (callers go straight to Finnhub)
# until the cooldown elapses, at which point one call probes it and either
# closes the breaker (recovered) or re-opens it. Consecutive, so an occasional
# unqualifiable symbol never trips it.
_IB_BREAKER_THRESHOLD = 5
_IB_BREAKER_COOLDOWN_SEC = 1800.0  # 30 min


class ReadOnlyPriceFeed:
    def __init__(self, host: str, port: int, client_id: int):
        self._host = host
        self._port = port
        self._client_id = client_id
        self.ib = IB()
        self._contracts: dict[str, Stock] = {}
        # Circuit-breaker state (see the module constants). monotonic so a
        # wall-clock adjustment can't wedge the breaker open or closed.
        self._consecutive_failures = 0
        self._breaker_until = 0.0

    def _breaker_open(self) -> bool:
        return time.monotonic() < self._breaker_until

    def _record_ib_success(self) -> None:
        """IB answered (a bar, an empty result, or 'no such contract') -- it is
        not hanging, so clear the failure streak."""
        self._consecutive_failures = 0

    def _record_ib_failure(self) -> None:
        """An IB call timed out. Trip the breaker once the streak crosses the
        threshold; a streak already past it (a post-cooldown probe that timed
        out again) re-opens it for another cooldown."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= _IB_BREAKER_THRESHOLD and not self._breaker_open():
            self._breaker_until = time.monotonic() + _IB_BREAKER_COOLDOWN_SEC
            log.warning(
                "IB price feed timed out on %d consecutive calls -- opening the circuit breaker: "
                "skipping IB for %d min and pricing off Finnhub. (The Gateway is likely up but its "
                "data farm is hung; a per-symbol timeout of %.0fs would otherwise stall the whole "
                "single-task engine every poll.)",
                self._consecutive_failures, int(_IB_BREAKER_COOLDOWN_SEC / 60), _IB_CALL_TIMEOUT_SEC,
            )

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
        # Breaker open: skip IB entirely and report "no price" so the caller
        # falls straight through to Finnhub, instead of paying the timeout.
        if self._breaker_open():
            return None
        try:
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
                    # IB ANSWERED (there is just no such contract) -- not a hang,
                    # so it clears the failure streak rather than tripping the
                    # breaker.
                    self._record_ib_success()
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
        except asyncio.TimeoutError:
            # The half-dead-Gateway signature: connected, but every data request
            # hangs to the timeout. Count it toward the breaker and report "no
            # price" so the caller falls through to Finnhub.
            self._record_ib_failure()
            return None
        self._record_ib_success()
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
        if self._breaker_open():
            # Report once, not a traceback per symbol: with the breaker open
            # every symbol would just return None. The caller prices them off
            # Finnhub. This is the fix for the per-symbol ERROR spam that
            # otherwise filled the rotating log during an IB outage.
            log.warning(
                "IB circuit breaker open -- skipping IB for %d symbol(s) this pass, using Finnhub.",
                len(symbols),
            )
            return {}
        bars: dict[str, PriceBar] = {}
        errors = 0
        for i, symbol in enumerate(symbols):
            if i > 0:
                await asyncio.sleep(_REQUEST_GAP_SEC)
            try:
                bar = await self.last_bar(symbol)
            except Exception:  # noqa: BLE001 - one bad symbol must not stop the rest
                errors += 1
                log.debug("%s: price lookup failed.", symbol)
                continue
            if bar is not None:
                bars[symbol] = bar
            # The breaker tripped partway through (a run of timeouts) -- stop
            # hammering IB for the rest of the universe and let them fall to
            # Finnhub, rather than paying the timeout on every remaining symbol.
            if self._breaker_open():
                skipped = len(symbols) - (i + 1)
                if skipped:
                    log.warning(
                        "IB circuit breaker tripped mid-pass -- skipping the remaining %d symbol(s), "
                        "using Finnhub.", skipped,
                    )
                break
        if errors:
            log.warning("IB price lookup errored on %d symbol(s) this pass (using Finnhub).", errors)
        return bars

    async def last_prices(self, symbols: list[str]) -> dict[str, float]:
        return {symbol: bar.close for symbol, bar in (await self.last_bars(symbols)).items()}

    def disconnect(self) -> None:
        if self.ib.isConnected():
            self.ib.disconnect()
