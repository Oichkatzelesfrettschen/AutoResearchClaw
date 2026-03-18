"""Europe PMC API client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``search_europepmc(query, limit, year_min)`` -> ``list[Paper]``

Rate limits:
  - No strict published limit, but recommended <10 req/s.
  - We enforce 0.5s between requests.

Europe PMC provides access to biomedical and life sciences literature,
including PubMed, PubMed Central, and additional European sources.
No authentication required.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from researchclaw.literature.models import Author, Paper

logger = logging.getLogger(__name__)

_BASE_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 0.5
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 20

_last_request_time: float = 0.0


def search_europepmc(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
) -> list[Paper]:
    """Search Europe PMC for biomedical papers matching *query*.

    Parameters
    ----------
    query:
        Free-text search query.
    limit:
        Maximum number of results (capped at 100).
    year_min:
        If >0, restrict to papers published in this year or later.

    Returns
    -------
    list[Paper]
        Parsed papers.  Empty list on network failure.
    """
    global _last_request_time  # noqa: PLW0603

    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_SEC:
        time.sleep(_RATE_LIMIT_SEC - elapsed)

    limit = min(limit, 100)

    # Build query with optional year filter
    q = query
    if year_min > 0:
        q = f"({query}) AND (PUB_YEAR:[{year_min} TO *])"

    params: dict[str, str] = {
        "query": q,
        "format": "json",
        "pageSize": str(limit),
        "resultType": "core",
    }

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    _last_request_time = time.monotonic()
    data = _request_with_retry(url)
    if data is None:
        return []

    result_list = data.get("resultList", {})
    results = result_list.get("result", [])
    if not isinstance(results, list):
        return []

    papers: list[Paper] = []
    for item in results:
        try:
            papers.append(_parse_europepmc_item(item))
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to parse Europe PMC item: %s", item.get("id", "?")
            )
    return papers


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _request_with_retry(url: str) -> dict[str, Any] | None:
    """GET *url* with exponential back-off retries."""
    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": _USER_AGENT,
                },
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                if retry_after:
                    try:
                        wait = float(retry_after)
                    except (ValueError, TypeError):
                        wait = 2 ** (attempt + 1)
                else:
                    wait = 2 ** (attempt + 1)
                if wait > 300:
                    logger.warning(
                        "[rate-limit] Europe PMC Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] Europe PMC 429. Waiting %.1fs (attempt %d/%d)...",
                    wait + jitter,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(wait + jitter)
                continue

            if exc.code in (500, 502, 503, 504):
                wait = 2 ** attempt
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "Europe PMC HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("Europe PMC HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "Europe PMC request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("Europe PMC request exhausted retries for: %s", url)
    return None


def _parse_europepmc_item(item: dict[str, Any]) -> Paper:
    """Convert a single Europe PMC result JSON to a ``Paper``."""
    title = str(item.get("title") or "").strip()
    # Remove trailing period that Europe PMC sometimes includes
    if title.endswith("."):
        title = title[:-1]

    # Authors: Europe PMC provides authorString as semicolon-separated
    author_string = str(item.get("authorString") or "").strip()
    if author_string:
        author_names = [a.strip() for a in author_string.split(",") if a.strip()]
    else:
        author_names = []
    # Also try structured authorList
    if not author_names:
        author_list = item.get("authorList", {}).get("author", [])
        for a in author_list:
            if isinstance(a, dict):
                full = f"{a.get('firstName', '')} {a.get('lastName', '')}".strip()
                if full:
                    author_names.append(full)

    authors = tuple(Author(name=name) for name in author_names if name)

    year = 0
    raw_year = item.get("pubYear") or item.get("journalInfo", {}).get("yearOfPublication")
    if raw_year:
        try:
            year = int(raw_year)
        except (ValueError, TypeError):
            pass

    abstract = str(item.get("abstractText") or "").strip()
    doi = str(item.get("doi") or "").strip()

    # PMID
    pmid = str(item.get("pmid") or "").strip()

    # PMC ID
    pmcid = str(item.get("pmcid") or "").strip()

    # Venue / journal
    journal_info = item.get("journalInfo") or {}
    journal = journal_info.get("journal", {})
    venue = str(journal.get("title") or journal_info.get("journalTitle") or "").strip()

    citation_count = int(item.get("citedByCount") or 0)

    # URL
    url = ""
    if pmcid:
        url = f"https://europepmc.org/article/PMC/{pmcid}"
    elif pmid:
        url = f"https://europepmc.org/article/MED/{pmid}"
    elif doi:
        url = f"https://doi.org/{doi}"

    # Paper ID
    epmc_id = str(item.get("id") or "").strip()
    if pmid:
        paper_id = f"europepmc-{pmid}"
    elif pmcid:
        paper_id = f"europepmc-{pmcid}"
    elif epmc_id:
        paper_id = f"europepmc-{epmc_id}"
    else:
        paper_id = f"europepmc-{title[:30]}"

    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        abstract=abstract,
        venue=venue,
        citation_count=citation_count,
        doi=doi,
        url=url,
        source="europepmc",
    )
