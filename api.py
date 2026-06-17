"""FastAPI web service — POST /run (sync) · POST /jobs (async) · GET /health."""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import sys
import threading
import uuid
from collections.abc import AsyncGenerator, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Response, Security
from fastapi.security import APIKeyHeader
from loguru import logger
from pydantic import BaseModel, Field

from agent.config import AgentConfig
from agent.core import AgentSession
from agent.exceptions import MaxStepsExceededError

_VERSION = "1.0.0"
_MAX_WORKERS = int(os.getenv("MAX_CONCURRENT_SESSIONS", "2"))
_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
# Slightly over the default 600 s session timeout so the agent can finish naturally.
_RUN_TIMEOUT = float(os.getenv("RUN_TIMEOUT_SECONDS", "660"))
_MAX_QUEUED_JOBS = int(os.getenv("MAX_QUEUED_JOBS", "100"))

# ── Auth ──────────────────────────────────────────────────────────────────────
# Set API_KEY env var to require X-API-Key on every agent endpoint.
# Leave unset to disable auth (suitable for private / local deployments).

_API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def _check_api_key(key: str | None = Security(_API_KEY_HEADER)) -> None:
    expected = os.getenv("API_KEY")
    if expected and key != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


_AUTH = [Depends(_check_api_key)]


# ── SQLite job store ──────────────────────────────────────────────────────────
# Jobs are written to {DATA_DIR}/jobs.db so they survive container restarts.
# For multi-replica deployments replace these helpers with a Redis/Postgres backend.


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


_db_file: Path | None = None  # set in lifespan once DATA_DIR is known


def _now() -> str:
    return datetime.now(UTC).isoformat()


@contextmanager
def _db() -> Generator[sqlite3.Connection, None, None]:
    assert _db_file is not None, "DB not initialised — service not started yet"
    conn = sqlite3.connect(str(_db_file), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def _db_ro() -> Generator[sqlite3.Connection, None, None]:
    """Read-only connection — no commit needed."""
    assert _db_file is not None, "DB not initialised — service not started yet"
    conn = sqlite3.connect(str(_db_file), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id     TEXT PRIMARY KEY,
                goal       TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                result     TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)


def _job_insert(job_id: str, goal: str) -> None:
    now = _now()
    with _db() as conn:
        conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?)",
            (job_id, goal, JobStatus.PENDING, None, now, now),
        )


def _job_set_running(job_id: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, updated_at=? WHERE job_id=?",
            (JobStatus.RUNNING, _now(), job_id),
        )


def _job_set_done(job_id: str, result: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, result=?, updated_at=? WHERE job_id=?",
            (JobStatus.DONE, result, _now(), job_id),
        )


def _job_set_failed(job_id: str, reason: str) -> None:
    with _db() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, result=?, updated_at=? WHERE job_id=?",
            (JobStatus.FAILED, reason, _now(), job_id),
        )


def _job_get(job_id: str) -> dict[str, Any] | None:
    with _db_ro() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    return dict(row) if row else None


def _job_delete(job_id: str) -> None:
    with _db() as conn:
        conn.execute("DELETE FROM jobs WHERE job_id=?", (job_id,))


def _job_insert_if_capacity(job_id: str, goal: str, max_active: int) -> bool:
    """Atomically insert job only when active count < max_active. Returns False if full."""
    now = _now()
    with _db() as conn:
        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN (?,?)",
                (JobStatus.PENDING, JobStatus.RUNNING),
            ).fetchone()[0]
        )
        if count >= max_active:
            return False
        conn.execute(
            "INSERT INTO jobs VALUES (?,?,?,?,?,?)",
            (job_id, goal, JobStatus.PENDING, None, now, now),
        )
    return True


def _recover_orphaned_jobs() -> None:
    """On startup, mark any jobs left in 'running' state by a prior process as failed."""
    with _db() as conn:
        conn.execute(
            "UPDATE jobs SET status=?, result=?, updated_at=? WHERE status=?",
            (JobStatus.FAILED, "Process restarted before job completed", _now(), JobStatus.RUNNING),
        )


# ── Background job runner ─────────────────────────────────────────────────────


def _run_job(job_id: str, goal: str, config_overrides: dict[str, Any]) -> None:
    _job_set_running(job_id)
    try:
        cfg = AgentConfig(**config_overrides)  # type: ignore[call-arg]
        result = AgentSession(config=cfg).run(goal=goal)
        _job_set_done(job_id, result)
    except Exception as exc:
        logger.exception("job_failed | job_id={}", job_id)
        _job_set_failed(job_id, str(exc))


# ── App lifespan ──────────────────────────────────────────────────────────────

_executor: ThreadPoolExecutor | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
    global _executor, _db_file

    logger.remove()
    logger.add(
        sys.stdout,
        level=_LOG_LEVEL,
        colorize=False,
        format="{time:YYYY-MM-DDTHH:mm:ssZ} | {level} | {name} | {message}",
    )

    _db_file = Path(os.getenv("DATA_DIR", "/tmp/browser-agent")) / "jobs.db"
    _init_db(_db_file)
    _recover_orphaned_jobs()

    _executor = ThreadPoolExecutor(
        max_workers=_MAX_WORKERS,
        thread_name_prefix="agent-session",
    )

    # SIGTERM → graceful shutdown; only callable from the main thread.
    _in_main_thread = threading.current_thread() is threading.main_thread()
    original_handler = signal.getsignal(signal.SIGTERM)

    if _in_main_thread:

        def _sigterm(_signum: int, _frame: object) -> None:
            logger.warning("SIGTERM received — shutting down gracefully")
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, _sigterm)

    logger.info("startup | workers={} version={} db={}", _MAX_WORKERS, _VERSION, _db_file)

    yield

    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
    if _in_main_thread:
        signal.signal(signal.SIGTERM, original_handler)
    logger.info("shutdown complete")


# ── FastAPI app ───────────────────────────────────────────────────────────────

app = FastAPI(
    title="Browser Agent API",
    description="Autonomous browser agent powered by Claude vision.",
    version=_VERSION,
    lifespan=lifespan,
)


# ── Schemas ───────────────────────────────────────────────────────────────────


class AgentRequest(BaseModel):
    goal: str = Field(..., min_length=1, max_length=2000, description="Natural-language goal")
    max_steps: int | None = Field(None, ge=1, le=100)
    headless: bool = Field(True)


class RunResponse(BaseModel):
    result: str
    failed: bool


class JobCreatedResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobResponse(BaseModel):
    job_id: str
    goal: str
    status: JobStatus
    result: str | None = None
    created_at: str
    updated_at: str


# ── Endpoints ─────────────────────────────────────────────────────────────────


@app.get("/health", tags=["ops"])
def health() -> dict[str, str]:
    """Liveness probe — no auth required (safe for load-balancer health checks)."""
    return {"status": "ok", "version": _VERSION}


@app.post("/run", response_model=RunResponse, tags=["agent"], dependencies=_AUTH)
async def run_sync(request: AgentRequest) -> RunResponse:
    """Run synchronously and wait for the result.

    Times out after RUN_TIMEOUT_SECONDS (default 660 s). The underlying thread
    is not cancellable — it completes in the background — but the HTTP response
    is sent immediately on timeout. Use POST /jobs for long-running tasks.
    """
    if _executor is None:
        raise HTTPException(status_code=503, detail="Service initialising, retry shortly")
    overrides = _build_overrides(request)
    try:
        loop = asyncio.get_running_loop()
        coro = loop.run_in_executor(
            _executor,
            lambda: AgentSession(config=AgentConfig(**overrides)).run(request.goal),  # type: ignore[call-arg]
        )
        result: str = await asyncio.wait_for(coro, timeout=_RUN_TIMEOUT)
    except TimeoutError as exc:
        raise HTTPException(
            status_code=408, detail=f"Agent timed out after {_RUN_TIMEOUT:.0f}s"
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MaxStepsExceededError as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("run_failed | goal={!r}", request.goal)
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RunResponse(result=result, failed=result.startswith("FAILED"))


@app.post(
    "/jobs", response_model=JobCreatedResponse, status_code=202, tags=["agent"], dependencies=_AUTH
)
def create_job(request: AgentRequest) -> JobCreatedResponse:
    """Submit a goal asynchronously. Poll GET /jobs/{id} for the result."""
    if _executor is None:
        raise HTTPException(status_code=503, detail="Service initialising, retry shortly")
    job_id = uuid.uuid4().hex
    if not _job_insert_if_capacity(job_id, request.goal, _MAX_QUEUED_JOBS):
        raise HTTPException(
            status_code=429,
            detail=f"Too many active jobs (limit {_MAX_QUEUED_JOBS}). Try again later.",
        )
    try:
        _executor.submit(_run_job, job_id, request.goal, _build_overrides(request))
    except RuntimeError as exc:
        _job_delete(job_id)
        raise HTTPException(status_code=503, detail="Service shutting down, retry shortly") from exc
    logger.info("job_created | job_id={} goal={!r}", job_id, request.goal)
    return JobCreatedResponse(job_id=job_id, status=JobStatus.PENDING)


@app.get("/jobs/{job_id}", response_model=JobResponse, tags=["agent"], dependencies=_AUTH)
def get_job(job_id: str) -> JobResponse:
    """Poll the status and result of an async job."""
    job = _job_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return JobResponse(**job)


@app.delete("/jobs/{job_id}", tags=["agent"], dependencies=_AUTH)
def delete_job(job_id: str) -> Response:
    """Remove a completed job record from the store."""
    job = _job_get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job["status"] in (JobStatus.PENDING, JobStatus.RUNNING):
        raise HTTPException(status_code=409, detail="Cannot delete a job that is still active")
    _job_delete(job_id)
    return Response(status_code=204)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _build_overrides(request: AgentRequest) -> dict[str, Any]:
    overrides: dict[str, Any] = {"headless": request.headless}
    if request.max_steps is not None:
        overrides["max_steps"] = request.max_steps
    return overrides
