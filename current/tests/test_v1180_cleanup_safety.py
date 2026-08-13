import pytest
pytest.importorskip("flask")
pytest.importorskip("requests")
import agent

class Resp:
    def __init__(self,status=200,body=None): self.status_code=status; self._body=body or {}; self.text=str(self._body); self.content=b'1'; self.url='http://x'
    def json(self): return self._body

class FakeClient:
    def __init__(self): self.deleted=[]
    def get(self,url,**kw):
        if '/production-orders?' in url: return Resp(body={'items':[{'id':1,'code':'QAV65817-PO-1','status':'IN_PROGRESS'},{'id':2,'code':'REAL-PO-1'}]})
        if '/employees?' in url: return Resp(body={'items':[{'id':3,'employee_no':'QAV65817-EMP-001','name':'QA'},{'id':4,'employee_no':'EMP-REAL'}]})
        if '/stations?' in url: return Resp(body={'items':[{'id':5,'code':'QAV65817-ST-001','name':'QA'},{'id':6,'code':'ST-REAL'}]})
        return Resp(404,{})
    def delete(self,url,**kw): self.deleted.append((url,kw.get('json'))); return Resp(body={'ok':True,'counts':{}})


def setup_function(): agent._runs.clear()


def test_cleanup_requires_no_active_run():
    agent._runs['r']=agent.RunState('r','functional')
    r=agent.app.test_client().post('/api/cleanup',json={'confirm':'DELETE QA DATA'}); assert r.status_code==409


def test_cleanup_requires_exact_confirmation():
    r=agent.app.test_client().post('/api/cleanup',json={}); assert r.status_code==400 and r.json['error']=='CONFIRMATION_REQUIRED'


def test_cleanup_preview_never_includes_real_records(monkeypatch):
    f=FakeClient(); monkeypatch.setattr(agent,'load_config',lambda:{'base_url':'http://x','internal_base_url':'','username':'','password':''})
    monkeypatch.setattr(agent,'session_from_config',lambda cfg:f)
    r=agent.app.test_client().post('/api/cleanup',json={'preview':True}); assert r.status_code==200
    assert r.json['counts']=={'production_orders':1,'employees':1,'stations':1}
    assert r.json['preview']['production_orders'][0]['code'].startswith('QAV')
    assert not f.deleted


def test_cleanup_delete_order_and_scope(monkeypatch):
    f=FakeClient(); monkeypatch.setattr(agent,'load_config',lambda:{'base_url':'http://x','internal_base_url':'','username':'','password':''})
    monkeypatch.setattr(agent,'session_from_config',lambda cfg:f)
    r=agent.app.test_client().post('/api/cleanup',json={'confirm':'DELETE QA DATA'}); assert r.status_code==200 and r.json['counts']['failed']==0
    urls=[x[0] for x in f.deleted]
    assert urls[0].endswith('/api/production-orders/1/force') and urls[1].endswith('/api/employees/3') and urls[2].endswith('/api/stations/5')
    assert all('/2' not in x and '/4' not in x and '/6' not in x for x in urls)
