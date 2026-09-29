"""
Computes the 5 data-quality dimensions for GET /quality-report/{org}.
Always computed LIVE from github_repos / pipeline_runs - never cached.
"""

import random
from typing import Any

from src.client import GitHubClient
from src.repository import get_repos_for_org, list_runs

REQUIRED_FIELDS = [
    "id", "name", "full_name", "private", "html_url",
    "stargazers_count", "forks_count", "open_issues_count",
    "archived", "created_at", "updated_at", "pushed_at",
]

ACCURACY_SAMPLE_SIZE = 5


def compute_completeness(repos: list[dict[str, Any]]) -> dict[str, Any]:
    """Non-null rate per required field, description reported separately
    since it's nullable-by-design rather than a defect."""
    total = len(repos)
    if total == 0:
        return {"total_records": 0, "field_rates": {}, "description_non_null_rate": None}

    field_rates = {
        field: sum(1 for r in repos if r.get(field) is not None) / total
        for field in REQUIRED_FIELDS
    }
    description_rate = sum(1 for r in repos if r.get("description") is not None) / total
    return {
        "total_records": total,
        "field_rates": field_rates,
        "description_non_null_rate": description_rate,
    }


def compute_validity(org: str) -> dict[str, Any]:
    """% of fetched records that passed validation, from the most recent
    finished run for this org."""
    runs = [r for r in list_runs(limit=100) if r["org"] == org and r["status"] != "running"]
    if not runs:
        return {"records_fetched": 0, "records_valid": 0, "validity_rate": None}
    latest = runs[0]  # list_runs already orders most-recent-first
    fetched = latest["records_fetched"]
    valid = latest["records_valid"]
    return {
        "records_fetched": fetched,
        "records_valid": valid,
        "validity_rate": (valid / fetched) if fetched else None,
    }


def compute_uniqueness(repos: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Duplicate id count. Structurally this should always be 0, since
    github_repos.id is a PRIMARY KEY - Postgres itself refuses a second
    row with the same id. This check exists as a live sanity confirmation
    of that guarantee, not as the primary enforcement mechanism.
    """
    ids = [r["id"] for r in repos]
    duplicate_count = len(ids) - len(set(ids))
    return {"total_records": len(ids), "duplicate_id_count": duplicate_count}


def compute_consistency(repos: list[dict[str, Any]]) -> dict[str, Any]:
    """% of records where pushed_at >= created_at holds."""
    total = len(repos)
    if total == 0:
        return {"total_records": 0, "consistent_count": 0, "consistency_rate": None}
    consistent = sum(1 for r in repos if r["pushed_at"] >= r["created_at"])
    return {
        "total_records": total,
        "consistent_count": consistent,
        "consistency_rate": consistent / total,
    }


async def compute_accuracy(
    org: str, repos: list[dict[str, Any]], client: GitHubClient | None = None
) -> dict[str, Any]:
    """
    Sample up to ACCURACY_SAMPLE_SIZE stored repos, re-fetch each live via
    GET /repos/{owner}/{repo}, and compare key fields. Reports the match
    rate across the sample.
    """
    if not repos:
        return {"sample_size": 0, "matches": 0, "accuracy_rate": None}

    client = client or GitHubClient()
    sample = random.sample(repos, min(ACCURACY_SAMPLE_SIZE, len(repos)))

    matches = 0
    checked = 0
    for stored in sample:
        try:
            live = await client.fetch_single_repo(org, stored["name"])
        except Exception:
            # Can't reach GitHub for this one - skip rather than count as
            # a mismatch; it's a network issue, not a data defect.
            continue
        checked += 1
        if (
            live.get("stargazers_count") == stored["stargazers_count"]
            and live.get("forks_count") == stored["forks_count"]
            and live.get("description") == stored["description"]
        ):
            matches += 1

    return {
        "sample_size": checked,
        "matches": matches,
        "accuracy_rate": (matches / checked) if checked else None,
    }


async def compute_quality_report(
    org: str, client: GitHubClient | None = None
) -> dict[str, Any]:
    repos = get_repos_for_org(org)
    return {
        "organization": org,
        "completeness": compute_completeness(repos),
        "validity": compute_validity(org),
        "uniqueness": compute_uniqueness(repos),
        "consistency": compute_consistency(repos),
        "accuracy": await compute_accuracy(org, repos, client),
    }