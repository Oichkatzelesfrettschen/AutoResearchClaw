"""SciELO search client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``search_scielo(query, limit, year_min, lang)`` -> ``list[Paper]``

Rate limits:
  - No published hard limit.
  - We enforce 1s between requests out of courtesy.

SciELO indexes Latin American and Iberian scholarly literature.
Results may be in Portuguese, Spanish, or English depending on the source.
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

_BASE_URL = "https://search.scielo.org/"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 1.0
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 20

_last_request_time: float = 0.0


def search_scielo(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
    lang: str = "en",
) -> list[Paper]:
    """Search SciELO for papers matching *query*.

    Parameters
    ----------
    query:
        Free-text search query.
    limit:
        Maximum number of results (capped at 100).
    year_min:
        If >0, restrict to papers published in this year or later.
    lang:
        Language for results (default "en").

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
        "q": query,
        "count": str(limit),
        "output": "json",
        "lang": lang,
    }
    if year_min > 0:
        params["filter[year_cluster][]"] = str(year_min)

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    _last_request_time = time.monotonic()
    data = _request_with_retry(url)
    if data is None:
        return []

    # SciELO response structure varies; try common paths
    results = []
    if isinstance(data, dict):
        results = data.get("results", data.get("docs", []))
    if not isinstance(results, list):
        return []

    papers: list[Paper] = []
    for item in results:
        try:
            papers.append(_parse_scielo_item(item))
        except Exception:  # noqa: BLE001
            item_id = item.get("id", "?") if isinstance(item, dict) else repr(item)
            logger.debug("Failed to parse SciELO item: %s", item_id)
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
                        "[rate-limit] SciELO Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] SciELO 429. Waiting %.1fs (attempt %d/%d)...",
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
                    "SciELO HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("SciELO HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "SciELO request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("SciELO request exhausted retries for: %s", url)
    return None


def _parse_scielo_item(item: dict[str, Any]) -> Paper:
    """Convert a single SciELO result JSON to a ``Paper``."""
    # Title: may be string or list
    raw_title = item.get("title") or item.get("ti") or ""
    if isinstance(raw_title, list):
        title = str(raw_title[0]).strip() if raw_title else ""
    else:
        title = str(raw_title).strip()

    # Authors: may be string (semicolon-separated) or list
    raw_authors = item.get("authors") or item.get("au") or []
    if isinstance(raw_authors, str):
        author_names = [a.strip() for a in raw_authors.split(";") if a.strip()]
    elif isinstance(raw_authors, list):
        author_names = [
            str(a).strip() if isinstance(a, str) else str(a.get("name", "")).strip()
            for a in raw_authors
        ]
    else:
        author_names = []

    authors = tuple(Author(name=name) for name in author_names if name)

    # Year
    raw_year = item.get("year") or item.get("da") or item.get("publication_year") or ""
    year = 0
    if raw_year:
        try:
            year = int(str(raw_year)[:4])
        except (ValueError, TypeError):
            pass

    abstract = str(item.get("abstract") or item.get("ab") or "").strip()
    doi = str(item.get("doi") or "").strip()

    # ID
    scielo_id = str(item.get("id") or item.get("pid") or "").strip()

    # URL
    url = str(item.get("url") or "").strip()
    if not url and doi:
        url = f"https://doi.org/{doi}"
    elif not url and scielo_id:
        url = f"https://www.scielo.br/scielo.php?pid={scielo_id}"

    paper_id = f"scielo-{scielo_id}" if scielo_id else f"scielo-{title[:30]}"

    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        abstract=abstract,
        doi=doi,
        url=url,
        source="scielo",
    )
