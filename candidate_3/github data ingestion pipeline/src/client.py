"""
GitHub API client.

Responsibilities (and ONLY these - no validation, no DB writes here):
1. Authenticate with GITHUB_TOKEN if present; log a warning and continue
   unauthenticated if not.
2. GET /orgs/{org}/repos?per_page=100, following the `Link: rel="next"`
   response header until there is no next page. Never hardcode a page count.
3. Before returning parsed data to the caller, write each raw page response
   to data/raw/{org}/{run_timestamp}_page{N}.json UNTOUCHED - this is the
   audit trail, so it must happen before any parsing/validation.
4. Retry transient failures (timeouts, connection drops, 5xx) with
   exponential backoff, up to a max attempt count.
5. Check /rate_limit before a batch run; during a run, watch
   X-RateLimit-Remaining / X-RateLimit-Reset and either wait until reset
   or fail fast with a clear log message - pick one behavior and be
   consistent, since the tests assert on it.
6. Handle malformed/truncated JSON bodies as a caught, logged failure -
   never let it bubble up as an unhandled exception.

Why async (httpx.AsyncClient):
- FastAPI's POST /ingest/{org} must return the run_id immediately and do
  the real work in the background. An async client lets that background
  task `await` each HTTP call instead of blocking a worker thread, so the
  rest of the API stays responsive while a run is in progress.

TODO (next pass):
- class GitHubClient:
    def __init__(self, token, timeouts, max_retries)
    async def check_rate_limit(self) -> RateLimitStatus
    async def fetch_org_repos(self, org: str, run_timestamp: str) -> list[dict]
        # handles pagination + raw dump + retries internally
    async def fetch_single_repo(self, owner: str, repo: str) -> dict
        # used later by the accuracy-sampling part of the quality engine
- A small internal `_request_with_backoff()` helper so retry logic isn't
  duplicated across the three public methods above.
"""

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import httpx

from src.config import get_settings

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
RAW_DATA_DIR = Path("data/raw")

# If a rate-limit reset is further out than this, fail fast instead of
# blocking. 15 minutes is a deliberate middle ground: long enough to ride
# out a short secondary limit, short enough not to wedge a run.
MAX_RATE_LIMIT_WAIT_SECONDS = 900


# --- exceptions -------------------------------------------------------


class GitHubClientError(Exception):
    """Base for all client errors."""


class RateLimitError(GitHubClientError):
    """Rate limited, and the reset is too far out to be worth waiting."""


class MalformedResponseError(GitHubClientError):
    """A response body was not valid JSON."""


class _RateLimitRetry(Exception):
    """Internal signal: rate limited, but close enough to wait it out."""

    def __init__(self, wait_seconds: int) -> None:
        self.wait_seconds = wait_seconds
        super().__init__(f"rate limited, retry in {wait_seconds}s")


# --- helpers ----------------------------------------------------------


def parse_next_link(link_header: str | None) -> str | None:
    """
    Extract the rel="next" URL from a Link header, or None if absent.

    GitHub sends something like:
        <https://api.github.com/...page=2>; rel="next",
        <https://api.github.com/...page=9>; rel="last"

    We follow ONLY rel="next" and stop when it disappears - that's how we
    avoid hardcoding any page count.
    """
    if not link_header:
        return None
    for part in link_header.split(","):
        match = re.search(r'<([^>]+)>;\s*rel="next"', part.strip())
        if match:
            return match.group(1)
    return None


def check_rate_limit_headers(response: httpx.Response) -> None:
    """
    Inspect X-RateLimit-* headers on a response.

    Raises _RateLimitRetry if we're out of quota but the reset is near
    (caller should sleep and retry), or RateLimitError if the reset is
    too far out (caller should give up cleanly).
    """
    if response.headers.get("X-RateLimit-Remaining") != "0":
        return

    reset_raw = response.headers.get("X-RateLimit-Reset")
    if reset_raw is None:
        raise RateLimitError("Rate limit exhausted and no reset time provided")

    try:
        wait_seconds = max(0, int(reset_raw) - int(time.time()))
    except ValueError as exc:
        raise RateLimitError(f"Unparseable X-RateLimit-Reset: {reset_raw!r}") from exc

    if wait_seconds > MAX_RATE_LIMIT_WAIT_SECONDS:
        raise RateLimitError(
            f"Rate limit exhausted; resets in {wait_seconds}s "
            f"(cap {MAX_RATE_LIMIT_WAIT_SECONDS}s) - failing fast"
        )
    raise _RateLimitRetry(wait_seconds)


def write_raw_page(org: str, run_timestamp: str, page_num: int, body: str) -> Path:
    """
    Persist an unmodified page body to
    data/raw/{org}/{run_timestamp}_page{N}.json.

    Called BEFORE any parsing, so even a malformed response is captured
    on disk for later inspection.
    """
    org_dir = RAW_DATA_DIR / org
    org_dir.mkdir(parents=True, exist_ok=True)
    path = org_dir / f"{run_timestamp}_page{page_num}.json"
    path.write_text(body, encoding="utf-8")
    return path


# --- client -----------------------------------------------------------


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        connect_timeout: float | None = None,
        read_timeout: float | None = None,
        max_retries: int | None = None,
        backoff_base: float | None = None,
    ) -> None:
        settings = get_settings()
        self.token = token if token is not None else settings.github_token
        self.max_retries = max_retries or settings.max_retries
        self.backoff_base = backoff_base or settings.backoff_base_seconds

        if not self.token:
            logger.warning(
                "No GITHUB_TOKEN set - proceeding unauthenticated. "
                "Rate limits are much lower (60/hr vs 5000/hr)."
            )

        # Explicit connect AND read timeouts on every request, per spec.
        self.timeout = httpx.Timeout(
            connect=connect_timeout or settings.request_connect_timeout,
            read=read_timeout or settings.request_read_timeout,
            write=10.0,
            pool=10.0,
        )

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    async def _request_with_backoff(
        self, client: httpx.AsyncClient, url: str
    ) -> httpx.Response:
        """
        GET a URL, retrying transient failures with exponential backoff.

        Retries on: timeouts, connection errors, 5xx, and near-reset rate
        limits. Does NOT retry on 4xx (other than rate limiting), since
        those won't fix themselves.
        """
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = await client.get(
                    url, headers=self._headers(), timeout=self.timeout
                )

                # Rate limiting can arrive as 403 or 429; the headers are
                # the authoritative signal, so check them either way.
                if response.status_code in (403, 429):
                    check_rate_limit_headers(response)

                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"server error {response.status_code}",
                        request=response.request,
                        response=response,
                    )

                response.raise_for_status()
                return response

            except _RateLimitRetry as exc:
                last_error = exc
                logger.warning(
                    "Rate limited on %s; sleeping %ss before retry",
                    url,
                    exc.wait_seconds,
                )
                await asyncio.sleep(exc.wait_seconds)

            except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                # 4xx that isn't rate limiting: don't retry, it won't heal
                if isinstance(exc, httpx.HTTPStatusError):
                    status = exc.response.status_code
                    if 400 <= status < 500 and status not in (403, 429):
                        raise GitHubClientError(
                            f"Non-retryable {status} for {url}"
                        ) from exc

                last_error = exc
                if attempt < self.max_retries:
                    delay = self.backoff_base * (2**attempt)
                    logger.warning(
                        "Request to %s failed (%s), retry %d/%d in %.1fs",
                        url,
                        type(exc).__name__,
                        attempt + 1,
                        self.max_retries,
                        delay,
                    )
                    await asyncio.sleep(delay)

        raise GitHubClientError(
            f"Request to {url} failed after {self.max_retries} retries: {last_error}"
        ) from last_error

    @staticmethod
    def _parse_json(body: str, url: str) -> Any:
        """Parse a body, converting JSON errors into a caught, logged failure."""
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            logger.error("Malformed JSON from %s: %s", url, exc)
            raise MalformedResponseError(f"Malformed JSON from {url}: {exc}") from exc

    async def check_rate_limit(self) -> dict[str, Any]:
        """Query /rate_limit - used as a pre-flight check before a batch run."""
        async with httpx.AsyncClient() as client:
            response = await self._request_with_backoff(
                client, f"{GITHUB_API_BASE}/rate_limit"
            )
            return self._parse_json(response.text, "/rate_limit")

    async def fetch_org_repos(
        self, org: str, run_timestamp: str
    ) -> list[dict[str, Any]]:
        """
        Fetch every repo for an org, following Link: rel="next" until
        exhausted. Each page's raw body is written to disk BEFORE parsing.

        Returns a flat list of raw repo dicts - unvalidated. Validation is
        schema.py's job, not this my file or block of code.
        """
        url: str | None = f"{GITHUB_API_BASE}/orgs/{org}/repos?per_page=100"
        all_repos: list[dict[str, Any]] = []
        page_num = 1

        async with httpx.AsyncClient() as client:
            while url:
                response = await self._request_with_backoff(client, url)

                # Raw capture FIRST - before any parsing can fail.
                write_raw_page(org, run_timestamp, page_num, response.text)

                page_data = self._parse_json(response.text, url)
                if not isinstance(page_data, list):
                    raise MalformedResponseError(
                        f"Expected a JSON array from {url}, got {type(page_data).__name__}"
                    )
                all_repos.extend(page_data)

                url = parse_next_link(response.headers.get("Link"))
                page_num += 1

        logger.info("Fetched %d repos for org %s across %d pages", len(all_repos), org, page_num - 1)
        return all_repos

    async def fetch_single_repo(self, owner: str, repo: str) -> dict[str, Any]:
        """
        Fetch one repo - used by quality.py's accuracy sampling, which
        re-checks stored records against the live API.
        """
        url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}"
        async with httpx.AsyncClient() as client:
            response = await self._request_with_backoff(client, url)
            return self._parse_json(response.text, url)