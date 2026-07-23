"""Scripted fake clients for engine-level tests (test_engine.py) -- plain
classes returning canned responses, no mocking framework. Each fake records
what it was called with in `.calls`, so tests can assert call COUNTS (e.g.
"the retry does not re-call propose_update"), not just return values."""
from __future__ import annotations


class FakeEdgarClient:
    def __init__(self):
        self.filings_by_symbol: dict[str, list] = {}
        self.text_by_accession: dict[str, str] = {}
        self.latest_filings: dict[tuple[str, str], object] = {}
        self.ticker_by_name: dict[str, str] = {}

    async def cik_for(self, symbol):
        return "0000000001"

    async def recent_filings(self, symbol, forms, since_date):
        return self.filings_by_symbol.get(symbol, [])

    async def latest_filing(self, symbol, form):
        return self.latest_filings.get((symbol, form))

    async def fetch_evidence_text(self, filing):
        return self.text_by_accession.get(filing.accession_number, "")

    async def fetch_text(self, filing, max_chars=150_000):
        return self.text_by_accession.get(filing.accession_number, "")

    async def find_ticker_by_name(self, name):
        return self.ticker_by_name.get(name)

    async def aclose(self):
        pass


class FakeFinnhub:
    def __init__(self):
        self.articles_by_symbol: dict[str, list] = {}
        self.ticker_by_name: dict[str, str] = {}
        self.market_cap_by_symbol: dict[str, float] = {}
        self.analyst_count_by_symbol: dict[str, int] = {}

    async def recent_news(self, symbol, from_date, to_date):
        return self.articles_by_symbol.get(symbol, [])

    async def market_cap_musd(self, symbol):
        return self.market_cap_by_symbol.get(symbol)

    async def analyst_count(self, symbol):
        return self.analyst_count_by_symbol.get(symbol)

    async def search_ticker_by_name(self, company_name):
        return self.ticker_by_name.get(company_name)

    async def aclose(self):
        pass


class _ScriptedCallable:
    """Shared machinery for FakeUpdater/FakeSkeptic/FakeExtractor: queued
    per-call responses (`.queue(...)`), falling back to `.default` once the
    queue is empty, with every call's args recorded to `.calls`."""

    def __init__(self, default=None):
        self.default = default
        self._queue: list = []
        self.calls: list[dict] = []

    def queue(self, response) -> None:
        self._queue.append(response)

    def _next(self):
        if self._queue:
            return self._queue.pop(0)
        return self.default


class FakeUpdater(_ScriptedCallable):
    async def propose_update(self, dossier, evidence_text, origin_symbol, relationship_note, relationship_confidence=None):
        self.calls.append({
            "symbol": dossier.symbol, "evidence_text": evidence_text,
            "origin_symbol": origin_symbol, "relationship_note": relationship_note,
            "relationship_confidence": relationship_confidence,
        })
        return self._next()

    async def aclose(self):
        pass


class FakeSkeptic(_ScriptedCallable):
    async def review(self, evidence_text, proposed, relationship_note="", relationship_confidence=None):
        self.calls.append({
            "evidence_text": evidence_text, "proposed": proposed,
            "relationship_note": relationship_note, "relationship_confidence": relationship_confidence,
        })
        return self._next()

    async def aclose(self):
        pass


class FakeExtractor(_ScriptedCallable):
    def __init__(self, default=None):
        super().__init__(default=default if default is not None else [])

    async def extract(self, filing_symbol, filing_form, filing_text, known_tickers):
        self.calls.append({
            "filing_symbol": filing_symbol, "filing_form": filing_form, "known_tickers": known_tickers,
        })
        return self._next()

    async def aclose(self):
        pass


class FakePriceFeed:
    def __init__(self, prices: dict[str, float] | None = None, connected: bool = True):
        self.prices = dict(prices or {})
        self._connected = connected

    async def ensure_connected(self):
        return self._connected

    async def connect(self):
        self._connected = True

    async def last_price(self, symbol):
        return self.prices.get(symbol)

    async def last_prices(self, symbols):
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def disconnect(self):
        self._connected = False


def proposal(is_new_information=True, direction="LONG", magnitude=0.6, confidence=0.7,
             horizon_days=20, reasoning="because reasons"):
    """A well-formed DossierUpdater.propose_update() response."""
    return {
        "is_new_information": is_new_information, "direction": direction,
        "magnitude": magnitude, "confidence": confidence,
        "horizon_days": horizon_days, "reasoning": reasoning,
    }


def verdict(refuted=False, reasoning="looks solid", adjusted_confidence=0.7, adjusted_magnitude=0.6):
    """A well-formed Skeptic.review() response."""
    return {
        "refuted": refuted, "reasoning": reasoning,
        "adjusted_confidence": adjusted_confidence, "adjusted_magnitude": adjusted_magnitude,
    }
