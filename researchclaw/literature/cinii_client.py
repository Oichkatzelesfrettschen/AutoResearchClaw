"""CiNii Research API client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``search_cinii(query, limit, year_min, app_id)`` -> ``list[Paper]``

Rate limits:
  - No published hard limit for basic search.
  - We enforce 1s between requests out of courtesy.

CiNii Research indexes Japanese academic papers, books, and dissertations
from J-STAGE, KAKEN, and institutional repositories.  An app_id is
optional but recommended for stable access.
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

_BASE_URL = "https://cir.nii.ac.jp/opensearch/articles"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 1.0
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 20

_last_request_time: float = 0.0


def search_cinii(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
    app_id: str = "",
) -> list[Paper]:
    """Search CiNii Research for articles matching *query*.

    Parameters
    ----------
    query:
        Free-text search query.
    limit:
        Maximum number of results (capped at 100).
    year_min:
        If >0, restrict to papers published in this year or later.
    app_id:
        Optional CiNii application ID for authenticated access.

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
        "format": "json",
    }
    if year_min > 0:
        params["from"] = str(year_min)
    if app_id:
        params["appid"] = app_id

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    _last_request_time = time.monotonic()
    data = _request_with_retry(url)
    if data is None:
        return []

    # CiNii opensearch JSON wraps results in various structures
    items = []
    if isinstance(data, dict):
        # Try common CiNii response paths
        items = data.get("items", [])
        if not items:
            graph = data.get("@graph", [])
            if graph:
                items = graph
    if not isinstance(items, list):
        return []

    papers: list[Paper] = []
    for item in items:
        try:
            papers.append(_parse_cinii_item(item))
        except Exception:  # noqa: BLE001
            logger.debug("Failed to parse CiNii item: %s", item.get("@id", "?"))
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
                        "[rate-limit] CiNii Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] CiNii 429. Waiting %.1fs (attempt %d/%d)...",
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
                    "CiNii HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("CiNii HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "CiNii request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("CiNii request exhausted retries for: %s", url)
    return None


def _parse_cinii_item(item: dict[str, Any]) -> Paper:
    """Convert a single CiNii JSON item to a ``Paper``."""
    # Title: may be string or dict with @value
    raw_title = item.get("title") or item.get("dc:title") or ""
    if isinstance(raw_title, dict):
        title = str(raw_title.get("@value", "")).strip()
    elif isinstance(raw_title, list):
        first = raw_title[0] if raw_title else ""
        title = str(first.get("@value", first) if isinstance(first, dict) else first).strip()
    else:
        title = str(raw_title).strip()

    # Authors
    raw_authors = item.get("dc:creator") or item.get("creator") or item.get("authors") or []
    if isinstance(raw_authors, str):
        author_names = [raw_authors.strip()]
    elif isinstance(raw_authors, dict):
        author_names = [str(raw_authors.get("@value", raw_authors.get("name", ""))).strip()]
    elif isinstance(raw_authors, list):
        author_names = []
        for a in raw_authors:
            if isinstance(a, str):
                author_names.append(a.strip())
            elif isinstance(a, dict):
                author_names.append(
                    str(a.get("@value", a.get("name", ""))).strip()
                )
    else:
        author_names = []

    authors = tuple(Author(name=name) for name in author_names if name)

    # Year
    raw_date = str(
        item.get("prism:publicationDate")
        or item.get("dc:date")
        or item.get("publicationDate")
        or ""
    ).strip()
    year = 0
    if raw_date:
        try:
            year = int(raw_date[:4])
        except (ValueError, TypeError):
            pass

    # DOI
    doi = ""
    raw_doi = item.get("doi") or item.get("prism:doi") or ""
    if isinstance(raw_doi, str):
        doi = raw_doi.strip()
    elif isinstance(raw_doi, list) and raw_doi:
        doi = str(raw_doi[0]).strip()

    # URL
    url = str(item.get("@id") or item.get("link") or item.get("url") or "").strip()
    if not url and doi:
        url = f"https://doi.org/{doi}"

    # ID
    cinii_id = str(item.get("@id") or item.get("id") or "").strip()
    paper_id = f"cinii-{cinii_id.split('/')[-1]}" if cinii_id else f"cinii-{title[:30]}"

    # Description / abstract
    abstract = str(
        item.get("description") or item.get("dc:description") or ""
    ).strip()

    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        abstract=abstract,
        doi=doi,
        url=url,
        source="cinii",
    )
