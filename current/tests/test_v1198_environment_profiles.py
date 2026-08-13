from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_profiles_select_configuration_without_forking_scenarios():
    source=(ROOT / "agent.py").read_text()
    compose=(ROOT / "docker/compose.yml").read_text()
    assert 'QA_PROFILE = os.environ.get("MESFLOW_QA_PROFILE", "LOCAL")' in source
    assert '{"LOCAL", "PRODUCTION_TEST"}' in source
    assert 'MESFLOW_QA_PROFILE: ${MESFLOW_QA_PROFILE:-LOCAL}' in compose
    assert '"profile": QA_PROFILE' in source
    assert 'os.environ.get("MESFLOW_QA_PASSWORD", "")' in source
