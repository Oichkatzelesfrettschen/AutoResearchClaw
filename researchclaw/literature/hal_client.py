"""HAL (Hyper Articles en Ligne) API client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``search_hal(query, limit, year_min)`` -> ``list[Paper]``

Rate limits:
  - No published hard limit.
  - We enforce 1s between requests out of courtesy.

HAL is the French national open archive for scholarly publications.
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

_BASE_URL = "https://api.archives-ouvertes.fr/search/"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 1.0
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 20

# Fields to retrieve from HAL
_FL = ",".join([
    "title_s",
    "authFullName_s",
    "doiId_s",
    "abstract_s",
    "producedDateY_i",
    "uri_s",
    "halId_s",
    "journalTitle_s",
    "citationRef_s",
])

_last_request_time: float = 0.0


def search_hal(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
) -> list[Paper]:
    """Search HAL for papers matching *query*.

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

    params: dict[str, str] = {
        "q": query,
        "rows": str(limit),
        "wt": "json",
        "fl": _FL,
    }
    if year_min > 0:
        params["fq"] = f"producedDateY_i:[{year_min} TO *]"

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    _last_request_time = time.monotonic()
    data = _request_with_retry(url)
    if data is None:
        return []

    response = data.get("response", {})
    docs = response.get("docs", [])
    if not isinstance(docs, list):
        return []

    papers: list[Paper] = []
    for doc in docs:
        try:
            papers.append(_parse_hal_doc(doc))
        except Exception:  # noqa: BLE001
            logger.debug(
                "Failed to parse HAL doc: %s", doc.get("halId_s", "?")
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
                        "[rate-limit] HAL Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] HAL 429. Waiting %.1fs (attempt %d/%d)...",
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
                    "HAL HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("HAL HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "HAL request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("HAL request exhausted retries for: %s", url)
    return None


def _parse_hal_doc(doc: dict[str, Any]) -> Paper:
    """Convert a single HAL Solr document to a ``Paper``."""
    # Title: HAL returns title_s as list of strings (one per language)
    raw_title = doc.get("title_s") or []
    if isinstance(raw_title, list):
        title = str(raw_title[0]).strip() if raw_title else ""
    else:
        title = str(raw_title).strip()

    # Authors: authFullName_s is a list of strings
    raw_authors = doc.get("authFullName_s") or []
    if isinstance(raw_authors, str):
        raw_authors = [raw_authors]
    authors = tuple(
        Author(name=str(a).strip())
        for a in raw_authors
        if isinstance(a, str) and a.strip()
    )

    year = int(doc.get("producedDateY_i") or 0)

    # Abstract: may be list (multi-language) or string
    raw_abstract = doc.get("abstract_s") or ""
    if isinstance(raw_abstract, list):
        abstract = str(raw_abstract[0]).strip() if raw_abstract else ""
    else:
        abstract = str(raw_abstract).strip()

    doi = str(doc.get("doiId_s") or "").strip()

    # URL: prefer uri_s
    uri = doc.get("uri_s") or ""
    if isinstance(uri, list):
        url = str(uri[0]).strip() if uri else ""
    else:
        url = str(uri).strip()
    if not url and doi:
        url = f"https://doi.org/{doi}"

    hal_id = str(doc.get("halId_s") or "").strip()
    paper_id = f"hal-{hal_id}" if hal_id else f"hal-{title[:30]}"

    # Venue
    venue = str(doc.get("journalTitle_s") or "").strip()

    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        abstract=abstract,
        venue=venue,
        doi=doi,
        url=url,
        source="hal",
    )
