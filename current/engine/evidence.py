"""Evidence bundle builder for bug records (requirement: enough evidence to
fix later, never secrets).

Kept intentionally standalone (no import of agent.py) so it has no
dependency on the Flask app and can be unit tested in isolation.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

_SECRET_KEYS = re.compile(r"(password|passwd|secret|token|cookie|authorization|api[_-]?key)", re.IGNORECASE)
_SECRET_VALUE_PATTERNS = [
    re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[=:]\s*([^\s&\"']+)"),
    re.compile(r"(?i)(authorization:\s*bearer)\s+[^\s]+"),
    re.compile(r"(?i)(set-cookie|cookie):\s*[^\r\n]+"),
]


def redact_text(value: Any) -> str:
    """Redact obvious secrets from free text before it is stored/shown."""
    text = "" if value is None else str(value)
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)}=<REDACTED>", text)
    return text


def redact_value(value: Any) -> Any:
    """Recursively redact dict/list/str structures (used for payload/response bodies)."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _SECRET_KEYS.search(str(k)):
                out[k] = "<REDACTED>"
            else:
                out[k] = redact_value(v)
        return out
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def build_evidence(
    *,
    feature: str = "",
    scenario: str = "",
    seed_version: str = "",
    mesflow_version: str = "",
    qa_center_version: str = "",
    commit_sha: str = "",
    url: str = "",
    api: str = "",
    request_payload: Any = None,
    response_status: int | None = None,
    response_body_excerpt: str = "",
    console_log: list[str] | None = None,
    pageerror: str = "",
    screenshot_path: str = "",
    backend_log_excerpt: str = "",
    expected: Any = None,
    actual: Any = None,
    fingerprint: str = "",
    related_source_files: list[str] | None = None,
    related_test_case: str = "",
) -> dict[str, Any]:
    """Assemble one bug's evidence dict. Never stores password/token/cookie."""
    return {
        "feature": feature,
        "scenario": scenario,
        "seed_version": seed_version,
        "mesflow_version": mesflow_version,
        "qa_center_version": qa_center_version,
        "commit_sha": commit_sha,
        "url": url,
        "api": api,
        "request_payload": redact_value(request_payload) if request_payload is not None else None,
        "response_status": response_status,
        "response_body_excerpt": redact_text(response_body_excerpt)[:4000],
        "console_log": [redact_text(x) for x in (console_log or [])][:50],
        "pageerror": redact_text(pageerror)[:2000],
        "screenshot_path": screenshot_path,
        "backend_log_excerpt": redact_text(backend_log_excerpt)[:4000],
        "expected": redact_value(expected),
        "actual": redact_value(actual),
        "fingerprint": fingerprint,
        "related_source_files": related_source_files or [],
        "related_test_case": related_test_case,
        "captured_at": now_iso(),
    }
