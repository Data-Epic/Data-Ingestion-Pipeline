"""
Client failure-mode coverage, per the test brief:
- multi-page pagination across >=3 mocked pages
- timeout/connection error followed by a successful retry
- a mocked 5xx triggers backoff and eventual success, or a clean failure
  after max retries
- 403/429 with X-RateLimit-Remaining: 0 triggers wait-or-fail-fast
- malformed/truncated JSON is caught and logged, not raised unhandled
"""

import time

import httpx
import pytest
import respx

from src.client import (
    GitHubClient,
    GitHubClientError,
    MalformedResponseError,
    RateLimitError,
    parse_next_link,
)

BASE = "https://api.github.com"


def _repo(i: int) -> dict:
    return {"id": i, "name": f"r{i}", "full_name": f"org/r{i}"}


def test_parse_next_link_extracts_next_only():
    header = (
        f'<{BASE}/orgs/x/repos?page=2>; rel="next", '
        f'<{BASE}/orgs/x/repos?page=9>; rel="last"'
    )
    assert parse_next_link(header) == f"{BASE}/orgs/x/repos?page=2"


def test_parse_next_link_none_when_absent():
    assert parse_next_link(None) is None
    assert parse_next_link('<...>; rel="last"') is None


@pytest.mark.asyncio
@respx.mock
async def test_pagination_across_three_pages():
    respx.get(f"{BASE}/orgs/testorg/repos?per_page=100").mock(
        return_value=httpx.Response(
            200, json=[_repo(1)],
            headers={"Link": f'<{BASE}/orgs/testorg/repos?page=2>; rel="next"'},
        )
    )
    respx.get(f"{BASE}/orgs/testorg/repos?page=2").mock(
        return_value=httpx.Response(
            200, json=[_repo(2)],
            headers={"Link": f'<{BASE}/orgs/testorg/repos?page=3>; rel="next"'},
        )
    )
    respx.get(f"{BASE}/orgs/testorg/repos?page=3").mock(
        return_value=httpx.Response(200, json=[_repo(3)])
    )
    client = GitHubClient(token="fake")
    repos = await client.fetch_org_repos("testorg", "20260101T000000")
    assert [r["id"] for r in repos] == [1, 2, 3]


@pytest.mark.asyncio
@respx.mock
async def test_timeout_then_successful_retry():
    route = respx.get(f"{BASE}/rate_limit")
    route.side_effect = [httpx.TimeoutException("timed out"), httpx.Response(200, json={"ok": True})]
    client = GitHubClient(token="fake", backoff_base=0.01)
    result = await client.check_rate_limit()
    assert result == {"ok": True}


@pytest.mark.asyncio
@respx.mock
async def test_5xx_backoff_then_success():
    route = respx.get(f"{BASE}/rate_limit")
    route.side_effect = [httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": True})]
    client = GitHubClient(token="fake", backoff_base=0.01, max_retries=3)
    result = await client.check_rate_limit()
    assert result == {"ok": True}


@pytest.mark.asyncio
@respx.mock
async def test_5xx_exhausts_retries_clean_failure():
    respx.get(f"{BASE}/rate_limit").mock(return_value=httpx.Response(500))
    client = GitHubClient(token="fake", backoff_base=0.01, max_retries=2)
    with pytest.raises(GitHubClientError):
        await client.check_rate_limit()


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_far_reset_fails_fast():
    far_future = int(time.time()) + 3600
    respx.get(f"{BASE}/rate_limit").mock(
        return_value=httpx.Response(
            403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(far_future)}
        )
    )
    client = GitHubClient(token="fake", backoff_base=0.01)
    with pytest.raises(RateLimitError):
        await client.check_rate_limit()


@pytest.mark.asyncio
@respx.mock
async def test_rate_limit_near_reset_waits_then_succeeds():
    soon = int(time.time()) + 1
    route = respx.get(f"{BASE}/rate_limit")
    route.side_effect = [
        httpx.Response(429, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(soon)}),
        httpx.Response(200, json={"ok": True}),
    ]
    client = GitHubClient(token="fake", backoff_base=0.01)
    result = await client.check_rate_limit()
    assert result == {"ok": True}


@pytest.mark.asyncio
@respx.mock
async def test_malformed_json_caught_not_raised_unhandled(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    respx.get(f"{BASE}/orgs/badorg/repos?per_page=100").mock(
        return_value=httpx.Response(200, text='[{"id": 1, "name": tru')
    )
    client = GitHubClient(token="fake")
    with pytest.raises(MalformedResponseError):
        await client.fetch_org_repos("badorg", "20260101T000000")


@pytest.mark.asyncio
@respx.mock
async def test_raw_page_written_before_parsing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    respx.get(f"{BASE}/orgs/dumporg/repos?per_page=100").mock(
        return_value=httpx.Response(200, json=[_repo(1)])
    )
    client = GitHubClient(token="fake")
    await client.fetch_org_repos("dumporg", "20260101T000000")
    dumped = tmp_path / "data" / "raw" / "dumporg" / "20260101T000000_page1.json"
    assert dumped.exists()
    assert '"id":1' in dumped.read_text() or '"id": 1' in dumped.read_text()