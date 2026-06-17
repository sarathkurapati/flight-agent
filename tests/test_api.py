"""Tests for the FastAPI web service (api.py)."""

from __future__ import annotations

import asyncio
import sqlite3

import pytest
from fastapi.testclient import TestClient

# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def client(monkeypatch, tmp_path):
    """TestClient with isolated SQLite DB in tmp_path (no auth required)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import api as api_module

    with TestClient(api_module.app) as c:
        yield c


@pytest.fixture()
def auth_client(monkeypatch, tmp_path):
    """TestClient with API_KEY='secret' configured."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("API_KEY", "secret")
    import api as api_module

    with TestClient(api_module.app) as c:
        yield c


# ── Health ────────────────────────────────────────────────────────────────────


class TestHealth:
    def test_returns_200(self, client: TestClient) -> None:
        assert client.get("/health").status_code == 200

    def test_status_is_ok(self, client: TestClient) -> None:
        assert client.get("/health").json()["status"] == "ok"

    def test_version_present(self, client: TestClient) -> None:
        assert "version" in client.get("/health").json()


# ── Auth ──────────────────────────────────────────────────────────────────────


class TestAuth:
    def test_health_bypasses_auth(self, auth_client: TestClient) -> None:
        """Load-balancers hit /health without credentials; must return 200."""
        assert auth_client.get("/health").status_code == 200

    def test_run_returns_401_without_key(self, auth_client: TestClient) -> None:
        r = auth_client.post("/run", json={"goal": "task"})
        assert r.status_code == 401

    def test_run_returns_401_with_wrong_key(self, auth_client: TestClient) -> None:
        r = auth_client.post("/run", json={"goal": "task"}, headers={"X-API-Key": "wrong"})
        assert r.status_code == 401

    def test_run_accepts_correct_key(self, auth_client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api.AgentSession.run", return_value="done")
        mocker.patch("api.AgentConfig")
        r = auth_client.post("/run", json={"goal": "task"}, headers={"X-API-Key": "secret"})
        assert r.status_code == 200

    def test_jobs_returns_401_without_key(self, auth_client: TestClient) -> None:
        assert auth_client.post("/jobs", json={"goal": "task"}).status_code == 401

    def test_no_auth_when_api_key_not_configured(self, client: TestClient) -> None:
        """With no API_KEY env var, requests succeed without a key."""
        # /health is always unprotected; other routes should also work without key
        assert client.get("/health").status_code == 200


# ── POST /run ─────────────────────────────────────────────────────────────────


class TestRunEndpoint:
    def test_returns_result(self, client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api.AgentSession.run", return_value="Task completed.")
        mocker.patch("api.AgentConfig")
        r = client.post("/run", json={"goal": "do something"})
        assert r.status_code == 200
        assert r.json()["result"] == "Task completed."
        assert r.json()["failed"] is False

    def test_failed_result_sets_flag(self, client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api.AgentSession.run", return_value="FAILED: blocked")
        mocker.patch("api.AgentConfig")
        r = client.post("/run", json={"goal": "impossible task"})
        assert r.status_code == 200
        assert r.json()["failed"] is True

    def test_rejects_empty_goal(self, client: TestClient) -> None:
        assert client.post("/run", json={"goal": ""}).status_code == 422

    def test_rejects_missing_goal(self, client: TestClient) -> None:
        assert client.post("/run", json={}).status_code == 422

    def test_rejects_goal_too_long(self, client: TestClient) -> None:
        assert client.post("/run", json={"goal": "x" * 2001}).status_code == 422

    def test_timeout_returns_408(self, client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api.asyncio.wait_for", side_effect=asyncio.TimeoutError)
        r = client.post("/run", json={"goal": "slow task"})
        assert r.status_code == 408
        assert "timed out" in r.json()["detail"].lower()

    def test_max_steps_forwarded_to_config(self, client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api.AgentSession.run", return_value="done")
        cfg_cls = mocker.patch("api.AgentConfig")
        client.post("/run", json={"goal": "task", "max_steps": 5})
        kwargs = cfg_cls.call_args.kwargs
        assert kwargs.get("max_steps") == 5


# ── POST /jobs ────────────────────────────────────────────────────────────────


class TestJobsCreate:
    def test_returns_202(self, client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api._run_job")
        assert client.post("/jobs", json={"goal": "async task"}).status_code == 202

    def test_response_has_job_id(self, client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api._run_job")
        data = client.post("/jobs", json={"goal": "task"}).json()
        assert len(data["job_id"]) == 32  # uuid4().hex

    def test_status_is_pending(self, client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api._run_job")
        data = client.post("/jobs", json={"goal": "task"}).json()
        assert data["status"] == "pending"

    def test_rejects_empty_goal(self, client: TestClient) -> None:
        assert client.post("/jobs", json={"goal": ""}).status_code == 422

    def test_returns_429_when_queue_full(self, client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api._job_insert_if_capacity", return_value=False)
        r = client.post("/jobs", json={"goal": "overflow"})
        assert r.status_code == 429


# ── GET /jobs/{id} ────────────────────────────────────────────────────────────


class TestJobsGet:
    def test_returns_created_job(self, client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api._run_job")
        job_id = client.post("/jobs", json={"goal": "find info"}).json()["job_id"]
        r = client.get(f"/jobs/{job_id}")
        assert r.status_code == 200
        assert r.json()["job_id"] == job_id

    def test_returns_404_for_unknown_id(self, client: TestClient) -> None:
        assert client.get("/jobs/doesnotexist").status_code == 404

    def test_job_goal_is_stored(self, client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api._run_job")
        job_id = client.post("/jobs", json={"goal": "my specific goal"}).json()["job_id"]
        assert client.get(f"/jobs/{job_id}").json()["goal"] == "my specific goal"


# ── DELETE /jobs/{id} ─────────────────────────────────────────────────────────


class TestJobsDelete:
    def test_deletes_done_job(self, client: TestClient) -> None:
        import api as api_module

        api_module._job_insert("testjob", "test goal")
        api_module._job_set_done("testjob", "result ok")
        assert client.delete("/jobs/testjob").status_code == 204

    def test_job_gone_after_delete(self, client: TestClient) -> None:
        import api as api_module

        api_module._job_insert("gone", "goal")
        api_module._job_set_done("gone", "ok")
        client.delete("/jobs/gone")
        assert client.get("/jobs/gone").status_code == 404

    def test_returns_404_for_unknown_id(self, client: TestClient) -> None:
        assert client.delete("/jobs/nosuchjob").status_code == 404

    def test_rejects_delete_of_pending_job(self, client: TestClient, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api._run_job")
        job_id = client.post("/jobs", json={"goal": "running task"}).json()["job_id"]
        assert client.delete(f"/jobs/{job_id}").status_code == 409


# ── SQLite persistence ────────────────────────────────────────────────────────


class TestSQLitePersistence:
    def test_job_written_to_db_on_create(self, client: TestClient, tmp_path, mocker) -> None:  # type: ignore[no-untyped-def]
        mocker.patch("api._run_job")
        job_id = client.post("/jobs", json={"goal": "persist me"}).json()["job_id"]

        conn = sqlite3.connect(str(tmp_path / "jobs.db"))
        row = conn.execute("SELECT goal FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "persist me"

    def test_job_status_updated_in_db(self, client: TestClient, tmp_path) -> None:
        import api as api_module

        api_module._job_insert("upd", "goal")
        api_module._job_set_done("upd", "completed")

        conn = sqlite3.connect(str(tmp_path / "jobs.db"))
        row = conn.execute("SELECT status, result FROM jobs WHERE job_id='upd'").fetchone()
        conn.close()
        assert row[0] == "done"
        assert row[1] == "completed"

    def test_deleted_job_removed_from_db(self, client: TestClient, tmp_path) -> None:
        import api as api_module

        api_module._job_insert("del", "goal")
        api_module._job_set_done("del", "ok")
        client.delete("/jobs/del")

        conn = sqlite3.connect(str(tmp_path / "jobs.db"))
        row = conn.execute("SELECT 1 FROM jobs WHERE job_id='del'").fetchone()
        conn.close()
        assert row is None
