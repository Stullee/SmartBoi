"""Source deduplication: collapses syndicated republishes of the same
underlying story into one data point, so a wire-service piece mirrored on
forty sites counts as one source, not forty -- see README point 3
("accumulate evidence, don't react to stories... dedupe syndication").
Only genuinely distinct source domains count toward the two-independent-
source rule dossier signals require (see signals.py)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from smartboi.persist import atomic_write_json, read_json

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


# Words that carry no story identity: corporate suffixes and glue words.
# Stripped before the token-overlap comparison so "Acme Corp wins Navy
# contract" and "Acme wins the Navy contract" compare as the same token
# set. Deliberately short -- overstripping (e.g. removing verbs) would
# collapse genuinely different stories ("wins contract" vs "loses
# contract" must stay distinct).
_HEADLINE_STOPWORDS = frozenset(
    "a an the in on of for to and with its as at by from inc corp corporation "
    "incorporated ltd plc co company holdings group".split()
)


def headline_tokens(headline: str) -> frozenset[str]:
    """The identity-bearing token set of a headline (normalized, stopwords
    and corporate suffixes removed) -- the unit near_duplicate compares."""
    return frozenset(
        t for t in normalize_headline(headline).split() if t not in _HEADLINE_STOPWORDS
    )


# Minimum Jaccard overlap between two headlines' token sets to call them
# the same underlying story. Calibrated conservative: at 0.7, "Acme Corp
# wins $50M Navy contract" matches "Acme wins $50M Navy contract" (a
# syndicated rewording) while "Acme wins Navy contract" vs "Acme loses
# Navy contract" (opposite stories, 3/5 shared tokens = 0.6) stays
# distinct. This is a best-effort second line of defense -- heavily
# reworded wire copy can still slip past it, which is why news-only
# dossiers additionally face a higher independent-source bar (see
# signals.evaluate's min_independent_sources_news_only).
_NEAR_DUP_JACCARD = 0.7


def near_duplicate(headline_a: str, headline_b: str, threshold: float = _NEAR_DUP_JACCARD) -> bool:
    """Whether two headlines look like rewordings of the same story --
    token-set Jaccard overlap at or above `threshold`. Exact duplicates are
    already collapsed by fingerprint(); this catches the syndication case
    the exact match misses: one wire story republished with lightly edited
    headlines, which would otherwise count as two 'independent' sources
    toward the corroboration gate that fires trades."""
    a, b = headline_tokens(headline_a), headline_tokens(headline_b)
    if not a or not b:
        return False
    union = len(a | b)
    if union == 0:
        return False
    return len(a & b) / union >= threshold


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
        raw = read_json(self.path, expect=dict)
        if raw is None:
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

    def find_near_duplicate(self, symbol: str, headline: str, published_date: str) -> str | None:
        """The fingerprint of an already-registered story this headline is
        a likely rewording of (same symbol, same or previous calendar day,
        token overlap >= the near-dup threshold) -- or None. Checked after
        the exact fingerprint miss: syndicated wire copy is routinely
        republished with a lightly edited headline (and sometimes after
        UTC midnight), which produced a distinct fingerprint, a second
        LLM-scored evidence item, and a second 'independent' source for
        what is one underlying story. Fingerprint keys are parseable
        because normalize_headline strips punctuation -- the embedded
        headline can never itself contain a colon."""
        candidate_tokens = headline_tokens(headline)
        if not candidate_tokens:
            return None
        dates = {published_date[:10]}
        try:
            d = datetime.fromisoformat(published_date[:10]).date()
            dates.add((d - timedelta(days=1)).isoformat())
        except ValueError:
            pass
        prefix = f"{symbol}:"
        for fp in self._seen:
            if not fp.startswith(prefix):
                continue
            rest, _, seen_date = fp.rpartition(":")
            if seen_date not in dates:
                continue
            seen_headline = rest[len(prefix):]
            if near_duplicate(headline, seen_headline):
                return fp
        return None

    def domain_for(self, fp: str) -> str | None:
        entry = self._seen.get(fp)
        return entry[0] if entry else None

    def register(self, fp: str, domain: str, registered_at: str | None = None) -> None:
        if fp in self._seen:
            return
        self._seen[fp] = [domain, registered_at or datetime.now(timezone.utc).isoformat()]
        atomic_write_json(self.path, self._seen)
