"""J-STAGE (Japan Science and Technology) API client.

Uses stdlib ``urllib`` + ``xml.etree.ElementTree`` -- zero extra dependencies.

Public API
----------
- ``search_jstage(query, limit, year_min)`` -> ``list[Paper]``

Rate limits:
  - J-STAGE does not publish formal rate limits.
  - We enforce 2s between requests as a conservative default.

Authentication:
  - No authentication required.
"""

from __future__ import annotations

import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from researchclaw.literature.models import Author, Paper

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.jstage.jst.go.jp/searchapi/do"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 2.0
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 30

_last_request_time: float = 0.0

# XML namespaces used in J-STAGE Atom responses
_NS: dict[str, str] = {
    "atom": "http://www.w3.org/2005/Atom",
    "prism": "http://prismstandard.org/namespaces/basic/2.0/",
    "opensearch": "http://a9.com/-/spec/opensearch/1.1/",
}


def search_jstage(
    query: str,
    *,
    limit: int = 20,
    year_min: int = 0,
) -> list[Paper]:
    """Search J-STAGE for papers matching *query*.

    Parameters
    ----------
    query:
        Free-text keyword search query.
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
        "service": "3",
        "keyword": query,
        "count": str(limit),
        "start": "1",
    }
    if year_min > 0:
        params["pubyearfrom"] = str(year_min)

    url = f"{_BASE_URL}?{urllib.parse.urlencode(params)}"

    _last_request_time = time.monotonic()
    xml_bytes = _request_with_retry(url)
    if xml_bytes is None:
        return []

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        logger.warning("J-STAGE XML parse error: %s", exc)
        return []

    entries = root.findall("atom:entry", _NS)
    if not entries:
        return []

    papers: list[Paper] = []
    for entry in entries:
        try:
            papers.append(_parse_jstage_entry(entry))
        except Exception:  # noqa: BLE001
            entry_id = _text(entry, "atom:id") or "?"
            logger.debug("Failed to parse J-STAGE entry: %s", entry_id)
    return papers


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------


def _request_with_retry(url: str) -> bytes | None:
    """GET *url* with exponential back-off retries.  Returns raw bytes."""
    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "Accept": "application/atom+xml, application/xml, text/xml",
                    "User-Agent": _USER_AGENT,
                },
            )
            with urllib.request.urlopen(req, timeout=_TIMEOUT_SEC) as resp:
                return resp.read()
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
                        "[rate-limit] J-STAGE Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] J-STAGE 429 (Retry-After: %s). "
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
                    "J-STAGE HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("J-STAGE HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "J-STAGE request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("J-STAGE request exhausted retries for: %s", url)
    return None


def _text(element: ET.Element, tag: str) -> str:
    """Extract text content of a child element, or empty string."""
    child = element.find(tag, _NS)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def _parse_jstage_entry(entry: ET.Element) -> Paper:
    """Convert a single J-STAGE Atom entry to a ``Paper``."""
    # Paper ID from <id>
    paper_id = _text(entry, "atom:id") or f"jstage-{id(entry)}"

    # Title
    title = _text(entry, "atom:title")

    # Authors -- multiple <author><name> elements
    author_elements = entry.findall("atom:author", _NS)
    authors = tuple(
        Author(name=_text(ae, "atom:name"))
        for ae in author_elements
        if _text(ae, "atom:name")
    )

    # Year from prism:publicationDate (format varies: "YYYY", "YYYY-MM", "YYYY-MM-DD")
    year = 0
    pub_date = _text(entry, "prism:publicationDate")
    if pub_date:
        try:
            year = int(pub_date[:4])
        except (ValueError, IndexError):
            pass

    # DOI
    doi = _text(entry, "prism:doi")

    # URL from <link rel="alternate">
    url = ""
    for link in entry.findall("atom:link", _NS):
        if link.get("rel") == "alternate":
            url = link.get("href", "").strip()
            break

    # Abstract from <summary>
    abstract = _text(entry, "atom:summary")

    # Venue from prism:publicationName
    venue = _text(entry, "prism:publicationName")

    return Paper(
        paper_id=paper_id,
        title=title,
        authors=authors,
        year=year,
        abstract=abstract,
        venue=venue,
        citation_count=0,  # J-STAGE does not provide citation counts
        doi=doi,
        url=url,
        source="jstage",
    )
