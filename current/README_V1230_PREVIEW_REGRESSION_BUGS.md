# QA Center 1.23.0 / 1.23.1 — UI Preview Lab / Regression Protection / Bug Center

Three new screens finish out the engine work that had already landed in
`engine/*.py` and `agent.py` (Preview Manager, Feature Registry, Regression
Policy, Bug Store) but had no UI yet:

## UI Preview Lab (`/preview-lab`)
- Create isolated MESFlow environments (own app container, own Postgres, own
  Docker network) from a fixed dataset preset (`FULL_UI`, `NORMAL_FACTORY`,
  `PROBLEM_FACTORY`, `REPORT_30_DAYS`, `EMPTY_STATE`, `EDGE_CASES`).
- Every resource is labeled `com.mesflow.qa.preview=1`; destructive actions
  (stop/reset/delete) re-check that label plus `MESFLOW_UI_PREVIEW=1` on the
  app container before acting. Never touches `mesflow-app`/`mesflow-postgres`.
- Start/Stop/Reset-reseed/Delete per environment, plus a one-click UI
  Coverage run once an environment is READY.
- Shows the generated admin login (`admin` / random password) so a reviewer
  can actually open the environment.

## Regression Protection (`/regression`)
- Feature Registry table (status NEW→COVERED→STABLE, with CHANGED /
  REGRESSION_RISK / BROKEN branches) — STABLE only after enough consecutive
  passes, no open bug, no coverage gap, no pending mandatory regression.
- Run plan for FAST/REGRESSION/FULL modes — every feature's RUN/SKIP_STABLE
  decision always carries a human-readable reason (no silent skips).
- Impact detection: diff two git commits, map changed files onto each
  feature's declared `source_paths`, mark impacted features CHANGED.

## Bug Center (`/bugs`)
- Dedup by fingerprint(feature, error type, title, endpoint); a bug
  reappearing after RESOLVED becomes REGRESSION, never a fresh OPEN.
- Status machine enforced server-side: OPEN → FIXING → READY_FOR_VERIFY →
  (verify PASS) → RESOLVED. There is no "mark resolved" shortcut — a bug can
  only resolve through `verify(passed=True)`, which also permanently attaches
  the regression test case to its feature.
- Filters by feature/severity/status, evidence history per bug.

## Wiring
- `agent.py` routes were already in place; this pass adds
  `templates/{preview_lab,regression,bugs}.html`,
  `static/{preview_lab,regression,bugs}.js`, and shared
  `static/ops_pages.css` (loaded together with the existing `app.css`).
- Dashboard (`/`) now links to all three from a new launcher card, next to
  the Demo Presentation launcher.
- `tests/test_v1230_preview_regression_bugs.py` covers route/template/JS
  wiring plus the bug verify-before-resolve and resolved→regression state
  machine against an isolated throwaway sqlite DB.

## 1.23.1 fix-up — the Preview Lab actually needed Docker

1.23.0 shipped the UI but the container itself had no way to run the
`docker` commands `engine/preview_manager.py` shells out to:

- `current/docker/Dockerfile.base` now installs `docker-ce-cli` (client
  only, from Docker's own apt repo, same pattern already used for
  `postgresql-client-17`).
- `compose.yml` and `current/docker/compose.yml` now bind-mount
  `/var/run/docker.sock` — Docker-outside-of-Docker, the same pattern
  `deploy-agent` already uses to manage containers from inside a container.
- `DEFAULT_IMAGE` no longer hardcodes `mesflow-app:latest` (a tag that never
  exists in this project's version-pinned tagging — see
  `reports/BUILD_ONCE_LOCAL_ARCHITECTURE.md`). `PreviewManager.default_image()`
  now clones whatever image the real `mesflow-app` container is currently
  running, unless `MESFLOW_QA_PREVIEW_IMAGE` is set or the UI's "Image to
  clone" field is filled in with an explicit tag.
- Preview Lab UI gained an optional "MESFlow image để clone" field so a
  reviewer can pin a specific version instead of always cloning "whatever's
  running now".

## 1.23.3 fix-up — the cloned app crash-looped on boot

First real end-to-end run (1.23.2, Docker access now working) created a
`FULL_UI` environment that reached `FAILED`: the cloned `mesflow-app`
container exited immediately with
`RuntimeError: MESFLOW_SECRET_KEY must be configured for production`
(`mesflow/core/config.py` refuses to boot with `MESFLOW_ENV=production` and
no real secret key). `finish_create` now generates a fresh
`MESFLOW_SECRET_KEY` per preview and passes it to the cloned app container
-- never persisted, since the app container is never recreated after
creation (stop/start reuses the same container). Added
`test_finish_create_passes_a_real_secret_key_to_the_cloned_app` against a
fully faked Docker CLI so this can't silently regress again.
