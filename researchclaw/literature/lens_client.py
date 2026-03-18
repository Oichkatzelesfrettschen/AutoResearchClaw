"""Lens.org Scholarly API client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``search_lens(query, limit, year_min, api_key)`` -> ``list[Paper]``

Rate limits:
  - Depends on subscription tier; free tier allows ~50 req/min.
  - We enforce 1s between requests.

Lens.org aggregates patents and scholarly works from CrossRef,
PubMed, CORE, and other sources.  An API key is required.
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

_BASE_URL = "https://api.lens.org/scholarly/search"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 1.0
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 20

_last_request_time: float = 0.0


def search_lens(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
    api_key: str = "",
) -> list[Paper]:
    """Search Lens.org for scholarly works matching *query*.

    Parameters
    ----------
    query:
        Free-text search query (matched against title).
    limit:
        Maximum number of results (capped at 100).
    year_min:
        If >0, restrict to papers published in this year or later.
    api_key:
        Lens.org API key (required).

    Returns
    -------
    list[Paper]
        Parsed papers.  Empty list on network failure or missing API key.
    """
    if not api_key:
        logger.warning("Lens API key is required.  Set api_key parameter.")
        return []

    global _last_request_time  # noqa: PLW0603

    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_SEC:
        time.sleep(_RATE_LIMIT_SEC - elapsed)

    limit = min(limit, 100)

    # Build request body
    body: dict[str, Any] = {
        "size": limit,
    }

    if year_min > 0:
        body["query"] = {
            "bool": {
                "must": [
                    {"match": {"title": query}},
                    {"range": {"year_published": {"gte": year_min}}},
                ],
            },
        }
    else:
        body["query"] = {"match": {"title": query}}

    _last_request_time = time.monotonic()
    data = _request_with_retry(body, api_key)
    if data is None:
        return []

    results = data.get("data", [])
    if not isinstance(results, list):
        return []

    papers: list[Paper] = []
    for item in results:
        try:
            papers.append(_parse_lens_item(item))
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to parse Lens item: %s", item.get("lens_id", "?")
            )
    return papers


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _request_with_retry(
    body: dict[str, Any],
    api_key: str,
) -> dict[str, Any] | None:
    """POST to Lens API with exponential back-off retries."""
    payload = json.dumps(body).encode("utf-8")

    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                _BASE_URL,
                data=payload,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "User-Agent": _USER_AGENT,
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
                resp_body = resp.read().decode("utf-8")
                return json.loads(resp_body)
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
                        "[rate-limit] Lens Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] Lens 429. Waiting %.1fs (attempt %d/%d)...",
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
                    "Lens HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("Lens HTTP %d for POST %s", exc.code, _BASE_URL)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "Lens request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("Lens request exhausted retries for: %s", _BASE_URL)
    return None


def _parse_lens_item(item: dict[str, Any]) -> Paper:
    """Convert a single Lens scholarly work JSON to a ``Paper``."""
    title = str(item.get("title") or "").strip()

    # Authors
    raw_authors = item.get("authors") or []
    authors = tuple(
        Author(
            name=f"{a.get('first_name', '')} {a.get('last_name', '')}".strip()
            or str(a.get("name", "Unknown")),
        )
        for a in raw_authors
        if isinstance(a, dict)
    )

    year = int(item.get("year_published") or 0)
    abstract = str(item.get("abstract") or "").strip()

    # DOI from external_ids
    external_ids = item.get("external_ids") or []
    doi = ""
    for eid in external_ids:
        if isinstance(eid, dict) and eid.get("type") == "doi":
            doi = str(eid.get("value", "")).strip()
            break

    citation_count = int(item.get("scholarly_citations_count") or 0)

    lens_id = str(item.get("lens_id") or "").strip()

    # URL
    url = ""
    if doi:
        url = f"https://doi.org/{doi}"
    elif lens_id:
        url = f"https://www.lens.org/lens/scholar/article/{lens_id}"

    paper_id = f"lens-{lens_id}" if lens_id else f"lens-{title[:30]}"

    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        abstract=abstract,
        citation_count=citation_count,
        doi=doi,
        url=url,
        source="lens",
    )
