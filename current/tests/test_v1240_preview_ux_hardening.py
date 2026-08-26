"""v1.24.0 -- Preview Lab UX overhaul + hardening pass.

Covers the things that changed behind the new UI:
  - a named Docker volume for the preview's Postgres data (stop/start must
    never lose it; delete must remove it, but only with the right labels),
  - internal readiness/coverage checks going over the preview's own Docker
    network by container name (never host.docker.internal), so they work
    regardless of PREVIEW_BIND_HOST,
  - configurable bind/public host for the published port,
  - "Làm mới" (refresh) being provably read-only,
  - deeper unit coverage for fingerprint dedup, the expected-state manifest,
    and the regression policy's SKIP_STABLE / CHANGED / coverage-gap rules.

All Docker interaction is through a fake, injectable runner -- nothing here
needs, or should ever need, a real Docker daemon.
"""
from __future__ import annotations

from pathlib import Path

import pytest
pytest.importorskip("flask")
import agent
from engine import qa_store, bug_store, feature_registry, regression_policy, preview_manager, fingerprint
from engine.preview import expectations as preview_expectations
from engine.preview import seed as preview_seed
from engine.preview_guard import PreviewSafetyError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_meta_db(tmp_path):
    qa_store.reset_for_tests(tmp_path / "qa_meta_test.sqlite3")
    yield
    qa_store.reset_for_tests(tmp_path / "qa_meta_test.sqlite3")


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _label_json(**kv):
    import json as _json
    return _json.dumps(kv)


def _default_fake_handler(cmd):
    """Shared fallback logic for the fake docker runner: labels/env inspects
    report a valid preview resource, everything else just succeeds. Returns
    None to mean "no opinion, use the generic 'ok' success"."""
    verb = cmd[0]
    if verb == "inspect" and len(cmd) >= 3 and "Labels" in cmd[2]:
        return _FakeResult(stdout=_label_json(**{"com.mesflow.qa.preview": "1"}) + "\n")
    if verb == "inspect" and len(cmd) >= 3 and "Env" in cmd[2]:
        return _FakeResult(stdout='["MESFLOW_UI_PREVIEW=1"]\n')
    if verb == "network" and cmd[1] == "inspect":
        return _FakeResult(stdout=_label_json(**{"com.mesflow.qa.preview": "1"}) + "\n")
    if verb == "volume" and cmd[1] == "inspect":
        return _FakeResult(stdout="null\n")
    return None


def _fake_ready_env(monkeypatch, tmp_path, *, calls=None, extra_handler=None):
    """Build a PreviewManager backed by a fake docker CLI, create+finish one
    environment, and return (mgr, env, calls). `calls` records every raw
    `docker ...` argv the manager issued. `extra_handler(cmd) -> result|None`
    gets first refusal on each call, falling back to `_default_fake_handler`."""
    calls = calls if calls is not None else []

    def fake_runner(args, timeout=60):
        calls.append(args)
        cmd = args[1:]
        if extra_handler is not None:
            result = extra_handler(cmd)
            if result is not None:
                return result
        result = _default_fake_handler(cmd)
        if result is not None:
            return result
        return _FakeResult(stdout="ok\n")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True, "version": "9.9.9"}

    monkeypatch.setattr(preview_manager.requests, "get", lambda *a, **k: FakeResp())
    monkeypatch.setattr(preview_manager.preview_seed, "run_seed", lambda database_url, preset: {"seed_version": "x"})
    mgr = preview_manager.PreviewManager.build(
        docker=preview_manager.DockerCLI(runner=fake_runner), self_container="qa-center-test",
    )
    env_id = mgr.begin_create("EMPTY_STATE", image="mesflow-app:test")
    env = mgr.finish_create(env_id)
    return mgr, env, calls


# --------------------------------------------------------------------------
# Named volume persistence (requirement 7)
# --------------------------------------------------------------------------

def test_finish_create_creates_a_labeled_volume_and_mounts_it_into_the_db(monkeypatch, tmp_path):
    mgr, env, calls = _fake_ready_env(monkeypatch, tmp_path)
    assert env["status"] == "READY"
    assert env["volume"] == f"mesflow-ui-data-{env['id']}"

    vol_create = next(c for c in calls if c[1:3] == ["volume", "create"])
    assert f"com.mesflow.qa.preview=1" in vol_create
    assert f"com.mesflow.qa.preview.id={env['id']}" in vol_create
    assert vol_create[-1] == env["volume"]

    db_run = next(c for c in calls if c[1] == "run" and env["db_container"] in c)
    assert "-v" in db_run
    idx = db_run.index("-v")
    assert db_run[idx + 1] == f"{env['volume']}:/var/lib/postgresql/data"


def test_delete_removes_the_volume_only_with_a_matching_id_label(monkeypatch, tmp_path):
    calls = []
    state = {}

    def handler(cmd):
        if cmd[0] == "volume" and cmd[1] == "inspect":
            return _FakeResult(stdout=state["volume_labels"])
        return None

    mgr, env, _ = _fake_ready_env(monkeypatch, tmp_path, calls=calls, extra_handler=handler)
    state["volume_labels"] = _label_json(**{
        "com.mesflow.qa.preview": "1", "com.mesflow.qa.preview.id": env["id"],
    })

    mgr.delete(env["id"])
    assert any(c[1:3] == ["volume", "rm"] and env["volume"] in c for c in calls)
    assert mgr.get(env["id"])["status"] == "DELETED"


def test_delete_refuses_to_remove_a_volume_whose_id_label_does_not_match(monkeypatch, tmp_path):
    calls = []

    def handler(cmd):
        if cmd[0] == "volume" and cmd[1] == "inspect":
            # Wrong id -- simulates a bug elsewhere computing the wrong name.
            return _FakeResult(stdout=_label_json(**{
                "com.mesflow.qa.preview": "1", "com.mesflow.qa.preview.id": "SOME-OTHER-ENV",
            }))
        return None

    mgr, env, _ = _fake_ready_env(monkeypatch, tmp_path, calls=calls, extra_handler=handler)
    calls.clear()

    with pytest.raises(PreviewSafetyError):
        mgr.docker.safe_remove_volume(env["volume"], env["id"])
    assert not any(c[1:3] == ["volume", "rm"] for c in calls)


def test_refuse_to_remove_a_container_missing_the_preview_label():
    def unlabeled_runner(args, timeout=60):
        cmd = args[1:]
        if cmd[0] == "inspect" and len(cmd) >= 3 and "Labels" in cmd[2]:
            return _FakeResult(stdout="null\n")  # no labels at all
        return _FakeResult(stdout="ok\n")

    docker = preview_manager.DockerCLI(runner=unlabeled_runner)
    with pytest.raises(PreviewSafetyError):
        docker.safe_remove_container("some-container")
    with pytest.raises(PreviewSafetyError):
        docker.safe_stop_container("some-container")


def test_refuse_to_remove_a_network_missing_the_preview_label():
    def unlabeled_runner(args, timeout=60):
        cmd = args[1:]
        if cmd[0] == "network" and cmd[1] == "inspect":
            return _FakeResult(stdout="null\n")
        return _FakeResult(stdout="ok\n")

    docker = preview_manager.DockerCLI(runner=unlabeled_runner)
    with pytest.raises(PreviewSafetyError):
        docker.safe_remove_network("some-network")


def test_stop_then_start_never_recreates_containers_or_touches_the_volume(monkeypatch, tmp_path):
    mgr, env, calls = _fake_ready_env(monkeypatch, tmp_path)
    calls.clear()

    mgr.stop(env["id"])
    mgr.start(env["id"])

    mutating_verbs = {("run",), ("rm",), ("volume", "rm"), ("volume", "create"), ("network", "rm")}
    seen = {tuple(c[1:1 + len(v)]) for c in calls for v in [c[1:2]]}
    for c in calls:
        verb = tuple(c[1:3]) if c[1] in ("volume", "network") else (c[1],)
        assert verb not in mutating_verbs, f"stop/start issued a mutating command: {c}"
    # Sanity: it did actually do something (stop x2, start x2, readiness poll).
    assert any(c[1] == "stop" for c in calls)
    assert any(c[1] == "start" for c in calls)


# --------------------------------------------------------------------------
# Internal networking never depends on the published host port (requirement 6)
# --------------------------------------------------------------------------

def test_internal_base_url_uses_the_container_name_never_host_docker_internal(monkeypatch, tmp_path):
    mgr, env, _ = _fake_ready_env(monkeypatch, tmp_path)
    url = mgr.internal_base_url(env["id"])
    assert url == f"http://{env['app_container']}:8080"
    assert "host.docker.internal" not in url


def test_base_url_respects_configurable_public_host(monkeypatch, tmp_path):
    monkeypatch.setattr(preview_manager, "PREVIEW_PUBLIC_HOST", "192.0.2.10")
    mgr, env, _ = _fake_ready_env(monkeypatch, tmp_path)
    assert mgr.base_url(env["id"]) == f"http://192.0.2.10:{env['port']}"


def test_app_container_binds_to_127_0_0_1_by_default(monkeypatch, tmp_path):
    mgr, env, calls = _fake_ready_env(monkeypatch, tmp_path)
    app_run = next(c for c in calls if c[1] == "run" and env["app_container"] in c)
    assert f"127.0.0.1:{env['port']}:8080" in app_run


def test_database_url_always_targets_the_mesflow_ui_prefixed_database(monkeypatch, tmp_path):
    mgr, env, calls = _fake_ready_env(monkeypatch, tmp_path)
    app_run = next(c for c in calls if c[1] == "run" and env["app_container"] in c)
    db_url = next(app_run[i + 1] for i in range(len(app_run)) if app_run[i] == "-e" and app_run[i + 1].startswith("DATABASE_URL="))
    assert "/mesflow_ui_" in db_url


# --------------------------------------------------------------------------
# Refresh must never mutate (requirement 2 / 21)
# --------------------------------------------------------------------------

def test_refresh_endpoints_never_issue_a_mutating_docker_command(monkeypatch):
    calls = []
    READ_ONLY_VERBS = {"inspect"}

    def readonly_runner(args, timeout=60):
        calls.append(args)
        cmd = args[1:]
        if cmd[0] == "volume" and cmd[1] == "inspect":
            return _FakeResult(returncode=1, stderr="no such volume")
        if cmd[0] not in READ_ONLY_VERBS:
            raise AssertionError(f"refresh must be read-only, got mutating call: {args}")
        return _FakeResult(returncode=1, stderr="no such object")  # container doesn't really exist -- fine

    # Insert a row directly (no real docker involved in setup) so the routes
    # have something to enrich.
    conn = qa_store.connect()
    conn.execute(
        """INSERT INTO preview_environments(id,image,preset,port,status,db_name,app_container,
               db_container,network,volume,admin_password,db_password,mesflow_version,seed_version,
               created_at,updated_at)
           VALUES('abcd1234','mesflow-app:test','EMPTY_STATE',18080,'READY','mesflow_ui_abcd1234',
               'mesflow-ui-preview-abcd1234','mesflow-ui-db-abcd1234','mesflow-ui-net-abcd1234',
               'mesflow-ui-data-abcd1234','pw','pw','1.0.0','1','2026-01-01T00:00:00','2026-01-01T00:00:00')""",
    )
    conn.commit()

    original_docker = agent._preview_mgr.docker
    monkeypatch.setattr(agent._preview_mgr, "docker", preview_manager.DockerCLI(runner=readonly_runner))
    try:
        client = agent.app.test_client()
        r = client.get("/api/preview/environments")
        assert r.status_code == 200 and r.json["ok"] is True
        assert r.json["environments"][0]["runtime"]["app_status"] == "MISSING"

        r2 = client.get("/api/preview/environments/abcd1234")
        assert r2.status_code == 200 and r2.json["environment"]["runtime"]["db_status"] == "MISSING"
    finally:
        agent._preview_mgr.docker = original_docker

    assert calls, "expected the refresh routes to actually query docker"


def test_runtime_state_never_raises_without_a_real_docker_daemon(monkeypatch, tmp_path):
    def broken_runner(args, timeout=60):
        raise FileNotFoundError("docker: command not found")

    mgr = preview_manager.PreviewManager.build(docker=preview_manager.DockerCLI(runner=broken_runner))
    conn = qa_store.connect()
    conn.execute(
        """INSERT INTO preview_environments(id,image,preset,port,status,db_name,app_container,
               db_container,network,volume,admin_password,db_password,created_at,updated_at)
           VALUES('zz1','x','EMPTY_STATE',1,'READY','mesflow_ui_zz1','app-zz1','db-zz1','net-zz1',
               'vol-zz1','pw','pw','2026-01-01T00:00:00','2026-01-01T00:00:00')""",
    )
    conn.commit()
    state = mgr.runtime_state("zz1")
    assert state == {"app_status": "UNKNOWN", "db_status": "UNKNOWN", "health": None}


# --------------------------------------------------------------------------
# Bug fingerprint / dedup (requirement 12)
# --------------------------------------------------------------------------

def test_fingerprint_normalizes_volatile_noise():
    a = "Request req-abc123f at 2026-08-22T14:20:32Z failed, pid=48213, id=193044"
    b = "Request req-999zzzz at 2026-08-23T09:01:00.123Z failed, pid=10029, id=284719"
    assert fingerprint.normalize(a) == fingerprint.normalize(b)
    assert fingerprint.compute("f", "E", a, "/api/x") == fingerprint.compute("f", "E", b, "/api/x")


def test_duplicate_bug_increments_occurrences_without_creating_a_new_record():
    conn = qa_store.connect()
    first = bug_store.record_bug(feature="f1", error_type="E", title="boom", conn=conn)
    second = bug_store.record_bug(feature="f1", error_type="E", title="boom", conn=conn)
    assert first["bug_id"] == second["bug_id"]
    assert second["occurrences"] == 2
    assert len(bug_store.list_bugs(conn=conn)) == 1


# --------------------------------------------------------------------------
# Regression policy: SKIP_STABLE / CHANGED / coverage gap (requirement 9/10)
# --------------------------------------------------------------------------

def _make_stable_feature(key: str) -> None:
    feature_registry.upsert_feature(key, source_paths=["app/x.py"], test_cases=["t.one"])
    for _ in range(3):
        feature_registry.record_result(key, "t.one", "PASS")


def test_untouched_stable_feature_is_skipped_with_an_explicit_reason():
    _make_stable_feature("f.stable")
    assert feature_registry.get_feature("f.stable")["status"] == feature_registry.STABLE
    plan = regression_policy.compute_run_plan(regression_policy.FAST)
    decision = next(d for d in plan["decisions"] if d["feature"] == "f.stable")
    assert decision["action"] == regression_policy.SKIP_STABLE
    assert decision["reason"] == "No related source changes since last verified commit"


def test_changed_stable_feature_requires_regression():
    _make_stable_feature("f.changed")
    feature_registry.mark_changed("f.changed", commit_sha="deadbeef")
    feature = feature_registry.get_feature("f.changed")
    assert feature["status"] == feature_registry.CHANGED
    assert feature["needs_regression"] is True
    plan = regression_policy.compute_run_plan(regression_policy.FAST)
    decision = next(d for d in plan["decisions"] if d["feature"] == "f.changed")
    assert decision["action"] == regression_policy.RUN
    assert decision["reason"] == "status=CHANGED"


def test_coverage_gap_blocks_stable_even_with_enough_passes():
    key = "f.gap"
    feature_registry.upsert_feature(key, source_paths=["app/x.py"], test_cases=["t.one"])
    feature_registry.declare_behavior(key, "new_branch")  # no test covers "new_branch" -> gap
    for _ in range(5):
        feature_registry.record_result(key, "t.one", "PASS")
    feature = feature_registry.get_feature(key)
    assert feature["coverage_gap"] is True
    assert feature["status"] != feature_registry.STABLE


# --------------------------------------------------------------------------
# Expected-state manifest (requirement 16)
# --------------------------------------------------------------------------

def test_expectations_validate_flags_a_real_shortfall():
    from datetime import datetime as _dt
    manifest = preview_expectations.build_manifest(
        "FULL_UI", _dt(2026, 1, 1),
        {"po_late": 2, "po_warning": 1, "po_completed": 3, "sessions_open": 4,
         "sessions_long_open": 1, "sessions_closed": 30, "exceptions": 2},
    )
    ok_observed = {
        "production_orders": {"late": 2, "warning": 1, "completed": 3},
        "sessions": {"open": 4, "long_open": 1, "closed": 30},
        "exceptions": {"count": 2},
    }
    assert preview_expectations.validate(manifest, ok_observed) == []

    bad_observed = {
        "production_orders": {"late": 0, "warning": 1, "completed": 3},
        "sessions": {"open": 4, "long_open": 1, "closed": 30},
        "exceptions": {"count": 2},
    }
    mismatches = preview_expectations.validate(manifest, bad_observed)
    assert any(m["path"] == "production_orders.late" for m in mismatches)


def test_wipe_and_seed_refuse_a_non_preview_database_url():
    with pytest.raises(PreviewSafetyError):
        preview_seed.wipe_runtime_data("postgresql://mesflow:pw@host:5432/mesflow")
    with pytest.raises(PreviewSafetyError):
        preview_seed.run_seed("postgresql://mesflow:pw@host:5432/mesflow", "EMPTY_STATE")


# --------------------------------------------------------------------------
# Version contract stays consistent after this round of changes.
# --------------------------------------------------------------------------

def test_regression_features_endpoint_exposes_regression_cases():
    conn = qa_store.connect()
    feature_registry.upsert_feature("f.rc", source_paths=["app/x.py"], test_cases=["t.one"])
    bug = bug_store.record_bug(feature="f.rc", error_type="E", title="boom", conn=conn)
    bug_store.mark_fixing(bug["bug_id"], conn=conn)
    bug_store.mark_ready_for_verify(bug["bug_id"], conn=conn)
    bug_store.verify(bug["bug_id"], True, test_case="f.rc.regression_1", conn=conn)

    client = agent.app.test_client()
    r = client.get("/api/regression/features")
    assert r.status_code == 200
    feature = next(f for f in r.json["features"] if f["key"] == "f.rc")
    assert feature["regression_cases"] == ["f.rc.regression_1"]


def test_coverage_screenshot_route_rejects_bad_names_and_missing_files():
    client = agent.app.test_client()
    # A run_id/name that reaches the handler (no "/") but fails the strict
    # allowlist -- e.g. an embedded ".." segment or a non-.png extension.
    r = client.get("/api/preview/coverage/cov-abc..123/screenshot/passwd.png")
    assert r.status_code == 400
    r2 = client.get("/api/preview/coverage/cov-abc123/screenshot/shot.txt")
    assert r2.status_code == 400
    r3 = client.get("/api/preview/coverage/cov-abc123/screenshot/nope.png")
    assert r3.status_code == 404


def test_capture_screenshot_is_best_effort_and_never_raises():
    from engine import coverage_runner

    class BrokenPage:
        def screenshot(self, **kwargs):
            raise RuntimeError("no display")

    assert coverage_runner._capture_screenshot(BrokenPage(), "cov-test", "01-login") == ""


# --------------------------------------------------------------------------
# UX copy contract (requirement 1/3/4): the operator-facing wording the task
# asked for, locked in so a future edit can't silently drift back to raw
# Docker/implementation language on the main screen.
# --------------------------------------------------------------------------

def test_preview_lab_page_uses_operator_friendly_copy_not_docker_jargon():
    html = (ROOT / "templates" / "preview_lab.html").read_text(encoding="utf-8")
    assert "Môi trường Preview" in html
    assert "Tách biệt hoàn toàn với MESFlow hiện tại" in html
    assert "ISOLATED PREVIEW" in html
    # The raw docker label must not appear on the main screen -- only inside
    # the collapsible technical details, which lives in the JS template.
    assert "com.mesflow.qa.preview=1" not in html


def test_preview_lab_js_defines_every_required_phase_and_action_state():
    js = (ROOT / "static" / "preview_lab.js").read_text(encoding="utf-8")
    for marker in (
        "Chưa có môi trường Preview", "Start Preview",
        "Đang tạo database và container riêng", "Đang tạo dữ liệu Preview",
        "Không thể tạo môi trường Preview", "Xem lỗi", "Delete Environment",
        "Open Preview", "Run UI Coverage", "Reset / Re-seed", "Start lại",
        "com.mesflow.qa.preview=1",  # present, but only inside tech details
    ):
        assert marker in js, marker


def test_seed_no_longer_inserts_the_nonexistent_operations_plan_qty_column():
    # Regression: caught live by the Docker smoke test -- every preset except
    # EMPTY_STATE crashed seeding with "column plan_qty does not exist"
    # (operations has no per-operation planned-quantity column; planned
    # quantity lives only on production_orders). A plain unit test can't
    # check a real Postgres schema, so this at least locks the fix in text
    # form: nothing in the seed script may reference that column again.
    text = (ROOT / "engine" / "preview" / "seed.py").read_text(encoding="utf-8")
    assert "plan_qty" not in text


def test_coverage_as_date_parses_the_real_api_http_date_format():
    # Regression: caught live -- /api/dashboard/production-orders serializes
    # due_date as an RFC 1123 HTTP-date ("Thu, 20 Aug 2026 00:00:00 GMT"),
    # not ISO 8601. The original ISO-only parser silently returned None for
    # every real due_date, so the "late production orders" REPORT_MISMATCH
    # check could never fire even when the dashboard was genuinely wrong.
    from datetime import date
    from engine.coverage_runner import _as_date
    assert _as_date("Thu, 20 Aug 2026 00:00:00 GMT") == date(2026, 8, 20)
    assert _as_date("2026-08-20") == date(2026, 8, 20)  # still accepts plain ISO
    assert _as_date("") is None
    assert _as_date(None) is None
    assert _as_date("not a date") is None


def test_coverage_login_wait_does_not_rely_on_load_state_alone():
    # Regression: caught live -- MESFlow's login page submits via fetch() +
    # location.href (static/login.js), not a normal <form> POST.
    # wait_for_load_state("domcontentloaded") can return immediately against
    # the pre-navigation page before the async fetch resolves, so the
    # "still on /login" check misfired on logins that actually succeeded a
    # moment later. Locks in that the fix (wait for the URL itself) stays.
    text = (ROOT / "engine" / "coverage_runner.py").read_text(encoding="utf-8")
    assert "wait_for_url" in text


def test_new_config_env_vars_are_documented_in_the_module_and_compose():
    text = (ROOT / "engine" / "preview_manager.py").read_text(encoding="utf-8")
    for name in ("MESFLOW_PREVIEW_BIND_HOST", "MESFLOW_PREVIEW_PUBLIC_HOST", "MESFLOW_PREVIEW_PORT_START"):
        assert name in text
