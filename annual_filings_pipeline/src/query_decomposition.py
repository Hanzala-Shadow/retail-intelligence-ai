"""Deterministic query decomposition and evidence aggregation (contract 1.3.0).

This module never selects candidates itself.  It wraps the locked
``query_api.ProductionRetriever`` through a strict adapter.
"""
from __future__ import annotations

import hashlib
import itertools
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Iterable, Protocol

CONTRACT_VERSION = "1.6.0"
SECTION_POLICY_VERSION = "1.6.0"


class QueryType(str, Enum):
    SINGLE_SOURCE = "single_source"
    TEMPORAL_COMPARISON = "temporal_comparison"
    CROSS_COMPANY_COMPARISON = "cross_company_comparison"
    CROSS_SECTION_SYNTHESIS = "cross_section_synthesis"
    MULTI_AXIS_COMPARISON = "multi_axis_comparison"
    UNSUPPORTED_OR_AMBIGUOUS = "unsupported_or_ambiguous"


class ContractError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class FilingRecord:
    ticker: str
    filing_year: int
    doc_type: str
    accession_number: str


@dataclass(frozen=True)
class SubQuery:
    subquery_id: str
    question: str
    claim_key: str
    comparison_side_id: str
    ticker: str
    filing_year: int
    doc_type: str
    accession_number: str
    section_code: str


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: int
    semantic_rank: int
    cross_encoder_rank: int
    cross_encoder_score: float
    ticker: str
    filing_year: int
    doc_type: str
    accession_number: str
    section_code: str
    chunk_text: str
    page_start: int | None = None
    page_end: int | None = None
    page_unavailable_reason: str | None = "html_source"


@dataclass
class EvidenceItem:
    chunk_id: int
    semantic_rank: int
    cross_encoder_rank: int
    cross_encoder_score: float
    aggregated_rank: int
    subquery_id: str
    claim_key: str
    comparison_side_id: str
    ticker: str
    filing_year: int
    doc_type: str
    accession_number: str
    section_code: str
    chunk_text: str
    page_start: int | None
    page_end: int | None
    page_unavailable_reason: str | None
    provenance: list[dict[str, Any]] = field(default_factory=list)


CLAIM_SECTION_TABLE = {
    # Item 1: business description and operating footprint.
    "customers and membership": "Item_1", "digital commerce": "Item_1",
    "distribution network": "Item_1", "store network": "Item_1",
    "sourcing and suppliers": "Item_1", "merchandising and products": "Item_1",
    "customer service": "Item_1", "operating footprint": "Item_1",
    "business expansion and strategic progress": "Item_1",
    # Item 1A: risk-factor families.
    "technology and artificial intelligence risk": "Item_1A",
    "cybersecurity and data privacy risk": "Item_1A",
    "supply chain disruption risk": "Item_1A",
    "regulatory and legal risk": "Item_1A",
    "fraud and payment risk": "Item_1A", "macroeconomic risk": "Item_1A",
    "competition risk": "Item_1A",
    "debt constraints and consequences": "Item_1A",
    # Item 7: performance, drivers, liquidity and operating analysis.
    "revenue and sales performance": "Item_7", "gross margin performance": "Item_7",
    "inventory performance": "Item_7", "liquidity and cash flow": "Item_7",
    "operating expenses": "Item_7", "segment performance": "Item_7",
    "revenue composition": "Item_7",
    # Item 8: financial-statement policies, notes and contingencies.
    "commitments and contingencies": "Item_8", "revenue recognition policy": "Item_8",
    "goodwill and impairment": "Item_8", "income tax accounting": "Item_8",
    "inventory accounting": "Item_8", "lease accounting": "Item_8",
    "debt and borrowings": "Item_8",
    "financial statements": "Item_8", "supply chain risk": "Item_1A",
    "gross margin": "Item_7", "operating margin": "Item_7",
    "net income": "Item_8", "risk factors": "Item_1A",
    "liquidity": "Item_7", "revenue": "Item_8", "margin": "Item_7",
    "trend": "Item_7", "risk": "Item_1A", "business": "Item_1",
    "operations": "Item_1", "segment": "Item_1",
    "competition": "Item_1", "accounting": "Item_8",
}
CLAIM_PROMPTS = {
    "gross margin": "gross margin performance and cost-of-sales drivers",
    "operating margin": "operating margin and operating-expense drivers",
    "revenue": "revenue performance and sales drivers",
    "liquidity": "liquidity, capital resources, and cash flows",
    "supply chain risk": "supply-chain disruptions and related risks",
    "business expansion and strategic progress": (
        "business expansion, service launches, and strategic progress"
    ),
    "operating expenses": "operating expenses, SG&A changes, and cost drivers",
    "lease accounting": "lease liabilities, payments, and maturity schedules",
    "debt and borrowings": (
        "debt, commercial paper, borrowings, and credit facilities"
    ),
    "debt constraints and consequences": (
        "how indebtedness could constrain operations, financing, strategy, "
        "and the ability to meet obligations"
    ),
    "revenue composition": (
        "revenue mix, category contributions, and percentages of total revenue"
    ),
    "customer service": (
        "customer-service capabilities, delivery, installation, support, "
        "and related operating features"
    ),
    "operating footprint": (
        "stores, stations, facilities, locations, and operating footprint"
    ),
    "revenue recognition policy": (
        "revenue recognition, loyalty points, rewards, deferred revenue, "
        "and the accounting treatment of customer incentives"
    ),
    "goodwill and impairment": (
        "impairment charges, affected assets, measurement, and components"
    ),
}
GENERIC_SECTION_CLAIMS = {
    "generic business disclosure": "Item_1",
    "generic risk disclosure": "Item_1A",
    "generic performance analysis": "Item_7",
    "generic financial-note disclosure": "Item_8",
}
CLAIM_SECTION_TABLE.update(GENERIC_SECTION_CLAIMS)
CLAIM_PROMPTS.update({
    "generic business disclosure": (
        "business operations, products, customers, suppliers, channels, "
        "workforce, footprint, and strategy"
    ),
    "generic risk disclosure": (
        "material risks, exposures, uncertainties, consequences, and mitigation"
    ),
    "generic performance analysis": (
        "results of operations, changes, trends, performance, and their drivers"
    ),
    "generic financial-note disclosure": (
        "financial-statement amounts, accounting policies, estimates, "
        "agreements, and note disclosures"
    ),
})

# Atomic disclosure requirements used to refine a broad section-level fallback.
# These rules describe common 10-K disclosure concepts; they do not contain
# benchmark IDs, issuers, years, or expected answers.  Refinement happens only
# inside the section already selected by the normal section ontology.
ATOMIC_SECTION_REQUIREMENTS = {
    "Item_1": (
        ("products and services", "products, services, brands, and merchandise",
         r"\b(?:products?|brands?|merchandise|portfolio)\b|(?<!customer )\bservices?\b"),
        ("customers and demand", "customers, members, demand, and customer concentration",
         r"\bcustomer(?! service)\b|\bcustomers\b|\b(?:members?|consumer demand|customer concentration)\b"),
        ("customer service", "customer-service capabilities, delivery, installation, support, and related operating features",
         r"\bcustomer service\b|\bcustomer support\b"),
        ("suppliers and sourcing", "suppliers, vendors, sourcing, and supply arrangements",
         r"\b(?:suppliers?|vendors?|sourcing|procurement)\b"),
        ("sales channels", "sales channels, digital commerce, wholesale, and distribution",
         r"\b(?:channels?|e-?commerce|digital commerce|wholesale|distribution)\b"),
        ("workforce", "workforce, employees, labor, recruitment, and retention",
         r"\b(?:workforce|employees?|labor|recruit(?:ment|ing)?|retention)\b"),
        ("operating footprint", "stores, locations, facilities, and operating footprint",
         r"\b(?:stores?|locations?|facilities|footprint)\b"),
        ("business strategy", "business strategy, acquisitions, expansion, and strategic priorities",
         r"\b(?:strateg(?:y|ic)|acquisitions?|expansion|growth initiatives?|priorities)\b"),
        ("competition", "competition, competitive position, and differentiators",
         r"\b(?:competition|competitive|competitors?|differentiators?)\b"),
    ),
    "Item_1A": (
        ("risk exposure", "the nature, source, and scope of the risk exposure",
         r"\b(?:risk|exposure|uncertaint(?:y|ies)|dependence|reliance)\b"),
        ("risk consequences", "potential operational, financial, legal, and reputational consequences",
         r"\b(?:consequences?|impacts?|effects?|harm|adverse|loss(?:es)?)\b"),
        ("risk mitigation", "mitigating actions, controls, safeguards, and contingency measures",
         r"\b(?:mitigat(?:e|ion|ing)|controls?|safeguards?|contingenc(?:y|ies)|responses?)\b"),
        ("risk trend and likelihood", "risk likelihood, timing, severity, and changes over time",
         r"\b(?:likelihood|probability|severity|timing|trend|increas(?:e|ing)|decreas(?:e|ing))\b"),
    ),
    "Item_7": (
        ("revenue performance", "revenue, sales, volume, and comparable-sales performance",
         r"\b(?:revenue|sales|volume|comparable[- ]store|same[- ]store)\b"),
        ("margin performance", "gross margin, operating margin, and profitability",
         r"\b(?:gross (?:profit|margin)|operating margin|profitability)\b"),
        ("expense performance", "operating expenses, SG&A, and cost changes",
         r"\b(?:expenses?|sg\s*&?\s*a|costs?|overhead)\b"),
        ("performance drivers", "causes, drivers, offsets, and management's explanation of changes",
         r"\b(?:drivers?|causes?|factors?|drove|due to|offset(?:s|ting)?)\b"),
        ("inventory performance", "inventory levels, turns, markdowns, and inventory changes",
         r"\b(?:inventory|inventories|markdowns?|turns)\b"),
        ("liquidity and cash flow", "liquidity, cash flows, capital resources, and funding needs",
         r"\b(?:liquidity|cash flows?|capital resources?|funding)\b"),
    ),
    "Item_8": (
        ("reported amounts", "reported balances, expense amounts, and financial-statement components",
         r"\b(?:amounts?|balances?|expense|components?|how much|what share)\b"),
        ("accounting recognition", "recognition, classification, presentation, and accounting policy",
         r"\b(?:recognition|recognized|classification|presentation|accounting polic(?:y|ies))\b"),
        ("measurement and estimates", "measurement methods, assumptions, estimates, and sensitivities",
         r"\b(?:measurement|measur(?:e|ed|es|ing)|assumptions?|estimates?|sensitivity|fair value)\b"),
        ("contractual terms", "agreement terms, maturities, rates, commitments, and obligations",
         r"\b(?:agreements?|terms?|maturit(?:y|ies)|rates?|commitments?|obligations?)\b"),
        ("changes and rollforwards", "period changes, additions, reductions, and rollforwards",
         r"\b(?:changes?|additions?|reductions?|rollforwards?|beginning|ending)\b"),
    ),
}

ATOMIC_PROMPTS = {
    key: prompt
    for requirements in ATOMIC_SECTION_REQUIREMENTS.values()
    for key, prompt, _ in requirements
}
CLAIM_PROMPTS.update(ATOMIC_PROMPTS)
VALID_SECTIONS = {f"Item_{n}" for n in range(1, 17)} | {
    "Item_1A", "Item_1B", "Item_1C", "Item_7A", "Item_9A", "Item_9B"
}
YEAR_RE = re.compile(
    r"(?<!\w)(?:fy\s*)?((?:19|20)\d{2})\b",
    re.I,
)
SECTION_RE = re.compile(
    r"\bitem\s*(\d{1,2}[a-c]?)\b",
    re.I,
)
FILING_YEAR_BEFORE_10K_RE = re.compile(
    r"(?<!\w)(?:fy\s*)?((?:19|20)\d{2})"
    r"(?:\s*(?:,|and|or|through|to|-)\s*"
    r"(?:fy\s*)?((?:19|20)\d{2}))?"
    r"\s+(?:fiscal[- ]year\s+)?"
    r"10-k(?:s|\s+filings?)?\b",
    re.I,
)
FILING_YEAR_AFTER_10K_RE = re.compile(
    r"\b10-k(?:s|\s+filings?)?\s+(?:for|from|filed\s+in)\s+((?:19|20)\d{2})\b",
    re.I,
)
CLAIM_PATTERNS = (
    (
        "goodwill and impairment",
        re.compile(
            r"\b(?:impairment charges?|asset impairments?|goodwill impairment|"
            r"tradename impairment|long[- ]lived asset impairment)\b",
            re.I,
        ),
    ),
    (
        "revenue recognition policy",
        re.compile(
            r"\b(?:revenue recognition|account(?:ed|ing)?\s+for\s+(?:revenue|"
            r"sales|points|rewards)|deferred revenue|loyalty points?|"
            r"reward certificates?|customer rewards?)\b|"
            r"\brevenue\b.{0,100}\baccount(?:ed|ing)?\s+for\b|"
            r"\baccount(?:ed|ing)?\s+for\b.{0,100}\b"
            r"(?:points?|rewards?|notes?|certificates?)\b",
            re.I,
        ),
    ),
    (
        "revenue composition",
        re.compile(
            r"\b(?:percentage|percent|share|portion|mix)\s+of\s+"
            r"(?:total\s+)?(?:revenue|sales)\b|\b(?:revenue|sales)\s+mix\b",
            re.I,
        ),
    ),
    (
        "customer service",
        re.compile(
            r"\b(?:customer service|customer support|installation services?|"
            r"delivery services?)\b",
            re.I,
        ),
    ),
    (
        "distribution network",
        re.compile(
            r"\b(?:distribution|distribution network|distribution centers?|"
            r"delivery network|logistics network)\b",
            re.I,
        ),
    ),
    (
        "operating footprint",
        re.compile(
            r"\b(?:how many|number of)\s+(?:stores?|stations?|locations?|"
            r"facilities|sites?)\b|\b(?:charging stations?|service locations?)\b",
            re.I,
        ),
    ),
    (
        "debt constraints and consequences",
        re.compile(
            r"\b(?:debt|indebtedness|borrowings?)\b.*\b"
            r"(?:constrain|restrict|limit|impair|prevent|adversely affect)\b"
            r"|\b(?:constrain|restrict|limit|impair|prevent)\b.*\b"
            r"(?:business|operations?|financing|strategy|obligations?)\b",
            re.I,
        ),
    ),
    (
        "risk factors",
        re.compile(
            r"\b(?:could|may|might)\s+(?:happen|occur|result)\s+if\b|"
            r"\bif\s+(?:the\s+company\s+)?(?:cannot|could not|is unable to|"
            r"fails? to|does not)\b|"
            r"\b(?:cannot|unable to|failure to|fails? to)\b.*\b"
            r"(?:protect|retain|maintain|strengthen|compete|comply|operate)\b",
            re.I,
        ),
    ),
    (
        "business expansion and strategic progress",
        re.compile(
            r"\b(?:progress|launch(?:ed|es|ing)?|open(?:ed|ing)?|"
            r"expan(?:d|ded|ding|sion)|roll(?:ed|ing)?\s+out)\b.*\b"
            r"(?:business|service|clinic|market|product|store|platform|care)\b"
            r"|\b(?:business|service|clinic|market|product|store|platform|care)\b.*\b"
            r"(?:progress|launch(?:ed|es|ing)?|open(?:ed|ing)?|"
            r"expan(?:d|ded|ding|sion)|roll(?:ed|ing)?\s+out)\b",
            re.I,
        ),
    ),
    (
        "operating expenses",
        re.compile(
            r"\b(?:sg\s*&?\s*a|sga|selling general and administrative|"
            r"operating expenses?|deleverage|overhead|wage investments?)\b",
            re.I,
        ),
    ),
    (
        "lease accounting",
        re.compile(
            r"\b(?:operating|finance)?\s*leases?\b|\blease\s+"
            r"(?:liabilit(?:y|ies)|payments?|maturit(?:y|ies)|obligations?)\b",
            re.I,
        ),
    ),
    (
        "debt and borrowings",
        re.compile(
            r"\b(?:commercial paper|short[- ]term borrowings?|long[- ]term debt|"
            r"credit facilit(?:y|ies)|debt maturit(?:y|ies)|senior notes?|"
            r"principal indebtedness|consolidated indebtedness|"
            r"outstanding indebtedness|debt balance|borrowings?)\b",
            re.I,
        ),
    ),
    (
        "risk factors",
        re.compile(r"\b(?:risks|risk exposures?)\b", re.I),
    ),
)

# Generic fallback ontology based on the standard purpose of the four supported
# 10-K sections. These signals are deliberately company- and benchmark-agnostic.
# They are consulted only when the existing precise claim detector finds
# nothing.
GENERIC_SECTION_SIGNALS = {
    "generic business disclosure": (
        (3, re.compile(r"\b(?:business model|operating model|value proposition)\b", re.I)),
        (2, re.compile(r"\b(?:products?|brands?|merchandise|portfolio|services?)\b", re.I)),
        (2, re.compile(r"\b(?:customers?|members?|suppliers?|vendors?)\b", re.I)),
        (2, re.compile(r"\b(?:stores?|locations?|footprint|distribution|channels?)\b", re.I)),
        (2, re.compile(r"\b(?:workforce|employees?|development|retention)\b", re.I)),
        (2, re.compile(r"\b(?:strategy|acquisition|marketplace|pricing)\b", re.I)),
        (2, re.compile(r"\b(?:competition|competitive|competitors?)\b", re.I)),
    ),
    "generic risk disclosure": (
        (4, re.compile(r"\b(?:risk factors?|material risks?|adverse(?:ly)?)\b", re.I)),
        (3, re.compile(r"\b(?:could|may)\s+(?:harm|affect|impact|result|lead)\b", re.I)),
        (3, re.compile(r"\b(?:unable|cannot|failure|fail(?:ed|ure)?|exposure)\b", re.I)),
        (2, re.compile(r"\b(?:uncertain(?:ty|ties)?|regulatory|cybersecurity|tariffs?)\b", re.I)),
        (2, re.compile(r"\b(?:dependence|reliance|seasonality|competition)\b", re.I)),
        (2, re.compile(r"\b(?:competitive pressure|larger competitors?)\b", re.I)),
    ),
    "generic performance analysis": (
        (4, re.compile(r"\b(?:results of operations|comparable store sales|same store sales)\b", re.I)),
        (3, re.compile(r"\b(?:revenue|sales|gross profit|gross margin|operating margin|net income|ebit|ebitda)\b", re.I)),
        (2, re.compile(r"\b(?:increase[ds]?|decrease[ds]?|decline[ds]?|grew|growth|change[ds]?)\b", re.I)),
        (2, re.compile(r"\b(?:drivers?|drove|factors?|performance|trend)\b", re.I)),
        (1, re.compile(r"\b(?:fiscal quarter|fiscal year)\b", re.I)),
    ),
    "generic financial-note disclosure": (
        (5, re.compile(r"\b(?:accounting polic(?:y|ies)|independent auditor|audit work)\b", re.I)),
        (4, re.compile(r"\b(?:share-based compensation|stock-based compensation|impairment charge)\b", re.I)),
        (4, re.compile(r"\b(?:credit agreement|revolving credit|nonvested shares?)\b", re.I)),
        (3, re.compile(r"\b(?:recognized|unrecognized|measurement|estimate[sd]?)\b", re.I)),
        (3, re.compile(r"\b(?:goodwill|tradename|leases?|borrowings?|debt|income taxes?)\b", re.I)),
        (2, re.compile(r"\b(?:financial statements?|notes? to)\b", re.I)),
    ),
}


def _phrase_present(text: str, phrase: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text, re.I))


def normalize_for_matching(value: str) -> str:
    """Return a punctuation-insensitive, Unicode-stable matching form."""
    value = unicodedata.normalize("NFKD", value).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def detect_entities(question: str, known_tickers: set[str], aliases: dict[str, str]) -> tuple[str, ...]:
    # Resolve symbols against the corpus catalog instead of using capitalization
    # as an identity signal. This supports lower/mixed-case and one-character
    # tickers while always returning the canonical catalog spelling.
    canonical_tickers = {
        ticker.casefold(): ticker
        for ticker in known_tickers
    }
    found: set[str] = set()
    for token in re.findall(
        r"(?<![A-Za-z0-9])\$?([A-Za-z][A-Za-z0-9.-]{0,9})(?![A-Za-z0-9])",
        question,
    ):
        canonical = canonical_tickers.get(token.casefold())
        if canonical is not None:
            found.add(canonical)
    normalized_question = normalize_for_matching(question)
    for alias, ticker in aliases.items():
        canonical = canonical_tickers.get(ticker.casefold())
        normalized_alias = normalize_for_matching(alias)
        if canonical and normalized_alias and _phrase_present(normalized_question, normalized_alias):
            found.add(canonical)
    return tuple(sorted(found))


def detect_claims(question: str) -> tuple[str, ...]:
    normalized_question = normalize_for_matching(question)
    found: list[str] = []
    for claim in sorted(CLAIM_SECTION_TABLE, key=len, reverse=True):
        if _phrase_present(normalized_question, claim) and not any(claim in prior for prior in found):
            found.append(claim)
    for claim, pattern in CLAIM_PATTERNS:
        if pattern.search(normalized_question) and claim not in found:
            found.append(claim)
    broad_claims = {
        "business", "operations", "segment", "accounting", "margin",
        "trend", "risk",
    }
    if any(claim not in broad_claims for claim in found):
        found = [claim for claim in found if claim not in broad_claims]
    if {
        "revenue recognition policy", "revenue composition"
    } & set(found):
        found = [claim for claim in found if claim != "revenue"]
    if not found:
        scores = {
            claim: sum(
                weight * len(pattern.findall(normalized_question))
                for weight, pattern in signals
            )
            for claim, signals in GENERIC_SECTION_SIGNALS.items()
        }
        best_score = max(scores.values(), default=0)
        if best_score:
            # A single primary fallback avoids manufacturing cross-section
            # requirements from incidental vocabulary.
            priority = (
                "generic financial-note disclosure",
                "generic risk disclosure",
                "generic performance analysis",
                "generic business disclosure",
            )
            found.append(next(
                claim for claim in priority if scores[claim] == best_score
            ))
    return tuple(found)


def refine_atomic_requirements(
    question: str,
    claims: tuple[str, ...],
    explicit_sections: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    """Turn one broad section fallback into independently retrievable facts.

    Precise claims retain their existing mapping. Broad generic claims are
    refined only when the question itself names atomic concepts belonging to
    the same routed section. This prevents incidental words from manufacturing
    cross-section routes while allowing one source to yield several focused
    subqueries.
    """
    inferred = tuple((claim, CLAIM_SECTION_TABLE[claim]) for claim in claims)
    if explicit_sections and claims:
        if len(explicit_sections) == 1:
            inferred = tuple((claim, explicit_sections[0]) for claim in claims)
        elif len(claims) == 1:
            inferred = tuple((claims[0], section) for section in explicit_sections)
        else:
            raise ContractError(
                "AMBIGUOUS_CLAIM_SECTION_PAIRING",
                "Multiple claims and sections require explicit caller metadata",
            )
    elif not inferred:
        inferred = tuple((section, section) for section in explicit_sections)

    broad_claims = set(GENERIC_SECTION_CLAIMS) | {
        "business", "operations", "risk", "risk factors", "accounting",
        "financial statements", "trend", "margin",
    }
    routed_sections = {section for _claim, section in inferred}
    can_refine = (
        bool(inferred)
        and len(routed_sections) == 1
        and all(claim in broad_claims or claim == section for claim, section in inferred)
    )
    if not can_refine:
        return inferred
    section = next(iter(routed_sections))
    normalized = normalize_for_matching(question)
    refined = [
        (key, section)
        for key, _prompt, pattern in ATOMIC_SECTION_REQUIREMENTS.get(section, ())
        if re.search(pattern, normalized, re.I)
    ]
    # A single focused requirement is still preferable to a very broad generic
    # query. The cap matches the default final evidence budget and is based on
    # deterministic ontology order, not benchmark performance.
    return tuple(refined[:5]) or inferred


def detect_filing_years(question: str) -> tuple[int, ...]:
    """Prefer years explicitly scoped to a 10-K over fiscal content years.

    A question may name a filing (the 2025 10-K) and separately discuss the
    fiscal period reported inside it (fiscal 2024).  When at least one year is
    syntactically attached to ``10-K``, only those filing years are routed.
    Otherwise all stated years retain the legacy comparison behavior.
    """
    explicit: set[int] = set()
    for match in FILING_YEAR_BEFORE_10K_RE.finditer(question):
        explicit.add(int(match.group(1)))
        if match.group(2):
            explicit.add(int(match.group(2)))
    explicit.update(
        int(match.group(1)) for match in FILING_YEAR_AFTER_10K_RE.finditer(question)
    )
    if explicit:
        return tuple(sorted(explicit))
    return tuple(sorted({int(value) for value in YEAR_RE.findall(question)}))


def detect_sections(question: str) -> tuple[str, ...]:
    result: list[str] = []
    for match in SECTION_RE.finditer(question):
        section = f"Item_{match.group(1).upper()}"
        if section in VALID_SECTIONS and section not in result:
            result.append(section)
    return tuple(result)


class SourceResolver:
    def __init__(self, records: Iterable[FilingRecord]):
        self.records = tuple(records)

    @classmethod
    def from_connection(cls, conn: Any) -> "SourceResolver":
        sql = """
          SELECT DISTINCT ticker, filing_year, doc_type, accession_number
          FROM public.rag_eligible_10k_chunks
          ORDER BY ticker, filing_year, doc_type, accession_number
        """
        with conn.cursor() as cursor:
            cursor.execute(sql)
            records = [FilingRecord(str(a), int(b), str(c), str(d)) for a, b, c, d in cursor.fetchall()]
        return cls(records)

    def resolve(self, ticker: str, year: int, doc_type: str = "10-K") -> FilingRecord:
        matches = [
            r for r in self.records
            if r.ticker.casefold() == ticker.casefold()
            and r.filing_year == year
            and r.doc_type.casefold() == doc_type.casefold()
        ]
        if not matches:
            raise ContractError("SOURCE_NOT_FOUND", f"No eligible {doc_type} source for {ticker} {year}")
        if len(matches) != 1:
            raise ContractError("SOURCE_AMBIGUOUS", f"Expected one eligible source for {ticker} {year}; found {len(matches)}")
        return matches[0]


def _pair_entities_years(question: str, entities: tuple[str, ...], years: tuple[int, ...], aliases: dict[str, str]) -> tuple[tuple[str, int], ...]:
    if len(entities) == 1:
        return tuple((entities[0], year) for year in years)
    if len(years) == 1:
        return tuple((ticker, years[0]) for ticker in entities)
    normalized_question = normalize_for_matching(question)
    names: dict[str, set[str]] = {ticker: {ticker.casefold()} for ticker in entities}
    for alias, ticker in aliases.items():
        if ticker in names:
            names[ticker].add(normalize_for_matching(alias))
    entity_positions: dict[str, int] = {}
    for ticker, variants in names.items():
        positions = [m.start() for variant in variants if variant
                     for m in re.finditer(rf"(?<!\w){re.escape(variant)}(?!\w)", normalized_question)]
        if positions:
            entity_positions[ticker] = min(positions)
    year_positions = {year: normalized_question.find(str(year)) for year in years}
    if len(entity_positions) != len(entities) or any(position < 0 for position in year_positions.values()):
        raise ContractError("AMBIGUOUS_COMPANY_YEAR_PAIRING", "Multi-axis comparison requires explicit company-year pairing")
    entity_pos = list(entity_positions.values())
    year_pos = list(year_positions.values())
    # Lists such as "A and B ... 2023 and 2024" state both axes but no pairing.
    if max(entity_pos) < min(year_pos) or max(year_pos) < min(entity_pos):
        raise ContractError("AMBIGUOUS_COMPANY_YEAR_PAIRING", "Multi-axis comparison requires explicit company-year pairing")
    ordered_entities = tuple(sorted(entities, key=entity_positions.get))
    if len(ordered_entities) == len(years):
        ordered_positions = [entity_positions[ticker] for ticker in ordered_entities]
        boundaries = [
            (left + right) // 2
            for left, right in zip(ordered_positions, ordered_positions[1:])
        ]
        local_pairs: list[tuple[str, int]] = []
        local_pairing_valid = True
        for index, ticker in enumerate(ordered_entities):
            start = -1 if index == 0 else boundaries[index - 1]
            end = len(normalized_question) + 1 if index == len(boundaries) else boundaries[index]
            local_years = [
                year for year in years
                if start < year_positions[year] < end
            ]
            if len(local_years) != 1:
                local_pairing_valid = False
                break
            local_pairs.append((ticker, local_years[0]))
        if local_pairing_valid and len({year for _, year in local_pairs}) == len(years):
            return tuple(local_pairs)
    scored: list[tuple[int, tuple[int, ...]]] = []
    for year_order in itertools.permutations(years):
        score = sum(abs(entity_positions[ticker] - year_positions[year]) for ticker, year in zip(ordered_entities, year_order))
        scored.append((score, year_order))
    scored.sort(key=lambda item: item[0])
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        raise ContractError("AMBIGUOUS_COMPANY_YEAR_PAIRING", "Multi-axis comparison requires explicit company-year pairing")
    return tuple(zip(ordered_entities, scored[0][1], strict=True))


def build_subqueries(request_id: str, question: str, known_tickers: set[str], aliases: dict[str, str], resolver: SourceResolver) -> tuple[QueryType, tuple[SubQuery, ...]]:
    entities = detect_entities(question, known_tickers, aliases)
    years = detect_filing_years(question)
    claims = detect_claims(question)
    explicit_sections = detect_sections(question)
    if not entities:
        raise ContractError("ENTITY_UNRESOLVED", "No company resolved from the approved corpus")
    if not years:
        raise ContractError("YEAR_UNRESOLVED", "No filing year was stated")
    if not claims and not explicit_sections:
        raise ContractError("SECTION_UNRESOLVED", "No supported claim or section was resolved")
    pairs = _pair_entities_years(question, entities, years, aliases)
    requirements = refine_atomic_requirements(
        question, claims, explicit_sections
    )
    if len(entities) > 1 and len(years) > 1:
        query_type = QueryType.MULTI_AXIS_COMPARISON
    elif len(years) > 1:
        query_type = QueryType.TEMPORAL_COMPARISON
    elif len(entities) > 1:
        query_type = QueryType.CROSS_COMPANY_COMPARISON
    elif len(requirements) > 1:
        query_type = QueryType.CROSS_SECTION_SYNTHESIS
    else:
        query_type = QueryType.SINGLE_SOURCE
    output: list[SubQuery] = []
    for ticker, year in pairs:
        source = resolver.resolve(ticker, year)
        for claim, section in requirements:
            description = CLAIM_PROMPTS.get(claim, claim)
            side = f"{ticker}-{year}-{section}-{re.sub(r'[^a-z0-9]+', '-', claim.lower()).strip('-')}"
            identity = "|".join((ticker, str(year), source.accession_number, section, claim))
            stable_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            output.append(SubQuery(
                subquery_id=f"sq-{stable_id}",
                question=f"In the {year} 10-K filing for {ticker}, Section {section}, what are the key disclosures regarding {description}?",
                claim_key=claim, comparison_side_id=side, ticker=ticker,
                filing_year=year, doc_type="10-K",
                accession_number=source.accession_number, section_code=section,
            ))
    return query_type, tuple(output)


class RetrieverLike(Protocol):
    def retrieve(self, subquery: SubQuery) -> list[RetrievedChunk]: ...


class ProductionRetrieverAdapter:
    """Strict adapter for query_api.ProductionRetriever.retrieve(question, sources)."""
    REQUIRED = {"chunk_id", "semantic_rank", "cross_encoder_rank_within_source", "cross_encoder_score", "ticker", "filing_year", "doc_type", "accession_number", "section_code", "chunk_text"}

    def __init__(
        self,
        retriever: Any,
        source_spec_class: Any,
        original_question: str | None = None,
    ):
        self.retriever = retriever
        self.source_spec_class = source_spec_class
        self.original_question = original_question

    def retrieve(self, subquery: SubQuery) -> list[RetrievedChunk]:
        source = self.source_spec_class(
            ticker=subquery.ticker, filing_year=subquery.filing_year,
            accession_number=subquery.accession_number,
            section_code=subquery.section_code, doc_type=subquery.doc_type,
        )
        adaptive = getattr(self.retriever, "retrieve_requirement", None)
        if callable(adaptive) and self.original_question:
            response = adaptive(subquery, original_question=self.original_question)
        else:
            response = self.retriever.retrieve(subquery.question, [source])
        if not isinstance(response, dict) or not isinstance(response.get("evidence"), list):
            raise ContractError("ADAPTER_CONTRACT_FAILED", "Production retriever response lacks an evidence list")
        adapted: list[RetrievedChunk] = []
        for item in response["evidence"]:
            if not isinstance(item, dict):
                raise ContractError("ADAPTER_CONTRACT_FAILED", "Evidence item is not a mapping")
            missing = sorted(self.REQUIRED - set(item))
            if missing:
                raise ContractError("ADAPTER_CONTRACT_FAILED", f"Evidence item missing required fields: {', '.join(missing)}")
            chunk = RetrievedChunk(
                chunk_id=int(item["chunk_id"]), semantic_rank=int(item["semantic_rank"]),
                cross_encoder_rank=int(item["cross_encoder_rank_within_source"]),
                cross_encoder_score=float(item["cross_encoder_score"]),
                ticker=str(item["ticker"]), filing_year=int(item["filing_year"]),
                doc_type=str(item["doc_type"]), accession_number=str(item["accession_number"]),
                section_code=str(item["section_code"]), chunk_text=str(item["chunk_text"]),
                page_start=item.get("page_start"), page_end=item.get("page_end"),
                page_unavailable_reason=item.get("page_unavailable_reason") or "html_source",
            )
            expected = (
                subquery.ticker,
                subquery.filing_year,
                subquery.doc_type,
                subquery.accession_number,
            )
            actual = (
                chunk.ticker,
                chunk.filing_year,
                chunk.doc_type,
                chunk.accession_number,
            )
            if actual != expected:
                raise ContractError("ROUTING_INTEGRITY_FAILED", f"Chunk {chunk.chunk_id} does not match its authorized source")
            if chunk.section_code not in {"Item_1", "Item_1A", "Item_7", "Item_8"}:
                raise ContractError(
                    "ROUTING_INTEGRITY_FAILED",
                    f"Chunk {chunk.chunk_id} is outside supported retrieval sections",
                )
            adapted.append(chunk)
        return adapted


def aggregate(
    subqueries: tuple[SubQuery, ...],
    retriever: RetrieverLike,
    evidence_limit: int | None = None,
    *,
    sufficiency_enabled: bool = False,
    direct_support_labels: dict[tuple[int, str], str] | None = None,
) -> dict[str, Any]:
    if not subqueries:
        raise ContractError("NO_SUBQUERIES", "At least one subquery is required")
    required = [sq.comparison_side_id for sq in subqueries]
    if len(set(required)) != len(required):
        raise ContractError("DUPLICATE_REQUIREMENT", "Subquery requirement IDs must be unique")
    buckets: dict[str, list[EvidenceItem]] = {}
    chunk_sources: dict[int, tuple[str, int, str, str, str]] = {}
    for sq in subqueries:
        chunks = retriever.retrieve(sq)[:5]
        bucket: list[EvidenceItem] = []
        for chunk in chunks:
            source_key = (chunk.ticker, chunk.filing_year, chunk.doc_type, chunk.accession_number, chunk.section_code)
            previous = chunk_sources.get(chunk.chunk_id)
            if previous is not None and previous != source_key:
                raise ContractError("ROUTING_INTEGRITY_FAILED", f"Chunk {chunk.chunk_id} appeared under conflicting sources")
            chunk_sources[chunk.chunk_id] = source_key
            bucket.append(EvidenceItem(
                chunk_id=chunk.chunk_id, semantic_rank=chunk.semantic_rank,
                cross_encoder_rank=chunk.cross_encoder_rank,
                cross_encoder_score=chunk.cross_encoder_score, aggregated_rank=-1,
                subquery_id=sq.subquery_id, claim_key=sq.claim_key,
                comparison_side_id=sq.comparison_side_id, ticker=chunk.ticker,
                filing_year=chunk.filing_year, doc_type=chunk.doc_type,
                accession_number=chunk.accession_number, section_code=chunk.section_code,
                chunk_text=chunk.chunk_text, page_start=chunk.page_start,
                page_end=chunk.page_end, page_unavailable_reason=chunk.page_unavailable_reason,
                provenance=[{"subquery_id": sq.subquery_id, "comparison_side_id": sq.comparison_side_id}],
            ))
        if not bucket:
            return {"status": "insufficient_evidence", "error_code": "MISSING_COMPARISON_SIDE", "answer": None, "missing_sides": [sq.comparison_side_id], "evidence": [], "contract_version": CONTRACT_VERSION}
        # Preserve the retriever's deterministic policy order. For the adaptive
        # implementation this may be RRF rather than cross-encoder order.
        buckets[sq.comparison_side_id] = bucket
    if evidence_limit is not None:
        limit = evidence_limit
    elif sufficiency_enabled:
        from src.evidence_sufficiency import dynamic_evidence_budget
        limit = dynamic_evidence_budget(
            len(subqueries),
            len({
                (sq.ticker, sq.filing_year, sq.doc_type, sq.accession_number)
                for sq in subqueries
            }),
        )
    else:
        limit = max(5, 2 * len(subqueries))
    if limit < len(subqueries):
        raise ContractError("INVALID_EVIDENCE_LIMIT", "Evidence limit cannot omit a required side")
    merged: list[EvidenceItem] = []
    seen: dict[int, EvidenceItem] = {}
    for depth in range(max(map(len, buckets.values()))):
        # Preserve deterministic decomposer requirement order. This is the
        # validated 3/2 allocation when five results cover two requirements.
        for side in required:
            if len(merged) >= limit or depth >= len(buckets[side]):
                continue
            item = buckets[side][depth]
            if item.chunk_id in seen:
                seen[item.chunk_id].provenance.extend(item.provenance)
                continue
            seen[item.chunk_id] = item
            merged.append(item)
    represented = {
        provenance["comparison_side_id"]
        for item in merged
        for provenance in item.provenance
    }
    missing = sorted(set(buckets) - represented)
    if missing:
        return {"status": "insufficient_evidence", "error_code": "MISSING_COMPARISON_SIDE", "answer": None, "missing_sides": missing, "evidence": [], "contract_version": CONTRACT_VERSION}
    for rank, item in enumerate(merged, 1):
        item.aggregated_rank = rank
    coverage_counts = {
        side: sum(
            any(
                provenance["comparison_side_id"] == side
                for provenance in item.provenance
            )
            for item in merged
        )
        for side in required
    }
    completeness = (
        "full"
        if all(count >= 2 for count in coverage_counts.values())
        else "limited"
    )
    response = {
        "status": "success",
        "contract_version": CONTRACT_VERSION,
        "evidence_completeness": completeness,
        "required_sides": required,
        "requirement_count": len(required),
        "requirement_coverage": coverage_counts,
        "all_requirements_represented": all(
            count >= 1 for count in coverage_counts.values()
        ),
        "evidence": [asdict(x) for x in merged],
    }
    if sufficiency_enabled:
        from src.evidence_sufficiency import (
            Candidate,
            Requirement,
            SupportLabel,
            build_support_contract,
        )
        labels = direct_support_labels or {}
        requirements = [
            Requirement(
                requirement_id=sq.subquery_id,
                comparison_side_id=sq.comparison_side_id,
                claim_key=sq.claim_key,
                ticker=sq.ticker,
                filing_year=sq.filing_year,
                doc_type=sq.doc_type,
                accession_number=sq.accession_number,
                section_code=sq.section_code,
            )
            for sq in subqueries
        ]
        candidates = []
        for rank, item in enumerate(merged, 1):
            support = {}
            for provenance in item.provenance:
                side = provenance["comparison_side_id"]
                subquery_id = provenance["subquery_id"]
                raw_label = labels.get((item.chunk_id, side), "partial")
                support[subquery_id] = SupportLabel(raw_label)
            candidates.append(Candidate(
                chunk_id=item.chunk_id,
                ticker=item.ticker,
                filing_year=item.filing_year,
                doc_type=item.doc_type,
                accession_number=item.accession_number,
                section_code=item.section_code,
                relevance_rank=rank,
                support=support,
            ))
        response["support"] = build_support_contract(
            "decomposed", requirements, candidates,
        )
    return response


CITATION_FIELDS = ("ticker", "filing_year", "doc_type", "accession_number", "section_code", "chunk_id", "aggregated_rank")


def validate_citations(citations: list[dict[str, Any]], evidence: list[dict[str, Any]], required_sides: list[str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    by_id = {int(item["chunk_id"]): item for item in evidence}
    cited: set[str] = set()
    for citation in citations:
        missing = [field for field in CITATION_FIELDS if field not in citation]
        if missing:
            errors.append(f"citation missing fields: {', '.join(missing)}")
            continue
        item = by_id.get(int(citation["chunk_id"]))
        if item is None:
            errors.append(f"chunk {citation['chunk_id']} not in supplied evidence")
            continue
        for field in CITATION_FIELDS:
            if citation[field] != item[field]:
                errors.append(f"chunk {citation['chunk_id']}: {field} mismatch")
        if item.get("page_start") is None and citation.get("page_unavailable_reason") != "html_source":
            errors.append(f"chunk {citation['chunk_id']}: null page requires html_source reason")
        cited.add(item["comparison_side_id"])
    for side in required_sides:
        if side not in cited:
            errors.append(f"comparison side {side} not represented")
    return not errors, errors
