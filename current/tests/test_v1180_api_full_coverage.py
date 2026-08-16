from pathlib import Path
import json, sqlite3
import pytest
pytest.importorskip("flask")
pytest.importorskip("requests")
import agent
from version_contract import source_version


def setup_function():
    agent._runs.clear()
    agent._kiosk_state.clear()


def test_version_status_and_dashboard_render():
    c=agent.app.test_client()
    expected=source_version(Path(__file__).resolve().parents[1])
    r=c.get('/api/version'); assert r.status_code==200 and r.json['version']==expected
    r=c.get('/api/status'); assert r.status_code==200 and r.json['ok'] is True and r.json['version']==expected
    r=c.get('/'); assert r.status_code==200
    text=r.get_data(as_text=True); assert 'MESFlow QA Center' in text and '<style>' in text and '<script>' in text
    assert 'no-store' in r.headers.get('Cache-Control','')


def test_kiosk_heartbeat_and_status():
    c=agent.app.test_client()
    r=c.post('/api/kiosk/heartbeat',json={'kiosk_id':'K-01','state':'READY'}); assert r.status_code==200
    assert r.json['state']['kiosk_id']=='K-01' and r.json['state']['state']=='READY' and r.json['state']['last_seen']
    r=c.get('/api/kiosk/status'); assert len(r.json['kiosks'])==1 and r.json['kiosks'][0]['kiosk_id']=='K-01'


def test_logs_not_found_and_last_500():
    c=agent.app.test_client(); assert c.get('/api/runs/missing/logs').status_code==404
    st=agent.RunState('r1','functional'); st.log_lines=[str(i) for i in range(700)]; agent._runs['r1']=st
    r=c.get('/api/runs/r1/logs'); assert r.status_code==200 and len(r.json['lines'])==500 and r.json['lines'][0]=='200'


def test_config_roundtrip_and_corrupt_config_falls_back(tmp_path, monkeypatch):
    cfg=tmp_path/'config.json'; monkeypatch.setattr(agent,'CONFIG_PATH',cfg)
    agent.save_config({'base_url':'http://x','username':'u'})
    loaded=agent.load_config(); assert loaded['base_url']=='http://x' and loaded['username']=='u' and 'functional_paths' in loaded
    cfg.write_text('{broken',encoding='utf-8'); loaded=agent.load_config(); assert loaded['base_url']==agent.DEFAULT_CONFIG['base_url']
    c=agent.app.test_client(); r=c.post('/api/config',json={'duration_minutes':7}); assert r.status_code==200 and r.json['config']['duration_minutes']==7


def test_start_rejects_bad_type_and_dispatches_valid(monkeypatch):
    class FakeThread:
        def __init__(self, target=None, args=(), daemon=None): self.target=target; self.args=args
        def start(self): pass
    monkeypatch.setattr(agent.threading,'Thread',FakeThread)
    c=agent.app.test_client(); assert c.post('/api/start',json={'test_type':'BAD'}).status_code==400
    for kind in ['functional','api_soak','browser_visual','factory_simulation','realtime_soak']:
        r=c.post('/api/start',json={'test_type':kind}); assert r.status_code==200 and r.json['run']['test_type']==kind


def test_stop_unknown_and_running_process():
    class P:
        terminated=False
        def poll(self): return None
        def terminate(self): self.terminated=True
    c=agent.app.test_client(); assert c.post('/api/stop/missing').status_code==404
    st=agent.RunState('r1','realtime_soak'); p=P(); st.process=p; agent._runs['r1']=st
    r=c.post('/api/stop/r1'); assert r.status_code==200 and st.stop_event.is_set() and p.terminated


def test_database_overview_and_backup(tmp_path):
    db=tmp_path/'qa.sqlite'; con=sqlite3.connect(db)
    con.execute('create table production_orders(code text)'); con.execute("insert into production_orders values('RTSIM-001')")
    con.execute("insert into production_orders values('REAL-001')"); con.commit(); con.close()
    ov=agent.database_overview(str(db)); assert ov['connected'] and ov['integrity']=='ok' and ov['counts']['production_orders']==2 and ov['test_counts']['production_orders']==1
    old=agent.ROOT; agent.ROOT=tmp_path
    try:
        out=Path(agent.backup_database(str(db),'test')); assert out.exists()
    finally: agent.ROOT=old


def test_database_overview_missing():
    ov=agent.database_overview('Z:/not-found.sqlite'); assert ov['connected'] is False
