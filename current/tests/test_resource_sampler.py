from __future__ import annotations

import json

import pytest

from engine import qa_store
from qualification.resource_sampler import ResourceSampler
from qualification.store import connect


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    qa_store.reset_for_tests(tmp_path / "meta.sqlite3")
    return tmp_path


def test_sample_once_persists_by_run_id_sandbox_id_and_timestamp(isolated):
    sampler = ResourceSampler(isolated / "evidence")
    metrics = sampler.sample_once("run-abc", "sandbox-xyz", app_container="no-such-app-container",
                                  db_container="no-such-db-container")
    # A missing container must never raise -- every metric is best-effort
    # and simply absent, not a reason to abort whatever scenario is running.
    assert metrics["app_cpu_percent"] is None
    assert metrics["db_connections"] is None

    conn = connect()
    rows = conn.execute("SELECT run_id,sandbox_id,sampled_at,metrics_json FROM qa_resource_samples "
                        "WHERE run_id='run-abc' AND sandbox_id='sandbox-xyz'").fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["run_id"] == "run-abc"
    assert row["sandbox_id"] == "sandbox-xyz"
    assert row["sampled_at"]
    assert json.loads(row["metrics_json"]) == metrics


def test_sample_once_records_a_failed_http_probe_without_raising(isolated):
    sampler = ResourceSampler(isolated / "evidence")
    metrics = sampler.sample_once("run-probe", "sandbox-probe", app_container="no-such-app",
                                  db_container="no-such-db", target_url="http://127.0.0.1:1")
    assert metrics["probe"]["error"] is True
    assert metrics["probe"]["latency_ms"] is not None


def test_summarize_computes_percentiles_and_error_rate_over_the_window():
    samples = [
        {"app_cpu_percent": 10.0, "app_rss_kb": 1000, "db_connections": 2,
         "probe": {"latency_ms": 10.0, "status_code": 200, "error": False}},
        {"app_cpu_percent": 20.0, "app_rss_kb": 1200, "db_connections": 3,
         "probe": {"latency_ms": 20.0, "status_code": 200, "error": False}},
        {"app_cpu_percent": 30.0, "app_rss_kb": 1400, "db_connections": 4,
         "probe": {"latency_ms": 500.0, "status_code": 503, "error": True}},
    ]
    summary = ResourceSampler.summarize(samples)
    assert summary["sample_count"] == 3
    assert summary["app_cpu_percent"]["avg"] == 20.0
    assert summary["app_cpu_percent"]["max"] == 30.0
    assert summary["app_rss_kb"]["max"] == 1400
    assert summary["db_connections"]["max"] == 4
    assert summary["probe_requests"] == 3
    assert summary["probe_errors"] == 1
    assert summary["probe_error_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert summary["probe_latency_ms"]["p50"] == 20.0
    assert summary["probe_latency_ms"]["p99"] == 500.0


def test_summarize_handles_an_empty_window_without_raising():
    summary = ResourceSampler.summarize([])
    assert summary["sample_count"] == 0
    assert summary["app_cpu_percent"]["avg"] is None
    assert summary["probe_latency_ms"]["p50"] is None
    assert summary["probe_error_rate"] is None
