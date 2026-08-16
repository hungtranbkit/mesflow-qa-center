from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_compose_can_render_before_credentials_are_configured():
    compose = (ROOT / "docker/compose.yml").read_text(encoding="utf-8")
    assert "MESFLOW_QA_PASSWORD: ${MESFLOW_QA_PASSWORD:-}" in compose
    assert "MESFLOW_QA_PASSWORD:?" not in compose


def test_missing_password_still_blocks_real_qa_runs():
    source = (ROOT / "agent.py").read_text(encoding="utf-8")
    assert "Chưa cấu hình mật khẩu MESFlow" in source
    assert 'finish(state,"FAILED"' in source
