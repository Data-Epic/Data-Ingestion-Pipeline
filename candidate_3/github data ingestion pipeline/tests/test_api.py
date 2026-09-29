"""
API-layer coverage, per the brief:
- POST /ingest/{org} returns immediately with a run_id
- GET /runs/{run_id} returns 404 for unknown, correct payload for known
- GET /quality-report/{org} returns correctly shaped scores
- GET /quarantine/{org} returns paginated results
- invalid input returns 422, not a 500 or unhandled exception
"""

import asyncio

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import ASGITransport

from src.api import app
from src.repository import create_pipeline_run, finish_pipeline_run, upsert_repo
from src.schema import Repo
from tests.conftest import sample_raw_repo

BASE = "https://api.github.com"
client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_ingest_invalid_org_name_returns_422():
    r = client.post("/ingest/bad!org name")
    assert r.status_code == 422


@respx.mock
def test_ingest_returns_run_id_immediately():
    respx.get(f"{BASE}/orgs/quickorg/repos?per_page=100").mock(
        return_value=httpx.Response(200, json=[])
    )
    r = client.post("/ingest/quickorg")
    assert r.status_code == 202
    assert "run_id" in r.json()


@pytest.mark.asyncio
@respx.mock
async def test_concurrent_ingest_same_org_returns_409():
    async def slow_response(request):
        await asyncio.sleep(0.3)
        return httpx.Response(200, json=[])

    respx.get(f"{BASE}/orgs/raceorg/repos?per_page=100").mock(side_effect=slow_response)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        r1, r2 = await asyncio.gather(
            ac.post("/ingest/raceorg"), ac.post("/ingest/raceorg")
        )
    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [202, 409]


def test_get_run_404_for_unknown():
    r = client.get("/runs/does-not-exist")
    assert r.status_code == 404


def test_get_run_returns_known_run():
    run_id = create_pipeline_run("known-org")
    finish_pipeline_run(run_id, records_fetched=5, records_valid=4, records_quarantined=1, status="success")
    r = client.get(f"/runs/{run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["run_id"] == run_id
    assert body["records_valid"] == 4


def test_list_runs():
    create_pipeline_run("org-a")
    create_pipeline_run("org-b")
    r = client.get("/runs")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_quality_report_shape():
    run_id = create_pipeline_run("qorg")
    upsert_repo(Repo(**sample_raw_repo(repo_id=1)), "qorg", run_id)
    finish_pipeline_run(run_id, records_fetched=1, records_valid=1, records_quarantined=0, status="success")

    with respx.mock:
        respx.get(f"{BASE}/repos/qorg/repo-1").mock(
            return_value=httpx.Response(200, json={
                "stargazers_count": 5, "forks_count": 1, "description": None
            })
        )
        r = client.get("/quality-report/qorg")

    assert r.status_code == 200
    body = r.json()
    for key in ("completeness", "validity", "uniqueness", "consistency", "accuracy"):
        assert key in body


def test_quality_report_invalid_org_422():
    r = client.get("/quality-report/bad org!")
    assert r.status_code == 422


def test_quarantine_paginated():
    from src.repository import insert_quarantine_record
    from src.schema import QuarantineRecord

    run_id = create_pipeline_run("qtorg")
    for i in range(3):
        insert_quarantine_record(
            QuarantineRecord(
                raw_payload=sample_raw_repo(repo_id=i), organization="qtorg",
                failed_field="name", error_message="bad",
            ),
            run_id,
        )
    r = client.get("/quarantine/qtorg?page=1&page_size=2")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["results"]) == 2


def test_quarantine_invalid_page_returns_422():
    r = client.get("/quarantine/qtorg?page=0")
    assert r.status_code == 422