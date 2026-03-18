"""CrossRef API client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``search_crossref(query, limit, year_min, email)`` -> ``list[Paper]``

Rate limits:
  - Polite pool (with mailto): ~50 req/s
  - Without mailto: ~20 req/s
  - We enforce 0.5s between requests to stay well within limits.
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

_BASE_URL = "https://api.crossref.org/works"
_POLITE_EMAIL = "researchclaw@example.com"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 0.5
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 20

_last_request_time: float = 0.0


def search_crossref(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
    email: str = _POLITE_EMAIL,
) -> list[Paper]:
    """Search CrossRef for papers matching *query*.

    Parameters
    ----------
    query:
        Free-text search query.
    limit:
        Maximum number of results (capped at 100).
    year_min:
        If >0, restrict to papers published in this year or later.
    email:
        Mailto for polite pool (higher rate limits).

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

    params: dict[str, str] = {
        "query": query,
        "rows": str(limit),
        "mailto": email,
    }
    if year_min > 0:
        params["filter"] = f"from-pub-date:{year_min}"

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    _last_request_time = time.monotonic()
    data = _request_with_retry(url)
    if data is None:
        return []

    message = data.get("message", {})
    items = message.get("items", [])
    if not isinstance(items, list):
        return []

    papers: list[Paper] = []
    for item in items:
        try:
            papers.append(_parse_crossref_item(item))
        except Exception:  # noqa: BLE001
            logger.debug("Failed to parse CrossRef item: %s", item.get("DOI", "?"))
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
                        "[rate-limit] CrossRef Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] CrossRef 429 (Retry-After: %s). "
                    "Waiting %.1fs (attempt %d/%d)...",
                    retry_after or "none",
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
                    "CrossRef HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("CrossRef HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "CrossRef request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("CrossRef request exhausted retries for: %s", url)
    return None


def _parse_crossref_item(item: dict[str, Any]) -> Paper:
    """Convert a single CrossRef work JSON to a ``Paper``."""
    title_list = item.get("title", [])
    title = str(title_list[0]).strip() if title_list else ""

    # Authors
    raw_authors = item.get("author", [])
    authors = tuple(
        Author(
            name=f"{a.get('given', '')} {a.get('family', '')}".strip(),
            affiliation=(
                a.get("affiliation", [{}])[0].get("name", "")
                if a.get("affiliation")
                else ""
            ),
        )
        for a in raw_authors
        if isinstance(a, dict) and a.get("family")
    )

    # Year from published-date-parts
    year = 0
    date_parts = item.get("published-date-parts") or item.get("published", {}).get(
        "date-parts", []
    )
    if date_parts and isinstance(date_parts, list) and date_parts[0]:
        try:
            year = int(date_parts[0][0])
        except (IndexError, ValueError, TypeError):
            pass

    # Venue
    container = item.get("container-title", [])
    venue = str(container[0]).strip() if container else ""

    # DOI
    doi = str(item.get("DOI", "")).strip()

    # Citation count
    citation_count = int(item.get("is-referenced-by-count") or 0)

    # URL
    url = str(item.get("URL", "")).strip()
    if not url and doi:
        url = f"https://doi.org/{doi}"

    # Abstract (some CrossRef records include it)
    abstract = str(item.get("abstract", "")).strip()
    # CrossRef abstracts sometimes contain JATS XML tags; strip them
    if abstract.startswith("<"):
        import re

        abstract = re.sub(r"<[^>]+>", "", abstract).strip()

    paper_id = f"crossref-{doi}" if doi else f"crossref-{title[:30]}"

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
        source="crossref",
    )
