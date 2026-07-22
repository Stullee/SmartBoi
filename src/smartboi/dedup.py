"""Source deduplication: collapses syndicated republishes of the same
underlying story into one data point, so a wire-service piece mirrored on
forty sites counts as one source, not forty -- see README point 3
("accumulate evidence, don't react to stories... dedupe syndication").
Only genuinely distinct source domains count toward the two-independent-
source rule dossier signals require (see signals.py)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

_WHITESPACE_RE = re.compile(r"\s+")
_PUNCTUATION_RE = re.compile(r"[^\w\s]")


def normalize_headline(headline: str) -> str:
    text = headline.lower().strip()
    text = _PUNCTUATION_RE.sub("", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()


def source_domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def fingerprint(symbol: str, headline: str, published_date: str) -> str:
    """A coarse per-story key: same company, same normalized headline, same
    calendar day -- deliberately coarse so wire-service syndication (the
    same story, byte-identical or near-identical, on dozens of outlets the
    same day) collapses to one fingerprint regardless of which outlet's
    exact URL/headline formatting a given feed returned. `published_date`
    may be an ISO datetime or a bare date; only the date portion is used."""
    return f"{symbol}:{normalize_headline(headline)}:{published_date[:10]}"


@dataclass
class DedupIndex:
    """Persisted so dedup state survives restarts -- an evidence item seen
    yesterday must still be recognized as a duplicate today, not reprocessed
    (and re-billed to the LLM) just because the process restarted.

    Entries older than `max_age_days` are pruned on load so the index can't
    grow without bound; the cutoff just has to comfortably exceed every
    ingestion lookback window (14 days for EDGAR, 3 for news), since a
    fingerprint only matters while its story can still reappear in a poll."""

    path: Path
    max_age_days: int = 90
    _seen: dict[str, list] = field(default_factory=dict)  # fingerprint -> [source_domain, registered_at_iso]

    def __post_init__(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError):
            return
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(days=self.max_age_days)).isoformat()
        for fp, value in raw.items():
            # Legacy format stored a bare domain string with no timestamp;
            # adopt it as seen-now so it ages out normally from here.
            domain, registered_at = (value, now.isoformat()) if isinstance(value, str) else value
            if registered_at >= cutoff:
                self._seen[fp] = [domain, registered_at]

    def is_duplicate(self, fp: str) -> bool:
        return fp in self._seen

    def domain_for(self, fp: str) -> str | None:
        entry = self._seen.get(fp)
        return entry[0] if entry else None

    def register(self, fp: str, domain: str, registered_at: str | None = None) -> None:
        if fp in self._seen:
            return
        self._seen[fp] = [domain, registered_at or datetime.now(timezone.utc).isoformat()]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._seen))
        tmp.replace(self.path)
