from pathlib import Path
import ast
ROOT=Path(__file__).resolve().parents[1]

def test_agent_internal_only_contract():
    t=(ROOT/"agent.py").read_text(encoding="utf-8")
    ast.parse(t)
    assert 'QA_INTERNAL_URL = os.environ.get("MESFLOW_QA_INTERNAL_URL", "http://mesflow-app:8080")' in t
    assert 'base["base_url"] = QA_INTERNAL_URL' in t
    assert '"--fallback-base-url", ""' in t

def test_ui_has_no_public_runtime_choice():
    h=(ROOT/"templates"/"index.html").read_text(encoding="utf-8")
    j=(ROOT/"static"/"app.js").read_text(encoding="utf-8")
    assert 'id="realtimePublicUrl" type="checkbox" disabled' in h
    assert 'use_public_url:false' in j

def test_compose_internal_network_target():
    c=(ROOT/"docker"/"compose.yml").read_text(encoding="utf-8")
    assert 'MESFLOW_QA_INTERNAL_ONLY: "1"' in c
    assert 'MESFLOW_QA_INTERNAL_URL: http://mesflow-app:8080' in c
    assert 'mesflow-edge' in c
