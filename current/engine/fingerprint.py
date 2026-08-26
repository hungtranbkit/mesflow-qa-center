"""Bug fingerprint normalization + hashing (requirement: dedup, not spam).

A fingerprint identifies "the same underlying bug", independent of which
run found it. Two evidence samples with different timestamps/request ids/
UUIDs/PIDs but the same feature+error type+normalized message+endpoint
collapse to one bug record instead of creating a new one every run.
"""
from __future__ import annotations

import hashlib
import re

_PATTERNS: list[tuple[re.Pattern, str]] = [
    # ISO-8601 timestamps, with or without timezone/millis.
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"), "<TS>"),
    # Plain dates.
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<DATE>"),
    # UUIDs (v1-v5, any case).
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<UUID>"),
    # request id / run id / correlation id style tokens, e.g. req-abc123, run_2f9a8b, rid=xxxx.
    (re.compile(r"\b(?:req|run|rid|corr|correlation|trace)[-_][a-zA-Z0-9]{4,}\b", re.IGNORECASE), "<REQID>"),
    # PID markers, e.g. "pid=12345" or "PID 12345".
    (re.compile(r"\bpid[=: ]\s*\d+\b", re.IGNORECASE), "<PID>"),
    # Bare long hex blobs (session tokens, hashes).
    (re.compile(r"\b[0-9a-fA-F]{16,}\b"), "<HEX>"),
    # Standalone large integers that look like autoincrement ids (5+ digits),
    # but keep short numbers (http codes, small counts) intact.
    (re.compile(r"\b\d{5,}\b"), "<NUM>"),
]


def normalize(text: str | None) -> str:
    """Strip volatile substrings so the same bug hashes the same every time."""
    if not text:
        return ""
    value = str(text)
    for pattern, replacement in _PATTERNS:
        value = pattern.sub(replacement, value)
    # Collapse whitespace so formatting differences don't change the hash.
    value = re.sub(r"\s+", " ", value).strip()
    return value


def compute(feature: str, error_type: str, message: str, endpoint: str) -> str:
    """Hash(feature + error type + normalized message + endpoint/page)."""
    parts = [
        (feature or "").strip(),
        (error_type or "").strip().upper(),
        normalize(message),
        (endpoint or "").strip(),
    ]
    digest_input = "\x1f".join(parts).encode("utf-8", errors="replace")
    return hashlib.sha256(digest_input).hexdigest()[:24]
