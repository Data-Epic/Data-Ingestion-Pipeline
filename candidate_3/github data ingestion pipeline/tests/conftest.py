"""
Shared fixtures. Uses a REAL Postgres test database (not sqlite, not
mocks) so constraints, upserts, and foreign keys are exercised exactly as
they behave in production. Each test truncates the tables it touches via
the `clean_db` fixture, rather than the suite relying on execution order.
"""

import os
import pytest

# Point the app at the test DB before any src module (which reads
# settings at import/call time) gets imported.
os.environ.setdefault(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/github_ingestion_test"
)

from src.db import get_connection, close_pool  # noqa: E402


@pytest.fixture(autouse=True)
def clean_db():
    """Truncate all tables before every test, so tests don't depend on order."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "TRUNCATE github_repos_quarantine, github_repos, pipeline_runs CASCADE"
            )
        conn.commit()
    yield


@pytest.fixture(scope="session", autouse=True)
def _close_pool_at_end():
    yield
    close_pool()


def sample_raw_repo(repo_id: int = 1, **overrides) -> dict:
    """A minimal, valid raw GitHub repo payload for tests to mutate."""
    base = {
        "id": repo_id,
        "name": f"repo-{repo_id}",
        "full_name": f"org/repo-{repo_id}",
        "private": False,
        "html_url": f"https://github.com/org/repo-{repo_id}",
        "description": None,
        "stargazers_count": 5,
        "forks_count": 1,
        "open_issues_count": 0,
        "archived": False,
        "created_at": "2020-01-01T00:00:00Z",
        "updated_at": "2020-01-02T00:00:00Z",
        "pushed_at": "2020-01-02T00:00:00Z",
    }
    base.update(overrides)
    return base