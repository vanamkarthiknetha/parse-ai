"""Shared fixtures: mock API lifecycle, per-test reset, result recording."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
API_URL = os.getenv("RESERVATION_API_URL", "http://localhost:8000")
RESULTS_DIR = Path(__file__).resolve().parent / "results"


def _api_healthy() -> bool:
    try:
        return httpx.get(f"{API_URL}/health", timeout=1.0).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="session")
def mock_api():
    """Reuse an already-running mock API, or start one for the test session."""
    proc = None
    if not _api_healthy():
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app:app", "--port", "8000", "--log-level", "warning"],
            cwd=REPO_ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(50):
            if _api_healthy():
                break
            time.sleep(0.2)
        else:
            proc.terminate()
            pytest.fail("mock reservation API did not become healthy on :8000")
    yield API_URL
    if proc is not None:
        proc.terminate()
        proc.wait(timeout=5)


@pytest.fixture()
def reset_api(mock_api):
    """POST /admin/reset before each test, per the starter README."""
    resp = httpx.post(f"{mock_api}/admin/reset", timeout=5.0)
    assert resp.status_code == 200
    return mock_api


@pytest.fixture(scope="session")
def recorder():
    """Collects per-scenario results and writes evals/results/scenario_results.json."""
    results: list[dict] = []
    yield results
    if results:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / "scenario_results.json"
        out.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nwrote {out} ({len(results)} scenarios)")
