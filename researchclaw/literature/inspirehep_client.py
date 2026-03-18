"""INSPIRE High-Energy Physics literature API client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``search_inspirehep(query, limit, year_min)`` -> ``list[Paper]``

Rate limits:
  - INSPIRE has no published rate limit but requests courtesy.
  - We enforce 0.5s between requests.

Authentication:
  - No authentication required.
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

_BASE_URL = "https://inspirehep.net/api/literature"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 0.5
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 20

_last_request_time: float = 0.0


def search_inspirehep(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
) -> list[Paper]:
    """Search INSPIRE-HEP for papers matching *query*.

    Parameters
    ----------
    query:
        Free-text search query (uses INSPIRE query syntax).
    limit:
        Maximum number of results (capped at 250).
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

    limit = min(limit, 250)

    # INSPIRE supports "and de > YEAR" syntax for year filtering
    q = query
    if year_min > 0:
        q = f"{query} and de > {year_min}"

    params: dict[str, str] = {
        "q": q,
        "size": str(limit),
        "fields": "titles,authors,earliest_date,abstracts,dois,citation_count",
    }

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    _last_request_time = time.monotonic()
    data = _request_with_retry(url)
    if data is None:
        return []

    hits = data.get("hits", {}).get("hits", [])
    if not isinstance(hits, list):
        return []

    papers: list[Paper] = []
    for hit in hits:
        try:
            papers.append(_parse_inspire_hit(hit))
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to parse INSPIRE hit: %s",
                hit.get("metadata", {}).get("control_number", "?"),
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
                        "[rate-limit] INSPIRE Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] INSPIRE 429 (Retry-After: %s). "
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
                    "INSPIRE HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("INSPIRE HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "INSPIRE request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("INSPIRE request exhausted retries for: %s", url)
    return None


def _parse_inspire_hit(hit: dict[str, Any]) -> Paper:
    """Convert a single INSPIRE hit JSON to a ``Paper``."""
    metadata = hit.get("metadata", {})

    # Control number as paper_id
    control_number = str(metadata.get("control_number", ""))
    paper_id = control_number or f"inspire-{id(hit)}"

    # Title from titles array
    titles = metadata.get("titles", [])
    title = str(titles[0].get("title", "")).strip() if titles else ""

    # Authors -- limit to first 10 to avoid huge lists
    raw_authors = metadata.get("authors", [])
    authors = tuple(
        Author(name=a.get("full_name", "").strip())
        for a in raw_authors[:10]
        if isinstance(a, dict) and a.get("full_name", "").strip()
    )

    # Year from earliest_date (format "YYYY-MM-DD" or "YYYY")
    year = 0
    earliest_date = str(metadata.get("earliest_date", "")).strip()
    if earliest_date:
        try:
            year = int(earliest_date[:4])
        except (ValueError, IndexError):
            pass

    # Abstract from abstracts array
    abstracts = metadata.get("abstracts", [])
    abstract = str(abstracts[0].get("value", "")).strip() if abstracts else ""

    # DOI from dois array
    dois = metadata.get("dois", [])
    doi = str(dois[0].get("value", "")).strip() if dois else ""

    # Citation count
    citation_count = int(metadata.get("citation_count") or 0)

    # URL
    url = f"https://inspirehep.net/literature/{control_number}" if control_number else ""

    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        abstract=abstract,
        venue="",
        citation_count=citation_count,
        doi=doi,
        url=url,
        source="inspirehep",
    )
