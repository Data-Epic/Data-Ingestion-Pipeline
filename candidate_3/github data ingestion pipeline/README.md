# GitHub Data Ingestion Pipeline

Goal: I am building a pipeline that pulls GitHub repo data (for orgs like stripe/shopify/microsoft), checks it's valid, saves the good stuff(valid data) to a database, and quarantines(Archived: both the bad data and why it failed) the bad stuff all wrapped in a small API. That's it.

## Layout

```
src/
  config.py       settings from environment (single chokepoint - no
                  other module reads os.getenv directly)
  client.py       GitHub HTTP client: pagination, backoff, rate limits,
                  raw payload capture
  schema.py       Pydantic Repo model + field/cross-field validators +
                  quarantine record shape
  db.py           Postgres connection management
  repository.py   all SQL lives here, parameterized, upsert + quarantine
                  + pipeline_runs bookkeeping
  quality.py      the 5-dimension quality report, computed live
  api.py          FastAPI endpoints (currently just a /health check)
db/
  schema.sql      table DDL (placeholder)
tests/
  test_client.py / test_schema.py / test_repository.py / test_api.py
  conftest.py     shared fixtures (placeholder)
data/raw/         raw page dumps land here at runtime, gitignored
```

## Build order (proposed)

1. `src/schema.py` — lock the `Repo` model + validators first; everything
   else references its field names and types.
2. `db/schema.sql` + `src/db.py` — table DDL matching the model exactly.
3. `src/client.py` — pagination, backoff, rate-limit handling.
4. `src/repository.py` — upsert + quarantine + pipeline_runs writes.
5. `src/quality.py` — the 5 dimensions, once there's real data to query.
6. `src/api.py` — wire endpoints to the above.
7. Tests alongside each step above, not saved for the end.

## Setup (once dependencies are installed)

```bash
cp .env.example .env      # fill in DATABASE_URL, optionally GITHUB_TOKEN
pip install -r requirements.txt
pytest                     # runs with coverage per pytest.ini
uvicorn src.api:app --reload
```


# Notes
async function are funtions that are defined using the async def syntax. 
They allow you to write asynchronous code (with tasks that can be ran in the background) that can be paused and resumed, 
making it easier to handle I/O-bound operations without blocking the main thread.

Ingesting in this context means collecting and importing data from various sournces (APIs, databases, files, streams) into a centralized system (data warehouse)

Quarantine is a holding area for bad records so nothing gets silently lost.

flow for one bad record looks like:
raw GitHub JSON → validate_repo() fails → becomes a QuarantineRecord (Python) → repository.py writes it → one row in github_repos_quarantine (Postgres)
someone can run GET /quarantine/stripe and actually see what went wrong and why, instead of the data quietly vanishing.