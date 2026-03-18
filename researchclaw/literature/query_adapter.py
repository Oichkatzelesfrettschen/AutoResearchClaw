"""Per-source query adaptation for academic search APIs.

Each source has different query syntax. This module transforms a generic
search query into source-specific variants that maximize relevance.

Public API
----------
- ``adapt_query(query, source, year_min)`` -> adapted query string
- ``expand_queries(queries, sources)`` -> dict of {source: [adapted_queries]}
"""

from __future__ import annotations

import re
from typing import Sequence


# ---------------------------------------------------------------------------
# Source-specific adapters
# ---------------------------------------------------------------------------


def _adapt_openalex(query: str, year_min: int = 0) -> str:
    """OpenAlex: simple text search, no special operators needed.
    Supports filter parameter for year but that's handled by the client.
    Best with short, focused queries. Handles Boolean implicitly.
    """
    return _clean_query(query)


def _adapt_semantic_scholar(query: str, year_min: int = 0) -> str:
    """Semantic Scholar: plain text, no Boolean, no field search.
    Relevance-ranked. Best with natural language phrases.
    Max ~200 chars practical limit.
    """
    q = _clean_query(query)
    return q[:200] if len(q) > 200 else q


def _adapt_arxiv(query: str, year_min: int = 0) -> str:
    """arXiv: supports field prefixes and Boolean.
    Fields: ti (title), au (author), abs (abstract), all (all fields).
    Boolean: AND, OR, ANDNOT. Must be uppercase.
    Phrases: use double quotes.
    """
    q = _clean_query(query)
    # If query looks like plain text, search all fields
    if not any(f in q for f in ("ti:", "au:", "abs:", "all:")):
        # Put multi-word phrases in quotes for better matching
        terms = _extract_phrases_and_terms(q)
        parts = []
        for term in terms:
            if " " in term:
                parts.append(f'all:"{term}"')
            else:
                parts.append(f"all:{term}")
        return " AND ".join(parts) if len(parts) > 1 else parts[0] if parts else q
    return q


def _adapt_ads(query: str, year_min: int = 0) -> str:
    """NASA ADS: powerful Solr-based query language.
    Fields: author:, title:, abs:, full:, bibstem:, object:, aff:, inst:
    Boolean: AND (default), OR, NOT (- prefix)
    Phrases: double quotes
    Proximity: title:(map NEAR5 planar)
    Wildcards: author:"huchra, jo*"
    Year: year:2000-2005 or year:2000-present
    Synonym control: =field:term (exact, no synonyms)
    """
    q = _clean_query(query)
    if not any(f in q for f in ("title:", "author:", "abs:", "bibcode:", "bibstem:", "year:")):
        terms = _extract_phrases_and_terms(q)
        parts = []
        for term in terms:
            if " " in term:
                parts.append(f'abs:"{term}"')
            else:
                parts.append(f"abs:{term}")
        result = " ".join(parts)
        if year_min:
            result += f" year:{year_min}-9999"
        return result
    return q


def _adapt_inspirehep(query: str, year_min: int = 0) -> str:
    """INSPIRE-HEP: supports SPIRES-style and Invenio queries.
    Fields: a: (author), af: (affiliation), t: (title), k: (keyword),
            j: (journal), d: (date), ft: (fulltext), eprint:, topcit:
    Boolean: and, or, not (lowercase)
    Phrases: double quotes
    Year: d:YYYY->YYYY (date range)
    Regex: /pattern/ supported
    """
    q = _clean_query(query)
    if not any(f in q for f in ("t:", "a:", "k:", "find ", "d:")):
        terms = _extract_phrases_and_terms(q)
        parts = []
        for term in terms:
            if " " in term:
                parts.append(f't:"{term}"')
            else:
                parts.append(f"t:{term}")
        result = " and ".join(parts)
        if year_min:
            result += f" and d:{year_min}->2030"
        return result
    return q


def _adapt_crossref(query: str, year_min: int = 0) -> str:
    """CrossRef: simple text query via query parameter.
    Also supports query.title, query.author, query.bibliographic.
    No Boolean operators in the query string itself.
    Phrases: just use words, relevance-ranked.
    """
    return _clean_query(query)


def _adapt_europepmc(query: str, year_min: int = 0) -> str:
    """Europe PMC: Lucene-style queries.
    Fields: TITLE:, AUTH:, ABSTRACT:, DOI:
    Boolean: AND, OR, NOT
    Phrases: double quotes
    Year: PUB_YEAR:[2020 TO 2030]
    """
    q = _clean_query(query)
    if not any(f in q.upper() for f in ("TITLE:", "AUTH:", "ABSTRACT:")):
        terms = _extract_phrases_and_terms(q)
        parts = []
        for term in terms:
            if " " in term:
                parts.append(f'"{term}"')
            else:
                parts.append(term)
        result = " AND ".join(parts) if len(parts) > 1 else parts[0] if parts else q
        if year_min:
            result += f" AND PUB_YEAR:[{year_min} TO 2030]"
        return result
    return q


def _adapt_core(query: str, year_min: int = 0) -> str:
    """CORE: Elasticsearch-style queries.
    Fields: title:, authors:, doi:, yearPublished:
    Boolean: AND, +, space (all AND); OR operator
    Phrases: double quotes
    Range: yearPublished>=2020, yearPublished>2018
    Existence: _exists_:fieldName
    """
    q = _clean_query(query)
    if not any(f in q for f in ("title:", "authors:", "fullText:", "_exists_:")):
        terms = _extract_phrases_and_terms(q)
        parts = []
        for term in terms:
            if " " in term:
                parts.append(f'title:"{term}"')
            else:
                parts.append(term)
        result = " AND ".join(parts) if len(parts) > 1 else parts[0] if parts else q
        if year_min:
            result += f" AND yearPublished>={year_min}"
        return result
    return q


def _adapt_hal(query: str, year_min: int = 0) -> str:
    """HAL: Solr query syntax.
    Fields: title_s, authFullName_s, abstract_s
    Boolean: AND, OR, NOT
    Phrases: double quotes
    Year: producedDateY_i:[2020 TO *] (as fq parameter, handled by client)
    """
    return _clean_query(query)


def _adapt_datacite(query: str, year_min: int = 0) -> str:
    """DataCite: Elasticsearch Query String syntax.
    Fields: publicationYear:, creators.familyName:, titles.title:
    Boolean: AND, OR, +, -
    Wildcards: * (e.g. creators.familyName:mil*)
    Year: publicationYear:[2019 TO *]
    """
    q = _clean_query(query)
    if year_min and "publicationYear" not in q:
        q += f" AND publicationYear:[{year_min} TO *]"
    return q


def _adapt_scielo(query: str, year_min: int = 0) -> str:
    """SciELO: supports field prefixes and Boolean.
    Fields: ti: (title), au: (author), publication_year:, aff_country:
    Boolean: AND, OR, NOT
    """
    q = _clean_query(query)
    if year_min and "publication_year:" not in q:
        q += f" AND publication_year:{year_min}"
    return q


def _adapt_cinii(query: str, year_min: int = 0) -> str:
    """CiNii: OpenSearch, plain text query.
    Limited query operators. Best with concise terms.
    """
    q = _clean_query(query)
    return q[:100] if len(q) > 100 else q


def _adapt_dblp(query: str, year_min: int = 0) -> str:
    """DBLP: simple text search with prefix auto-completion.
    Boolean operators (AND, OR, NOT) are DISABLED.
    Phrase search also disabled. Case/diacritics insensitive.
    Only supports plain keyword matching with prefix completion
    on the rightmost term. Keep queries short and simple.
    """
    q = _clean_query(query)
    # Remove any Boolean operators since DBLP ignores them
    q = re.sub(r"\b(AND|OR|NOT)\b", " ", q)
    q = re.sub(r"\s+", " ", q).strip()
    # DBLP works best with short queries
    words = q.split()
    if len(words) > 6:
        words = words[:6]
    return " ".join(words)


def _adapt_jstage(query: str, year_min: int = 0) -> str:
    """J-STAGE: keyword search, plain text.
    Year filtering via pubyearfrom parameter (handled by client).
    """
    return _clean_query(query)


def _adapt_lens(query: str, year_min: int = 0) -> str:
    """The Lens: Elasticsearch match queries.
    Uses POST body with match/bool syntax (handled by client).
    Plain text query passed to title match.
    """
    return _clean_query(query)


# ---------------------------------------------------------------------------
# Adapter registry
# ---------------------------------------------------------------------------

_ADAPTERS: dict[str, type[...] | None] = {
    "openalex": _adapt_openalex,
    "semantic_scholar": _adapt_semantic_scholar,
    "s2": _adapt_semantic_scholar,
    "arxiv": _adapt_arxiv,
    "ads": _adapt_ads,
    "inspirehep": _adapt_inspirehep,
    "crossref": _adapt_crossref,
    "europepmc": _adapt_europepmc,
    "core": _adapt_core,
    "hal": _adapt_hal,
    "datacite": _adapt_datacite,
    "scielo": _adapt_scielo,
    "cinii": _adapt_cinii,
    "dblp": _adapt_dblp,
    "jstage": _adapt_jstage,
    "lens": _adapt_lens,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def adapt_query(query: str, source: str, year_min: int = 0) -> str:
    """Adapt a generic query for a specific source's syntax.

    If no adapter exists for the source, returns the cleaned query as-is.
    """
    adapter = _ADAPTERS.get(source.lower().replace("-", "_").replace(" ", "_"))
    if adapter is not None:
        return adapter(query, year_min)
    return _clean_query(query)


def expand_queries(
    queries: list[str],
    sources: Sequence[str],
    *,
    year_min: int = 0,
) -> dict[str, list[str]]:
    """Expand a list of generic queries into per-source adapted variants.

    Returns {source_name: [adapted_query_1, adapted_query_2, ...]}.
    Each source gets its own optimized version of each query.
    """
    result: dict[str, list[str]] = {}
    for source in sources:
        src_key = source.lower().replace("-", "_").replace(" ", "_")
        adapted: list[str] = []
        seen: set[str] = set()
        for q in queries:
            aq = adapt_query(q, src_key, year_min)
            aq_lower = aq.strip().lower()
            if aq_lower and aq_lower not in seen:
                seen.add(aq_lower)
                adapted.append(aq)
        result[src_key] = adapted
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _clean_query(query: str) -> str:
    """Remove noise from a query string."""
    # Strip leading/trailing whitespace
    q = query.strip()
    # Collapse multiple spaces
    q = re.sub(r"\s+", " ", q)
    # Remove markdown formatting
    q = re.sub(r"[*_`#]", "", q)
    return q


def _extract_phrases_and_terms(query: str) -> list[str]:
    """Split a query into meaningful phrases and individual terms.

    Groups of 2-4 related words become phrases (quoted in search).
    Single technical terms stay as individual terms.
    """
    # First extract any existing quoted phrases
    phrases: list[str] = []
    remaining = query

    # Find explicit quotes
    for match in re.finditer(r'"([^"]+)"', query):
        phrases.append(match.group(1))
        remaining = remaining.replace(match.group(0), " ")

    # Split remaining into words
    words = remaining.split()

    # Filter stopwords
    _stop = {
        "the", "and", "for", "with", "from", "that", "this", "into",
        "over", "across", "multiple", "three", "result", "comprehensive",
        "using", "based", "between", "various", "different", "several",
        "about", "their", "these", "those", "which", "where", "when",
        "have", "been", "some", "each", "also", "much", "very", "more",
        "than", "does", "what", "such", "only", "other", "like",
    }
    meaningful = [w for w in words if w.lower() not in _stop and len(w) > 2]

    # Group adjacent terms into bigrams where they look like domain phrases
    # (e.g., "rotation curve", "dark matter", "null result")
    i = 0
    while i < len(meaningful):
        if i + 1 < len(meaningful):
            bigram = f"{meaningful[i]} {meaningful[i + 1]}"
            # Keep bigram if both words are substantial
            if len(meaningful[i]) > 3 and len(meaningful[i + 1]) > 3:
                phrases.append(bigram)
                i += 2
                continue
        phrases.append(meaningful[i])
        i += 1

    return phrases
