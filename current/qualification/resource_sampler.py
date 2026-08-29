"""Resource Sampler (spec section 7): one reusable resource-sampling
service, usable by LONG_RUNNING_FACTORY_SIMULATION/LOAD/SOAK/CHAOS/
RELEASE_QUALIFICATION alike, persisting samples by run_id/sandbox_id/
timestamp in qa_resource_samples so they remain queryable after the
sandbox itself is gone.

Container-level metrics (app CPU/RSS/FDs/threads, DB connections) come
straight from `docker stats`/`/proc` and pg_stat_activity -- the same
DooD primitives deployment.py/recovery.py already use, no new dependency.
There is no existing per-request latency histogram anywhere in MESFlow's
app or in engine/simulation to reuse for HTTP request count/rate and
p50/p95/p99 (grepped for one; none exists) -- fabricating one from
whatever a suite happens to log would not be honest. Instead this sampler
makes its own small number of real, timed HTTP probes each tick and
reports percentiles/error-rate over ITS OWN probe traffic, labelled as
such. That is a real, load-bearing signal (a probe timing out or erroring
under a genuinely overloaded app is a genuine finding), just not the same
thing as full production request-histogram observability -- reported
honestly as sampler-probe-derived, not invented per-request-in-production
numbers.
"""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import requests

from .database import DeterministicDatabase
from .store import connect, now

_DEVNULL = subprocess.DEVNULL


def _docker_stats(container: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}\t{{.MemUsage}}", container],
            stdout=subprocess.PIPE, stderr=_DEVNULL, timeout=10, check=False)
        if result.returncode != 0:
            return {}
        line = result.stdout.decode("utf-8", "replace").strip()
        cpu_raw, _, mem_raw = line.partition("\t")
        cpu_percent = float(cpu_raw.strip().rstrip("%") or 0.0)
        mem_used_raw = mem_raw.split("/")[0].strip() if mem_raw else ""
        return {"cpu_percent": cpu_percent, "mem_usage_raw": mem_used_raw}
    except Exception:
        return {}


def _proc1_status(container: str) -> dict[str, Any]:
    try:
        result = subprocess.run(["docker", "exec", container, "cat", "/proc/1/status"],
                                stdout=subprocess.PIPE, stderr=_DEVNULL, timeout=10, check=False)
        if result.returncode != 0:
            return {}
        fields: dict[str, Any] = {}
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("VmRSS:"):
                fields["rss_kb"] = int(line.split()[1])
            elif line.startswith("Threads:"):
                fields["threads"] = int(line.split()[1])
        return fields
    except Exception:
        return {}


def _proc1_fd_count(container: str) -> int | None:
    try:
        result = subprocess.run(["docker", "exec", container, "sh", "-c", "ls /proc/1/fd | wc -l"],
                                stdout=subprocess.PIPE, stderr=_DEVNULL, timeout=10, check=False)
        if result.returncode != 0:
            return None
        return int(result.stdout.decode("utf-8", "replace").strip())
    except Exception:
        return None


class ResourceSampler:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence_root = evidence_root

    def sample_once(self, run_id: str, sandbox_id: str, *, app_container: str, db_container: str,
                    target_url: str = "", probe_path: str = "/api/system/health") -> dict[str, Any]:
        """One point-in-time sample, persisted immediately. Never raises --
        a container that's briefly unreachable mid-fault-injection is a
        legitimate sample (partial/empty metrics), not a reason to abort
        whatever scenario is running."""
        app_stats = _docker_stats(app_container)
        app_status = _proc1_status(app_container)
        app_fds = _proc1_fd_count(app_container)
        db_stats = _docker_stats(db_container)

        db_connections = None
        try:
            rows = DeterministicDatabase(self.evidence_root).query_json(
                db_container, "SELECT count(*) n FROM pg_stat_activity")
            db_connections = rows[0]["n"] if rows else None
        except Exception:
            pass

        probe: dict[str, Any] = {}
        if target_url:
            started = time.time()
            try:
                response = requests.get(f"{target_url.rstrip('/')}{probe_path}", timeout=5)
                probe = {"latency_ms": round((time.time() - started) * 1000, 2),
                         "status_code": response.status_code, "error": response.status_code >= 400}
            except requests.RequestException as exc:
                probe = {"latency_ms": round((time.time() - started) * 1000, 2),
                         "status_code": None, "error": True, "exception": type(exc).__name__}

        metrics = {
            "app_cpu_percent": app_stats.get("cpu_percent"),
            "app_rss_kb": app_status.get("rss_kb"),
            "app_threads": app_status.get("threads"),
            "app_fds": app_fds,
            "db_connections": db_connections,
            "db_mem_usage_raw": db_stats.get("mem_usage_raw"),
            "probe": probe,
        }
        self.conn.execute(
            "INSERT INTO qa_resource_samples(run_id,sandbox_id,sampled_at,metrics_json) VALUES(?,?,?,?)",
            (run_id, sandbox_id, now(), __import__("json").dumps(metrics, default=str)))
        self.conn.commit()
        return metrics

    def run_window(self, run_id: str, sandbox_id: str, *, app_container: str, db_container: str,
                   target_url: str = "", window_seconds: float, interval_seconds: float = 5.0,
                   probe_path: str = "/api/system/health") -> dict[str, Any]:
        """Samples repeatedly for a bounded wall-clock window (the same
        pattern load_soak.py's own window loop already uses) and returns an
        aggregate summary -- p50/p95/p99 computed over this window's own
        probe latencies, plus min/avg/max for the container metrics.
        Sampling itself must not add excessive load: a single docker
        stats/exec + one small HTTP GET per interval is negligible next to
        whatever real workload (simulation/soak traffic) is running
        alongside it."""
        deadline = time.time() + window_seconds
        samples: list[dict[str, Any]] = []
        while True:
            samples.append(self.sample_once(run_id, sandbox_id, app_container=app_container,
                                            db_container=db_container, target_url=target_url,
                                            probe_path=probe_path))
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(interval_seconds, remaining))
        return self.summarize(samples)

    @staticmethod
    def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
        def _percentile(values: list[float], pct: float) -> float | None:
            if not values:
                return None
            ordered = sorted(values)
            index = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
            return ordered[index]

        cpu_values = [s["app_cpu_percent"] for s in samples if s.get("app_cpu_percent") is not None]
        rss_values = [s["app_rss_kb"] for s in samples if s.get("app_rss_kb") is not None]
        db_conn_values = [s["db_connections"] for s in samples if s.get("db_connections") is not None]
        latencies = [s["probe"]["latency_ms"] for s in samples if s.get("probe", {}).get("latency_ms") is not None]
        probe_errors = sum(1 for s in samples if s.get("probe", {}).get("error"))
        probe_total = sum(1 for s in samples if s.get("probe"))

        return {
            "sample_count": len(samples),
            "app_cpu_percent": {"avg": round(sum(cpu_values) / len(cpu_values), 2) if cpu_values else None,
                                "max": max(cpu_values) if cpu_values else None},
            "app_rss_kb": {"avg": round(sum(rss_values) / len(rss_values)) if rss_values else None,
                          "max": max(rss_values) if rss_values else None},
            "db_connections": {"avg": round(sum(db_conn_values) / len(db_conn_values), 2) if db_conn_values else None,
                               "max": max(db_conn_values) if db_conn_values else None},
            "probe_requests": probe_total,
            "probe_errors": probe_errors,
            "probe_error_rate": round(probe_errors / probe_total, 4) if probe_total else None,
            "probe_latency_ms": {"p50": _percentile(latencies, 50), "p95": _percentile(latencies, 95),
                                 "p99": _percentile(latencies, 99)},
        }
