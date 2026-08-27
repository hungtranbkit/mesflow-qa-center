"""v1.23.0 -- UI Preview Lab / Regression Protection / Bug Center.

Keeps two things honest:
  1. the three new screens actually exist and are wired end to end
     (route -> template -> static JS -> the real API contract), and
  2. the underlying engine modules behave correctly against an isolated
     throwaway sqlite DB (never the real QA Center metadata DB).
"""
from pathlib import Path
import pytest
pytest.importorskip("flask")
import agent
from engine import qa_store, bug_store, feature_registry, regression_policy, preview_manager
from version_contract import assert_version_contract

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_meta_db(tmp_path):
    qa_store.reset_for_tests(tmp_path / "qa_meta_test.sqlite3")
    yield
    qa_store.reset_for_tests(tmp_path / "qa_meta_test.sqlite3")


def test_version_contract_is_internally_consistent():
    # Never hardcode "the current version" here -- this file's own release
    # gate re-runs this suite on every version bump, so a pinned string
    # would fail the very next bump for no real reason. Just require every
    # declared-version location (VERSION, agent.py, install.sh, compose.yml)
    # to agree with each other.
    expected = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    assert assert_version_contract(ROOT) == expected


def test_three_pages_render():
    c = agent.app.test_client()
    for path, marker in (
        ("/preview-lab", "UI Preview Lab"),
        ("/regression", "Regression Protection"),
        ("/bugs", "Bug Center"),
    ):
        r = c.get(path)
        assert r.status_code == 200, path
        assert marker in r.get_data(as_text=True)
        assert "no-store" in r.headers.get("Cache-Control", "")


def test_index_links_to_all_three_screens():
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    for href in ("/preview-lab", "/regression", "/bugs"):
        assert f'href="{href}"' in html


def test_preview_presets_endpoint_lists_all_specs():
    c = agent.app.test_client()
    r = c.get("/api/preview/presets")
    assert r.status_code == 200 and r.json["ok"] is True
    keys = {p["key"] for p in r.json["presets"]}
    assert keys == {"FULL_UI", "NORMAL_FACTORY", "PROBLEM_FACTORY", "REPORT_30_DAYS", "EMPTY_STATE", "EDGE_CASES"}


def test_preview_create_rejects_unknown_preset():
    c = agent.app.test_client()
    r = c.post("/api/preview/environments", json={"preset": "NOT_A_PRESET"})
    assert r.status_code == 400 and r.json["error"] == "UNKNOWN_PRESET"


def test_preview_get_missing_environment_is_404():
    c = agent.app.test_client()
    r = c.get("/api/preview/environments/doesnotexist")
    assert r.status_code == 404 and r.json["error"] == "NOT_FOUND"


def test_regression_features_bootstraps_defaults():
    c = agent.app.test_client()
    r = c.get("/api/regression/features")
    assert r.status_code == 200 and r.json["ok"] is True
    keys = {f["key"] for f in r.json["features"]}
    assert {"dashboard.production_overview", "session.finish"} <= keys
    for f in r.json["features"]:
        assert f["status"] == feature_registry.NEW


def test_regression_plan_fast_mode_runs_non_stable_features():
    c = agent.app.test_client()
    r = c.get("/api/regression/plan?mode=FAST")
    assert r.status_code == 200 and r.json["ok"] is True
    assert r.json["mode"] == "FAST"
    # Nothing is STABLE yet (bootstrapped as NEW), so FAST must run everything
    # and never silently skip -- every decision carries a reason.
    assert r.json["run_count"] == len(r.json["decisions"])
    assert all(d["reason"] for d in r.json["decisions"])


def test_regression_plan_rejects_invalid_mode():
    c = agent.app.test_client()
    r = c.get("/api/regression/plan?mode=BOGUS")
    assert r.status_code == 400 and r.json["error"] == "INVALID_MODE"


def test_regression_impact_requires_previous_commit():
    c = agent.app.test_client()
    r = c.post("/api/regression/impact", json={})
    assert r.status_code == 400 and r.json["error"] == "MISSING_PREVIOUS_COMMIT"


def test_bug_lifecycle_requires_verify_before_resolve():
    conn = qa_store.connect()
    bug = bug_store.record_bug(feature="session.finish", error_type="UI_ERROR", title="Finish button 500s", conn=conn)
    assert bug["status"] == bug_store.OPEN

    c = agent.app.test_client()
    bug_id = bug["bug_id"]

    # Can't verify before it's even being worked on.
    r = c.post(f"/api/bugs/{bug_id}/verify", json={"passed": True, "test_case": "x"})
    assert r.status_code == 409 and r.json["error"] == "INVALID_TRANSITION"

    assert c.post(f"/api/bugs/{bug_id}/fixing").json["bug"]["status"] == bug_store.FIXING
    assert c.post(f"/api/bugs/{bug_id}/ready-for-verify").json["bug"]["status"] == bug_store.READY_FOR_VERIFY

    r = c.post(f"/api/bugs/{bug_id}/verify", json={"passed": True, "test_case": "session.finish.ui_500"})
    assert r.status_code == 200
    assert r.json["bug"]["status"] == bug_store.RESOLVED

    # A resolved bug reappearing is always a regression, never a fresh OPEN.
    again = bug_store.record_bug(feature="session.finish", error_type="UI_ERROR", title="Finish button 500s", conn=conn)
    assert again["bug_id"] == bug_id
    assert again["status"] == bug_store.REGRESSION


def test_bugs_summary_counts_by_status():
    conn = qa_store.connect()
    bug_store.record_bug(feature="f1", error_type="E", title="t1", conn=conn)
    bug_store.record_bug(feature="f2", error_type="E", title="t2", conn=conn)
    c = agent.app.test_client()
    r = c.get("/api/bugs/summary")
    assert r.status_code == 200 and r.json["counts"].get(bug_store.OPEN) == 2


class _FakeResult:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_default_image_uses_env_override_without_touching_docker(monkeypatch):
    monkeypatch.setattr(preview_manager, "DEFAULT_IMAGE", "mesflow-app:pinned")

    def boom(args, timeout=60):
        raise AssertionError("must not shell out to docker when an override is set")

    mgr = preview_manager.PreviewManager.build(docker=preview_manager.DockerCLI(runner=boom))
    assert mgr.default_image() == "mesflow-app:pinned"


def test_default_image_falls_back_to_the_running_mesflow_app(monkeypatch):
    monkeypatch.setattr(preview_manager, "DEFAULT_IMAGE", "")
    calls = []

    def fake_runner(args, timeout=60):
        calls.append(args)
        return _FakeResult(stdout="mesflow-app:71.0.0.52\n")

    mgr = preview_manager.PreviewManager.build(docker=preview_manager.DockerCLI(runner=fake_runner))
    assert mgr.default_image() == "mesflow-app:71.0.0.52"
    assert calls[0] == ["docker", "inspect", "--format", "{{.Config.Image}}", "mesflow-app"]


def test_default_image_raises_a_clear_error_when_mesflow_app_is_not_running(monkeypatch):
    monkeypatch.setattr(preview_manager, "DEFAULT_IMAGE", "")

    def fake_runner(args, timeout=60):
        return _FakeResult(returncode=1, stderr="Error: No such object: mesflow-app")

    mgr = preview_manager.PreviewManager.build(docker=preview_manager.DockerCLI(runner=fake_runner))
    with pytest.raises(preview_manager.NoDefaultImageError):
        mgr.default_image()


def test_finish_create_passes_a_real_secret_key_to_the_cloned_app(monkeypatch, tmp_path):
    # Regression: a cloned mesflow-app boots with MESFLOW_ENV=production, and
    # mesflow.core.config refuses to start in production without a real
    # MESFLOW_SECRET_KEY -- caught live when the very first preview
    # environment crash-looped on exactly this.
    qa_store.reset_for_tests(tmp_path / "qa_meta_secret_key.sqlite3")
    calls = []

    def fake_runner(args, timeout=60):
        calls.append(args)
        cmd = args[1:]
        if cmd[0] == "inspect" and "Labels" in cmd[2]:
            return _FakeResult(stdout='{"com.mesflow.qa.preview":"1"}\n')
        return _FakeResult(stdout="ok\n")  # network create/connect, run, exec all just need returncode 0

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
    assert env["status"] == "READY"

    app_run = next(c for c in calls if c[1] == "run" and env["app_container"] in c)
    env_flags = {app_run[i + 1].split("=", 1)[0]: app_run[i + 1].split("=", 1)[1]
                 for i in range(len(app_run)) if app_run[i] == "-e"}
    assert "MESFLOW_SECRET_KEY" in env_flags and len(env_flags["MESFLOW_SECRET_KEY"]) >= 32
    assert env_flags.get("MESFLOW_ENV") == "production"


def test_dockerfile_base_installs_docker_cli_for_the_preview_lab():
    text = (ROOT / "docker" / "Dockerfile.base").read_text(encoding="utf-8")
    assert "docker-ce-cli" in text


def test_both_compose_files_mount_the_docker_socket():
    for rel_root, rel_path in ((ROOT.parent, "compose.yml"), (ROOT, "docker/compose.yml")):
        text = (rel_root / rel_path).read_text(encoding="utf-8")
        assert "/var/run/docker.sock:/var/run/docker.sock" in text, rel_path


@pytest.mark.parametrize("page,js", [
    ("preview_lab.html", "preview_lab.js"),
    ("regression.html", "regression.js"),
    ("bugs.html", "bugs.js"),
])
def test_each_page_links_its_own_static_assets(page, js):
    html = (ROOT / "templates" / page).read_text(encoding="utf-8")
    assert "/static/app.css" in html
    assert "/static/ops_pages.css" in html
    assert f"/static/{js}" in html
