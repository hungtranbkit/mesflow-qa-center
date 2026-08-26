"""The UI Preview Lab safety guards (requirement 1), in one place so both
``preview_manager`` (Docker lifecycle) and ``preview.seed`` (direct DB
writes) enforce exactly the same fail-closed rules with no risk of drift.

Three guards are mandatory. If ANY is missing, the caller must refuse:

  1. ``MESFLOW_UI_PREVIEW=1`` in the target app container's environment
  2. the database name starts with ``mesflow_ui_``
  3. the Docker resource carries label ``com.mesflow.qa.preview=1``
"""
from __future__ import annotations

from urllib.parse import unquote, urlparse

PREVIEW_LABEL_KEY = "com.mesflow.qa.preview"
PREVIEW_LABEL_VALUE = "1"
PREVIEW_DB_PREFIX = "mesflow_ui_"
REQUIRED_ENV_FLAG = "MESFLOW_UI_PREVIEW"


class PreviewSafetyError(Exception):
    """Raised instead of proceeding when a required preview guard is missing."""


def assert_preview_db_name(name: str) -> None:
    if not name or not name.startswith(PREVIEW_DB_PREFIX):
        raise PreviewSafetyError(
            f"REFUSE_UNSAFE_DATABASE: {name!r} does not start with {PREVIEW_DB_PREFIX!r}"
        )


def db_name_from_url(database_url: str) -> str:
    return unquote(urlparse(database_url).path.lstrip("/"))


def assert_preview_database_url(database_url: str) -> str:
    """Convenience: extract + validate the db name from a connection URL in
    one call. Returns the db name so callers don't reparse it."""
    name = db_name_from_url(database_url)
    assert_preview_db_name(name)
    return name


def assert_preview_env(env: dict[str, str]) -> None:
    if str((env or {}).get(REQUIRED_ENV_FLAG, "")).strip() != "1":
        raise PreviewSafetyError(
            f"REFUSE_MISSING_GUARD: environment does not have {REQUIRED_ENV_FLAG}=1"
        )


def assert_preview_label(labels: dict[str, str] | None) -> None:
    labels = labels or {}
    if str(labels.get(PREVIEW_LABEL_KEY, "")) != PREVIEW_LABEL_VALUE:
        raise PreviewSafetyError(
            f"REFUSE_UNLABELED_RESOURCE: missing docker label {PREVIEW_LABEL_KEY}={PREVIEW_LABEL_VALUE}"
        )


PREVIEW_VOLUME_ID_LABEL_KEY = "com.mesflow.qa.preview.id"


def assert_preview_volume_label(labels: dict[str, str] | None, env_id: str) -> None:
    """Stronger guard for volume deletion (requirement 7/21): the volume must
    carry both the generic preview label AND an id label matching the exact
    environment being deleted, so a bug in id/name construction can never
    make one preview's Delete remove a *different* preview's data volume."""
    assert_preview_label(labels)
    labels = labels or {}
    if str(labels.get(PREVIEW_VOLUME_ID_LABEL_KEY, "")) != str(env_id):
        raise PreviewSafetyError(
            f"REFUSE_VOLUME_ID_MISMATCH: volume label {PREVIEW_VOLUME_ID_LABEL_KEY}="
            f"{labels.get(PREVIEW_VOLUME_ID_LABEL_KEY)!r} does not match environment {env_id!r}"
        )
