import json
from pathlib import Path
import agent

def test_release_profiles_are_named_and_required():
 assert set(agent.RELEASE_PROFILES)=={"release-local","release-test"}
 assert all(p["required"] for p in agent.RELEASE_PROFILES.values())

def test_release_run_rejects_unknown_profile():
 r=agent.app.test_client().post('/api/release-runs',json={'profile':'manual-pass','release_version':'70.0.0.1','artifact_digest':'sha256:'+'a'*64})
 assert r.status_code==400 and r.json['error']=='UNKNOWN_RELEASE_PROFILE'

def test_release_run_requires_immutable_digest():
 r=agent.app.test_client().post('/api/release-runs',json={'profile':'release-local','release_version':'70.0.0.1','artifact_digest':'latest'})
 assert r.status_code==400 and r.json['error']=='INVALID_RELEASE_IDENTITY'

def test_duplicate_active_release_run_is_rejected(monkeypatch,tmp_path):
 monkeypatch.setattr(agent,'LOG_DIR',tmp_path)
 monkeypatch.setattr(agent.threading,'Thread',lambda **kw:type('T',(),{'start':lambda self:None})())
 client=agent.app.test_client();payload={'profile':'release-local','release_version':'70.0.0.1','artifact_digest':'sha256:'+'a'*64}
 first=client.post('/api/release-runs',json=payload);second=client.post('/api/release-runs',json=payload)
 assert first.status_code==202 and second.status_code==409
 agent._runs.clear()

def test_release_run_detail_returns_structured_evidence(tmp_path,monkeypatch):
 monkeypatch.setattr(agent,'REPORT_DIR',tmp_path)
 state=agent.RunState('LOCAL-DETAIL','release_gate',status='PASSED',profile='release-local',release_version='70.0.0.1',artifact_digest='sha256:'+'a'*64,total=2,passed=2)
 state.report_file=str(tmp_path/'LOCAL-DETAIL.json');Path(state.report_file).write_text(json.dumps({**state.public(),'checks':[{'name':'health','status':'PASS','required':True}]}))
 agent._runs[state.run_id]=state
 body=agent.app.test_client().get('/api/runs/LOCAL-DETAIL').json['run']
 assert body['artifact_digest']==state.artifact_digest and body['checks'][0]['status']=='PASS'
 agent._runs.clear()
