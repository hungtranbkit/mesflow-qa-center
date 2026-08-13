from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
def test_no_auto_login_in_qa_runtime():
    files=[ROOT/"agent.py",ROOT/"scenarios"/"lifecycle_core.py",ROOT/"scenarios"/"realistic_factory_simulation.py"]
    for p in files:
        t=p.read_text(encoding="utf-8")
        assert "/api/auth/test-auto-login" not in t
def test_session_verified_after_password_login():
    t=(ROOT/"scenarios"/"lifecycle_core.py").read_text(encoding="utf-8")
    assert "/api/auth/login" in t
    assert "/api/auth/me" in t
    assert "AUTH-002 Session" in t
def test_internal_only_target_preserved():
    c=(ROOT/"docker"/"compose.yml").read_text(encoding="utf-8")
    assert "MESFLOW_QA_INTERNAL_URL: http://mesflow-app:8080" in c
