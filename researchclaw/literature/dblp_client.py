"""DBLP Computer Science Bibliography API client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``search_dblp(query, limit, year_min)`` -> ``list[Paper]``

Rate limits:
  - DBLP asks for courteous use; no formal published limit.
  - We enforce 1s between requests.

Authentication:
  - No authentication required.  Data is CC0 licensed.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from researchclaw.literature.models import Author, Paper

logger = logging.getLogger(__name__)

_BASE_URL = "https://dblp.org/search/publ/api"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 1.0
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 20

_last_request_time: float = 0.0

# Regex for stripping HTML tags from DBLP titles
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def search_dblp(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
) -> list[Paper]:
    """Search DBLP for papers matching *query*.

    Parameters
    ----------
    query:
        Free-text search query.
    limit:
        Maximum number of results (capped at 1000).
    year_min:
        If >0, filter results to this year or later (post-query filtering
        since the DBLP search API does not support year range parameters).

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

    limit = min(limit, 1000)

    # Fetch more than needed if year filtering, since we filter post-query
    fetch_limit = limit * 2 if year_min > 0 else limit

    params: dict[str, str] = {
        "q": query,
        "h": str(fetch_limit),
        "format": "json",
    }

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    _last_request_time = time.monotonic()
    data = _request_with_retry(url)
    if data is None:
        return []

    result = data.get("result", {})
    hits_obj = result.get("hits", {})
    hit_list = hits_obj.get("hit", [])
    if not isinstance(hit_list, list):
        return []

    papers: list[Paper] = []
    for hit in hit_list:
        try:
            paper = _parse_dblp_hit(hit)
            # Post-query year filtering
            if year_min > 0 and paper.year > 0 and paper.year < year_min:
                continue
            papers.append(paper)
            if len(papers) >= limit:
                break
        except Exception:  # noqa: BLE001
            hit_id = hit.get("info", {}).get("key", "?") if isinstance(hit, dict) else repr(hit)
            logger.debug("Failed to parse DBLP hit: %s", hit_id)
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
                        "[rate-limit] DBLP Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] DBLP 429 (Retry-After: %s). "
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
                    "DBLP HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("DBLP HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "DBLP request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("DBLP request exhausted retries for: %s", url)
    return None


def _parse_dblp_hit(hit: dict[str, Any]) -> Paper:
    """Convert a single DBLP hit JSON to a ``Paper``."""
    info = hit.get("info", {})

    # Paper key as ID (e.g. "journals/corr/abs-2301-00001")
    paper_id = str(info.get("key", "")).strip()
    if not paper_id:
        paper_id = f"dblp-{id(hit)}"

    # Title -- strip HTML tags that DBLP sometimes includes
    raw_title = str(info.get("title", "")).strip()
    title = _HTML_TAG_RE.sub("", raw_title).strip()
    # Remove trailing period that DBLP sometimes appends
    if title.endswith("."):
        title = title[:-1]

    # Authors -- can be a single string, a dict, or a list of strings/dicts
    authors = _parse_dblp_authors(info.get("authors", {}))

    # Year
    year = 0
    raw_year = info.get("year")
    if raw_year is not None:
        try:
            year = int(raw_year)
        except (ValueError, TypeError):
            pass

    # Venue -- can be a string or a list
    raw_venue = info.get("venue", "")
    if isinstance(raw_venue, list):
        venue = ", ".join(str(v) for v in raw_venue)
    else:
        venue = str(raw_venue).strip()

    # DOI
    doi = str(info.get("doi", "")).strip()

    # URL (electronic edition)
    raw_url = info.get("ee", "")
    if isinstance(raw_url, list):
        url = str(raw_url[0]).strip() if raw_url else ""
    else:
        url = str(raw_url).strip()

    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        abstract="",  # DBLP does not provide abstracts
        venue=venue,
        citation_count=0,  # DBLP does not provide citation counts
        doi=doi,
        url=url,
        source="dblp",
    )


def _parse_dblp_authors(authors_obj: Any) -> tuple[Author, ...]:
    """Parse DBLP's variable author format.

    The ``authors.author`` field can be:
    - A single string: ``"John Smith"``
    - A single dict: ``{"text": "John Smith", "@pid": "..."}``
    - A list of strings or dicts
    """
    if not authors_obj:
        return ()

    raw = authors_obj.get("author", []) if isinstance(authors_obj, dict) else []
    if not raw:
        return ()

    # Normalise to list
    if not isinstance(raw, list):
        raw = [raw]

    result: list[Author] = []
    for entry in raw:
        if isinstance(entry, str):
            name = entry.strip()
        elif isinstance(entry, dict):
            name = str(entry.get("text", "")).strip()
        else:
            continue
        if name:
            result.append(Author(name=name))
    return tuple(result)
