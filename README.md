# MESFlow QA Center 1.22.14


## v1.22.14 — Fast Cached QA Builds

- Splits heavy Playwright/Chromium/PostgreSQL/Python dependencies into a fingerprinted local base image.
- `scripts/build-release.sh` builds that base once, then reuses it while `Dockerfile.base` + `requirements.txt` are unchanged.
- A normal QA version bump/source change now builds only a thin application layer; dependency changes automatically invalidate the base fingerprint.
- The immutable Build Once / Promote Same Artifact release contract is unchanged.

## v1.22.13 — Demo Execution Monitor

- Test Case Monitor hiển thị mục tiêu, input và expected output trước/during demo.
- Realtime browser activity hiển thị NAVIGATE/FOCUS/TYPE/SELECT/CLICK/WAIT.
- Heartbeat cảnh báo khi action không cập nhật lâu, giúp phân biệt đang chờ với bị treo.
- Danh sách testcase có planned/running/pass/fail và phần trăm tiến trình.


## Install source into the MESFlow workspace

This release package follows the ProjectFlow/Deploy Agent workflow. The installer only places source code into the standard workspace; it does not build or deploy QA Center.

```bash
unzip qa-center-v1.22.3-workspace-only-install.zip
cd qa-center
sudo ./install.sh
```

Default destination:

```text
~/workspace/mesflow/qa-center
```

For the normal `dell` account this resolves to:

```text
/home/dell/workspace/mesflow/qa-center
```

When run through `sudo`, `install.sh` resolves `SUDO_USER` and copies into that user's workspace instead of `/root/workspace`.

The installer preserves local-only `.env`, `.env.local`, and `config.local.json` when they already exist in the workspace. Runtime/build residue is not copied.

### Deliberately not performed by install.sh

- no `docker build`
- no `docker compose up/restart`
- no network creation
- no systemd changes
- no database migrations
- no local/staging/production deployment

Build and deployment are performed afterward by Deploy Agent / ProjectFlow from the workspace source.

## Demo Center

The separate Demo Center remains available in this source release, including editable MESFlow target, credential preflight, Auto/Presenter/Manual modes, pause/resume and screenshot review controls.

## Database cleanup

The QA Center UI still includes the guarded **Xóa sạch Database Test** workflow. Database access/cleanup is a runtime capability of QA Center after Deploy Agent deploys it; `install.sh` itself never connects to or modifies the database.
## Safe QA Database Reset (v1.22.11)

Full table cleanup is disabled. Configure a dedicated disposable QA database and an immutable Golden Template:

- `MESFLOW_QA_DATABASE_URL=postgresql://...@postgres:5432/mesflow_qa`
- `MESFLOW_QA_TEMPLATE_DATABASE=mesflow_qa_template`
- `MESFLOW_QA_DB_ALLOW_RESET=1`
- `MESFLOW_QA_DB_ALLOWED_NAMES=mesflow_qa,mesflow_test,mesflow_demo`

Protected names (`mesflow`, `postgres`, `template0`, `template1`) are hard-blocked. Reset uses `DROP DATABASE` + `CREATE DATABASE ... TEMPLATE`, then verifies master/config invariants and zero runtime rows. Legacy cleanup APIs are disabled.


## v1.22.11 — Demo Database Automation

QA Center now standardizes demo database preparation in the UI. Configure the PostgreSQL URL once, then use **Chuẩn bị Demo DB** to clone the source database into a disposable demo template, clean runtime data only on that clone, verify master/config invariants, and create `mesflow_demo`. Use **Reset Demo DB** to return to that verified baseline. The `mesflow` source database is read/clone-only and is never passed to the destructive cleanup helper.

The same guarded implementation is available through `current/scripts/db-demo-workflow.py`, `db-demo-prepare.sh`, and `db-demo-reset.sh`.
