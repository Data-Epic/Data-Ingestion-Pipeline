"""
Repository-layer coverage against a REAL Postgres test DB, per the brief:
- a new record inserts successfully
- re-inserting the same id upserts rather than duplicating
- a quarantined record persists with its raw payload and error metadata
- a malformed foreign-key reference is rejected by the DB, not silently accepted
- a pipeline_runs row reflects fetched/valid/quarantined counts correctly
"""

import pytest
from psycopg import errors as psycopg_errors

from src.db import get_connection
from src.repository import (
    RunAlreadyInProgressError,
    create_pipeline_run,
    finish_pipeline_run,
    get_quarantine,
    get_repos_for_org,
    get_run,
    insert_quarantine_record,
    is_run_in_progress,
    upsert_repo,
)
from src.schema import QuarantineRecord, Repo
from tests.conftest import sample_raw_repo


def _repo(**overrides) -> Repo:
    return Repo(**sample_raw_repo(**overrides))


def test_new_record_inserts_successfully():
    run_id = create_pipeline_run("org1")
    upsert_repo(_repo(repo_id=1), "org1", run_id)
    repos = get_repos_for_org("org1")
    assert len(repos) == 1
    assert repos[0]["id"] == 1


def test_reinsert_same_id_upserts_not_duplicates():
    run_id = create_pipeline_run("org1")
    upsert_repo(_repo(repo_id=1, stargazers_count=5), "org1", run_id)
    upsert_repo(_repo(repo_id=1, stargazers_count=500), "org1", run_id)
    repos = get_repos_for_org("org1")
    assert len(repos) == 1
    assert repos[0]["stargazers_count"] == 500


def test_quarantined_record_persists_with_payload_and_metadata():
    run_id = create_pipeline_run("org1")
    raw = sample_raw_repo(repo_id=2, name="")
    record = QuarantineRecord(
        raw_payload=raw, organization="org1",
        failed_field="name", error_message="must not be empty",
    )
    insert_quarantine_record(record, run_id)
    rows, total = get_quarantine("org1")
    assert total == 1
    assert rows[0]["failed_field"] == "name"
    assert rows[0]["error_message"] == "must not be empty"
    assert rows[0]["raw_payload"]["id"] == 2


def test_malformed_foreign_key_rejected_by_constraint():
    with get_connection() as conn:
        with pytest.raises(psycopg_errors.ForeignKeyViolation):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO github_repos (
                        id, organization, name, full_name, private, html_url,
                        stargazers_count, forks_count, open_issues_count,
                        archived, created_at, updated_at, pushed_at, last_run_id
                    ) VALUES (
                        1, 'org1', 'x', 'org1/x', false, 'http://x',
                        0, 0, 0, false, '2020-01-01', '2020-01-02', '2020-01-02',
                        'run-id-that-does-not-exist'
                    )
                    """
                )
        conn.rollback()


def test_pipeline_run_reflects_counts():
    run_id = create_pipeline_run("org1")
    finish_pipeline_run(
        run_id, records_fetched=10, records_valid=7, records_quarantined=3, status="success"
    )
    run = get_run(run_id)
    assert run["records_fetched"] == 10
    assert run["records_valid"] == 7
    assert run["records_quarantined"] == 3
    assert run["status"] == "success"


def test_duplicate_running_run_rejected():
    create_pipeline_run("org1")
    with pytest.raises(RunAlreadyInProgressError):
        create_pipeline_run("org1")


def test_get_run_returns_none_for_unknown_id():
    assert get_run("does-not-exist") is None


def test_is_run_in_progress_reflects_status():
    run_id = create_pipeline_run("org1")
    assert is_run_in_progress("org1") is True
    finish_pipeline_run(run_id, 1, 1, 0, "success")
    assert is_run_in_progress("org1") is False


def test_quarantine_pagination():
    run_id = create_pipeline_run("org1")
    for i in range(5):
        record = QuarantineRecord(
            raw_payload=sample_raw_repo(repo_id=i), organization="org1",
            failed_field="name", error_message=f"error {i}",
        )
        insert_quarantine_record(record, run_id)
    rows, total = get_quarantine("org1", page=1, page_size=2)
    assert total == 5
    assert len(rows) == 2