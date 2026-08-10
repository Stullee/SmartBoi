"""DoD daily contract announcements -- every award at or above the DFARS
205.303 threshold, published ~5pm ET each business day.

WHY THIS IS OFF (enable_dod_contracts defaults to False)
--------------------------------------------------------
war.gov is closed to automated clients. Measured live 2026-08-10, in this
order, so nobody has to re-derive it:

  GET /News/Contracts/                            -> 403, server: AkamaiGHost
  GET /News/.../Story/Article/4540596/...         -> 403, server: AkamaiGHost
  GET /DesktopModules/ArticleCS/RSS.ashx?...      -> 200, text/xml

So every HTML path is denied by Akamai's bot manager -- the listing AND
individual articles -- while the RSS endpoint is open. That is the site
stating a policy: consume the feed, not the pages.

The feed cannot carry this. ContentType=400 is the Contracts channel
("Contracts - U.S. Dept. of War"), and its item description is a fixed
boilerplate sentence:

    "Today's Department of War contracts valued at $7.5 million or more are
     now live on War.gov."

Identical every day. No award text, no dollar values, no company names --
nothing an alias can match and nothing a thesis can use. The awards exist
only in the article body, which is 403.

Getting the article body from war.gov directly would mean impersonating a
browser to defeat a bot manager, which is not something this system does.

The API substitutes do not work either: USASpending, FPDS and SAM's award API
all sit behind DoD's 90-day publication hold -- ~6x past the 14-day floor in
dossier.evidence_is_stale -- so evidence from them is born aged out, which is
the whole reason war.gov was chosen over them in the first place. (FPDS-NG's
ATOM feed is separately gone: FPDS.gov was decommissioned in 2026 and folded
into SAM.gov.)

STILL OPEN: reading the article bodies from the Wayback Machine. That is not
circumvention -- it is a public archive with a documented API, reading a
public record, with no bot manager in front of it -- and the freshness maths
works, since a 1-3 day archive lag is comfortably inside the 14-day staleness
floor that killed the 90-day APIs. The RSS feed already hands over the exact
article URL in <link>, so archive coverage is the only unknown. If it is
there, this becomes a fetch-layer change plus a settings flip and everything
below is reused as-is. Untested at the time of writing.

Everything below is kept and still tested. The alias table, the value floor
and the verbatim pass-through are correct and cost nothing while dormant, so
if war.gov ever opens an automated route this becomes a fetch-layer change
plus a settings flip rather than a rewrite. The known-good feed URL is
recorded in _RSS_URL for exactly that day.

WHY THIS AND NOT USASPENDING. DoD awards are withheld from
FPDS/USASpending/SAM for 90 days. That is ~6x past the 14-day floor in
`dossier.evidence_is_stale`, so evidence sourced there would be born aged
out -- it would arrive already too stale to contribute mass. The daily
announcements carry no such hold. (FPDS-NG's ATOM feed is also gone: FPDS.gov
was decommissioned in 2026 and folded into SAM.gov, so any design referencing
it is dead on arrival.)

THE HARD PART IS NAME MATCHING, NOT FETCHING. Announcements use legal entity
names, not tickers or brands:

    "Ducommun LaBarge Technologies Inc., Tulsa, Oklahoma"   -> DCO
    "Vertex Aerospace LLC"                                  -> V2X
    "Sierra Nevada Corp."                                   -> private, no ticker

and "Vertex" alone collides with Vertex Pharmaceuticals. So matching is
whole-word, case-insensitive, against a HAND-REVIEWED alias table and nothing
else. No fuzzy matching, no substring matching, no token soup: this is the
ATRO/Advantest misresolution failure mode in its worst form, and the cost of
a false match is an LLM scoring a defense award against an unrelated company's
dossier.

WHAT IS PASSED TO THE SCORER. The announcement text VERBATIM. Many "awards"
are IDIQ ceilings or modifications rather than new revenue, and the difference
lives in the wording ("not-to-exceed", "ceiling", "modification", "option
exercise"). Summarising here would destroy exactly what the skeptic needs to
catch it.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

_BASE_URL = "https://www.war.gov/News/Contracts/"
# The one war.gov endpoint that answers an automated client (200, text/xml).
# ContentType=400 is the Contracts channel; Site=945 is war.gov. Recorded
# because it was expensive to find and because it is the starting point for
# any future attempt -- not used today, since its description field carries
# only boilerplate (see "WHY THIS IS OFF" above).
_RSS_URL = "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=945&max=10"
_TIMEOUT_SEC = 25.0

# Awards below this dollar value are not scored for an ANCHOR. The big primes
# appear most business days, and without a floor their routine awards would
# dominate the propagation budget while saying nothing a thesis can use. No
# floor applies to a tradeable's own award: a $12M award to a $90M-cap company
# is material to it in a way a $12M award to Lockheed is not.
ANCHOR_VALUE_FLOOR_USD = 100_000_000.0


@dataclass(frozen=True)
class ContractAward:
    symbol: str            # the matched universe symbol
    matched_alias: str     # which alias matched, so a bad rule is traceable
    text: str              # the announcement paragraph, verbatim
    value_usd: float | None
    announced_date: str

    @property
    def evidence_text(self) -> str:
        return (f"U.S. Department of Defense contract announcement, "
                f"{self.announced_date}:\n\n{self.text}")


# Hand-reviewed. Every entry is a legal entity name as it appears in DoD
# announcements, mapped to the ticker that entity's revenue actually accrues
# to. Rules for adding one:
#
#   - Whole words only, matched case-insensitively. No substrings.
#   - An alias must be unambiguous ACROSS ALL US-LISTED COMPANIES, not just
#     across this universe. "Vertex" is banned even though Vertex
#     Pharmaceuticals is not in the universe today.
#   - A subsidiary maps to its listed parent only where the parent actually
#     books the revenue.
#   - When in doubt, leave it out. A missed award costs nothing; a wrong one
#     puts a defense contract into an unrelated company's thesis.
COMPANY_ALIASES: dict[str, tuple[str, ...]] = {
    "LMT": ("Lockheed Martin",),
    "RTX": ("Raytheon", "RTX Corp", "Pratt & Whitney", "Collins Aerospace"),
    "NOC": ("Northrop Grumman",),
    "GD": ("General Dynamics",),
    "BA": ("Boeing",),
    "LHX": ("L3Harris", "L3 Harris"),
    "HII": ("Huntington Ingalls",),
    "LDOS": ("Leidos",),
    "TXT": ("Textron", "Bell Textron"),
    "DCO": ("Ducommun",),
    "V2X": ("Vertex Aerospace", "V2X Inc", "Vectrus"),
    "AIR": ("AAR Corp",),
    "KTOS": ("Kratos Defense", "Kratos Unmanned"),
    "MOG.A": ("Moog Inc",),
    "TDG": ("TransDigm",),
    "HEI": ("HEICO",),
    "CW": ("Curtiss-Wright",),
    "ATRO": ("Astronics",),
    "SIF": ("SIFCO Industries",),
    "TAYD": ("Taylor Devices",),
    "UFPT": ("UFP Technologies",),
    "BKTI": ("BK Technologies",),
}

# Aliases that look tempting and are BANNED, with the collision that bans
# them. Kept in code rather than a comment so a future edit that adds one
# fails a test rather than a live dossier.
BANNED_ALIASES: dict[str, str] = {
    "Vertex": "Vertex Pharmaceuticals (VRTX) -- use 'Vertex Aerospace'",
    "General": "General Dynamics / General Electric / General Motors / generic English",
    "Bell": "Bell Textron vs Bell Canada vs generic English",
    "Collins": "Collins Aerospace (RTX) vs Collins Industries vs a surname",
    "Sierra": "Sierra Nevada Corp is private; Sierra Wireless is unrelated",
    "L3": "too short to be a safe whole-word token",
    "AAR": "collides with common acronyms -- use 'AAR Corp'",
    "Moog": "Moog Inc (MOG.A) vs Moog Music -- use 'Moog Inc'",
    "Kratos": "Kratos Defense vs unrelated 'Kratos' brands -- use the full name",
}

# Award value, e.g. "$1,234,567,890" or "$12,345,678". DoD writes the value
# early in the paragraph and always with a dollar sign and grouping commas.
_VALUE_RE = re.compile(r"\$\s?([0-9][0-9,]{5,})(?:\.\d+)?")


def parse_value_usd(text: str) -> float | None:
    """The FIRST dollar figure in an announcement, which is the award value.

    Deliberately first-only. Announcements frequently contain several figures
    (the award, then obligated funds, then a ceiling), and taking the maximum
    would systematically read an IDIQ ceiling as new revenue -- the exact
    misreading the value floor exists to avoid."""
    match = _VALUE_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _alias_pattern(alias: str) -> re.Pattern:
    """Whole-word, case-insensitive, punctuation-tolerant.

    `\\b` on both ends is what makes this safe: without it "AIR" matches
    "AIRCRAFT" and every announcement in the corpus mentions aircraft."""
    return re.compile(rf"\b{re.escape(alias)}\b", re.IGNORECASE)


_COMPILED: dict[str, tuple[tuple[str, re.Pattern], ...]] = {
    symbol: tuple((alias, _alias_pattern(alias)) for alias in aliases)
    for symbol, aliases in COMPANY_ALIASES.items()
}


def match_symbols(text: str, universe: set[str]) -> list[tuple[str, str]]:
    """(symbol, matched_alias) for every in-universe company named in this
    announcement. Whole-word only; never fuzzy."""
    if not text:
        return []
    out: list[tuple[str, str]] = []
    for symbol, patterns in _COMPILED.items():
        if symbol not in universe:
            continue
        for alias, pattern in patterns:
            if pattern.search(text):
                out.append((symbol, alias))
                break   # one match per company is enough
    return out


def split_announcements(text: str) -> list[str]:
    """The page into individual award paragraphs.

    DoD publishes one paragraph per award, separated by blank lines and
    grouped under service headings (ARMY, NAVY, ...). Paragraphs shorter than
    a plausible award are dropped -- those are the headings themselves and the
    boilerplate footer."""
    chunks = re.split(r"\n\s*\n", text or "")
    return [c.strip() for c in chunks if len(c.strip()) >= 120]


def awards_from_page(page_text: str, universe: set[str], announced_date: str,
                     anchors: set[str] = frozenset(),
                     value_floor: float = ANCHOR_VALUE_FLOOR_USD) -> list[ContractAward]:
    """Every announcement paragraph naming an in-universe company.

    The value floor applies to ANCHORS ONLY. LMT/RTX/NOC/GD/BA appear most
    business days and their routine awards would dominate the propagation
    budget; a $12M award to a $90M-cap tradeable is material to that company
    in a way the same award to Lockheed is not."""
    awards: list[ContractAward] = []
    for paragraph in split_announcements(page_text):
        value = parse_value_usd(paragraph)
        for symbol, alias in match_symbols(paragraph, universe):
            if symbol in anchors and (value is None or value < value_floor):
                continue
            awards.append(ContractAward(
                symbol=symbol, matched_alias=alias, text=paragraph,
                value_usd=value, announced_date=announced_date,
            ))
    return awards


class DodContractsClient:
    """Fetches one business day's announcements page.

    Returns "" on anything unexpected. The page is HTML whose structure is not
    a contract of any kind, so every failure mode -- a redesign, a 404, a
    maintenance page -- must degrade to "no awards today" rather than to
    garbage paragraphs fed to an LLM."""

    def __init__(self, client: httpx.AsyncClient | None = None,
                 user_agent: str = "SmartBoi") -> None:
        self._client = client
        self._owns_client = client is None
        self._user_agent = user_agent

    async def fetch_day(self, day: date) -> str:
        """The announcements page text for one day, or "" if there isn't one.

        Weekends and federal holidays simply have no page; that is the normal
        case, not an error."""
        if day.weekday() >= 5:
            return ""
        client = self._client
        if client is None:
            client = httpx.AsyncClient(
                timeout=_TIMEOUT_SEC, follow_redirects=True,
                headers={"User-Agent": self._user_agent},
            )
            self._client = client
        url = f"{_BASE_URL}Contract/Article/{day.isoformat()}/"
        try:
            response = await client.get(url)
        except Exception:  # noqa: BLE001 - a missed day is a no-op, never a crash
            log.exception("[DOD] Request failed: %s", url)
            return ""
        if response.status_code == 404:
            return ""
        if response.status_code >= 400:
            log.warning("[DOD] HTTP %d for %s -- body starts: %s",
                        response.status_code, url, response.text[:200])
            return ""
        return html_to_text(response.text)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None


def html_to_text(html: str) -> str:
    """Announcement text out of the page.

    BeautifulSoup is already a dependency (edgar.py uses it). Scripts and
    styles are dropped before extraction so a page's inline JS cannot end up
    inside an evidence paragraph."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a hard dependency
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # noqa: BLE001 - a malformed page is "no awards", not a crash
        log.exception("[DOD] Could not parse the announcements page.")
        return ""
    for tag in soup(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    return soup.get_text("\n")


def business_days_back(days: int, today: date | None = None) -> list[date]:
    """The last `days` business days, newest first. Weekends are skipped;
    holidays simply return no page."""
    today = today or datetime.now(timezone.utc).date()
    out: list[date] = []
    cursor = today
    while len(out) < days:
        if cursor.weekday() < 5:
            out.append(cursor)
        cursor -= timedelta(days=1)
    return out
