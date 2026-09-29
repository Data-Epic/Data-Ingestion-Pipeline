"""Field-level and cross-field validation, per the test brief:
- a fully valid record passes
- a record missing a required field is rejected with a specific error
- a record with a wrong type is rejected
"""

from tests.conftest import sample_raw_repo
from src.schema import Repo, QuarantineRecord, validate_repo


def test_valid_record_passes():
    result = validate_repo(sample_raw_repo(), organization="org")
    assert isinstance(result, Repo)
    assert result.id == 1


def test_missing_required_field_rejected():
    raw = sample_raw_repo()
    del raw["name"]
    result = validate_repo(raw, organization="org")
    assert isinstance(result, QuarantineRecord)
    assert result.failed_field == "name"
    assert result.raw_payload == raw


def test_wrong_type_rejected():
    raw = sample_raw_repo(stargazers_count="not-a-number")
    result = validate_repo(raw, organization="org")
    assert isinstance(result, QuarantineRecord)
    assert result.failed_field == "stargazers_count"


def test_negative_count_rejected():
    raw = sample_raw_repo(forks_count=-3)
    result = validate_repo(raw, organization="org")
    assert isinstance(result, QuarantineRecord)
    assert result.failed_field == "forks_count"


def test_pushed_before_created_rejected():
    raw = sample_raw_repo(created_at="2020-06-01T00:00:00Z", pushed_at="2020-01-01T00:00:00Z")
    result = validate_repo(raw, organization="org")
    assert isinstance(result, QuarantineRecord)
    assert result.failed_field == "pushed_at"


def test_archived_recently_pushed_is_flagged_not_quarantined():
    from datetime import datetime, timezone
    raw = sample_raw_repo(archived=True, pushed_at=datetime.now(timezone.utc).isoformat())
    result = validate_repo(raw, organization="org")
    assert isinstance(result, Repo)
    assert "archived_but_recently_pushed" in result.flags


def test_quarantine_dedup_key_uses_id_when_present():
    raw = sample_raw_repo(repo_id=7, name="")
    result = validate_repo(raw, organization="acme")
    assert isinstance(result, QuarantineRecord)
    assert result.dedup_key == "acme:7:name"


def test_quarantine_dedup_key_falls_back_without_id():
    raw = sample_raw_repo()
    del raw["id"]
    del raw["name"]
    result = validate_repo(raw, organization="acme")
    assert isinstance(result, QuarantineRecord)
    assert result.dedup_key.startswith("acme:")
    assert result.dedup_key != "acme:None:name"