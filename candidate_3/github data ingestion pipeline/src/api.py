"""
FastAPI service layer. Thin: every endpoint delegates to
client.py / schema.py / repository.py / quality.py - no business logic
lives here beyond request/response shaping and status codes.
"""

import logging
import re
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from src.client import GitHubClient, GitHubClientError
from src.quality import compute_quality_report
from src.repository import (
    RunAlreadyInProgressError,
    create_pipeline_run,
    finish_pipeline_run,
    get_quarantine,
    get_run,
    list_runs,
    upsert_repos,
    insert_quarantine_records,
)
from src.schema import validate_repo

logger = logging.getLogger(__name__)

app = FastAPI(title="GitHub Data Ingestion Pipeline")

# GitHub org names: alphanumeric + hyphens, can't start/end with a hyphen.
ORG_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,37}[a-zA-Z0-9])?$")


class IngestResponse(BaseModel):
    run_id: str


class RunResponse(BaseModel):
    run_id: str
    org: str
    started_at: datetime
    finished_at: datetime | None
    records_fetched: int
    records_valid: int
    records_quarantined: int
    status: str


# --- cross-cutting: never leak internals in an error response -------------


@app.exception_handler(Exception)
async def unhandled_exception_handler(request, exc: Exception) -> JSONResponse:
    """
    Catches anything not already handled below. Logs the real error
    server-side but returns only a generic message - never the exception
    text itself, which could contain a connection string or token.
    """
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


# --- background ingestion work ---------------------------------------------


async def run_ingestion(org: str, run_id: str) -> None:
    """
    The actual pipeline: fetch -> validate -> write. Runs in the
    background so POST /ingest/{org} can return immediately.
    """
    try:
        client = GitHubClient()
        run_timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        raw_repos = await client.fetch_org_repos(org, run_timestamp)

        valid_repos = []
        quarantine_records = []
        for raw in raw_repos:
            result = validate_repo(raw, org)
            if hasattr(result, "id"):  # it's a Repo
                valid_repos.append(result)
            else:
                quarantine_records.append(result)

        upsert_repos(valid_repos, org, run_id)
        insert_quarantine_records(quarantine_records, run_id)

        finish_pipeline_run(
            run_id,
            records_fetched=len(raw_repos),
            records_valid=len(valid_repos),
            records_quarantined=len(quarantine_records),
            status="success",
        )
    except (GitHubClientError, Exception) as exc:  # noqa: BLE001
        logger.exception("Ingestion run %s for org %s failed", run_id, org)
        finish_pipeline_run(
            run_id,
            records_fetched=0,
            records_valid=0,
            records_quarantined=0,
            status="failed",
        )


# --- endpoints ---------------------------------------------------------


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/ingest/{org}", response_model=IngestResponse, status_code=202)
async def ingest(org: str, background_tasks: BackgroundTasks) -> IngestResponse:
    if not ORG_NAME_PATTERN.match(org):
        raise HTTPException(status_code=422, detail="Invalid organization name")

    try:
        run_id = create_pipeline_run(org)
    except RunAlreadyInProgressError:
        raise HTTPException(
            status_code=409, detail=f"A run is already in progress for org '{org}'"
        )

    background_tasks.add_task(run_ingestion, org, run_id)
    return IngestResponse(run_id=run_id)


@app.get("/runs/{run_id}", response_model=RunResponse)
def get_run_endpoint(run_id: str) -> RunResponse:
    run = get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunResponse(**run)


@app.get("/runs", response_model=list[RunResponse])
def list_runs_endpoint(limit: int = Query(default=50, ge=1, le=200)) -> list[RunResponse]:
    return [RunResponse(**r) for r in list_runs(limit=limit)]


@app.get("/quality-report/{org}")
async def quality_report_endpoint(org: str) -> dict:
    if not ORG_NAME_PATTERN.match(org):
        raise HTTPException(status_code=422, detail="Invalid organization name")
    return await compute_quality_report(org)


@app.get("/quarantine/{org}")
def quarantine_endpoint(
    org: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
) -> dict:
    rows, total = get_quarantine(org, page=page, page_size=page_size)
    return {
        "organization": org,
        "page": page,
        "page_size": page_size,
        "total": total,
        "results": rows,
    }