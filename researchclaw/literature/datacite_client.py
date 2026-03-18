"""DataCite API client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``search_datacite(query, limit, year_min)`` -> ``list[Paper]``

Rate limits:
  - No published hard limit, but we enforce 1s between requests.

DataCite indexes datasets, software, and other research outputs
with DOIs -- complementary to CrossRef which focuses on journal articles.
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

_BASE_URL = "https://api.datacite.org/dois"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 1.0
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 20

_last_request_time: float = 0.0


def search_datacite(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
) -> list[Paper]:
    """Search DataCite for records matching *query*.

    Parameters
    ----------
    query:
        Free-text search query.
    limit:
        Maximum number of results (capped at 100).
    year_min:
        If >0, restrict to records published in this year or later.

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
        "page[size]": str(limit),
    }
    if year_min > 0:
        params["query"] = f"{query} AND publicationYear:[{year_min} TO *]"

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    _last_request_time = time.monotonic()
    data = _request_with_retry(url)
    if data is None:
        return []

    items = data.get("data", [])
    if not isinstance(items, list):
        return []

    papers: list[Paper] = []
    for item in items:
        try:
            papers.append(_parse_datacite_item(item))
        except Exception:  # noqa: BLE001
            item_id = item.get("id", "?") if isinstance(item, dict) else repr(item)
            logger.debug("Failed to parse DataCite item: %s", item_id)
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
                        "[rate-limit] DataCite Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] DataCite 429. Waiting %.1fs (attempt %d/%d)...",
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
                    "DataCite HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("DataCite HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "DataCite request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("DataCite request exhausted retries for: %s", url)
    return None


def _parse_datacite_item(item: dict[str, Any]) -> Paper:
    """Convert a single DataCite JSON:API resource to a ``Paper``."""
    attrs = item.get("attributes", {})

    # Title
    titles = attrs.get("titles") or []
    title = ""
    if titles and isinstance(titles, list):
        title = str(titles[0].get("title", "")).strip()

    # Authors
    creators = attrs.get("creators") or []
    authors = tuple(
        Author(name=str(c.get("name", "Unknown")).strip())
        for c in creators
        if isinstance(c, dict) and c.get("name")
    )

    year = int(attrs.get("publicationYear") or 0)

    # Abstract from descriptions
    descriptions = attrs.get("descriptions") or []
    abstract = ""
    if descriptions and isinstance(descriptions, list):
        abstract = str(descriptions[0].get("description", "")).strip()

    doi = str(attrs.get("doi") or "").strip()
    url = str(attrs.get("url") or "").strip()
    if not url and doi:
        url = f"https://doi.org/{doi}"

    paper_id = f"datacite-{doi}" if doi else f"datacite-{item.get('id', title[:30])}"

    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        abstract=abstract,
        doi=doi,
        url=url,
        source="datacite",
    )
