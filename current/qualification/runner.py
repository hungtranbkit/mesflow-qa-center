from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any, Callable

from .evidence import EvidenceStore
from .models import Driver, Scenario
from .store import connect, now


def fingerprint(value: str) -> str:
    normalized = " ".join(str(value).split())[:4000]
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class QualificationRunner:
    def __init__(self, evidence_root: Path):
        self.conn = connect()
        self.evidence = EvidenceStore(evidence_root)

    def run_command_suite(self, run_id: str, *, suite_key: str, layer: str,
                          command: list[str], cwd: Path, required: bool = True,
                          timeout_seconds: int = 1800) -> dict[str, Any]:
        suite_id = f"suite-{uuid.uuid4().hex}"
        started = now()
        self.conn.execute(
            """INSERT INTO qa_suite_runs(id,qualification_run_id,suite_key,layer,required,status,started_at,command_json)
               VALUES(?,?,?,?,?,?,?,?)""",
            (suite_id, run_id, suite_key, layer, int(required), "RUNNING", started, json.dumps(command)),
        )
        self.conn.commit()
        output_dir = self.evidence.root / run_id / "raw"
        output_dir.mkdir(parents=True, exist_ok=True)
        log_path = output_dir / f"{suite_id}.log"
        try:
            completed = subprocess.run(command, cwd=cwd, text=True, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, timeout=timeout_seconds,
                                       env={**os.environ, "PYTHONUNBUFFERED": "1"})
            log_path.write_text(completed.stdout or "", encoding="utf-8")
            status = "PASSED" if completed.returncode == 0 else "FAILED"
            exit_code = completed.returncode
            summary = {"output_tail": (completed.stdout or "").splitlines()[-80:]}
        except subprocess.TimeoutExpired as exc:
            text = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            log_path.write_text(text + f"\nTIMEOUT after {timeout_seconds}s\n", encoding="utf-8")
            status, exit_code = "FAILED", 124
            summary = {"error": "TIMEOUT", "timeout_seconds": timeout_seconds}
        evidence = self.evidence.add_file(run_id, log_path, kind="SUITE_LOG", suite_run_id=suite_id,
                                          metadata={"suite_key": suite_key, "layer": layer})
        self.conn.execute(
            "UPDATE qa_suite_runs SET status=?,finished_at=?,exit_code=?,summary_json=? WHERE id=?",
            (status, now(), exit_code, json.dumps({**summary, "evidence_id": evidence["id"]}), suite_id),
        )
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM qa_suite_runs WHERE id=?", (suite_id,)).fetchone())

    def run_scenario(self, suite_run_id: str, scenario: Scenario, driver: Driver,
                     *, context: dict[str, Any] | None = None, retries: int = 0,
                     invariant_checks: dict[str, Callable[[dict[str, Any]], tuple[bool, Any, Any]]] | None = None) -> dict[str, Any]:
        scenario_run_id = f"scenario-{uuid.uuid4().hex}"
        self.conn.execute(
            """INSERT INTO qa_scenario_runs(id,suite_run_id,scenario_key,scenario_version,driver,status,started_at)
               VALUES(?,?,?,?,?,'RUNNING',?)""",
            (scenario_run_id, suite_run_id, scenario.key, scenario.version, driver.name, now()),
        )
        self.conn.commit()
        attempts: list[str] = []
        final_context = dict(context or {})
        first_failure = ""
        failure_fingerprint = ""
        actual: dict[str, Any] = {}
        for attempt_no in range(1, retries + 2):
            attempt_started = now()
            attempt_status = "PASSED"
            for index, step in enumerate(scenario.steps, 1):
                result = driver.execute(step, final_context)
                actual[f"step_{index}"] = result.actual
                if not result.ok:
                    attempt_status = "FAILED"
                    first_failure = f"{index}:{step.action}"
                    failure_fingerprint = fingerprint(f"{scenario.key}|{driver.name}|{first_failure}|{result.error}|{result.actual}")
                    break
            self.conn.execute(
                "INSERT INTO qa_attempts(scenario_run_id,attempt_no,status,fingerprint,started_at,finished_at) VALUES(?,?,?,?,?,?)",
                (scenario_run_id, attempt_no, attempt_status, failure_fingerprint, attempt_started, now()),
            )
            self.conn.commit()
            attempts.append(attempt_status)
            if attempt_status == "PASSED":
                break
        status = "PASSED" if attempts == ["PASSED"] else ("FLAKY" if attempts[-1] == "PASSED" else "FAILED")
        suite = self.conn.execute("SELECT qualification_run_id FROM qa_suite_runs WHERE id=?", (suite_run_id,)).fetchone()
        run_id = suite["qualification_run_id"]
        for invariant_key in scenario.invariants:
            checker = (invariant_checks or {}).get(invariant_key)
            ok, expected, observed = checker(final_context) if checker else (False, "registered checker", "missing checker")
            self.conn.execute(
                """INSERT INTO qa_invariant_results(qualification_run_id,scenario_run_id,invariant_key,status,
                   expected_json,actual_json,created_at) VALUES(?,?,?,?,?,?,?)""",
                (run_id, scenario_run_id, invariant_key, "PASSED" if ok else "FAILED",
                 json.dumps(expected, default=str), json.dumps(observed, default=str), now()),
            )
            if not ok:
                status = "FAILED"
        expected = {f"step_{i}": step.expected for i, step in enumerate(scenario.steps, 1)}
        self.conn.execute(
            """UPDATE qa_scenario_runs SET status=?,finished_at=?,first_failing_step=?,fingerprint=?,
               expected_json=?,actual_json=? WHERE id=?""",
            (status, now(), first_failure, failure_fingerprint, json.dumps(expected), json.dumps(actual, default=str), scenario_run_id),
        )
        scenario_statuses = [r["status"] for r in self.conn.execute(
            "SELECT status FROM qa_scenario_runs WHERE suite_run_id=?", (suite_run_id,)).fetchall()]
        suite_status = "FAILED" if "FAILED" in scenario_statuses else ("FLAKY" if "FLAKY" in scenario_statuses else "PASSED")
        self.conn.execute("UPDATE qa_suite_runs SET status=?,finished_at=? WHERE id=?", (suite_status, now(), suite_run_id))
        self.conn.commit()
        return dict(self.conn.execute("SELECT * FROM qa_scenario_runs WHERE id=?", (scenario_run_id,)).fetchone())
