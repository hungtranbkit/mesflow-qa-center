from pathlib import Path
import ast, json
ROOT=Path(__file__).resolve().parents[1]

def test_qa_version_and_runtime_mount():
    assert (ROOT/"VERSION").read_text().strip()=="1.19.5"
    c=(ROOT/"docker"/"compose.yml").read_text()
    assert "image: mesflow-qa-runtime:1.0.0" in c
    assert "- ..:/app:ro" in c
    assert "MESFLOW_QA_STATE_DIR: /data/state" in c
    assert "MESFLOW_QA_AUTO_RESUME: \"1\"" in c

def test_runtime_manifest_limits_rebuild_inputs():
    m=json.loads((ROOT/"docker"/"runtime-manifest.json").read_text())
    assert m["signature_files"]==["requirements.txt","docker/Dockerfile"]

def test_web_app_persists_and_resumes_realtime_run():
    t=(ROOT/"agent.py").read_text()
    ast.parse(t)
    assert 'ACTIVE_RUN_FILE = STATE_DIR / "active_run.json"' in t
    assert "_resume_persistent_run_after_startup" in t
    assert '"auto_resume": True' in t
    assert 'active["auto_resume"] = False' in t

def test_soak_waits_and_persists():
    t=(ROOT/"scenarios"/"realtime_factory_soak_test.py").read_text()
    ast.parse(t)
    assert 'RUNTIME_DIR=Path(os.environ.get("MESFLOW_QA_RUNTIME_DIR"' in t
    assert '"WAITING_MESFLOW"' in t
    assert "_connect_context" in t
    assert "paused_seconds" in t
    assert "SIM-RESUME-GAP" in t
