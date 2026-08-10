"""Federal Register: regulatory actions that name specific companies.

WHY A CURATED LIST AND NOT A FEED. The Federal Register publishes ~200
documents per business day. Watching it broadly is a disqualifying firehose
that would drown the LLM budget in notices about grazing permits. What makes
it worth reading at all is that a handful of proceedings name THIS universe's
companies explicitly, and those are among the cleanest catalysts available:

  - BWEN  -- utility-scale wind tower AD/CVD proceedings name Broadwind as a
             member of the Wind Tower Trade Coalition.
  - HDSN  -- EPA AIM Act HFC allowance allocation notices are entity-specific
             and are the single biggest driver of Hudson's refrigerant
             economics.
  - AOSL  -- BIS Entity List actions.
  - semi_equipment / auto_supply -- BIS export controls, FMVSS.

So this is a small set of hand-written searches, each with a declared target,
and nothing else is read. Adding a search is a deliberate act.

ON PROPAGATION. A rule is not a company, so a regulatory document has no
origin symbol in the ordinary sense. Propagation therefore runs from
synthetic `signal_source_only` pseudo-members (BIS, EPA, ITC, NHTSA) carrying
hand-seeded `regulator` edges. Those edges are seeded at 0.60-0.80,
deliberately BELOW dossier.DISCLOSED_LINK_CONFIDENCE (0.85), so a sector-wide
rule can raise a thesis but never buys the corroboration discount that a
quantified customer disclosure earns. A rule affecting an industry is not
evidence that a specific company's news is corroborated.

Free, no API key, no auth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

log = logging.getLogger(__name__)

_API_URL = "https://www.federalregister.gov/api/v1/documents.json"
_TIMEOUT_SEC = 25.0

# Fields requested explicitly. The API returns a large default document
# otherwise, and every extra field is bytes over the wire for something
# nothing reads.
_FIELDS = (
    "document_number", "title", "abstract", "html_url", "publication_date",
    "type", "agencies", "action",
)

# Pseudo-symbols standing in for the issuing bodies. These are NOT companies:
# they exist only so a regulatory document has an origin to propagate from,
# and they are registered signal_source_only so they can never become trade
# targets. Kept here rather than in universe.py's curated list because they
# are an implementation detail of this module's propagation, not a view about
# any market.
REGULATOR_SYMBOLS: dict[str, str] = {
    "BIS": "Bureau of Industry and Security",
    "EPA": "Environmental Protection Agency",
    "ITC": "International Trade Commission",
    "NHTSA": "National Highway Traffic Safety Administration",
}


@dataclass(frozen=True)
class RegSearch:
    """One hand-curated saved search and what it is allowed to reach.

    `targets` are explicit symbols; `ecosystem` fans out to a whole sector.
    A search must declare one or the other -- a search that reaches nothing
    is a firehose with extra steps."""

    key: str
    term: str
    regulator: str                       # a REGULATOR_SYMBOLS key
    targets: tuple[str, ...] = ()
    ecosystem: str = ""
    agencies: tuple[str, ...] = ()       # API agency slugs, when narrowing helps
    note: str = ""


# Every search here was written against a specific, checkable claim about a
# specific company or sector. Do not add one because a topic sounds relevant.
CURATED_SEARCHES: tuple[RegSearch, ...] = (
    RegSearch(
        key="wind-towers-adcvd",
        term="utility scale wind towers",
        regulator="ITC",
        targets=("BWEN",),
        agencies=("international-trade-administration", "international-trade-commission"),
        note="AD/CVD proceedings on utility-scale wind towers name Broadwind as a "
             "Wind Tower Trade Coalition member.",
    ),
    RegSearch(
        key="hfc-allowances",
        term="hydrofluorocarbon allowance allocation",
        regulator="EPA",
        targets=("HDSN",),
        agencies=("environmental-protection-agency",),
        note="AIM Act HFC allowance notices are entity-specific and are the single "
             "biggest driver of Hudson Technologies' refrigerant economics.",
    ),
    RegSearch(
        key="entity-list",
        term="Entity List",
        regulator="BIS",
        targets=("AOSL",),
        agencies=("industry-and-security-bureau",),
        note="BIS Entity List additions/removals reach power-semi and RF names "
             "with China exposure.",
    ),
    RegSearch(
        key="semi-export-controls",
        term="semiconductor manufacturing equipment export controls",
        regulator="BIS",
        ecosystem="semi_equipment",
        agencies=("industry-and-security-bureau",),
        note="Export-control rules move the whole semi-equipment chain, which is "
             "why this one is scoped to the ecosystem rather than a ticker.",
    ),
    RegSearch(
        key="fmvss",
        term="Federal Motor Vehicle Safety Standards",
        regulator="NHTSA",
        ecosystem="auto_supply",
        agencies=("national-highway-traffic-safety-administration",),
        note="A new or amended FMVSS creates or destroys content-per-vehicle for "
             "suppliers well before it shows up in anyone's guidance.",
    ),
)


@dataclass(frozen=True)
class RegDocument:
    document_number: str
    title: str
    abstract: str
    url: str
    publication_date: str
    doc_type: str
    agencies: tuple[str, ...] = field(default=())

    @property
    def evidence_text(self) -> str:
        """What the dossier updater reads. Verbatim title + abstract: whether a
        rule helps or hurts a given company is exactly the judgement the LLM is
        for, and summarising here would throw away the words it needs."""
        parts = [f"Federal Register {self.doc_type or 'document'} "
                 f"published {self.publication_date}:", self.title]
        if self.abstract:
            parts.append(self.abstract)
        if self.agencies:
            parts.append(f"Issuing agency: {', '.join(self.agencies)}.")
        return "\n\n".join(p for p in parts if p)


def search_url(search: RegSearch, since: date, per_page: int = 20) -> str:
    params: list[tuple[str, str]] = [
        ("conditions[term]", search.term),
        ("conditions[publication_date][gte]", since.isoformat()),
        ("per_page", str(per_page)),
        ("order", "newest"),
    ]
    params += [("fields[]", f) for f in _FIELDS]
    params += [("conditions[agencies][]", a) for a in search.agencies]
    return f"{_API_URL}?{urlencode(params)}"


def parse_documents(payload: object) -> list[RegDocument]:
    """Defensive by the same rule as the EDGAR search parser: an unrecognised
    response yields ZERO documents rather than documents built from misread
    fields. A missed rule costs nothing; a garbage one is scored by an LLM and
    merged into a dossier."""
    if not isinstance(payload, dict):
        log.warning("[FEDREG] Response was %s, not an object -- no documents.",
                    type(payload).__name__)
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        # count==0 legitimately omits `results`; only warn when it looked like
        # there should have been some.
        if payload.get("count"):
            log.warning("[FEDREG] Response reported count=%s but carried no results list.",
                        payload.get("count"))
        return []

    out: list[RegDocument] = []
    for raw in results:
        if not isinstance(raw, dict):
            continue
        number = str(raw.get("document_number") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not number or not title:
            continue
        out.append(RegDocument(
            document_number=number,
            title=title,
            abstract=str(raw.get("abstract") or "").strip(),
            url=str(raw.get("html_url") or "").strip(),
            publication_date=str(raw.get("publication_date") or "").strip(),
            doc_type=str(raw.get("type") or "").strip(),
            agencies=_agency_names(raw.get("agencies")),
        ))
    return out


def _agency_names(value: object) -> tuple[str, ...]:
    """`agencies` is a list of objects with name/raw_name, but the API has been
    known to return bare strings on some document types."""
    if not isinstance(value, list):
        return ()
    names = []
    for item in value:
        if isinstance(item, dict):
            name = item.get("name") or item.get("raw_name")
        else:
            name = item
        if name:
            names.append(str(name).strip())
    return tuple(names)


class FederalRegisterClient:
    def __init__(self, client: httpx.AsyncClient | None = None,
                 user_agent: str = "SmartBoi") -> None:
        self._client = client
        self._owns_client = client is None
        self._user_agent = user_agent

    async def _get(self, url: str) -> object | None:
        client = self._client
        if client is None:
            client = httpx.AsyncClient(
                timeout=_TIMEOUT_SEC, headers={"User-Agent": self._user_agent},
            )
            self._client = client
        try:
            response = await client.get(url)
        except Exception:  # noqa: BLE001 - a missed rule is a no-op, never a crash
            log.exception("[FEDREG] Request failed: %s", url)
            return None
        if response.status_code >= 400:
            # Logged with the URL because this is the pass most likely to be
            # debugged from a deployment's logs rather than a test.
            log.warning("[FEDREG] HTTP %d for %s -- body starts: %s",
                        response.status_code, url, response.text[:200])
            return None
        try:
            return response.json()
        except ValueError:
            log.warning("[FEDREG] Non-JSON body for %s -- starts: %s", url, response.text[:200])
            return None

    async def fetch(self, search: RegSearch, lookback_days: int = 3,
                    today: date | None = None) -> list[RegDocument]:
        """Documents matching one curated search since `lookback_days` ago.

        Overlapping windows on purpose: the caller dedupes on
        document_number, and a lookback shorter than the poll gap silently
        drops documents published while the process was down."""
        today = today or datetime.now(timezone.utc).date()
        since = today - timedelta(days=max(1, lookback_days))
        payload = await self._get(search_url(search, since))
        if payload is None:
            return []
        docs = parse_documents(payload)
        log.info("[FEDREG] %s: %d document(s) since %s.", search.key, len(docs), since)
        return docs

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
