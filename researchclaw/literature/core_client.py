"""CORE API client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``search_core(query, limit, year_min, api_key)`` -> ``list[Paper]``

Rate limits:
  - Free tier: 5 requests per 10 seconds
  - We enforce 2s between requests to stay within free tier.

CORE aggregates open-access research from repositories worldwide.
An API key is optional but recommended for higher rate limits.
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

_BASE_URL = "https://api.core.ac.uk/v3/search/works"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 2.0
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 20

_last_request_time: float = 0.0


def search_core(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
    api_key: str = "",
) -> list[Paper]:
    """Search CORE for open-access papers matching *query*.

    Parameters
    ----------
    query:
        Free-text search query.
    limit:
        Maximum number of results (capped at 100).
    year_min:
        If >0, restrict to papers published in this year or later.
    api_key:
        Optional CORE API key for higher rate limits.

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
        q = f"({query}) AND yearPublished>={year_min}"

    params: dict[str, str] = {
        "q": q,
        "limit": str(limit),
    }

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    _last_request_time = time.monotonic()
    data = _request_with_retry(url, headers)
    if data is None:
        return []

    results = data.get("results", [])
    if not isinstance(results, list):
        return []

    papers: list[Paper] = []
    for item in results:
        try:
            papers.append(_parse_core_item(item))
        except Exception:  # noqa: BLE001
            logger.debug("Failed to parse CORE item: %s", item.get("id", "?"))
    return papers


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _request_with_retry(
    url: str,
    headers: dict[str, str],
) -> dict[str, Any] | None:
    """GET *url* with exponential back-off retries."""
    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
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
                        "[rate-limit] CORE Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] CORE 429. Waiting %.1fs (attempt %d/%d)...",
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
                    "CORE HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("CORE HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "CORE request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("CORE request exhausted retries for: %s", url)
    return None


def _parse_core_item(item: dict[str, Any]) -> Paper:
    """Convert a single CORE result JSON to a ``Paper``."""
    title = str(item.get("title") or "").strip()

    # Authors
    raw_authors = item.get("authors") or []
    authors = tuple(
        Author(name=str(a.get("name", "Unknown")).strip())
        for a in raw_authors
        if isinstance(a, dict) and a.get("name")
    )

    year = int(item.get("yearPublished") or 0)
    abstract = str(item.get("abstract") or "").strip()
    doi = str(item.get("doi") or "").strip()

    # URL: prefer downloadUrl, fall back to doi link
    download_url = str(item.get("downloadUrl") or "").strip()
    url = download_url
    if not url and doi:
        url = f"https://doi.org/{doi}"

    core_id = str(item.get("id") or "").strip()
    paper_id = f"core-{core_id}" if core_id else f"core-{title[:30]}"

    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        abstract=abstract,
        doi=doi,
        url=url,
        source="core",
    )
