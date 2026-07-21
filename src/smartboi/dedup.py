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
    (and re-billed to the LLM) just because the process restarted."""

    path: Path
    _seen: dict[str, str] = field(default_factory=dict)  # fingerprint -> first-seen source_domain

    def __post_init__(self) -> None:
        if self.path.exists():
            try:
                self._seen = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                self._seen = {}

    def is_duplicate(self, fp: str) -> bool:
        return fp in self._seen

    def register(self, fp: str, domain: str) -> None:
        if fp in self._seen:
            return
        self._seen[fp] = domain
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._seen))
        tmp.replace(self.path)
