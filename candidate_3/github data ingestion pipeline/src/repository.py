"""
Data access layer - the ONLY place SQL is written.

Every function here takes/returns plain Python objects or the Pydantic
models from schema.py - never a raw DB row crosses into api.py. All
queries are parameterized (psycopg's %s placeholders) - never an
f-string near SQL.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from psycopg import errors as psycopg_errors
from psycopg.rows import dict_row
from psycopg.types.json import Json

from src.db import get_connection
from src.schema import QuarantineRecord, Repo


class RunAlreadyInProgressError(Exception):
    """Raised when a run is requested for an org that already has one running."""


def new_run_id() -> str:
    return str(uuid.uuid4())


# --- pipeline_runs ------------------------------------------------------


def create_pipeline_run(org: str) -> str:
    """
    Insert a new 'running' row for this org. Relies on the partial unique
    index (one_running_run_per_org) to guarantee only one running run per
    org exists at a time - if that index rejects us, we translate it into
    a clear RunAlreadyInProgressError rather than leaking the raw DB error.
    """
    run_id = new_run_id()
    with get_connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO pipeline_runs (run_id, org, status) "
                    "VALUES (%s, %s, 'running')",
                    (run_id, org),
                )
            conn.commit()
        except psycopg_errors.UniqueViolation as exc:
            conn.rollback()
            raise RunAlreadyInProgressError(
                f"A run is already in progress for org '{org}'"
            ) from exc
    return run_id


def finish_pipeline_run(
    run_id: str,
    records_fetched: int,
    records_valid: int,
    records_quarantined: int,
    status: str,
) -> None:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE pipeline_runs
                SET finished_at = %s, records_fetched = %s,
                    records_valid = %s, records_quarantined = %s, status = %s
                WHERE run_id = %s
                """,
                (
                    datetime.now(timezone.utc),
                    records_fetched,
                    records_valid,
                    records_quarantined,
                    status,
                    run_id,
                ),
            )
        conn.commit()


def get_run(run_id: str) -> dict[str, Any] | None:
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM pipeline_runs WHERE run_id = %s", (run_id,))
            return cur.fetchone()


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM pipeline_runs ORDER BY started_at DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()


def is_run_in_progress(org: str) -> bool:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pipeline_runs WHERE org = %s AND status = 'running'",
                (org,),
            )
            return cur.fetchone() is not None


# --- github_repos ---------------------------------------------------------


def upsert_repo(repo: Repo, organization: str, run_id: str) -> None:
    """
    INSERT ... ON CONFLICT (id) DO UPDATE - idempotent by design. Running
    this twice with the same repo id always ends with exactly one row,
    holding whatever values were passed most recently.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO github_repos (
                    id, organization, name, full_name, private, html_url,
                    description, stargazers_count, forks_count,
                    open_issues_count, archived, created_at, updated_at,
                    pushed_at, flags, last_run_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    organization = EXCLUDED.organization,
                    name = EXCLUDED.name,
                    full_name = EXCLUDED.full_name,
                    private = EXCLUDED.private,
                    html_url = EXCLUDED.html_url,
                    description = EXCLUDED.description,
                    stargazers_count = EXCLUDED.stargazers_count,
                    forks_count = EXCLUDED.forks_count,
                    open_issues_count = EXCLUDED.open_issues_count,
                    archived = EXCLUDED.archived,
                    created_at = EXCLUDED.created_at,
                    updated_at = EXCLUDED.updated_at,
                    pushed_at = EXCLUDED.pushed_at,
                    flags = EXCLUDED.flags,
                    last_run_id = EXCLUDED.last_run_id,
                    fetched_at = now()
                """,
                (
                    repo.id, organization, repo.name, repo.full_name,
                    repo.private, repo.html_url, repo.description,
                    repo.stargazers_count, repo.forks_count,
                    repo.open_issues_count, repo.archived, repo.created_at,
                    repo.updated_at, repo.pushed_at, repo.flags, run_id,
                ),
            )
        conn.commit()


def upsert_repos(repos: list[Repo], organization: str, run_id: str) -> None:
    for repo in repos:
        upsert_repo(repo, organization, run_id)


def get_repos_for_org(organization: str) -> list[dict[str, Any]]:
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM github_repos WHERE organization = %s", (organization,)
            )
            return cur.fetchall()


# --- github_repos_quarantine ----------------------------------------------


def insert_quarantine_record(record: QuarantineRecord, run_id: str) -> None:
    """
    UPSERT keyed on dedup_key - re-quarantining the same bad record across
    repeated runs updates the existing row (fresh run_id, error, timestamp)
    instead of piling up duplicates.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO github_repos_quarantine (
                    dedup_key, run_id, organization, raw_payload,
                    failed_field, error_message, quarantined_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (dedup_key) DO UPDATE SET
                    run_id = EXCLUDED.run_id,
                    error_message = EXCLUDED.error_message,
                    quarantined_at = EXCLUDED.quarantined_at
                """,
                (
                    record.dedup_key, run_id, record.organization,
                    Json(record.raw_payload), record.failed_field,
                    record.error_message, record.quarantined_at,
                ),
            )
        conn.commit()


def insert_quarantine_records(records: list[QuarantineRecord], run_id: str) -> None:
    for record in records:
        insert_quarantine_record(record, run_id)


def get_quarantine(
    organization: str, page: int = 1, page_size: int = 20
) -> tuple[list[dict[str, Any]], int]:
    """Returns (rows, total_count) for the given page."""
    offset = (page - 1) * page_size
    with get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT count(*) AS total FROM github_repos_quarantine WHERE organization = %s",
                (organization,),
            )
            count_row = cur.fetchone()
            # COUNT(*) always returns exactly one row, so count_row is
            # never actually None at runtime - but the cursor's return
            # type is Optional, so we check explicitly rather than
            # asserting past it. This keeps the type checker (and anyone
            # reading this later) honest about what fetchone() can return.
            total = count_row["total"] if count_row is not None else 0

            cur.execute(
                """
                SELECT * FROM github_repos_quarantine
                WHERE organization = %s
                ORDER BY quarantined_at DESC
                LIMIT %s OFFSET %s
                """,
                (organization, page_size, offset),
            )
            rows = cur.fetchall()
    return rows, total