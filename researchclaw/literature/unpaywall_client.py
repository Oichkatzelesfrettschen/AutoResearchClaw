"""Unpaywall API client.

Uses stdlib ``urllib`` + ``json`` -- zero extra dependencies.

Public API
----------
- ``resolve_oa(doi, email)`` -> ``dict | None``
- ``batch_resolve_oa(dois, email)`` -> ``list[dict | None]``

Unpaywall is a DOI resolver for open-access locations, not a search engine.
Given a DOI, it returns the best open-access URL if one exists.

Rate limits:
  - 100,000 requests per day
  - We enforce 0.1s between requests.
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

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.unpaywall.org/v2"
_DEFAULT_EMAIL = "researchclaw@example.com"
_USER_AGENT = "ResearchClaw/0.5.0 (mailto:researchclaw@example.com)"
_RATE_LIMIT_SEC = 0.1
_MAX_RETRIES = 3
_MAX_WAIT_SEC = 60
_TIMEOUT_SEC = 15

_last_request_time: float = 0.0


def resolve_oa(
    doi: str,
    email: str = _DEFAULT_EMAIL,
) -> dict[str, Any] | None:
    """Resolve open-access location for a single DOI.

    Parameters
    ----------
    doi:
        The DOI to resolve (e.g. "10.1234/example").
    email:
        Required email for Unpaywall API access.

    Returns
    -------
    dict | None
        Dictionary with keys: ``doi``, ``title``, ``is_oa``,
        ``best_oa_url``, ``oa_status``, ``journal_name``.
        None on network failure or unknown DOI.
    """
    global _last_request_time  # noqa: PLW0603

    now = time.monotonic()
    elapsed = now - _last_request_time
    if elapsed < _RATE_LIMIT_SEC:
        time.sleep(_RATE_LIMIT_SEC - elapsed)

    doi = doi.strip()
    if not doi:
        return None

    # Encode DOI in URL path (DOIs can contain special characters)
    encoded_doi = urllib.parse.quote(doi, safe="")
    params = urllib.parse.urlencode({"email": email})
    url = f"{_BASE_URL}/{encoded_doi}?{params}"

    _last_request_time = time.monotonic()
    data = _request_with_retry(url)
    if data is None:
        return None

    return _parse_unpaywall_response(data)


def batch_resolve_oa(
    dois: list[str],
    email: str = _DEFAULT_EMAIL,
) -> list[dict[str, Any] | None]:
    """Resolve open-access locations for multiple DOIs.

    Parameters
    ----------
    dois:
        List of DOIs to resolve.
    email:
        Required email for Unpaywall API access.

    Returns
    -------
    list[dict | None]
        One result per DOI, in the same order.  None for failures.
    """
    results: list[dict[str, Any] | None] = []
    for doi in dois:
        results.append(resolve_oa(doi, email=email))
    return results


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
            if exc.code == 404:
                logger.debug("Unpaywall: DOI not found at %s", url)
                return None

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
                        "[rate-limit] Unpaywall Retry-After=%s (>300s). Skipping.",
                        retry_after,
                    )
                    return None
                wait = min(wait, _MAX_WAIT_SEC)
                jitter = random.uniform(0, wait * 0.2)
                logger.warning(
                    "[rate-limit] Unpaywall 429. Waiting %.1fs (attempt %d/%d)...",
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
                    "Unpaywall HTTP %d. Retry %d/%d in %.0fs...",
                    exc.code,
                    attempt + 1,
                    _MAX_RETRIES,
                    wait + jitter,
                )
                time.sleep(wait + jitter)
                continue

            logger.warning("Unpaywall HTTP %d for %s", exc.code, url)
            return None

        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            wait = min(2**attempt, _MAX_WAIT_SEC)
            jitter = random.uniform(0, wait * 0.2)
            logger.warning(
                "Unpaywall request failed (%s). Retry %d/%d in %ds...",
                exc,
                attempt + 1,
                _MAX_RETRIES,
                wait,
            )
            time.sleep(wait + jitter)

    logger.error("Unpaywall request exhausted retries for: %s", url)
    return None


def _parse_unpaywall_response(data: dict[str, Any]) -> dict[str, Any]:
    """Extract relevant fields from Unpaywall response."""
    best_oa = data.get("best_oa_location") or {}
    best_oa_url = str(best_oa.get("url") or best_oa.get("url_for_pdf") or "").strip()

    return {
        "doi": str(data.get("doi") or "").strip(),
        "title": str(data.get("title") or "").strip(),
        "is_oa": bool(data.get("is_oa")),
        "best_oa_url": best_oa_url,
        "oa_status": str(data.get("oa_status") or "").strip(),
        "journal_name": str(data.get("journal_name") or "").strip(),
    }
