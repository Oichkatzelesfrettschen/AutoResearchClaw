"""NASA Astrophysics Data System (ADS) API client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``search_ads(query, limit, year_min, api_key)`` -> ``list[Paper]``

Rate limits:
  - ADS allows ~5000 requests/day with a valid API key.
  - We enforce 1s between requests to stay well within limits.

Authentication:
  - Requires a free API key from https://ui.adsabs.harvard.edu/user/settings/token
  - If no key is provided, returns an empty list with a warning.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from researchclaw.literature.models import Author, Paper

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.adsabs.harvard.edu/v1/search/query"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 1.0
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 20

_last_request_time: float = 0.0


def search_ads(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
    api_key: str = "",
) -> list[Paper]:
    """Search NASA ADS for papers matching *query*.

    Parameters
    ----------
    query:
        Free-text search query.
    limit:
        Maximum number of results (capped at 200).
    year_min:
        If >0, restrict to papers published in this year or later.
    api_key:
        ADS API token.  Falls back to ``ADS_API_KEY`` env var.

    Returns
    -------
    list[Paper]
        Parsed papers.  Empty list on network failure or missing key.
    """
    global _last_request_time  # noqa: PLW0603

    key = api_key or os.environ.get("ADS_API_KEY", "")
    if not key:
        logger.warning("No ADS API key provided. Set ADS_API_KEY or pass api_key=.")
        return []

    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_SEC:
        time.sleep(_RATE_LIMIT_SEC - elapsed)

    limit = min(limit, 200)

    # Build query with optional year filter
    q = query
    if year_min > 0:
        q = f"{query} year:[{year_min} TO 9999]"

    params: dict[str, str] = {
        "q": q,
        "rows": str(limit),
        "fl": "title,author,year,abstract,doi,bibcode,citation_count,pub",
    }

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    _last_request_time = time.monotonic()
    data = _request_with_retry(url, key)
    if data is None:
        return []

    response = data.get("response", {})
    docs = response.get("docs", [])
    if not isinstance(docs, list):
        return []

    papers: list[Paper] = []
    for doc in docs:
        try:
            papers.append(_parse_ads_doc(doc))
        except Exception:  # noqa: BLE001
            logger.debug("Failed to parse ADS doc: %s", doc.get("bibcode", "?"))
    return papers


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _request_with_retry(url: str, api_key: str) -> dict[str, Any] | None:
    """GET *url* with exponential back-off retries."""
    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/json",
                    "User-Agent": _USER_AGENT,
                    "Authorization": f"Bearer {api_key}",
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
                        "[rate-limit] ADS Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] ADS 429 (Retry-After: %s). "
                    "Waiting %.1fs (attempt %d/%d)...",
                    retry_after or "none",
                    wait + jitter,
                    attempt + 1,
                    _MAX_RETRIES,
                )
                time.sleep(wait + jitter)
                continue

            if exc.code in (500, 502, 503, 504):
                wait = 2**attempt
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "ADS HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("ADS HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "ADS request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("ADS request exhausted retries for: %s", url)
    return None


def _parse_ads_doc(doc: dict[str, Any]) -> Paper:
    """Convert a single ADS doc JSON to a ``Paper``."""
    # Title is an array in ADS; take first element
    title_list = doc.get("title", [])
    title = str(title_list[0]).strip() if title_list else ""

    # Authors -- ADS returns list of "Last, First" strings
    raw_authors = doc.get("author", [])
    authors = tuple(
        Author(name=name.strip())
        for name in raw_authors
        if isinstance(name, str) and name.strip()
    )

    # Year
    year = 0
    raw_year = doc.get("year")
    if raw_year is not None:
        try:
            year = int(raw_year)
        except (ValueError, TypeError):
            pass

    # Abstract
    abstract = str(doc.get("abstract", "")).strip()

    # DOI is an array in ADS; take first element
    doi_list = doc.get("doi", [])
    doi = str(doi_list[0]).strip() if doi_list else ""

    # Bibcode as paper_id
    bibcode = str(doc.get("bibcode", "")).strip()
    paper_id = bibcode or f"ads-{title[:30]}"

    # Citation count
    citation_count = int(doc.get("citation_count") or 0)

    # Venue (publication name)
    venue = str(doc.get("pub", "")).strip()

    # URL from bibcode
    url = f"https://ui.adsabs.harvard.edu/abs/{bibcode}" if bibcode else ""

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
        source="ads",
    )
