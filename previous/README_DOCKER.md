# MESFlow QA Center 1.19.5 — Fast Deploy + Resumable Multi-day

## Fast deploy

The heavy runtime image contains Python dependencies, Playwright and Chromium.
QA source is bind-mounted from `/opt/mesflow-qa-center/current`.

A normal source-only QA release therefore does **not** rebuild Chromium or pip
dependencies. Deploy Agent 2.12.3 compares the runtime signature and uses
`docker compose up -d --no-build --force-recreate` when unchanged.

Only changes to `requirements.txt` or `docker/Dockerfile` rebuild the runtime.

## Multi-day campaign persistence

Persistent files live under:

```text
/opt/mesflow-qa-center/runtime/
  config.json
  logs/
  reports/
  state/
    active_run.json
    realistic_soak_state.json
```

If QA Center is redeployed/restarted while a realtime multi-day campaign is
RUNNING, 1.19.5 automatically resumes it after startup. A user-requested Stop
sets `auto_resume=false`, so it will not restart unexpectedly.

The scenario reconciles its persisted sessions with MESFlow before continuing.

## MESFlow outage behavior

A transient MESFlow outage no longer ends the multi-day campaign. QA enters
`WAITING_MESFLOW`, saves state, retries the internal MESFlow URL, and resumes
after health/auth are available again.

MESFlow/QA downtime is accumulated as `paused_seconds` and does not consume the
configured campaign duration.
