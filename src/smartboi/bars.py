"""Real historical daily OHLC bars -- the market ground truth the paper
record gets checked against.

Everything else in this system measures forward performance against
`logs/price_marks.jsonl`: one price per symbol per day, captured live by
the running engine (see forward_returns.py). That log is the right tool
for "does the score predict returns" and the wrong one for "what actually
happened to the names we traded", for three reasons:

- **It cannot be backfilled.** It starts the day capture started, so on a
  young deployment the 20-day horizon every drift question turns on is
  simply not in the file yet.
- **It has holes.** A mark is written only if a price source answered that
  day; an IB outage or a Finnhub 429 silently drops a symbol-day, and the
  forward-return join then reaches forward up to five CALENDAR days for a
  substitute, quietly stretching the very window it is measuring.
- **It is close-only.** A stop that traded intraday and recovered by the
  close does not exist in it at all.

Historical bars have none of those problems: they are complete, they carry
the intraday extremes, and -- the point of this module -- they can be
fetched today for a window that already happened. That is what makes a
real backtest of the existing record possible at all, rather than another
forward-looking capture that needs another month before it says anything.

Read-only market data, like prices.py: this module fetches bars and writes
a cache. It contains no order-placement code and no broker connection.

Providers, in the order a deployment is likely to have them:

- **stooq** (default) -- daily OHLC CSV for US listings, no API key, no
  account, full history in one request per symbol. Split-adjusted but not
  dividend-adjusted, which is immaterial over the days-to-weeks windows
  this analysis uses on names that mostly pay nothing; the reconciliation
  pass in backtest.py flags any symbol whose bars disagree with the price
  the engine actually recorded, so an adjustment artefact shows up as a
  flagged row rather than a silent 40% "move".
- **tiingo** -- needs a free key, returns fully adjusted (split AND
  dividend) bars. Preferred when a key exists.

Both are cached to disk (`data/bars/<SYMBOL>.csv`) so re-running the
report costs no requests, and so the analysis itself can run with no
network at all once the bars are in hand."""
from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

import httpx

log = logging.getLogger(__name__)

STOOQ_URL = "https://stooq.com/q/d/l/"
TIINGO_URL = "https://api.tiingo.com/tiingo/daily/{symbol}/prices"

# Spacing between symbol requests. Neither provider publishes a hard rate
# limit for these endpoints, but a universe-sized burst from one IP is how
# a keyless free service decides to start refusing -- and this whole
# module is worthless the moment the source blocks the deployment. One
# request every ~400ms fetches a 40-symbol universe in under 20 seconds,
# which is not worth optimising against that risk.
_REQUEST_GAP_SEC = 0.4
_TIMEOUT_SEC = 30.0
_MAX_ATTEMPTS = 3

# A cached series is refetched when its newest bar is older than this many
# days before the requested end date. Not zero: the most recent session's
# bar does not exist until that session closes, and a run at 11:00 ET would
# otherwise refetch every symbol every time, forever, to be told the same
# thing. Three days covers a weekend plus a holiday.
_CACHE_STALE_DAYS = 3

PROVIDERS = ("stooq", "tiingo")


class DailyBar(NamedTuple):
    """One session. `date` is the exchange-local session date (YYYY-MM-DD),
    which is what every event-time alignment downstream keys on -- not a
    UTC timestamp, because the UTC date rolls thirteen and a half hours
    before the session it would be naming (the same trap market_hours.py
    documents for the live path)."""

    date: str
    open: float
    high: float
    low: float
    close: float


class BarFetchError(RuntimeError):
    """A provider answered, but not with bars -- an unknown symbol, a plan
    rejection, a maintenance page. Raised rather than returned as an empty
    series so a whole-universe fetch can report which symbols failed and
    why, instead of leaving the caller to guess whether "no bars" means
    "delisted" or "the key is wrong"."""


def stooq_symbol(symbol: str) -> str:
    """SmartBoi's ticker as Stooq spells it: lowercase, US suffix, and
    class separators as hyphens (BRK.B -> brk-b.us). Getting this wrong
    returns a valid-looking empty CSV rather than an error, so it is its
    own function with its own test."""
    return f"{symbol.strip().lower().replace('.', '-')}.us"


def parse_stooq_csv(text: str) -> list[DailyBar]:
    """Stooq's daily CSV -> bars, oldest first.

    A symbol Stooq does not carry answers 200 with the body "No data" (or
    a header and nothing else), not an HTTP error -- so an empty result
    here is a real outcome the caller must distinguish, and this raises
    rather than returning [] for a body that isn't a bar table at all."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    if not lines:
        raise BarFetchError("empty response")
    header = lines[0].lower()
    if not header.startswith("date,"):
        raise BarFetchError(f"unexpected response: {lines[0][:80]!r}")
    bars: list[DailyBar] = []
    for line in lines[1:]:
        parts = line.split(",")
        if len(parts) < 5:
            continue
        try:
            bar = DailyBar(
                date=parts[0][:10],
                open=float(parts[1]),
                high=float(parts[2]),
                low=float(parts[3]),
                close=float(parts[4]),
            )
        except ValueError:
            # Stooq writes "N/A" into a field on a session with no print
            # rather than omitting the row. Skip the row; do not let one
            # malformed session drop the other 500.
            continue
        bars.append(bar)
    return sorted(bars, key=lambda b: b.date)


def parse_tiingo_json(rows: list[dict]) -> list[DailyBar]:
    """Tiingo's daily JSON -> bars, oldest first, using the ADJUSTED
    fields. Tiingo returns both; the raw ones would reintroduce exactly
    the split artefact the adjusted series exists to remove."""
    bars: list[DailyBar] = []
    for row in rows:
        stamp = row.get("date") or ""
        try:
            bar = DailyBar(
                date=stamp[:10],
                open=float(row["adjOpen"]),
                high=float(row["adjHigh"]),
                low=float(row["adjLow"]),
                close=float(row["adjClose"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if bar.date:
            bars.append(bar)
    return sorted(bars, key=lambda b: b.date)


# --- disk cache -------------------------------------------------------

def cache_path(cache_dir: Path, symbol: str) -> Path:
    return cache_dir / f"{symbol.upper().replace('/', '_')}.csv"


def write_cache(path: Path, bars: list[DailyBar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["date,open,high,low,close"]
    lines += [f"{b.date},{b.open},{b.high},{b.low},{b.close}" for b in bars]
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(path)


def read_cache(path: Path) -> list[DailyBar]:
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    try:
        return parse_stooq_csv(text)  # same shape: date,o,h,l,c
    except BarFetchError:
        return []


def cache_is_stale(bars: list[DailyBar], end_date: str, stale_days: int = _CACHE_STALE_DAYS) -> bool:
    """Whether the cached series stops far enough short of the window the
    caller needs to be worth another request."""
    if not bars:
        return True
    try:
        newest = date.fromisoformat(bars[-1].date)
        wanted = date.fromisoformat(end_date)
    except ValueError:
        return True
    return (wanted - newest).days > stale_days


# --- fetching ---------------------------------------------------------

class BarClient:
    """Fetches daily bars, cache first. One instance per run; call
    `aclose()` when done (or use it as an async context manager)."""

    def __init__(
        self,
        cache_dir: Path,
        provider: str = "stooq",
        tiingo_token: str = "",
        offline: bool = False,
    ):
        if provider not in PROVIDERS:
            raise ValueError(f"unknown provider {provider!r} (known: {', '.join(PROVIDERS)})")
        if provider == "tiingo" and not tiingo_token:
            raise ValueError("provider 'tiingo' needs a token (TIINGO_API_KEY)")
        self.cache_dir = cache_dir
        self.provider = provider
        self.tiingo_token = tiingo_token
        # Offline runs read the cache and never open a socket -- so the
        # report is reproducible, and so a machine with no egress (CI, a
        # locked-down container) can still run the analysis over bars
        # fetched elsewhere.
        self.offline = offline
        self._client: httpx.AsyncClient | None = None
        self.failures: dict[str, str] = {}

    async def __aenter__(self) -> "BarClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            # Stooq serves the CSV to a browser-shaped request; the default
            # httpx User-Agent gets an HTML interstitial on some edges.
            self._client = httpx.AsyncClient(
                timeout=_TIMEOUT_SEC,
                headers={"User-Agent": "smartboi-backtest/1.0 (research; contact via repo)"},
                follow_redirects=True,
            )
        return self._client

    async def _fetch_stooq(self, symbol: str) -> list[DailyBar]:
        response = await self._http().get(STOOQ_URL, params={"s": stooq_symbol(symbol), "i": "d"})
        response.raise_for_status()
        return parse_stooq_csv(response.text)

    async def _fetch_tiingo(self, symbol: str, start_date: str, end_date: str) -> list[DailyBar]:
        response = await self._http().get(
            TIINGO_URL.format(symbol=symbol.lower()),
            params={"startDate": start_date, "endDate": end_date, "token": self.tiingo_token},
        )
        if response.status_code in (401, 403):
            raise BarFetchError(f"tiingo rejected the request ({response.status_code}) -- check TIINGO_API_KEY")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise BarFetchError(f"unexpected tiingo payload: {str(payload)[:120]}")
        return parse_tiingo_json(payload)

    async def _fetch(self, symbol: str, start_date: str, end_date: str) -> list[DailyBar]:
        last_exc: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            try:
                if self.provider == "tiingo":
                    return await self._fetch_tiingo(symbol, start_date, end_date)
                return await self._fetch_stooq(symbol)
            except BarFetchError:
                # A shaped refusal (unknown symbol, bad key) does not get
                # better on a retry -- only a transport error does.
                raise
            except Exception as exc:  # noqa: BLE001 - transport/HTTP, retried below
                last_exc = exc
                if attempt < _MAX_ATTEMPTS - 1:
                    await asyncio.sleep(1.5 * (attempt + 1))
        raise BarFetchError(f"{type(last_exc).__name__}: {last_exc}")

    async def bars_for(
        self, symbol: str, start_date: str, end_date: str, cached: list[DailyBar] | None = None
    ) -> list[DailyBar]:
        """Cached bars for `symbol`, refetched when the cache stops short
        of `end_date`. Returns [] and records the reason in `failures`
        rather than raising: one unfetchable symbol (delisted, renamed,
        never carried by the provider) must not sink a whole-universe
        pass.

        `cached` lets a caller that has already read the cache hand it in.
        Stooq returns a symbol's FULL history, so these files run to
        thousands of rows -- re-reading and re-parsing every one of them a
        second time per pass is real work, over the whole universe."""
        path = cache_path(self.cache_dir, symbol)
        if cached is None:
            cached = read_cache(path)
        if not cache_is_stale(cached, end_date):
            return _slice(cached, start_date, end_date)
        if self.offline:
            if cached:
                # Usable, just short of the window -- say so once and use
                # what there is, rather than reporting no data at all.
                log.info("%s: offline, using cached bars through %s.", symbol, cached[-1].date)
                return _slice(cached, start_date, end_date)
            self.failures[symbol] = "no cached bars and --offline was set"
            return []
        try:
            fetched = await self._fetch(symbol, start_date, end_date)
        except BarFetchError as exc:
            self.failures[symbol] = str(exc)
            # Fall back to whatever was cached: a stale series still
            # answers more of the report than nothing does.
            return _slice(cached, start_date, end_date)
        if not fetched:
            self.failures[symbol] = "provider returned no bars"
            return _slice(cached, start_date, end_date)
        merged = _merge(cached, fetched)
        write_cache(path, merged)
        return _slice(merged, start_date, end_date)

    async def bars_for_all(
        self, symbols: list[str], start_date: str, end_date: str
    ) -> dict[str, list[DailyBar]]:
        """Sequential with a small gap -- see _REQUEST_GAP_SEC. Symbols
        already covered by a fresh cache cost no request and no wait."""
        out: dict[str, list[DailyBar]] = {}
        requested = False
        for symbol in sorted(set(symbols)):
            cached = read_cache(cache_path(self.cache_dir, symbol))
            needs_request = not self.offline and cache_is_stale(cached, end_date)
            # Space only the calls that actually go out, and only between
            # them -- a cache-warm pass over the universe should not sleep
            # its way through symbols it never asks about.
            if needs_request and requested:
                await asyncio.sleep(_REQUEST_GAP_SEC)
            requested = requested or needs_request
            bars = await self.bars_for(symbol, start_date, end_date, cached=cached)
            if bars:
                out[symbol] = bars
        return out


def _merge(cached: list[DailyBar], fetched: list[DailyBar]) -> list[DailyBar]:
    """Fetched bars win on a date collision -- a provider revising a bar
    (a late print, a split re-adjustment) is exactly the case where the
    newer answer is the right one."""
    by_date = {b.date: b for b in cached}
    by_date.update({b.date: b for b in fetched})
    return sorted(by_date.values(), key=lambda b: b.date)


def _slice(bars: list[DailyBar], start_date: str, end_date: str) -> list[DailyBar]:
    return [b for b in bars if start_date <= b.date <= end_date]


def window_bounds(event_dates: list[str], pre_days: int, post_days: int) -> tuple[str, str]:
    """The calendar range that covers every event's [-pre, +post] window in
    TRADING days, with the usual weekend/holiday padding (a 7/5 ratio plus
    a fixed week) so the caller never has to reason about it. Clamped at
    today: no provider has tomorrow's bar, and asking for it is how a
    request turns into an error instead of a short series."""
    if not event_dates:
        today = datetime.now(timezone.utc).date()
        return today.isoformat(), today.isoformat()
    parsed = sorted(date.fromisoformat(d[:10]) for d in event_dates)
    start = parsed[0] - timedelta(days=int(pre_days * 7 / 5) + 7)
    end = parsed[-1] + timedelta(days=int(post_days * 7 / 5) + 7)
    today = datetime.now(timezone.utc).date()
    return start.isoformat(), min(end, today).isoformat()
